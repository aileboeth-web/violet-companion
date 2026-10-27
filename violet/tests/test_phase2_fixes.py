"""Phase 3 review of phase-2 boundaries (each change has its own test)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.test_phase2_scenarios import arm, chat, decide, make_world, say  # noqa: F401
from tests.test_phase3_scenarios import age_all, quiet_memory


# --- 1. events: a narrow, safe allowance instead of a total ban --------------------------------

def _planned_event(w, hours_ahead, content="用户明天上午有一场篮球比赛"):
    event_time = (datetime.fromisoformat(w.db.now()) + timedelta(hours=hours_ahead)).isoformat()
    return w.db.save_event_memory(content, kind="event", progress="planned", event_time=event_time, new_event=True)["memory"]


def test_upcoming_event_the_user_raised_may_be_mentioned_once_before_it_happens(make_world):
    w = make_world()
    say(w, "早")
    event = _planned_event(w, 20)
    age_all(w.db, 3)                       # user raised it 3h ago; game is in 17h
    state = w.scheduler._conversation_state()
    allowed = {m["id"]: m for m in state["allowed_memories"]}
    assert allowed[event["id"]]["type"] == "upcoming_event" and "不要追问结果" in allowed[event["id"]]["note"]
    assert event["id"] not in [m["memory_id"] for m in state["restricted_memories"]]

    decide(w, topic_key="game_wish", mentioned_memory_ids=[event["id"]], chat_brief={"topic": "祝比赛顺利"})
    chat(w, proactive="明天篮球比赛加油呀")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts[-1] == "明天篮球比赛加油呀"
    # only once: a second raise is no longer allowed
    assert event["id"] not in [m["id"] for m in w.scheduler._conversation_state()["allowed_memories"]]


def test_event_is_not_allowed_after_it_happened_or_when_stale(make_world):
    w = make_world()
    say(w, "早")
    past = _planned_event(w, 1, "用户今天下午有一场面试")
    far = _planned_event(w, 72, "用户周末要去露营")
    age_all(w.db, 3)                       # the interview time has passed
    w.db.refresh_event_states()
    state = w.scheduler._conversation_state()
    ids = [m["id"] for m in state["allowed_memories"]]
    assert past["id"] not in ids and far["id"] not in ids
    assert w.db.get_event_memory(past["id"])["progress"] == "unknown"   # still not "done"
    old = _planned_event(w, 30, "用户下周一要交论文")
    age_all(w.db, 24 * 3)                                                    # user mentioned it 3 days ago
    w.db.execute("UPDATE event_memories SET event_time = ? WHERE id = ?",
                 ((datetime.fromisoformat(w.db.now()) + timedelta(hours=5)).isoformat(), old["id"]))
    assert old["id"] not in [m["id"] for m in w.scheduler._conversation_state()["allowed_memories"]]


def test_event_allowance_can_be_switched_off(make_world, tmp_path):
    w = make_world()
    event = _planned_event(w, 10)
    config = json.loads(w.scheduler.config_path.read_text(encoding="utf-8"))
    config["event_topics"] = {"enabled": False}
    w.scheduler.config_path.write_text(json.dumps(config), encoding="utf-8")
    w.scheduler.load_config()
    assert event["id"] not in [m["id"] for m in w.scheduler._conversation_state()["allowed_memories"]]


# --- 2. short replies: friendly-but-short is not a brush-off ---------------------------------

@pytest.mark.parametrize("text,category", [
    ("嗯", "disengaged"), ("哦", "disengaged"), ("好的", "disengaged"), ("", "disengaged"),
    ("哈哈哈", "neutral"), ("好呀", "neutral"), ("真的假的", "neutral"), ("为啥？", "neutral"), ("还行吧", "neutral"),
    ("刚下课，累死了", "engaged"),
])
def test_three_way_reply_classification(text, category):
    from ai.memory_model import classify_reply
    assert classify_reply(text) == category


def test_warm_short_replies_do_not_cool_a_topic(violetdb):
    from ai.conversation_state import TopicTracker
    tracker = TopicTracker(violetdb)
    for reply in ("哈哈哈", "好呀", "真的假的"):
        tracker.record_eve_topic("music")
        tracker.evaluate(reply)
    assert not tracker.topic_state("music")["cooling"]
    assert [m["user_engaged"] for m in violetdb.list_topic_mentions(topic_key="music")] == [2, 2, 2]
    assert tracker.cold_streak() == 0


def test_bare_acknowledgements_still_cool_a_topic(violetdb):
    from ai.conversation_state import TopicTracker
    tracker = TopicTracker(violetdb)
    for reply in ("嗯", "哦"):
        tracker.record_eve_topic("music")
        tracker.evaluate(reply)
    assert tracker.topic_state("music")["cooling"]


def test_user_starting_a_conversation_resets_the_cold_streak(violetdb):
    from ai.conversation_state import TopicTracker
    tracker = TopicTracker(violetdb)
    for _ in range(2):
        violetdb.add_proactive_event("scheduled_proactive_check", status="sent", action_type="chat")
        tracker.record_eve_topic("chat")
        tracker.evaluate("嗯")
    assert tracker.cold_streak() == 2
    result = tracker.evaluate("在吗")                       # nothing pending, no Violet message recently
    assert result["user_initiated"] and tracker.cold_streak() == 0


# --- 3. configurable IANA time zone (see also tests/test_profile.py) ----------------------------

def test_scheduler_clock_follows_the_configured_zone(monkeypatch):
    from ai.proactive_scheduler import now_china, parse_iso
    monkeypatch.setenv("VIOLET_TIMEZONE", "Europe/London")
    assert now_china().tzinfo.key == "Europe/London"
    assert parse_iso("2026-07-01T12:00:00").utcoffset() == timedelta(hours=1)    # BST


# --- 4. topic fatigue knows who raised the topic --------------------------------------------------

def test_user_raising_a_topic_is_recorded_immediately(make_world):
    from tests.test_phase2_scenarios import script, tool
    from tests.fakes import intent_json
    w = make_world()
    guitar = w.db.save_event_memory("用户在学吉他", kind="fact")["memory"]
    for _ in range(2):
        w.scheduler.tracker.record_eve_topic("guitar", [guitar["id"]])
        w.scheduler.tracker.evaluate("嗯")
    assert w.scheduler.tracker.topic_state(f"memory:{guitar['id']}")["cooling"]
    w.llm.responders["fake-intent"] = script(tool("query_memory", query="吉他"), final=intent_json("chat"))
    w.llm.responders["fake-memory"] = lambda kw: (_ for _ in ()).throw(RuntimeError("memory worker is slow today"))
    w.rt.ingest_text("今天终于学会了一首吉他曲")
    assert w.rt.wait_idle()
    # recorded in the same turn, without waiting for the (failing) memory job
    sources = [m["source"] for m in w.db.list_topic_mentions(memory_id=guitar["id"])]
    assert sources[0] == "user"
    assert not w.scheduler.tracker.topic_state(f"memory:{guitar['id']}")["cooling"]


def test_eve_follow_up_question_in_a_reply_counts_as_eve_raising_it(make_world):
    from tests.test_phase2_scenarios import script, tool
    from tests.fakes import intent_json
    w = make_world()
    quiet_memory(w)
    outage = w.db.save_event_memory("用户的服务器出现故障", kind="event", progress="ongoing")["memory"]
    w.llm.responders["fake-intent"] = script(tool("query_memory", query="服务器"), final=intent_json("chat"))
    chat(w, normal="对了服务器修好了吗？")
    say(w, "今天加班好累")                                   # user did not raise the server
    rows = w.db.list_topic_mentions(memory_id=outage["id"])
    assert [(r["source"], r["user_engaged"]) for r in rows] == [("violet_reply", None)]
    chat(w, normal="嗯嗯")
    say(w, "嗯")
    assert w.db.list_topic_mentions(memory_id=outage["id"])[0]["user_engaged"] == 0

    # a statement without a question is not a follow-up; a user-raised topic is not Violet's
    w.llm.responders["fake-intent"] = script(tool("query_memory", query="服务器"), final=intent_json("chat"))
    chat(w, normal="服务器那事确实烦人？")
    say(w, "服务器的故障又出现了")
    latest = w.db.list_topic_mentions(memory_id=outage["id"])[0]
    assert latest["source"] == "user"
