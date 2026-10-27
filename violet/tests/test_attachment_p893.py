"""Phase 8.9.3: Violet grows from shy to clingy as the relationship does -- from evidence (how you talked, what you
told her), never from a round count -- and reaches out because she has a reason (something to share, missing
you, a small decision), not because it is morning. Offline: these prove what Violet is told and offered, not how
a real model now talks."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from ai import interaction as ia
from ai.relationship import RelationshipView, assess, describe, proactive_motives
from tests.test_consistency_p891 import seed_episode  # noqa: F401
from tests.test_life_relationship_p89 import START, add_message, clock, history, section  # noqa: F401
from tests.test_phase2_scenarios import make_world  # noqa: F401

PERSONA = json.loads((__import__("ai.persona", fromlist=["persona_dir"]).persona_dir() / "persona_core.json")
                     .read_text(encoding="utf-8"))


def remember(db, n, kind="event", status="active"):
    for i in range(n):
        db.execute("INSERT INTO event_memories (content, kind, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                   (f"对方的事 {i}", kind, status, db.now(), db.now()))


# ============================================================================== growth from evidence

def test_three_stages_grow_from_shy_to_clingy():
    new, warming, close = (describe(RelationshipView(s, evidence=["聊过 6 天"])) for s in ("new", "warming", "close"))
    assert "害羞" in new and "等熟一点再说" in new
    assert "主动找话题" in warming and "偶尔撒个娇" in warming and "有点想对方" in warming
    # phase 8.9.8: "请对方帮你拿主意" is said in the persona only (repeated here it made Violet undecided again and again)
    for word in ("明显黏人", "主动讨点关注", "分享小心事", "自己有主意就直接说", "想念就说出来", "舍不得结束"):
        assert word in close, word
    assert "拿主意" not in close.replace("自己有主意", "")
    assert "不是每句都要有" in close and "不让对方愧疚" in close and "不要求对方只陪你" in close


def test_many_messages_in_one_day_are_not_closeness(violetdb, clock):
    at = START - timedelta(hours=5)
    for i in range(40):                                        # forty messages, one evening: still getting to know
        add_message(violetdb, "user", "哈哈哈 想你了", at + timedelta(minutes=i))
    assert assess(violetdb, START).stage == "new"


def test_what_the_user_told_her_counts_as_closeness(violetdb, clock):
    history(violetdb, 6)                                          # reaches out on many days, never warm or playful
    assert assess(violetdb, START).stage == "warming"
    remember(violetdb, 2)
    remember(violetdb, 3, status="forgotten")                     # what the user had forgotten does not count
    assert assess(violetdb, START).stage == "warming"
    remember(violetdb, 1, kind="fact")
    view = assess(violetdb, START)
    assert view.stage == "close" and view.shared == 3 and "对方跟你说过不少自己的事" in describe(view)


def test_a_wish_for_distance_still_wins_over_everything(violetdb, clock):
    history(violetdb, 8, user_text="今天好累 想找你聊聊")
    remember(violetdb, 5)
    add_message(violetdb, "user", "别这么黏 正常点说话", START - timedelta(hours=1))
    view = assess(violetdb, START)
    assert view.stage == "distance" and "不撒娇" in describe(view)


# ============================================================================== persona 1.2.0

def test_persona_is_soft_and_leans_on_the_user_but_does_not_bind():
    assert PERSONA["version"] == "1.2.0"
    text = json.dumps(PERSONA, ensure_ascii=False)
    for word in ("软萌", "黏人", "请对方帮忙拿主意", "舍不得", "顺着对方的安排", "黏人但不绑人", "不让对方愧疚",
                 "不要求对方只陪自己", "不让对方疏远别人"):
        assert word in text, word
    assert "有自己的主见" not in text and "同意就说同意，不同意会温和地说出自己的看法" not in text
    assert "真诚比讨好重要" in PERSONA["values"] and "尊重别人的边界和节奏" in PERSONA["values"]


# ============================================================================== leaving

@pytest.mark.parametrize("stage,reluctant", [("close", True), ("warming", True), ("new", False), ("distance", False),
                                             (None, False)])
def test_leaving_may_be_a_little_reluctant_once_close(stage, reluctant):
    note = ia.turn_note("我要睡了", None, stage=stage)
    assert ("舍不得" in note) is reluctant
    assert "不提问" in note and "不用再叮嘱" in note
    if reluctant:
        assert "顺着对方的安排" in note and "不挽留" in note


def test_e4_leaving_in_context(violetdb, clock):
    from ai.context_engine import ContextEngine
    episode = seed_episode(violetdb, clock, "E4")
    at = datetime.fromisoformat(episode["start"])
    for text in ("在干嘛呢", "你是不是又在拖延看书", "哈哈哈 被我说中了吧"):
        add_message(violetdb, "user", text, at)
        add_message(violetdb, "assistant", "嗯嗯", at + timedelta(seconds=20))
        at += timedelta(minutes=1)
    clock.set(at)
    packet = ContextEngine(violetdb).build("chat", "我要睡了")
    assert "已经很亲近了" in section(packet, "【你们的相处") and "舍不得" in packet.turn_note


def test_a_new_user_leaving_gets_the_plain_note(violetdb, clock):
    from ai.context_engine import ContextEngine
    seed_episode(violetdb, clock, "E1")
    assert "舍不得" not in ContextEngine(violetdb).build("chat", "我去洗澡了").turn_note


# ============================================================================== affection: more, not the same

def test_affection_note_asks_for_variety_not_restraint():
    note = ia.habit_note(["想你了呀", "抱抱"], "你好可爱", last_turn=["抱抱"])
    assert "想继续亲近也很好" in note and "别重复同一句" in note and "不用再加码" not in note
    note = ia.habit_note(["想你了呀"], "就这？", last_turn=["想你了呀"])
    assert "可以大方一点、更可爱地回应" in note and "越说越重" not in note


# ============================================================================== reasons to reach out

def view(stage, cooled=False):
    return RelationshipView(stage, cooled=cooled)


PLAN = [{"id": 7, "content": "这周想把《边城》读完"}]
EVENT = [{"id": 3, "content": "又往后读了几章《边城》"}]


@pytest.mark.parametrize("stage,hours,cold,expected", [
    ("close", 8, 0, ["miss"]), ("close", 3, 0, []),               # a few hours is no time at all
    ("warming", 8, 0, []), ("warming", 30, 0, ["miss"]),          # missing grows with closeness
    ("close", 30, 1, []),                                         # an unanswered message: wait for the user
    ("new", 72, 0, []), ("distance", 72, 0, []),
])
def test_missing_the_user_depends_on_closeness_time_and_their_response(stage, hours, cold, expected):
    assert [m["type"] for m in proactive_motives(view(stage), hours, cold, [], [])] == expected


def test_a_cooled_relationship_does_not_say_it_missed_you():
    assert proactive_motives(view("warming", cooled=True), 600, 0, [], []) == []


def test_her_news_and_her_small_decisions():
    close = proactive_motives(view("close"), 1, 0, EVENT, PLAN)
    assert [m["type"] for m in close] == ["share", "ask_opinion"] and close[1]["about"] == "这周想把《边城》读完"
    assert "请对方帮她出个主意" in close[1]["note"] and "拿不定主意" not in close[1]["note"]   # no invented doubt
    assert [m["type"] for m in proactive_motives(view("new"), 1, 0, EVENT, PLAN)] == ["share"]   # too early to lean
    assert proactive_motives(view("distance"), 1, 0, EVENT, PLAN) == []


def test_missing_note_never_blames_or_hurries():
    note = proactive_motives(view("close"), 9, 0, [], [])[0]["note"]
    assert "想对方了" in note and "不埋怨对方没来找她" in note and "不催对方回" in note


def test_the_scheduler_offers_her_reasons_with_the_relationship(make_world, clock):
    from ai.life_store import LifeStore
    w = make_world()
    history(w.db, 8, user_text="今天好累 想找你聊聊")             # close; the last message is days ago
    store = LifeStore(w.db)
    thread = store.create_thread("t:边城", "reading", "读《边城》", "边城", (START - timedelta(days=5)).isoformat())
    store.create_plan(thread, "这周想把《边城》读完", (START + timedelta(days=1)).isoformat())
    context = w.scheduler._build_event_context(START)
    assert context["relationship_stage"] == "close"
    assert [m["type"] for m in context["violet_motives"]] == ["miss", "ask_opinion"]
    assert "不用为了发消息而按时间段问候" in context["motive_note"]
    decision = w.scheduler.proactive_agent.build_context_messages(json.dumps(context, ensure_ascii=False))
    assert any("【你们的相处" in (m["content"] or "") and "已经很亲近了" in m["content"] for m in decision)


def test_the_scheduler_stays_quiet_after_an_unanswered_message(make_world, clock):
    w = make_world()
    history(w.db, 8, user_text="今天好累 想找你聊聊")
    w.scheduler.tracker.cold_streak = lambda: 1                  # Violet wrote last time and got no reply
    assert [m["type"] for m in w.scheduler._build_event_context(START)["violet_motives"]] == []
