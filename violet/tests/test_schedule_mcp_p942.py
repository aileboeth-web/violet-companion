"""Phase 9.4.2: the built-in read-only timetable MCP server violet-schedule (offline).

A fictional term in the isolated test database (ScheduleStore), Violet's clock pinned to a scenario moment that keeps
ticking, the fake SDK for Intent / Chat / Memory. The four tools run for real -- whole turns go through the runtime,
Intent's catalog, the Tool Manager plan, ChatAgent and the in-process MCP bridge -- and the fake Chat writes its
reply FROM the tool result, so what reaches the user is what the store computed."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.fakes import completion, intent_json
from tests.test_phase2_scenarios import make_world, say  # noqa: F401  (make_world: fixture)
from tests.test_tools_p92 import fresh_registry  # noqa: F401  (fixture)
from tests.test_web_p93 import chat_plays, offered, systems, tool_texts

SH = timezone(timedelta(hours=8))
DAY, RANGE, STATUS, FIND = (f"violet-schedule__{t}" for t in ("get_day_schedule", "get_schedule_range",
                                                            "get_current_status", "find_course"))
TEACHER, ROOM, ROOM_LAB, SEAT = "测试老师甲", "虚构楼101", "虚构实验楼3", "虚构座位17"
PERIODS = [(1, "08:00", "08:45"), (2, "08:55", "09:40"), (3, "10:00", "10:45"), (4, "10:55", "11:40"),
           (5, "14:00", "14:45"), (6, "14:55", "15:40")]


@pytest.fixture()
def clock(monkeypatch):
    """Violet's clock (ai.settings.now_local) at a scenario moment that keeps ticking; set(moment) moves it."""
    from ai import settings
    state = {"base": datetime(2026, 9, 8, 9, 30, tzinfo=SH), "t0": time.monotonic()}

    def now():
        return state["base"] + timedelta(seconds=time.monotonic() - state["t0"])

    def set_to(moment):
        state.update(base=moment, t0=time.monotonic())
    monkeypatch.setattr(settings, "now_local", now)
    monkeypatch.setenv("VIOLET_TIMEZONE", "Asia/Shanghai")
    now.set = set_to
    return now


def fill(store):
    """2026 秋（虚构）: week 1 = Mon 2026-09-07. 高数 Mon p1-2 weeks 1-16 + Wed p3-4 odd weeks (room 101, teacher 甲);
    物理 Tue 14:00-15:40 weeks 1-16; 实验 Thu p5-6 even weeks; a 高数 quiz Fri 2026-09-11 10:00-11:00 (seat)."""
    t = store.create_term(name="2026 秋（虚构）", start_date="2026-09-07", end_date="2027-01-15", timezone="Asia/Shanghai")
    store.set_periods(t, PERIODS)
    math = store.add_course(t, name="高等数学（虚构）", teacher=TEACHER, notes="私人备注（虚构）")
    phys = store.add_course(t, name="大学物理（虚构）")
    lab = store.add_course(t, name="物理实验（虚构）", kind="lab")
    ids = {"term": t, "math": math, "phys": phys, "lab": lab,
           "math_mon": store.add_slot(math, weekday=1, weeks="1-16", period_start=1, period_end=2, location=ROOM),
           "math_wed": store.add_slot(math, weekday=3, weeks="1-16单", period_start=3, period_end=4, location=ROOM),
           "phys_tue": store.add_slot(phys, weekday=2, weeks="1-16", start="14:00", end="15:40"),
           "lab_thu": store.add_slot(lab, weekday=4, weeks="2-16双", period_start=5, period_end=6, location=ROOM_LAB)}
    ids["quiz"] = store.add_exam(t, name="高数小测（虚构）", exam_date="2026-09-11", kind="quiz", course_id=math,
                                 start="10:00", end="11:00", location=ROOM, seat=SEAT)
    return ids


@pytest.fixture()
def timetable(violetdb, clock):
    from ai.schedule.store import ScheduleStore
    store = ScheduleStore(violetdb)
    return store, fill(store)


@pytest.fixture()
def tools(timetable, clock):
    from ai.schedule.tools import ScheduleTools
    return ScheduleTools(timetable[0])


def courses(view):
    return [i["course"] for i in view["items"] if i["occupies"]]


