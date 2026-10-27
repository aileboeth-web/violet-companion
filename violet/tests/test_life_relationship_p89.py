"""Phase 8.9: Violet's ordinary day, what she already told, how close the two of you are, and what her own replies
keep doing. Offline: these prove what Violet is given and what is stored -- not that real replies became natural."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

import pytest

from ai import interaction as ia
from tests.test_phase2_scenarios import make_world, say, script, tool  # noqa: F401
from tests.test_replay_b3 import clean_env, run_eval  # noqa: F401

START = datetime.fromisoformat("2026-11-02T20:14:00+08:00")


@pytest.fixture()
def clock(monkeypatch):
    from tools.harness import ScenarioClock, install_clock, uninstall_clock
    monkeypatch.setenv("VIOLET_DAILY_LIFE", "1")
    c = ScenarioClock(START)
    saved = install_clock(c)
    yield c
    uninstall_clock(saved)


@pytest.fixture()
def engine(violetdb, clock):
    from ai.context_engine import ContextEngine
    return ContextEngine(violetdb)


def section(packet, header):
    return next((s for s in packet.sections if s.startswith(header)), "")


def daily_rows(db):
    return db.fetch_all("SELECT * FROM life_events WHERE source = 'life_daily' ORDER BY occurred_at")


def busy_day(db, tz, after=date(2026, 10, 25), min_items=2, before_hour=20):
    """A date whose plan has at least ``min_items`` items, all started before ``before_hour`` (from the real config)."""
    from ai.daily_life import DailyLife
    daily = DailyLife(db)
    for i in range(60):
        day = after + timedelta(days=i)
        plan = daily.plan_for(day, tz)
        if len(plan) >= min_items and all(p["start"].hour < before_hour for p in plan):
            return day, plan
    raise AssertionError("no suitable day in the config")


def add_message(db, role, text, at):
    db.add_message(role, "incoming" if role == "user" else "outgoing", "text", text, created_at=at.isoformat())


# ============================================================================== ordinary day (ai/daily_life.py)

def test_a_day_is_decided_by_its_date(violetdb, clock):
    from ai.daily_life import DailyLife
    daily, tz = DailyLife(violetdb), START.tzinfo
    days = [daily.plan_for(date(2026, 11, 1) + timedelta(days=i), tz) for i in range(60)]
    assert days[3] == daily.plan_for(date(2026, 11, 4), tz)                       # same date, same day
    assert any(d == [] for d in days) and any(len(d) == 3 for d in days)           # quiet days exist; at most 3
    assert all(len(d) <= 3 for d in days)
    slots = json.loads((__import__("ai.persona", fromlist=["persona_dir"]).persona_dir() / "life_config.json")
                       .read_text(encoding="utf-8"))["daily"]["slots"]
    allowed = {i["done"] for s in slots.values() for i in s["items"]}
    for d in days:
        assert {p["done"] for p in d} <= allowed
        assert [p["start"] for p in d] == sorted(p["start"] for p in d)         # morning, afternoon, evening
        for p in d:
            first, last = slots[p["slot"]]["hours"]
            assert first <= p["start"].hour < last


def test_only_what_has_started_is_recorded_and_never_twice(violetdb, clock):
    from ai.daily_life import DailyLife
    day, plan = busy_day(violetdb, START.tzinfo)
    daily = DailyLife(violetdb)
    morning = datetime.combine(day, datetime.min.time(), tzinfo=START.tzinfo).replace(hour=7)
    view = daily.day(day, morning)
    assert view["done"] == [] and view["doing"] is None and len(view["later"]) == len(plan)
    assert daily_rows(violetdb) == []                                                 # plans of today are not stored
    second = plan[1]
    during = second["start"] + timedelta(minutes=10)
    view = daily.day(day, during)
    assert [e["content"] for e in view["done"]] == [plan[0]["done"]]
    assert view["doing"]["doing"] == second["doing"]                                # in progress: "现在…"
    evening = datetime.combine(day, datetime.min.time(), tzinfo=START.tzinfo).replace(hour=23)
    daily.day(day, evening)
    daily.day(day, evening)
    rows = daily_rows(violetdb)
    assert [r["content"] for r in rows] == [p["done"] for p in plan]               # once each, in order
    assert {r["event_type"] for r in rows} == {"daily"} and all(r["is_virtual"] == 1 for r in rows)


@pytest.mark.parametrize("salt", ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"])
def test_yesterday_stays_the_same_and_is_not_repeated_today(violetdb, clock, salt):
    from ai.daily_life import DailyLife
    config = {"daily": {"enabled": True, "seed_salt": salt, "quiet_day_probability": 0, "items_per_day": [1, 1],
                        "duration_minutes": 30, "backfill_days": 1, "repeat_window_days": 3,
                        "slots": {"afternoon": {"label": "下午", "hours": [14, 15],
                                                "items": [{"done": "去书店翻书", "doing": "在书店"},
                                                          {"done": "在公园散步", "doing": "在公园"}]}}}}
    daily = DailyLife(violetdb, config)
    first = daily.recent(START)                                                    # yesterday, then today
    assert [v["done"][0]["content"] for v in first] == [r["content"] for r in daily_rows(violetdb)]
    assert first[0]["done"][0]["content"] != first[1]["done"][0]["content"]       # two options, not twice running
    again = daily.recent(START + timedelta(hours=1))
    assert [v["done"][0]["content"] for v in again] == [v["done"][0]["content"] for v in first]


def test_a_conflicting_item_is_left_out_not_forced_in(violetdb, clock):
    from ai.daily_life import DailyLife
    from ai.life_store import LifeStore
    day, plan = busy_day(violetdb, START.tzinfo)
    first = plan[0]
    LifeStore(violetdb).insert_event(uid="thread-1", thread_id=None, plan_id=None, event_type="progress",
                                  content="又往后读了几章《边城》", occurred_at=first["start"].isoformat(),
                                  duration_minutes=60, source="life_engine:template")
    evening = datetime.combine(day, datetime.min.time(), tzinfo=START.tzinfo).replace(hour=23)
    view = DailyLife(violetdb).day(day, evening)
    assert first["done"] not in [e["content"] for e in view["done"]]               # time conflict: not part of the day
    assert first["done"] not in [r["content"] for r in daily_rows(violetdb)]


def test_the_users_own_experience_never_becomes_eves_day(violetdb, clock):
    from ai.daily_life import DailyLife
    day, plan = busy_day(violetdb, START.tzinfo)
    violetdb.execute("INSERT INTO event_memories (content, kind, status, created_at, updated_at) VALUES (?, 'event', 'active', ?, ?)",
                  (plan[0]["done"], violetdb.now(), violetdb.now()))
    evening = datetime.combine(day, datetime.min.time(), tzinfo=START.tzinfo).replace(hour=23)
    DailyLife(violetdb).day(day, evening)
    assert plan[0]["done"] not in [r["content"] for r in daily_rows(violetdb)]


def test_switched_off_means_nothing_is_written(violetdb, clock, monkeypatch):
    from ai.daily_life import DailyLife
    monkeypatch.setenv("VIOLET_DAILY_LIFE", "0")
    assert DailyLife(violetdb).recent(START) == [] and daily_rows(violetdb) == []


def test_life_engine_moves_off_the_days_small_things(violetdb, clock):
    from ai.life_engine import LifeEngine
    from ai.life_store import LifeStore
    engine = LifeEngine(violetdb, proposer=object())
    at = START.replace(hour=10, minute=0)
    assert engine._clear_of_daily(at, START) == at                                 # nothing there: unchanged
    LifeStore(violetdb).insert_event(uid="daily:x", thread_id=None, plan_id=None, event_type="daily", content="浇水",
                                  occurred_at=START.replace(hour=9, minute=45).isoformat(), duration_minutes=45,
                                  source="life_daily")
    assert engine._clear_of_daily(at, START) == START.replace(hour=10, minute=30)


def test_the_days_small_things_are_not_proactive_shares(make_world, monkeypatch):
    from ai.life_store import LifeStore
    w = make_world()
    store = LifeStore(w.db)
    past = (datetime.fromisoformat(w.db.now()) - timedelta(hours=5)).isoformat()
    store.insert_event(uid="daily:y", thread_id=None, plan_id=None, event_type="daily", content="给绿萝浇了水",
                       occurred_at=past, duration_minutes=45, source="life_daily")
    store.insert_event(uid="t:z", thread_id=None, plan_id=None, event_type="progress", content="又往后读了几章《边城》",
                       occurred_at=(datetime.fromisoformat(past) - timedelta(hours=2)).isoformat(), duration_minutes=60,
                       source="life_engine:template")
    assert [e["content"] for e in w.scheduler._shareable_life_events()] == ["又往后读了几章《边城》"]


# ============================================================================== Violet's life in the chat context

def test_what_did_you_do_today_has_a_recorded_answer(violetdb, clock):
    from ai.consistency import ConsistencyGuard
    from ai.context_engine import ContextEngine
    day, _ = busy_day(violetdb, START.tzinfo)
    clock.set(datetime.combine(day, datetime.min.time(), tzinfo=START.tzinfo).replace(hour=21))
    life = section(ContextEngine(violetdb).build("chat", "你今天干嘛了"), "【Violet 的生活状态")
    today = [r for r in daily_rows(violetdb) if r["occurred_at"][:10] == day.isoformat()]
    assert today                                                                   # the earlier days were filled first
    for row in today:
        assert f"{row['content']}（已发生）" in life and f"今天" in life
    assert "最近没有记录的生活事件" not in life
    guard = ConsistencyGuard(violetdb)
    assert guard.check_reply(f"我今天{today[0]['content']}") == []                 # recorded: backs the claim
    assert [v.check for v in guard.check_reply("我今天去了一趟海边")] == ["unrecorded_experience"]


def test_a_quiet_day_is_said_to_be_quiet(violetdb, clock):
    from ai.context_engine import ContextEngine
    from ai.daily_life import DailyLife
    daily = DailyLife(violetdb)
    day = next(date(2026, 11, 1) + timedelta(days=i) for i in range(60)
               if daily.plan_for(date(2026, 11, 1) + timedelta(days=i), START.tzinfo) == [])
    clock.set(datetime.combine(day, datetime.min.time(), tzinfo=START.tzinfo).replace(hour=21))
    life = section(ContextEngine(violetdb).build("chat", "你今天干嘛了"), "【Violet 的生活状态")
    assert "普普通通的一天" in life


@pytest.fixture()
def reading(violetdb, clock):
    from ai.life_store import LifeStore
    store = LifeStore(violetdb)
    thread = store.create_thread("t:潜水艇", "reading", "读《夜晚的潜水艇》", "夜晚的潜水艇", (START - timedelta(days=6)).isoformat())
    store.insert_event(uid="e1", thread_id=thread, plan_id=None, event_type="progress", content="又往后读了几章《夜晚的潜水艇》",
                       occurred_at=(START - timedelta(days=1)).isoformat(), duration_minutes=60, source="test")
    store.insert_event(uid="e2", thread_id=thread, plan_id=None, event_type="progress", content="把几首钢琴曲从头听了一遍",
                       occurred_at=(START - timedelta(days=2)).isoformat(), duration_minutes=60, source="test")
    return store


def test_details_and_how_she_feels_about_it_now(engine, reading):
    life = section(engine.build("chat", "你最近在干嘛"), "【Violet 的生活状态")
    assert "把几首钢琴曲从头听了一遍（德彪西的《月光》、萨蒂的《裸体歌舞》）（已发生）" in life
    assert "正在进行：读《夜晚的潜水艇》（现在的感觉：快读完了，有点舍不得）" in life       # two events in the thread


def test_what_she_already_told_is_marked(engine, violetdb, reading):
    add_message(violetdb, "user", "好无聊", START - timedelta(minutes=10))
    add_message(violetdb, "assistant", "我最近在读《夜晚的潜水艇》，有几段像在做梦", START - timedelta(minutes=9))
    old = reading.event_by_uid("e2")
    reading.mark_shared(old["id"])                                                  # told on an earlier day
    life = section(engine.build("chat", "随便聊点什么吧"), "【Violet 的生活状态")
    line = next(l for l in life.splitlines() if "又往后读了几章《夜晚的潜水艇》" in l)
    assert "这次聊天里已经说过了" in line
    piano = next(l for l in life.splitlines() if "钢琴曲" in l)
    assert "之前跟对方提过" in piano and "这次聊天里" not in piano


def test_an_earlier_conversation_is_not_this_one(engine, violetdb, reading):
    add_message(violetdb, "assistant", "我最近在读《夜晚的潜水艇》", START - timedelta(hours=5))
    life = section(engine.build("chat", "好无聊"), "【Violet 的生活状态")
    assert "这次聊天里已经说过了" not in life


@pytest.mark.parametrize("violet_before,user,shown", [
    ("最近还挺喜欢听钢琴曲的", "钢琴曲？好文艺啊", True),           # her item named by the user
    ("最近还挺喜欢听钢琴曲的", "好文艺啊", True),                   # picks up what she just told
    ("嗯嗯，长电影确实累", "好文艺啊", False),                       # nothing of hers on the table
    ("最近还挺喜欢听钢琴曲的", "今天被导师当众批评了 好委屈", "focus"),
])
def test_life_follows_the_topic_she_opened(engine, violetdb, reading, violet_before, user, shown):
    add_message(violetdb, "user", "那你最近还喜欢什么", START - timedelta(minutes=3))
    add_message(violetdb, "assistant", violet_before, START - timedelta(minutes=2))
    life = section(engine.build("chat", user), "【Violet 的生活状态")
    if shown == "focus":
        assert "不要主动谈你自己的生活" in life
    else:
        assert ("钢琴曲" in life) is shown


def test_legacy_context_has_no_ordinary_day(engine, violetdb, reading, monkeypatch):
    monkeypatch.setenv("VIOLET_CONTEXT_STYLE", "legacy")
    life = section(engine.build("chat", "你今天干嘛了"), "【Violet 的生活状态")
    assert "今天上午" not in life and "今天下午" not in life and "现在的感觉" not in life
    assert daily_rows(violetdb) == []


def test_named_piece_is_backed_by_the_recorded_listening(violetdb, reading):
    from ai.consistency import ConsistencyGuard
    guard = ConsistencyGuard(violetdb)
    assert guard.check_reply("我最近听过德彪西的《月光》，很轻") == []             # E5 step 8 of the 8.8 real run
    violetdb.execute("DELETE FROM life_events WHERE uid = 'e2'")
    assert [v.check for v in guard.check_reply("我最近听过德彪西的《月光》，很轻")] == ["unrecorded_experience"]


# ============================================================================== relationship (ai/relationship.py)

def history(db, days, user_text="今天好累", opener="user", gap_hours=20, start=START - timedelta(days=30)):
    at = start
    for i in range(days):
        if opener == "violet":
            add_message(db, "assistant", "在干嘛呀", at)
            add_message(db, "user", user_text, at + timedelta(minutes=5))
        else:
            add_message(db, "user", user_text, at)
            add_message(db, "assistant", "嗯嗯", at + timedelta(minutes=1))
        at += timedelta(hours=gap_hours + 24 * 3)
    return at


def test_stages_from_how_you_talked_not_from_days_alone(violetdb, clock):
    from ai.relationship import assess
    assert assess(violetdb, START).stage == "new"
    history(violetdb, 2)
    assert assess(violetdb, START).stage == "new"
    violetdb.execute("DELETE FROM messages")
    history(violetdb, 3)
    assert assess(violetdb, START).stage == "warming"
    violetdb.execute("DELETE FROM messages")
    history(violetdb, 8, user_text="今天好累 想找你聊聊")                         # reaches out, says something warm
    view = assess(violetdb, START)
    assert view.stage == "close" and view.initiations == 8
    violetdb.execute("DELETE FROM messages")
    history(violetdb, 8, user_text="今天好累 想找你聊聊", opener="violet")          # same days, but only ever answers Violet
    view = assess(violetdb, START)
    assert view.stage == "warming" and view.initiations == 0
    violetdb.execute("DELETE FROM messages")
    history(violetdb, 8)                                                          # reaches out, never warm or playful
    assert assess(violetdb, START).stage == "warming"


def test_distance_wins_until_the_user_lifts_it(violetdb, clock):
    from ai.relationship import assess, describe
    last = history(violetdb, 8, user_text="今天好累 想找你聊聊")
    assert assess(violetdb, START).stage == "close"
    add_message(violetdb, "user", "别撒娇了，正常说话", START - timedelta(hours=1))
    view = assess(violetdb, START)
    assert view.stage == "distance" and "不撒娇" in describe(view) and "别撒娇了" in describe(view)
    add_message(violetdb, "user", "好啦 可以撒娇", START - timedelta(minutes=30))
    assert assess(violetdb, START).stage == "close"
    assert last


def test_a_long_silence_cools_down(violetdb, clock):
    from ai.relationship import assess, describe
    history(violetdb, 8, user_text="今天好累 想找你聊聊", start=START - timedelta(days=70))
    view = assess(violetdb, START)
    assert view.stage == "warming" and view.cooled and "不埋怨" in describe(view)


def test_closeness_is_described_never_demanded(violetdb, clock):
    from ai.relationship import RelationshipView, describe
    close = describe(RelationshipView("close", chat_days=9, initiations=6, warm=2, evidence=["聊过 9 天"]))
    assert "不是每句都要有" in close and "不让对方愧疚" in close and "不要求对方只陪你" in close
    for stage in ("new", "warming", "close", "distance"):
        text = describe(RelationshipView(stage, distance_quote="别黏", distance_at="2026-11-01T10:00:00+08:00"))
        assert "概率" not in text and "分" not in text.replace("分享", "")


def test_relationship_line_in_the_natural_context_only(engine, violetdb, monkeypatch):
    history(violetdb, 8, user_text="今天好累 想找你聊聊")
    rel = section(engine.build("chat", "在吗"), "【你们的相处")
    assert "已经很亲近了" in rel and engine.build("chat", "在吗").report["relationship"] == "close"
    monkeypatch.setenv("VIOLET_RELATIONSHIP_STATE", "0")
    assert "已经很亲近了" not in section(engine.build("chat", "在吗"), "【你们的相处")
    monkeypatch.setenv("VIOLET_CONTEXT_STYLE", "legacy")
    assert section(engine.build("chat", "在吗"), "【你们的相处") == ""


# ============================================================================== turn signals and reply habits

@pytest.mark.parametrize("text,kind", [
    ("我去洗澡了", "closing"), ("那我去忙啦", "closing"), ("不说了 练琴去", "closing"), ("晚安", "closing"),
    ("洗完了 好困", "closing"), ("我要睡了", "closing"), ("好了 我去收拾行李", "closing"),
    ("你睡了吗", "question"), ("明天要早起", "share"), ("嗯", "ack"), ("嗯嗯", "ack"), ("好吧", "ack"),
    ("哈哈", "share"), ("在吗", "open"),
])
def test_closing_and_bare_acknowledgements(text, kind):
    assert ia.turn_kind(text) == kind


def test_notes_for_leaving_and_for_a_bare_ack():
    assert "不用再叮嘱" in ia.turn_note("那我去忙啦", None) and "不提问" in ia.turn_note("那我去忙啦", "no")
    assert "只回了“嗯”" in ia.turn_note("嗯", None)
    invite = ia.turn_note("你有什么想问我的吗", None)
    assert "问一句" in invite and "真正好奇" not in invite                    # 8.8's wording came back verbatim


def test_the_answer_to_an_invited_question_may_get_one_follow_up():
    assert ia.follow_up_violation("才两周呀？手指疼吗？", "嗯 就是最近在学吉他", "no")
    assert not ia.follow_up_violation("才两周呀？手指疼吗？", "嗯 就是最近在学吉他", "no", "你有什么想问我的吗")
    assert ia.turn_note("嗯 就是最近在学吉他", "no", "你有什么想问我的吗") is None
    assert "不喜欢被追问" in ia.turn_note("嗯 就是最近在学吉他", "no", "今天去看了场电影")
    assert ia.follow_up_violation("你呢？", "那我去忙啦", "no")                    # leaving: still no question


def test_reply_habits_are_counted_from_her_own_replies():
    replies = ["嗯，长电影看完确实有点累，今晚早点歇着吧🙂", "被你笑到了", "好，去吧，洗完暖和一点🙂"]
    note = ia.habit_note(replies, "洗完了 好困")
    assert "叮嘱、关心过对方 2 次" in note and "这一句不用再叮嘱" in note
    assert ia.habit_note(replies, "今天被导师当众批评了 好委屈") is None           # a hard moment: care is right
    assert ia.habit_note(["被你笑到了", "哈哈是吧"], "嗯") is None
    asks = ["看的什么电影呀？", "那你现在在家吗？", "好呀"]
    assert "3 条里有 2 条是用问句结尾" in ia.habit_note(asks, "今天去看了场电影")
    assert ia.habit_note(asks, "你觉得呢") is None                                 # the user asked: answer as usual


def test_habit_note_sits_next_to_the_message_and_only_for_this_conversation(make_world, monkeypatch):
    w = make_world()
    replies = iter(["那早点歇着吧", "好呀", "去吧，记得多穿点", "晚安"])
    w.llm.responders["fake-chat"] = lambda kw: next(replies)
    say(w, "看完电影了")
    say(w, "嗯")
    say(w, "我出门买个东西")
    say(w, "回来了")
    last = w.llm.calls_for("fake-chat")[-1]["messages"]
    assert last[-1] == {"role": "user", "content": "回来了"} and "叮嘱、关心过对方 2 次" in last[-2]["content"]
    monkeypatch.setenv("VIOLET_REPLY_HABIT_NOTE", "0")
    w.llm.responders["fake-chat"] = lambda kw: "好"
    say(w, "还行")
    assert not any("叮嘱、关心过" in (m.get("content") or "") for m in w.llm.calls_for("fake-chat")[-1]["messages"])


def test_invited_answer_is_not_rewritten_through_the_runtime(make_world):
    w = make_world()
    w.db.execute("INSERT INTO interaction_preferences (pref_key, value, origin, evidence, confidence, observations, "
                 "status, created_at, updated_at) VALUES ('follow_up_questions', 'no', 'explicit', 'x', 1, 0, 'active', ?, ?)",
                 (w.db.now(), w.db.now()))
    w.llm.responders["fake-chat"] = lambda kw: "最近在学什么呀？"
    say(w, "你有什么想问我的吗")
    w.llm.responders["fake-chat"] = lambda kw: "才两周呀？手指疼吗？"
    calls = len(w.llm.calls_for("fake-chat"))
    say(w, "嗯 就是最近在学吉他")
    assert len(w.llm.calls_for("fake-chat")) == calls + 1 and w.client.sent_texts[-1] == "才两周呀？手指疼吗？"


def test_markdown_emphasis_is_not_sent_as_stars():
    from ai.chat_output import normalize_chat_output
    out = normalize_chat_output("好呀，去忙吧～**注意休息，别太累啦**\n5*3=15")
    assert [m["content"] for m in out["messages"]] == ["好呀，去忙吧～注意休息，别太累啦", "5*3=15"]


def test_a_fresh_32_round_run_keeps_the_ordinary_day_off(run_eval, clean_env):
    # its reports are compared with runs recorded before 8.9; a replay turns it off too (tools/replay.py)
    rc, _ = run_eval("--rounds", "1")
    assert rc == 0 and clean_env.get("VIOLET_DAILY_LIFE") == "0"


# ============================================================================== 8.7 episodes, offline (mock)

@pytest.fixture(scope="module")
def episodes_run():
    from tools import chat_dialogue as cd
    out = cd.ROOT / "local/p89/test-cases" / uuid.uuid4().hex
    summary = cd.run(out, label="p89", episode_ids=["E1", "E4", "E5", "E7", "E8"])
    return summary, json.loads((out / "debug.json").read_text(encoding="utf-8"))["episodes"]


def request_of(step, agent="ChatAgent"):
    return next(r["request"] for r in step["requests"] if r["agent"] == agent)


def system(step):
    return "\n".join(m["content"] for m in request_of(step) if m["role"] == "system")


def test_episodes_complete_with_the_new_mechanisms(episodes_run):
    summary, episodes = episodes_run
    assert summary["state"] == "complete" and summary["steps_executed"] == summary["steps_planned"]
    assert all("follow_up_check" in s for e in episodes.values() for s in e["steps"])   # recorded per step now


def test_e1_today_has_recorded_things_and_leaving_gets_its_note(episodes_run):
    steps = episodes_run[1]["E1"]["steps"]
    life = system(steps[3])                                                        # 你今天干嘛了
    assert "（已发生）" in life and "最近没有记录的生活事件" not in life
    assert "只回了“嗯”" in request_of(steps[2])[-2]["content"]                      # 嗯
    assert "要去忙或去休息了" in request_of(steps[5])[-2]["content"]                # 我去洗澡了
    assert "还不太熟" in system(steps[0])


def test_e4_three_weeks_of_reaching_out_reads_as_close(episodes_run):
    # phase 8.9.1: "都没顾上找你" is no warmth, so the history alone reads as warming; the teasing in the episode
    # ("你是不是又…", "哈哈哈 被我说中了吧", both in the log by step 4 "想我没") makes it close from then on
    steps = episodes_run[1]["E4"]["steps"]
    assert all("慢慢熟起来了" in system(s) and "已经很亲近了" not in system(s) for s in steps[:3])
    assert all("已经很亲近了" in system(s) for s in steps[3:])


def test_e5_piano_follow_up_keeps_her_life_in_view(episodes_run):
    steps = episodes_run[1]["E5"]["steps"]
    assert "德彪西的《月光》" in system(steps[6]) and "德彪西的《月光》" in system(steps[7])    # 钢琴曲？/ 推荐一首


def test_e7_coming_back_after_days(episodes_run):
    steps = episodes_run[1]["E7"]["steps"]
    rel = system(steps[0])
    assert "已经很亲近了" not in rel and "【你们的相处" in rel
    assert "一张老的爵士专辑" in system(steps[3])                                 # 你这几天干嘛了
    assert "越听越顺耳" in system(steps[3]) and "读进去了" not in system(steps[3])  # a music thread, not reading


def test_e8_answer_after_the_invitation_and_no_onboarding(episodes_run):
    steps = episodes_run[1]["E8"]["steps"]
    assert "请你问问题" in request_of(steps[6])[-2]["content"]
    after = request_of(steps[7])                                                   # 嗯 就是最近在学吉他
    assert not any("不喜欢被追问" in m["content"] for m in after[-3:]) and "【可选的了解】" not in system(steps[7])
