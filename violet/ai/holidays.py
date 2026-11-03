"""Holiday greetings (2026-11-03, user: 做个开关“节日祝福”，在你可能感兴趣、符合人设的节日给予祝福；恋人人设
应该是情人节这样的节日).

Which holidays count depends on the relationship of the channel's persona (ai/persona_templates.RELATIONSHIPS):
everyone gets the common ones; a partner also 情人节 / 520 / 七夕 / ...; a daughter 父亲节 or 母亲节 (by what she calls
the owner) and 儿童节; the developer persona 程序员节. "Optional" holidays (端午, 重阳, 万圣节, 感恩节) only when the
owner's profile / core memories mention them. No 清明 (user decision). The owner's birthday and a partner's
anniversary come from the profile / core memories ("生日是3月5日", "纪念日 5.20").

Lunar dates are computed from the lunar year table (month lengths + leap month per year, the public table used by
lunardate / the Hong Kong Observatory data), anchored at 春节 2026 = 2026-02-17 -- no extra dependency, 2026-2045.

VIOLET_HOLIDAY_GREETING (default off): the switch on the settings page. The scheduler sends one greeting per
holiday per persona (ai/proactive_scheduler._holiday_greeting); the chat context names today's holiday either way
when the switch is on, so a conversation that started first can mention it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from ai.settings import env_bool

# lunar year info 2026..2045: bits 15..4 = months 1..12 long (30 days) or short (29); bits 3..0 = leap month (0 none);
# bit 16 = the leap month is long
_LUNAR_INFO = (0x0a4e0, 0x0d260, 0x0ea65, 0x0d530, 0x05aa0,     # 2026-2030
               0x076a3, 0x096d0, 0x04afb, 0x04ad0, 0x0a4d0,     # 2031-2035
               0x1d0b6, 0x0d250, 0x0d520, 0x0dd45, 0x0b5a0,     # 2036-2040
               0x056d0, 0x055b2, 0x049b0, 0x0a577, 0x0a4b0)     # 2041-2045
_FIRST_YEAR = 2026
_NEW_YEAR_2026 = date(2026, 2, 17)


def _months(info: int) -> list[tuple[int, bool, int]]:
    """(month, is_leap, days) in order."""
    out = [(m, False, 30 if (info >> (16 - m)) & 1 else 29) for m in range(1, 13)]
    leap = info & 0xF
    if leap:
        out.insert(leap, (leap, True, 30 if (info >> 16) & 1 else 29))
    return out


def lunar_to_solar(year: int, month: int, day: int) -> date | None:
    """The solar date of a (non-leap) lunar date; None outside 2026-2045."""
    if not _FIRST_YEAR <= year < _FIRST_YEAR + len(_LUNAR_INFO):
        return None
    start = _NEW_YEAR_2026
    for y in range(_FIRST_YEAR, year):
        start += timedelta(days=sum(d for _, _, d in _months(_LUNAR_INFO[y - _FIRST_YEAR])))
    offset = 0
    for m, leap, days in _months(_LUNAR_INFO[year - _FIRST_YEAR]):
        if m == month and not leap:
            return start + timedelta(days=offset + min(day, days) - 1)
        offset += days
    return None


@dataclass(frozen=True)
class Holiday:
    key: str
    name: str
    group: str            # all | partner | daughter | father | mother | creator | optional | birthday | anniversary
    hint: str = ""        # how it may be mentioned
    words: tuple[str, ...] = ()      # optional ones: profile / memory words that make it count


# (month, day) solar
_SOLAR = [
    ((1, 1), Holiday("new_year", "元旦", "all")),
    ((2, 14), Holiday("valentine", "情人节", "partner")),
    ((3, 14), Holiday("white_day", "白色情人节", "partner")),
    ((5, 20), Holiday("520", "520", "partner", "谐音“我爱你”的日子")),
    ((6, 1), Holiday("children", "儿童节", "daughter", "可以撒娇说自己也要过节")),
    ((10, 1), Holiday("national", "国庆节", "all")),
    ((10, 24), Holiday("programmer", "程序员节", "creator")),
    ((10, 31), Holiday("halloween", "万圣节", "optional", words=("万圣", "cos", "南瓜", "鬼"))),
    ((12, 24), Holiday("christmas_eve", "平安夜", "partner")),
    ((12, 25), Holiday("christmas", "圣诞节", "partner")),
]
# (lunar month, lunar day); 除夕 = the day before 春节
_LUNAR = [
    ((1, 1), Holiday("spring", "春节", "all")),
    ((1, 15), Holiday("lantern", "元宵节", "all")),
    ((5, 5), Holiday("dragon_boat", "端午节", "optional", words=("端午", "粽子", "龙舟"))),
    ((7, 7), Holiday("qixi", "七夕", "partner")),
    ((8, 15), Holiday("mid_autumn", "中秋节", "all")),
    ((9, 9), Holiday("double_ninth", "重阳节", "optional", words=("重阳", "爬山", "登高"))),
]
_CHUXI = Holiday("new_year_eve", "除夕", "all", "年夜饭、守岁")
_MOTHER = Holiday("mother", "母亲节", "mother")
_FATHER = Holiday("father", "父亲节", "father")
_THANKS = Holiday("thanksgiving", "感恩节", "optional", words=("感恩节", "火鸡"))

# which groups each relationship gets (persona_templates.RELATIONSHIPS)
GROUPS = {"close": {"all"}, "friend": {"all"}, "partner": {"all", "partner"},
          "daughter": {"all", "daughter"}, "creator": {"all", "creator"}}
_MOTHER_CALLS = ("妈", "母")
_FATHER_CALLS = ("爸", "父")


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def on_day(day: date) -> list[Holiday]:
    """Every holiday of the table on this day (before the relationship filter)."""
    out = [h for (m, d), h in _SOLAR if (day.month, day.day) == (m, d)]
    for (m, d), h in _LUNAR:
        if lunar_to_solar(day.year, m, d) == day:
            out.append(h)
    spring_next = lunar_to_solar(day.year + 1, 1, 1)
    spring_this = lunar_to_solar(day.year, 1, 1)
    if any(s and s - timedelta(days=1) == day for s in (spring_this, spring_next)):
        out.append(_CHUXI)
    if day == _nth_weekday(day.year, 5, 6, 2):
        out.append(_MOTHER)
    if day == _nth_weekday(day.year, 6, 6, 3):
        out.append(_FATHER)
    if day == _nth_weekday(day.year, 11, 3, 4):
        out.append(_THANKS)
    return out


_DATE_RE = r"(\d{1,2})\s*(?:月|\.|/|-)\s*(\d{1,2})\s*(?:日|号)?"
_BIRTHDAY_RE = re.compile(r"生日[^0-9农]{0,8}" + _DATE_RE)
_ANNIVERSARY_RE = re.compile(r"(?:纪念日|在一起)[^0-9农]{0,8}" + _DATE_RE)


def personal_days(texts: Iterable[str]) -> dict[str, tuple[int, int]]:
    """The owner's birthday / anniversary (solar month, day) found in profile facts and core memories. A lunar
    birthday ("农历生日") is not understood and left out."""
    found: dict[str, tuple[int, int]] = {}
    for text in texts:
        for key, pattern in (("birthday", _BIRTHDAY_RE), ("anniversary", _ANNIVERSARY_RE)):
            m = pattern.search(text or "")
            if m and key not in found and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
                found[key] = (int(m.group(1)), int(m.group(2)))
    return found


def for_owner(day: date, kind: str, call: str | None, texts: list[str]) -> list[Holiday]:
    """Today's holidays for this relationship (kind) and owner."""
    groups = set(GROUPS.get(kind, {"all"}))
    if kind == "daughter":
        groups |= {"mother"} if any(c in (call or "") for c in _MOTHER_CALLS) else {"father"}
    joined = "\n".join(texts)
    out = [h for h in on_day(day) if h.group in groups
           or (h.group == "optional" and any(w in joined for w in h.words))]
    personal = personal_days(texts)
    if personal.get("birthday") == (day.month, day.day):
        out.insert(0, Holiday("birthday", "主人的生日", "birthday", "最重要：真心祝生日快乐"))
    if kind == "partner" and personal.get("anniversary") == (day.month, day.day):
        out.insert(0, Holiday("anniversary", "你们的纪念日", "anniversary"))
    return out