# ----------------------------------------------------------------------------------------------- the four tools

def test_day_schedule_reads_the_real_store(tools):
    tomorrow = tools.day("tomorrow")                                       # Wed 2026-09-09, week 1 (odd)
    assert tomorrow["ok"] and tomorrow["date"] == "2026-09-09" and tomorrow["weekday"] == "周三"
    assert tomorrow["term"]["week"] == 1 and tomorrow["term"]["parity"] == "odd" and tomorrow["timezone"] == "Asia/Shanghai"
    math = tomorrow["items"][0]
    assert (math["course"], math["start"], math["end"], math["status_text"]) == ("高等数学（虚构）", "10:00", "11:40", "照常")
    assert tools.day("2026-09-16")["items"] == [] and "没有课" in tools.day("2026-09-16")["state_note"]   # even week
    assert tools.day("周五")["date"] == "2026-09-11" and courses(tools.day("周五")) == ["高数小测（虚构）"]
    assert tools.day("next tuesday")["error_code"] == "invalid_arguments"
    assert tools.day("2026-02-30")["error_code"] == "invalid_arguments"


def test_no_class_no_timetable_not_started_and_ended_are_told_apart(violetdb, clock, tools):
    from ai.schedule.store import ScheduleStore
    from ai.schedule.tools import ScheduleTools
    assert tools.day("2026-09-13")["data_state"] == "active" and "课表上没有课" in tools.day("2026-09-13")["state_note"]
    before = tools.day("2026-09-01")
    assert before["data_state"] == "not_started" and "还没开始" in before["state_note"] and before["items"] == []
    after = tools.day("2027-01-20")
    assert after["data_state"] == "ended" and "不要用旧课表" in after["state_note"]
    ScheduleStore(violetdb).purge_all(confirm=True)
    empty = ScheduleTools(ScheduleStore(violetdb)).day("today")
    assert empty["data_state"] == "no_data" and "不要说成没课" in empty["state_note"]


def test_changes_holidays_and_makeup_days_come_through(timetable, tools):
    store, ids = timetable
    store.add_exception(ids["math"], kind="move", on_date="2026-09-09", slot_id=ids["math_wed"], new_date="2026-09-10",
                        new_start="16:00", new_end="17:40")
    store.add_holiday(ids["term"], kind="holiday", name="国庆（虚构）", start_date="2026-10-01", end_date="2026-10-07")
    store.add_holiday(ids["term"], kind="makeup", name="国庆补课（虚构）", start_date="2026-10-10", follows_date="2026-10-07")
    wed = tools.day("2026-09-09")
    assert [(i["status"], i["occupies"], i.get("moved_to")) for i in wed["items"]] == [("moved_out", False, "2026-09-10")]
    thu = tools.day("2026-09-10")
    assert [(i["course"], i["status_text"], i["start"]) for i in thu["items"]] == [("高等数学（虚构）", "调到这天上", "16:00")]
    assert tools.day("2026-10-05")["day_kind"] == "holiday" and "放假" in tools.day("2026-10-05")["state_note"]
    makeup = tools.day("2026-10-10")
    assert makeup["day_kind"] == "makeup" and makeup["follows_date"] == "2026-10-07" and courses(makeup) == ["高等数学（虚构）"]


def test_range_compares_days_and_finds_exams(tools):
    week = tools.range("this_week", 7)
    assert week["start_date"] == "2026-09-07" and len(week["schedule"]) == 7
    assert week["free_days"] == ["2026-09-10", "2026-09-12", "2026-09-13"]       # Thu (odd week), Sat, Sun
    assert week["lightest_days"][0] == "2026-09-10"
    friday = next(s for s in week["summary"] if s["date"] == "2026-09-11")
    assert friday["exams"] == 1 and friday["classes"] == 0
    for bad in (0, 15, "x"):
        assert tools.range("today", bad)["error_code"] == "invalid_arguments"
    crossing = tools.range("2027-01-12", 7)                                      # over the end of the term
    assert crossing["data_states"] == ["active", "ended"]


