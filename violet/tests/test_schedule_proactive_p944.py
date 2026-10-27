"""Phase 9.4.4: the timetable in Violet's proactive side -- the class / exam gate, the final check before sending, the
optional moments (exam tomorrow, classes over, dense week) and their once-only bookkeeping, the chat status line.

A fictional term in the isolated test database, Violet's clock pinned (and moved by hand), the fake SDK for every model
call, the real scheduler / runtime / pipeline. No network, no real model."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_phase2_scenarios import arm, chat, decide, latest_event, make_world, say  # noqa: F401

SH = timezone(timedelta(hours=8))
TEACHER, ROOM, SEAT, NOTE = "虚构老师甲", "虚构楼101", "虚构座位17", "虚构的私人备注"
PERIODS = [(1, "08:00", "08:45"), (2, "08:55", "09:40"), (3, "10:00", "10:45"), (4, "10:55", "11:40"),
           (5, "14:00", "14:45"), (6, "14:55", "15:40")]
ON = {"VIOLET_SCHEDULE_ENABLED": "1", "VIOLET_SCHEDULE_PROACTIVE": "1"}


def at(day: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    """2026-09-<day> hh:mm Shanghai (Mon 2026-09-07 = week 1)."""
    return datetime(2026, 9, day, hh, mm, ss, tzinfo=SH)


@pytest.fixture()
def clock(monkeypatch):
    """Violet's clock, frozen at a moment and moved only by clock.set()."""
    from ai import settings
    state = {"now": at(7, 6)}
    monkeypatch.setattr(settings, "now_local", lambda: state["now"])
    monkeypatch.setenv("VIOLET_TIMEZONE", "Asia/Shanghai")

    def set_to(moment):
        state["now"] = moment
    set_to.now = lambda: state["now"]
    return set_to


def fill(store):
    """2026 秋（虚构）: 高数 Mon p1-2 (08:00-09:40) weeks 1-16 + Wed p3-4 odd weeks; 英语 Mon 10:00-11:40 weeks 1-16;
    物理 Tue 14:00-15:40 + 实验 Tue 15:00-16:00 (overlapping); 体育 Fri p3-4 weeks 1-8,10-16; a 高数 quiz Thu 09-10
    10:00-11:00 with a seat; a holiday 10-01..10-07."""
    t = store.create_term(name="2026 秋（虚构）", start_date="2026-09-07", end_date="2027-01-15", timezone="Asia/Shanghai")
    store.set_periods(t, PERIODS)
    ids = {"term": t,
           "math": store.add_course(t, name="高等数学（虚构）", teacher=TEACHER, notes=NOTE),
           "eng": store.add_course(t, name="大学英语（虚构）", teacher=TEACHER),
           "phys": store.add_course(t, name="大学物理（虚构）"),
           "lab": store.add_course(t, name="物理实验（虚构）", kind="lab"),
           "pe": store.add_course(t, name="体育（虚构）", kind="pe")}
    ids["math_mon"] = store.add_slot(ids["math"], weekday=1, weeks="1-16", period_start=1, period_end=2, location=ROOM)
    ids["math_wed"] = store.add_slot(ids["math"], weekday=3, weeks="1-16单", period_start=3, period_end=4, location=ROOM)
    ids["eng_mon"] = store.add_slot(ids["eng"], weekday=1, weeks="1-16", start="10:00", end="11:40", location=ROOM)
    ids["phys_tue"] = store.add_slot(ids["phys"], weekday=2, weeks="1-16", start="14:00", end="15:40", location=ROOM)
    ids["lab_tue"] = store.add_slot(ids["lab"], weekday=2, weeks="1-16", start="15:00", end="16:00", location=ROOM)
    ids["pe_fri"] = store.add_slot(ids["pe"], weekday=5, weeks="1-8,10-16", period_start=3, period_end=4)
    ids["quiz"] = store.add_exam(t, name="高数小测（虚构）", exam_date="2026-09-10", kind="quiz", course_id=ids["math"],
                                 start="10:00", end="11:00", location=ROOM, seat=SEAT, notes=NOTE)
    store.add_holiday(t, kind="holiday", name="国庆（虚构）", start_date="2026-10-01", end_date="2026-10-07")
    return ids


@pytest.fixture()
def timetable(violetdb, clock):
    from ai.schedule.store import ScheduleStore
    store = ScheduleStore(violetdb)
    return store, fill(store)


