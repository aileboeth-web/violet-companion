"""Phase 9.4 real retest (session cys-p94-sched-r1): "我提前回宿舍了，下午实验课也请假不去了。" -- MemoryAgent ran
query_memory on an empty store, then set_situation(location), and its next model call was refused by the test
budget; the job was simply "failed" although the situation was written.

Reproduced offline with fictional words, the isolated database and the fake SDK:
* a job that wrote something and then stopped is "partial" (with the exact writes), never "complete";
* a retry is told what was written and repeats nothing (set_situation is idempotent per turn);
* a budget refusal (test tools only) is recorded as budget-interrupted and is not retried there;
* the leave is kept as its own short-term state and used in the next turn, the timetable line steps back;
* an empty store does not cost a query_memory round; a non-empty one still offers it;
* the normal one-step path (writes + memory_done) is unchanged."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.fakes import completion
from tests.test_phase2_scenarios import make_world, say  # noqa: F401
from tests.test_schedule_proactive_p944 import ON, at, clock, fill, timetable, world  # noqa: F401
from tests.test_test_tools import clean_env, session_factory, workdir  # noqa: F401  (fixtures)

WORDS = "我提前回宿舍了，下午实验课也请假不去了。"
LOCATION = {"name": "set_situation", "arguments": {"kind": "location", "value": "已提前回到宿舍", "until": "today",
                                                    "evidence": "我提前回宿舍了"}}
LEAVE = {"name": "set_situation", "arguments": {"kind": "availability", "value": "下午实验课请假了，没去",
                                                 "until": "today", "evidence": "下午实验课也请假不去了"}}
DONE = {"name": "memory_done", "arguments": {}}


def jobs(w):
    from ai.memory_jobs import job_outcome
    return [(dict(j), job_outcome(j)) for j in w.db.fetch_all("SELECT * FROM memory_jobs ORDER BY id")]


def situations(w):
    return w.db.fetch_all("SELECT kind, value, status, source_turn_id FROM user_situations ORDER BY id")


def memory_calls(w):
    return w.llm.calls_for("fake-memory")


# ----------------------------------------------------------------------------------------------- partial + retry

def test_a_write_then_a_failure_is_partial_and_the_retry_repeats_nothing(make_world, clock, timetable):
    w = world(make_world, **ON)
    clock(at(8, 11, 2))
    seen = []

    def memory(kw):
        seen.append(kw)
        n = len(seen)
        if n == 1:
            return completion(tool_calls=[LOCATION])                  # the r1 step: one write, no memory_done
        if n == 2:
            raise RuntimeError("upstream went away")                   # the next model call fails
        return completion(tool_calls=[LOCATION, LEAVE, DONE])          # the retry repeats the first write
    w.llm.responders["fake-memory"] = memory
    say(w, WORDS)
    (job, outcome), = jobs(w)
    assert job["status"] == "done" and job["attempts"] == 2 and outcome["state"] == "complete"
    progress = json.loads(job["progress"])
    assert [(s["attempt"], s["stop"]) for s in progress["stops"]] == [(1, "error"), (2, "complete")]
    assert [(p["attempt"], p["tool"], p["action"]) for p in progress["writes"]] == [
        (1, "set_situation", "created"), (2, "set_situation", "already_set"), (2, "set_situation", "created")]
    retry_system = "\n".join(m.get("content") or "" for m in seen[2]["messages"] if m["role"] == "system")
    assert "【这一轮之前的尝试已经成功写入】set_situation（created" in retry_system   # told what was done
    rows = situations(w)
    assert [(r["kind"], r["value"], r["status"]) for r in rows] == [
        ("location", "已提前回到宿舍", "active"), ("availability", "下午实验课请假了，没去", "active")]   # no duplicate


def test_a_job_that_keeps_failing_after_a_write_ends_partial_not_done(make_world, clock, timetable, monkeypatch):
    monkeypatch.setenv("VIOLET_MEMORY_MAX_ATTEMPTS", "2")
    w = world(make_world, **ON)
    clock(at(8, 11, 2))
    calls = []

    def memory(kw):
        calls.append(kw)
        if len(calls) % 2 == 1:
            return completion(tool_calls=[LOCATION])
        raise RuntimeError("upstream went away")
    w.llm.responders["fake-memory"] = memory
    say(w, WORDS)
    (job, outcome), = jobs(w)
    assert job["status"] == "failed" and outcome["state"] == "partial" and not outcome["budget_interrupted"]
    assert [w_["action"] for w_ in outcome["writes"]] == ["created", "already_set"]
    assert [(r["kind"], r["status"]) for r in situations(w)] == [("location", "active")]        # kept, once


def test_budget_interruption_is_recorded_and_not_retried_in_the_test_tool(violetdb):
    """The test tools' budget refusal (BudgetExceeded): the worker records the stop as "budget"; the harness then
    marks the job failed without a retry -- the written situation stays, the outcome says partial + budget."""
    from ai.memory_jobs import MemoryWorker, job_outcome
    from tools.harness import BudgetExceeded

    class Stub:
        def run(self, text, call_context):
            call_context["write_log"].append({"tool": "set_situation", "action": "created", "situation_id": 1})
            raise BudgetExceeded("token cap 40000: 32612 used + 8139 reserved for the next MemoryAgent call")
    worker = MemoryWorker(violetdb, Stub())
    violetdb.add_memory_job("t-r1", WORDS, None, status="pending")
    assert job_outcome(violetdb.get_memory_job_by_turn("t-r1"))["state"] == "unprocessed"
    worker.run_once()
    job = violetdb.get_memory_job_by_turn("t-r1")
    outcome = job_outcome(job)
    assert job["status"] == "pending"                        # Violet's own worker would retry (policy unchanged)
    assert outcome["state"] == "partial" and outcome["budget_interrupted"] and outcome["retry_pending"]


def test_the_harness_reports_a_budget_stopped_memory_job_as_partial(session_factory, monkeypatch):
    """The r1 shape in the test tool itself: Intent + Chat + one MemoryAgent step (set_situation, no memory_done),
    then the per-turn cap refuses MemoryAgent's next call."""
    from tools import harness
    from tools.harness import Budget
    plain = harness.MockLLM._create

    def create(self, **kwargs):
        if harness._AGENT.get() == "MemoryAgent":
            return completion(tool_calls=[LOCATION])
        return plain(self, **kwargs)
    monkeypatch.setattr(harness.MockLLM, "_create", create)
    session = session_factory(budget=Budget(max_calls_per_turn=3, max_calls_total=50))
    result = session.say(WORDS, 1)
    job, = result["memory_jobs"]
    assert job["status"] == "failed" and job["attempts"] == 1 and job["budget_not_retried"]   # test tool: no retry
    assert job["outcome"] == "partial" and job["budget_interrupted"]                         # not "done", not lost
    assert [(w_["tool"], w_["action"]) for w_ in job["writes"]] == [("set_situation", "created")]
    assert result["background_complete"] is False
    rows = session.db.fetch_all("SELECT kind, value, status FROM user_situations")
    assert [(r["kind"], r["value"], r["status"]) for r in rows] == [("location", "已提前回到宿舍", "active")]