def test_current_status_is_a_plan_from_eves_own_clock(tools, clock):
    clock.set(datetime(2026, 9, 7, 9, 0, tzinfo=SH))                             # Mon, in 高数 until 09:40
    now = tools.status()
    assert now["in_class_by_plan"] and now["current"][0]["course"] == "高等数学（虚构）"
    assert now["current"][0]["end"] == "09:40" and now["current"][0]["ends_in_minutes"] == 40
    assert now["basis"] == "timetable_plan" and "真实在哪里" in now["not_known"] and "计划" in now["plan_note"]
    clock.set(datetime(2026, 9, 8, 9, 30, tzinfo=SH))                            # Tue morning
    later = tools.status()
    assert not later["in_class_by_plan"] and later["next"]["course"] == "大学物理（虚构）"
    assert later["next"]["starts_in_minutes"] == 270 and later["remaining_today"] == 1
    clock.set(datetime(2027, 1, 18, 9, 0, tzinfo=SH))                            # a Monday after the term
    ended = tools.status()
    assert ended["data_state"] == "ended" and not ended["in_class_by_plan"] and ended["next"] is None


def test_find_course_by_name_abbreviation_or_teacher_and_never_invents(tools, clock):
    found = tools.find("高数")
    assert [m["course"] for m in found["matches"]] == ["高等数学（虚构）"] and found["matches"][0]["matched_by"] == "abbreviation"
    nxt = found["matches"][0]["upcoming"][0]
    assert (nxt["date"], nxt["start"]) == ("2026-09-09", "10:00")
    assert [x["course"] for x in found["matches"][0]["upcoming_exams"]] == ["高数小测（虚构）"]
    assert tools.find("物理")["matches"][0]["matched_by"] == "name" and len(tools.find("物理")["matches"]) == 2
    by_teacher = tools.find("老师甲")
    assert by_teacher["matches"][0]["matched_by"] == "teacher" and "teacher" not in by_teacher["matches"][0]
    nothing = tools.find("天文学")
    assert nothing["matches"] == [] and "不要编造" in nothing["note"]
    for bad in ("", " ", "长" * 31, "高\x00数"):
        assert tools.find(bad)["error_code"] == "invalid_arguments"
    clock.set(datetime(2026, 9, 9, 11, 0, tzinfo=SH))                            # during Wednesday's 高数
    assert tools.find("高数")["matches"][0]["upcoming"][0]["date"] == "2026-09-09"   # not over yet: still listed
    clock.set(datetime(2026, 9, 9, 12, 0, tzinfo=SH))                            # after it
    assert tools.find("高数")["matches"][0]["upcoming"][0]["date"] == "2026-09-14"
    clock.set(datetime(2027, 2, 1, 9, 0, tzinfo=SH))                             # term over
    old = tools.find("高数")
    assert old["matches"] == [] and old["data_state"] == "ended"


def test_find_course_is_bounded(timetable, tools):
    store, ids = timetable
    for n in range(7):
        store.add_course(ids["term"], name=f"选修{n}（虚构）")
    many = tools.find("选修")
    assert len(many["matches"]) == 5 and many["more_matches"] is True


# ----------------------------------------------------------------------------------------------- privacy

def test_personal_fields_only_for_the_users_own_question(tools, monkeypatch):
    from ai.schedule.access import turn_message
    plain = tools.day("tomorrow", details=["location", "teacher"])               # no user message at all
    assert "location" not in plain["items"][0] and plain["details_included"] == []
    with turn_message("明天有什么课"):
        asked_nothing = tools.day("tomorrow", details=["location"])
    assert "location" not in asked_nothing["items"][0] and "没有给" in asked_nothing["details_note"]
    with turn_message("明天要去哪个教室上课"):
        where = tools.day("tomorrow", details=["location", "teacher"])
    assert where["items"][0]["location"] == ROOM and "teacher" not in where["items"][0]
    assert where["details_included"] == ["location"]
    with turn_message("高数是哪个老师上的"):
        who = tools.find("高数", details=["teacher"])
    assert who["matches"][0]["teacher"] == TEACHER
    with turn_message("周五小测我坐几号座位，在哪个考场"):
        seat = tools.day("周五", details=["seat", "location"])
    assert seat["items"][0]["seat"] == SEAT and seat["items"][0]["location"] == ROOM
    monkeypatch.setenv("VIOLET_SCHEDULE_DETAIL_FIELDS", "teacher")                   # the location is never given now
    with turn_message("明天要去哪个教室上课"):
        assert "location" not in tools.day("tomorrow", details=["location"])["items"][0]
    with turn_message("高数的备注写了什么，哪个老师"):
        assert "私人备注" not in json.dumps(tools.find("高数", details=["notes", "teacher"]), ensure_ascii=False)  # never