def gate(violetdb, moment):
    from ai.schedule.awareness import Awareness
    from ai.schedule.store import ScheduleStore
    return Awareness(ScheduleStore(violetdb), "Asia/Shanghai").gate(moment)


def moments(violetdb, moment, **kw):
    from ai.schedule.awareness import Awareness
    from ai.schedule.store import ScheduleStore
    return Awareness(ScheduleStore(violetdb), "Asia/Shanghai").moments(moment, violetdb, **kw)


# ----------------------------------------------------------------------------------------------- the gate (pure)

def test_class_window_is_five_minutes_before_to_five_after(violetdb, timetable):
    assert not gate(violetdb, at(7, 7, 54, 59)).blocked
    g = gate(violetdb, at(7, 7, 55))
    assert g.blocked and g.code == "in_class" and g.until == at(7, 9, 45)     # 高数 p1-2 08:00-09:40 (+5)
    assert gate(violetdb, at(7, 9, 44, 59)).blocked
    assert not gate(violetdb, at(7, 9, 45)).blocked                             # until 09:55 (英语 10:00 - 5)
    assert gate(violetdb, at(7, 9, 55)).until == at(7, 11, 45)


def test_exam_window_is_thirty_minutes_before_to_the_end(violetdb, timetable):
    assert not gate(violetdb, at(10, 9, 29, 59)).blocked                        # Thu: only the quiz 10:00-11:00
    g = gate(violetdb, at(10, 9, 30))
    assert g.blocked and g.code == "exam" and g.until == at(10, 11, 0)
    assert gate(violetdb, at(10, 10, 59, 59)).blocked
    assert not gate(violetdb, at(10, 11, 0)).blocked


def test_back_to_back_and_overlapping_classes_merge(violetdb, timetable):
    store, ids = timetable
    g = gate(violetdb, at(8, 14, 30))                                           # Tue 物理 14:00-15:40 + 实验 15:00-16:00
    assert g.until == at(8, 16, 5)
    other = store.add_course(ids["term"], name="接着上的课（虚构）")
    store.add_slot(other, weekday=4, weeks="1-16", start="08:00", end="08:45")
    store.add_slot(other, weekday=4, weeks="1-16", start="08:55", end="09:40")    # a 10-minute break: one window
    assert gate(violetdb, at(17, 8, 50)).until == at(17, 9, 45)                 # Thu week 2 (no quiz)
    g = gate(violetdb, at(10, 8, 50))                                           # Thu 09-10: the quiz window (09:30-11:00)
    assert (g.until, g.code) == (at(10, 11, 0), "exam")                      # joins the classes: one, until 11:00


def test_parity_holidays_cancellations_and_moves_use_the_computed_day(violetdb, timetable):
    store, ids = timetable
    assert gate(violetdb, at(9, 10, 30)).blocked                                # Wed week 1 (odd): 高数
    assert not gate(violetdb, at(16, 10, 30)).blocked                           # Wed week 2 (even): nothing
    assert not gate(violetdb, at(1, 10, 30).replace(month=10)).blocked          # 国庆 Thu 10-01... no class on Thu
    assert not gate(violetdb, datetime(2026, 10, 5, 8, 30, tzinfo=SH)).blocked  # 国庆 Monday: holiday
    assert not gate(violetdb, datetime(2026, 11, 6, 10, 30, tzinfo=SH)).blocked  # Fri week 9: 体育 skipped
    assert gate(violetdb, datetime(2026, 11, 13, 10, 30, tzinfo=SH)).blocked     # Fri week 10
    store.add_exception(ids["math"], kind="cancel", on_date="2026-09-14", slot_id=ids["math_mon"])
    assert not gate(violetdb, at(14, 8, 30)).blocked                            # cancelled
    store.add_exception(ids["math"], kind="move", on_date="2026-09-23", slot_id=ids["math_wed"], new_date="2026-09-24",
                        new_start="16:00", new_end="17:40")
    assert not gate(violetdb, at(23, 10, 30)).blocked                           # moved away
    assert gate(violetdb, at(24, 16, 30)).until == at(24, 17, 45)               # ... to here


def test_a_change_counts_at_the_next_check(violetdb, timetable):
    store, ids = timetable
    assert gate(violetdb, at(21, 8, 30)).blocked
    exc = store.add_exception(ids["math"], kind="cancel", on_date="2026-09-21", slot_id=ids["math_mon"])
    assert not gate(violetdb, at(21, 8, 30)).blocked
    store.revoke_exception(exc)
    assert gate(violetdb, at(21, 8, 30)).blocked