def test_with_enough_budget_the_r1_shape_finishes_and_is_complete(make_world, clock, timetable):
    """The follow-up call after a lone set_situation is needed (no memory_done yet, the leave not written):
    it is kept, and the job ends complete with both states."""
    w = world(make_world, **ON)
    clock(at(8, 11, 2))
    steps = iter([completion(tool_calls=[LOCATION]), completion(tool_calls=[LEAVE, DONE])])
    seen = []
    w.llm.responders["fake-memory"] = lambda kw: (seen.append(kw), next(steps))[1]
    say(w, WORDS)
    assert len(seen) == 2                                         # stops right after memory_done, no summary call
    (job, outcome), = jobs(w)
    assert job["status"] == "done" and job["attempts"] == 1 and outcome["state"] == "complete"
    assert [r["kind"] for r in situations(w)] == ["location", "availability"]


# ----------------------------------------------------------------------------------------------- the leave, next turn

def test_the_leave_is_its_own_state_and_used_next_turn(make_world, clock, timetable):
    w = world(make_world, **ON, VIOLET_SCHEDULE_CONTEXT="1")
    clock(at(8, 11, 2))                                          # Tue; 物理 14:00 by the plan
    w.llm.responders["fake-memory"] = lambda kw: (completion(tool_calls=[LOCATION, LEAVE, DONE])
                                                  if not any(m["role"] == "tool" for m in kw["messages"]) else "")
    rules = []
    first = w.llm.responders["fake-memory"]
    w.llm.responders["fake-memory"] = lambda kw: (rules.append(kw), first(kw))[1]
    say(w, WORDS)
    memory_system = "\n".join(m.get("content") or "" for m in rules[0]["messages"] if m["role"] == "system")
    assert "请假、不去某节课、接下来有空没空用 availability" in memory_system and "不会改动课表" in memory_system
    assert w.db.fetch_one("SELECT COUNT(*) AS n FROM event_memories")["n"] == 0                  # not long-term
    assert w.db.fetch_one("SELECT COUNT(*) AS n FROM user_profile_facts")["n"] == 0             # not the profile
    assert w.db.fetch_one("SELECT COUNT(*) AS n FROM schedule_exceptions")["n"] == 0            # not the timetable
    clock(at(8, 14, 10))                                         # in the 物理 hour by the plan
    seen = []
    w.llm.responders["fake-chat"] = lambda kw: (seen.append(kw), "嗯嗯")[1]
    say(w, "躺着刷手机，好舒服～")
    system = "\n".join(m.get("content") or "" for m in seen[-1]["messages"] if m["role"] == "system")
    assert "已提前回到宿舍" in system and "下午实验课请假了，没去" in system
    assert "按课表，对方这会儿可能在上课" not in system