def test_lookups_log_no_personal_value(tools, caplog):
    from ai.schedule.access import turn_message
    caplog.set_level(logging.DEBUG)
    with turn_message("明天去哪个教室，哪个老师，小测座位号"):
        tools.day("tomorrow", details=["location", "teacher", "seat"])
        tools.range("this_week", 7, details=["location", "seat"])
        tools.find("老师甲", details=["teacher"])
        tools.status(details=["location"])
    for value in (TEACHER, ROOM, ROOM_LAB, SEAT, "私人备注", "老师甲"):
        assert value not in caplog.text, value
    assert "[schedule] get_day_schedule" in caplog.text


# ----------------------------------------------------------------------------------------------- whole turns

@pytest.fixture()
def school(make_world, clock, violetdb):
    """VIOLET_SCHEDULE_ENABLED, the default on-demand offer, the fictional timetable in Violet's own database."""
    from ai.schedule.store import ScheduleStore
    w = make_world(VIOLET_SCHEDULE_ENABLED="1")
    w.ids = fill(ScheduleStore(violetdb))
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    w.chats = []
    return w


def wants(*tools, reason="课表"):
    return lambda kw: intent_json("chat", tool_query={"needed": True, "tools": list(tools), "reason": reason})


def result_of(kw, index=0):
    text = tool_texts(kw)[index]
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def tasks(w):
    return w.db.fetch_one("SELECT COUNT(*) AS n FROM task_sessions")["n"]


def test_intent_catalog_lists_the_schedule_tools_for_chat_only(school):
    seen = []
    school.llm.responders["fake-intent"] = lambda kw: (seen.append(kw), intent_json("chat"))[1]
    chat_plays(school, final="嗯嗯")
    say(school, "早上好呀")
    catalog = systems(seen[0])
    for name in (DAY, RANGE, STATUS, FIND):
        assert name in catalog
    assert "查用户自己的课表里某一天的课和考试" in catalog
    assert not offered(school.chats[0]) and "【这一轮先查再回答】" not in systems(school.chats[0])   # small talk
    from ai.mcp import builtin_servers, server_applies_to_agent
    server = next(s for s in builtin_servers() if s["name"] == "violet-schedule")
    assert server["agents"] == ["chat"] and server["offer"] == "on_demand"
    assert not server_applies_to_agent(server, "proactive") and not server_applies_to_agent(server, "performer")


def test_tomorrow_morning_is_looked_up_with_one_required_tool(school):
    w = school
    w.llm.responders["fake-intent"] = wants(DAY, reason="明天上午有没有课")

    def reply(kw):
        data = result_of(kw)
        morning = [i for i in data["items"] if i["occupies"] and i["start"] < "12:00"]
        return f"明天上午有{morning[0]['course'].split('（')[0]}，{morning[0]['start']} 开始哦" if morning else "明天上午没课～"
    chat_plays(w, (DAY, {"date": "tomorrow"}), final=reply)
    say(w, "明天上午有课吗？")
    assert offered(w.chats[0]) == [DAY] and w.chats[0]["tool_choice"] == "required"
    assert "【这一轮先查再回答】" in systems(w.chats[0])
    assert w.client.sent_texts[-1] == "明天上午有高等数学，10:00 开始哦"
    assert tasks(w) == 0


def test_which_day_is_free_this_week(school):
    w = school
    w.llm.responders["fake-intent"] = wants(RANGE, reason="这周哪天空")
    chat_plays(w, (RANGE, {"start_date": "this_week", "days": 7}),
               final=lambda kw: f"这周 {result_of(kw)['free_days'][0][5:]} 那天最空，一节课都没有")
    say(w, "这周哪天比较空？")
    assert w.client.sent_texts[-1] == "这周 09-10 那天最空，一节课都没有"