def enabled() -> bool:
    return env_bool("VIOLET_HOLIDAY_GREETING", False)


def owner_texts(db) -> list[str]:
    """Profile facts and core memories, where the owner's interests / birthday are written."""
    texts: list[str] = []
    try:
        texts += [f"{r.get('fact_key') or ''} {r.get('value') or ''}" for r in
                  db.fetch_all("SELECT fact_key, value FROM user_profile_facts WHERE status = 'active'")]
    except Exception:                                   # noqa: BLE001 -- an older database
        pass
    try:
        texts += [str(r.get("content") or "") for r in db.list_core_memories(limit=200)]
    except Exception:                                   # noqa: BLE001
        pass
    return texts


def today_for_channel(db, day: date, channel: str) -> tuple[list[Holiday], dict]:
    """(holidays, relationship) for the persona of this channel."""
    from ai.persona_templates import relationship
    rel = relationship(channel)
    return for_owner(day, rel.get("kind") or "close", rel.get("call"), owner_texts(db)), rel


def context_line(holidays: list[Holiday], rel: dict, greeted: bool) -> str:
    """The line for the chat context: today's holiday, and whether she has already said something."""
    if not holidays:
        return ""
    names = "、".join(h.name for h in holidays)
    hints = "；".join(f"{h.name}：{h.hint}" for h in holidays if h.hint)
    tail = "今天已经祝福过了，不要再专门祝一遍。" if greeted else "聊到合适的时候可以自然地说一句节日祝福，一次就够。"
    return f"今天是{names}（你们的关系：{rel.get('label') or '亲近'}）。" + (hints + "。" if hints else "") + tail
