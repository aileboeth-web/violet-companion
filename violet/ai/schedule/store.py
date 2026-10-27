"""Phase 9.4.1: the one data-access layer for the timetable (reused by the violet-schedule MCP server and the
Control Center later).

* Every write validates first (ScheduleValidationError, field by field; messages never repeat personal values),
  then runs in ONE transaction together with its revision row (before / after JSON). A failure leaves nothing.
* Source precedence: a manual change to an imported row marks it manual_override; apply_import never changes,
  re-creates or deletes an overridden row (it records an import_skip revision instead).
* Queries load a ScheduleData snapshot and hand it to the pure engine; teacher / location / seat / notes are
  left out unless include_personal=True (only the user's own direct question needs them).
* Logs carry ids, kinds and counts only -- never teacher, location, seat, notes or reasons.

Phase 9.4.3 (Control Center): apply_import also lists what it did item by item (the import preview runs it inside a
transaction that is rolled back -- quiet(), nothing is logged as done); a course with the same name as a MANUAL course
of the term is never imported beside it; data_version() fingerprints the timetable content (a preview is confirmed
only against the version it was computed on); purge_course / purge_term / purge_all delete the rows AND the
revisions of everything in that scope, leaving one metadata revision (counts only, no names or personal values).
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import re
import sqlite3
import types
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from ai.schedule import engine
from ai.schedule.model import (COURSE_KINDS, EXAM_KINDS, EXCEPTION_KINDS, HOLIDAY_KINDS, MAX_TERM_DAYS, MAX_WEEK,
                               SOURCES, ClassException, Course, Exam, Holiday, Period, ScheduleData,
                               ScheduleValidationError, Slot, Term, TermData, WeekRule, parse_date, parse_time,
                               parse_weeks, zone_of)

logger = logging.getLogger("violet.schedule")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_QUIET: contextvars.ContextVar[bool] = contextvars.ContextVar("violet_schedule_quiet", default=False)
CONTENT_TABLES = ("schedule_terms", "schedule_periods", "schedule_courses", "schedule_slots", "schedule_exceptions",
                  "schedule_exams", "schedule_holidays")
PURGE_CONFIRM_ALL = "删除全部课表"


class _QuietFilter(logging.Filter):
    """Drops this logger's records while a dry run (import preview) is in progress in this context."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _QUIET.get()


logger.addFilter(_QuietFilter())


@contextlib.contextmanager
def quiet():
    token = _QUIET.set(True)
    try:
        yield
    finally:
        _QUIET.reset(token)


