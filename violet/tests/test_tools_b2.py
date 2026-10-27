"""Phase 7 B.2 part 1: per-call report details (M1), test-directory cleanup (M2) and the semantic
round-3 context check (M3). Offline only: mock model, mock providers, temp directories."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PRIVATE_TEXT = "我在星河大学读大二，学的是天文"


@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def workdir(tmp_path):
    return tmp_path / "violet-testchat-b2"


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

    def make(*extra, input_fn=None):
        from tools.violet_test_chat import build_parser, open_session
        args = build_parser().parse_args(["--workdir", str(workdir), *extra])
        session = open_session(args, input_fn=input_fn or _inputs(), out=lambda s: None, db=violetdb)
        sessions.append(session)
        return session
    yield make
    for s in sessions:
        s.close()


def _completion(content=None, tool_calls=None, usage=None, model="m"):
    from openai.types.chat import ChatCompletion
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                                 for i, (name, args) in enumerate(tool_calls)]
    data = {"id": "x", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}]}
    if usage is not None:
        data["usage"] = usage
    return ChatCompletion.model_validate(data)


def _scripted_inner(monkeypatch, scripts: dict[str, list]):
    """Replace the mock model: per agent, a list of completions (then the normal mock answer)."""
    from tools import harness

    class Scripted(harness.MockLLM):
        def _create(self, **kwargs):
            queue = scripts.get(harness._AGENT.get())
            if queue:
                return queue.pop(0)
            return super()._create(**kwargs)
    monkeypatch.setattr(harness, "MockLLM", Scripted)


# ---------------------------------------------------------------------------
# M1: call details
# ---------------------------------------------------------------------------

def test_call_details_are_allow_listed_and_content_free(session_factory):
    from tools.harness import DETAIL_FIELDS
    session = session_factory()
    result = session.say(PRIVATE_TEXT, 1)
    details = result["calls"]
    assert details and all(set(d) == set(DETAIL_FIELDS) for d in details)
    dumped = json.dumps(details, ensure_ascii=False)
    for private in (PRIVATE_TEXT, "星河大学", "（模拟回复）", "mock-key-not-a-real-key"):
        assert private not in dumped
    assert [d["call_no"] for d in details] == list(range(1, len(details) + 1))
    assert [d["round_call_no"] for d in details] == list(range(1, len(details) + 1))
    assert all(d["round"] == 1 and d["messages"] > 0 and d["request_chars"] > 0 for d in details)
    memory = [d for d in details if d["agent"] == "MemoryAgent"]
    assert memory and memory[0]["tools"] > 0 and memory[0]["tools_chars"] > 0 and memory[0]["step"] == 1
    first = details[0]
    assert (first["prompt_tokens"], first["completion_tokens"], first["total_tokens"]) == (10, 5, 15)
    assert first["cached_tokens"] is None and first["reasoning_tokens"] is None and first["usage_present"] is True
    runs = result["runs"]
    assert {r["agent"] for r in runs} >= {"IntentAgent", "ChatAgent", "MemoryAgent"}
    chat_run = next(r for r in runs if r["agent"] == "ChatAgent")
    assert set(chat_run["context"]) <= {"mode", "sections", "fact_ids", "situation_ids", "history_messages",
                                         "trimmed_items", "approx_tokens"}
    assert all(isinstance(i, int) for i in chat_run["context"].get("situation_ids") or [])
    second = session.say("在吗", 2)["calls"]
    assert [d["round_call_no"] for d in second] == list(range(1, len(second) + 1))
    assert second[0]["call_no"] == len(details) + 1


def test_usage_fields_are_reported_never_inferred(session_factory, monkeypatch):
    _scripted_inner(monkeypatch, {
        "IntentAgent": [_completion(json.dumps({"route": "chat", "should_reply": True}))],            # no usage
        "ChatAgent": [_completion("好呀", usage={"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 140,
                                                "prompt_tokens_details": {"cached_tokens": 64},
                                                "completion_tokens_details": {"reasoning_tokens": 30}})],
    })
    session = session_factory()
    details = session.say("你好", 1)["calls"]
    intent = next(d for d in details if d["agent"] == "IntentAgent")
    chat = next(d for d in details if d["agent"] == "ChatAgent")
    assert intent["usage_present"] is False
    assert all(intent[k] is None for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
                                           "reasoning_tokens"))
    assert (chat["prompt_tokens"], chat["completion_tokens"], chat["total_tokens"]) == (100, 7, 140)
    assert (chat["cached_tokens"], chat["reasoning_tokens"]) == (64, 30)
    from tools.violet_test_chat import usage_line
    from tools.harness import summarize_calls
    summary = summarize_calls(session.recorder.calls, session.recorder.runs)
    assert summary["usage_missing_calls"] == 1
    assert summary["total_vs_prompt_plus_completion"]["calls_differing"] >= 1               # 140 != 107: counted only
    assert summary["by_agent"]["ChatAgent"]["cached_tokens"] == {"sum": 64, "calls_reporting": 1}
    assert "没有返回 usage" in usage_line(session.recorder)
    assert session.recorder.tokens == sum(c.billed for c in session.recorder.calls)


def test_tool_results_are_summarized_without_private_text(session_factory, monkeypatch, violetdb):
    violetdb.add_event_memory("用户下周三有天文观测课程答辩", category="event")
    _scripted_inner(monkeypatch, {"MemoryAgent": [
        _completion(tool_calls=[("query_memory", {"query": "答辩 观测"})]),
        _completion(tool_calls=[("remember_profile_fact", {"key": "study.major", "value": "天文",
                                                           "evidence": "学的是天文"})]),
        _completion("done"),
    ]})
    session = session_factory()
    details = session.say(PRIVATE_TEXT, 1)["calls"]
    memory = [d for d in details if d["agent"] == "MemoryAgent"]
    assert [d["tool_calls"] for d in memory] == [["query_memory"], ["remember_profile_fact"], []]
    query = memory[0]["tool_results"][0]
    assert query["tool"] == "query_memory" and query["memories_count"] == 1 and query["memories_ids"]
    fact = memory[1]["tool_results"][0]
    assert fact == {"tool": "remember_profile_fact", "key": "study.major", "ok": True, "action": "created",
                    "status": "active", "fact_id": fact["fact_id"]}
    dumped = json.dumps(details, ensure_ascii=False)
    assert "天文" not in dumped and "答辩" not in dumped and "学的是" not in dumped


def test_tool_result_summary_drops_free_text():
    from tools.harness import summarize_tool_result
    info = summarize_tool_result("save_memory", {"summary": "私人内容", "key": "不是 schema key"},
                                 json.dumps({"ok": False, "error": "summary 为空，私人内容", "reason": "中文原因"},
                                            ensure_ascii=False))
    assert info == {"tool": "save_memory", "ok": False, "has_error_text": True}
    assert summarize_tool_result("x", {}, "plain text")["result"] == "non_json"


def test_errors_are_typed_and_free_text_stays_out_of_details():
    from tools.harness import Budget, BudgetedClient, CallRecorder, call_detail

    class Failing(Exception):
        status_code = 429

    class Inner:
        chat = SimpleNamespace(completions=SimpleNamespace(
            create=lambda **k: (_ for _ in ()).throw(Failing("rate limited for 私人内容"))))
    rec = CallRecorder(Budget())
    with pytest.raises(Failing):
        BudgetedClient(Inner(), rec).chat.completions.create(model="m", messages=[{"role": "user", "content": "私人内容"}])
    detail = call_detail(rec.calls[0])
    assert detail["error_type"] == "Failing" and detail["status_code"] == 429 and detail["ok"] is False
    assert "私人内容" not in json.dumps(detail, ensure_ascii=False) and "error" not in detail


def test_fit_is_reported_as_a_statistic_only():
    from tools.harness import fit_prompt_tokens
    rows = [{"request_chars": x, "tools_chars": 0, "prompt_tokens": int(4000 + 0.5 * x)} for x in (1000, 3000, 9000)]
    fit = fit_prompt_tokens(rows)
    assert abs(fit["intercept_tokens"] - 4000) < 2 and abs(fit["slope_tokens_per_char"] - 0.5) < 0.01
    assert "NOT a confirmed" in fit["note"]
    assert "no fit" in fit_prompt_tokens(rows[:2])["note"]
    assert "no fit" in fit_prompt_tokens([dict(rows[0]) for _ in range(3)])["note"]


def test_dialogue_report_contains_details_but_no_key_or_private_call_text(clean_env, violetdb, workdir, tmp_path,
                                                                         monkeypatch):
    from tools import dialogue_eval, violet_test_chat
    monkeypatch.setattr(dialogue_eval, "OUTPUT_DIR", tmp_path / "out")
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input: real_open(args, input_fn=input_fn, out=lambda s: None, db=violetdb))
    assert dialogue_eval.main(["--rounds", "1-3", "--workdir", str(workdir)]) == 0
    report = json.loads(next((tmp_path / "out").rglob("report.json")).read_text(encoding="utf-8"))
    assert report["call_summary"]["calls"] == sum(r["calls"] for r in report["rounds"])
    assert set(report["call_summary"]["by_agent"]) >= {"IntentAgent", "ChatAgent", "MemoryAgent"}
    for r in report["rounds"]:
        assert len(r["call_details"]) == r["calls"] and r["runs"]
        dumped = json.dumps(r["call_details"], ensure_ascii=False) + json.dumps(r["runs"], ensure_ascii=False)
        assert r["user"] not in dumped and "（模拟回复）" not in dumped


def test_compat_report_has_token_fields_without_inventing_them(tmp_path):
    import httpx
    from tools.model_compat import CompatClient, check_model, mock_transport
    std = check_model(CompatClient("https://mock.invalid/v1", "k", mock_transport()), "mock-standard")
    basic = std["checks"]["basic"]
    assert basic["total_tokens"] == 12 and basic["tokens"] == 12
    assert basic["prompt_tokens"] is None and basic["completion_tokens"] is None              # mock reports total only
    assert basic["usage_present"] is True and basic["request_chars"] > 0 and basic["messages"] == 1
    assert std["checks"]["tools"]["tools"] == 1 and std["checks"]["tools"]["tools_chars"] > 0
    assert std["summary"]["tokens"]["total_tokens"]["calls_reporting"] >= 9
    assert std["summary"]["tokens"]["prompt_tokens"] == {"sum": None, "calls_reporting": 0}

    def detailed(request):
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "好"}}],
                                         "usage": {"prompt_tokens": 4390, "completion_tokens": 7, "total_tokens": 4397,
                                                   "prompt_tokens_details": {"cached_tokens": 4352}}})
    res = CompatClient("https://mock.invalid/v1", "k", httpx.MockTransport(detailed)).post({"model": "m", "messages": []})
    from tools.model_compat import _usage
    assert _usage(res) == {"usage_present": True, "prompt_tokens": 4390, "completion_tokens": 7, "total_tokens": 4397,
                           "cached_tokens": 4352, "reasoning_tokens": None}

    def no_usage(request):
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "好"}}]})
    res = CompatClient("https://mock.invalid/v1", "k", httpx.MockTransport(no_usage)).post({"model": "m", "messages": []})
    assert _usage(res)["usage_present"] is False and _usage(res)["total_tokens"] is None


# ---------------------------------------------------------------------------
# M2: cleanup
# ---------------------------------------------------------------------------

def _own_db(path: Path):
    from database import VioletDatabase
    return VioletDatabase(path / "violet-test.db")


def test_normal_exit_closes_the_owned_database_and_removes_the_directory(clean_env, violetdb, workdir, tmp_path,
                                                                        monkeypatch, capsys):
    from tools import violet_test_chat, harness
    monkeypatch.setattr(harness, "OUTPUT_DIR", tmp_path / "out")
    opened = []

    def factory(path):
        db = _own_db(path)
        opened.append(db)
        return db
    assert violet_test_chat.main(["--workdir", str(workdir)], input_fn=_inputs("你好", "/quit"), db_factory=factory) == 0
    assert opened and (workdir / "violet-test.db").exists() is False and not workdir.exists()
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].fetch_all("SELECT 1")                                                       # closed
    assert "测试数据目录已删除" in capsys.readouterr().out


def test_ctrl_c_exit_cleans_up(clean_env, violetdb, workdir, tmp_path, monkeypatch):
    from tools import violet_test_chat, harness
    monkeypatch.setattr(harness, "OUTPUT_DIR", tmp_path / "out")
    assert violet_test_chat.main(["--workdir", str(workdir)], input_fn=_inputs("你好", KeyboardInterrupt()),
                              db_factory=_own_db) == 0
    assert not workdir.exists()


def test_crash_cleans_up_and_keeps_the_original_error(clean_env, violetdb, workdir, tmp_path, monkeypatch):
    from tools import violet_test_chat, harness
    monkeypatch.setattr(harness, "OUTPUT_DIR", tmp_path / "out")

    def boom(session, input_fn=input, out=print):
        session.say("你好", 1)
        raise RuntimeError("boom in the middle of a session")
    monkeypatch.setattr(violet_test_chat, "run_interactive", boom)
    with pytest.raises(RuntimeError, match="boom in the middle"):
        violet_test_chat.main(["--workdir", str(workdir)], db_factory=_own_db)
    assert not workdir.exists()


def test_cleanup_failure_is_reported_and_never_masks_the_original_error(clean_env, violetdb, workdir, tmp_path,
                                                                        monkeypatch, capsys):
    from tools import violet_test_chat, harness

    def failing_cleanup(path, *a, **k):
        raise harness.CleanupError(f"could not delete {path}; left: ['violet-test.db']")
    monkeypatch.setattr(harness, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(harness, "cleanup", failing_cleanup)
    monkeypatch.setattr(violet_test_chat, "run_interactive",
                        lambda session, input_fn=input, out=print: (_ for _ in ()).throw(ValueError("original")))
    with pytest.raises(ValueError, match="original"):
        violet_test_chat.main(["--workdir", str(workdir)], db_factory=_own_db)
    assert "清理失败" in capsys.readouterr().out
    monkeypatch.setattr(violet_test_chat, "run_interactive",
                        lambda session, input_fn=input, out=print: {"transcript": []})
    assert violet_test_chat.main(["--workdir", str(tmp_path / "violet-testchat-second")], db_factory=_own_db) == 3
    assert "清理失败" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="open-file locks are a Windows behaviour")
def test_locked_files_make_cleanup_fail_loudly_then_succeed_after_close(workdir):
    from tools.harness import CleanupError, cleanup, prepare_environment
    prepare_environment(workdir, environ={})
    conn = sqlite3.connect(workdir / "violet-test.db")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    with pytest.raises(CleanupError, match="violet-test.db"):
        cleanup(workdir, attempts=2, delay=0)
    assert workdir.exists()
    conn.close()
    cleanup(workdir, attempts=2, delay=0)
    assert not workdir.exists()


def test_non_test_directories_are_refused_by_name_before_anything_else(tmp_path):
    from tools.harness import IsolationError, cleanup
    precious = tmp_path / "precious"
    precious.mkdir()
    with pytest.raises(IsolationError) as info:
        cleanup(precious)
    assert "not created by this run" not in str(info.value) and precious.exists()


def test_cli_process_closes_its_own_database_so_windows_can_delete_it(tmp_path):
    """A real CLI process (mock model): the process-wide connection points into the test directory,
    the session owns it, closes it, and the directory is gone afterwards (exit code 0, not 3)."""
    import subprocess
    project = Path(__file__).resolve().parent.parent
    workdir = tmp_path / "violet-testchat-process"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_", "MCP_"))}
    proc = subprocess.run([sys.executable, "-m", "tools.violet_test_chat", "--workdir", str(workdir),
                           "--output-dir", str(tmp_path / "out")], input="你好\n/quit\n", cwd=project,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=env)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert not workdir.exists()
    assert (tmp_path / "out" / "chat_transcript.json").exists()


def test_directories_not_created_by_this_run_are_never_deleted(clean_env, violetdb, tmp_path, monkeypatch, capsys):
    from tools import violet_test_chat, harness
    from tools.harness import IsolationError, cleanup, created_this_run, prepare_environment
    existing = tmp_path / "violet-testchat-existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("x", encoding="utf-8")
    prepare_environment(existing, environ={})
    assert not created_this_run(existing)
    with pytest.raises(IsolationError, match="not created by this run"):
        cleanup(existing)
    monkeypatch.setattr(harness, "OUTPUT_DIR", tmp_path / "out")
    assert violet_test_chat.main(["--workdir", str(existing)], input_fn=_inputs("/quit")) == 0
    assert (existing / "keep.txt").exists() and "不是本次创建的" in capsys.readouterr().out


def test_leftovers_are_listed_never_deleted(clean_env, violetdb, tmp_path, monkeypatch, capsys):
    import tempfile
    from tools import violet_test_chat, harness
    root = tmp_path / "tmproot"
    root.mkdir()
    old = root / "violet-testchat-old123"
    old.mkdir()
    (old / "violet-test.db").write_text("old", encoding="utf-8")
    other = root / "unrelated-dir"
    other.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(harness, "OUTPUT_DIR", tmp_path / "out")
    assert violet_test_chat.main(["--list-leftovers"]) == 0
    assert str(old) in capsys.readouterr().out
    assert violet_test_chat.main(["--workdir", str(tmp_path / "violet-testchat-new")], input_fn=_inputs("/quit")) == 0
    out = capsys.readouterr().out
    assert str(old) in out and "没有自动删除" in out
    assert old.exists() and (old / "violet-test.db").exists() and other.exists()


def test_close_is_idempotent_and_leaves_the_db_open_while_a_worker_runs(session_factory, workdir):
    session = session_factory()
    session.owns_db = True

    class Alive:
        def is_alive(self):
            return True
    session.runtime.stop()
    session.runtime._worker_thread = Alive()
    closed = []
    session.db = SimpleNamespace(close=lambda: closed.append(1))
    problems = session.close()
    assert any("still running" in p for p in problems) and not closed
    assert session.close() == []


def test_dialogue_eval_crash_cleans_up_and_reraises(clean_env, violetdb, workdir, tmp_path, monkeypatch):
    from tools import dialogue_eval
    original = dialogue_eval.run_plan
    monkeypatch.setattr(dialogue_eval, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(dialogue_eval, "run_plan", lambda *a, **k: (_ for _ in ()).throw(KeyError("plan broke")))
    with pytest.raises(KeyError, match="plan broke"):
        dialogue_eval.main(["--rounds", "1", "--workdir", str(workdir)], db_factory=_own_db)
    assert not workdir.exists()
    monkeypatch.setattr(dialogue_eval, "run_plan", original)
    ok_dir = tmp_path / "violet-testchat-ok"
    assert dialogue_eval.main(["--rounds", "1", "--workdir", str(ok_dir)], db_factory=_own_db) == 0
    assert not ok_dir.exists()


# ---------------------------------------------------------------------------
# M3: semantic context check for round 3
# ---------------------------------------------------------------------------

def _facts(violetdb, *items):
    from ai.profile_store import Profile
    facts = Profile(violetdb).facts
    ids = {}
    for key, value, said in items:
        result = facts.remember(key, value, evidence=said, user_message=said)
        ids[key] = (result.get("fact") or {}).get("id")
    return ids


def _round3(violetdb):
    from ai.context_engine import ContextEngine
    from tools.dialogue_eval import context_checks
    packet = ContextEngine(violetdb).build("chat", "平时住学校宿舍，周末偶尔回家")
    return context_checks(violetdb, {"context_has": ["身份：大学生"]}, "\n".join(packet.sections), packet.report)


def test_round3_accepts_confirmed_university_and_grade(violetdb):
    _facts(violetdb, ("address_name", "小满", "以后叫我小满就好"), ("study.school", "星河大学", PRIVATE_TEXT),
           ("study.grade", "大二", PRIVATE_TEXT), ("study.major", "天文", PRIVATE_TEXT))
    result = _round3(violetdb)
    assert result["has"]["身份：大学生"] is True
    assert result["matched_by"]["身份：大学生"] == "study.school+study.grade"


def test_round3_literal_identity_still_counts(violetdb):
    _facts(violetdb, ("identity.status", "大学生", "我是大学生"))
    from ai.context_engine import ContextEngine
    from tools.dialogue_eval import context_checks
    packet = ContextEngine(violetdb).build("chat", "我是大学生，平时住宿舍")
    result = context_checks(violetdb, {"context_has": ["身份：大学生"]}, "\n".join(packet.sections), packet.report)
    assert result["has"]["身份：大学生"] is True and result["matched_by"]["身份：大学生"] in ("literal", "identity.status")


@pytest.mark.parametrize("items", [
    [],                                                                              # nothing known
    [("study.school", "星河中学", "我在星河中学读高二"), ("study.grade", "高二", "我在星河中学读高二")],
    [("study.school", "星河大学", "我在星河大学")],                                     # no grade
    [("study.grade", "大二", "我读大二")],                                              # no school
    [("study.school", "星河中学", "我在星河中学读大二"), ("study.grade", "大二", "我在星河中学读大二")],
    [("study.school", "星河大学", "我在星河大学读高二"), ("study.grade", "高二", "我在星河大学读高二")],
])
def test_round3_rejects_non_equivalent_profiles(violetdb, items):
    _facts(violetdb, *items)
    result = _round3(violetdb)
    assert result["has"]["身份：大学生"] is False and result["matched_by"]["身份：大学生"] is None


def test_round3_rejects_unconfirmed_or_disabled_facts(violetdb):
    ids = _facts(violetdb, ("study.school", "星河大学", PRIVATE_TEXT), ("study.grade", "大二", PRIVATE_TEXT))
    violetdb.execute("UPDATE user_profile_facts SET use_in_context = 0 WHERE id = ?", (ids["study.grade"],))
    assert _round3(violetdb)["has"]["身份：大学生"] is False
    violetdb.execute("UPDATE user_profile_facts SET use_in_context = 1, origin = 'inferred' WHERE id = ?",
                  (ids["study.grade"],))
    assert _round3(violetdb)["has"]["身份：大学生"] is False


def test_round3_needs_the_grade_to_be_visible_to_eve(violetdb):
    from tools.dialogue_eval import context_checks
    ids = _facts(violetdb, ("study.school", "星河大学", PRIVATE_TEXT), ("study.grade", "大二", PRIVATE_TEXT))
    without_grade = context_checks(violetdb, {"context_has": ["身份：大学生"]}, "- 学校：星河大学",
                                   {"fact_ids": [ids["study.school"]]})
    assert without_grade["has"]["身份：大学生"] is False
    with_grade = context_checks(violetdb, {"context_has": ["身份：大学生"]}, "- 学校：星河大学\n- 年级：大二",
                                {"fact_ids": [ids["study.school"], ids["study.grade"]]})
    assert with_grade["has"]["身份：大学生"] is True
    school_not_in_context = context_checks(violetdb, {"context_has": ["身份：大学生"]}, "- 年级：大二",
                                           {"fact_ids": [ids["study.grade"]]})
    assert school_not_in_context["has"]["身份：大学生"] is False


def test_the_word_school_alone_is_not_enough(violetdb):
    from tools.dialogue_eval import context_checks
    system = "【用户档案】\n- 学校：（未知）\n【已经了解过的方面（不要再问这些）】\n- 学校、年级"
    result = context_checks(violetdb, {"context_has": ["身份：大学生"]}, system, {"fact_ids": []})
    assert result["has"]["身份：大学生"] is False


def test_other_checks_are_unchanged(violetdb):
    from tools.dialogue_eval import context_checks
    result = context_checks(violetdb, {"context_has": ["希望被称呼为：小满"], "context_lacks": ["专业：天文"]},
                            "- 希望被称呼为：小满", {"fact_ids": []})
    assert result["has"] == {"希望被称呼为：小满": True} and result["lacks"] == {"专业：天文": True}
    result = context_checks(violetdb, {"context_has": ["希望被称呼为：小满"]}, "- 学校：星河大学", {"fact_ids": []})
    assert result["has"] == {"希望被称呼为：小满": False}