def test_no_timetable_not_started_ended_and_errors_never_block(violetdb, timetable, monkeypatch):
    assert gate(violetdb, at(1, 9, 0)).code == "not_started"
    assert gate(violetdb, datetime(2027, 1, 18, 9, 0, tzinfo=SH)).code == "ended"
    assert not gate(violetdb, datetime(2027, 1, 18, 9, 0, tzinfo=SH)).blocked
    from ai.schedule.store import ScheduleStore
    monkeypatch.setattr(ScheduleStore, "load", lambda self: (_ for _ in ()).throw(RuntimeError("broken")))
    g = gate(violetdb, at(7, 8, 30))
    assert (g.blocked, g.code) == (False, "error")
    assert moments(violetdb, at(9, 18, 0)) == []                               # never pretends to know
    monkeypatch.undo()
    ScheduleStore(violetdb).purge_all(confirm=True)
    assert gate(violetdb, at(7, 8, 30)).code == "no_timetable"


# ----------------------------------------------------------------------------------------------- the scheduler

def world(make_world, **env):
    """make_world with quiet hours pinned to 03:00-03:01 (far_quiet_hours() follows the real clock, ours is frozen)."""
    w = make_world(**env)
    cfg = json.loads(w.scheduler.config_path.read_text(encoding="utf-8"))
    cfg["quiet_hours"] = {"start": "03:00", "end": "03:01"}
    w.scheduler.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    w.scheduler.load_config()
    return w


def started(w, clock, moment, last_user=timedelta(hours=3)):
    """The user last wrote `last_user` before `moment`; the clock is at `moment`."""
    clock(moment - last_user)
    w.db.add_user_message("text", "我先去忙啦")
    clock(moment)


def test_no_proactive_message_and_no_model_call_in_class(make_world, clock, timetable):
    w = world(make_world, **ON)
    started(w, clock, at(7, 8, 30))
    decide(w, chat_brief={"topic": "想你了"})
    chat(w, proactive="在干嘛呀")
    arm(w)
    w.scheduler.tick()
    assert w.llm.calls_for("fake-proactive") == [] and w.client.sent_texts == []
    due, reason = w.scheduler._is_due(clock.now())
    assert not due and reason == "not due yet"                               # postponed past the class, not queued
    next_at = datetime.fromisoformat(w.db.get_proactive_setting("next_proactive_at"))
    assert next_at >= at(7, 9, 45) + timedelta(minutes=60)                   # window end + the usual random wait
    clock(at(7, 9, 46))                                                      # class over: nothing is "made up"
    w.scheduler.tick()
    assert w.llm.calls_for("fake-proactive") == [] and w.client.sent_texts == []
    clock(next_at)                                                           # 10:45: 英语 (10:00-11:40) -> later again
    w.scheduler.tick()
    assert w.llm.calls_for("fake-proactive") == [] and w.client.sent_texts == []
    next_at = datetime.fromisoformat(w.db.get_proactive_setting("next_proactive_at"))
    assert next_at >= at(7, 11, 45) + timedelta(minutes=60)
    clock(next_at)                                                           # its own time, no class: one ordinary check
    w.scheduler.tick()
    assert w.client.sent_texts == ["在干嘛呀"] and len(w.llm.calls_for("fake-proactive")) == 1


def test_explicit_wishes_quiet_hours_and_busy_come_first(make_world, clock, timetable):
    w = world(make_world, **ON)
    started(w, clock, at(7, 8, 30))
    from ai.profile_store import Profile
    profile = Profile(w.db)
    arm(w)
    profile.situations.observe("availability", "在忙", "user_stated", evidence="我在忙", user_message="我在忙",
                               until=(at(7, 12, 0)).isoformat())
    assert w.scheduler._is_due(clock.now())[1].startswith("the user said they are busy")
    profile.preferences.set_explicit("proactive_chat", "no", evidence="别主动找我", user_message="别主动找我")
    assert w.scheduler._is_due(clock.now())[1] == "the user asked Violet not to start conversations"
    w.write_config = None
    cfg = json.loads(w.scheduler.config_path.read_text(encoding="utf-8"))
    cfg["quiet_hours"] = {"start": "08:00", "end": "09:00"}
    w.scheduler.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    w.scheduler.load_config()
    assert w.scheduler._is_due(clock.now())[1] == "quiet hours"


