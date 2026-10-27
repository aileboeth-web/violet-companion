"""Phase 9.4.1: timetable data classes, the week rule and input validation (no database, no clock)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

MAX_WEEK = 30
MAX_TERM_DAYS = 400
COURSE_KINDS = ("lecture", "lab", "seminar", "pe", "other")
EXAM_KINDS = ("final", "midterm", "quiz", "other")
EXCEPTION_KINDS = ("cancel", "move", "time", "room", "extra")
HOLIDAY_KINDS = ("holiday", "no_class", "makeup")
SOURCES = ("manual", "import")
# fields that identify a person or a place: never in ordinary logs, web search queries or proactive messages
PERSONAL_FIELDS = ("teacher", "location", "seat", "notes")


class ScheduleValidationError(ValueError):
    """What cannot be written, field by field (messages never repeat the personal values themselves)."""

    def __init__(self, problems: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = dict(problems)


# ----------------------------------------------------------------------------------------------- parsing

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not _DATE_RE.match(text):
        raise ValueError("日期要写成 YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("不存在的日期") from None


def parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    text = str(value or "").strip()
    if re.match(r"^\d:[0-5]\d$", text):
        text = "0" + text
    if not _TIME_RE.match(text):
        raise ValueError("时间要写成 HH:MM（00:00–23:59）")
    return time.fromisoformat(text)


def zone_of(name: str):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(str(name))
    except Exception:
        raise ValueError("不认识的时区（要用 IANA 名称，例如 Asia/Shanghai）") from None


# ----------------------------------------------------------------------------------------------- weeks

_WEEK_TEXT_RE = re.compile(r"^[0-9,\-]+$")


@dataclass(frozen=True)
class WeekRule:
    """The teaching weeks a meeting runs in: {w : week_from <= w <= week_to, parity matches, w not in skip}."""
    week_from: int
    week_to: int
    parity: str = "all"
    skip: frozenset[int] = frozenset()

    def runs_in(self, week: int | None) -> bool:
        if week is None or not self.week_from <= week <= self.week_to or week in self.skip:
            return False
        return self.parity == "all" or (week % 2 == 1) == (self.parity == "odd")

    def weeks(self) -> list[int]:
        return [w for w in range(self.week_from, self.week_to + 1) if self.runs_in(w)]

    def text(self) -> str:
        weeks = self.weeks()
        if not weeks:
            return ""
        runs, start, prev = [], weeks[0], weeks[0]
        step = 1 if self.parity == "all" else 2
        for w in weeks[1:]:
            if w != prev + step:
                runs.append((start, prev))
                start = w
            prev = w
        runs.append((start, prev))
        body = ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)
        return body + {"all": "", "odd": "单", "even": "双"}[self.parity]


def parse_weeks(text: str) -> WeekRule:
    """'1-16', '1-16单', '第1-16周(双)', '1-8,10-16', '1,3,5,7', '2-16 even' -> WeekRule (canonical form)."""
    raw = str(text or "").strip()
    parity = "all"
    for marker, value in (("单", "odd"), ("odd", "odd"), ("双", "even"), ("even", "even")):
        if marker in raw.lower():
            parity = value
            raw = re.sub(marker, "", raw, flags=re.I)
    raw = re.sub(r"[第周()（）\s]", "", raw).replace("，", ",").replace("~", "-").replace("–", "-")
    if not raw or not _WEEK_TEXT_RE.match(raw):
        raise ValueError("周次写法不认识（例如 1-16、1-16单、1-8,10-16）")
    listed: set[int] = set()
    for part in raw.split(","):
        if not part:
            continue
        bounds = part.split("-")
        if len(bounds) == 1 and bounds[0].isdigit():
            a = b = int(bounds[0])
        elif len(bounds) == 2 and all(x.isdigit() for x in bounds):
            a, b = int(bounds[0]), int(bounds[1])
        else:
            raise ValueError("周次写法不认识（例如 1-16、1-16单、1-8,10-16）")
        if a > b:
            raise ValueError("周次范围要从小到大")
        listed.update(range(a, b + 1))
    if parity != "all":
        listed = {w for w in listed if (w % 2 == 1) == (parity == "odd")}
    if not listed:
        raise ValueError("这个周次写法里一周都没有")
    if min(listed) < 1 or max(listed) > MAX_WEEK:
        raise ValueError(f"周次要在 1–{MAX_WEEK} 之间")
    lo, hi = min(listed), max(listed)
    if parity == "all" and len(listed) > 1:
        # 1,3,5,7 written without a marker is an odd-week rule, not "all minus 2,4,6"
        if all(w % 2 == lo % 2 for w in listed) and listed == set(range(lo, hi + 1, 2)):
            parity = "odd" if lo % 2 else "even"
    rule = WeekRule(lo, hi, parity)
    return WeekRule(lo, hi, parity, frozenset(set(rule.weeks()) - listed))


# ----------------------------------------------------------------------------------------------- data classes

@dataclass(frozen=True)
class Term:
    id: int
    name: str
    start_date: date
    end_date: date
    week1_monday: date
    timezone: str
    status: str = "active"

    def contains(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date

    def week_of(self, day: date) -> int | None:
        """Teaching week of a date inside the term (week 1 starts on week1_monday)."""
        if not self.contains(day):
            return None
        return (day - self.week1_monday).days // 7 + 1


@dataclass(frozen=True)
class Period:
    period_no: int
    start: time
    end: time


@dataclass(frozen=True)
class Course:
    id: int
    term_id: int
    name: str
    kind: str = "lecture"
    code: str | None = None
    teacher: str | None = None
    status: str = "active"


@dataclass(frozen=True)
class Slot:
    id: int
    course_id: int
    weekday: int
    weeks: WeekRule
    start: time | None = None
    end: time | None = None
    period_start: int | None = None
    period_end: int | None = None
    location: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    status: str = "active"
    source: str = "manual"

    def matches(self, day: date, week: int | None) -> bool:
        """The plan has this meeting on that date (weekday, teaching week, validity) -- holidays aside."""
        if self.status != "active" or day.isoweekday() != self.weekday or not self.weeks.runs_in(week):
            return False
        return (self.valid_from is None or day >= self.valid_from) and (self.valid_until is None or day <= self.valid_until)


@dataclass(frozen=True)
class ClassException:
    id: int
    course_id: int
    kind: str
    on_date: date
    slot_id: int | None = None
    new_date: date | None = None
    new_start: time | None = None
    new_end: time | None = None
    new_period_start: int | None = None
    new_period_end: int | None = None
    new_location: str | None = None
    reason: str | None = None
    status: str = "active"


@dataclass(frozen=True)
class Exam:
    id: int
    name: str
    exam_date: date
    kind: str = "other"
    course_id: int | None = None
    start: time | None = None
    end: time | None = None
    location: str | None = None
    seat: str | None = None
    status: str = "active"


@dataclass(frozen=True)
class Holiday:
    id: int
    kind: str
    name: str
    start_date: date
    end_date: date
    follows_date: date | None = None
    status: str = "active"

    def covers(self, day: date) -> bool:
        return self.status == "active" and self.start_date <= day <= self.end_date


@dataclass(frozen=True)
class TermData:
    term: Term
    periods: dict[int, Period] = field(default_factory=dict)
    courses: dict[int, Course] = field(default_factory=dict)
    slots: tuple[Slot, ...] = ()
    exceptions: tuple[ClassException, ...] = ()
    exams: tuple[Exam, ...] = ()
    holidays: tuple[Holiday, ...] = ()


@dataclass(frozen=True)
class ScheduleData:
    """Everything the engine needs, read once (a snapshot): computations never touch the database."""
    terms: tuple[TermData, ...] = ()
    last_updated: str | None = None


def dates(start: date, days: int):
    for i in range(days):
        yield start + timedelta(days=i)