def test_when_is_math_next(school):
    w = school
    w.llm.responders["fake-intent"] = wants(FIND, reason="高数下一次")
    chat_plays(w, (FIND, {"query": "高数"}),
               final=lambda kw: "下一次是 {date} {start}".format(**result_of(kw)["matches"][0]["upcoming"][0]))
    say(w, "高数下一次什么时候上？")
    assert w.client.sent_texts[-1] == "下一次是 2026-09-09 10:00"


def test_am_i_out_of_class_is_answered_as_a_plan(school, clock):
    w = school
    clock.set(datetime(2026, 9, 7, 9, 0, tzinfo=SH))
    w.llm.responders["fake-intent"] = wants(STATUS, reason="现在下课没")

    def reply(kw):
        data = result_of(kw)
        assert "真实在哪里" in data["not_known"]
        return f"按课表还没下课哦，{data['current'][0]['end']} 才下" if data["in_class_by_plan"] else "按课表已经下课啦"
    chat_plays(w, (STATUS, {}), final=reply)
    say(w, "我现在下课了吗？")
    assert w.client.sent_texts[-1] == "按课表还没下课哦，09:40 才下"


def test_wednesday_afternoon_and_this_weeks_exams(school):
    w = school
    w.llm.responders["fake-intent"] = wants(DAY, reason="周三下午")
    chat_plays(w, (DAY, {"date": "周三"}),
               final=lambda kw: "周三下午没课" if not [i for i in result_of(kw)["items"] if i["occupies"]
                                                       and i["start"] >= "12:00"] else "周三下午还有课")
    say(w, "周三下午还有课吗？")
    assert w.client.sent_texts[-1] == "周三下午没课"
    w.chats.clear()
    w.llm.responders["fake-intent"] = wants(RANGE, reason="这周考试")
    chat_plays(w, (RANGE, {"start_date": "this_week", "days": 7}),
               final=lambda kw: "这周有：" + "、".join(i["course"] for d in result_of(kw)["schedule"] for i in d["items"]
                                                   if i["kind"] == "exam"))
    say(w, "这周有考试吗？")
    assert w.client.sent_texts[-1] == "这周有：高数小测（虚构）"


def test_the_classroom_only_when_the_user_asks_for_it(school):
    w = school
    w.llm.responders["fake-intent"] = wants(DAY, reason="明天的教室")
    chat_plays(w, (DAY, {"date": "tomorrow", "details": ["location"]}),
               final=lambda kw: "明天在" + (result_of(kw)["items"][0].get("location") or "（不知道）"))
    say(w, "明天要去哪个教室上课？")
    assert w.client.sent_texts[-1] == f"明天在{ROOM}"
    w.chats.clear()
    w.llm.responders["fake-intent"] = wants(DAY, reason="明天的课")
    chat_plays(w, (DAY, {"date": "tomorrow", "details": ["location", "teacher"]}),   # the model asks for more
               final=lambda kw: "明天在" + (result_of(kw)["items"][0].get("location") or "（不知道）"))
    say(w, "明天有什么课？")
    assert w.client.sent_texts[-1] == "明天在（不知道）"
    assert ROOM not in tool_texts(w.chats[-1])[0] and TEACHER not in tool_texts(w.chats[-1])[0]


def test_a_mood_with_a_class_in_it_is_left_to_intent(school):
    w = school
    w.llm.responders["fake-intent"] = lambda kw: intent_json("chat")              # Intent reads it as a mood
    chat_plays(w, final="抱抱你，明天的事明天再说")
    say(w, "最近有点烦，明天又要上课了")
    assert not offered(w.chats[0]) and "【这一轮先查再回答】" not in systems(w.chats[0])
    w.chats.clear()
    w.llm.responders["fake-intent"] = wants(DAY, reason="明天的课")               # ...or decides to look it up
    chat_plays(w, (DAY, {"date": "tomorrow"}), final="明天只有上午一节，下午可以歇歇")
    say(w, "最近有点烦，明天又要上课了")
    assert offered(w.chats[0]) == [DAY]


def test_two_tools_are_offered_on_auto(school):
    w = school
    w.llm.responders["fake-intent"] = wants(STATUS, DAY, reason="现在和明天")
    chat_plays(w, (STATUS, {}), (DAY, {"date": "tomorrow"}), final="现在没课，明天上午有一节")
    say(w, "我现在有课吗，明天呢")
    assert sorted(offered(w.chats[0])) == sorted([STATUS, DAY]) and w.chats[0]["tool_choice"] == "auto"


