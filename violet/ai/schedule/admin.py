"""Phase 9.4.3: what the Control Center timetable page shows (read side) and the CSV / JSON export.

Everything is computed from ScheduleStore.load() / its tables with the same engine the violet-schedule tools use, so
the week view and Violet's answers can never disagree. The page belongs to the single administrator, so it shows the
personal fields (teacher, location, seat, notes); nothing here logs them, and they never leave through any other
path. An archived term is shown as it would run (its status is lifted for the view only)."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from ai.schedule import engine
from ai.schedule.importer import FORMULA_START, slot_key
from ai.schedule.model import ScheduleData, TermData, WeekRule

KIND_NAMES = {"lecture": "理论", "lab": "实验", "seminar": "研讨", "pe": "体育", "other": "其他"}
CSV_EXPORT_COLUMNS = ("课程", "课程编号", "课程代码", "类型", "老师", "星期", "节次", "开始", "结束", "周次", "地点", "备注",
                      "编号")
PERSONAL_EXPORT = ("teacher", "notes", "location", "new_location", "seat", "reason")


def _term_data(data: ScheduleData, term_id: int) -> TermData | None:
    td = next((t for t in data.terms if t.term.id == int(term_id)), None)
    if td is None:
        return None
    return replace(td, term=replace(td.term, status="active"))          # archived terms are viewable too


def weeks_total(td: TermData) -> int:
    return (td.term.end_date - td.term.week1_monday).days // 7 + 1


def term_state(term: dict[str, Any], today: date) -> dict[str, Any]:
    start, end = date.fromisoformat(term["start_date"]), date.fromisoformat(term["end_date"])
    monday = date.fromisoformat(term["week1_monday"])
    total = (end - monday).days // 7 + 1
    if term["status"] == "archived":
        state = "archived"
    elif today < start:
        state = "not_started"
    elif today > end:
        state = "ended"
    else:
        state = "active"
    week = (today - monday).days // 7 + 1 if start <= today <= end else None
    return {"state": state, "weeks_total": total, "current_week": week}


def week_view(data: ScheduleData, term_id: int, week: int) -> dict[str, Any] | None:
    td = _term_data(data, term_id)
    if td is None:
        return None
    total = weeks_total(td)
    week = max(1, min(int(week), total))
    monday = td.term.week1_monday + timedelta(days=7 * (week - 1))
    only = ScheduleData((td,), data.last_updated)
    days = []
    for i in range(7):
        result = engine.day(only, monday + timedelta(days=i))
        view = result.to_dict(include_personal=True)
        # what the page acts on when a class is clicked (not part of the engine's / Violet's output)
        for item, occ in zip(view["items"], result.items):
            item.update(slot_id=occ.slot_id, exception_ids=list(occ.exception_ids), exam_id=occ.exam_id)
        days.append(view)
    return {"term_id": td.term.id, "week": week, "weeks_total": total, "parity": "odd" if week % 2 else "even",
            "monday": monday.isoformat(), "days": days}


def term_conflicts(data: ScheduleData, term_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Overlapping occupying items over the whole term, grouped by (the two courses, weekday, their times)."""
    td = _term_data(data, term_id)
    if td is None:
        return []
    only = ScheduleData((td,), data.last_updated)
    groups: dict[tuple, dict[str, Any]] = {}
    day = td.term.start_date
    while day <= td.term.end_date:
        result = engine.day(only, day)
        by_key = {i.key: i for i in result.items}
        for first in result.items:
            for other in first.conflict_with:
                b = by_key.get(other)
                if b is None:
                    continue
                # each pair once; the one that starts first (then by name) is "a" -- never by id text order
                a, b = sorted((first, b), key=lambda o: (o.start, o.course, o.key))
                if a is not first:
                    continue
                times = (f"{a.start:%H:%M}-{a.end:%H:%M}", f"{b.start:%H:%M}-{b.end:%H:%M}")
                group_key = (a.course, b.course, day.isoweekday(), *times)
                entry = groups.setdefault(group_key, {"a": a.course, "b": b.course, "weekday": day.isoweekday(),
                                                      "a_time": times[0], "b_time": times[1],
                                                      "first_date": day.isoformat(), "count": 0, "dates": []})
                entry["count"] += 1
                if len(entry["dates"]) < 5:
                    entry["dates"].append(day.isoformat())
        day += timedelta(days=1)
    return sorted(groups.values(), key=lambda g: (g["first_date"], g["a"]))[:limit]


