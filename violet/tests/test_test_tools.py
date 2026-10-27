"""Phase 7 B.1: the local test tools (tools/*) -- isolation, the real-API gate, budgets, mock chat,
the 32-round evaluator and the model compatibility checker. Everything here is offline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


@pytest.fixture()
def clean_env():
    """The tools rewrite os.environ on purpose; put it back afterwards."""
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def workdir(tmp_path):
    return tmp_path / "violet-testchat-case"


def _args(workdir, *extra):
    from tools.violet_test_chat import build_parser
    return build_parser().parse_args(["--workdir", str(workdir), *extra])


def _inputs(*lines):
    it = iter(lines)

    def fn(prompt=""):
        try:
            value = next(it)
        except StopIteration:
            raise EOFError from None
        if isinstance(value, BaseException):
            raise value
        return value
    return fn


@pytest.fixture()
def session_factory(violetdb, clean_env, workdir):
    sessions = []

    def make(*extra, budget=None, input_fn=None):
        from tools.violet_test_chat import open_session
        args = _args(workdir, *extra)
        session = open_session(args, input_fn=input_fn or _inputs(), out=lambda s: None, db=violetdb)
        if budget is not None:
            session.recorder.budget = budget
        sessions.append(session)
        return session
    yield make
    for s in sessions:
        s.close()


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_prepare_environment_isolates_paths_and_strips_the_shell(workdir):
    from tools.harness import FORCED, MOCK_KEY, prepare_environment
    env = {"LLM_API_KEY": "sk-from-shell", "LLM_API_URL": "https://prod.example/v1", "MCP_SERVERS": "phone=http://x",
           "VIOLET_DB_PATH": str(PROJECT / "violet.db"), "VIOLET_WEB_ENABLED": "1", "VIOLET_IMAGE_ENABLED": "1",
           "HTTPS_PROXY": "http://proxy:1", "VIOLET_TEST_ALLOW_REAL_API": "1", "CHAT_LLM_MODEL": "shell-model",
           "VIOLET_MODEL_PARAMS_FILE": "/prod/model_params.json", "VIOLET_PROACTIVE_DAILY_LIMIT": "50",
           "LLM_REQUEST_TIMEOUT_SECONDS_X": "1"}
    out = prepare_environment(workdir, environ=env)
    assert out == workdir.resolve()
    assert env["LLM_API_KEY"] == MOCK_KEY and env["LLM_API_URL"].startswith("http://127.0.0.1")
    assert Path(env["VIOLET_DB_PATH"]).parent == out and Path(env["VIOLET_DATA_DIR"]).parent == out
    assert Path(env["VIOLET_MEDIA_DIR"]).parent == out
    assert Path(env["VIOLET_ENV_FILE"]).read_text(encoding="utf-8").startswith("#")          # an empty .env
    assert env["MCP_SERVERS"] == "" and env["VIOLET_WEB_ENABLED"] == "0" and env["VIOLET_IMAGE_ENABLED"] == "0"
    assert all(env[k] == v for k, v in FORCED.items())
    assert "HTTPS_PROXY" not in env
    assert env["VIOLET_TEST_ALLOW_REAL_API"] == "1"                                           # gate input survives
    assert env["CHAT_LLM_MODEL"] != "shell-model"                                         # models from env_example
    for leaked in ("VIOLET_MODEL_PARAMS_FILE", "VIOLET_PROACTIVE_DAILY_LIMIT", "LLM_REQUEST_TIMEOUT_SECONDS_X"):
        assert leaked not in env                                                          # shell settings dropped


@pytest.mark.parametrize("target", ["", "data", "data/sub", "violet.db"])
def test_prepare_environment_refuses_project_and_production_paths(target):
    from tools.harness import IsolationError, prepare_environment
    with pytest.raises(IsolationError):
        prepare_environment(PROJECT / target if target else PROJECT, environ={})


def test_config_file_may_set_models_but_never_keys(workdir, tmp_path):
    from tools.harness import IsolationError, prepare_environment
    good = tmp_path / "violet_test.env"
    good.write_text("CHAT_LLM_MODEL=model-under-test\nVIOLET_WEB_ENABLED=1\nCHAT_LLM_MODEL_TEMPERATURE=0.7\n", encoding="utf-8")
    env = {}
    prepare_environment(workdir, good, environ=env)
    assert env["CHAT_LLM_MODEL"] == "model-under-test" and env["CHAT_LLM_MODEL_TEMPERATURE"] == "0.7"
    assert env["VIOLET_WEB_ENABLED"] == "0"                                                  # forced off anyway
    bad = tmp_path / "bad.env"
    bad.write_text("LLM_API_KEY=sk-should-not-be-here\n", encoding="utf-8")
    with pytest.raises(IsolationError):
        prepare_environment(workdir, bad, environ={})


def test_tool_modules_import_nothing_from_eve_before_isolation():
    code = ("import sys, tools.violet_test_chat, tools.dialogue_eval, tools.model_compat;"
            "print(sorted(m for m in sys.modules if m == 'ai' or m.startswith(('ai.', 'violet_runtime', 'database'))))")
    out = subprocess.run([PYTHON, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_a_real_env_file_in_the_shell_is_never_read(tmp_path):
    leak = tmp_path / "prod.env"
    leak.write_text("LLM_API_KEY=sk-production-should-never-load\nLLM_API_URL=https://prod.example/v1\n", encoding="utf-8")
    code = ("import os; from tools.harness import prepare_environment; "
            f"w = prepare_environment(r'{tmp_path / 'violet-testchat-sub'}'); "
            "from ai.settings import ENV_PATH; from ai.chat import get_llm_api_key, get_llm_api_url; "
            "from ai.agents.base_agent import _ENV_PATH; from dotenv import load_dotenv; load_dotenv(_ENV_PATH, override=True); "
            "print(ENV_PATH == w / 'empty.env', get_llm_api_key(), get_llm_api_url())")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env["VIOLET_ENV_FILE"] = str(leak)
    out = subprocess.run([PYTHON, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1].split() == ["True", "mock-key-not-a-real-key", "http://127.0.0.1:9/v1"]


def test_cleanup_only_deletes_test_directories(tmp_path, workdir):
    from tools.harness import IsolationError, cleanup, prepare_environment
    other = tmp_path / "precious"
    other.mkdir()
    with pytest.raises(IsolationError):
        cleanup(other)
    prepare_environment(workdir, environ={})
    cleanup(workdir)
    assert not workdir.exists() and other.exists()


# ---------------------------------------------------------------------------
# Real API gate
# ---------------------------------------------------------------------------

def _gate(env, answers=("yes",), key=""):
    from tools.harness import RealApiGate
    return RealApiGate(env, input_fn=_inputs(*answers), key_prompt=lambda prompt: key)


def test_gate_stays_closed_without_every_authorization():
    from tools.harness import Budget
    b, models = Budget(), {"chat": "m"}
    assert _gate({}).open(False, None, b, models, out=lambda s: None) is None               # mock: no --real
    with pytest.raises(PermissionError, match="VIOLET_TEST_ALLOW_REAL_API"):
        _gate({"VIOLET_TEST_API_KEY": "sk-test-abcdefgh123"}).open(True, "https://t.example/v1", b, models, out=lambda s: None)
    with pytest.raises(PermissionError, match="endpoint"):
        _gate({"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": "sk-test-abcdefgh123"}).open(True, None, b, models, out=lambda s: None)
    with pytest.raises(PermissionError, match="no test key"):
        _gate({"VIOLET_TEST_ALLOW_REAL_API": "1"}).open(True, "https://t.example/v1", b, models, out=lambda s: None)
    env = {"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": "sk-test-abcdefgh123", "LLM_API_KEY": "mock"}
    with pytest.raises(PermissionError, match="not confirmed"):
        _gate(env, answers=("no",)).open(True, "https://t.example/v1", b, models, out=lambda s: None)
    assert env["LLM_API_KEY"] == "mock"                                                   # nothing switched


def test_gate_prints_models_endpoint_budget_and_a_masked_key():
    from tools.harness import Budget
    lines = []
    env = {"VIOLET_TEST_ALLOW_REAL_API": "1"}
    key = _gate(env, key="sk-test-0123456789abcdef").open(
        True, "https://t.example/v1", Budget(max_calls_per_turn=4, max_calls_total=40), {"chat": "model-x"},
        out=lines.append)
    text = "\n".join(lines)
    assert key == "sk-test-0123456789abcdef" and env["LLM_API_KEY"] == key and env["LLM_API_URL"] == "https://t.example/v1"
    assert "https://t.example/v1" in text and "model-x" in text and "4 model calls per round" in text
    assert "price unknown" in text and key not in text and "sk-" in text


def test_cli_refuses_real_mode_without_authorization(clean_env, workdir, monkeypatch, capsys):
    import openai
    from tools import violet_test_chat
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: pytest.fail("no real client may be created"))
    clean_env.pop("VIOLET_TEST_ALLOW_REAL_API", None)
    assert violet_test_chat.main(["--real", "--api-url", "https://t.example/v1", "--workdir", str(workdir)]) == 2
    assert "未启动" in capsys.readouterr().out


def test_mock_mode_never_builds_a_real_client(session_factory, monkeypatch):
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: pytest.fail("mock mode built a real client"))
    session = session_factory()
    assert session.real is False
    assert session.say("你好")["violet"]


def test_real_mode_builds_a_budgeted_client_with_the_test_key(session_factory, monkeypatch, clean_env):
    """With full authorization the agents get OpenAI(test key, test url, max_retries=0) behind the budget."""
    import openai
    from tools.harness import MockLLM
    built = []

    class FakeOpenAI(MockLLM):
        def __init__(self, **kwargs):
            built.append(kwargs)
            super().__init__()
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    clean_env.update({"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": "sk-test-only-0123456789"})
    session = session_factory("--real", "--api-url", "https://t.example/v1", input_fn=_inputs("yes"))
    result = session.say("你好")
    assert session.real and result["violet"] and built
    assert all(b["api_key"] == "sk-test-only-0123456789" and b["base_url"] == "https://t.example/v1"
               and b["max_retries"] == 0 for b in built)
    assert len(session.recorder.calls) == len(result["calls"]) >= 2


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class _CountingInner:
    def __init__(self):
        from tools.harness import MockLLM
        self.mock = MockLLM()
        self.n = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.n += 1
        return self.mock.chat.completions.create(**kwargs)


def test_budgeted_client_refuses_before_the_call_is_made():
    from tools.harness import Budget, BudgetedClient, BudgetExceeded, CallRecorder
    inner = _CountingInner()
    rec = CallRecorder(Budget(max_calls_per_turn=2, max_calls_total=3))
    client = BudgetedClient(inner, rec)
    rec.start_round(1)
    client.chat.completions.create(model="m", messages=[])
    client.chat.completions.create(model="m", messages=[])
    with pytest.raises(BudgetExceeded, match="per-round"):
        client.chat.completions.create(model="m", messages=[])
    assert inner.n == 2 and rec.stopped is None
    rec.start_round(2)
    client.chat.completions.create(model="m", messages=[])
    with pytest.raises(BudgetExceeded, match="total call cap"):
        client.chat.completions.create(model="m", messages=[])
    rec.start_round(3)
    with pytest.raises(BudgetExceeded):
        client.chat.completions.create(model="m", messages=[])                              # stays stopped
    assert inner.n == 3 and rec.stopped


@pytest.mark.parametrize("budget,expect", [
    # no reserve beyond the observed call size: the cap is reached by reported usage (B.10.1 reserves: test_budget_b101)
    (dict(max_tokens_total=30, first_call_reserve=0, reserve_factor=1.0), "token cap"),
    (dict(price_per_1k_tokens=1.0, max_cost=0.03), "cost cap"),
])
def test_token_and_cost_caps(budget, expect):
    from tools.harness import Budget, BudgetedClient, BudgetExceeded, CallRecorder
    inner = _CountingInner()
    rec = CallRecorder(Budget(**budget))
    client = BudgetedClient(inner, rec)
    rec.start_round(1)
    with pytest.raises(BudgetExceeded, match=expect):
        for _ in range(10):
            client.chat.completions.create(model="m", messages=[])
    assert inner.n == 2                                                                    # 15 tokens per mock call


def test_budget_stops_a_real_session_and_eve_degrades_without_crashing(session_factory):
    from tools.harness import Budget
    session = session_factory(budget=Budget(max_calls_per_turn=1, max_calls_total=3))
    first = session.say("你好", 1)
    assert len(first["calls"]) == 1                                                        # intent ran, chat refused
    session.say("在吗", 2)
    session.say("还在吗", 3)
    assert session.recorder.stopped and "total call cap" in session.recorder.stopped
    fourth = session.say("最后一句", 4)
    assert fourth["calls"] == [] and len(session.recorder.calls) == 3
    rows = session.db.fetch_all("SELECT role FROM messages WHERE role = 'user'")
    assert len(rows) == 4                                                                   # every user line still stored


def test_errors_and_reports_never_contain_the_key(tmp_path):
    from tools.harness import Budget, BudgetedClient, CallRecorder, write_report
    key = "sk-test-secret-abcdef123456"

    class Failing:
        chat = SimpleNamespace(completions=SimpleNamespace(
            create=lambda **k: (_ for _ in ()).throw(RuntimeError(f"401 bad key {key}"))))
    rec = CallRecorder(Budget(), secrets=[key])
    with pytest.raises(RuntimeError):
        BudgetedClient(Failing(), rec).chat.completions.create(model="m", messages=[])
    assert key not in rec.calls[0].error and "<redacted>" in rec.calls[0].error
    path = write_report("r.json", {"note": f"key was {key}", "calls": [c.__dict__ for c in rec.calls]}, rec, tmp_path)
    assert key not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Mock chat through the real pipeline
# ---------------------------------------------------------------------------

def test_mock_chat_runs_the_real_pipeline_end_to_end(session_factory):
    session = session_factory()
    from tools.violet_test_chat import run_interactive
    out = []
    result = run_interactive(session, input_fn=_inputs("我叫小满", "/advance 2", "/proactive", "/state", "/usage",
                                                       "/score 2 很自然", "/score 9", "/quit"), out=out.append)
    text = "\n".join(out)
    assert "Violet> （模拟回复）收到：我叫小满" in text
    agents = {c["agent"] for c in result["transcript"][0]["calls"]}
    assert {"IntentAgent", "ChatAgent", "MemoryAgent"} <= agents                          # the real three-stage turn
    assert result["transcript"][0]["score"] == 2 and result["transcript"][0]["note"] == "很自然"
    assert any("advance_hours" in t for t in result["transcript"]) and any("proactive" in t for t in result["transcript"])
    roles = [r["role"] for r in session.db.fetch_all("SELECT role FROM messages ORDER BY id")]
    assert roles == ["user", "assistant"]
    assert session.wechat.sent and "调用" in text and "用法" in text


def test_chat_cli_saves_a_transcript_and_removes_its_test_data(violetdb, clean_env, workdir, tmp_path, monkeypatch,
                                                                 capsys):
    from tools import violet_test_chat, harness
    monkeypatch.setattr(harness, "OUTPUT_DIR", tmp_path / "out")
    assert violet_test_chat.main(["--workdir", str(workdir)], input_fn=_inputs("你好", "/quit")) == 0
    transcript = next((tmp_path / "out").rglob("chat_transcript.json"))
    data = json.loads(transcript.read_text(encoding="utf-8"))
    assert data["real"] is False and data["transcript"][0]["user"] == "你好"
    assert not workdir.exists() and "测试数据目录已删除" in capsys.readouterr().out
    assert violet_test_chat.main(["--workdir", str(workdir), "--keep"], input_fn=_inputs("/quit")) == 0
    assert workdir.exists()


def test_interactive_chat_stops_cleanly_on_ctrl_c(session_factory):
    from tools.violet_test_chat import run_interactive
    session = session_factory()
    out = []
    result = run_interactive(session, input_fn=_inputs("你好", KeyboardInterrupt()), out=out.append)
    assert "已停止" in out[-1] and len([t for t in result["transcript"] if "user" in t]) == 1


def test_advance_time_moves_every_recorded_timestamp(violetdb):
    from tools.harness import advance_time
    violetdb.add_user_message("text", "测试")
    before = violetdb.fetch_one("SELECT created_at FROM messages")["created_at"]
    advance_time(violetdb, 3)
    after = violetdb.fetch_one("SELECT created_at FROM messages")["created_at"]
    assert datetime.fromisoformat(before) - datetime.fromisoformat(after) == timedelta(hours=3)


def test_no_memory_mode_stores_nothing(session_factory):
    session = session_factory("--no-memory")
    session.say("我下周三有期中考试")
    assert session.db.fetch_all("SELECT id FROM event_memories") == []


# ---------------------------------------------------------------------------
# 32-round evaluator
# ---------------------------------------------------------------------------

def test_full_32_round_plan_runs_in_mock_mode(session_factory):
    from tools.dialogue_eval import PLAN_PATH, run_plan, summarize
    session = session_factory("--quiet-hours", "off")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    records = run_plan(session, plan, set(range(1, 33)), scripted_memory=True, out=lambda s: None)
    assert [r["n"] for r in records] == list(range(1, 33))
    assert all(r["violet"] for r in records) and all(not r["errors"] for r in records)
    by_n = {r["n"]: r for r in records}
    assert by_n[9]["proactive"][0]["check"] == "blocked_busy" and by_n[9]["proactive"][0]["ok"] is True
    assert "busy" in by_n[9]["proactive"][0]["due_reason"]                                 # blocked for the right reason
    assert any(p["check"] == "ended_event_not_allowed" and p["ok"] for p in by_n[16]["proactive"])
    assert by_n[30]["auto"]["recall_needed"] is True                                     # evidence for R-M2
    facts = {f["value"] for f in session.db.fetch_all("SELECT value FROM user_profile_facts WHERE status = 'active'")}
    assert "物理" in facts and "天文" not in facts                                        # the correction took
    assert session.db.fetch_all("SELECT id FROM life_events")                             # Violet's preloaded past
    summary = summarize({"rounds": records})
    assert summary["rounds"] == 32 and summary["verdict"].startswith("incomplete")
    assert summary["calls_per_round"] <= 15 and summary["api_error_rate"] == 0.0


def test_evaluator_keeps_finished_rounds_when_stopped(session_factory):
    from tools.dialogue_eval import PLAN_PATH, run_plan
    session = session_factory()
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    records, sent = [], []

    def manual(rnd):
        if rnd["n"] == 3:
            raise KeyboardInterrupt
        sent.append(rnd["n"])
        return "" if rnd["n"] == 1 else "换个说法：" + rnd["user"]
    with pytest.raises(KeyboardInterrupt):
        run_plan(session, plan, {1, 2, 3, 4}, False, out=lambda s: None, records=records, manual_fn=manual)
    assert [r["n"] for r in records] == [1, 2]
    assert records[0]["user"] == records[0]["planned_user"] and records[1]["user"].startswith("换个说法")


def _round(n, score=None, question=False, flags=(), busy_sent=False, recall=False, searched=False):
    return {"n": n, "violet": ["好的？" if question else "好的"], "calls": 3, "tokens": 100, "errors": [],
            "reply_latency_ms": 900, "score": score, "note": "",
            "proactive": [{"check": "blocked_busy", "sent": ["x"] if busy_sent else []}] if n == 9 else [],
            "auto": {"ends_with_question": question, "consistency_flags": list(flags), "mentions_ended_event": [],
                     "recall_needed": recall, "intent_searched_memory": searched}}


def test_summary_verdicts():
    from tools.dialogue_eval import summarize
    good = {"rounds": [_round(n, 2, recall=n == 30, searched=True) for n in range(1, 33)]}
    assert summarize(good)["verdict"] == "pass"
    assert summarize(good)["recall_searched"] == 1
    zero_critical = {"rounds": [_round(n, 0 if n == 14 else 2) for n in range(1, 33)]}
    assert summarize(zero_critical)["verdict"] == "fail" and summarize(zero_critical)["critical_rounds_with_zero"] == [14]
    low = {"rounds": [_round(n, 1) for n in range(1, 33)]}
    assert summarize(low)["verdict"] == "fail"
    questions = {"rounds": [_round(n, 2, question=n in (3, 4, 5)) for n in range(1, 33)]}
    assert summarize(questions)["longest_question_streak"] == 3 and summarize(questions)["verdict"] == "fail"
    busy = {"rounds": [_round(n, 2, busy_sent=True) for n in range(1, 33)]}
    assert summarize(busy)["proactive_sent_while_busy"] == 1 and summarize(busy)["verdict"] == "fail"


def test_score_sheet_round_trip(tmp_path):
    from tools.dialogue_eval import load_scores, summarize
    sheet = tmp_path / "score_sheet.csv"
    sheet.write_text("round,user,violet,rubric,auto_flags,score(0/1/2),note\n1,a,b,c,,2,自然\n2,a,b,c,,,\n",
                     encoding="utf-8-sig")
    scores = load_scores(sheet)
    assert scores == {1: (2, "自然")}
    result = summarize({"rounds": [_round(1), _round(2)]}, scores)
    assert result["scored"] == 1 and result["verdict"].startswith("incomplete")
    sheet.write_text("round,user,violet,rubric,auto_flags,score(0/1/2),note\n1,a,b,c,,5,\n", encoding="utf-8-sig")
    with pytest.raises(ValueError):
        load_scores(sheet)


def test_evaluator_cli_writes_report_and_score_sheet(clean_env, workdir, tmp_path, monkeypatch, capsys):
    from tools import dialogue_eval, harness
    monkeypatch.setattr(dialogue_eval, "OUTPUT_DIR", tmp_path / "out")
    from ai.chat import violetdb
    from tools import violet_test_chat
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input: real_open(args, input_fn=input_fn, out=lambda s: None, db=violetdb))
    assert dialogue_eval.main(["--rounds", "1-2", "--scripted-memory", "--workdir", str(workdir)]) == 0
    run_dir = next((tmp_path / "out").iterdir())
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert [r["n"] for r in report["rounds"]] == [1, 2] and report["real"] is False
    assert (run_dir / "score_sheet.csv").read_text(encoding="utf-8-sig").startswith("round,user,violet")
    assert not workdir.exists()                                                           # cleaned up
    assert dialogue_eval.main(["--summarize", str(run_dir / "report.json")]) == 0
    assert json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["rounds"] == 2
    assert harness.OUTPUT_DIR != tmp_path                                                  # untouched


# ---------------------------------------------------------------------------
# Model compatibility checker
# ---------------------------------------------------------------------------

def test_compat_checker_against_mock_providers(tmp_path):
    from tools.model_compat import CompatClient, check_model, mock_transport
    tx = mock_transport()
    std = check_model(CompatClient("https://mock.invalid/v1", "k", tx), "mock-standard", include_timeout=True)
    assert all(c["ok"] for c in std["checks"].values())
    assert std["suggested_policy"]["max_tokens_field"] == "max_tokens" and std["suggested_policy"]["send_temperature"]
    rsn = check_model(CompatClient("https://mock.invalid/v1", "k", tx), "mock-reasoning")
    assert rsn["checks"]["max_tokens"]["kind"] == "bad_request" and rsn["checks"]["max_tokens"]["retryable"] is False
    assert rsn["suggested_policy"]["max_tokens_field"] == "max_completion_tokens"
    assert rsn["suggested_policy"]["send_temperature"] is False
    nt = check_model(CompatClient("https://mock.invalid/v1", "k", tx), "mock-no-tools")
    assert nt["checks"]["tools"]["ok"] is False and nt["suggested_policy"]["_tools"] is False
    flaky = check_model(CompatClient("https://mock.invalid/v1", "k", tx), "mock-flaky")
    kinds = {c.get("kind") for c in flaky["checks"].values()}
    assert {"rate_limited", "server_error", "timeout"} <= kinds and flaky["suggested_policy"] is None
    assert all(c["retryable"] for c in flaky["checks"].values() if c.get("kind") in ("rate_limited", "server_error", "timeout"))


def test_compat_request_cap_is_hard():
    from tools.model_compat import CompatClient, check_model, mock_transport
    client = CompatClient("https://mock.invalid/v1", "k", mock_transport(), max_requests=3)
    result = check_model(client, "mock-standard")
    assert client.requests == 3
    assert sum(1 for c in result["checks"].values() if c.get("skipped")) >= 5


def test_compat_cli_mock_mode_and_policy_guard(tmp_path, capsys):
    from tools import model_compat
    env = {}
    assert model_compat.main(["--out", str(tmp_path)], environ=env) == 0
    report = json.loads((tmp_path / "model_compat.json").read_text(encoding="utf-8"))
    assert report["real"] is False and len(report["results"]) == 4
    assert model_compat.main(["--write-policy", "--out", str(tmp_path)], environ={}) == 2   # mock never writes
    assert model_compat.main(["--model", "grok-4.2", "--out", str(tmp_path)], environ={}) == 2   # no silent real call
    assert model_compat.main(["--real", "--api-url", "https://t.example/v1", "--model", "m"], environ={}) == 2


def test_compat_cli_real_mode_redacts_key_and_writes_policy_only_after_yes(tmp_path):
    from tools import model_compat
    from tools.model_compat import mock_transport
    key = "sk-test-compat-0123456789"
    env = {"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": key}
    policy = tmp_path / "model_params.json"
    policy.write_text(json.dumps({"defaults": {"send_temperature": True}, "models": {}}), encoding="utf-8")
    import ai.model_params as mp
    orig = mp.policy_path
    mp.policy_path = lambda: policy
    try:
        rc = model_compat.main(["--real", "--api-url", "https://mock.invalid/v1", "--model", "mock-reasoning",
                                "--yes", "--write-policy", "--out", str(tmp_path)],
                               input_fn=_inputs("yes"), transport=mock_transport(), environ=env)
    finally:
        mp.policy_path = orig
    assert rc == 0
    assert key not in (tmp_path / "model_compat.json").read_text(encoding="utf-8")
    data = json.loads(policy.read_text(encoding="utf-8"))
    entry = data["models"]["mock-reasoning"]
    assert data["defaults"] == {"send_temperature": True}
    assert entry["max_tokens_field"] == "max_completion_tokens" and entry["send_temperature"] is False
    assert "_tools" not in entry and "tools.model_compat" in entry["_verified"]


def test_compat_errors_never_carry_the_key(tmp_path, monkeypatch):
    import httpx
    from tools import model_compat
    from tools.model_compat import CompatClient
    key = "sk-test-echoed-0123456789"
    echo = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": f"invalid key {key}"}}))
    result = CompatClient("https://mock.invalid/v1", key, echo).post_classified({"model": "m", "messages": []})
    assert key not in result["error"] and result["kind"] == "bad_request"
    # the report is scrubbed a second time, whatever ends up in it
    monkeypatch.setattr(model_compat, "check_model",
                        lambda client, model, include_timeout=False: {"model": model, "suggested_policy": None,
                                                                      "checks": {"basic": {"ok": False, "error": key}}})
    env = {"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": key}
    assert model_compat.main(["--real", "--api-url", "https://mock.invalid/v1", "--model", "m", "--yes",
                              "--out", str(tmp_path)], environ=env) == 0
    assert key not in (tmp_path / "model_compat.json").read_text(encoding="utf-8")


def test_compat_policy_is_not_written_without_yes(tmp_path, monkeypatch):
    import ai.model_params as mp
    from tools import model_compat
    from tools.model_compat import mock_transport
    policy = tmp_path / "model_params.json"
    policy.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mp, "policy_path", lambda: policy)
    env = {"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": "sk-test-compat-0123456789"}
    assert model_compat.main(["--real", "--api-url", "https://mock.invalid/v1", "--model", "mock-standard", "--yes",
                              "--write-policy", "--out", str(tmp_path)],
                             input_fn=_inputs("no"), transport=mock_transport(), environ=env) == 0
    assert policy.read_text(encoding="utf-8") == "{}"


def test_suggested_policy_is_accepted_by_eve_model_params(tmp_path, monkeypatch):
    from ai import model_params
    from tools.model_compat import write_policy
    path = tmp_path / "p.json"
    write_policy([{"model": "m-x", "suggested_policy": {"send_temperature": False, "max_tokens_field": "max_completion_tokens",
                                                         "allowed_extra_params": [], "_tools": True}}], "test", path)
    monkeypatch.setenv("VIOLET_MODEL_PARAMS_FILE", str(path))
    if hasattr(model_params, "_cache"):
        monkeypatch.setattr(model_params, "_cache", None, raising=False)
    out = model_params.apply({"model": "m-x", "temperature": 0.5, "max_tokens": 100, "messages": []})
    assert "temperature" not in out and out.get("max_completion_tokens") == 100 and "max_tokens" not in out
