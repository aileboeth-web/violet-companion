"""Phase 7 B.6.1: new_event=true cannot skip the similar-event decision (round 15 of the real re-test
created a second, done copy of the still open 天文观测课程答辩 instead of updating it).

The state below is the one the real round-15 memory job saw (retest11, copied by value: fictional test
data). Everything is offline: scripted fake LLM, temp database."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from datetime import datetime

import pytest

from tests.fakes import FakeLLM, completion, default_responders, install_fake_llm

DEFENSE = "用户将于2026-10-21参加天文观测课程答辩，并对此感到有些紧张。"
INTERVIEW = "用户将于2026年10月14日参加实习面试，面试前紧张到睡不着。"
DONE_TEXT = "用户的天文观测课程答辩已结束，过程挺顺利。"          # the summary the real model saved as id 3
USER_15 = "答辩结束了，挺顺利的"
DONE = {"name": "memory_done", "arguments": {}}


@pytest.fixture()
def llm(monkeypatch, violetdb):
    return install_fake_llm(monkeypatch, FakeLLM(default_responders("好呀")))


@pytest.fixture()
def round15(violetdb):
    """Open events as they were when the round-15 memory job started."""
    defense = violetdb.add_event_memory(DEFENSE, category="general", kind="event", progress="unknown",
                                     event_time="2026-10-21T19:23:00+08:00", valid_until="2026-11-04T19:23:00+08:00")
    interview = violetdb.add_event_memory(INTERVIEW, category="event", kind="event", progress="unknown",
                                       event_time="2026-10-14T23:59:00+08:00", valid_until="2026-10-28T23:59:00+08:00")
    return {"defense": defense, "interview": interview}


@pytest.fixture()
def memory(llm):
    from ai.agents import MemoryAgent
    return MemoryAgent()


@contextlib.contextmanager
def run_context(**ctx):
    """One agent run's call context (what BaseAgent.run sets up)."""
    from ai.agents.base_agent import _CALL_CONTEXT
    token = _CALL_CONTEXT.set({"turn_id": "t-15", "user_message": USER_15, **ctx})
    try:
        yield
    finally:
        _CALL_CONTEXT.reset(token)


def save(agent, **args):
    return json.loads(agent._tool_save_memory({"category": "event", "kind": "event", **args}))


def count(db):
    return db.fetch_one("SELECT COUNT(*) AS c FROM event_memories WHERE status != 'deleted'")["c"]


def _script(llm, *steps):
    queue = list(steps)
    llm.responders["fake-memory"] = lambda kw: queue.pop(0) if queue else completion(content="ok")
    return queue


def _memory_run(user=USER_15, background=True, turn_id="t-15"):
    from ai.agents import MemoryAgent
    from ai.memory_jobs import build_memory_input
    context = {"turn_id": turn_id, "user_message": user, "assistant_reply": "太好了", "background_job": background}
    return MemoryAgent().run(build_memory_input(user, "太好了"), call_context=context)


def _tool_results(call):
    return [json.loads(m["content"]) for m in call["messages"] if m.get("role") == "tool"]


# ---------------------------------------------------------------------------
# C: the decision gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("summary,progress", [
    (DONE_TEXT, "done"),                                  # the real round-15 summary (text similarity 0.48)
    ("答辩结束了，很顺利", "done"),                          # too different for text similarity (0.27)
    ("用户答辩顺利结束", "done"),
    ("天文观测课程的答辩挺顺利", "unknown"),                  # an outcome mislabelled as unknown is asked too
    ("用户的天文观测课程答辩取消了", "cancelled"),
])
def test_round15_new_event_without_seen_candidates_needs_a_decision(memory, violetdb, round15, summary, progress):
    with run_context():
        result = save(memory, summary=summary, progress=progress, new_event=True)
    assert result["action"] == "needs_decision" and result["ok"] is False
    ids = [c["id"] for c in result["candidates"]]
    assert round15["defense"] in ids and round15["interview"] not in ids
    assert result["undecided_ids"] == ids
    defense = next(c for c in result["candidates"] if c["id"] == round15["defense"])
    assert defense["progress"] == "unknown" and defense["event_time"].startswith("2026-10-21")
    assert "答辩" in defense["basis"]["shared_terms"]
    assert set(defense) == {"id", "content", "kind", "progress", "event_time", "status", "similarity", "basis"}
    assert count(violetdb) == 2 and violetdb.get_event_memory(round15["defense"])["progress"] == "unknown"