# ----------------------------------------------------------------------------------------------- empty store

def test_an_empty_store_offers_no_query_and_says_why(make_world, clock, timetable):
    w = world(make_world, **ON)
    clock(at(8, 11, 2))
    seen = []
    w.llm.responders["fake-memory"] = lambda kw: (seen.append(kw), completion(tool_calls=[LOCATION, LEAVE, DONE]))[1]
    say(w, WORDS)
    tools = [t["function"]["name"] for t in seen[0].get("tools") or []]
    assert "query_memory" not in tools and {"set_situation", "save_memory", "memory_done"} <= set(tools)
    system = "\n".join(m.get("content") or "" for m in seen[0]["messages"] if m["role"] == "system")
    assert "记忆库里还没有任何事件记忆" in system and "请先用 query_memory" not in system
    assert len(seen) == 1                                         # writes + memory_done: one model call in all
    (job, outcome), = jobs(w)
    assert outcome["state"] == "complete" and [p["tool"] for p in outcome["writes"]] == ["set_situation", "set_situation"]


def test_a_store_with_memories_still_offers_the_query(make_world, clock, timetable):
    w = world(make_world, **ON)
    clock(at(8, 11, 2))
    w.db.add_event_memory("用户下周要交虚构的实验报告", category="study")
    seen = []
    w.llm.responders["fake-memory"] = lambda kw: (seen.append(kw), json.dumps({"should_save": False, "reason": "t"}))[1]
    say(w, "实验报告写了一半")
    tools = [t["function"]["name"] for t in seen[0].get("tools") or []]
    assert "query_memory" in tools
    assert "请先用 query_memory" in "\n".join(m.get("content") or "" for m in seen[0]["messages"] if m["role"] == "system")


def test_the_hiding_can_be_switched_off(make_world, clock, timetable, monkeypatch):
    monkeypatch.setenv("VIOLET_MEMORY_HIDE_EMPTY_QUERY_TOOL", "0")
    w = world(make_world, **ON)
    clock(at(8, 11, 2))
    seen = []
    w.llm.responders["fake-memory"] = lambda kw: (seen.append(kw), json.dumps({"should_save": False, "reason": "t"}))[1]
    say(w, "随便聊聊")
    assert "query_memory" in [t["function"]["name"] for t in seen[0].get("tools") or []]


# ----------------------------------------------------------------------------------------------- situations

def test_set_situation_is_idempotent_per_turn_only(violetdb):
    from ai.profile_store import Profile
    s = Profile(violetdb).situations
    first = s.observe("location", "在宿舍", "user_stated", evidence="在宿舍", user_message="我在宿舍", turn_id="t1")
    again = s.observe("location", "在宿舍", "user_stated", evidence="在宿舍", user_message="我在宿舍", turn_id="t1")
    assert first["action"] == "created" and again["action"] == "already_set"
    assert again["situation"]["id"] == first["situation"]["id"]
    later = s.observe("location", "在宿舍", "user_stated", evidence="在宿舍", user_message="我还在宿舍", turn_id="t2")
    assert later["action"] == "created"                                       # a new statement is a new row
    moved = s.observe("location", "在图书馆", "user_stated", evidence="我到图书馆了", user_message="我到图书馆了",
                      turn_id="t2")
    assert moved["action"] == "created" and [r["value"] for r in s.current(["location"])] == ["在图书馆"]
