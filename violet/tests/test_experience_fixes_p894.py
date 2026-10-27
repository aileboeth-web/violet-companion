"""Phase 8.9.4: defects the 8.9.2 / 8.9.3 real runs showed, each at its confirmed root cause.

1. a recorded "在公园散了会儿步" backed "我刚刚也随手听了会儿歌" through filler bigrams (了会 / 会儿) only;
2. earlier weather retold as now ("今天也冷", "雨天确实容易无聊");
3. care counted per 3-hour conversation, so on a busy day 喝水 / 别硬撑 / 别累着 came back every few hours;
4. coming back was met with a routine question;
5. stray model fragments ("_久久爱", "ablytyped", ",伊人") are flagged in the diagnostics (never removed).
Offline: what is checked, told and flagged -- not how a real model now replies."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ai import interaction as ia
from tests.test_consistency_p891 import seed_episode  # noqa: F401
from tests.test_life_relationship_p89 import START, add_message, clock  # noqa: F401


def record(db, content, at):
    from ai.life_store import LifeStore
    LifeStore(db).insert_event(uid=f"t-{content}", thread_id=None, plan_id=None, event_type="daily", content=content,
                               occurred_at=at.isoformat(), duration_minutes=45, source="life_daily")


def checks(db, text, weather=False):
    from ai.consistency import ConsistencyGuard
    guard = ConsistencyGuard(db)
    guard.check_weather = weather
    return [v.check for v in guard.check_reply(text)]


# ============================================================================== 1. backing needs content

def test_filler_words_do_not_back_a_claim(violetdb, clock):
    record(violetdb, "在公园散了会儿步", START - timedelta(hours=2))
    assert checks(violetdb, "我刚刚也随手听了会儿歌，感觉晚上特别适合放空一下") == ["unrecorded_experience"]   # 8.9.2 E1


@pytest.mark.parametrize("line", ["我晚上去公园散了会儿步", "我下午在咖啡馆坐了一会儿", "我今天给绿萝浇了水"])
def test_real_overlap_still_backs(violetdb, clock, line):
    for content in ("在公园散了会儿步", "在咖啡馆坐了一会儿，点了杯拿铁", "给窗台上的绿萝浇了水"):
        record(violetdb, content, START - timedelta(hours=3))
    assert checks(violetdb, line) == []


def test_the_backing_helper_ignores_fillers():
    from ai.consistency import backed_by
    assert not backed_by("我刚刚也随手听了会儿歌", "在公园散了会儿步")
    assert backed_by("我去公园散步了", "在公园散了会儿步")


# ============================================================================== 2. earlier weather is not now

def test_e1_yesterdays_cold_is_not_todays(violetdb, clock):
    episode = seed_episode(violetdb, clock, "E1")                  # history: 11-01 "今天好冷"
    clock.set(datetime.fromisoformat(episode["start"]) + timedelta(hours=11))
    add_message(violetdb, "user", "早", clock.now())
    assert checks(violetdb, "早呀～今天也冷，记得多穿一点哦", weather=True) == ["stale_weather"]      # 8.9.3 E1 step 10
    assert checks(violetdb, "早呀～今天也冷，记得多穿一点哦") == []                               # off unless asked


@pytest.mark.parametrize("line,stale", [("雨天确实容易无聊……我这会儿也没在做什么特别的事", True),       # 8.9.2 E5 step 1
                                        ("安安静静的，很适合这种下雨天", True),                       # 8.9.2 E5 step 6
                                        ("如果今天的雨有味道，你觉得会像什么", True),                  # 8.9.1 E5 step 2
                                        ("挺适合下雨天看的", False),                                   # generic, not now
                                        ("这本书安安静静的", False)])
def test_e5_two_day_old_rain(violetdb, clock, line, stale):
    seed_episode(violetdb, clock, "E5")                            # history: 11-04 "今天下雨了"
    add_message(violetdb, "user", "好无聊", clock.now())
    assert ("stale_weather" in checks(violetdb, line, weather=True)) is stale


def test_weather_the_user_brings_up_now_is_fine(violetdb, clock):
    seed_episode(violetdb, clock, "E5")
    add_message(violetdb, "user", "今天又下雨了 好烦", clock.now())
    assert checks(violetdb, "雨天确实容易烦，窝着就好", weather=True) == []


def test_no_weather_in_the_history_leaves_small_talk_alone(violetdb, clock):
    add_message(violetdb, "user", "随便聊聊", clock.now())
    assert checks(violetdb, "今天降温了，记得多穿点", weather=True) == []          # phase 5A: harmless chat


def test_the_rewrite_note_and_the_entities(violetdb, clock, monkeypatch):
    import ai.chat as chat
    seed_episode(violetdb, clock, "E5")
    add_message(violetdb, "user", "好无聊", clock.now())
    notes = []
    text, violations = chat.guard_reply("雨天确实容易无聊\n要不要听我讲讲最近看的书",
                                        regenerate=lambda note: notes.append(note) or "要不要听我讲讲最近看的书")
    assert [v.check for v in violations] == ["stale_weather"] and text == "要不要听我讲讲最近看的书"
    assert "以前聊到的天气是那时候的" in notes[0]
    text, violations = chat.guard_reply("雨天确实容易无聊", entity="proactive_reply")
    assert violations == [] and text == "雨天确实容易无聊"                     # a weather task has real data


# ============================================================================== 3. care over the day

def test_care_said_this_morning_counts_this_afternoon(violetdb, clock):
    from ai.context_engine import ContextEngine
    morning = START.replace(hour=9, minute=30)
    add_message(violetdb, "user", "早 今天一整天都要在实验室", morning)
    add_message(violetdb, "assistant", "中间记得吃点东西、喝水～", morning + timedelta(minutes=1))
    add_message(violetdb, "assistant", "昨天的叮嘱：早点睡", morning - timedelta(days=1))     # yesterday does not count
    clock.set(morning + timedelta(hours=5))                                              # a new "conversation"
    note = ContextEngine(violetdb).build("chat", "终于吃上午饭了").turn_note
    assert "今天你已经叮嘱、关心过对方 1 次了" in note


@pytest.mark.parametrize("line", ["慢慢吃一点，别一忙又忘了喝水～", "先喝点水", "记得吃点东西", "别饿着"])
def test_more_ways_of_caring_are_seen(line):
    assert ia.reply_habits([line])["care"] == 1


# ============================================================================== 4. coming back

@pytest.mark.parametrize("text,stage,expected", [
    ("我回来啦", "close", True), ("忙完了", "warming", True), ("出差回来了", "close", True),
    ("我回来啦", "new", False), ("我回来啦", None, False),         # strangers: nothing new
    ("回来啦？", "close", False), ("我回来啦 今天好累", "close", False),
    ("在吗 好久没聊了", "warming", True), ("好久不见", "close", True),       # phase 8.9.5: back after a long time
    ("在吗 好久没聊了", "new", False),
])
def test_coming_back_is_a_moment_to_be_glad(text, stage, expected):
    note = ia.turn_note(text, None, stage=stage) or ""
    assert ("对方回来找你了" in note) is expected                         # 8.9.5 wording (was "忙完回来")
    assert "不用机械地问" in note or not expected


def test_the_no_questions_preference_wins_on_return():
    assert "不喜欢被追问" in ia.turn_note("我回来啦", "no", stage="close")


# ============================================================================== 5. stray fragments are flagged

@pytest.mark.parametrize("line,flagged", [("《边城》还摊在旁边呢，我又想偷懒了_久久爱", True),
                                          ("想你了，比一点点多很多，满意没ablytyped", True),
                                          ("现在在听那张老爵士，越听越顺耳了,伊人", True),
                                          ("今天好开心", False), ("好的ok", False), ("嗯嗯，等你回来", False)])
def test_suspicious_fragments_are_listed_for_reading(line, flagged):
    from tools import chat_dialogue as cd
    episodes = {"X": {"id": "X", "seed": {}}}
    results = {"X": {"steps": [{"n": 1, "kind": "user", "user": "在吗", "violet": [line]}]}}
    found = cd.diagnose(episodes, results)["episodes"]["X"]["suspicious_fragments"]
    assert (found == [{"step": 1, "line": line}]) is flagged