def test_a_fixed_time_task_in_class_is_skipped_not_queued(make_world, clock, timetable):
    w = world(make_world, **ON)
    cfg = json.loads(w.scheduler.config_path.read_text(encoding="utf-8"))
    cfg["allowed_tasks"] = {"daily_checkin": {"enabled": True, "fixed_check_time": "08:30", "description": "测试任务"}}
    w.scheduler.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    w.scheduler.load_config()

    def fixed_runs():
        return [c for c in w.llm.calls_for("fake-proactive")
                if json.loads(c["messages"][-1]["content"]).get("event_type") == "fixed_time_task_check"]
    started(w, clock, at(7, 8, 30))
    w.scheduler.tick()
    assert w.llm.calls_for("fake-proactive") == []
    assert not w.db.get_proactive_setting("fixed_check_done_daily_checkin_2026-09-07")      # not marked, not queued
    clock(at(7, 9, 50))                                                      # after class: its time has passed
    w.scheduler.tick()
    assert fixed_runs() == []                                                # (an ordinary check may run here)
    clock(at(8, 8, 30))                                                      # Tue 08:30, no class: it runs
    w.scheduler.tick()
    assert len(fixed_runs()) == 1


def test_reminders_the_user_asked_for_still_arrive_in_class(make_world, clock, timetable):
    w = world(make_world, **ON)
    started(w, clock, at(7, 8, 30))
    result = w.db.add_commitment("交实验报告", (clock.now() + timedelta(hours=1)).isoformat(), evidence="一会儿提醒我交实验报告")
    past = (clock.now() - timedelta(minutes=1)).isoformat()
    w.db.update_commitment(result["commitment"]["id"], due_at=past, next_attempt_at=past)
    chat(w, reminder="记得交实验报告哦")
    w.scheduler.tick()
    assert w.client.sent_texts == ["记得交实验报告哦"]
    assert w.db.list_commitments(["sent"])


def test_the_user_writing_in_class_gets_an_answer(make_world, clock, timetable):
    w = world(make_world, **ON, VIOLET_SCHEDULE_CONTEXT="1")
    clock(at(7, 8, 30))
    seen = []
    w.llm.responders["fake-chat"] = lambda kw: (seen.append(kw), "我在呢")[1]
    say(w, "上课好无聊")
    assert w.client.sent_texts == ["我在呢"]
    system = "\n".join(m.get("content") or "" for m in seen[0]["messages"] if m["role"] == "system")
    assert "按课表，对方这会儿可能在上课（大约到 09:45）" in system and "照常回复" in system
    assert "高等数学" not in system and ROOM not in system and TEACHER not in system


def test_the_chat_status_line_is_off_by_default(make_world, clock, timetable):
    w = world(make_world, **ON)
    clock(at(7, 8, 30))
    seen = []
    w.llm.responders["fake-chat"] = lambda kw: (seen.append(kw), "嗯嗯")[1]
    say(w, "上课好无聊")
    assert "按课表" not in json.dumps(seen[0]["messages"], ensure_ascii=False)


def test_final_check_stops_a_message_when_class_started_meanwhile(make_world, clock, timetable):
    w = world(make_world, **ON)
    started(w, clock, at(7, 7, 30))                                          # before the 07:55 window
    decide(w, chat_brief={"topic": "早安"})

    def slow_chat(kw):                                                       # generation ends inside the window
        clock(at(7, 7, 58))
        return "早呀"
    chat(w, proactive=slow_chat)
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts == []
    event = latest_event(w)
    assert event["status"] == "skipped" and event["reason"].startswith("final check: schedule: class")
    assert datetime.fromisoformat(w.db.get_proactive_setting("next_proactive_at")) >= at(7, 9, 45)


def test_switched_off_means_exactly_the_old_behaviour(make_world, clock, timetable, monkeypatch):
    w = world(make_world, VIOLET_SCHEDULE_ENABLED="1")                                 # VIOLET_SCHEDULE_PROACTIVE not set
    started(w, clock, at(7, 8, 30))
    decide(w, chat_brief={"topic": "想你了"})
    chat(w, proactive="在干嘛呀")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts == ["在干嘛呀"]                               # in class by the plan: not looked at
    context = json.loads(w.llm.calls_for("fake-proactive")[-1]["messages"][-1]["content"])
    assert "schedule_note" not in context and "allowed_schedule_moments" not in context["conversation_state"]
    monkeypatch.delenv("VIOLET_SCHEDULE_ENABLED")
    monkeypatch.setenv("VIOLET_SCHEDULE_PROACTIVE", "1")                        # without the master switch: nothing
    assert w.scheduler._schedule() is None and w.scheduler._schedule_gate(clock.now()) is None