def test_a_switched_off_tool_degrades_and_creates_no_task(school):
    from ai.tool_manager import tool_store
    w = school
    tool_store().set_tool("violet-schedule", "get_day_schedule", False)

    def intent(kw):
        return intent_json("task", task={"exists": True, "name": "查课表", "description": "查明天的课"},
                           tool_query={"needed": True, "tools": [DAY], "reason": "明天的课"})
    w.llm.responders["fake-intent"] = intent
    chat_plays(w, final="课表查询现在关着，我这边看不了")
    say(w, "明天有什么课？")
    assert not offered(w.chats[0]) and "需要的工具已经停用了" in systems(w.chats[0])
    assert tasks(w) == 0


def test_not_enabled_means_not_configured_and_no_task(make_world, clock, violetdb):
    from ai.schedule.store import ScheduleStore
    w = make_world()                                                             # VIOLET_SCHEDULE_ENABLED unset
    fill(ScheduleStore(violetdb))
    w.chats = []
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    seen = []

    def intent(kw):
        seen.append(kw)
        return intent_json("task", task={"exists": True, "name": "查课表", "description": "查课表"},
                           tool_query={"needed": True, "tools": [DAY], "reason": "课表"})
    w.llm.responders["fake-intent"] = intent
    chat_plays(w, final="我这边还没法看你的课表")
    say(w, "明天有什么课？")
    assert "violet-schedule" not in systems(seen[0])                                # not in the catalog at all
    assert not offered(w.chats[0]) and "没有配置" in systems(w.chats[0]) and tasks(w) == 0