def term_detail(store, term_id: int) -> dict[str, Any] | None:
    db = store.db
    term = db.fetch_one("SELECT * FROM schedule_terms WHERE id = ?", (int(term_id),))
    if term is None:
        return None
    t = int(term_id)
    courses = db.fetch_all("SELECT * FROM schedule_courses WHERE term_id = ? ORDER BY status, name, id", (t,))
    slots = db.fetch_all("SELECT s.* FROM schedule_slots s JOIN schedule_courses c ON c.id = s.course_id "
                         "WHERE c.term_id = ? ORDER BY s.weekday, COALESCE(s.period_start, 99), s.start_time, s.id", (t,))
    for c in courses:
        c["slots"] = [s for s in slots if s["course_id"] == c["id"]]
        for s in c["slots"]:
            s["skip_weeks"] = json.loads(s["skip_weeks"] or "[]")
    return {"term": term,
            "periods": db.fetch_all("SELECT period_no, start_time, end_time FROM schedule_periods WHERE term_id = ? "
                                    "ORDER BY period_no", (t,)),
            "courses": courses,
            "exceptions": db.fetch_all("SELECT e.*, c.name AS course_name FROM schedule_exceptions e JOIN "
                                       "schedule_courses c ON c.id = e.course_id WHERE e.term_id = ? "
                                       "ORDER BY e.status, e.on_date, e.id LIMIT 500", (t,)),
            "exams": db.fetch_all("SELECT * FROM schedule_exams WHERE term_id = ? AND status <> 'deleted' "
                                  "ORDER BY exam_date, start_time, id", (t,)),
            "holidays": db.fetch_all("SELECT * FROM schedule_holidays WHERE term_id = ? AND status = 'active' "
                                     "ORDER BY start_date, id", (t,)),
            "imports": db.fetch_all("SELECT id, file_name, format, status, summary, error_count, actor, created_at, "
                                    "applied_at FROM schedule_imports WHERE term_id = ? ORDER BY id DESC LIMIT 20", (t,))}


# ----------------------------------------------------------------------------------------------- export

def _slot_export_key(slot: dict[str, Any]) -> str:
    if slot.get("import_key"):
        return slot["import_key"]
    periods = (slot["period_start"], slot["period_end"]) if slot["period_start"] else None
    return slot_key(slot["weekday"], slot["start_time"], slot["end_time"], periods, slot["weeks_text"] or "")


def _course_export_key(course: dict[str, Any]) -> str:
    from ai.schedule.importer import course_key
    return course["import_key"] or course_key(course["name"], course["code"])