# ----------------------------------------------------------------------------------------------- moments

def test_exam_tomorrow_is_offered_with_the_minimum_and_wished_once(make_world, clock, timetable, caplog):
    caplog.set_level(logging.INFO)
    w = world(make_world, **ON)
    started(w, clock, at(9, 18, 0))                                          # Wed evening, the quiz is Thu 10:00
    decide(w, chat_brief={"topic": "明天小测，祝顺利"}, mentioned_schedule_ids=[f"schedule:exam:{timetable[1]['quiz']}"])
    chat(w, proactive="明天小测加油呀")
    arm(w)
    w.scheduler.tick()
    context = json.loads(w.llm.calls_for("fake-proactive")[-1]["messages"][-1]["content"])
    offered = context["conversation_state"]["allowed_schedule_moments"]
    exam = next(m for m in offered if m["type"] == "exam_tomorrow")
    assert (exam["exam"], exam["kind"], exam["start"]) == ("高数小测（虚构）", "小测", "10:00")
    text = json.dumps(context, ensure_ascii=False)
    for secret in (TEACHER, ROOM, SEAT, NOTE):
        assert secret not in text                                            # never in the decision's input
    event = latest_event(w)
    assert event["status"] == "sent" and SEAT not in (event.get("metadata") or "")
    assert w.db.list_topic_mentions(topic_key=exam["id"])
    for record in caplog.records:
        for secret in (TEACHER, ROOM, SEAT, NOTE):
            assert secret not in record.getMessage()
    # once: not offered again, and a decision naming it is refused
    later = clock.now() + timedelta(hours=4)
    clock(later)
    assert not [m for m in moments(w.db, later) if m["type"] == "exam_tomorrow"]


def test_an_exam_named_in_the_sent_text_counts_even_without_the_id(make_world, clock, timetable):
    w = world(make_world, **ON)
    started(w, clock, at(9, 18, 0))
    decide(w, chat_brief={"topic": "随便聊聊"})
    chat(w, proactive="对了明天高数小测别紧张")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts == ["对了明天高数小测别紧张"]
    assert not [m for m in moments(w.db, clock.now()) if m["type"] == "exam_tomorrow"]


def test_a_restart_remembers_what_was_already_said(make_world, clock, timetable):
    w = world(make_world, **ON)
    started(w, clock, at(9, 18, 0))
    decide(w, mentioned_schedule_ids=[f"schedule:exam:{timetable[1]['quiz']}"], chat_brief={"topic": "祝顺利"})
    chat(w, proactive="明天加油")
    arm(w)
    w.scheduler.tick()
    from ai.proactive_scheduler import ProactiveScheduler
    fresh = ProactiveScheduler(None, None, None, send_messages=lambda m, e=None: None,
                               config_path=w.scheduler.config_path)          # a new process would build this
    context = fresh._build_event_context(clock.now())
    assert not [m for m in context["conversation_state"].get("allowed_schedule_moments") or []
                if m["type"] == "exam_tomorrow"]


def test_a_decision_may_not_name_a_moment_that_was_not_offered(make_world, clock, timetable):
    w = world(make_world, **ON)
    started(w, clock, at(9, 18, 0))
    decide(w, mentioned_schedule_ids=["schedule:exam:999"], chat_brief={"topic": "x"})
    chat(w, proactive="明天加油")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts == [] and "schedule moment schedule:exam:999 was not offered" in latest_event(w)["reason"]


def test_changed_or_cancelled_exams_and_avoided_topics(make_world, clock, timetable):
    store, ids = timetable
    w = world(make_world, **ON)
    clock(at(9, 18, 0))
    assert [m["exam"] for m in moments(w.db, clock.now()) if m["type"] == "exam_tomorrow"] == ["高数小测（虚构）"]
    store.update_exam(ids["quiz"], {"exam_date": "2026-09-17"})                # moved a week: not tomorrow any more
    assert not [m for m in moments(w.db, clock.now()) if m["type"] == "exam_tomorrow"]
    clock(at(16, 18, 0))
    assert [m["start"] for m in moments(w.db, clock.now()) if m["type"] == "exam_tomorrow"] == ["10:00"]
    store.set_exam_status(ids["quiz"], "cancelled")
    assert not [m for m in moments(w.db, clock.now()) if m["type"] == "exam_tomorrow"]
    store.set_exam_status(ids["quiz"], "active")
    from ai.profile_store import Profile
    Profile(w.db).preferences.set_explicit("avoid_topic", "考试", evidence="别老提考试", user_message="别老提考试了")
    assert not [m for m in moments(w.db, clock.now(), avoid_topics=["考试"]) if m["type"] == "exam_tomorrow"]
    w.db.add_user_message("text", "别老提考试了")
    w.scheduler.load_config()
    context = w.scheduler._build_event_context(clock.now())
    assert not [m for m in context["conversation_state"].get("allowed_schedule_moments") or []
                if m["type"] == "exam_tomorrow"]


