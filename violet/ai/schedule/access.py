"""Phase 9.4.2: what a timetable lookup may return in this turn, and date phrases.

* Personal fields (teacher, location -- of a class or an exam --, seat) are returned only when (1) the model asked
  for them in `details`, (2) the field is allowed by VIOLET_SCHEDULE_DETAIL_FIELDS (default location,teacher,seat) and
  (3) the user's OWN message this turn asks for that kind of detail ("哪个教室", "哪位老师", "座位号"). Notes are
  never returned. The user's message reaches the in-process MCP server through a turn-scoped context variable set by
  ai.chat.run_pipeline (turn_message); without one (proactive, tests that do not set it) no personal field is given.
* Dates: today / tomorrow / the day after / yesterday (also 今天 / 明天 / 后天 / 昨天), YYYY-MM-DD, and a weekday of
  THIS week (周三, 星期五, wednesday). Anything else is invalid -- the model must give a date.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from datetime import date, timedelta
from typing import Any, Iterable

from ai.settings import env_list

DETAIL_FIELDS = ("location", "teacher", "seat")
# what the user's words must ask for before a field is given (Chinese first, a few English words)
DETAIL_ASKS = {
    "location": re.compile(r"(教室|教学楼|哪栋|哪个楼|哪间|几楼|地点|地方|哪里|哪儿|在哪|去哪|考场|room|where)", re.I),
    "teacher": re.compile(r"(老师|教师|任课|谁上|谁教|谁讲|teacher|professor)", re.I),
    "seat": re.compile(r"(座位|座号|几号座|考号|seat)", re.I),
}
_TURN_MESSAGE: contextvars.ContextVar[str | None] = contextvars.ContextVar("violet_schedule_turn_message", default=None)

WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
EN_WEEKDAYS = {"monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6, "sunday": 7}
RELATIVE = {"today": 0, "今天": 0, "tomorrow": 1, "明天": 1, "day_after_tomorrow": 2, "后天": 2, "yesterday": -1,
            "昨天": -1}
WEEKDAY_NAMES = ("", "周一", "周二", "周三", "周四", "周五", "周六", "周日")


@contextlib.contextmanager
def turn_message(text: str | None):
    """The user's message of the turn being answered (what personal fields may be returned for)."""
    token = _TURN_MESSAGE.set(text or "")
    try:
        yield
    finally:
        _TURN_MESSAGE.reset(token)


def current_turn_message() -> str | None:
    return _TURN_MESSAGE.get()


def allowed_detail_fields() -> set[str]:
    configured = env_list("VIOLET_SCHEDULE_DETAIL_FIELDS", list(DETAIL_FIELDS))
    return {f.strip().lower() for f in configured if f.strip().lower() in DETAIL_FIELDS}


def granted_details(requested: Iterable[Any] | None, user_message: str | None) -> tuple[list[str], list[str]]:
    """(fields given, fields refused) for this call."""
    asked = []
    for field in requested or []:
        name = str(field or "").strip().lower()
        if name in DETAIL_FIELDS and name not in asked:
            asked.append(name)
    allowed = allowed_detail_fields()
    text = user_message or ""
    given = [f for f in asked if f in allowed and DETAIL_ASKS[f].search(text)]
    return given, [f for f in asked if f not in given]


def project(item: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """An occurrence as the engine gave it (with personal fields), reduced to what this call may return."""
    keep = set(fields)
    out = {k: v for k, v in item.items() if k not in DETAIL_FIELDS}
    for name in DETAIL_FIELDS:
        if name in keep and item.get(name):
            out[name] = item[name]
    return out


_PAREN = re.compile(r"[（(][^）)]*[）)]")
_CJK = re.compile(r"[一-鿿]+")


def core_name(name: str) -> str:
    """A course name without its parenthesised part ("高等数学（虚构）" -> "高等数学")."""
    return _PAREN.sub("", name or "").strip() or (name or "").strip()


def is_abbreviation(short: str, name: str) -> bool:
    """"高数" -> "高等数学": at least 2 characters, the same first character, all of them in order in the name."""
    short, name = short.lower(), name.lower()
    if len(short) < 2 or len(short) >= len(name) or short[0] != name[0]:
        return False
    pos = 0
    for ch in short:
        pos = name.find(ch, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def mentions_course(text: str, name: str) -> bool:
    """Whether a text names a course -- its name (bigrams) or an abbreviation of it inside the text."""
    from ai.memory_model import mentions_topic
    core = core_name(name)
    if mentions_topic(text or "", core):
        return True
    for run in _CJK.findall(text or ""):
        for size in (2, 3, 4):
            if any(is_abbreviation(run[i:i + size], core) for i in range(len(run) - size + 1)):
                return True
    return False


def resolve_date(text: Any, today: date) -> date:
    """A date phrase -> a date (ValueError when it is not one of the supported forms)."""
    raw = str(text if text is not None else "today").strip()
    key = raw.lower().replace(" ", "_")
    if key in RELATIVE:
        return today + timedelta(days=RELATIVE[key])
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValueError("不存在的日期") from None
    match = re.fullmatch(r"(?:本周|这周|这个)?(?:周|星期|礼拜)([一二三四五六日天])", raw)
    weekday = WEEKDAYS[match.group(1)] if match else EN_WEEKDAYS.get(key)
    if weekday:
        return today + timedelta(days=weekday - today.isoweekday())       # that day of THIS week
    if key in ("this_week", "本周", "这周", "这个星期"):
        return today - timedelta(days=today.isoweekday() - 1)             # Monday of this week
    if key in ("next_week", "下周", "下个星期"):
        return today + timedelta(days=8 - today.isoweekday())             # Monday of next week
    raise ValueError("日期要写 today / tomorrow / YYYY-MM-DD / 周几（本周）")