def name_key(name: Any) -> str:
    """How two course names are compared (NFKC, no spaces, case-folded): "高等数学 （虚构）" == "高等数学(虚构)"."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(name or ""))).casefold()


def _text(problems: dict[str, str], name: str, value: Any, limit: int, required: bool = False) -> str | None:
    text = None if value is None else str(value).strip()
    if not text:
        if required:
            problems[name] = "不能为空"
        return None
    if _CONTROL.search(text) or "\n" in text:
        problems[name] = "不能包含换行或控制字符"
    elif len(text) > limit:
        problems[name] = f"不能超过 {limit} 个字"
    return text


def _date(problems: dict[str, str], name: str, value: Any, required: bool = True) -> date | None:
    if value is None or value == "":
        if required:
            problems[name] = "不能为空"
        return None
    try:
        return parse_date(value)
    except ValueError as exc:
        problems[name] = str(exc)
        return None


def _time(problems: dict[str, str], name: str, value: Any) -> time | None:
    if value is None or value == "":
        return None
    try:
        return parse_time(value)
    except ValueError as exc:
        problems[name] = str(exc)
        return None


def _choice(problems: dict[str, str], name: str, value: Any, allowed: Iterable[str]) -> str:
    allowed = tuple(allowed)
    if value not in allowed:
        problems[name] = f"只能是 {' / '.join(allowed)}"
    return str(value)


def _int(problems: dict[str, str], name: str, value: Any, lo: int, hi: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        problems[name] = "要是整数"
        return None
    if not lo <= number <= hi:
        problems[name] = f"要在 {lo}–{hi} 之间"
    return number


def _hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value is not None else None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


class ScheduleStore:
    def __init__(self, db):
        self.db = db
        self.last_changed: list[str] = []          # fields the last _update changed (import preview details)
        self._collect: list[dict[str, str]] | None = None      # problems gathered by an import dry run
        self._items: list[dict[str, Any]] = []

    # ------------------------------------------------------------------------------------------- plumbing
    def _now(self) -> str:
        return self.db.now()

    def _row(self, table: str, row_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))

    def _require(self, table: str, row_id: Any, what: str) -> dict[str, Any]:
        row = self._row(table, int(row_id)) if str(row_id).isdigit() else None
        if row is None:
            raise ScheduleValidationError({what: "不存在"})
        return row

    def _revision(self, entity: str, entity_id: int, action: str, before: dict | None, after: dict | None,
                  actor: str, source: str, import_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO schedule_revisions (entity, entity_id, action, before_json, after_json, actor, source, "
            "import_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entity, entity_id, action, json.dumps(before, ensure_ascii=False) if before else None,
             json.dumps(after, ensure_ascii=False) if after else None, actor, source, import_id, self._now()))

    def _insert(self, table: str, entity: str, values: dict[str, Any], actor: str, source: str,
                import_id: int | None = None) -> int:
        now = self._now()
        values = {**values, "created_at": now, "updated_at": now}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.db.transaction():
            cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
            row_id = int(cur.lastrowid)
            self._revision(entity, row_id, "create", None, self._row(table, row_id), actor, source, import_id)
        logger.info("[schedule] %s %s created (%s)", entity, row_id, source)
        return row_id

    def _update(self, table: str, entity: str, row_id: int, changes: dict[str, Any], actor: str, source: str,
                action: str = "update", import_id: int | None = None) -> bool:
        before = self._row(table, row_id)
        if before is None:
            raise ScheduleValidationError({entity: "不存在"})
        if source == "manual" and before.get("source") == "import" and "manual_override" in before:
            changes = {**changes, "manual_override": 1}
        changes = {k: v for k, v in changes.items() if before.get(k) != v}
        self.last_changed = sorted(k for k in changes if k != "manual_override")
        if not changes:
            return False
        changes["updated_at"] = self._now()
        assignments = ", ".join(f"{k} = ?" for k in changes)
        with self.db.transaction():
            self.db.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*changes.values(), row_id))
            self._revision(entity, row_id, action, before, self._row(table, row_id), actor, source, import_id)
        logger.info("[schedule] %s %s %s (%s; fields: %s)", entity, row_id, action, source,
                    ",".join(sorted(k for k in changes if k != "updated_at")))
        return True

    # ------------------------------------------------------------------------------------------- terms
    def _term_values(self, data: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = {**(current or {}), **data}
        problems: dict[str, str] = {}
        name = _text(problems, "name", merged.get("name"), 60, required=True)
        start = _date(problems, "start_date", merged.get("start_date"))
        end = _date(problems, "end_date", merged.get("end_date"))
        monday = _date(problems, "week1_monday", merged.get("week1_monday") or (
            start - timedelta(days=start.weekday()) if start else None))
        zone = str(merged.get("timezone") or "").strip()
        try:
            zone_of(zone)
        except ValueError as exc:
            problems["timezone"] = str(exc)
        status = _choice(problems, "status", merged.get("status") or "active", ("active", "archived"))
        if start and end:
            if end < start:
                problems["end_date"] = "不能早于开始日期"
            elif (end - start).days + 1 > MAX_TERM_DAYS:
                problems["end_date"] = f"一个学期最长 {MAX_TERM_DAYS} 天"
        if monday and start:
            if monday.weekday() != 0:
                problems["week1_monday"] = "第一周的起点必须是星期一"
            elif not 0 <= (start - monday).days <= 6:
                problems["week1_monday"] = "要是开学那一周的星期一"
        if start and end and monday and not problems and (end - monday).days // 7 + 1 > MAX_WEEK:
            problems["end_date"] = f"最多 {MAX_WEEK} 个教学周"
        if problems:
            raise ScheduleValidationError(problems)
        if status == "active":
            clash = self.db.fetch_one(
                "SELECT id FROM schedule_terms WHERE status = 'active' AND id <> ? AND start_date <= ? AND end_date >= ?",
                (int((current or {}).get("id") or 0), end.isoformat(), start.isoformat()))
            if clash:
                raise ScheduleValidationError({"start_date": "和另一个还在用的学期时间重叠"})
        return {"name": name, "start_date": start.isoformat(), "end_date": end.isoformat(),
                "week1_monday": monday.isoformat(), "timezone": zone, "status": status}

    def create_term(self, *, name: str, start_date: Any, end_date: Any, timezone: str,
                    week1_monday: Any = None, source: str = "manual", actor: str = "dashboard",
                    import_id: int | None = None) -> int:
        values = self._term_values({"name": name, "start_date": start_date, "end_date": end_date,
                                    "week1_monday": week1_monday, "timezone": timezone})
        if self.db.fetch_one("SELECT id FROM schedule_terms WHERE name = ?", (values["name"],)):
            raise ScheduleValidationError({"name": "已经有同名的学期"})
        return self._insert("schedule_terms", "term", {**values, "source": source, "import_id": import_id},
                            actor, source, import_id)

    def update_term(self, term_id: int, changes: dict[str, Any], *, actor: str = "dashboard",
                    source: str = "manual") -> bool:
        current = self._require("schedule_terms", term_id, "term")
        values = self._term_values(changes, current)
        values = {k: values[k] for k in changes if k in values} | ({"week1_monday": values["week1_monday"]}
                                                                    if "start_date" in changes else {})
        return self._update("schedule_terms", "term", int(term_id), values, actor, source)

    def archive_term(self, term_id: int, *, actor: str = "dashboard") -> bool:
        self._require("schedule_terms", term_id, "term")
        return self._update("schedule_terms", "term", int(term_id), {"status": "archived"}, actor, "manual")

    def set_periods(self, term_id: int, periods: Iterable[tuple[int, Any, Any]], *, actor: str = "dashboard",
                    source: str = "manual") -> int:
        """Replace the term's period table (one revision with the whole table before / after)."""
        self._require("schedule_terms", term_id, "term")
        problems: dict[str, str] = {}
        cleaned: dict[int, tuple[str, str]] = {}
        for index, entry in enumerate(periods):
            try:
                number, start, end = entry
            except (TypeError, ValueError):
                problems[f"periods[{index}]"] = "要写成（第几节, 开始, 结束）"
                continue
            n = _int(problems, f"periods[{index}].period_no", number, 1, 20)
            a = _time(problems, f"periods[{index}].start", start)
            b = _time(problems, f"periods[{index}].end", end)
            if n is None or a is None or b is None:
                problems.setdefault(f"periods[{index}]", "节次和时间都要写")
                continue
            if b <= a:
                problems[f"periods[{index}].end"] = "要晚于开始时间"
            if n in cleaned:
                problems[f"periods[{index}].period_no"] = "节次重复"
            cleaned[n] = (_hhmm(a), _hhmm(b))
        if problems:
            raise ScheduleValidationError(problems)
        before = self.db.fetch_all("SELECT period_no, start_time, end_time FROM schedule_periods WHERE term_id = ? "
                                   "ORDER BY period_no", (term_id,))
        now = self._now()
        with self.db.transaction():
            self.db.execute("DELETE FROM schedule_periods WHERE term_id = ?", (term_id,))
            for n, (a, b) in sorted(cleaned.items()):
                self.db.execute("INSERT INTO schedule_periods (term_id, period_no, start_time, end_time, created_at, "
                                "updated_at) VALUES (?, ?, ?, ?, ?, ?)", (term_id, n, a, b, now, now))
            after = self.db.fetch_all("SELECT period_no, start_time, end_time FROM schedule_periods WHERE term_id = ? "
                                      "ORDER BY period_no", (term_id,))
            self._revision("period", int(term_id), "update", {"periods": before}, {"periods": after}, actor, source)
        logger.info("[schedule] periods of term %s replaced (%d)", term_id, len(cleaned))
        return len(cleaned)

    # ------------------------------------------------------------------------------------------- courses
    def _course_values(self, data: dict[str, Any], problems: dict[str, str]) -> dict[str, Any]:
        out = {}
        if "name" in data:
            out["name"] = _text(problems, "name", data.get("name"), 60, required=True)
        if "kind" in data:
            out["kind"] = _choice(problems, "kind", data.get("kind") or "lecture", COURSE_KINDS)
        for key, limit in (("code", 40), ("teacher", 40), ("notes", 500)):
            if key in data:
                out[key] = _text(problems, key, data.get(key), limit)
        return out

    def add_course(self, term_id: int, *, name: str, kind: str = "lecture", code: str | None = None,
                   teacher: str | None = None, notes: str | None = None, source: str = "manual",
                   actor: str = "dashboard", import_key: str | None = None, import_id: int | None = None) -> int:
        self._require("schedule_terms", term_id, "term")
        problems: dict[str, str] = {}
        values = self._course_values({"name": name, "kind": kind, "code": code, "teacher": teacher, "notes": notes},
                                     problems)
        _choice(problems, "source", source, SOURCES)
        if problems:
            raise ScheduleValidationError(problems)
        return self._insert("schedule_courses", "course", {**values, "term_id": int(term_id), "source": source,
                                                             "import_key": import_key, "import_id": import_id},
                            actor, source, import_id)

    def update_course(self, course_id: int, changes: dict[str, Any], *, actor: str = "dashboard",
                      source: str = "manual", import_id: int | None = None) -> bool:
        self._require("schedule_courses", course_id, "course")
        problems: dict[str, str] = {}
        values = self._course_values(changes, problems)
        if problems:
            raise ScheduleValidationError(problems)
        return self._update("schedule_courses", "course", int(course_id), values, actor, source, import_id=import_id)

    def delete_course(self, course_id: int, *, actor: str = "dashboard", source: str = "manual",
                      import_id: int | None = None) -> bool:
        self._require("schedule_courses", course_id, "course")
        return self._update("schedule_courses", "course", int(course_id), {"status": "deleted"}, actor, source,
                            action="delete", import_id=import_id)

    def restore_course(self, course_id: int, *, actor: str = "dashboard", source: str = "manual") -> bool:
        """Undo delete_course ("停用" in the Control Center): the course and its active meetings count again."""
        self._require("schedule_courses", course_id, "course")
        return self._update("schedule_courses", "course", int(course_id), {"status": "active"}, actor, source,
                            action="restore")

    # ------------------------------------------------------------------------------------------- slots
    def _slot_values(self, course: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        problems: dict[str, str] = {}
        term = self._row("schedule_terms", course["term_id"])
        weekday = _int(problems, "weekday", data.get("weekday"), 1, 7)
        if weekday is None:
            problems.setdefault("weekday", "不能为空")
        rule = data.get("weeks")
        if not isinstance(rule, WeekRule):
            try:
                rule = parse_weeks(rule)
            except ValueError as exc:
                problems["weeks"] = str(exc)
                rule = None
        if rule is not None and term is not None:
            last_week = (date.fromisoformat(term["end_date"]) - date.fromisoformat(term["week1_monday"])).days // 7 + 1
            if rule.week_to > last_week:
                problems["weeks"] = f"这个学期只有 {last_week} 周"
        start, end = _time(problems, "start", data.get("start")), _time(problems, "end", data.get("end"))
        p_start = _int(problems, "period_start", data.get("period_start"), 1, 20)
        p_end = _int(problems, "period_end", data.get("period_end"), 1, 20)
        timed, by_period = start is not None or end is not None, p_start is not None or p_end is not None
        if timed == by_period:
            problems["time"] = "要么写开始 / 结束时间，要么写起止节次（只写一种）"
        elif timed and (start is None or end is None or end <= start):
            problems["end"] = "开始和结束都要写，结束要晚于开始"
        elif by_period:
            if p_start is None or p_end is None or p_end < p_start:
                problems["period_end"] = "起止节次都要写，结束节不能早于开始节"
            else:
                known = {r["period_no"] for r in self.db.fetch_all(
                    "SELECT period_no FROM schedule_periods WHERE term_id = ?", (course["term_id"],))}
                missing = [n for n in (p_start, p_end) if n not in known]
                if missing:
                    problems["period_start"] = f"第 {missing[0]} 节还没有设置时间"
        location = _text(problems, "location", data.get("location"), 60)
        valid_from = _date(problems, "valid_from", data.get("valid_from"), required=False)
        valid_until = _date(problems, "valid_until", data.get("valid_until"), required=False)
        if valid_from and valid_until and valid_until < valid_from:
            problems["valid_until"] = "不能早于开始生效的日期"
        if problems:
            raise ScheduleValidationError(problems)
        return {"weekday": weekday, "start_time": _hhmm(start), "end_time": _hhmm(end), "period_start": p_start,
                "period_end": p_end, "week_from": rule.week_from, "week_to": rule.week_to, "parity": rule.parity,
                "skip_weeks": json.dumps(sorted(rule.skip)), "weeks_text": rule.text(), "location": location,
                "valid_from": _iso(valid_from), "valid_until": _iso(valid_until)}

    def add_slot(self, course_id: int, *, weekday: int, weeks: Any, start: Any = None, end: Any = None,
                 period_start: Any = None, period_end: Any = None, location: str | None = None,
                 valid_from: Any = None, valid_until: Any = None, source: str = "manual", actor: str = "dashboard",
                 import_key: str | None = None, import_id: int | None = None) -> int:
        course = self._require("schedule_courses", course_id, "course")
        values = self._slot_values(course, {"weekday": weekday, "weeks": weeks, "start": start, "end": end,
                                            "period_start": period_start, "period_end": period_end,
                                            "location": location, "valid_from": valid_from, "valid_until": valid_until})
        return self._insert("schedule_slots", "slot", {**values, "course_id": int(course_id), "source": source,
                                                         "import_key": import_key, "import_id": import_id},
                            actor, source, import_id)

    def update_slot(self, slot_id: int, changes: dict[str, Any], *, actor: str = "dashboard", source: str = "manual",
                    import_id: int | None = None) -> bool:
        slot = self._require("schedule_slots", slot_id, "slot")
        course = self._row("schedule_courses", slot["course_id"])
        current = {"weekday": slot["weekday"], "start": slot["start_time"], "end": slot["end_time"],
                   "period_start": slot["period_start"], "period_end": slot["period_end"],
                   "weeks": WeekRule(slot["week_from"], slot["week_to"], slot["parity"],
                                     frozenset(json.loads(slot["skip_weeks"]))),
                   "location": slot["location"], "valid_from": slot["valid_from"], "valid_until": slot["valid_until"]}
        if "start" in changes or "end" in changes:
            current.update(period_start=None, period_end=None)
        if "period_start" in changes or "period_end" in changes:
            current.update(start=None, end=None)
        values = self._slot_values(course, {**current, **changes})
        return self._update("schedule_slots", "slot", int(slot_id), values, actor, source, import_id=import_id)

    def delete_slot(self, slot_id: int, *, actor: str = "dashboard", source: str = "manual",
                    import_id: int | None = None) -> bool:
        self._require("schedule_slots", slot_id, "slot")
        return self._update("schedule_slots", "slot", int(slot_id), {"status": "deleted"}, actor, source,
                            action="delete", import_id=import_id)

    # ------------------------------------------------------------------------------------------- exceptions
    def add_exception(self, course_id: int, *, kind: str, on_date: Any, slot_id: int | None = None,
                      new_date: Any = None, new_start: Any = None, new_end: Any = None, new_period_start: Any = None,
                      new_period_end: Any = None, new_location: str | None = None, reason: str | None = None,
                      source: str = "manual", actor: str = "dashboard", import_key: str | None = None,
                      import_id: int | None = None) -> int:
        course = self._require("schedule_courses", course_id, "course")
        values = self._exception_values(course, {
            "kind": kind, "on_date": on_date, "slot_id": slot_id, "new_date": new_date, "new_start": new_start,
            "new_end": new_end, "new_period_start": new_period_start, "new_period_end": new_period_end,
            "new_location": new_location, "reason": reason})
        return self._insert("schedule_exceptions", "exception", {
            **values, "term_id": course["term_id"], "course_id": int(course_id), "source": source,
            "import_key": import_key, "import_id": import_id}, actor, source, import_id)

    def update_exception(self, exception_id: int, changes: dict[str, Any], *, actor: str = "dashboard",
                         source: str = "manual", import_id: int | None = None) -> bool:
        row = self._require("schedule_exceptions", exception_id, "exception")
        course = self._row("schedule_courses", row["course_id"])
        current = {"kind": row["kind"], "on_date": row["on_date"], "slot_id": row["slot_id"], "new_date": row["new_date"],
                   "new_start": row["new_start_time"], "new_end": row["new_end_time"],
                   "new_period_start": row["new_period_start"], "new_period_end": row["new_period_end"],
                   "new_location": row["new_location"], "reason": row["reason"]}
        values = self._exception_values(course, {**current, **changes}, exclude_id=int(exception_id))
        if "status" in changes:
            values["status"] = "active" if changes["status"] == "active" else "revoked"
        return self._update("schedule_exceptions", "exception", int(exception_id), values, actor, source,
                            action="restore" if values.get("status") == "active" and row["status"] != "active"
                            else "update", import_id=import_id)

    def _exception_values(self, course: dict[str, Any], data: dict[str, Any],
                          exclude_id: int | None = None) -> dict[str, Any]:
        course_id, slot_id, kind = int(course["id"]), data.get("slot_id"), data.get("kind")
        term = self._row("schedule_terms", course["term_id"])
        problems: dict[str, str] = {}
        _choice(problems, "kind", kind, EXCEPTION_KINDS)
        on = _date(problems, "on_date", data.get("on_date"))
        moved_to = _date(problems, "new_date", data.get("new_date"), required=False)
        a, b = _time(problems, "new_start", data.get("new_start")), _time(problems, "new_end", data.get("new_end"))
        pa = _int(problems, "new_period_start", data.get("new_period_start"), 1, 20)
        pb = _int(problems, "new_period_end", data.get("new_period_end"), 1, 20)
        place = _text(problems, "new_location", data.get("new_location"), 60)
        why = _text(problems, "reason", data.get("reason"), 200)
        tdata = TermData(term=_term(term))
        for name, value in (("on_date", on), ("new_date", moved_to)):
            if value is not None and not tdata.term.contains(value):
                problems[name] = "不在这个学期里"
        if (a is None) != (b is None) or (a is not None and b <= a):
            problems["new_end"] = "开始和结束都要写，结束要晚于开始"
        if (pa is None) != (pb is None) or (pa is not None and pb < pa):
            problems["new_period_end"] = "起止节次都要写，结束节不能早于开始节"
        if a is not None and pa is not None:
            problems["new_start"] = "时间和节次只写一种"
        if pa is not None:
            known = {r["period_no"] for r in self.db.fetch_all(
                "SELECT period_no FROM schedule_periods WHERE term_id = ?", (course["term_id"],))}
            if pa not in known or pb not in known:
                problems["new_period_start"] = "这个节次还没有设置时间"
        has_time = a is not None or pa is not None
        rules = {"cancel": not (moved_to or has_time or place), "move": bool(moved_to or has_time),
                 "time": has_time and moved_to is None, "room": bool(place) and not (moved_to or has_time),
                 "extra": has_time and moved_to is None}
        if kind in rules and not rules[kind]:
            problems["kind"] = {"cancel": "停课不要写新的日期 / 时间 / 地点", "move": "调课要写新的日期或时间",
                                "time": "改时间要写新时间（同一天）", "room": "换教室只写新地点",
                                "extra": "加课要写时间（日期写在 on_date）"}[kind]
        if kind == "extra":
            if slot_id is not None:
                problems["slot_id"] = "加课不属于某个固定的上课时间"
        else:
            slot = self._row("schedule_slots", int(slot_id)) if str(slot_id).isdigit() else None
            if slot is None or slot["course_id"] != course_id or slot["status"] != "active":
                problems["slot_id"] = "要指定这门课的一个上课时间"
            elif on is not None and not problems:
                s = _slot(slot)
                if not s.matches(on, tdata.term.week_of(on)):
                    problems["on_date"] = "课表里那天没有这次课"
        if problems:
            raise ScheduleValidationError(problems)
        if kind != "extra" and self.db.fetch_one(
                "SELECT id FROM schedule_exceptions WHERE slot_id = ? AND on_date = ? AND kind = ? AND status = 'active' "
                "AND id <> ?", (int(slot_id), on.isoformat(), kind, exclude_id or 0)):
            raise ScheduleValidationError({"kind": "这次课已经有一条同类的变动，先撤销那条"})
        return {"slot_id": int(slot_id) if kind != "extra" else None, "kind": kind, "on_date": on.isoformat(),
                "new_date": _iso(moved_to), "new_start_time": _hhmm(a), "new_end_time": _hhmm(b),
                "new_period_start": pa, "new_period_end": pb, "new_location": place, "reason": why}

    def revoke_exception(self, exception_id: int, *, actor: str = "dashboard", source: str = "manual") -> bool:
        self._require("schedule_exceptions", exception_id, "exception")
        return self._update("schedule_exceptions", "exception", int(exception_id), {"status": "revoked"}, actor,
                            source, action="delete")

    # ------------------------------------------------------------------------------------------- exams
    def _exam_values(self, term: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        problems: dict[str, str] = {}
        name = _text(problems, "name", data.get("name"), 60, required=True)
        kind = _choice(problems, "kind", data.get("kind") or "other", EXAM_KINDS)
        on = _date(problems, "exam_date", data.get("exam_date"))
        if on is not None and not _term(term).contains(on):
            problems["exam_date"] = "不在这个学期里"
        a, b = _time(problems, "start", data.get("start")), _time(problems, "end", data.get("end"))
        if (a is None) != (b is None) or (a is not None and b <= a):
            problems["end"] = "开始和结束要么都写、要么都不写，结束要晚于开始"
        course_id = data.get("course_id")
        if course_id is not None:
            course = self._row("schedule_courses", int(course_id)) if str(course_id).isdigit() else None
            if course is None or course["term_id"] != term["id"]:
                problems["course_id"] = "不是这个学期的课"
        values = {"name": name, "kind": kind, "exam_date": _iso(on), "start_time": _hhmm(a), "end_time": _hhmm(b),
                  "course_id": int(course_id) if course_id is not None else None,
                  "location": _text(problems, "location", data.get("location"), 60),
                  "seat": _text(problems, "seat", data.get("seat"), 20),
                  "notes": _text(problems, "notes", data.get("notes"), 500)}
        if problems:
            raise ScheduleValidationError(problems)
        return values

    def add_exam(self, term_id: int, *, name: str, exam_date: Any, kind: str = "other", course_id: int | None = None,
                 start: Any = None, end: Any = None, location: str | None = None, seat: str | None = None,
                 notes: str | None = None, source: str = "manual", actor: str = "dashboard",
                 import_key: str | None = None, import_id: int | None = None) -> int:
        term = self._require("schedule_terms", term_id, "term")
        values = self._exam_values(term, {"name": name, "exam_date": exam_date, "kind": kind, "course_id": course_id,
                                          "start": start, "end": end, "location": location, "seat": seat,
                                          "notes": notes})
        return self._insert("schedule_exams", "exam", {**values, "term_id": int(term_id), "source": source,
                                                         "import_key": import_key, "import_id": import_id},
                            actor, source, import_id)

    def update_exam(self, exam_id: int, changes: dict[str, Any], *, actor: str = "dashboard", source: str = "manual",
                    import_id: int | None = None) -> bool:
        exam = self._require("schedule_exams", exam_id, "exam")
        term = self._row("schedule_terms", exam["term_id"])
        current = {"name": exam["name"], "kind": exam["kind"], "exam_date": exam["exam_date"],
                   "course_id": exam["course_id"], "start": exam["start_time"], "end": exam["end_time"],
                   "location": exam["location"], "seat": exam["seat"], "notes": exam["notes"]}
        values = self._exam_values(term, {**current, **changes})
        return self._update("schedule_exams", "exam", int(exam_id), values, actor, source, import_id=import_id)

    def set_exam_status(self, exam_id: int, status: str, *, actor: str = "dashboard", source: str = "manual",
                        import_id: int | None = None) -> bool:
        self._require("schedule_exams", exam_id, "exam")
        problems: dict[str, str] = {}
        _choice(problems, "status", status, ("active", "cancelled", "deleted"))
        if problems:
            raise ScheduleValidationError(problems)
        return self._update("schedule_exams", "exam", int(exam_id), {"status": status}, actor, source,
                            action="delete" if status == "deleted" else "update", import_id=import_id)

    # ------------------------------------------------------------------------------------------- holidays
    def _holiday_values(self, term: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        problems: dict[str, str] = {}
        kind = _choice(problems, "kind", data.get("kind"), HOLIDAY_KINDS)
        name = _text(problems, "name", data.get("name"), 60, required=True)
        start = _date(problems, "start_date", data.get("start_date"))
        end = _date(problems, "end_date", data.get("end_date") or data.get("start_date"))
        follows = _date(problems, "follows_date", data.get("follows_date"), required=False)
        t = _term(term)
        for label, value in (("start_date", start), ("end_date", end), ("follows_date", follows)):
            if value is not None and not t.contains(value):
                problems[label] = "不在这个学期里"
        if start and end and end < start:
            problems["end_date"] = "不能早于开始日期"
        if kind == "makeup":
            if follows is None:
                problems["follows_date"] = "补课日要写按哪一天的课表上"
            if start and end and start != end:
                problems["end_date"] = "补课日只能是一天"
            if follows and start and follows == start:
                problems["follows_date"] = "不能按自己这一天的课表"
        elif follows is not None:
            problems["follows_date"] = "只有补课日才写这一项"
        if problems:
            raise ScheduleValidationError(problems)
        return {"kind": kind, "name": name, "start_date": _iso(start), "end_date": _iso(end),
                "follows_date": _iso(follows)}

    def add_holiday(self, term_id: int, *, kind: str, name: str, start_date: Any, end_date: Any = None,
                    follows_date: Any = None, source: str = "manual", actor: str = "dashboard",
                    import_key: str | None = None, import_id: int | None = None) -> int:
        term = self._require("schedule_terms", term_id, "term")
        values = self._holiday_values(term, {"kind": kind, "name": name, "start_date": start_date,
                                             "end_date": end_date, "follows_date": follows_date})
        if kind == "makeup" and self.db.fetch_one(
                "SELECT id FROM schedule_holidays WHERE term_id = ? AND kind = 'makeup' AND status = 'active' "
                "AND start_date = ?", (term_id, values["start_date"])):
            raise ScheduleValidationError({"start_date": "这天已经是补课日了"})
        return self._insert("schedule_holidays", "holiday", {**values, "term_id": int(term_id), "source": source,
                                                               "import_key": import_key, "import_id": import_id},
                            actor, source, import_id)

    def delete_holiday(self, holiday_id: int, *, actor: str = "dashboard", source: str = "manual",
                       import_id: int | None = None) -> bool:
        self._require("schedule_holidays", holiday_id, "holiday")
        return self._update("schedule_holidays", "holiday", int(holiday_id), {"status": "deleted"}, actor, source,
                            action="delete", import_id=import_id)

    # ------------------------------------------------------------------------------------------- imports
    def record_import(self, *, file_name: str, sha256: str, fmt: str, actor: str, term_id: int | None = None,
                      summary: dict[str, Any] | None = None, error_count: int = 0) -> int:
        problems: dict[str, str] = {}
        _text(problems, "file_name", file_name, 200, required=True)
        _choice(problems, "format", fmt, ("csv", "json"))
        if not re.fullmatch(r"[0-9a-f]{64}", str(sha256 or "")):
            problems["sha256"] = "要是 64 位十六进制"
        if problems:
            raise ScheduleValidationError(problems)
        now = self._now()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO schedule_imports (file_name, sha256, format, term_id, status, summary, error_count, actor, "
                "created_at) VALUES (?, ?, ?, ?, 'previewed', ?, ?, ?, ?)",
                (file_name.strip(), sha256, fmt, term_id, json.dumps(summary or {}, ensure_ascii=False), error_count,
                 actor, now))
            import_id = int(cur.lastrowid)
            self._revision("import", import_id, "create", None, {"format": fmt, "status": "previewed"}, actor, "import",
                           import_id)
        return import_id

    def apply_import(self, import_id: int, normalized: dict[str, Any], *, actor: str = "dashboard",
                     collect: list[dict[str, str]] | None = None, course_scope: str = "term") -> dict[str, Any]:
        """Apply a parsed import (all or nothing). normalized = {"term": {...}, "periods": [(no, start, end)],
        "courses": [{"key", "name", "kind", "code", "teacher", "notes", "slots": [{"key", "weekday", "weeks",
        "start" / "end" | "period_start" / "period_end", "location"}]}], "exceptions": [{"key", "course_key",
        "slot_key", "kind", "on_date", ...}] (None = leave exceptions alone), "exams": [{"key", "course_key", ...}],
        "holidays": [{"key", "kind", "name", "start_date", "end_date", "follows_date"}]}. A section that is missing /
        None is left alone (a CSV has no exams); an empty list removes what earlier imports added. Rows with
        manual_override are never changed, re-created or deleted, and a course named like a MANUAL course of the term is not
        imported beside it; imported rows missing from the file are deleted (soft). Returns counts + items.

        collect (the preview's dry run, inside a transaction the caller rolls back): every item's problems are
        appended to it and the run continues; without it the first problem aborts and rolls everything back.

        course_scope (phase 9.4 closeout): "term" = the file is the whole term's course list, imported courses of THIS
        term that are not in it are deleted (the JSON semantics; a CSV only when the user chose "整学期替换");
        "file" = only the courses in the file are created / updated, every other course is left as it is (the CSV
        default). Either way a course the file does list is described completely by it (its meetings that are not in
        the file go), and other terms are never touched."""
        if course_scope not in ("term", "file"):
            raise ScheduleValidationError({"mode": "导入范围只能是 term / file"})
        record = self._require("schedule_imports", import_id, "import")
        if record["status"] != "previewed":
            raise ScheduleValidationError({"import": "这次导入已经处理过了"})
        report: dict[str, Any] = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "skipped_manual": 0}
        self._items, self._collect = [], collect
        self._manual_courses: dict[str, int] = {}       # file course key -> the same-named MANUAL course kept instead
        term_id = None
        try:
            with self.db.transaction():
                with self._guard("term") as g:
                    term_id = self._import_term(import_id, normalized.get("term") or {}, actor)
                if g.failed:
                    return {**report, "term_id": None, "items": self._items}
                if normalized.get("periods") is not None:
                    with self._guard("periods"):
                        before = self.db.fetch_all("SELECT period_no, start_time, end_time FROM schedule_periods "
                                                   "WHERE term_id = ? ORDER BY period_no", (term_id,))
                        self.set_periods(term_id, normalized["periods"], actor=actor, source="import")
                        after = self.db.fetch_all("SELECT period_no, start_time, end_time FROM schedule_periods "
                                                  "WHERE term_id = ? ORDER BY period_no", (term_id,))
                        self._item("period", "periods", "update" if before != after else "unchanged",
                                   f"节次表（{len(after)} 节）", report, count=False)
                # a section the file does not have (None) is left alone; an empty list means "none any more"
                if normalized.get("courses") is not None:
                    self._import_courses(import_id, term_id, normalized["courses"], actor, report,
                                         prune=course_scope == "term")
                self._import_exceptions(import_id, term_id, normalized.get("exceptions"), actor, report)
                for table, entity, section in (("schedule_exams", "exam", "exams"),
                                               ("schedule_holidays", "holiday", "holidays")):
                    if normalized.get(section) is not None:
                        self._import_simple(import_id, term_id, table, entity, normalized[section], actor, report)
                self.db.execute("UPDATE schedule_imports SET status = 'applied', term_id = ?, applied_at = ?, "
                                "summary = ? WHERE id = ?", (term_id, self._now(),
                                                              json.dumps(report, ensure_ascii=False), import_id))
        except Exception:
            if collect is None:
                self.db.execute("UPDATE schedule_imports SET status = 'failed' WHERE id = ?", (import_id,))
                logger.warning("[schedule] import %s failed and was rolled back", import_id)
            raise
        finally:
            self._collect = None
        logger.info("[schedule] import %s applied: %s", import_id, report)
        return {**report, "term_id": term_id, "items": self._items}

    def _item(self, entity: str, key: str, action: str, label: str, report: dict[str, Any], *,
              fields: list[str] | None = None, reason: str | None = None, count: bool = True) -> None:
        """One line of the import report (the preview shows them); counts go to report[action]."""
        if count:
            report[action] += 1
        entry = {"entity": entity, "key": key, "action": action, "label": label}
        if fields:
            entry["fields"] = fields
        if reason:
            entry["reason"] = reason
        self._items.append(entry)

    def _import_term(self, import_id: int, data: dict[str, Any], actor: str) -> int:
        existing = self.db.fetch_one("SELECT * FROM schedule_terms WHERE name = ?", (str(data.get("name") or "").strip(),))
        if existing is None:
            return self.create_term(name=data.get("name"), start_date=data.get("start_date"),
                                    end_date=data.get("end_date"), week1_monday=data.get("week1_monday"),
                                    timezone=data.get("timezone"), source="import", actor=actor, import_id=import_id)
        if existing["source"] == "import":
            changes = {k: data[k] for k in ("start_date", "end_date", "week1_monday", "timezone") if data.get(k)}
            self.update_term(existing["id"], changes, actor=actor, source="import")
        return int(existing["id"])

    @staticmethod
    def _located(label: str, exc: ScheduleValidationError) -> ScheduleValidationError:
        """The same problems, each prefixed with the item it belongs to (the importer maps keys to file lines)."""
        return ScheduleValidationError({f"{label}.{k}": v for k, v in exc.problems.items()})

    @contextlib.contextmanager
    def _guard(self, label: str):
        """One import item. Strict (a real import): a problem aborts the whole import, prefixed with the item.
        Collecting (the preview's dry run): the item is undone to a savepoint, its problems are recorded and the
        import goes on with the next item -- so the preview lists every bad row, not only the first."""
        state = types.SimpleNamespace(failed=False)
        if self._collect is None:
            try:
                yield state
            except ScheduleValidationError as exc:
                raise self._located(label, exc) from None
            return
        self.db.execute("SAVEPOINT violet_schedule_item")
        try:
            yield state
        except (ScheduleValidationError, sqlite3.IntegrityError) as exc:
            self.db.execute("ROLLBACK TO violet_schedule_item")
            state.failed = True
            problems = exc.problems if isinstance(exc, ScheduleValidationError) else {"row": "和已有数据冲突"}
            self._collect.extend({"where": f"{label}.{k}", "message": v} for k, v in problems.items())
        finally:
            self.db.execute("RELEASE violet_schedule_item")

    def _skip(self, entity: str, row_id: int, key: str, label: str, actor: str, import_id: int,
              report: dict[str, Any], reason: str = "手动改过，导入不覆盖") -> None:
        self._revision(entity, row_id, "import_skip", None, None, actor, "import", import_id)
        self._item(entity, key, "skipped_manual", label, report, reason=reason)

    def _import_courses(self, import_id: int, term_id: int, courses: list[dict[str, Any]], actor: str,
                        report: dict[str, Any], prune: bool = True) -> None:
        seen = set()
        manual_names = {name_key(r["name"]): int(r["id"]) for r in self.db.fetch_all(
            "SELECT id, name FROM schedule_courses WHERE term_id = ? AND source = 'manual' AND status = 'active'",
            (term_id,))}
        for course in courses:
            key = str(course.get("key") or "").strip()
            if not key:
                raise ScheduleValidationError({"courses": "每门课都要有 key"})
            seen.add(key)
            label = str(course.get("name") or key)
            # a field the file does not carry at all (a CSV without that column) is left as it is
            fields = {k: course[k] for k in ("name", "kind", "code", "teacher", "notes") if k in course}
            row = self.db.fetch_one("SELECT * FROM schedule_courses WHERE term_id = ? AND import_key = ?", (term_id, key))
            course_id, skip_slots = None, False
            with self._guard(f"courses[{key}]") as g:
                if row is None and name_key(course.get("name")) in manual_names:
                    # a course the user entered by hand under the same name wins: never a second copy beside it
                    self._skip("course", manual_names[name_key(course.get("name"))], key, label, actor, import_id,
                               report, reason="已有同名的手动录入课程，导入不另建、不覆盖（连同它的上课时间和调课）")
                    self._manual_courses[key] = manual_names[name_key(course.get("name"))]
                    skip_slots = True
                elif row is None:
                    course_id = self.add_course(term_id, **fields, source="import", actor=actor, import_key=key,
                                                import_id=import_id)
                    self._item("course", key, "created", label, report)
                elif row["manual_override"]:
                    self._skip("course", row["id"], key, label, actor, import_id, report)
                    course_id = int(row["id"])
                else:
                    course_id = int(row["id"])
                    changed = self.update_course(course_id, fields, actor=actor, source="import", import_id=import_id)
                    fields_changed = list(self.last_changed) if changed else []
                    if row["status"] != "active":
                        self._update("schedule_courses", "course", course_id, {"status": "active"}, actor, "import",
                                     action="restore", import_id=import_id)
                        changed, fields_changed = True, fields_changed + ["status"]
                    self._item("course", key, "updated" if changed else "unchanged", label, report,
                               fields=fields_changed)
            if g.failed or skip_slots or course_id is None:
                continue
            self._import_slots(import_id, course_id, key, label, course.get("slots") or [], actor, report)
        if not prune:
            return                          # "file" scope: courses the file does not mention are left alone
        for row in self.db.fetch_all("SELECT id, name, import_key, manual_override, status FROM schedule_courses WHERE "
                                     "term_id = ? AND source = 'import' AND import_key IS NOT NULL", (term_id,)):
            if row["import_key"] in seen or row["status"] != "active":
                continue
            if row["manual_override"]:
                self._skip("course", row["id"], row["import_key"], row["name"], actor, import_id, report,
                           reason="文件里没有了，但手动改过，保留")
                continue
            self.delete_course(row["id"], actor=actor, source="import", import_id=import_id)
            self._item("course", row["import_key"], "deleted", row["name"], report)

    @staticmethod
    def _slot_label(course: str, slot: dict[str, Any]) -> str:
        from ai.schedule.access import WEEKDAY_NAMES
        try:
            day = WEEKDAY_NAMES[int(slot.get("weekday"))]
        except (TypeError, ValueError, IndexError):
            day = "?"
        when = (f"{slot.get('start')}-{slot.get('end')}" if slot.get("start") else
                f"第{slot.get('period_start')}-{slot.get('period_end')}节")
        weeks = slot.get("weeks")
        return f"{course} {day} {when} {weeks.text() if isinstance(weeks, WeekRule) else weeks or ''}".strip()

    def _import_slots(self, import_id: int, course_id: int, course_key: str, course_name: str,
                      slots: list[dict[str, Any]], actor: str, report: dict[str, Any]) -> None:
        seen = set()
        for slot in slots:
            key = str(slot.get("key") or "").strip()
            if not key:
                raise ScheduleValidationError({"slots": "每个上课时间都要有 key"})
            seen.add(key)
            label = self._slot_label(course_name, slot)
            fields = {k: slot[k] for k in ("weekday", "weeks", "start", "end", "period_start", "period_end",
                                            "location", "valid_from", "valid_until") if k in slot}
            row = self.db.fetch_one("SELECT * FROM schedule_slots WHERE course_id = ? AND import_key = ?", (course_id, key))
            with self._guard(f"courses[{course_key}].slots[{key}]"):
                if row is None:
                    self.add_slot(course_id, **fields, source="import", actor=actor, import_key=key,
                                  import_id=import_id)
                    self._item("slot", key, "created", label, report)
                elif row["manual_override"]:
                    self._skip("slot", row["id"], key, label, actor, import_id, report)
                else:
                    changed = self.update_slot(row["id"], fields, actor=actor, source="import", import_id=import_id)
                    fields_changed = list(self.last_changed) if changed else []
                    if row["status"] != "active":
                        self._update("schedule_slots", "slot", int(row["id"]), {"status": "active"}, actor, "import",
                                     action="restore", import_id=import_id)
                        changed, fields_changed = True, fields_changed + ["status"]
                    self._item("slot", key, "updated" if changed else "unchanged", label, report,
                               fields=fields_changed)
        for row in self.db.fetch_all("SELECT * FROM schedule_slots WHERE course_id = ? AND source = 'import' AND "
                                     "import_key IS NOT NULL", (course_id,)):
            if row["import_key"] in seen or row["status"] != "active":
                continue
            label = self._slot_label(course_name, {"weekday": row["weekday"], "start": row["start_time"],
                                                   "end": row["end_time"], "period_start": row["period_start"],
                                                   "period_end": row["period_end"], "weeks": row["weeks_text"]})
            if row["manual_override"]:
                self._skip("slot", row["id"], row["import_key"], label, actor, import_id, report,
                           reason="文件里没有了，但手动改过，保留")
                continue
            self.delete_slot(row["id"], actor=actor, source="import", import_id=import_id)
            self._item("slot", row["import_key"], "deleted", label, report)

    def _import_exceptions(self, import_id: int, term_id: int, items: list[dict[str, Any]] | None, actor: str,
                           report: dict[str, Any]) -> None:
        """Exceptions of imported courses (JSON only). None = the file says nothing about exceptions: keep them."""
        if items is None:
            return
        seen = set()
        for item in items:
            key = str(item.get("key") or "").strip()
            if not key:
                raise ScheduleValidationError({"exceptions": "每一条都要有 key"})
            seen.add(key)
            course = self.db.fetch_one("SELECT * FROM schedule_courses WHERE term_id = ? AND import_key = ?",
                                       (term_id, str(item.get("course_key") or "")))
            manual = self._manual_courses.get(str(item.get("course_key") or ""))
            if course is None and manual is not None:
                # the course is the user's hand-entered one: its changes are the user's too, never imported
                name = self._row("schedule_courses", manual)["name"]
                self._revision("course", manual, "import_skip", None, None, actor, "import", import_id)
                self._item("exception", key, "skipped_manual", f"{name} {item.get('on_date')} {item.get('kind')}",
                           report, reason="所属课程是手动录入的，导入不改它的调课停课")
                continue
            label = f"{course['name'] if course else item.get('course_key')} {item.get('on_date')} {item.get('kind')}"
            with self._guard(f"exceptions[{key}]"):
                if course is None:
                    raise ScheduleValidationError({"course": "指向的课程不在这个文件里（或没有导入成功）"})
                slot_id = None
                if item.get("slot_key"):
                    slot = self.db.fetch_one("SELECT id FROM schedule_slots WHERE course_id = ? AND import_key = ?",
                                             (course["id"], str(item["slot_key"])))
                    if slot is None:
                        raise ScheduleValidationError({"slot": "指向的上课时间不在这门课里"})
                    slot_id = int(slot["id"])
                fields = {k: item.get(k) for k in ("kind", "on_date", "new_date", "new_start", "new_end",
                                                    "new_period_start", "new_period_end", "new_location", "reason")}
                row = self.db.fetch_one("SELECT * FROM schedule_exceptions WHERE term_id = ? AND import_key = ?",
                                        (term_id, key))
                if row is None:
                    self.add_exception(int(course["id"]), slot_id=slot_id, **fields, source="import", actor=actor,
                                       import_key=key, import_id=import_id)
                    self._item("exception", key, "created", label, report)
                elif row["manual_override"]:
                    self._skip("exception", row["id"], key, label, actor, import_id, report)
                else:
                    changed = self.update_exception(row["id"], {**fields, "slot_id": slot_id, "status": "active"},
                                                    actor=actor, source="import", import_id=import_id)
                    self._item("exception", key, "updated" if changed else "unchanged", label, report,
                               fields=list(self.last_changed) if changed else [])
        for row in self.db.fetch_all("SELECT * FROM schedule_exceptions WHERE term_id = ? AND source = 'import' AND "
                                     "import_key IS NOT NULL AND status = 'active'", (term_id,)):
            if row["import_key"] in seen:
                continue
            label = f"{row['on_date']} {row['kind']}"
            if row["manual_override"]:
                self._skip("exception", row["id"], row["import_key"], label, actor, import_id, report,
                           reason="文件里没有了，但手动改过，保留")
                continue
            self.revoke_exception(row["id"], actor=actor, source="import")
            self._item("exception", row["import_key"], "deleted", label, report)

    def _manual_twin(self, term_id: int, entity: str, item: dict[str, Any]) -> int | None:
        """A MANUAL exam with the same name and date / holiday of the same kind and start date, if there is one."""
        if entity == "exam":
            for r in self.db.fetch_all("SELECT id, name FROM schedule_exams WHERE term_id = ? AND source = 'manual' AND "
                                       "status <> 'deleted' AND exam_date = ?", (term_id, str(item.get("exam_date")))):
                if name_key(r["name"]) == name_key(item.get("name")):
                    return int(r["id"])
            return None
        r = self.db.fetch_one("SELECT id FROM schedule_holidays WHERE term_id = ? AND source = 'manual' AND status = "
                              "'active' AND kind = ? AND start_date = ?", (term_id, item.get("kind"),
                                                                           str(item.get("start_date"))))
        return int(r["id"]) if r else None

    def _import_simple(self, import_id: int, term_id: int, table: str, entity: str, items: list[dict[str, Any]],
                       actor: str, report: dict[str, Any]) -> None:
        seen = set()
        for item in items:
            key = str(item.get("key") or "").strip()
            if not key:
                raise ScheduleValidationError({entity: "每一条都要有 key"})
            seen.add(key)
            fields = {k: v for k, v in item.items() if k not in ("key", "course_key")}
            label = f"{item.get('name') or key} {item.get('exam_date') or item.get('start_date') or ''}".strip()
            row = self.db.fetch_one(f"SELECT * FROM {table} WHERE term_id = ? AND import_key = ?", (term_id, key))
            with self._guard(f"{entity}s[{key}]"):
                if entity == "exam" and item.get("course_key"):
                    course = self.db.fetch_one("SELECT id FROM schedule_courses WHERE term_id = ? AND import_key = ?",
                                               (term_id, str(item["course_key"])))
                    course_id = course["id"] if course else self._manual_courses.get(str(item["course_key"]))
                    if course_id is None:
                        raise ScheduleValidationError({"course": "指向的课程不在这个文件里（或没有导入成功）"})
                    fields["course_id"] = int(course_id)
                twin = self._manual_twin(term_id, entity, item) if row is None else None
                if twin is not None:
                    # the same exam / day the user entered by hand: kept as it is, never a second copy
                    self._skip(entity, twin, key, label, actor, import_id, report,
                               reason="已有相同的手动录入记录，导入不另建、不覆盖")
                elif row is None:
                    adder = self.add_exam if entity == "exam" else self.add_holiday
                    adder(term_id, **fields, source="import", actor=actor, import_key=key, import_id=import_id)
                    self._item(entity, key, "created", label, report)
                elif row["manual_override"]:
                    self._skip(entity, row["id"], key, label, actor, import_id, report)
                elif entity == "exam":
                    changed = self.update_exam(row["id"], fields, actor=actor, source="import", import_id=import_id)
                    fields_changed = list(self.last_changed) if changed else []
                    if row["status"] == "deleted":
                        self.set_exam_status(row["id"], "active", actor=actor, source="import", import_id=import_id)
                        changed, fields_changed = True, fields_changed + ["status"]
                    self._item(entity, key, "updated" if changed else "unchanged", label, report, fields=fields_changed)
                else:
                    values = self._holiday_values(self._row("schedule_terms", term_id), fields)
                    if row["status"] != "active":
                        values["status"] = "active"
                    changed = self._update(table, entity, int(row["id"]), values, actor, "import", import_id=import_id)
                    self._item(entity, key, "updated" if changed else "unchanged", label, report,
                               fields=list(self.last_changed) if changed else [])
        live = "status <> 'deleted'"
        for row in self.db.fetch_all(f"SELECT * FROM {table} WHERE term_id = ? AND "
                                     f"source = 'import' AND import_key IS NOT NULL AND {live}", (term_id,)):
            if row["import_key"] in seen:
                continue
            label = f"{row['name']} {row.get('exam_date') or row.get('start_date') or ''}".strip()
            if row["manual_override"]:
                self._skip(entity, row["id"], row["import_key"], label, actor, import_id, report,
                           reason="文件里没有了，但手动改过，保留")
                continue
            if entity == "exam":
                self.set_exam_status(row["id"], "deleted", actor=actor, source="import", import_id=import_id)
            else:
                self.delete_holiday(row["id"], actor=actor, source="import", import_id=import_id)
            self._item(entity, row["import_key"], "deleted", label, report)

    # ------------------------------------------------------------------------------------------- reading
    def revisions(self, entity: str | None = None, entity_id: int | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM schedule_revisions", []
        where = []
        if entity:
            where.append("entity = ?")
            args.append(entity)
        if entity_id is not None:
            where.append("entity_id = ?")
            args.append(entity_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.db.fetch_all(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit)))

    def load(self) -> ScheduleData:
        """One consistent snapshot for the engine (read under the database lock)."""
        with self.db.transaction():
            terms = self.db.fetch_all("SELECT * FROM schedule_terms ORDER BY start_date, id")
            out = []
            for t in terms:
                term_id = t["id"]
                periods = {r["period_no"]: Period(r["period_no"], time.fromisoformat(r["start_time"]),
                                                  time.fromisoformat(r["end_time"]))
                           for r in self.db.fetch_all("SELECT * FROM schedule_periods WHERE term_id = ?", (term_id,))}
                courses = {r["id"]: Course(r["id"], term_id, r["name"], r["kind"], r["code"], r["teacher"], r["status"])
                           for r in self.db.fetch_all("SELECT * FROM schedule_courses WHERE term_id = ?", (term_id,))}
                slots = tuple(_slot(r) for r in self.db.fetch_all(
                    "SELECT s.* FROM schedule_slots s JOIN schedule_courses c ON c.id = s.course_id "
                    "WHERE c.term_id = ? AND s.status = 'active' ORDER BY s.id", (term_id,)))
                exceptions = tuple(_exception(r) for r in self.db.fetch_all(
                    "SELECT * FROM schedule_exceptions WHERE term_id = ? AND status = 'active' ORDER BY id", (term_id,)))
                exams = tuple(_exam(r) for r in self.db.fetch_all(
                    "SELECT * FROM schedule_exams WHERE term_id = ? AND status <> 'deleted' ORDER BY id", (term_id,)))
                holidays = tuple(_holiday(r) for r in self.db.fetch_all(
                    "SELECT * FROM schedule_holidays WHERE term_id = ? AND status = 'active' ORDER BY id", (term_id,)))
                out.append(TermData(_term(t), periods, courses, slots, exceptions, exams, holidays))
            stamps = [r["m"] for r in self.db.fetch_all(
                " UNION ALL ".join(f"SELECT MAX(updated_at) AS m FROM {t}" for t in
                                   ("schedule_terms", "schedule_periods", "schedule_courses", "schedule_slots",
                                    "schedule_exceptions", "schedule_exams", "schedule_holidays"))) if r["m"]]
        return ScheduleData(tuple(out), max(stamps) if stamps else None)

    def day(self, on: Any, *, include_personal: bool = False) -> dict[str, Any]:
        data = self.load()
        result = engine.day(data, parse_date(on)).to_dict(include_personal)
        return {**result, "last_updated": data.last_updated}

    def day_range(self, start: Any, days: int, *, include_personal: bool = False) -> list[dict[str, Any]]:
        data = self.load()
        return [d.to_dict(include_personal) for d in engine.day_range(data, parse_date(start), int(days))]

    def status_at(self, moment: datetime, *, include_personal: bool = False, before: timedelta = timedelta(0),
                  after: timedelta = timedelta(0), default_zone: str | None = None) -> dict[str, Any]:
        if default_zone is None:
            from ai.settings import env_str
            default_zone = env_str("VIOLET_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai"
        data = self.load()
        return engine.status_at(data, moment, default_zone=default_zone, before=before,
                                after=after).to_dict(include_personal)

    def personal_values(self) -> list[str]:
        return engine.personal_values(self.load())

    def known_names(self) -> list[str]:
        """Names of the active courses and exams of active terms (phase 9.4.2: what the memory guard keeps out of
        event memories when only the timetable -- not the user -- said it)."""
        rows = self.db.fetch_all(
            "SELECT c.name FROM schedule_courses c JOIN schedule_terms t ON t.id = c.term_id WHERE c.status = 'active' "
            "AND t.status = 'active' UNION SELECT x.name FROM schedule_exams x JOIN schedule_terms t ON t.id = x.term_id "
            "WHERE x.status = 'active' AND t.status = 'active'")
        return sorted({r["name"] for r in rows if r["name"]})

    # ------------------------------------------------------------------------------------------- privacy
    def export_all(self) -> dict[str, list[dict[str, Any]]]:
        from ai.schedule.schema import TABLES
        return {t: self.db.fetch_all(f"SELECT * FROM {t} ORDER BY id") for t in TABLES}

    def purge_all(self, *, confirm: bool, actor: str = "dashboard") -> dict[str, int]:
        """Delete every timetable row, revisions included (the user's "delete my timetable"). Needs confirm."""
        from ai.schedule.schema import TABLES, row_counts
        if not confirm:
            raise ScheduleValidationError({"confirm": "删除全部课表要确认"})
        counts = row_counts(self.db)
        with self.db.transaction():
            for table in TABLES:
                self.db.execute(f"DELETE FROM {table}")
            self._purge_note("term", 0, "all", counts, actor)
        logger.info("[schedule] all timetable data deleted by %s (%d rows)", actor, sum(counts.values()))
        return counts

    def _purge_note(self, entity: str, entity_id: int, scope: str, counts: dict[str, int], actor: str) -> None:
        """What is left of a purge: when, by whom, the scope and row counts -- never a name or a personal value."""
        self._revision(entity, entity_id, "delete", {"purged": True, "scope": scope,
                                                     "rows": {k: v for k, v in counts.items() if v}},
                       None, actor, "manual")

    def _scope_ids(self, where: str, args: tuple) -> dict[str, list[int]]:
        """Ids of every timetable row in a scope (courses of `where` on schedule_courses c)."""
        courses = [r["id"] for r in self.db.fetch_all(f"SELECT c.id FROM schedule_courses c WHERE {where}", args)]
        marks = ",".join("?" for _ in courses) or "NULL"
        return {"course": courses,
                "slot": [r["id"] for r in self.db.fetch_all(
                    f"SELECT id FROM schedule_slots WHERE course_id IN ({marks})", tuple(courses))],
                "exception": [r["id"] for r in self.db.fetch_all(
                    f"SELECT id FROM schedule_exceptions WHERE course_id IN ({marks})", tuple(courses))]}

    def _scrub_revisions(self, ids: dict[str, list[int]]) -> int:
        removed = 0
        for entity, values in ids.items():
            for start in range(0, len(values), 500):
                chunk = values[start:start + 500]
                cur = self.db.execute(f"DELETE FROM schedule_revisions WHERE entity = ? AND entity_id IN "
                                      f"({','.join('?' for _ in chunk)})", (entity, *chunk))
                removed += max(cur.rowcount, 0)
        return removed

    def purge_course(self, course_id: int, *, confirm: bool, actor: str = "dashboard") -> dict[str, int]:
        """Delete a course for good: the course, its meetings and changes, and every revision of them (the old
        teacher / location / notes copies). Its exams stay (unlinked). Needs confirm."""
        if not confirm:
            raise ScheduleValidationError({"confirm": "彻底删除要确认"})
        course = self._require("schedule_courses", course_id, "course")
        with self.db.transaction():
            ids = self._scope_ids("c.id = ?", (int(course_id),))
            counts = {"schedule_courses": len(ids["course"]), "schedule_slots": len(ids["slot"]),
                      "schedule_exceptions": len(ids["exception"])}
            counts["schedule_revisions"] = self._scrub_revisions(ids)
            self.db.execute("DELETE FROM schedule_courses WHERE id = ?", (int(course_id),))   # cascades
            # the note is filed under the term (the course id means nothing any more)
            self._purge_note("term", int(course["term_id"]), "course", counts, actor)
        logger.info("[schedule] course %s purged by %s (%d rows)", course_id, actor, sum(counts.values()))
        return counts

    def purge_term(self, term_id: int, *, confirm: bool, actor: str = "dashboard") -> dict[str, int]:
        """Delete a term's whole timetable for good, its import records and every revision of anything in it."""
        if not confirm:
            raise ScheduleValidationError({"confirm": "删除学期课表要确认"})
        self._require("schedule_terms", term_id, "term")
        t = int(term_id)
        with self.db.transaction():
            ids = self._scope_ids("c.term_id = ?", (t,))
            ids["exam"] = [r["id"] for r in self.db.fetch_all("SELECT id FROM schedule_exams WHERE term_id = ?", (t,))]
            ids["holiday"] = [r["id"] for r in self.db.fetch_all("SELECT id FROM schedule_holidays WHERE term_id = ?",
                                                                  (t,))]
            ids["exception"] = sorted(set(ids["exception"]) | {r["id"] for r in self.db.fetch_all(
                "SELECT id FROM schedule_exceptions WHERE term_id = ?", (t,))})
            ids["import"] = [r["id"] for r in self.db.fetch_all("SELECT id FROM schedule_imports WHERE term_id = ?",
                                                                 (t,))]
            ids["term"], ids["period"] = [t], [t]
            counts = {"schedule_terms": 1, "schedule_courses": len(ids["course"]), "schedule_slots": len(ids["slot"]),
                      "schedule_exceptions": len(ids["exception"]), "schedule_exams": len(ids["exam"]),
                      "schedule_holidays": len(ids["holiday"]), "schedule_imports": len(ids["import"]),
                      "schedule_periods": int(self.db.fetch_one("SELECT COUNT(*) AS n FROM schedule_periods WHERE "
                                                                "term_id = ?", (t,))["n"])}
            counts["schedule_revisions"] = self._scrub_revisions(ids)
            if ids["import"]:
                self.db.execute(f"DELETE FROM schedule_imports WHERE id IN ({','.join('?' for _ in ids['import'])})",
                                tuple(ids["import"]))
            self.db.execute("DELETE FROM schedule_terms WHERE id = ?", (t,))                 # cascades
            self._purge_note("term", t, "term", counts, actor)
        logger.info("[schedule] term %s purged by %s (%d rows)", t, actor, sum(counts.values()))
        return counts

    def scope_counts(self, term_id: int | None = None) -> dict[str, int]:
        """What a purge of one term (or of everything) would remove -- shown before the user confirms."""
        from ai.schedule.schema import row_counts
        if term_id is None:
            return row_counts(self.db)
        t = int(term_id)
        one = lambda sql: int(self.db.fetch_one(sql, (t,))["n"])                           # noqa: E731
        return {"schedule_courses": one("SELECT COUNT(*) AS n FROM schedule_courses WHERE term_id = ?"),
                "schedule_slots": one("SELECT COUNT(*) AS n FROM schedule_slots s JOIN schedule_courses c ON "
                                      "c.id = s.course_id WHERE c.term_id = ?"),
                "schedule_exceptions": one("SELECT COUNT(*) AS n FROM schedule_exceptions WHERE term_id = ?"),
                "schedule_exams": one("SELECT COUNT(*) AS n FROM schedule_exams WHERE term_id = ?"),
                "schedule_holidays": one("SELECT COUNT(*) AS n FROM schedule_holidays WHERE term_id = ?"),
                "schedule_periods": one("SELECT COUNT(*) AS n FROM schedule_periods WHERE term_id = ?"),
                "schedule_imports": one("SELECT COUNT(*) AS n FROM schedule_imports WHERE term_id = ?")}

    def data_version(self) -> str:
        """A fingerprint of the timetable content (not of revisions or import records): an import preview is
        confirmed only while this is unchanged."""
        with self.db.transaction():
            payload = {t: self.db.fetch_all(f"SELECT * FROM {t} ORDER BY id") for t in CONTENT_TABLES}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                              .encode("utf-8")).hexdigest()[:32]

    def term_revisions(self, term_id: int, limit: int = 100, before_id: int | None = None) -> list[dict[str, Any]]:
        """Revisions of a term and of everything in it, newest first."""
        t = int(term_id)
        where = ("((entity IN ('term', 'period') AND entity_id = :t) "
                 "OR (entity = 'course' AND entity_id IN (SELECT id FROM schedule_courses WHERE term_id = :t)) "
                 "OR (entity = 'slot' AND entity_id IN (SELECT s.id FROM schedule_slots s JOIN schedule_courses c "
                 "ON c.id = s.course_id WHERE c.term_id = :t)) "
                 "OR (entity = 'exception' AND entity_id IN (SELECT id FROM schedule_exceptions WHERE term_id = :t)) "
                 "OR (entity = 'exam' AND entity_id IN (SELECT id FROM schedule_exams WHERE term_id = :t)) "
                 "OR (entity = 'holiday' AND entity_id IN (SELECT id FROM schedule_holidays WHERE term_id = :t)) "
                 "OR (entity = 'import' AND entity_id IN (SELECT id FROM schedule_imports WHERE term_id = :t)))")
        with self.db.transaction():
            rows = self.db.conn.execute(
                f"SELECT * FROM schedule_revisions WHERE {where} AND id < :b ORDER BY id DESC LIMIT :n",
                {"t": t, "b": int(before_id or 2**62), "n": int(limit)}).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------------------------------- row -> model

def _term(r: dict[str, Any]) -> Term:
    return Term(r["id"], r["name"], date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]),
                date.fromisoformat(r["week1_monday"]), r["timezone"], r["status"])