def test_classes_over_today_is_a_plan_once_a_day_and_never_while_unanswered(make_world, clock, timetable):
    w = world(make_world, **ON)
    clock(at(7, 11, 50))                                                     # Mon: last class 英语 ends 11:40
    over = [m for m in moments(w.db, clock.now()) if m["type"] == "classes_over_today"]
    assert over and over[0]["last_end"] == "11:40" and "不要说“你下课了吧”" in over[0]["note"]
    assert not [m for m in moments(w.db, at(7, 11, 30)) if m["type"] == "classes_over_today"]    # still in class
    assert not [m for m in moments(w.db, at(7, 15, 0)) if m["type"] == "classes_over_today"]     # too long after
    assert not [m for m in moments(w.db, at(8, 12, 0)) if m["type"] == "classes_over_today"]     # Tue: more later
    store, ids = timetable                                                   # Thu 09-10: a class, then the quiz
    early = store.add_course(ids["term"], name="早课（虚构）")
    store.add_slot(early, weekday=4, weeks="1", start="08:00", end="08:45")
    assert not [m for m in moments(w.db, at(10, 9, 0)) if m["type"] == "classes_over_today"]     # the exam is ahead
    assert [m for m in moments(w.db, at(10, 11, 30)) if m["type"] == "classes_over_today"]       # after it: yes
    assert moments(w.db, clock.now(), unanswered=True) == []                 # no extra reason after silence
    w.db.add_topic_mention(over[0]["id"], "violet_schedule")
    assert not [m for m in moments(w.db, clock.now()) if m["type"] == "classes_over_today"]


def test_dense_week_is_offered_once_a_week(violetdb, timetable):
    store, ids = timetable
    for day in (3, 4, 5):                                                    # Wed-Fri: four classes each
        c = store.add_course(ids["term"], name=f"加的课{day}（虚构）")
        for start in ("13:00", "17:00", "18:00", "19:00"):
            h = int(start[:2])
            store.add_slot(c, weekday=day, weeks="1-16", start=start, end=f"{h:02d}:50")
    dense = [m for m in moments(violetdb, at(7, 20, 0)) if m["type"] == "dense_week"]
    assert dense and dense[0]["full_days"] >= 3 and "不要因此多发消息" in dense[0]["note"]
    violetdb.add_topic_mention(dense[0]["id"], "violet_schedule")
    assert not [m for m in moments(violetdb, at(8, 20, 0)) if m["type"] == "dense_week"]


def test_moments_never_create_a_message_on_their_own(make_world, clock, timetable):
    """The schedule adds background only: with the default "do not act" decision nothing is sent, and no check runs
    before the scheduler's own time."""
    w = world(make_world, **ON)
    started(w, clock, at(9, 18, 0))
    w.scheduler.tick()                                                       # next time not reached
    assert w.llm.calls_for("fake-proactive") == []
    arm(w)
    w.scheduler.tick()
    assert len(w.llm.calls_for("fake-proactive")) == 1 and w.client.sent_texts == []
    assert latest_event(w)["status"] == "skipped"


def test_no_timetable_text_reaches_long_term_memory(make_world, clock, timetable):
    w = world(make_world, **ON)
    started(w, clock, at(9, 18, 0))
    decide(w, mentioned_schedule_ids=[f"schedule:exam:{timetable[1]['quiz']}"], chat_brief={"topic": "祝顺利"})
    chat(w, proactive="明天高数小测加油")
    arm(w)
    w.scheduler.tick()
    assert w.db.fetch_one("SELECT COUNT(*) AS n FROM event_memories")["n"] == 0
    assert w.db.fetch_one("SELECT COUNT(*) AS n FROM task_sessions")["n"] == 0
