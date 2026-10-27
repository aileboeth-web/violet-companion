"""Phase 9.4.1: the timetable engine -- pure functions over a ScheduleData snapshot. No database, no clock: the
caller passes the date or the moment, so every result is repeatable.

Rules (docs/knowledge/decisions.md T38):
* Term of a date: the active term whose [start_date, end_date] contains it. Outside every term the day has a
  data_state instead of classes: no_data (no term at all), not_started, between_terms, ended. "No class today"
  is data_state active with no items -- never confused with "no timetable".
* Teaching week = (date - week1_monday) // 7 + 1; parity odd / even from that number.
* Holidays (holiday / no_class) suppress REGULAR meetings only; extras and exams still happen (they were entered
  for that day on purpose). A make-up day ("makeup", one day) runs the timetable of follows_date: the weekday,
  teaching week and parity of follows_date, and that date's exceptions; a make-up day wins over a holiday on the
  same date.
* An occurrence of a meeting is identified by (slot, nominal date). Exceptions refer to that identity; at most
  one active exception of each kind, precedence cancel > move > time > room: cancel removes it (shown as
  cancelled, occupies nothing); a move with another date frees the original date (shown there as moved_out,
  occupying nothing) and occupies the new date only; date / times / place set by a stronger exception are never
  overridden by a weaker one. A move or a change of a meeting the plan does not have is ignored with a note.
* Times: wall-clock times in the term's IANA zone -> aware datetimes; a time that does not exist (spring-forward
  gap) is moved forward by the gap; an ambiguous one (fall-back) takes the first occurrence (fold=0).
* Conflicts: occupying, timed items of a day whose intervals overlap list each other in conflict_with.
* Busy right now (status_at) comes only from the plan of an active term -- never from an ended term -- and is
  marked as a plan, not as where the person really is; what the user said about being busy outranks it (the
  proactive layer applies that).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

from ai.schedule.model import (PERSONAL_FIELDS, ClassException, Exam, Holiday, ScheduleData, Slot, TermData,
                               dates, zone_of)

PLAN_NOTE = "课表只代表计划，不代表对方真实在哪里、是不是真的在上课；对方自己说的情况优先。"
OCCUPYING = ("normal", "changed", "moved_in", "extra", "exam")
MAX_RANGE_DAYS = 62


@dataclass
class Occurrence:
    key: str
    kind: str                         # class | exam
    status: str                       # normal | changed | moved_in | moved_out | cancelled | extra | exam |
    #                                   exam_cancelled | invalid_time
    date: date
    course_id: int | None
    course: str
    start: datetime | None = None
    end: datetime | None = None
    nominal_date: date | None = None
    slot_id: int | None = None
    exam_id: int | None = None
    exception_ids: list[int] = field(default_factory=list)
    location: str | None = None
    teacher: str | None = None
    seat: str | None = None
    exam_kind: str | None = None
    moved_to: date | None = None
    moved_from: date | None = None
    change_note: str | None = None
    conflict_with: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def occupies(self) -> bool:
        return self.status in OCCUPYING

    @property
    def all_day(self) -> bool:
        return self.start is None

    def to_dict(self, include_personal: bool = False) -> dict[str, Any]:
        out = {"key": self.key, "kind": self.kind, "status": self.status, "occupies": self.occupies,
               "date": self.date.isoformat(), "course": self.course, "course_id": self.course_id,
               "start": self.start.isoformat() if self.start else None,
               "end": self.end.isoformat() if self.end else None, "all_day": self.all_day,
               "nominal_date": self.nominal_date.isoformat() if self.nominal_date else None,
               "moved_to": self.moved_to.isoformat() if self.moved_to else None,
               "moved_from": self.moved_from.isoformat() if self.moved_from else None,
               "exam_kind": self.exam_kind, "change_note": self.change_note,
               "conflict_with": list(self.conflict_with), "notes": list(self.notes)}
        if include_personal:
            out.update(location=self.location, teacher=self.teacher, seat=self.seat)
        return out


@dataclass
class DayResult:
    date: date
    weekday: int
    data_state: str                   # active | no_data | not_started | between_terms | ended
    term_id: int | None = None
    term_name: str | None = None
    timezone: str | None = None
    week: int | None = None
    parity: str | None = None
    day_kind: str = "regular"         # regular | holiday | no_class | makeup
    day_name: str | None = None
    follows_date: date | None = None
    items: list[Occurrence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_classes(self) -> bool:
        return any(i.occupies for i in self.items)

    def to_dict(self, include_personal: bool = False) -> dict[str, Any]:
        return {"date": self.date.isoformat(), "weekday": self.weekday, "data_state": self.data_state,
                "term": ({"id": self.term_id, "name": self.term_name, "week": self.week, "parity": self.parity}
                         if self.term_id is not None else None),
                "timezone": self.timezone, "day_kind": self.day_kind, "day_name": self.day_name,
                "follows_date": self.follows_date.isoformat() if self.follows_date else None,
                "items": [i.to_dict(include_personal) for i in self.items], "notes": list(self.notes)}


@dataclass
class StatusResult:
    now: datetime
    data_state: str
    term_id: int | None
    term_name: str | None
    week: int | None
    parity: str | None
    current: list[Occurrence]
    next: Occurrence | None
    remaining_today: int
    basis: str = "timetable_plan"
    note: str = PLAN_NOTE

    def to_dict(self, include_personal: bool = False) -> dict[str, Any]:
        return {"now": self.now.isoformat(), "data_state": self.data_state,
                "term": ({"id": self.term_id, "name": self.term_name, "week": self.week, "parity": self.parity}
                         if self.term_id is not None else None),
                "in_class": bool(self.current), "current": [c.to_dict(include_personal) for c in self.current],
                "next": self.next.to_dict(include_personal) if self.next else None,
                "remaining_today": self.remaining_today, "basis": self.basis, "note": self.note}


# ----------------------------------------------------------------------------------------------- time

def local_moment(day: date, wall: time, zone_name: str) -> tuple[datetime, str | None]:
    """An aware datetime for a wall-clock time; (moment, note) -- the note says when the time had to be moved."""
    zone = zone_of(zone_name)
    naive = datetime.combine(day, wall)
    moment = naive.replace(tzinfo=zone, fold=0)
    back = moment.astimezone(timezone.utc).astimezone(zone)
    if back.replace(tzinfo=None) != naive:                # a time inside the spring-forward gap does not exist
        return back, f"{wall.strftime('%H:%M')} 在这天不存在（夏令时调整），按 {back.strftime('%H:%M')} 算"
    if naive.replace(tzinfo=zone, fold=1).utcoffset() != moment.utcoffset():
        return moment, f"{wall.strftime('%H:%M')} 在这天出现两次（夏令时结束），按第一次算"
    return moment, None


def _resolve_times(td: TermData, start: time | None, end: time | None, p_start: int | None,
                   p_end: int | None) -> tuple[time | None, time | None, str | None]:
    if start is not None and end is not None:
        return start, end, None
    if p_start is None or p_end is None:
        return None, None, "没有时间也没有节次"
    first, last = td.periods.get(p_start), td.periods.get(p_end)
    if first is None or last is None:
        missing = p_start if first is None else p_end
        return None, None, f"第 {missing} 节没有设置时间"
    if last.end <= first.start:
        return None, None, "节次的时间顺序不对"
    return first.start, last.end, None


# ----------------------------------------------------------------------------------------------- terms

def _active_terms(data: ScheduleData) -> list[TermData]:
    return sorted((t for t in data.terms if t.term.status == "active"), key=lambda t: (t.term.start_date, t.term.id))


def term_for(data: ScheduleData, day: date) -> tuple[TermData | None, str]:
    terms = _active_terms(data)
    if not terms:
        return None, "no_data"
    holding = [t for t in terms if t.term.contains(day)]
    if holding:
        return holding[0], "active"
    if all(day < t.term.start_date for t in terms):
        return None, "not_started"
    if all(day > t.term.end_date for t in terms):
        return None, "ended"
    return None, "between_terms"


def _parity(week: int | None) -> str | None:
    return None if week is None else ("odd" if week % 2 else "even")


# ----------------------------------------------------------------------------------------------- one day

def _holiday_on(td: TermData, day: date) -> tuple[Holiday | None, Holiday | None]:
    """(make-up rule for the day, holiday / no_class rule for the day)."""
    makeup = next((h for h in td.holidays if h.kind == "makeup" and h.covers(day)), None)
    off = next((h for h in td.holidays if h.kind in ("holiday", "no_class") and h.covers(day)), None)
    return makeup, off


def _exceptions_by_occurrence(td: TermData) -> dict[tuple[int, date], dict[str, ClassException]]:
    out: dict[tuple[int, date], dict[str, ClassException]] = {}
    for e in td.exceptions:
        if e.status != "active" or e.slot_id is None:
            continue
        out.setdefault((e.slot_id, e.on_date), {}).setdefault(e.kind, e)
    return out


def _planned(td: TermData, slot: Slot, nominal: date) -> bool:
    """The plan has this meeting on its nominal date (pattern only: weekday, week, validity)."""
    course = td.courses.get(slot.course_id)
    return course is not None and course.status == "active" and slot.matches(nominal, td.term.week_of(nominal))


def _build(td: TermData, slot: Slot, nominal: date, placed_on: date, exc: dict[str, ClassException],
           notes: list[str]) -> Occurrence:
    """The occurrence of (slot, nominal) as it ends up on placed_on, with the exceptions applied by precedence."""
    course = td.courses[slot.course_id]
    occ = Occurrence(key=f"slot:{slot.id}:{nominal.isoformat()}", kind="class", status="normal", date=placed_on,
                     course_id=course.id, course=course.name, nominal_date=nominal, slot_id=slot.id,
                     location=slot.location, teacher=course.teacher)
    start, end, p_start, p_end = slot.start, slot.end, slot.period_start, slot.period_end
    if "cancel" in exc:
        occ.status, occ.change_note = "cancelled", "停课"
        occ.exception_ids.append(exc["cancel"].id)
        if "move" in exc:
            occ.notes.append("同一次课既停课又调课：按停课算")
    else:
        set_by_move = set()
        move = exc.get("move")
        if move is not None:
            occ.exception_ids.append(move.id)
            if move.new_date is not None and move.new_date != placed_on:
                # freed here: shown at its ORIGINAL time and place, occupying nothing; it happens on new_date
                occ.status, occ.moved_to, occ.change_note = "moved_out", move.new_date, f"调到 {move.new_date.isoformat()}"
                _place_times(td, occ, placed_on, start, end, p_start, p_end)
                return occ
            if move.new_start is not None or move.new_period_start is not None:
                start, end, p_start, p_end = move.new_start, move.new_end, move.new_period_start, move.new_period_end
                set_by_move.add("time")
            if move.new_location is not None:
                occ.location = move.new_location
                set_by_move.add("location")
            occ.status, occ.change_note = "changed", "调了时间"
        timed = exc.get("time")
        if timed is not None:
            if "time" in set_by_move:
                occ.notes.append("调课已经定了时间，另一条改时间的记录不生效")
            else:
                start, end, p_start, p_end = timed.new_start, timed.new_end, timed.new_period_start, timed.new_period_end
                occ.exception_ids.append(timed.id)
                if occ.status == "normal":
                    occ.status, occ.change_note = "changed", "改了时间"
        room = exc.get("room")
        if room is not None:
            if "location" in set_by_move:
                occ.notes.append("调课已经定了地点，另一条换教室的记录不生效")
            else:
                occ.location = room.new_location
                occ.exception_ids.append(room.id)
                if occ.status == "normal":
                    occ.status, occ.change_note = "changed", "换了教室"
    _place_times(td, occ, placed_on, start, end, p_start, p_end)
    return occ


def _place_times(td: TermData, occ: Occurrence, day: date, start: time | None, end: time | None,
                 p_start: int | None, p_end: int | None) -> None:
    wall_start, wall_end, problem = _resolve_times(td, start, end, p_start, p_end)
    if problem:
        occ.notes.append(problem)
        if occ.occupies:
            occ.status = "invalid_time"
        return
    occ.start, note_a = local_moment(day, wall_start, td.term.timezone)
    occ.end, note_b = local_moment(day, wall_end, td.term.timezone)
    occ.notes += [n for n in (note_a, note_b) if n]


def _regular(td: TermData, day: date, by_occ, notes: list[str]) -> tuple[list[Occurrence], str, str | None, date | None]:
    makeup, off = _holiday_on(td, day)
    if makeup is not None:
        teaching = makeup.follows_date
        if teaching is None or not td.term.contains(teaching):
            notes.append(f"补课日“{makeup.name}”要跟的日期不在这个学期里，这天不排课")
            return [], "makeup", makeup.name, teaching
        kind, name, follows = "makeup", makeup.name, teaching
    elif off is not None:
        return [], off.kind, off.name, None
    else:
        teaching, kind, name, follows = day, "regular", None, None
    items = [_build(td, s, teaching, day, by_occ.get((s.id, teaching), {}), notes)
             for s in td.slots if _planned(td, s, teaching)]
    return items, kind, name, follows


def _moved_in(td: TermData, day: date, by_occ, notes: list[str], already: set[str]) -> list[Occurrence]:
    """Meetings moved INTO this date from another one. `already` = keys the regular pass placed here (a move to
    the day the meeting is on anyway only changes it -- it must not appear twice)."""
    out = []
    slots = {s.id: s for s in td.slots}
    for (slot_id, nominal), exc in sorted(by_occ.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        move = exc.get("move")
        if move is None or move.new_date != day or "cancel" in exc:
            continue
        if f"slot:{slot_id}:{nominal.isoformat()}" in already:
            continue
        slot = slots.get(slot_id)
        if slot is None or not _planned(td, slot, nominal):
            notes.append(f"一条调课记录指向 {nominal.isoformat()} 那次课，但课表里那天没有这次课，没有生效")
            continue
        occ = _build(td, slot, nominal, day, {**exc, "move": replace(move, new_date=None)}, notes)
        occ.moved_from = nominal
        if occ.status != "invalid_time":
            occ.status, occ.change_note = "moved_in", f"从 {nominal.isoformat()} 调过来"
        out.append(occ)
    return out


def _extras(td: TermData, day: date) -> list[Occurrence]:
    out = []
    for e in td.exceptions:
        if e.status != "active" or e.kind != "extra" or e.on_date != day:
            continue
        course = td.courses.get(e.course_id)
        if course is None or course.status != "active":
            continue
        occ = Occurrence(key=f"extra:{e.id}", kind="class", status="extra", date=day, course_id=course.id,
                         course=course.name, nominal_date=day, exception_ids=[e.id], location=e.new_location,
                         teacher=course.teacher, change_note="加课")
        _place_times(td, occ, day, e.new_start, e.new_end, e.new_period_start, e.new_period_end)
        out.append(occ)
    return out


def _exams(td: TermData, day: date) -> list[Occurrence]:
    out = []
    for x in td.exams:
        if x.exam_date != day or x.status == "deleted":
            continue
        course = td.courses.get(x.course_id) if x.course_id else None
        occ = Occurrence(key=f"exam:{x.id}", kind="exam", status="exam" if x.status == "active" else "exam_cancelled",
                         date=day, course_id=x.course_id, course=x.name, nominal_date=day, exam_id=x.id,
                         location=x.location, seat=x.seat, exam_kind=x.kind,
                         teacher=course.teacher if course else None)
        if x.start is not None and x.end is not None:
            occ.start, a = local_moment(day, x.start, td.term.timezone)
            occ.end, b = local_moment(day, x.end, td.term.timezone)
            occ.notes += [n for n in (a, b) if n]
        else:
            occ.notes.append("考试时间还不知道")
        out.append(occ)
    return out


def _mark_conflicts(items: list[Occurrence]) -> None:
    timed = [i for i in items if i.occupies and i.start is not None]
    for a_index, a in enumerate(timed):
        for b in timed[a_index + 1:]:
            if a.start < b.end and b.start < a.end:
                a.conflict_with.append(b.key)
                b.conflict_with.append(a.key)


def _sort_key(o: Occurrence):
    far = datetime.max.replace(tzinfo=timezone.utc)
    return (o.start or far, 0 if o.occupies else 1, o.key)


def day(data: ScheduleData, on: date) -> DayResult:
    td, state = term_for(data, on)
    result = DayResult(date=on, weekday=on.isoweekday(), data_state=state)
    if td is None:
        return result
    week = td.term.week_of(on)
    result.term_id, result.term_name, result.timezone = td.term.id, td.term.name, td.term.timezone
    by_occ = _exceptions_by_occurrence(td)
    regular, kind, name, follows = _regular(td, on, by_occ, result.notes)
    result.day_kind, result.day_name, result.follows_date = kind, name, follows
    teaching_week = td.term.week_of(follows) if follows else week
    result.week, result.parity = teaching_week, _parity(teaching_week)
    if follows:
        result.notes.append(f"这天按 {follows.isoformat()} 的课表上课（第 {teaching_week} 周）")
    items = (regular + _moved_in(td, on, by_occ, result.notes, {i.key for i in regular}) + _extras(td, on)
             + _exams(td, on))
    _mark_conflicts(items)
    result.items = sorted(items, key=_sort_key)
    return result


def day_range(data: ScheduleData, start: date, days: int) -> list[DayResult]:
    if not 1 <= days <= MAX_RANGE_DAYS:
        raise ValueError(f"一次最多查 {MAX_RANGE_DAYS} 天")
    return [day(data, d) for d in dates(start, days)]


# ----------------------------------------------------------------------------------------------- right now

def _zone_for_moment(data: ScheduleData, moment: datetime, default_zone: str) -> tuple[str, date]:
    for td in _active_terms(data):
        local = moment.astimezone(zone_of(td.term.timezone))
        if td.term.contains(local.date()):
            return td.term.timezone, local.date()
    return default_zone, moment.astimezone(zone_of(default_zone)).date()


def local_date(data: ScheduleData, moment: datetime, default_zone: str = "Asia/Shanghai") -> tuple[date, str]:
    """(the date of a moment in the zone of the term it falls in -- else default_zone --, that zone)."""
    zone, today = _zone_for_moment(data, moment, default_zone)
    return today, zone


def status_at(data: ScheduleData, moment: datetime, *, default_zone: str = "Asia/Shanghai",
              before: timedelta = timedelta(0), after: timedelta = timedelta(0), lookahead_days: int = 7) -> StatusResult:
    """What the plan says about a moment: in class (occupying timed items around it), the next one, how many are
    left today. Only an active term counts -- after the term ended nothing is "in class"."""
    if moment.tzinfo is None:
        raise ValueError("status_at needs an aware datetime")
    zone, today = _zone_for_moment(data, moment, default_zone)
    first = day(data, today)
    now_local = moment.astimezone(zone_of(first.timezone or zone))
    if first.data_state != "active":
        return StatusResult(now_local, first.data_state, None, None, None, None, [], None, 0)
    timed = [i for i in first.items if i.occupies and i.start is not None]
    current = [i for i in timed if i.start - before <= moment < i.end + after]
    upcoming = [i for i in timed if i.start > moment]
    nxt = upcoming[0] if upcoming else None
    if nxt is None:
        for later in dates(today + timedelta(days=1), lookahead_days):
            d = day(data, later)
            if d.data_state != "active":
                break
            found = [i for i in d.items if i.occupies and i.start is not None]
            if found:
                nxt = found[0]
                break
    return StatusResult(now_local, "active", first.term_id, first.term_name, first.week, first.parity, current, nxt,
                        len(upcoming))


def personal_values(data: ScheduleData) -> list[str]:
    """Every teacher / location / seat in the snapshot (to keep them out of web searches and logs)."""
    values: set[str] = set()
    for td in data.terms:
        values.update(c.teacher for c in td.courses.values() if c.teacher)
        values.update(s.location for s in td.slots if s.location)
        values.update(e.new_location for e in td.exceptions if e.new_location)
        values.update(x.location for x in td.exams if x.location)
        values.update(x.seat for x in td.exams if x.seat)
    return sorted(v for v in values if len(v.strip()) >= 2)


__all__ = ["Occurrence", "DayResult", "StatusResult", "PLAN_NOTE", "PERSONAL_FIELDS", "day", "day_range",
           "status_at", "term_for", "local_moment", "personal_values"]