def test_a_failing_lookup_is_reported_not_invented(school, monkeypatch):
    from ai.schedule import tools as tools_module
    w = school
    monkeypatch.setattr(tools_module.ScheduleTools, "day", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    w.llm.responders["fake-intent"] = wants(DAY)
    chat_plays(w, (DAY, {"date": "tomorrow"}), final=lambda kw: "刚才没查到，晚点再看" if '"ok": false' in tool_texts(kw)[0]
               else "有课")
    say(w, "明天有课吗？")
    assert '"error_code": "schedule_error"' in tool_texts(w.chats[-1])[0]
    assert w.client.sent_texts[-1] == "刚才没查到，晚点再看" and tasks(w) == 0


def test_an_ended_term_is_not_used_for_now(school, clock):
    w = school
    clock.set(datetime(2027, 2, 1, 9, 0, tzinfo=SH))
    w.llm.responders["fake-intent"] = wants(DAY)
    chat_plays(w, (DAY, {"date": "today"}), final=lambda kw: result_of(kw)["state_note"][:20])
    say(w, "今天有课吗？")
    data = result_of(w.chats[-1])
    assert data["data_state"] == "ended" and data["items"] == [] and "不要用旧课表" in data["state_note"]


def test_saying_i_looked_at_your_timetable_is_not_an_unrecorded_experience(school):
    from ai.chat import guard_reply
    line = "我刚看了下你的课表，明天上午有一节高数"
    assert guard_reply(line)[1]                                                  # not looked up: flagged
    assert guard_reply(line, turn_used_schedule=True)[1] == []                   # looked up this turn: fine
    assert guard_reply("我刚看了下你的课表，然后去了食堂", turn_used_schedule=True)[1]   # a lived claim stays checked
    w = school
    w.llm.responders["fake-intent"] = wants(DAY)
    chat_plays(w, (DAY, {"date": "tomorrow"}), final=line)
    say(w, "明天上午有课吗")
    assert w.client.sent_texts[-1] == line


# ----------------------------------------------------------------------------------------------- memory

@pytest.fixture()
def llm(monkeypatch, violetdb):
    from tests.fakes import FakeLLM, default_responders, install_fake_llm
    return install_fake_llm(monkeypatch, FakeLLM(default_responders("好呀")))


@pytest.fixture()
def memory_agent(llm, monkeypatch):
    from ai.agents import MemoryAgent
    monkeypatch.setenv("VIOLET_SCHEDULE_ENABLED", "1")
    return MemoryAgent()


def save_as(agent, user_message, **args):
    from ai.agents.base_agent import _CALL_CONTEXT
    token = _CALL_CONTEXT.set({"turn_id": "t-1", "user_message": user_message})
    try:
        return json.loads(agent._tool_save_memory({"category": "event", "kind": "event", **args}))
    finally:
        _CALL_CONTEXT.reset(token)


def events(db):
    return db.fetch_one("SELECT COUNT(*) AS n FROM event_memories WHERE status != 'deleted'")["n"]


def test_the_timetable_is_never_copied_into_event_memories(timetable, memory_agent, violetdb):
    refused = save_as(memory_agent, "明天上午有课吗？", summary="用户明天上午 10 点有高等数学课", progress="planned",
                      event_date="2026-09-09")
    assert refused["action"] == "rejected" and refused["reason"] == "timetable" and events(violetdb) == 0
    refused = save_as(memory_agent, "这周有考试吗", summary="用户周五有高数小测", progress="planned")
    assert refused["reason"] == "timetable"
    # what the user says themselves is their own news and is kept -- also with an abbreviation
    kept = save_as(memory_agent, "明天高数停课了，好耶", summary="用户的高等数学课明天停课", progress="planned")
    assert kept.get("action") != "rejected" and events(violetdb) == 1
    kept = save_as(memory_agent, "我周末要去爬山", summary="用户周末去爬山", progress="planned")
    assert kept.get("action") != "rejected"


def test_the_memory_check_is_off_with_the_timetable(timetable, llm, monkeypatch, violetdb):
    from ai.agents import MemoryAgent
    monkeypatch.delenv("VIOLET_SCHEDULE_ENABLED", raising=False)
    kept = save_as(MemoryAgent(), "明天上午有课吗？", summary="用户明天上午有高等数学课", progress="planned")
    assert kept.get("action") != "rejected"


def test_a_whole_turn_does_not_store_the_lookup(school):
    from tests.test_phase2_scenarios import script, tool
    w = school
    w.llm.responders["fake-intent"] = wants(DAY)
    chat_plays(w, (DAY, {"date": "tomorrow"}), final="明天上午 10 点有高数哦")
    w.llm.responders["fake-memory"] = script(
        tool("save_memory", summary="用户明天上午 10 点有高等数学课", category="event", kind="event", progress="planned",
             event_date="2026-09-09"))
    say(w, "明天上午有课吗？")
    assert events(w.db) == 0


# ----------------------------------------------------------------------------------------------- the replay

REPLAY = [
    # (user message, what the fake Intent decides, the calls the fake Chat makes, how it words the result)
    ("明天上午有课吗？", [DAY], [(DAY, {"date": "tomorrow"})], "day_morning"),
    ("这周哪天比较空？", [RANGE], [(RANGE, {"start_date": "this_week", "days": 7})], "free"),
    ("高数下一次什么时候上？", [FIND], [(FIND, {"query": "高数"})], "next"),
    ("我现在下课了吗？", [STATUS], [(STATUS, {})], "status"),
    ("周三下午还有课吗？", [DAY], [(DAY, {"date": "周三"})], "afternoon"),
    ("这周有考试吗？", [RANGE], [(RANGE, {"start_date": "this_week", "days": 7})], "exams"),
    ("明天要去哪个教室上课？", [DAY], [(DAY, {"date": "tomorrow", "details": ["location"]})], "room"),
    ("最近有点烦，明天又要上课了", [], [], "mood"),
]


def _word(style: str, results: list[dict]) -> str:
    r = results[0] if results else {}
    if style == "day_morning":
        morning = [i for i in r["items"] if i["occupies"] and i["start"] and i["start"] < "12:00"]
        return (f"有的，明天上午{morning[0]['start']}有{morning[0]['course'].split('（')[0]}，别迟到啦" if morning
                else "明天上午没课，可以睡个懒觉～")
    if style == "free":
        day = r["free_days"][0] if r["free_days"] else r["lightest_days"][0]
        return f"看了下课表，这周{_weekday(day)}最空，一节课都没有"
    if style == "next":
        nxt = r["matches"][0]["upcoming"][0]
        return f"高数下一次是{nxt['weekday']}（{nxt['date'][5:]}）{nxt['start']}"
    if style == "status":
        if r["in_class_by_plan"]:
            return f"按课表你这会儿应该还在上{r['current'][0]['course'].split('（')[0]}，{r['current'][0]['end']}才下课"
        return "按课表现在没课啦"
    if style == "afternoon":
        afternoon = [i for i in r["items"] if i["occupies"] and i["start"] and i["start"] >= "12:00"]
        return "周三下午还有课" if afternoon else "周三下午没课，上午那节上完就自由啦"
    if style == "exams":
        exams = [(d["weekday"], i["course"]) for d in r["schedule"] for i in d["items"] if i["kind"] == "exam"]
        return "这周有一个：" + "、".join(f"{w}的{c.split('（')[0]}" for w, c in exams) if exams else "这周没有考试"
    if style == "room":
        return f"明天在{r['items'][0].get('location') or '……课表里没写教室'}"
    return "抱抱你～烦的时候就先别想明天啦，今晚早点休息"


def _weekday(day: str) -> str:
    from ai.schedule.access import WEEKDAY_NAMES
    from datetime import date
    return WEEKDAY_NAMES[date.fromisoformat(day).isoweekday()]


def test_offline_dialogue_replay(school, clock):
    """The eight phrases of the 9.4.2 brief through the whole pipeline; VIOLET_P942_REPLAY=<path> saves the record."""
    import os
    from pathlib import Path
    w = school
    clock.set(datetime(2026, 9, 8, 10, 0, tzinfo=SH))                             # Tue 10:00, week 1
    record = []
    def intent_for(tools):
        extra = {"tool_query": {"needed": True, "tools": tools, "reason": "课表"}} if tools else {}
        return lambda kw: intent_json("chat", **extra)

    for text, planned, calls, style in REPLAY:
        w.llm.responders["fake-intent"] = intent_for(planned)
        w.chats.clear()
        chat_plays(w, *calls, final=lambda kw, style=style: _word(style, [
            json.loads(re.search(r"\{.*\}", t, re.S).group(0)) for t in tool_texts(kw)]))
        if style == "status":
            clock.set(datetime(2026, 9, 7, 9, 0, tzinfo=SH))                      # this one is asked in Monday's class
        say(w, text)
        if style == "status":
            clock.set(datetime(2026, 9, 8, 10, 0, tzinfo=SH))
        first, last = w.chats[0], w.chats[-1]
        results = [json.loads(re.search(r"\{.*\}", t, re.S).group(0)) for t in tool_texts(last)]
        record.append({"user": text, "intent_tools": planned, "offered": offered(first),
                       "tool_choice": first.get("tool_choice") if offered(first) else None,
                       "calls": [{"tool": n, "arguments": a} for n, a in calls],
                       "results": [{k: r.get(k) for k in ("data_state", "state_note", "date", "free_days", "in_class_by_plan",
                                                          "details_included") if k in r} for r in results],
                       "reply": w.client.sent_texts[-1]})
    replies = [r["reply"] for r in record]
    assert replies == [
        "有的，明天上午10:00有高等数学，别迟到啦", "看了下课表，这周周四最空，一节课都没有", "高数下一次是周三（09-09）10:00",
        "按课表你这会儿应该还在上高等数学，09:40才下课", "周三下午没课，上午那节上完就自由啦", "这周有一个：周五的高数小测",
        f"明天在{ROOM}", "抱抱你～烦的时候就先别想明天啦，今晚早点休息"]
    assert [r["tool_choice"] for r in record] == ["required"] * 7 + [None]
    assert tasks(w) == 0 and events(w.db) == 0
    out = os.environ.get("VIOLET_P942_REPLAY")
    if out:
        Path(out).write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


# ----------------------------------------------------------------------------------------------- web stays apart

def test_web_search_never_gets_timetable_personal_values(timetable, violetdb):
    from ai.web.providers import MockSearchProvider
    from ai.web.service import WebConfig, WebService
    svc = WebService(violetdb, provider=MockSearchProvider(), config=WebConfig(enabled=True))
    assert svc.search(f"{ROOM} 怎么走")["error_code"] == "blocked_private_data"
    assert svc.search(f"{TEACHER} 怎么样")["error_code"] == "blocked_private_data"
