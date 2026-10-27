"""Phase 9.4 R2 (real retest cys-p94-sched-r2), offline with fictional words, fixed times, isolated SQLite and a fake SDK.

#1 "我刚提前回宿舍了，今天下午的实验课也请假不去了。" went into event_memories twice (save_memory background + event/done),
   user_situations stayed empty. The only pointer to set_situation was PROFILE_RULES 3; save_memory's own description,
   its kind=background ("普通聊天背景") and MEMORY_RULES gave no boundary. Fix: the boundary is in both tool descriptions
   and in MEMORY_RULES 1. Whether a real model follows it can only be seen with a real model; here the request the
   model receives is checked, and the path when it does choose set_situation.
#2 With no situation, the 14:00 Chat context carried "按课表，对方这会儿可能在上课（大约到 15:45）". With the two
   situations it does not (T43 (2)); checked on the real request Chat got, against an R2-shaped control session.
#3 ChatAgent APITimeoutError -> base_agent retry -> reply sent, but the step was "failed": the report only counted
   502/503 as recovered. Now a timeout / connection error retried in the same run and model also counts; the error
   stays in calls / call_errors; a timeout that never recovered is still a failure."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from tests.fakes import completion
from tests.test_phase2_scenarios import make_world, say  # noqa: F401
from tests.test_schedule_proactive_p944 import ON, at, clock, timetable, world  # noqa: F401
from tools import chat_dialogue as cd
from tools import free_chat as fc

URL = "http://127.0.0.1:18997/v1"
KEY = "fake-free-chat-p94r2-credential-0000"
R2 = "我刚提前回宿舍了，今天下午的实验课也请假不去了。"
LATER = "躺着刷手机真舒服，终于能歇会儿了。"
PLAN_LINE = "按课表，对方这会儿可能在上课"
LOCATION = {"kind": "location", "value": "提前回到宿舍了", "until": "today", "evidence": "我刚提前回宿舍了"}
LEAVE = {"kind": "availability", "value": "今天下午的实验课请假了，不去", "until": "today",
         "evidence": "今天下午的实验课也请假不去了"}

BOOT = r'''
import json, os, sys, time, uuid
from unittest.mock import patch
import httpx
from openai import APITimeoutError
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
FAIL = {k: int(v) for k, v in json.loads(os.environ.get("R2_CHAT_FAILS") or "{}").items()}
MODE = os.environ.get("R2_MEMORY", "situation")
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
        assert kwargs["api_key"] == "%s" and kwargs["base_url"] == "%s" and kwargs.get("max_retries") == 0
        super().__init__()
    def _create(self, **kwargs):
        agent, msgs, model = _AGENT.get(), kwargs.get("messages") or [], str(kwargs.get("model"))
        last = str(msgs[-1].get("content") or "") if msgs else ""
        whole = json.dumps(msgs, ensure_ascii=False)
        if agent == "ChatAgent":
            if FAIL.get(last, 0) > 0:
                FAIL[last] -= 1
                raise APITimeoutError(request=httpx.Request("POST", "%s/chat/completions"))
            return done("嗯嗯，好好歇着吧", model)
        if agent == "MemoryAgent" and "请假" in last and '"role": "tool"' not in whole:
            if MODE == "situation":
                return done(None, model, [("set_situation", %s), ("set_situation", %s), ("memory_done", {})])
            return done(None, model, [("save_memory", {"summary": "用户提前回宿舍了。", "kind": "background",
                                                        "category": "other", "event_date": "today"}),
                                      ("save_memory", {"summary": "用户今天下午请假，不去实验课。", "kind": "event",
                                                        "progress": "done", "category": "other", "event_date": "today"}),
                                      ("memory_done", {})])
        if agent == "ProactiveAgent":
            return done(json.dumps({"should_act": False, "action_type": "none", "reason": "不打扰"}), model)
        return done(super()._create(**kwargs).choices[0].message.content, model)
with patch("openai.OpenAI", FakeSDK):
    from tools import free_chat
    raise SystemExit(free_chat.main(json.loads(sys.argv[1])))
''' % (KEY, URL, URL, json.dumps(LOCATION, ensure_ascii=False), json.dumps(LEAVE, ensure_ascii=False))


def launch(path, extra_env):
    argv = ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys", "--start",
            "2026-10-29T11:00:00+08:00", "--schedule", "on", "--schedule-context", "on"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY, NO_PROXY="127.0.0.1,localhost",
               **extra_env)
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv)], cwd=cd.PROJECT, env=env,
                          input="\n".join(["yes", R2, "/advance 3h", LATER, "/quit"]) + "\n", capture_output=True,
                          text=True, encoding="utf-8", timeout=1800)
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    return path


@pytest.fixture(scope="module")
def sessions(tmp_path_factory):
    root = tmp_path_factory.mktemp("p94r2")
    fixed = launch(root / "fixed", {"R2_MEMORY": "situation", "R2_CHAT_FAILS": json.dumps({LATER: 1})})
    control = launch(root / "control", {"R2_MEMORY": "event"})
    exhausted = launch(root / "exhausted", {"R2_MEMORY": "situation", "R2_CHAT_FAILS": json.dumps({LATER: 9})})
    return {"fixed": fixed, "control": control, "exhausted": exhausted}


def rows(path, sql):
    con = sqlite3.connect(path / "violet" / "violet-test.db")
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def user_events(path):
    return [e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user"]


def chat_context(path, text):
    n = next(e["n"] for e in user_events(path) if e["user"] == text)
    request = [r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["n"] == n and r["agent"] == "ChatAgent"][-1]
    return "\n".join(str(m.get("content") or "") for m in request if m["role"] == "system")


# ----------------------------------------------------------------------------------------------- database

def test_the_two_states_are_situations_valid_today_and_nothing_else_is_written(sessions):
    path = sessions["fixed"]
    got = rows(path, "SELECT kind, value, source, status, expires_at, source_turn_id FROM user_situations ORDER BY id")
    assert [(r["kind"], r["value"], r["source"], r["status"]) for r in got] == [
        ("location", "提前回到宿舍了", "user_stated", "active"),
        ("availability", "今天下午的实验课请假了，不去", "user_stated", "active")]
    assert {r["expires_at"][:16] for r in got} == {"2026-10-29T23:59"}                       # the day, not 3 hours
    assert rows(path, "SELECT COUNT(*) AS n FROM event_memories")[0]["n"] == 0                 # no event copies
    assert rows(path, "SELECT COUNT(*) AS n FROM user_profile_facts")[0]["n"] == 0
    exceptions = rows(path, "SELECT kind, on_date FROM schedule_exceptions ORDER BY id")
    assert exceptions == [{"kind": "time", "on_date": "2026-11-02"}, {"kind": "cancel", "on_date": "2026-11-03"}]  # fixture only
    first = user_events(path)[0]
    assert [(j["status"], j["outcome"]) for j in first["memory_jobs"]] == [("done", "complete")]
    assert [w["tool"] for w in first["memory_jobs"][0]["writes"]] == ["set_situation", "set_situation"]


def test_the_r2_shaped_control_writes_events_and_no_situation(sessions):
    path = sessions["control"]
    assert rows(path, "SELECT COUNT(*) AS n FROM user_situations")[0]["n"] == 0
    assert [r["kind"] for r in rows(path, "SELECT kind FROM event_memories ORDER BY id")] == ["background", "event"]


# ----------------------------------------------------------------------------------------------- the 14:00 context

def test_at_14_chat_gets_the_users_states_and_no_conflicting_plan_line(sessions):
    fixed, control = chat_context(sessions["fixed"], LATER), chat_context(sessions["control"], LATER)
    assert PLAN_LINE in control and "大约到 15:45" in control                  # the R2 conflict, reproduced
    assert PLAN_LINE not in fixed
    assert "提前回到宿舍了" in fixed and "今天下午的实验课请假了，不去" in fixed
    state = next(e for e in user_events(sessions["fixed"]) if e["user"] == LATER)["schedule_state"]
    assert state["gate"]["blocked"] is True and state["gate"]["code"] == "in_class"   # the proactive gate is unchanged


# ----------------------------------------------------------------------------------------------- the step status

def test_a_chat_timeout_retried_successfully_is_recovered_not_failed(sessions):
    later = next(e for e in user_events(sessions["fixed"]) if e["user"] == LATER)
    assert later["reply"] == "sent" and later["violet"] == ["嗯嗯，好好歇着吧"]
    assert later["status"] == "recovered" and later["problems"] == []
    assert later["call_errors"] == ["APITimeoutError"]                                 # the original error is kept
    failed = [c for c in later["calls"] if not c["ok"]]
    assert [(c["agent"], c["error_type"]) for c in failed] == [("ChatAgent", "APITimeoutError")]
    rec, = later["recovered_calls"]
    assert rec["error_type"] == "APITimeoutError" and rec["status_code"] is None
    assert rec["recovered_by_call"] == next(c["call_no"] for c in later["calls"]
                                            if c["agent"] == "ChatAgent" and c["ok"])


def test_a_timeout_that_never_recovers_is_still_failed(sessions):
    later = next(e for e in user_events(sessions["exhausted"]) if e["user"] == LATER)
    assert later["status"] == "failed" and "APITimeoutError" in later["problems"]
    assert later["recovered_calls"] == []
    assert any(cd.FALLBACK_REPLY in t for t in later["violet"])


@pytest.mark.parametrize("calls,run_rows,recovered", [
    ([("ChatAgent", 1, False, None, "APITimeoutError", "m"), ("ChatAgent", 1, True, None, None, "m")],
     [(1, "ChatAgent", True)], [1]),
    ([("IntentAgent", 1, False, None, "APIConnectionError", "m"), ("IntentAgent", 1, True, None, None, "m")],
     [(1, "IntentAgent", True)], [1]),
    ([("ChatAgent", 1, False, None, "APITimeoutError", "m")] * 3, [(1, "ChatAgent", False)], []),        # exhausted
    ([("ChatAgent", 1, False, None, "APITimeoutError", "m"), ("ChatAgent", 1, True, None, None, "fallback")],
     [(1, "ChatAgent", True)], []),                                                                   # fallback model
    ([("MemoryAgent", 1, False, None, "APITimeoutError", "m"), ("MemoryAgent", 1, True, None, None, "m")],
     [(1, "MemoryAgent", True)], []),                                                                 # another agent
    ([("ChatAgent", 1, False, None, "APIError", "m"), ("ChatAgent", 1, True, None, None, "m")],
     [(1, "ChatAgent", True)], []),                                                                   # unknown kind
    ([("ChatAgent", 1, False, 500, "APITimeoutError", "m"), ("ChatAgent", 1, True, None, None, "m")],
     [(1, "ChatAgent", True)], []),                                                                   # has a status
])
def test_the_classifier(calls, run_rows, recovered):
    rows_ = [{"call_no": i + 1, "agent": a, "run_id": r, "ok": ok, "status_code": st, "error_type": et, "model": m,
              "step": i + 1, "latency_ms": 1} for i, (a, r, ok, st, et, m) in enumerate(calls)]
    rec, unrec = cd.classify_failed_calls(rows_, [{"run_id": r, "agent": a, "ok": ok} for r, a, ok in run_rows])
    assert [c["call_no"] for c in rec] == recovered
    assert len(rec) + len(unrec) == sum(not c["ok"] for c in rows_)                  # nothing dropped


# ----------------------------------------------------------------------------------------------- the boundary

def test_memory_agent_is_given_the_boundary_in_tools_and_rules(make_world, clock, timetable):
    w = world(make_world, **ON)
    clock(at(8, 11, 0))
    seen = []
    w.llm.responders["fake-memory"] = lambda kw: (seen.append(kw), json.dumps({"should_save": False, "reason": "t"}))[1]
    say(w, R2)
    tools = {t["function"]["name"]: t["function"] for t in seen[0]["tools"]}
    assert "用 set_situation，不要用这个工具" in tools["save_memory"]["description"]
    assert "set_situation" in tools["save_memory"]["parameters"]["properties"]["kind"]["description"]
    situation = tools["set_situation"]
    assert "不用 save_memory" in situation["description"] and "一句话里有几项就分别调用" in situation["description"]
    assert "请假、不去某节课" in situation["parameters"]["properties"]["kind"]["description"]
    assert "今天下午" in situation["parameters"]["properties"]["until"]["description"]
    system = "\n".join(m.get("content") or "" for m in seen[0]["messages"] if m["role"] == "system")
    assert "不属于这三类，不用 save_memory：用 set_situation" in system


# ----------------------------------------------------------------------------------------------- change of mind, expiry

def memory_plays(w, plays):
    def memory(kw):
        if any(m["role"] == "tool" for m in kw["messages"]):
            return ""
        last = kw["messages"][-1]["content"]
        return next((completion(tool_calls=calls) for words, calls in plays if words in last),
                    json.dumps({"should_save": False, "reason": "t"}))
    w.llm.responders["fake-memory"] = memory


def chat_seen(w):
    seen = []
    w.llm.responders["fake-chat"] = lambda kw: (seen.append(kw), "嗯嗯")[1]
    return seen


def system_of(kw):
    return "\n".join(m.get("content") or "" for m in kw["messages"] if m["role"] == "system")


CHANGE = "算了我还是回去上实验课了，刚到实验楼。"
DONE = {"name": "memory_done", "arguments": {}}


def test_a_change_of_mind_replaces_both_states(make_world, clock, timetable):
    w = world(make_world, **ON, VIOLET_SCHEDULE_CONTEXT="1")
    memory_plays(w, [
        (R2, [{"name": "set_situation", "arguments": LOCATION}, {"name": "set_situation", "arguments": LEAVE}, DONE]),
        (CHANGE, [{"name": "set_situation", "arguments": {"kind": "location", "value": "到实验楼了", "until": "today",
                                                          "evidence": "刚到实验楼"}},
                  {"name": "set_situation", "arguments": {"kind": "availability", "value": "回去上实验课了",
                                                          "until": "today", "evidence": "我还是回去上实验课了"}}, DONE]),
    ])
    clock(at(8, 11, 0))                                                 # Tue 2026-09-08; 物理 14:00-15:40
    say(w, R2)
    clock(at(8, 13, 50))
    say(w, CHANGE)
    current = w.db.fetch_all("SELECT kind, value FROM user_situations WHERE status = 'active' ORDER BY kind")
    assert [(r["kind"], r["value"]) for r in current] == [("availability", "回去上实验课了"), ("location", "到实验楼了")]
    old = w.db.fetch_all("SELECT value FROM user_situations WHERE status = 'superseded' ORDER BY id")
    assert [r["value"] for r in old] == ["提前回到宿舍了", "今天下午的实验课请假了，不去"]
    clock(at(8, 14, 10))
    seen = chat_seen(w)
    say(w, "老师开始讲了")
    system = system_of(seen[-1])
    assert "到实验楼了" in system and "回去上实验课了" in system
    assert "提前回到宿舍了" not in system and "请假" not in system                        # nothing stale
    assert w.db.fetch_one("SELECT COUNT(*) AS n FROM event_memories")["n"] == 0


def test_the_states_expire_and_the_plan_line_comes_back(make_world, clock, timetable):
    w = world(make_world, **ON, VIOLET_SCHEDULE_CONTEXT="1")
    memory_plays(w, [(R2, [{"name": "set_situation", "arguments": LOCATION},
                           {"name": "set_situation", "arguments": LEAVE}, DONE])])
    clock(at(8, 11, 0))
    say(w, R2)
    clock(at(8, 14, 10))
    seen = chat_seen(w)
    say(w, LATER)
    assert PLAN_LINE not in system_of(seen[-1]) and "请假了" in system_of(seen[-1])
    clock(at(15, 14, 10))                                               # next Tuesday, same class
    say(w, "又是周二了")
    system = system_of(seen[-1])
    assert PLAN_LINE in system                                          # normal timetable awareness again
    assert "提前回到宿舍了" not in system and "请假了" not in system                      # never asserted as now
    assert not w.db.fetch_all("SELECT id FROM user_situations WHERE status = 'active'")
