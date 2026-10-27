"""Phase 9.4.1: timetable data and engine (offline). Fixed dates, a fictional term and fictional courses; the
engine is pure and the store only reads the real clock for created_at / updated_at."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai.schedule import engine
from ai.schedule.model import ScheduleValidationError, WeekRule, parse_weeks

PROJECT = Path(__file__).resolve().parents[1]
TEACHER_A, TEACHER_B, ROOM_A, ROOM_B, ROOM_C, SEAT = "测试老师甲", "测试老师乙", "虚构楼101", "虚构楼202", "虚构楼909", "虚构座位17"
PERIODS = [(1, "08:00", "08:45"), (2, "08:55", "09:40"), (3, "10:00", "10:45"), (4, "10:55", "11:40"),
           (5, "14:00", "14:45"), (6, "14:55", "15:40"), (7, "16:00", "16:45"), (8, "16:55", "17:40")]
SH = timezone(timedelta(hours=8))


def d(text: str) -> date:
    return date.fromisoformat(text)


@pytest.fixture()
def sdb(tmp_path):
    from database import VioletDatabase
    db = VioletDatabase(tmp_path / "schedule.db")
    yield db
    db.close()


@pytest.fixture()
def store(sdb):
    from ai.schedule.store import ScheduleStore
    return ScheduleStore(sdb)


@pytest.fixture()
def term(store):
    """2026 秋（虚构）: week 1 starts Mon 2026-09-07; 高数 Mon p1-2 weeks 1-16 + Wed p3-4 odd weeks; 物理 Tue
    14:00-15:40 weeks 1-8,10-16; 实验 Thu p5-6 even weeks."""
    t = store.create_term(name="2026 秋（虚构）", start_date="2026-09-07", end_date="2027-01-15",
                          timezone="Asia/Shanghai")
    store.set_periods(t, PERIODS)
    math = store.add_course(t, name="高等数学（虚构）", teacher=TEACHER_A)
    phys = store.add_course(t, name="大学物理（虚构）", teacher=TEACHER_B)
    lab = store.add_course(t, name="物理实验（虚构）", kind="lab")
    ids = {"term": t, "math": math, "phys": phys, "lab": lab,
           "math_mon": store.add_slot(math, weekday=1, weeks="1-16", period_start=1, period_end=2, location=ROOM_A),
           "math_wed": store.add_slot(math, weekday=3, weeks="1-16单", period_start=3, period_end=4, location=ROOM_A),
           "phys_tue": store.add_slot(phys, weekday=2, weeks="1-8,10-16", start="14:00", end="15:40", location=ROOM_B),
           "lab_thu": store.add_slot(lab, weekday=4, weeks="2-16双", period_start=5, period_end=6)}
    return ids


def classes(result: dict, occupying_only: bool = True) -> list[str]:
    return [i["course"] for i in result["items"] if i["kind"] == "class" and (i["occupies"] or not occupying_only)]


def item(result: dict, course: str) -> dict:
    return next(i for i in result["items"] if i["course"] == course)


# ----------------------------------------------------------------------------------------------- weeks

@pytest.mark.parametrize("text,weeks", [
    ("1-16", list(range(1, 17))), ("1-16单", list(range(1, 17, 2))), ("第2-16周(双)", list(range(2, 17, 2))),
    ("1-8,10-16", [w for w in range(1, 17) if w != 9]), ("1,3,5,7", [1, 3, 5, 7]), ("2,5,11", [2, 5, 11]),
    ("4", [4]), ("1-16 even", list(range(2, 17, 2))),
])
def test_week_rules_are_canonical(text, weeks):
    rule = parse_weeks(text)
    assert rule.weeks() == weeks and parse_weeks(rule.text()).weeks() == weeks


@pytest.mark.parametrize("text", ["", "abc", "16-1", "0-3", "1-31", "2单", "1--3"])
def test_bad_week_texts_are_refused(text):
    with pytest.raises(ValueError):
        parse_weeks(text)


def test_teaching_weeks_parity_and_skipped_weeks(store, term):
    first = store.day("2026-09-07")                                    # Mon, week 1
    assert first["term"] == {"id": term["term"], "name": "2026 秋（虚构）", "week": 1, "parity": "odd"}
    assert classes(first) == ["高等数学（虚构）"]
    assert classes(store.day("2026-09-09")) == ["高等数学（虚构）"]     # Wed week 1 (odd)
    assert classes(store.day("2026-09-16")) == []                       # Wed week 2 (even): not the odd-week class
    assert classes(store.day("2026-09-17")) == ["物理实验（虚构）"]     # Thu week 2 (even)
    assert classes(store.day("2026-11-03")) == []                       # Tue week 9: skipped
    assert classes(store.day("2026-11-10")) == ["大学物理（虚构）"]     # Tue week 10
    assert store.day("2026-12-28")["term"]["week"] == 17 and classes(store.day("2026-12-28")) == []  # after week 16


def test_periods_become_wall_clock_times(store, term):
    math = item(store.day("2026-09-07"), "高等数学（虚构）")
    assert (math["start"], math["end"]) == ("2026-09-07T08:00:00+08:00", "2026-09-07T09:40:00+08:00")
    wed = item(store.day("2026-09-09"), "高等数学（虚构）")
    assert (wed["start"][11:16], wed["end"][11:16]) == ("10:00", "11:40")
    store.set_periods(term["term"], PERIODS[2:])                       # periods 1-2 gone later: flagged, not guessed
    broken = item(store.day("2026-09-14"), "高等数学（虚构）")
    assert broken["status"] == "invalid_time" and not broken["occupies"] and "第 1 节没有设置时间" in broken["notes"]


# ----------------------------------------------------------------------------------------------- term state

def test_before_after_between_and_without_any_term(sdb, store, term):
    assert store.day("2026-09-01")["data_state"] == "not_started" and store.day("2026-09-01")["items"] == []
    assert store.day("2027-01-20")["data_state"] == "ended"
    store.create_term(name="2027 春（虚构）", start_date="2027-03-01", end_date="2027-06-30", timezone="Asia/Shanghai")
    assert store.day("2027-02-10")["data_state"] == "between_terms"
    from ai.schedule.store import ScheduleStore
    from database import VioletDatabase
    empty = VioletDatabase(Path(sdb.db_path).with_name("empty.db"))
    try:
        assert ScheduleStore(empty).day("2026-09-07")["data_state"] == "no_data"
    finally:
        empty.close()


def test_an_ended_term_never_says_in_class(store, term):
    during = store.status_at(datetime(2026, 9, 7, 8, 30, tzinfo=SH))
    assert during["in_class"] and during["current"][0]["course"] == "高等数学（虚构）"
    after = store.status_at(datetime(2027, 1, 18, 8, 30, tzinfo=SH))     # a Monday after the term ended
    assert after["data_state"] == "ended" and after["in_class"] is False and after["next"] is None
    assert "课表只代表计划" in during["note"] and during["basis"] == "timetable_plan"


def test_no_class_no_data_and_stale_are_different(store, term):
    sunday = store.day("2026-09-13")
    assert sunday["data_state"] == "active" and sunday["items"] == [] and sunday["term"]["week"] == 1
    assert store.day("2027-01-20")["data_state"] == "ended"                # stale: the term is over
    assert store.day("2026-09-13")["last_updated"]


# ----------------------------------------------------------------------------------------------- exceptions

def test_cancel_move_extra_room_and_time(store, term):
    m = term["math"]
    store.add_exception(m, kind="cancel", on_date="2026-09-21", slot_id=term["math_mon"], reason="老师出差（虚构）")
    cancelled = item(store.day("2026-09-21"), "高等数学（虚构）")
    assert cancelled["status"] == "cancelled" and not cancelled["occupies"]
    assert not store.status_at(datetime(2026, 9, 21, 8, 30, tzinfo=SH))["in_class"]

    store.add_exception(m, kind="move", on_date="2026-09-28", slot_id=term["math_mon"], new_date="2026-09-30",
                        new_start="14:00", new_end="15:40")
    away = [i for i in store.day("2026-09-28")["items"] if i["course"] == "高等数学（虚构）"]
    assert [(i["status"], i["occupies"], i["start"][11:16], i["moved_to"]) for i in away] == [
        ("moved_out", False, "08:00", "2026-09-30")]
    wed = [i for i in store.day("2026-09-30")["items"] if i["course"] == "高等数学（虚构）"]
    # 2026-09-30 is Wed of week 4 (even): no regular odd-week class, only the moved one
    assert sorted((i["status"], i["start"][11:16]) for i in wed) == [("moved_in", "14:00")]
    assert not store.status_at(datetime(2026, 9, 28, 8, 30, tzinfo=SH))["in_class"]
    assert store.status_at(datetime(2026, 9, 30, 14, 30, tzinfo=SH))["in_class"]

    store.add_exception(m, kind="extra", on_date="2026-09-26", new_start="09:00", new_end="10:30", new_location=ROOM_C)
    extra = item(store.day("2026-09-26"), "高等数学（虚构）")             # a Saturday
    assert extra["status"] == "extra" and extra["occupies"]

    store.add_exception(m, kind="room", on_date="2026-10-12", slot_id=term["math_mon"], new_location=ROOM_C)
    store.add_exception(term["phys"], kind="time", on_date="2026-10-13", slot_id=term["phys_tue"],
                        new_start="16:00", new_end="17:40")
    room = item(store.day("2026-10-12", include_personal=True), "高等数学（虚构）")
    assert room["status"] == "changed" and room["location"] == ROOM_C and room["start"][11:16] == "08:00"
    timed = item(store.day("2026-10-13"), "大学物理（虚构）")
    assert timed["status"] == "changed" and timed["start"][11:16] == "16:00"


def test_a_move_never_occupies_twice_and_precedence_is_fixed(store, term):
    m, mon = term["math"], term["math_mon"]
    store.add_exception(m, kind="move", on_date="2026-09-14", slot_id=mon, new_start="16:00", new_end="17:40")
    same_day = [i for i in store.day("2026-09-14")["items"] if i["course"] == "高等数学（虚构）"]
    assert [(i["status"], i["start"][11:16]) for i in same_day] == [("changed", "16:00")]     # once, at the new time
    store.add_exception(m, kind="move", on_date="2026-09-07", slot_id=mon, new_date="2026-09-07",  # its own date, named
                        new_start="13:00", new_end="14:00")
    named = [i for i in store.day("2026-09-07")["items"] if i["course"] == "高等数学（虚构）"]
    assert [(i["status"], i["start"][11:16]) for i in named] == [("changed", "13:00")]
    # cancel beats move: nothing lands on the move's target date
    store.add_exception(m, kind="move", on_date="2026-10-19", slot_id=mon, new_date="2026-10-24")
    store.add_exception(m, kind="cancel", on_date="2026-10-19", slot_id=mon)
    assert item(store.day("2026-10-19"), "高等数学（虚构）")["status"] == "cancelled"
    assert classes(store.day("2026-10-24"), occupying_only=False) == []
    # a move that sets the time beats a time change of the same class; the room change still applies
    store.add_exception(m, kind="move", on_date="2026-10-26", slot_id=mon, new_date="2026-10-31", new_start="09:00",
                        new_end="10:00")
    store.add_exception(m, kind="time", on_date="2026-10-26", slot_id=mon, new_start="13:00", new_end="14:00")
    store.add_exception(m, kind="room", on_date="2026-10-26", slot_id=mon, new_location=ROOM_C)
    landed = item(store.day("2026-10-31", include_personal=True), "高等数学（虚构）")
    assert (landed["status"], landed["start"][11:16], landed["location"]) == ("moved_in", "09:00", ROOM_C)
    assert any("另一条改时间的记录不生效" in n for n in landed["notes"])
    with pytest.raises(ScheduleValidationError) as err:                     # one active exception per kind
        store.add_exception(m, kind="room", on_date="2026-10-26", slot_id=mon, new_location=ROOM_B)
    assert "同类的变动" in str(err.value)


def test_an_exception_needs_a_planned_class(store, term):
    with pytest.raises(ScheduleValidationError) as err:
        store.add_exception(term["math"], kind="cancel", on_date="2026-09-16", slot_id=term["math_wed"])  # even week
    assert "那天没有这次课" in str(err.value)
    with pytest.raises(ScheduleValidationError):
        store.add_exception(term["math"], kind="move", on_date="2026-09-14", slot_id=term["math_mon"],
                            new_date="2027-02-01")                        # outside the term
    with pytest.raises(ScheduleValidationError):
        store.add_exception(term["math"], kind="cancel", on_date="2026-09-14", slot_id=term["phys_tue"])  # other course


# ----------------------------------------------------------------------------------------------- holidays

def test_holiday_makeup_day_and_its_teaching_week(store, term):
    t = term["term"]
    store.add_holiday(t, kind="holiday", name="国庆（虚构）", start_date="2026-10-01", end_date="2026-10-07")
    store.add_holiday(t, kind="makeup", name="国庆补课（虚构）", start_date="2026-10-10", follows_date="2026-10-07")
    holiday = store.day("2026-10-05")                                      # Mon week 5 inside the holiday
    assert holiday["day_kind"] == "holiday" and classes(holiday, occupying_only=False) == []
    makeup = store.day("2026-10-10")                                       # Sat runs Wed 2026-10-07 (week 5, odd)
    assert (makeup["day_kind"], makeup["follows_date"], makeup["term"]["week"], makeup["term"]["parity"]) == (
        "makeup", "2026-10-07", 5, "odd")
    assert classes(makeup) == ["高等数学（虚构）"] and item(makeup, "高等数学（虚构）")["start"].startswith("2026-10-10T10:00")
    assert classes(store.day("2026-10-07")) == []                          # the followed date itself stays off
    # an exception of the followed date's class applies on the make-up day
    store.add_exception(term["math"], kind="cancel", on_date="2026-10-07", slot_id=term["math_wed"])
    assert item(store.day("2026-10-10"), "高等数学（虚构）")["status"] == "cancelled"
    # extras and exams still happen on a holiday; a no_class day only stops regular classes
    store.add_exception(term["phys"], kind="extra", on_date="2026-10-03", new_start="09:00", new_end="10:00")
    store.add_exam(t, name="物理小测（虚构）", exam_date="2026-10-03", kind="quiz", start="14:00", end="15:00")
    assert sorted(i["course"] for i in store.day("2026-10-03")["items"]) == ["大学物理（虚构）", "物理小测（虚构）"]
    store.add_holiday(t, kind="no_class", name="运动会（虚构）", start_date="2026-10-20")
    assert store.day("2026-10-20")["day_kind"] == "no_class" and classes(store.day("2026-10-20")) == []


def test_a_makeup_day_in_another_week_reports_the_followed_week(store, term):
    # Sun 2026-09-20 is in week 2 (even) but runs Wed 2026-10-07 (week 5, odd): the odd-week class, week 5
    store.add_holiday(term["term"], kind="makeup", name="提前补课（虚构）", start_date="2026-09-20",
                      follows_date="2026-10-07")
    early = store.day("2026-09-20")
    assert (early["term"]["week"], early["term"]["parity"]) == (5, "odd") and classes(early) == ["高等数学（虚构）"]


def test_a_makeup_day_wins_over_a_holiday_on_the_same_date(store, term):
    t = term["term"]
    store.add_holiday(t, kind="holiday", name="长假（虚构）", start_date="2026-10-01", end_date="2026-10-11")
    store.add_holiday(t, kind="makeup", name="补课（虚构）", start_date="2026-10-10", follows_date="2026-10-05")
    assert classes(store.day("2026-10-10")) == ["高等数学（虚构）"]


def test_makeup_rules_are_enforced(store, term):
    t = term["term"]
    for kwargs, field in ((dict(start_date="2026-10-10", end_date="2026-10-11", follows_date="2026-10-07"), "end_date"),
                          (dict(start_date="2026-10-10", follows_date="2026-10-10"), "follows_date"),
                          (dict(start_date="2026-10-10"), "follows_date"),
                          (dict(start_date="2026-10-10", follows_date="2026-08-01"), "follows_date")):
        with pytest.raises(ScheduleValidationError) as err:
            store.add_holiday(t, kind="makeup", name="补课（虚构）", **kwargs)
        assert field in err.value.problems


# ----------------------------------------------------------------------------------------------- conflicts, exams

def test_overlaps_are_conflicts_and_cancelled_ones_are_not(store, term):
    extra = store.add_course(term["term"], name="选修课（虚构）")
    store.add_slot(extra, weekday=1, weeks="1-16", start="08:30", end="09:00")
    monday = store.day("2026-09-07")
    keys = {i["course"]: i["key"] for i in monday["items"]}
    assert item(monday, "高等数学（虚构）")["conflict_with"] == [keys["选修课（虚构）"]]
    assert item(monday, "选修课（虚构）")["conflict_with"] == [keys["高等数学（虚构）"]]
    store.add_exception(term["math"], kind="cancel", on_date="2026-09-14", slot_id=term["math_mon"])
    assert item(store.day("2026-09-14"), "选修课（虚构）")["conflict_with"] == []


def test_exams_next_to_classes(store, term):
    t = term["term"]
    store.add_exam(t, name="高数期中（虚构）", exam_date="2026-11-02", kind="midterm", course_id=term["math"],
                   start="08:30", end="10:30", location=ROOM_C, seat=SEAT)
    store.add_exam(t, name="英语口试（虚构）", exam_date="2026-11-02")             # time not known yet
    cancelled = store.add_exam(t, name="取消的小测（虚构）", exam_date="2026-11-02", start="16:00", end="16:30")
    store.set_exam_status(cancelled, "cancelled")
    day = store.day("2026-11-02")
    exam, oral, gone = item(day, "高数期中（虚构）"), item(day, "英语口试（虚构）"), item(day, "取消的小测（虚构）")
    assert exam["kind"] == "exam" and exam["occupies"] and exam["conflict_with"] == [item(day, "高等数学（虚构）")["key"]]
    assert oral["all_day"] and oral["conflict_with"] == [] and "考试时间还不知道" in oral["notes"]
    assert gone["status"] == "exam_cancelled" and not gone["occupies"]
    assert "seat" not in exam and "location" not in exam
    assert item(store.day("2026-11-02", include_personal=True), "高数期中（虚构）")["seat"] == SEAT


# ----------------------------------------------------------------------------------------------- time zones

def test_dst_changes_the_utc_moment_not_the_wall_clock(store):
    t = store.create_term(name="Spring（虚构）", start_date="2026-02-23", end_date="2026-05-15",
                          timezone="America/New_York")
    c = store.add_course(t, name="Seminar（虚构）")
    store.add_slot(c, weekday=1, weeks="1-12", start="09:00", end="10:00")
    store.add_slot(c, weekday=7, weeks="1-12", start="02:30", end="04:00")           # inside the spring-forward gap
    before, after = item(store.day("2026-03-02"), "Seminar（虚构）"), item(store.day("2026-03-09"), "Seminar（虚构）")
    assert before["start"] == "2026-03-02T09:00:00-05:00" and after["start"] == "2026-03-09T09:00:00-04:00"
    assert store.status_at(datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc))["in_class"]       # 09:30 EDT
    assert not store.status_at(datetime(2026, 3, 2, 13, 30, tzinfo=timezone.utc))["in_class"]   # 08:30 EST
    gap = next(i for i in store.day("2026-03-08")["items"])
    assert gap["start"] == "2026-03-08T03:30:00-04:00" and any("不存在" in n for n in gap["notes"])


def test_an_ambiguous_fall_back_time_takes_the_first(store):
    t = store.create_term(name="Fall（虚构）", start_date="2026-10-26", end_date="2026-12-18", timezone="America/New_York")
    c = store.add_course(t, name="Night（虚构）")
    store.add_slot(c, weekday=7, weeks="1-8", start="01:30", end="03:00")
    occ = item(store.day("2026-11-01"), "Night（虚构）")
    assert occ["start"] == "2026-11-01T01:30:00-04:00" and any("出现两次" in n for n in occ["notes"])


def test_the_local_date_decides_the_day(store, term):
    status = store.status_at(datetime(2026, 9, 6, 16, 30, tzinfo=timezone.utc))      # 00:30 Mon in Shanghai
    assert status["data_state"] == "active" and status["now"].startswith("2026-09-07T00:30")
    assert status["next"]["course"] == "高等数学（虚构）" and status["remaining_today"] == 1
    tuesday = store.status_at(datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc))       # Mon in UTC, Tue 04:00 here
    assert tuesday["now"].startswith("2026-09-08T04:00") and tuesday["remaining_today"] == 1
    assert tuesday["next"]["course"] == "大学物理（虚构）" and tuesday["next"]["date"] == "2026-09-08"
    late = store.status_at(datetime(2026, 9, 12, 20, 0, tzinfo=SH))                   # Saturday night
    assert late["next"]["date"] == "2026-09-14"                                        # looks ahead to Monday


def test_the_engine_never_reads_the_clock():
    source = (PROJECT / "ai" / "schedule" / "engine.py").read_text(encoding="utf-8")
    assert not re.search(r"\.now\(|\.today\(|time\.time\(|utcnow", source)


# ----------------------------------------------------------------------------------------------- migration

def test_an_older_database_gets_the_tables_once_and_keeps_its_data(tmp_path):
    from database import VioletDatabase
    from ai.schedule.schema import TABLES, present_tables
    path = tmp_path / "old.db"
    db = VioletDatabase(path)
    db.execute("INSERT INTO messages (role, direction, message_type, content, created_at) VALUES "
               "('user', 'incoming', 'text', '旧消息（虚构）', '2026-09-01T10:00:00+08:00')")
    with db.transaction():
        for table in TABLES:                                        # back to a pre-9.4.1 database
            db.execute(f"DROP TABLE {table}")
    db.close()
    raw = sqlite3.connect(path)
    assert not [r for r in raw.execute("SELECT name FROM sqlite_master WHERE name LIKE 'schedule_%'")]
    raw.close()
    db = VioletDatabase(path)
    assert present_tables(db) == sorted(TABLES)
    assert db.fetch_one("SELECT content FROM messages")["content"] == "旧消息（虚构）"
    assert db.fetch_one("PRAGMA foreign_keys")["foreign_keys"] == 1
    from ai.schedule.store import ScheduleStore
    ScheduleStore(db).create_term(name="T（虚构）", start_date="2026-09-07", end_date="2026-12-31", timezone="Asia/Shanghai")
    schema_before = db.fetch_all("SELECT type, name, sql FROM sqlite_master ORDER BY name")
    db.close()
    for _ in range(2):                                             # restarts change nothing
        db = VioletDatabase(path)
        assert db.fetch_all("SELECT type, name, sql FROM sqlite_master ORDER BY name") == schema_before
        assert db.fetch_one("SELECT COUNT(*) AS n FROM schedule_terms")["n"] == 1
        assert db.fetch_one("SELECT COUNT(*) AS n FROM schedule_revisions")["n"] == 1
        assert db.fetch_one("PRAGMA foreign_key_check") is None
        db.close()


def test_the_sql_mirror_matches_and_the_down_script_drops_nothing():
    from ai.schedule.schema import SCHEMA_SQL
    up = (PROJECT / "migrations" / "phase94_up.sql").read_text(encoding="utf-8")
    assert SCHEMA_SQL.strip() in up
    down = (PROJECT / "migrations" / "phase94_down.sql").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(DROP|DELETE|ALTER)\b", down, re.I | re.M)
    conn = sqlite3.connect(":memory:")
    conn.executescript(up)
    conn.executescript(up)
    conn.executescript(down)
    assert len([r for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'schedule_%'")]) == 9


def test_the_rollback_refuses_with_data_and_backs_up_with_confirm(tmp_path, sdb, store, term):
    from ai.schedule.schema import RollbackRefused, drop_schedule_tables, present_tables
    sdb.execute("INSERT INTO messages (role, direction, message_type, content, created_at) VALUES "
                "('user', 'incoming', 'text', 'x', '2026-09-01T10:00:00+08:00')")
    with pytest.raises(RollbackRefused):
        drop_schedule_tables(sdb)
    assert present_tables(sdb)
    done = drop_schedule_tables(sdb, confirm=True, backup_dir=tmp_path / "bk")
    backup = json.loads(Path(done["backup"]).read_text(encoding="utf-8"))
    assert [c["name"] for c in backup["schedule_courses"]] == ["高等数学（虚构）", "大学物理（虚构）", "物理实验（虚构）"]
    assert present_tables(sdb) == [] and sdb.fetch_one("SELECT COUNT(*) AS n FROM messages")["n"] == 1
    assert drop_schedule_tables(sdb)["rows"] == 0                    # nothing left: an empty drop is fine


def test_the_rollback_tool(tmp_path, sdb, store, term):
    from tools import schedule_rollback
    path = sdb.db_path
    sdb.close()
    assert schedule_rollback.main(["--db", str(path)]) == 1
    assert schedule_rollback.main(["--db", str(path), "--confirm", "--backup-dir", str(tmp_path / "bk")]) == 0
    assert list((tmp_path / "bk").glob("schedule-backup-*.json"))
    from database import VioletDatabase
    reopened = VioletDatabase(path)                                    # the next start recreates empty tables
    try:
        assert reopened.fetch_one("SELECT COUNT(*) AS n FROM schedule_terms")["n"] == 0
    finally:
        reopened.close()


def test_a_failing_import_writes_nothing(store, sdb, monkeypatch):
    import_id = store.record_import(file_name="课表.json", sha256="a" * 64, fmt="json", actor="test")
    calls = {"n": 0}
    original = store._revision

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("disk full (simulated)")
        return original(*a, **kw)
    monkeypatch.setattr(store, "_revision", flaky)
    with pytest.raises(RuntimeError):
        store.apply_import(import_id, normalized())
    monkeypatch.setattr(store, "_revision", original)
    assert sdb.fetch_one("SELECT COUNT(*) AS n FROM schedule_terms")["n"] == 0
    assert sdb.fetch_one("SELECT COUNT(*) AS n FROM schedule_courses")["n"] == 0
    assert sdb.fetch_one("SELECT status FROM schedule_imports WHERE id = ?", (import_id,))["status"] == "failed"


def test_a_failing_write_leaves_no_row(store, sdb, monkeypatch):
    monkeypatch.setattr(store, "_revision", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        store.create_term(name="X（虚构）", start_date="2026-09-07", end_date="2026-12-31", timezone="Asia/Shanghai")
    assert sdb.fetch_one("SELECT COUNT(*) AS n FROM schedule_terms")["n"] == 0


# ----------------------------------------------------------------------------------------------- constraints

def test_the_database_itself_refuses_bad_rows(sdb, store, term):
    now = "2026-09-01T00:00:00+08:00"
    bad = [
        ("INSERT INTO schedule_slots (course_id, weekday, start_time, end_time, period_start, period_end, week_from, "
         "week_to, source, created_at, updated_at) VALUES (?, 1, '08:00', '09:00', 1, 2, 1, 16, 'manual', ?, ?)",
         (term["math"], now, now)),                                          # both times and periods
        ("INSERT INTO schedule_slots (course_id, weekday, start_time, end_time, week_from, week_to, source, created_at, "
         "updated_at) VALUES (99999, 1, '08:00', '09:00', 1, 16, 'manual', ?, ?)", (now, now)),   # no such course
        ("INSERT INTO schedule_slots (course_id, weekday, start_time, end_time, week_from, week_to, source, created_at, "
         "updated_at) VALUES (?, 8, '08:00', '09:00', 1, 16, 'manual', ?, ?)", (term["math"], now, now)),  # weekday 8
        ("INSERT INTO schedule_exceptions (term_id, course_id, slot_id, kind, on_date, new_start_time, new_end_time, "
         "source, created_at, updated_at) VALUES (?, ?, ?, 'extra', '2026-09-07', '09:00', '10:00', 'manual', ?, ?)",
         (term["term"], term["math"], term["math_mon"], now, now)),          # an extra tied to a slot
        ("INSERT INTO schedule_exceptions (term_id, course_id, slot_id, kind, on_date, new_location, source, created_at, "
         "updated_at) VALUES (?, ?, ?, 'cancel', '2026-09-07', 'x', 'manual', ?, ?)",
         (term["term"], term["math"], term["math_mon"], now, now)),          # cancel with a new place
        ("INSERT INTO schedule_terms (name, start_date, end_date, week1_monday, timezone, source, created_at, updated_at) "
         "VALUES ('坏（虚构）', '2026-09-07', '2026-09-01', '2026-09-07', 'Asia/Shanghai', 'manual', ?, ?)", (now, now)),
        ("INSERT INTO schedule_holidays (term_id, kind, name, start_date, end_date, source, created_at, updated_at) "
         "VALUES (?, 'makeup', '补（虚构）', '2026-10-10', '2026-10-10', 'manual', ?, ?)", (term["term"], now, now)),
        ("INSERT INTO schedule_periods (term_id, period_no, start_time, end_time, created_at, updated_at) VALUES "
         "(?, 1, '09:00', '08:00', ?, ?)", (term["term"], now, now)),
    ]
    for sql, args in bad:
        with pytest.raises(sqlite3.IntegrityError):
            sdb.execute(sql, args)
    # partial unique index: one active exception of a kind per occurrence
    sdb.execute("INSERT INTO schedule_exceptions (term_id, course_id, slot_id, kind, on_date, source, created_at, "
                "updated_at) VALUES (?, ?, ?, 'cancel', '2026-09-07', 'manual', ?, ?)",
                (term["term"], term["math"], term["math_mon"], now, now))
    with pytest.raises(sqlite3.IntegrityError):
        sdb.execute("INSERT INTO schedule_exceptions (term_id, course_id, slot_id, kind, on_date, source, created_at, "
                    "updated_at) VALUES (?, ?, ?, 'cancel', '2026-09-07', 'manual', ?, ?)",
                    (term["term"], term["math"], term["math_mon"], now, now))


@pytest.mark.parametrize("kwargs,field", [
    (dict(start_date="2026-09-08", end_date="2027-01-15", week1_monday="2026-09-08"), "week1_monday"),   # a Tuesday
    (dict(start_date="2026-09-07", end_date="2026-09-01"), "end_date"),
    (dict(start_date="2026-09-07", end_date="2028-01-01"), "end_date"),
    (dict(start_date="2026-09-07", end_date="2027-01-15", timezone="Mars/Olympus"), "timezone"),
    (dict(start_date="2026/09/07", end_date="2027-01-15"), "start_date"),
    (dict(start_date="2026-02-30", end_date="2027-01-15"), "start_date"),
])
def test_term_validation(store, kwargs, field):
    args = {"name": "坏学期（虚构）", "timezone": "Asia/Shanghai", **kwargs}
    with pytest.raises(ScheduleValidationError) as err:
        store.create_term(**args)
    assert field in err.value.problems


def test_overlapping_terms_and_bad_meetings_are_refused(store, term):
    with pytest.raises(ScheduleValidationError):
        store.create_term(name="重叠（虚构）", start_date="2026-12-01", end_date="2027-02-01", timezone="Asia/Shanghai")
    for kwargs in (dict(weekday=1, weeks="17-20", start="08:00", end="09:00"),        # beyond the term's weeks
                   dict(weekday=1, weeks="1-16", start="9:00", end="8:00"),
                   dict(weekday=1, weeks="1-16", start="08:00", end="09:00", period_start=1, period_end=2),
                   dict(weekday=1, weeks="1-16", period_start=9, period_end=10),       # periods not set
                   dict(weekday=0, weeks="1-16", start="08:00", end="09:00"),
                   dict(weekday=1, weeks="1-16", start="25:00", end="26:00"),
                   dict(weekday=1, weeks="1-16", start="08:00", end="09:00", location="教\n室")):
        with pytest.raises(ScheduleValidationError):
            store.add_slot(term["math"], **kwargs)
    with pytest.raises(ScheduleValidationError) as err:
        store.add_course(term["term"], name="x" * 61)
    assert "x" * 61 not in str(err.value)
    with pytest.raises(ScheduleValidationError):
        store.add_exam(term["term"], name="期末（虚构）", exam_date="2027-02-01")       # outside the term
    with pytest.raises(ScheduleValidationError):
        store.add_exam(term["term"], name="期末（虚构）", exam_date="2027-01-10", start="10:00")   # half a time


def test_validation_messages_never_repeat_personal_values(store, term):
    secret = "私人地点\x07测试"
    with pytest.raises(ScheduleValidationError) as err:
        store.add_slot(term["math"], weekday=1, weeks="1-16", start="08:00", end="09:00", location=secret)
    assert "私人地点" not in str(err.value) and "location" in err.value.problems


# ----------------------------------------------------------------------------------------------- import precedence

def normalized(teacher: str = TEACHER_A, extra_course: bool = True, exam_seat: str = SEAT) -> dict:
    courses = [{"key": "MATH101", "name": "高等数学（虚构）", "teacher": teacher, "slots": [
        {"key": "MATH101-1", "weekday": 1, "weeks": "1-16", "period_start": 1, "period_end": 2, "location": ROOM_A}]}]
    if extra_course:
        courses.append({"key": "ART1", "name": "艺术欣赏（虚构）", "slots": [
            {"key": "ART1-1", "weekday": 5, "weeks": "1-8", "start": "19:00", "end": "20:30"}]})
    return {"term": {"name": "导入学期（虚构）", "start_date": "2026-09-07", "end_date": "2027-01-15",
                     "timezone": "Asia/Shanghai"},
            "periods": PERIODS, "courses": courses,
            "exams": [{"key": "E1", "name": "高数期末（虚构）", "exam_date": "2027-01-11", "kind": "final", "start": "09:00",
                       "end": "11:00", "seat": exam_seat}],
            "holidays": [{"key": "H1", "kind": "holiday", "name": "元旦（虚构）", "start_date": "2027-01-01"}]}


def test_manual_changes_win_over_imports(store, sdb):
    first = store.record_import(file_name="课表.json", sha256="b" * 64, fmt="json", actor="test")
    report = store.apply_import(first, normalized())
    assert report["created"] == 6 and report["deleted"] == 0           # 2 courses, 2 meetings, 1 exam, 1 holiday
    math = sdb.fetch_one("SELECT * FROM schedule_courses WHERE import_key = 'MATH101'")
    assert math["source"] == "import" and math["manual_override"] == 0
    store.update_course(math["id"], {"teacher": "手动改过（虚构）"})                        # the user's own edit
    assert sdb.fetch_one("SELECT manual_override FROM schedule_courses WHERE id = ?", (math["id"],))["manual_override"] == 1
    again = store.record_import(file_name="课表.json", sha256="c" * 64, fmt="json", actor="test")
    report = store.apply_import(again, normalized(teacher="导入的新老师（虚构）", extra_course=False))
    assert sdb.fetch_one("SELECT teacher FROM schedule_courses WHERE id = ?", (math["id"],))["teacher"] == "手动改过（虚构）"
    art = sdb.fetch_one("SELECT status FROM schedule_courses WHERE import_key = 'ART1'")
    assert art["status"] == "deleted" and report["deleted"] >= 1 and report["skipped_manual"] == 1
    skips = [r for r in store.revisions("course", math["id"]) if r["action"] == "import_skip"]
    assert len(skips) == 1
    # a course the user deleted by hand is not brought back by the next import
    store.delete_course(math["id"])
    third = store.record_import(file_name="课表.json", sha256="d" * 64, fmt="json", actor="test")
    store.apply_import(third, normalized())
    assert sdb.fetch_one("SELECT status FROM schedule_courses WHERE id = ?", (math["id"],))["status"] == "deleted"
    with pytest.raises(ScheduleValidationError):
        store.apply_import(third, normalized())                                       # applied once only


def test_the_same_import_again_changes_nothing(store):
    store.apply_import(store.record_import(file_name="a.json", sha256="e" * 64, fmt="json", actor="t"), normalized())
    report = store.apply_import(store.record_import(file_name="a.json", sha256="e" * 64, fmt="json", actor="t"),
                                normalized())
    assert report["created"] == report["updated"] == report["deleted"] == 0 and report["unchanged"] == 6


def test_every_change_has_a_revision(store, term):
    store.update_course(term["math"], {"code": "M-1"})
    store.delete_slot(term["lab_thu"])
    actions = [(r["entity"], r["action"]) for r in store.revisions(limit=100)]
    assert ("course", "update") in actions and ("slot", "delete") in actions and ("period", "update") in actions
    deleted = next(r for r in store.revisions("slot", term["lab_thu"]) if r["action"] == "delete")
    assert json.loads(deleted["before_json"])["status"] == "active" and json.loads(deleted["after_json"])["status"] == "deleted"


def test_purge_deletes_everything_only_when_confirmed(store, sdb, term):
    with pytest.raises(ScheduleValidationError):
        store.purge_all(confirm=False)
    store.purge_all(confirm=True)
    counts = __import__("ai.schedule.schema", fromlist=["x"]).row_counts(sdb)
    # phase 9.4.3: only the purge note is left (scope and row counts -- no names, no personal values)
    assert counts.pop("schedule_revisions") == 1 and all(v == 0 for v in counts.values())
    note = sdb.fetch_one("SELECT * FROM schedule_revisions")
    assert note["action"] == "delete" and note["after_json"] is None
    assert set(json.loads(note["before_json"])) == {"purged", "scope", "rows"}


# ----------------------------------------------------------------------------------------------- privacy

def test_personal_fields_only_when_asked(store, term):
    plain = store.day("2026-09-07")
    assert all(k not in i for i in plain["items"] for k in ("teacher", "location", "seat"))
    full = item(store.day("2026-09-07", include_personal=True), "高等数学（虚构）")
    assert (full["teacher"], full["location"]) == (TEACHER_A, ROOM_A)
    status = store.status_at(datetime(2026, 9, 7, 8, 30, tzinfo=SH))
    assert "location" not in status["current"][0] and "teacher" not in status["current"][0]


def test_personal_values_never_reach_ordinary_logs(store, sdb, caplog):
    caplog.set_level(logging.DEBUG)
    t = store.create_term(name="日志（虚构）", start_date="2027-02-22", end_date="2027-06-30", timezone="Asia/Shanghai")
    store.set_periods(t, PERIODS)
    c = store.add_course(t, name="日志课（虚构）", teacher=TEACHER_B, notes="私人备注（虚构）")
    s = store.add_slot(c, weekday=1, weeks="1-16", start="08:00", end="09:00", location=ROOM_B)
    store.update_slot(s, {"location": ROOM_C})
    store.add_exception(c, kind="room", on_date="2027-03-01", slot_id=s, new_location=ROOM_A, reason="私人原因（虚构）")
    store.add_exam(t, name="日志考（虚构）", exam_date="2027-03-03", seat=SEAT, location=ROOM_C)
    store.apply_import(store.record_import(file_name="x.json", sha256="f" * 64, fmt="json", actor="t"), normalized())
    with pytest.raises(ScheduleValidationError):
        store.add_slot(c, weekday=1, weeks="1-16", start="08:00", end="09:00", location=ROOM_A + "\n二楼")
    store.day("2027-03-01", include_personal=True)
    text = caplog.text
    for value in (TEACHER_A, TEACHER_B, ROOM_A, ROOM_B, ROOM_C, SEAT, "私人备注", "私人原因"):
        assert value not in text, value
    assert "[schedule]" in text                                                       # it did log (ids only)


def test_timetable_personal_values_are_kept_out_of_web_searches(sdb, store, term):
    from ai.web.providers import MockSearchProvider
    from ai.web.service import WebConfig, WebService
    svc = WebService(sdb, provider=MockSearchProvider(), config=WebConfig(enabled=True))
    assert svc.search(f"{TEACHER_A} 评价")["error_code"] == "blocked_private_data"
    assert svc.search(f"{ROOM_B} 在哪")["error_code"] == "blocked_private_data"
    assert svc.search("高等数学 学习方法")["ok"]                               # a course name is not personal