def _t(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _slot(r: dict[str, Any]) -> Slot:
    return Slot(r["id"], r["course_id"], r["weekday"],
                WeekRule(r["week_from"], r["week_to"], r["parity"], frozenset(json.loads(r["skip_weeks"] or "[]"))),
                _t(r["start_time"]), _t(r["end_time"]), r["period_start"], r["period_end"], r["location"],
                _d(r["valid_from"]), _d(r["valid_until"]), r["status"], r["source"])


def _exception(r: dict[str, Any]) -> ClassException:
    return ClassException(r["id"], r["course_id"], r["kind"], date.fromisoformat(r["on_date"]), r["slot_id"],
                          _d(r["new_date"]), _t(r["new_start_time"]), _t(r["new_end_time"]), r["new_period_start"],
                          r["new_period_end"], r["new_location"], r["reason"], r["status"])


def _exam(r: dict[str, Any]) -> Exam:
    return Exam(r["id"], r["name"], date.fromisoformat(r["exam_date"]), r["kind"], r["course_id"], _t(r["start_time"]),
                _t(r["end_time"]), r["location"], r["seat"], r["status"])


def _holiday(r: dict[str, Any]) -> Holiday:
    return Holiday(r["id"], r["kind"], r["name"], date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]),
                   _d(r["follows_date"]), r["status"])