def export_document(store, term_id: int, *, include_personal: bool = True) -> dict[str, Any] | None:
    """One term in the import JSON format (re-importing it changes nothing). Active rows only; never revisions,
    import records or other terms."""
    detail = term_detail(store, term_id)
    if detail is None:
        return None
    term = detail["term"]
    courses, keys, slot_keys = [], {}, {}
    for c in detail["courses"]:
        if c["status"] != "active":
            continue
        keys[c["id"]] = _course_export_key(c)
        slots = []
        for s in c["slots"]:
            if s["status"] != "active":
                continue
            slot_keys[s["id"]] = _slot_export_key(s)
            entry = {"key": slot_keys[s["id"]], "weekday": s["weekday"],
                     "weeks": s["weeks_text"] or WeekRule(s["week_from"], s["week_to"], s["parity"],
                                                          frozenset(s["skip_weeks"])).text(),
                     "location": s["location"], "valid_from": s["valid_from"], "valid_until": s["valid_until"]}
            if s["period_start"]:
                entry["periods"] = f"{s['period_start']}-{s['period_end']}"
            else:
                entry.update(start=s["start_time"], end=s["end_time"])
            slots.append(entry)
        courses.append({"key": keys[c["id"]], "name": c["name"], "code": c["code"], "kind": c["kind"],
                        "teacher": c["teacher"], "notes": c["notes"], "slots": slots})
    exceptions = []
    for e in detail["exceptions"]:
        if e["status"] != "active" or e["course_id"] not in keys or (e["slot_id"] and e["slot_id"] not in slot_keys):
            continue
        entry = {"key": e["import_key"] or f"{e['kind']}|{keys[e['course_id']]}|{slot_keys.get(e['slot_id'], '')}|"
                                          f"{e['on_date']}"[:120],
                 "course": keys[e["course_id"]], "slot": slot_keys.get(e["slot_id"]), "kind": e["kind"],
                 "date": e["on_date"], "new_date": e["new_date"], "new_start": e["new_start_time"],
                 "new_end": e["new_end_time"], "new_location": e["new_location"], "reason": e["reason"]}
        if e["new_period_start"]:
            entry["new_periods"] = f"{e['new_period_start']}-{e['new_period_end']}"
        exceptions.append(entry)
    from ai.schedule.store import name_key
    exams = [{"key": x["import_key"] or f"{name_key(x['name'])}|{x['exam_date']}"[:120], "name": x["name"],
              "kind": x["kind"], "date": x["exam_date"], "start": x["start_time"], "end": x["end_time"],
              "course": keys.get(x["course_id"]), "location": x["location"], "seat": x["seat"], "notes": x["notes"]}
             for x in detail["exams"] if x["status"] == "active"]
    holidays = [{"key": h["import_key"] or f"{h['kind']}|{h['start_date']}", "kind": h["kind"], "name": h["name"],
                 "start_date": h["start_date"], "end_date": h["end_date"], "follows_date": h["follows_date"]}
                for h in detail["holidays"]]
    doc = {"format": "violet-schedule", "version": 1,
           "term": {k: term[k] for k in ("name", "start_date", "end_date", "week1_monday", "timezone")},
           "periods": [{"no": p["period_no"], "start": p["start_time"], "end": p["end_time"]} for p in detail["periods"]],
           "courses": courses, "exceptions": exceptions, "exams": exams, "holidays": holidays}
    return _strip(doc, include_personal)


def _strip(value: Any, include_personal: bool) -> Any:
    """Drop empty values and, unless asked for, the personal fields."""
    if isinstance(value, dict):
        return {k: _strip(v, include_personal) for k, v in value.items()
                if v not in (None, "") and (include_personal or k not in PERSONAL_EXPORT)}
    if isinstance(value, list):
        return [_strip(v, include_personal) for v in value]
    return value


def formula_safe(value: Any) -> str:
    """A CSV cell a spreadsheet will not run: text starting with = + - @ (or a tab / CR) gets a leading '."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def export_csv(store, term_id: int, *, include_personal: bool = True) -> str | None:
    """The term's meetings in the CSV import format (one row per meeting), every cell formula-safe, UTF-8 BOM."""
    doc = export_document(store, term_id, include_personal=include_personal)
    if doc is None:
        return None
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_EXPORT_COLUMNS)
    for c in doc["courses"]:
        for s in c.get("slots") or []:
            start, end = (s.get("start"), s.get("end")) if "periods" not in s else ("", "")
            writer.writerow([formula_safe(v) for v in (
                c["name"], c["key"], c.get("code"), KIND_NAMES.get(c.get("kind"), c.get("kind")), c.get("teacher"),
                s["weekday"], s.get("periods"), start, end, s["weeks"], s.get("location"), c.get("notes"), s["key"])])
    return "﻿" + out.getvalue()
