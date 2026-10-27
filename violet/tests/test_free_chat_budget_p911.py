"""Phase 9.1.1: free_chat reliability after the Phase 9.1 real test (offline).

D-P91-4  --max-calls / --max-tokens were per launch: launches 3 and 4 both got "the remaining" 41 / 101399 and the session
         ended at 200478 tokens, 478 above its 200000 cap. Now a real session has caps of its own, counted from a
         per-call ledger, and a launch only gets what is left.
D-P91-5  launches 2-4 never reached finish() (window closed): the next launch says so, counts their usage, checks the
         database and folds the WAL back in; closing the window now records the launch as ended.
D-P91-3  MemoryAgent's real tool arguments were not recorded; now they are, redacted.
Unit tests on the functions, then child processes with a fake SDK (as tests/test_free_chat_p896.py)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tools import chat_dialogue as cd
from tools import free_chat as fc


def args(**kw):
    base = dict(max_calls=150, max_tokens=500_000, session_max_calls=None, session_max_tokens=None)
    return SimpleNamespace(**{**base, **kw})


MEM91_META = {"launches": [
    {"n": 1, "ended_at": "2026-10-24T04:28:30+00:00", "calls": 15, "tokens": 81098,
     "budget": {"max_calls_total": 60, "max_tokens_total": 200000}},
    {"n": 2, "ended_at": None, "calls": 4, "tokens": 17503, "budget": {"max_calls_total": 10, "max_tokens_total": 50000}},
    {"n": 3, "ended_at": None, "calls": 4, "tokens": 17911, "budget": {"max_calls_total": 41, "max_tokens_total": 101399}},
    {"n": 4, "ended_at": None, "calls": 16, "tokens": 83966, "budget": {"max_calls_total": 41, "max_tokens_total": 101399}},
]}


def test_the_real_session_would_have_been_stopped(tmp_path):
    """mem91-real-1 as it ended: the session takes launch 1's caps (60 / 200000) and has nothing left."""
    used = fc.ledger_usage(tmp_path, MEM91_META)
    assert (used["calls"], used["tokens"]) == (39, 200478)
    caps = fc.session_budget(MEM91_META, args(max_calls=41, max_tokens=101399), new=False, mode="real_model")
    assert (caps["max_calls"], caps["max_tokens"], caps["origin"]) == (60, 200000, "first_launch_caps")
    with pytest.raises(fc.SessionError, match="会话累计预算已用完"):
        fc.launch_caps(args(max_calls=41, max_tokens=101399), caps, used)
    # launch 4 would have got 101399 - 17911 = 83488 tokens, not a fresh 101399
    three = {"launches": MEM91_META["launches"][:3]}
    assert fc.launch_caps(args(max_calls=41, max_tokens=101399), caps, fc.ledger_usage(tmp_path, three)) == (37, 83488)


def test_the_ledger_counts_a_launch_whose_round_was_never_saved(tmp_path):
    meta = {"launches": [{"n": 1, "ended_at": None, "calls": 2, "tokens": 2000}]}       # the row lags behind
    fc.append_jsonl(tmp_path / fc.USAGE, [{"launch": 1, "agent": "IntentAgent", "ok": True, "tokens": 3000},
                                          {"launch": 1, "agent": "ChatAgent", "ok": True, "tokens": 1500},
                                          {"launch": 1, "agent": "MemoryAgent", "ok": False, "tokens": 0}])
    used = fc.ledger_usage(tmp_path, meta)
    assert (used["calls"], used["tokens"]) == (3, 4500) and used["launches"][0]["source"] == "usage.jsonl"
    assert used["sizes"] == {"IntentAgent": 3000, "ChatAgent": 1500}


def test_session_caps_are_set_once_and_can_only_go_down():
    new = fc.session_budget(None, args(max_calls=60, max_tokens=200000), new=True, mode="real_model")
    assert (new["max_calls"], new["max_tokens"]) == (60, 200000)
    asked = fc.session_budget(None, args(session_max_calls=20, session_max_tokens=9000), new=True, mode="real_model")
    assert (asked["max_calls"], asked["max_tokens"]) == (20, 9000)
    meta = {"session_budget": {"max_calls": 60, "max_tokens": 200000}, "launches": []}
    lowered = fc.session_budget(meta, args(session_max_tokens=150000), new=False, mode="real_model")
    assert lowered["max_tokens"] == 150000 and lowered["origin"] == "lowered"
    with pytest.raises(fc.SessionError, match="只能调低"):
        fc.session_budget(meta, args(session_max_calls=61), new=False, mode="real_model")
    assert fc.session_budget(meta, args(), new=False, mode="mock") is None
    assert fc.launch_caps(args(max_calls=7, max_tokens=50), None, {"calls": 999, "tokens": 999}) == (7, 50)