def test_after_the_candidates_were_shown_the_open_event_is_updated(memory, violetdb, round15):
    with run_context():
        assert save(memory, summary=DONE_TEXT, progress="done", new_event=True)["action"] == "needs_decision"
        result = json.loads(memory._tool_update_event_progress(
            {"memory_id": round15["defense"], "progress": "done", "summary": DONE_TEXT, "reason": USER_15}))
    assert result["action"] == "progress_updated"
    defense = violetdb.get_event_memory(round15["defense"])
    assert defense["progress"] == "done" and defense["content"] == DONE_TEXT
    assert defense["event_time"].startswith("2026-10-21")              # the real date is kept
    assert count(violetdb) == 2


def test_related_to_after_the_decision_links_instead_of_creating(memory, violetdb, round15):
    with run_context():
        save(memory, summary=DONE_TEXT, progress="done")
        result = save(memory, summary=DONE_TEXT, progress="done", related_to=round15["defense"])
    assert result["action"] == "updated" and result["memory_id"] == round15["defense"]
    assert count(violetdb) == 2 and violetdb.get_event_memory(round15["defense"])["progress"] == "done"


def test_a_different_defense_is_created_once_the_model_has_seen_the_candidates(memory, violetdb, round15):
    other = "用户的英语口语课答辩也结束了"
    with run_context():
        first = save(memory, summary=other, progress="done", new_event=True)
        assert first["action"] == "needs_decision" and [c["id"] for c in first["candidates"]] == [round15["defense"]]
        created = save(memory, summary=other, progress="done", new_event=True)
    assert created["action"] == "created" and count(violetdb) == 3
    assert violetdb.get_event_memory(round15["defense"])["progress"] == "unknown"     # never merged or closed


def test_a_reworded_summary_has_to_be_decided_again(memory, violetdb, round15):
    """Seeing the candidates for one wording does not decide for another one (the drill on the copied
    retest11 database found this way around the gate)."""
    with run_context():
        assert save(memory, summary=DONE_TEXT, progress="done")["action"] == "needs_decision"
        reworded = save(memory, summary="答辩结束了，很顺利", progress="done", new_event=True)
        assert reworded["action"] == "needs_decision" and reworded["undecided_ids"] == [round15["defense"]]
        assert save(memory, summary="答辩结束了，很顺利", progress="done", new_event=True)["action"] == "created"
    assert violetdb.get_event_memory(round15["defense"])["progress"] == "unknown"


def test_round14_interview_is_still_a_separate_event(memory, violetdb):
    defense = violetdb.add_event_memory(DEFENSE, category="general", kind="event", progress="planned",
                                     event_time="2026-10-21T19:23:00+08:00")
    with run_context(user_message="我明天面试实习，现在紧张得睡不着，怎么办啊"):
        first = save(memory, summary=INTERVIEW, progress="planned", event_date="2026-10-14")
        assert first["action"] == "needs_decision" and [c["id"] for c in first["candidates"]] == [defense]
        assert first["candidates"][0]["basis"] == {"rule": "text_similarity", "shared_terms": []}
        created = save(memory, summary=INTERVIEW, progress="planned", event_date="2026-10-14", new_event=True)
    assert created["action"] == "created"
    assert violetdb.get_event_memory(defense)["content"] == DEFENSE and count(violetdb) == 2


def test_no_candidates_means_no_extra_step(memory, violetdb, round15):
    with run_context(user_message="周末去看了一场电影"):
        result = save(memory, summary="用户周末去看了一场电影", progress="done")
    assert result["action"] == "created" and count(violetdb) == 3


