"""Phase 8.9.5: what the 8.9.4 real run (local/p894/real-a-20261020-222955) showed, as regressions on its records.

1. classification: A1 step 5 (ProactiveAgent 503 recovered in the same run, message sent) is "recovered"; A3 step 4
   (IntentAgent gave up, ai.chat fell back to a plain chat turn, Violet really replied) is "degraded"; the canned
   fallback reply is never a completed reply;
2. A4 step 1: the model's English drafting notes in a reply are a guard finding (rewrite, then drop);
3. proactive messages that keep ending on a check-in question get a note built from what Violet actually sent;
   a plan talked about lately can be picked up again; "在吗 好久没聊了" is a return.
Offline: what is classified, checked and told -- not how a real model now writes."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.test_life_relationship_p89 import clock  # noqa: F401
from tests.test_phase2_scenarios import make_world  # noqa: F401
from tools import chat_dialogue as cd

# ============================================================================== 1. classification on the real records

A1_STEP5_CALLS = [{"call_no": 9, "agent": "ProactiveAgent", "run_id": 9, "step": 1, "ok": False, "status_code": 503,
                   "error_type": "InternalServerError", "model": "gpt-6-luna", "latency_ms": 17594},
                  {"call_no": 10, "agent": "ProactiveAgent", "run_id": 9, "step": 2, "ok": True, "status_code": None,
                   "error_type": None, "model": "gpt-6-luna", "latency_ms": 21859},
                  {"call_no": 11, "agent": "ChatAgent", "run_id": 10, "step": 1, "ok": True, "status_code": None,
                   "error_type": None, "model": "gpt-6-luna", "latency_ms": 14969}]
A1_STEP5_RUNS = [{"run_id": 9, "agent": "ProactiveAgent", "ok": True}, {"run_id": 10, "agent": "ChatAgent", "ok": True}]
A3_STEP4_CALLS = [{"call_no": n, "agent": "IntentAgent", "run_id": 7, "step": n - 6, "ok": False, "status_code": 503,
                   "error_type": "InternalServerError", "model": "gpt-6-luna", "latency_ms": 8000} for n in (7, 8, 9)] + [
                  {"call_no": 10, "agent": "ChatAgent", "run_id": 8, "step": 1, "ok": True, "status_code": None,
                   "error_type": None, "model": "gpt-6-luna", "latency_ms": 7671}]
A3_STEP4_RUNS = [{"run_id": 7, "agent": "IntentAgent", "ok": False}, {"run_id": 8, "agent": "ChatAgent", "ok": True}]
A3_REPLY = ["有呀，偶尔会想起你，想着你什么时候又来找我聊天", "今天见到你，还是挺开心的嘿嘿"]


def test_a1_proactive_503_recovered_in_the_same_run():
    recovered, unrecovered = cd.classify_failed_calls(A1_STEP5_CALLS, A1_STEP5_RUNS)
    assert unrecovered == [] and [(c["agent"], c["status_code"], c["recovered_by_call"]) for c in recovered] == [
        ("ProactiveAgent", 503, 10)]


def test_a3_intent_gave_up_but_eve_really_replied():
    recovered, unrecovered = cd.classify_failed_calls(A3_STEP4_CALLS, A3_STEP4_RUNS)
    assert recovered == [] and len(unrecovered) == 3
    assert cd.degraded_by_design(unrecovered, A3_STEP4_RUNS, A3_REPLY, [])


@pytest.mark.parametrize("why,runs,violet,errors", [
    ("the canned fallback was sent", A3_STEP4_RUNS, [cd.FALLBACK_REPLY], []),
    ("ChatAgent failed too", [{"run_id": 7, "agent": "IntentAgent", "ok": False},
                              {"run_id": 8, "agent": "ChatAgent", "ok": False}], A3_REPLY, []),
    ("no reply at all", A3_STEP4_RUNS, [], []),
    ("a memory job failed", A3_STEP4_RUNS, A3_REPLY, [{"kind": "memory_job"}]),
])
def test_a_real_failure_is_never_called_degraded(why, runs, violet, errors):
    _, unrecovered = cd.classify_failed_calls(A3_STEP4_CALLS, runs)
    assert not cd.degraded_by_design(unrecovered, runs, violet, errors), why


def test_only_intent_failures_degrade():
    calls = [{**c, "agent": "MemoryAgent"} for c in A3_STEP4_CALLS[:3]]
    runs = [{"run_id": 7, "agent": "MemoryAgent", "ok": False}, {"run_id": 8, "agent": "ChatAgent", "ok": True}]
    _, unrecovered = cd.classify_failed_calls(calls, runs)
    assert not cd.degraded_by_design(unrecovered, runs, A3_REPLY, [])


def test_the_fallback_constant_is_the_runtimes():
    import violet_runtime
    assert cd.FALLBACK_REPLY == violet_runtime.DEFAULT_FALLBACK_REPLY


# ============================================================================== 2. drafting notes in a reply

A4_LEAK = ('快来聊会儿，今天想从哪件小事说起呀陪伴? no. Question okay but perhaps don\'t mechanical. '
           '"我给你留着位置呢" maybe service-y but cute. Use 2 lines.')


@pytest.mark.parametrize("line,leak", [(A4_LEAK, True), ("Let me think, maybe use a softer tone here", True),
                                       ("I miss you so much", False), ("好的ok", False), ("今天又要赶deadline", False),
                                       ("I'll be there, don't worry about it okay", False),
                                       ("maybe I should sleep now", False),             # two drafting words: a reply
                                       ("想你了 I love you more than words", False), ("满意没ablytyped", False)])
def test_drafting_notes_are_told_apart_from_english_in_chat(line, leak):
    from ai.consistency import leaked_draft
    assert leaked_draft(line) is leak


def test_a_leaked_draft_is_rewritten_then_dropped(violetdb):
    import ai.chat as chat
    notes = []
    text, violations = chat.guard_reply("回来啦，小满～我一下子就开心了\n" + A4_LEAK,
                                        regenerate=lambda note: notes.append(note) or "回来啦，小满～我一下子就开心了\n快来跟我说说嘛")
    assert [v.check for v in violations] == ["leaked_draft"] and text.endswith("快来跟我说说嘛")
    assert "混进了英文的草稿" in notes[0]
    text, _ = chat.guard_reply("回来啦\n" + A4_LEAK, regenerate=lambda note: "回来啦\n" + A4_LEAK)
    assert text == "回来啦"                                              # still leaking: that line goes, the rest stays
    text, violations = chat.guard_reply(A4_LEAK, entity="proactive_reply")
    assert violations and A4_LEAK not in text                            # proactive messages too


# ============================================================================== 3. proactive: shapes and reasons

def test_check_in_endings_of_the_real_messages():
    from ai.proactive_scheduler import CHECKIN_RE
    for text in ("今天过得还顺吗？", "小满今晚吃得怎么样呀？", "今天准备怎么过呀？", "你今天过得怎么样呀"):   # 8.9.4 A1/A2/A3
        assert CHECKIN_RE.search(text), text
    for text in ("刚刚又把那张老爵士专辑放起来了", "有点想你了", "周末想去书店挑本新书，你帮我选？"):
        assert not CHECKIN_RE.search(text), text


def sent_event(db, at, texts):
    event_id = db.add_proactive_event(event_type="scheduled_proactive_check", status="planned", action_type="chat",
                                      reason="t", decision={}, metadata={}, scheduled_at=at.isoformat())
    db.update_proactive_event(event_id, status="sent", sent_messages=[{"type": "text", "content": t} for t in texts])
    db.execute("UPDATE proactive_events SET created_at = ?, updated_at = ? WHERE id = ?", (at.isoformat(), at.isoformat(), event_id))


def test_her_recent_check_ins_become_a_note(make_world, clock):
    from tests.test_life_relationship_p89 import START
    w = make_world()
    sent_event(w.db, START - timedelta(hours=20), ["我刚刚反复听了几遍喜欢的钢琴曲，听着听着又想你了", "小满今晚吃得怎么样呀？"])
    sent_event(w.db, START - timedelta(hours=6), ["早呀～论文交完后的第一天，状态有没有轻松一点", "今天准备怎么过呀？"])
    sent_event(w.db, START - timedelta(days=5), ["很久以前的一条：今天过得怎么样"])          # older than 3 days
    context = w.scheduler._build_event_context(START)
    assert [r["checkin"] for r in context["recent_proactive"]] == [True, True]
    note = context["proactive_habit_note"]
    assert "主动找对方 2 次，有 2 次是用问近况收尾的" in note and "今天准备怎么过呀" in note


def test_the_note_reaches_the_proactive_message(make_world, clock):
    from tests.test_life_relationship_p89 import START
    w = make_world()
    sent_event(w.db, START - timedelta(hours=6), ["想你啦", "今天过得怎么样？"])
    note = w.scheduler._build_event_context(START)["proactive_habit_note"]
    from ai.agents.base_agent import _CALL_CONTEXT
    token = _CALL_CONTEXT.set({"is_proactive": True, "proactive_result": {"should_act": True}, "proactive_note": note})
    try:
        messages = w.agents["chat"].build_context_messages("[proactive event]")
    finally:
        _CALL_CONTEXT.reset(token)
    assert any(m["role"] == "system" and m["content"] == note for m in messages)
    assert "不一定要提问" in messages[-1]["content"]


def test_the_pipeline_carries_the_note_end_to_end(make_world, clock):
    """Scheduler -> decision -> ChatAgent (proactive) -> sent: the note built from her sent messages reaches the
    request that writes the new message, and the new message is sent and logged."""
    from tests.test_life_relationship_p89 import START, history
    w = make_world()
    history(w.db, 8, user_text="今天好累 想找你聊聊")                    # close; the user last wrote days ago
    sent_event(w.db, START - timedelta(hours=7), ["想你啦", "今天过得怎么样？"])
    w.db.set_proactive_setting("last_engaged_user_turn_at", (START - timedelta(hours=6)).isoformat())   # cold streak 0
    w.llm.responders["fake-proactive"] = lambda kw: json.dumps(
        {"should_act": True, "action_type": "chat", "reason": "想她了", "topic_key": "miss", "mentioned_memory_ids": [],
         "mentioned_life_event_ids": [], "mentioned_web_finding_ids": [], "chat_brief": {"topic": "miss"},
         "task": {"name": None}}, ensure_ascii=False)
    w.llm.responders["fake-chat"] = lambda kw: "刚刚突然想到你了，就是想跟你说一声"
    w.db.set_proactive_setting("next_proactive_at", (START - timedelta(minutes=1)).isoformat())
    snapshot = w.scheduler.status_snapshot()
    assert snapshot["due_reason"] == "due", snapshot
    w.scheduler.tick()
    assert w.llm.calls_for("fake-proactive"), "the decision was never asked"
    request = w.llm.calls_for("fake-chat")[-1]["messages"]
    assert any((m.get("content") or "").startswith("【你最近主动发的消息】") for m in request)
    assert w.client.sent_texts[-1] == "刚刚突然想到你了，就是想跟你说一声"
    decision = json.loads(w.llm.calls_for("fake-proactive")[-1]["messages"][-1]["content"])
    assert decision["proactive_habit_note"] and "miss" in [m["type"] for m in decision["violet_motives"]]


def test_no_check_ins_no_note(make_world, clock):
    from tests.test_life_relationship_p89 import START
    w = make_world()
    sent_event(w.db, START - timedelta(hours=6), ["刚刚又把那张老爵士专辑放起来了"])
    context = w.scheduler._build_event_context(START)
    assert context["proactive_habit_note"] is None and len(context["recent_proactive"]) == 1


def test_a_plan_talked_about_lately_is_picked_up_again(make_world, clock):
    from ai.life_store import LifeStore
    from tests.test_life_relationship_p89 import START, add_message, history
    w = make_world()
    history(w.db, 8, user_text="今天好累 想找你聊聊")
    store = LifeStore(w.db)
    thread = store.create_thread("t:书", "reading", "读《小王子》", "小王子", (START - timedelta(days=5)).isoformat())
    store.create_plan(thread, "周末想去书店挑一本新书", (START + timedelta(days=1)).isoformat())
    assert "ask_opinion" in [m["type"] for m in w.scheduler._build_event_context(START)["violet_motives"]]
    add_message(w.db, "user", "那就书店吧 周末去挑本新书", START - timedelta(hours=10))
    motives = w.scheduler._build_event_context(START)["violet_motives"]
    kinds = [m["type"] for m in motives]
    assert "continue" in kinds and "ask_opinion" not in kinds
    assert next(m for m in motives if m["type"] == "continue")["about"] == "周末想去书店挑一本新书"


def test_missing_may_be_just_a_sweet_line():
    from ai.relationship import RelationshipView, proactive_motives
    note = proactive_motives(RelationshipView("close"), 9, 0, [], [])[0]["note"]
    assert "撒个娇讨点关注" in note and "不一定要问对方近况" in note and "不埋怨对方没来找她" in note


def test_close_allows_shyness_and_little_tempers():
    from ai.relationship import RelationshipView, describe
    close = describe(RelationshipView("close", evidence=["聊过 6 天"]))
    assert "害羞、俏皮或闹点小脾气" in close and "不让对方愧疚" in close