def test_the_first_call_of_a_new_launch_reserves_what_that_agent_used_before():
    from tools.harness import Budget, CallRecord, CallRecorder
    rec = CallRecorder(Budget(max_tokens_total=10_000))
    assert rec.reserve_for("MemoryAgent")[0] == 16_000                 # nothing known: first_call_reserve
    rec.prior_sizes = {"MemoryAgent": 9000, "ChatAgent": 2000}
    assert rec.reserve_for("MemoryAgent")[0] == 9900 and rec.reserve_for("IntentAgent")[0] == 9900
    assert rec.reserve_for("ChatAgent")[0] == 2200                     # its own size, not the largest of all
    seen = []
    rec.on_record = seen.append
    rec.record(CallRecord(agent="ChatAgent", model="m", started_at="t", latency_ms=1, ok=True, total_tokens=1800))
    rec.on_record = lambda r: 1 / 0                                      # a broken hook never fails the call
    rec.record(CallRecord(agent="ChatAgent", model="m", started_at="t", latency_ms=1, ok=True, total_tokens=1800))
    assert len(seen) == 1 and len(rec.calls) == 2


def test_tool_arguments_are_logged_redacted():
    from tools.harness import redact_for_log
    got = redact_for_log({"summary": "用户的电话是13812345678", "evidence": "最近确诊了胃病", "key": "other",
                          "nested": [{"value": "家里养了一只柯基", "token": "sk-SECRET"}], "fact_id": 3},
                         scrub=lambda s: s.replace("sk-SECRET", "<redacted>"))
    assert got["summary"].startswith("[敏感内容已隐藏") and got["evidence"].startswith("[敏感内容已隐藏")
    assert got["nested"] == [{"value": "家里养了一只柯基", "token": "<redacted>"}] and got["fact_id"] == 3


def test_closing_the_window_records_the_launch_once(tmp_path):
    meta = {"tool": fc.TOOL_VERSION, "launches": [{"n": 1, "ended_at": None}], "scenario_now": None}
    fc.write_meta(tmp_path, meta)
    chat = SimpleNamespace(meta=meta, path=tmp_path,
                           session=SimpleNamespace(clock=SimpleNamespace(now=lambda: fc.datetime.fromisoformat(
                               "2026-11-04T20:03:00+08:00"))),
                           recorder=SimpleNamespace(scrub=lambda s: s, budget_state=lambda: {"calls": 2}),
                           usage=lambda: {"calls": 2, "tokens": 300})
    fc.emergency_save(chat, "console_closed")
    saved = fc.read_meta(tmp_path)["launches"][0]
    assert saved["end"] == "console_closed" and saved["ended_at"] and saved["tokens"] == 300
    first = saved["ended_at"]
    fc.emergency_save(chat, "logoff")                                   # a second event changes nothing
    again = fc.read_meta(tmp_path)["launches"][0]
    assert again["ended_at"] == first and again["end"] == "console_closed"


def test_usage_shows_round_launch_and_session_apart():
    from tools.harness import Budget
    chat = SimpleNamespace(
        launch=2, events=[{"n": 5, "kind": "user", "launch": 2, "calls": [{}, {}, {}], "tokens": 17503}],
        usage=lambda: {"calls": 4, "tokens": 17503, "failed_calls": 0, "avg_latency_ms": 10, "stopped": None},
        recorder=SimpleNamespace(budget=Budget(max_calls_total=41, max_tokens_total=101399)),
        used_before={"calls": 15, "tokens": 81098, "launches": [{"n": 1}]},
        session_caps={"max_calls": 60, "max_tokens": 200000})
    lines = fc.usage_lines(chat)
    assert lines[0] == "上一轮（#5）：调用 3 次，token 17503"
    assert "本次上限 41 次 / 101399 token" in lines[1]
    assert lines[2] == ("累计（含之前 1 次启动）：调用 19 次，token 98601；会话上限 60 次 / 200000 token，"
                        "剩余 41 次 / 101399 token")


# ------------------------------------------------------------------------------------------------ child processes

URL = "http://127.0.0.1:18911/v1"
KEY = "fake-free-chat-p911-credential-0000"
BOOT = r'''
import json, sys, time, uuid
from unittest.mock import patch
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
Mock = cd._mock_class()
def done(content, model, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:6], "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for n, a in tool_calls]
    return ChatCompletion.model_validate({
        "id": "fake-" + uuid.uuid4().hex[:8], "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": msg}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}})
class FakeSDK(Mock):
    def __init__(self, **kwargs):
        super().__init__()
    def _create(self, **kwargs):
        agent, msgs, model = _AGENT.get(), kwargs.get("messages") or [], str(kwargs.get("model"))
        whole = json.dumps(msgs, ensure_ascii=False)
        if agent == "ChatAgent":
            return done("嗯嗯，我在呢～", model)
        if agent == "MemoryAgent" and "花盆" in whole and '"role": "tool"' not in whole:
            return done(None, model, [("save_memory", {"summary": "用户养了一只叫团子的橘猫，电话13812345678",
                                                        "category": "pet", "kind": "fact"})])
        return done(super()._create(**kwargs).choices[0].message.content, model)
with patch("openai.OpenAI", FakeSDK):
    from tools import free_chat
    raise SystemExit(free_chat.main(json.loads(sys.argv[1])))
'''


