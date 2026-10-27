"""Phase 8.9.1: four consistency fixes found by the 8.9 real-run preflight (local/p89/preflight/fake-e1e4e5).

1. a daily item never starts while Violet is in the middle of a conversation, and stops being "正在做" once she replied;
2. no "上学还是工作" get-to-know hint once the two of you are close or have talked for weeks;
3. "都没顾上找你" / "跟你说个事" / negated phrases are no evidence of warmth;
4. a life thread talked about in an earlier conversation is marked as told (title, or "你那本书" with one book going).
Offline: these prove what Violet is given and what is stored, not how a real model replies."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.test_life_relationship_p89 import (START, add_message, busy_day, clock, daily_rows,  # noqa: F401
                                              section)


def at_day(day, hour, minute=0, tz=START.tzinfo):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def seed_episode(db, clock, eid):
    """The original 8.7 episode seed (file unchanged), with the scenario clock at its start."""
    from tools import chat_dialogue as cd
    data, _ = cd.load_episodes()
    episode = next(e for e in data["episodes"] if e["id"] == eid)
    clock.set(datetime.fromisoformat(episode["start"]))
    cd._seed(SimpleNamespace(db=db), episode["seed"])
    return episode


# ============================================================================== 1. daily items vs. chatting

def chat_around(db, start, before=5, after=4):
    add_message(db, "user", "随便聊点什么吧", start - timedelta(minutes=before))
    add_message(db, "assistant", "好呀", start - timedelta(minutes=before - 1))
    add_message(db, "user", "听了 还行", start + timedelta(minutes=after))


@pytest.mark.parametrize("incoming_logged", [True, False])
def test_an_item_that_would_start_mid_conversation_did_not_happen(violetdb, clock, incoming_logged):
    from ai.daily_life import DailyLife
    day, plan = busy_day(violetdb, START.tzinfo)
    item = plan[1]
    chat_around(violetdb, item["start"])
    if not incoming_logged:                              # the message being answered is not in the log yet
        violetdb.execute("DELETE FROM messages WHERE content = '听了 还行'")
    now = item["start"] + timedelta(minutes=4)
    view = DailyLife(violetdb).day(day, now)
    assert view["doing"] is None and item["done"] not in [e["content"] for e in view["done"]]
    assert [r["content"] for r in daily_rows(violetdb)] == [plan[0]["done"]]           # the earlier one still happened
    if not incoming_logged:                                                          # then the runtime logs it
        add_message(violetdb, "user", "听了 还行", now)
    late = DailyLife(violetdb).day(day, at_day(day, 23))                                # and it stays that way
    assert item["done"] not in [e["content"] for e in late["done"]]
    assert item["done"] not in [r["content"] for r in daily_rows(violetdb)]


def test_a_quiet_gap_before_or_after_is_not_a_conversation(violetdb, clock):
    from ai.daily_life import DailyLife
    day, plan = busy_day(violetdb, START.tzinfo)
    item = plan[1]
    add_message(violetdb, "user", "我去忙啦", item["start"] - timedelta(minutes=40))     # chat ended long before
    add_message(violetdb, "user", "回来了", item["start"] + timedelta(minutes=5))
    view = DailyLife(violetdb).day(day, item["start"] + timedelta(minutes=5))
    assert view["doing"]["done"] == item["done"]                                      # the user caught her at it


def test_in_progress_until_she_replies_then_done(violetdb, clock):
    from ai.daily_life import DailyLife
    day, plan = busy_day(violetdb, START.tzinfo)
    item = plan[1]
    add_message(violetdb, "user", "在干嘛呢", item["start"] + timedelta(minutes=10))
    asked = DailyLife(violetdb).day(day, item["start"] + timedelta(minutes=10))
    assert asked["doing"]["doing"] == item["doing"]                                   # "现在：在…" when asked
    add_message(violetdb, "assistant", "在外面呢", item["start"] + timedelta(minutes=11))
    add_message(violetdb, "user", "哈哈", item["start"] + timedelta(minutes=12))
    later = DailyLife(violetdb).day(day, item["start"] + timedelta(minutes=12))
    assert later["doing"] is None and item["done"] in [e["content"] for e in later["done"]]
    row = next(r for r in daily_rows(violetdb) if r["content"] == item["done"])
    assert row["duration_minutes"] == item["minutes"]                                 # the stored fact is unchanged


def test_an_event_stored_before_the_fix_is_kept(violetdb, clock):
    from ai.daily_life import SOURCE, DailyLife
    from ai.life_store import LifeStore
    day, plan = busy_day(violetdb, START.tzinfo)
    item = plan[1]
    LifeStore(violetdb).insert_event(uid=item["uid"], thread_id=None, plan_id=None, event_type="daily", content=item["done"],
                                  occurred_at=item["start"].isoformat(), duration_minutes=item["minutes"], source=SOURCE)
    chat_around(violetdb, item["start"])
    view = DailyLife(violetdb).day(day, at_day(day, 23))
    assert item["done"] in [e["content"] for e in view["done"]]                       # history is not rewritten
    assert len([r for r in daily_rows(violetdb) if r["uid"] == item["uid"]]) == 1


def test_e5_step_9_eve_is_chatting_not_napping(violetdb, clock):
    """8.9 preflight: 15:15 "午睡不小心睡过头了" was stored while E5 ran 15:00-15:21, step 9 read "现在：在午睡"."""
    from ai.context_engine import ContextEngine
    from ai.daily_life import DailyLife
    episode = seed_episode(violetdb, clock, "E5")
    start = datetime.fromisoformat(episode["start"])
    DailyLife(violetdb).recent(start)                       # the days before, as the context builds them (repeat window)
    plan = DailyLife(violetdb).plan_for(start.date(), start.tzinfo)
    nap = next(p for p in plan if p["done"] == "午睡不小心睡过头了")                  # precondition: the config's day
    assert start < nap["start"] < start + timedelta(minutes=19)
    at = start
    for step in episode["steps"][:8]:
        at += timedelta(minutes=int(step["after"].rstrip("m")))
        add_message(violetdb, "user", step["user"], at)
        add_message(violetdb, "assistant", "嗯嗯", at + timedelta(seconds=20))
    clock.set(start + timedelta(minutes=19))
    # as in the product: the first context of the turn (IntentAgent) is built before "听了 还行" is in the log
    life = section(ContextEngine(violetdb).build("chat", "听了 还行"), "【Violet 的生活状态")
    assert "午睡" not in life and "现在：没在做什么特别的事" in life
    assert "午睡不小心睡过头了" not in [r["content"] for r in daily_rows(violetdb)]


@pytest.fixture(scope="module")
def episodes_run():
    """E4 and E5 through the whole product pipeline (mock): the turn order there is what hid the nap from 8.9."""
    import uuid
    from tools import chat_dialogue as cd
    out = cd.ROOT / "local/p891/test-cases" / uuid.uuid4().hex
    summary = cd.run(out, label="p891", episode_ids=["E4", "E5"])
    assert summary["state"] == "complete"
    return json.loads((out / "debug.json").read_text(encoding="utf-8"))["episodes"]


def chat_system(step):
    request = next(r["request"] for r in step["requests"] if r["agent"] == "ChatAgent")
    return "\n".join(m["content"] for m in request if m["role"] == "system")


def test_e5_pipeline_no_nap_while_chatting(episodes_run):
    steps = episodes_run["E5"]["steps"]
    assert all("午睡" not in chat_system(s) for s in steps)
    assert "现在：没在做什么特别的事" in chat_system(steps[8])                       # 听了 还行 (15:19)


def test_e4_pipeline_no_census_question_and_the_book_was_talked_about(episodes_run):
    steps = episodes_run["E4"]["steps"]
    assert all("【可选的了解】" not in chat_system(s) for s in steps)
    assert "读《边城》" in chat_system(steps[0]) and "之前跟对方提过" in chat_system(steps[0])
    assert "慢慢熟起来了" in chat_system(steps[0]) and "已经很亲近了" in chat_system(steps[3])   # 想我没


# ============================================================================== 2. get-to-know hints

def onboarding(db, text="今天去看了场电影"):
    from ai.context_engine import ContextEngine
    packet = ContextEngine(db).build("chat", text)
    offered = db.fetch_all("SELECT fact_key FROM profile_questions WHERE outcome = 'offered'")
    return "【可选的了解】" in "\n".join(packet.sections), offered


def test_e4_weeks_of_chatting_get_no_census_question(violetdb, clock):
    seed_episode(violetdb, clock, "E4")
    shown, offered = onboarding(violetdb, "你是不是又在拖延看书")                       # E4 step 2
    assert not shown and offered == []                                               # not offered, nothing recorded


def test_a_new_user_is_still_got_to_know(violetdb, clock):
    seed_episode(violetdb, clock, "E1")
    shown, offered = onboarding(violetdb)
    assert shown and [o["fact_key"] for o in offered] == ["identity.status"]


@pytest.mark.parametrize("view,welcome", [
    (dict(stage="new", chat_days=1, span_days=0), True),
    (dict(stage="warming", chat_days=3, span_days=10), True),          # a few days in: still getting to know
    (dict(stage="warming", chat_days=6, span_days=21), False),         # weeks on many days (E4 before the teasing)
    (dict(stage="warming", chat_days=6, span_days=60, cooled=True), False),
    (dict(stage="close", chat_days=2, span_days=3), False),
    (dict(stage="distance", chat_days=1, span_days=0), False),         # asked for distance: no personal questions
])
def test_who_is_still_getting_to_know_each_other(view, welcome):
    from ai.relationship import RelationshipView, getting_to_know
    assert getting_to_know(RelationshipView(**view)) is welcome


def test_distance_stops_the_hint_in_context(violetdb, clock):
    seed_episode(violetdb, clock, "E1")
    add_message(violetdb, "user", "别撒娇了 正常点说话", START.replace(day=2, hour=20, minute=0))
    shown, offered = onboarding(violetdb)
    assert not shown and offered == []


def test_switches_keep_the_old_behaviour(violetdb, clock, monkeypatch):
    seed_episode(violetdb, clock, "E4")
    monkeypatch.setenv("VIOLET_RELATIONSHIP_STATE", "0")
    shown, _ = onboarding(violetdb, "你是不是又在拖延看书")
    assert shown                                                                      # 8.9 behaviour when switched off


# ============================================================================== 3. warmth evidence

@pytest.mark.parametrize("text,warm", [
    ("今天好忙 都没顾上找你", False), ("跟你说个事", False), ("告诉你个好消息", False), ("我不想你", False),
    ("没人陪我", False), ("你真好笑", False), ("顾不上找你", False),
    ("想你了", True), ("好想找你聊聊", True), ("有没有想我", True), ("想我没", True), ("你说话挺可爱的", True),
    ("我不知道为什么就想找你", True), ("陪我聊会儿", True), ("今天好累 想找你聊聊", True),
])
def test_warm_words_need_warm_meaning(text, warm):
    from ai.relationship import is_warm
    assert is_warm(text) is warm


def test_e4_history_alone_is_warming_and_teasing_makes_it_close(violetdb, clock):
    from ai.relationship import assess, describe
    episode = seed_episode(violetdb, clock, "E4")
    view = assess(violetdb)
    assert view.stage == "warming" and view.warm == 0 and "还不用太黏" in describe(view)     # 8.9.3 wording
    at = datetime.fromisoformat(episode["start"])
    for text in ("在干嘛呢", "你是不是又在拖延看书", "哈哈哈 被我说中了吧"):
        add_message(violetdb, "user", text, at)
        at += timedelta(minutes=1)
    clock.set(at)
    assert assess(violetdb).stage == "close"                                             # playful twice: close now
    add_message(violetdb, "user", "想我没", at)
    assert assess(violetdb).warm == 1


# ============================================================================== 4. told in an earlier conversation

def thread_line(db, text="在干嘛呢"):
    from ai.context_engine import ContextEngine
    life = section(ContextEngine(db).build("chat", text), "【Violet 的生活状态")
    return life, next(l for l in life.splitlines() if l.startswith("- 正在进行："))


def test_e4_the_book_asked_about_before_is_not_news(violetdb, clock):
    seed_episode(violetdb, clock, "E4")
    life, line = thread_line(violetdb)
    assert "读《边城》" in line and "之前跟对方提过" in line                          # 10-20 "你那本书看完没"
    event = next(l for l in life.splitlines() if "又往后读了几章《边城》" in l)
    assert "之前跟对方提过" not in event                                              # happened 11-04, after that talk


@pytest.fixture()
def book(violetdb, clock):
    from ai.life_store import LifeStore
    store = LifeStore(violetdb)
    store.create_thread("t:边城", "reading", "读《边城》", "边城", (START - timedelta(days=10)).isoformat())
    return store


def test_your_book_with_two_books_going_is_ambiguous(violetdb, book):
    book.create_thread("t:小王子", "reading", "读《小王子》", "小王子", (START - timedelta(days=9)).isoformat())
    add_message(violetdb, "user", "你那本书看完没", START - timedelta(days=3))
    add_message(violetdb, "assistant", "还没", START - timedelta(days=3, seconds=-30))
    _, line = thread_line(violetdb)
    assert "之前跟对方提过" not in line


def test_an_unanswered_question_or_a_talk_before_the_thread_is_no_evidence(violetdb, book):
    add_message(violetdb, "user", "你那本书看完没", START - timedelta(days=3))           # Violet never answered
    add_message(violetdb, "assistant", "我想读《边城》很久了", START - timedelta(days=20))  # before she started it
    _, line = thread_line(violetdb)
    assert "之前跟对方提过" not in line


def test_a_title_named_earlier_counts_but_this_conversation_wins(violetdb, book):
    add_message(violetdb, "assistant", "我最近在读《边城》", START - timedelta(days=2))
    _, line = thread_line(violetdb)
    assert "之前跟对方提过" in line
    add_message(violetdb, "user", "在吗", START - timedelta(minutes=2))
    add_message(violetdb, "assistant", "在呢，刚读了几页《边城》", START - timedelta(minutes=1))
    _, line = thread_line(violetdb, "嗯")
    assert "这次聊天里已经说过了" in line and "之前跟对方提过" not in line


def test_this_conversation_is_not_an_earlier_one(violetdb, book):
    add_message(violetdb, "user", "你在读《边城》吗", START - timedelta(minutes=1))      # asked just now, not answered yet
    _, line = thread_line(violetdb, "你最近在干嘛")
    assert "之前跟对方提过" not in line and "这次聊天里" not in line


def test_an_event_eve_told_earlier_is_marked_without_a_shared_mark(violetdb, book):
    from ai.life_store import LifeStore
    LifeStore(violetdb).insert_event(uid="e-old", thread_id=None, plan_id=None, event_type="progress",
                                  content="把几首钢琴曲从头听了一遍", occurred_at=(START - timedelta(days=3)).isoformat(),
                                  duration_minutes=60, source="test")
    add_message(violetdb, "assistant", "前阵子还在想钢琴曲的事", START - timedelta(days=4))   # before it happened
    life, _ = thread_line(violetdb)
    assert "之前跟对方提过" not in next(l for l in life.splitlines() if "钢琴曲" in l)
    add_message(violetdb, "assistant", "这两天把几首钢琴曲从头听了一遍", START - timedelta(days=2))
    life, _ = thread_line(violetdb)
    assert "之前跟对方提过" in next(l for l in life.splitlines() if "钢琴曲" in l)
    row = LifeStore(violetdb).event_by_uid("e-old")
    assert row["shared_with_user_at"] is None                                         # derived, nothing written