def test_corrections_retractions_and_cancellations_keep_their_meaning(memory, violetdb, round15):
    with run_context(user_message="不对，答辩是在周四"):
        corrected = json.loads(memory._tool_correct_memory({
            "memory_id": round15["defense"], "corrected_summary": "用户将于2026-10-22参加天文观测课程答辩", "reason": "周四"}))
        assert corrected["action"] == "corrected"
        cancelled = json.loads(memory._tool_update_event_progress(
            {"memory_id": round15["interview"], "progress": "cancelled", "reason": "面试取消了"}))
        assert cancelled["action"] == "progress_updated"
        retracted = json.loads(memory._tool_retract_memory({"memory_id": round15["defense"], "reason": "根本没有答辩"}))
        assert retracted["action"] == "retracted"
        # a retracted event is no candidate any more: the next outcome is saved without a decision
        assert save(memory, summary="答辩结束了，很顺利", progress="done")["action"] == "created"
    assert violetdb.get_event_memory(round15["interview"])["progress"] == "cancelled"


# ---------------------------------------------------------------------------
# The decision belongs to one run: other runs, turns, retries and threads never inherit it
# ---------------------------------------------------------------------------

def test_a_decision_shown_in_another_run_does_not_count(memory, violetdb, round15):
    with run_context(turn_id="t-earlier"):
        assert save(memory, summary=DONE_TEXT, progress="done")["action"] == "needs_decision"
    with run_context(turn_id="t-15"):
        assert save(memory, summary=DONE_TEXT, progress="done", new_event=True)["action"] == "needs_decision"
    assert count(violetdb) == 2


def test_every_agent_run_starts_with_nothing_shown(llm, violetdb, round15):
    """Even when run_async is reused in one context, the guard hook resets the decisions."""
    from ai.agents import MemoryAgent
    from ai.agents.base_agent import _CALL_CONTEXT
    from ai.agents.memory_agent import EventDecisionLedger
    stale = EventDecisionLedger()
    stale.show(DONE_TEXT, [{"id": round15["defense"], "content": DEFENSE, "progress": "unknown", "status": "active",
                            "event_time": "2026-10-21T19:23:00+08:00"}])
    _script(llm, completion(tool_calls=[{"name": "save_memory", "arguments": {
        "summary": DONE_TEXT, "category": "event", "kind": "event", "progress": "done", "new_event": True}}]))
    token = _CALL_CONTEXT.set({"turn_id": "t-15", "user_message": USER_15, "event_decisions": stale})
    try:
        asyncio.run(MemoryAgent().run_async(USER_15))
    finally:
        _CALL_CONTEXT.reset(token)
    first = _tool_results(llm.calls_for("fake-memory")[1])[0]
    assert first["action"] == "needs_decision" and count(violetdb) == 2