def child_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY, NO_PROXY="127.0.0.1,localhost")
    return env


def launch(argv, lines):
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv)], cwd=cd.PROJECT,
                          env=child_env(), input="\n".join(lines) + "\n", capture_output=True, text=True,
                          encoding="utf-8", timeout=900)
    return done.returncode, done.stdout + done.stderr


def real(path, *extra):
    return ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys", *extra]


def test_the_session_cap_holds_across_launches(tmp_path):
    path = tmp_path / "s1"
    # 20000: the first call of a session reserves first_call_reserve (16000) before anything is known
    code1, out1 = launch(real(path, "--session-max-calls", "40", "--session-max-tokens", "20000"),
                         ["yes", "我家团子今天把花盆打翻了，气死我了", "/usage", "/quit"])
    assert code1 == 0, out1[-3000:]
    ledger = fc.read_jsonl(path / fc.USAGE)
    used1 = sum(r["tokens"] for r in ledger)
    assert ledger and all(r["launch"] == 1 for r in ledger) and used1 == 1010 * len(ledger)
    assert "会话上限 40 次 / 20000 token" in out1
    events = fc.read_jsonl(path / fc.EVENTS)
    logged = events[0]["memory_tools"]
    assert logged and logged[0]["tool"] == "save_memory" and logged[0]["arguments"]["category"] == "pet"
    assert logged[0]["arguments"]["summary"].startswith("[敏感内容已隐藏")      # the phone number never reaches the log
    assert "13812345678" not in json.dumps(logged, ensure_ascii=False)
    # (state_changes still shows what the product stored -- that is the test database, not this log)
    # a new launch asking for far more still only gets what is left of the session
    code2, out2 = launch(real(path, "--max-tokens", "500000"),
                         ["yes"] + [f"今天第{i}件小事" for i in range(12)] + ["/quit"])
    assert code2 == 0, out2[-3000:]
    assert f"本次启动最多：{40 - len(ledger)} 次调用 / {20000 - used1} token" in out2 and "预算已用完" in out2
    total = sum(r["tokens"] for r in fc.read_jsonl(path / fc.USAGE))
    assert total <= 20000                          # the reserve (largest earlier call x 1.1) kept it under the cap
    code3, out3 = launch(real(path), ["yes", "/quit"])
    assert code3 == 2 and "会话累计预算已用完" in out3 and "确认开始真实调用" not in out3    # refused before the key prompt
    assert fc.read_meta(path)["session_budget"]["max_tokens"] == 20000
    code4, out4 = launch(real(path, "--session-max-tokens", "30000"), ["yes", "/quit"])
    assert code4 == 2 and "只能调低" in out4


def test_a_killed_launch_is_counted_and_its_database_recovered(tmp_path):
    path = tmp_path / "s2"
    child = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(real(path))], cwd=cd.PROJECT,
                             env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8")
    try:
        child.stdin.write("yes\n我家团子今天把花盆打翻了，气死我了\n")
        child.stdin.flush()
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not fc.read_jsonl(path / fc.EVENTS):
            time.sleep(0.5)
        assert fc.read_jsonl(path / fc.EVENTS), "the first round never finished"
    finally:
        child.kill()                                   # like closing the window: finish() never runs
        child.communicate(timeout=60)
    meta = fc.read_meta(path)
    assert meta["launches"][0]["ended_at"] is None
    calls = len(fc.read_jsonl(path / fc.USAGE))
    code, out = launch(real(path), ["yes", "/usage", "/quit"])
    assert code == 0, out[-3000:]
    assert "第 1 次启动没有正常结束" in out and f"累计（含之前 1 次启动）：调用 {calls} 次" in out
    meta = fc.read_meta(path)
    first, second = meta["launches"]
    assert first["end"] == "abnormal" and first["counted"]["calls"] == calls and first["counted"]["from"] == "usage.jsonl"
    assert second["db_open"]["quick_check"] == "ok" and second["db_open"]["wal_bytes_after"] == 0
    assert second["db_open"]["previous_abnormal_launches"] == [1] and second["end"] == "normal"
    assert second["used_before"]["calls"] == calls