def test_concurrent_runs_keep_their_own_decisions(memory, violetdb, round15):
    results, barrier = {}, threading.Barrier(2)

    def worker(name, decide_first):
        with run_context(turn_id=name):
            if decide_first:
                save(memory, summary=DONE_TEXT, progress="done")
            barrier.wait()
            results[name] = save(memory, summary=DONE_TEXT, progress="done", new_event=True)["action"]
    threads = [threading.Thread(target=worker, args=("a", True)), threading.Thread(target=worker, args=("b", False))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["b"] == "needs_decision"
    assert results["a"] in ("created", "needs_decision")          # "a" saw its candidates; "b" never decided for it


# ---------------------------------------------------------------------------
# Stale decisions: candidates that change, appear, disappear
# ---------------------------------------------------------------------------

def test_a_candidate_changed_since_it_was_shown_is_asked_again(memory, violetdb, round15):
    with run_context():
        save(memory, summary=DONE_TEXT, progress="done")
        violetdb.apply_event_update(round15["defense"], content="用户将于2026-10-21参加天文观测课程答辩（改到了下午）",
                                 source="user")                     # another job changed it meanwhile
        again = save(memory, summary=DONE_TEXT, progress="done", new_event=True)
    assert again["action"] == "needs_decision" and again["undecided_ids"] == [round15["defense"]]
    assert count(violetdb) == 2


def test_a_new_similar_event_created_meanwhile_is_asked(memory, violetdb, round15):
    with run_context():
        save(memory, summary=DONE_TEXT, progress="done")
        newer = violetdb.add_event_memory("用户下周还有一场天文观测课程答辩补考", kind="event", progress="planned")
        again = save(memory, summary=DONE_TEXT, progress="done", new_event=True)
    assert again["action"] == "needs_decision" and again["undecided_ids"] == [newer]


def test_deleted_or_disabled_candidates_are_no_reason_to_wait(memory, violetdb, round15):
    with run_context():
        save(memory, summary=DONE_TEXT, progress="done")
        violetdb.execute("UPDATE event_memories SET use_in_context = 0 WHERE id = ?", (round15["defense"],))
        created = save(memory, summary=DONE_TEXT, progress="done", new_event=True)
    assert created["action"] == "created"


def test_a_memory_the_user_disabled_is_never_offered_as_a_candidate(memory, violetdb, round15):
    violetdb.execute("UPDATE event_memories SET use_in_context = 0 WHERE id = ?", (round15["defense"],))
    with run_context():
        result = save(memory, summary="答辩结束了，很顺利", progress="done", new_event=True)
    assert result["action"] == "created" and DEFENSE not in json.dumps(result, ensure_ascii=False)


def test_candidates_that_keep_changing_end_in_a_refusal_not_a_loop(memory, violetdb, round15):
    with run_context():
        actions = []
        for i in range(5):
            violetdb.add_event_memory(f"用户的天文观测课程答辩第{i}次彩排", kind="event", progress="planned")
            actions.append(save(memory, summary=DONE_TEXT, progress="done", new_event=True)["action"])
    assert actions == ["needs_decision"] * 3 + ["rejected"] * 2
    assert violetdb.fetch_one("SELECT COUNT(*) AS c FROM event_memories WHERE progress = 'done'")["c"] == 0


def test_the_database_gate_holds_for_any_deciding_caller(violetdb, round15):
    blocked = violetdb.save_event_memory(DONE_TEXT, kind="event", progress="done", new_event=True, decided={})
    assert blocked["action"] == "needs_decision"
    fp = {c["id"]: violetdb.event_candidate_fingerprint(c) for c in blocked["candidates"]}
    assert violetdb.save_event_memory(DONE_TEXT, kind="event", progress="done", new_event=True, decided=fp)["action"] == "created"


# ---------------------------------------------------------------------------
# The real round-15 tool sequence, end to end through the agent loop
# ---------------------------------------------------------------------------

def test_round15_failure_sequence_now_updates_the_open_event(llm, violetdb, round15):
    """Real run: query_memory -> 0, save_memory(new_event) -> created id 3, memory_done. Now the save is
    stopped and shown the open defense; the next step updates it."""
    _script(llm,
            completion(tool_calls=[{"name": "query_memory", "arguments": {"query": USER_15, "category": "event"}}]),
            completion(tool_calls=[{"name": "save_memory", "arguments": {
                "summary": DONE_TEXT, "category": "event", "kind": "event", "progress": "done", "new_event": True,
                "importance": 3, "emotional_weight": 2}}, DONE]),
            completion(tool_calls=[{"name": "update_event_progress", "arguments": {
                "memory_id": round15["defense"], "progress": "done", "summary": DONE_TEXT, "reason": USER_15}}, DONE]))
    result = _memory_run()
    calls = llm.calls_for("fake-memory")
    assert len(calls) == 3 and result["finished_after_tools"] is True
    assert _tool_results(calls[1])[0]["memories"] == []                      # the search still finds nothing ...
    context = "\n".join(m.get("content") or "" for m in calls[0]["messages"])
    assert f"id={round15['defense']}; 进展未知" in context                   # ... but the open list showed it
    assert "不必先 query_memory" in context
    gate = _tool_results(calls[2])[1]
    assert gate["action"] == "needs_decision" and round15["defense"] in gate["undecided_ids"]
    rows = violetdb.fetch_all("SELECT id, progress FROM event_memories ORDER BY id")
    assert [(r["id"], r["progress"]) for r in rows] == [(round15["defense"], "done"), (round15["interview"], "unknown")]


def test_needs_decision_or_a_refusal_never_counts_as_a_finished_write(llm, violetdb, round15):
    from ai.agents.memory_agent import write_step_succeeded
    assert not write_step_succeeded("save_memory", json.dumps({"ok": False, "action": "needs_decision"}))
    assert not write_step_succeeded("save_memory", json.dumps({"ok": False, "action": "rejected",
                                                               "reason": "decision_not_settled"}))
    _script(llm, completion(tool_calls=[{"name": "save_memory", "arguments": {
        "summary": DONE_TEXT, "category": "event", "progress": "done", "new_event": True}}, DONE]))
    result = _memory_run()
    assert not result.get("finished_after_tools") and count(violetdb) == 2


@pytest.mark.parametrize("name", ["save_event_memory", "SaveMemory", "save_memory_v2", "add_event_memory"])
def test_other_tool_names_cannot_write_events(llm, violetdb, round15, name):
    _script(llm, completion(tool_calls=[{"name": name, "arguments": {
        "summary": DONE_TEXT, "content": DONE_TEXT, "progress": "done", "new_event": True}}]))
    _memory_run()
    result = _tool_results(llm.calls_for("fake-memory")[1])[0]
    assert result["error_code"] == "UNKNOWN_TOOL" and count(violetdb) == 2


@pytest.mark.parametrize("extra", [{"event_key": "brand_new_key"}, {"kind": "background"}, {"kind": "fact"},
                                   {"related_to": None}, {"new_event": "yes"}])
def test_changing_other_arguments_does_not_skip_the_gate(memory, violetdb, round15, extra):
    with run_context():
        result = save(memory, **{"summary": DONE_TEXT, "progress": "done", "new_event": True, **extra})
    assert result["action"] == "needs_decision" and count(violetdb) == 2


# ---------------------------------------------------------------------------
# A: the prompt
# ---------------------------------------------------------------------------

def test_memory_rules_put_the_open_event_list_first():
    from ai.agents.memory_agent import MEMORY_RULES
    from ai.tool_schemas import SAVE_MEMORY_SCHEMA
    assert "列表里的 id 可以直接用于 update_event_progress，不需要先 query_memory" in MEMORY_RULES
    assert "query_memory 返回空，不代表列表里的事件不存在" in MEMORY_RULES
    assert "确认是另一件事才传 new_event=true" in MEMORY_RULES
    assert "不要为了少调用一次而跳过这个判断" in MEMORY_RULES
    assert "不要强行修改原事件" in MEMORY_RULES
    assert "没看过候选时传 true 会先返回候选" in SAVE_MEMORY_SCHEMA["function"]["parameters"]["properties"]["new_event"]["description"]


# ---------------------------------------------------------------------------
# Round 16 acceptance check: an ended copy next to the open original is a failure
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def session(clean_env, violetdb, tmp_path):
    from tools.violet_test_chat import build_parser, open_session
    from tools.harness import ScenarioClock
    s = open_session(build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-b6"), "--quiet-hours", "off"]),
                     out=lambda x: None, db=violetdb)
    s.use_clock(ScenarioClock(datetime.fromisoformat("2026-10-22T16:00:00+08:00")))
    yield s
    s.close()


def test_round16_check_flags_an_open_and_an_ended_copy_of_one_event(session, violetdb):
    from tools.dialogue_eval import _proactive
    defense = violetdb.add_event_memory(DEFENSE, category="general", kind="event", progress="unknown",
                                     event_time="2026-10-21T19:23:00+08:00")
    violetdb.add_event_memory(INTERVIEW, category="event", kind="event", progress="unknown")
    copy = violetdb.add_event_memory(DONE_TEXT, category="event", kind="event", progress="done")
    result = _proactive(session, "ended_event_not_allowed")
    assert result["ok"] is False
    assert [(c["ended_id"], c["open_id"]) for c in result["contradictory_events"]] == [(copy, defense)]
    violetdb.retract_event_memory(copy)
    violetdb.update_event_progress(defense, "done", content=DONE_TEXT)
    fixed = _proactive(session, "ended_event_not_allowed")
    assert fixed["ok"] is True and fixed["contradictory_events"] == []


def test_round16_check_does_not_flag_different_events(violetdb):
    violetdb.add_event_memory(INTERVIEW, category="event", kind="event", progress="unknown")
    violetdb.add_event_memory(DONE_TEXT, category="event", kind="event", progress="done")
    violetdb.add_event_memory("用户周末去看了一场电影", kind="event", progress="done")
    assert violetdb.ended_event_contradictions() == []


def test_round16_check_does_not_flag_two_ended_records_of_one_topic(violetdb):
    violetdb.add_event_memory(DONE_TEXT, category="event", kind="event", progress="done")
    violetdb.add_event_memory("用户的天文观测课程答辩彩排已结束", category="event", kind="event", progress="done")
    assert violetdb.ended_event_contradictions() == []
