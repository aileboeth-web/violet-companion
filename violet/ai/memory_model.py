"""Memory vocabulary and pure helpers (no DB, no LLM).

Kinds of event_memories rows
    fact        long-term fact about the user ("养了一只狗")
    event       something that happens / happened ("服务器出故障")
    background  passing chat context, low value, short-lived
    legacy      rows written before phase 2 (kind unknown; treated like an
                event whose progress is unknown)

Progress of an event
    planned  -> before event_time
    ongoing  -> in progress, user has not said it ended
    done / cancelled -> ended (only ever set from explicit user information)
    unknown  -> cannot tell (e.g. planned event whose time has passed)
    na       -> not applicable (facts, background)

Time passing never turns an event into "done"; at most into "unknown".
Commitments (explicit reminders / follow-ups) live in their own table and are
only created when the user's own words explicitly ask for it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ai.settings import env_bool, env_float, env_int, env_str, local_timezone, now_local

# Kept for backward compatibility; all calculations use local_timezone() (VIOLET_TIMEZONE).
CHINA_TZ = timezone(timedelta(hours=8))

MEMORY_KINDS = {"fact", "event", "background", "legacy"}
EVENT_KINDS = {"event", "legacy"}
EVENT_PROGRESS = {"planned", "ongoing", "done", "cancelled", "unknown", "na"}
ENDED_PROGRESS = {"done", "cancelled"}

KIND_LABELS = {"fact": "长期事实", "event": "事件", "background": "聊天背景", "legacy": "早期记录"}
PROGRESS_LABELS = {
    "planned": "计划中",
    "ongoing": "进行中",
    "done": "已结束",
    "cancelled": "已取消",
    "unknown": "进展未知",
    "na": "",
}


def recall_search_text(query: str) -> str:
    """Remove bounded conversational recall wrappers, keeping the topic verbatim.

    Only query_memory uses this: event writing/deduplication and the shared
    similarity metric retain their original semantics. No topic inference or
    fallback to unrelated memories when the wrapper contains no topic.
    """
    punctuation = " \t\r\n，。！？,.!?"
    text = (query or "").strip(punctuation)
    text = re.sub(r"^(?:用户)?(?:上次|之前|以前|先前)(?:说|提|聊)(?:过|到)?的", "", text)
    text = re.sub(r"(?:你)?(?:还)?记得(?:吗|么)$", "", text)
    return text.strip(punctuation)


def now_china() -> datetime:
    """Current time in the configured zone (name kept for compatibility)."""
    return now_local()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    zone = local_timezone()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


# ---------------------------------------------------------------------------
# Absolute time resolution
# ---------------------------------------------------------------------------

_DATE_OFFSETS = {
    "today": 0, "今天": 0, "今日": 0,
    "tomorrow": 1, "明天": 1, "明日": 1,
    "day_after_tomorrow": 2, "后天": 2,
    "yesterday": -1, "昨天": -1,
    "day_before_yesterday": -2, "前天": -2,
}
_PART_OF_DAY = {
    "morning": "09:00", "早上": "08:00", "上午": "10:00",
    "noon": "12:00", "中午": "12:00",
    "afternoon": "15:00", "下午": "15:00",
    "evening": "19:00", "傍晚": "18:00", "晚上": "20:00",
    "night": "21:00", "夜里": "22:00",
}


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    value = str(value).strip()
    value = _PART_OF_DAY.get(value, value)
    match = re.fullmatch(r"(\d{1,2})[:：](\d{2})", value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def resolve_time(
    absolute: str | None = None,
    date_hint: str | None = None,
    time_hint: str | None = None,
    now: datetime | None = None,
    default_time: str | None = None,
) -> str | None:
    """Turn an absolute ISO time or a (date_hint, time_hint) pair into an ISO string.

    date_hint: today/tomorrow/后天/... or YYYY-MM-DD. time_hint: HH:MM or a part
    of day (早上/晚上/...). Without a time, ``default_time`` is used; if that is
    None too, the end of that day (23:59) is used so "planned" only becomes
    "unknown" after the whole day has passed.
    """
    now = now or now_china()
    parsed = parse_time(absolute)
    if parsed is not None:
        return parsed.isoformat()
    if not date_hint:
        return None
    hint = str(date_hint).strip()
    if hint in _DATE_OFFSETS:
        day = (now + timedelta(days=_DATE_OFFSETS[hint])).date()
    else:
        try:
            day = datetime.strptime(hint, "%Y-%m-%d").date()
        except ValueError:
            return None
    hm = _parse_hhmm(time_hint) or _parse_hhmm(default_time) or (23, 59)
    return datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=local_timezone()).isoformat()


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class EventTimeMismatch(ValueError):
    """A model-written memory text whose date / clock contradicts the stored event_time (phase 9.1.1)."""


def is_date_only(value: str | None) -> bool:
    """Phase 9.1.1 (D-P91-1): an event time known only to the day is stored as "YYYY-MM-DD", never as an invented clock
    time (it used to become 23:59, which reached Chat as if the user had said it)."""
    return bool(value) and bool(_DATE_ONLY_RE.match(str(value).strip()))


def event_deadline(value: str | None) -> datetime | None:
    """When an event time counts as passed: the time itself, or the end of the day for a date-only time."""
    if is_date_only(value):
        day = datetime.strptime(str(value).strip(), "%Y-%m-%d")
        return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=local_timezone())
    return parse_time(value)


def event_time_label(value: str | None) -> str | None:
    """How an event time is shown to the models: the clock only when it is known."""
    if not value:
        return None
    if is_date_only(value):
        return f"{str(value).strip()}（具体时刻不知道）"
    parsed = parse_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else str(value)


def resolve_event_time(absolute: str | None = None, date_hint: str | None = None, time_hint: str | None = None,
                       now: datetime | None = None) -> str | None:
    """resolve_time for event memories: a day without a clock stays a day ("2026-11-11"), no 23:59 is made up.
    Phase 9.2.1: nor a part of day ("下午") -- its default clock (15:00) is not something the user said; the words stay
    in the memory's text."""
    if is_date_only(absolute):
        return str(absolute).strip()
    explicit = time_hint is not None and str(time_hint).strip() not in _PART_OF_DAY and _parse_hhmm(time_hint)
    if parse_time(absolute) is None and date_hint and not explicit:
        day = resolve_time(None, date_hint, "00:00", now=now)
        return day[:10] if day else None
    return resolve_time(absolute, date_hint, time_hint, now=now)


def invented_part_clock(event_time: str | None, user_text: str | None, summary: str | None) -> bool:
    """Phase 9.2.1: an event time whose clock is only the default of a part of day the user named ("下午" -> 15:00),
    with no clock time said by the user this turn nor written in the memory."""
    parsed = None if is_date_only(event_time) else parse_time(event_time)
    if parsed is None or f"{parsed:%H:%M}" not in set(_PART_OF_DAY.values()):
        return False
    if time_details(user_text)["clocks"] or time_details(summary)["clocks"]:
        return False
    return any(part in (user_text or "") for part in _PART_OF_DAY if not part.isascii())


# Phase 9.2.1: how settled a future event is. A condition ("要是没课就去") or a passing wish is not a plan.
INTENT_LEVELS = {"plan", "conditional", "wish"}
INTENT_LABELS = {"conditional": "有条件的打算，还没定", "wish": "只是随口的想法，不是计划"}
_USER_CONDITION_RE = re.compile(r"要是|如果|假如|假设|万一|的话|看情况|说不定|不一定")
_SUMMARY_CONDITION_RE = re.compile(r"若|如果|要是|假如|假设|万一|的话|看情况|视情况|说不定|不一定")
_CONDITION_CLAUSE_RE = re.compile(r"(?:若|如果|要是|假如|假设|万一)(.{1,24}?)(?:的话|就|，|,|。|$)")


def sounds_conditional(user_text: str | None, summary: str | None) -> bool:
    """Both the user's words and the model's own summary carry a condition (the summary alone could be a paraphrase
    slip; the user's words alone could belong to another sentence)."""
    return bool(_USER_CONDITION_RE.search(user_text or "")) and bool(_SUMMARY_CONDITION_RE.search(summary or ""))


def condition_clause(*texts: str | None) -> str | None:
    for text in texts:
        match = _CONDITION_CLAUSE_RE.search(text or "")
        if match and match.group(1).strip():
            return match.group(1).strip()[:40]
    return None


def precise_time_kept(current: str | None, new: str | None) -> str | None:
    """The event time after an update: a date-only value for the same day never replaces a known clock time."""
    if not new:
        return current
    if is_date_only(new) and current and not is_date_only(current):
        known = parse_time(current)
        if known is not None and known.date().isoformat() == str(new).strip():
            return current
    return new


# Phase 9.1.1 (D-P91-1): dates and clock times written in a memory's text. A rewrite (merge / update) must not drop
# them silently, and they must agree with the stored event_time.
_PERIOD_PM = ("下午", "晚上", "傍晚", "夜里")
_CLOCK_RE = re.compile(r"(上午|下午|中午|晚上|早上|傍晚|凌晨|夜里)?\s*(\d{1,2})[:：](\d{2})")
_CN_CLOCK_RE = re.compile(r"(上午|下午|中午|晚上|早上|傍晚|凌晨|夜里)?\s*([零〇一二两三四五六七八九十\d]{1,3})点(半|(\d{1,2})分?)?")
_DATE_RES = (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]"))
_TIME_CHANGE_RE = re.compile(r"改到|改成|改为|推迟|提前|延期|换到|挪到|调到|不是.{0,6}[点号日]")


def time_details(text: str | None) -> dict[str, dict[str, str]]:
    """{"clocks": {"15:00": "下午15:00"}, "dates": {"11-11": "2026-11-11"}} -- normalized value -> how it was written."""
    text = text or ""
    clocks: dict[str, str] = {}
    for match in _CLOCK_RE.finditer(text):
        hour, minute = int(match.group(2)), int(match.group(3))
        if match.group(1) in _PERIOD_PM and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            clocks.setdefault(f"{hour:02d}:{minute:02d}", match.group(0).strip())
    for match in _CN_CLOCK_RE.finditer(text):
        if not (match.group(1) or match.group(2).isdigit() or match.group(3)):
            continue                                  # a bare "一点 / 三点" is as often "a little / three points"
        hour = _cn_number(match.group(2))
        if hour is None:
            continue
        minute = 30 if match.group(3) == "半" else int(match.group(4)) if match.group(4) else 0
        if match.group(1) in _PERIOD_PM and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            clocks.setdefault(f"{hour:02d}:{minute:02d}", match.group(0).strip())
    dates: dict[str, str] = {}
    for pattern in _DATE_RES:
        for match in pattern.finditer(text):
            month, day = int(match.group(2)), int(match.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                dates.setdefault(f"{month:02d}-{day:02d}", match.group(0))
    return {"clocks": clocks, "dates": dates}


def dropped_time_details(old: str | None, new: str | None, user_text: str | None = None,
                         event_time: str | None = None) -> list[str]:
    """Dates / clock times the old text had that are lost: not in the new text and not held by the stored event_time
    (the structured time is the record of when; the text only has to keep what that field cannot hold). Empty when
    the user's own words of this turn give a time or say it changed (then the old one may rightly go)."""
    said = time_details(user_text)
    if said["clocks"] or said["dates"] or _TIME_CHANGE_RE.search(user_text or ""):
        return []
    held_date = held_clock = None
    if is_date_only(event_time):
        held_date = str(event_time).strip()[5:]
    elif parse_time(event_time) is not None:
        known = parse_time(event_time)
        held_date, held_clock = known.strftime("%m-%d"), known.strftime("%H:%M")
    before, after = time_details(old), time_details(new)
    dropped = [before["dates"][d] for d in before["dates"] if d not in after["dates"] and d != held_date]
    # a clock is held only together with its day (15:00 on 11-11 says nothing about 15:00 on another day)
    same_day = not before["dates"] or held_date in before["dates"]
    dropped += [before["clocks"][c] for c in before["clocks"]
                if c not in after["clocks"] and not (c == held_clock and same_day)]
    return dropped


def time_mismatch(content: str | None, event_time: str | None) -> str | None:
    """Why the text and the stored event_time disagree (a date / clock in the text that is not the stored one)."""
    if not event_time:
        return None
    details = time_details(content)
    if is_date_only(event_time):
        day = str(event_time).strip()
        if details["dates"] and day[5:] not in details["dates"]:
            return f"内容里的日期 {'、'.join(details['dates'].values())} 和记录的日期 {day} 不一致"
        return None
    known = parse_time(event_time)
    if known is None:
        return None
    if details["dates"] and known.strftime("%m-%d") not in details["dates"]:
        return f"内容里的日期 {'、'.join(details['dates'].values())} 和记录的时间 {known:%Y-%m-%d %H:%M} 不一致"
    if details["clocks"] and known.strftime("%H:%M") not in details["clocks"]:
        return f"内容里的时刻 {'、'.join(details['clocks'].values())} 和记录的时间 {known:%Y-%m-%d %H:%M} 不一致"
    return None


def default_valid_until(kind: str, progress: str, event_time: str | None, now: datetime | None = None) -> str | None:
    """How long a memory stays "current" (it is not deleted afterwards, only marked expired)."""
    now = now or now_china()
    if kind == "fact":
        return None
    if kind == "background":
        return (now + timedelta(days=env_float("VIOLET_BACKGROUND_VALID_DAYS", 3))).isoformat()
    if progress in ENDED_PROGRESS:
        return (now + timedelta(days=env_float("VIOLET_ENDED_EVENT_VALID_DAYS", 7))).isoformat()
    base = max(now, event_deadline(event_time) or now)
    return (base + timedelta(days=env_float("VIOLET_EVENT_VALID_DAYS", 14))).isoformat()


# ---------------------------------------------------------------------------
# Explicit request detection (authorisation for commitments)
# ---------------------------------------------------------------------------

_EXPLICIT_REQUEST_RE = re.compile(
    r"(提醒|记得叫我|记得喊我|叫我一下|喊我一下|到时候?(问|叫|喊|催|告诉)我|别让我忘|帮我记着|"
    r"记得问我|问问我|催我|跟进一下|跟我确认|remind me|follow up)",
    re.IGNORECASE,
)
_NEGATED_REQUEST_RE = re.compile(r"(不用|不要|别|无需|不必|甭)(再)?(提醒|叫|喊|催|问)")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def check_explicit_request(evidence: str | None, user_message: str | None) -> tuple[bool, str]:
    """A commitment needs the user's own words (verbatim) explicitly asking for it."""
    evidence_s, message_s = _squash(evidence or ""), _squash(user_message or "")
    if not evidence_s:
        return False, "missing_evidence"
    if evidence_s not in message_s:
        return False, "evidence_not_in_user_message"
    if _NEGATED_REQUEST_RE.search(evidence_s):
        return False, "request_is_negated"
    if not _EXPLICIT_REQUEST_RE.search(evidence_s):
        return False, "not_an_explicit_request"
    return True, "explicit_request"


# ---------------------------------------------------------------------------
# User engagement after Violet raised something
# ---------------------------------------------------------------------------

_ACK_ONLY = {
    "嗯", "嗯嗯", "恩", "哦", "噢", "喔", "好", "好的", "好吧", "行", "行吧", "ok", "okay", "k",
    "哈", "哈哈", "哈哈哈", "呵呵", "知道了", "收到", "没事", "还行", "还好", "随便", "啊", "呃",
    "emmm", "emm", "对", "是", "是的", "嗯呢", "1",
}
_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def is_engaged_reply(text: str | None) -> bool:
    """Whether a user turn shows willingness to keep talking (vs. a bare acknowledgement)."""
    cleaned = _STRIP_RE.sub("", (text or "").lower())
    if not cleaned:
        return False
    if cleaned in _ACK_ONLY:
        return False
    return len(cleaned) > env_int("VIOLET_SHORT_REPLY_MAX_CHARS", 3)


# Short but warm / responsive replies: not "engaged", but not a brush-off either.
_WARM_SHORT = {
    "哈", "哈哈", "哈哈哈", "哈哈哈哈", "嘻嘻", "嘿嘿", "hhh", "hhhh", "233", "好呀", "好哒", "好滴", "真的吗", "真的假的",
    "是吗", "然后呢", "对呀", "对啊", "可以呀", "好耶", "冲", "笑死", "绝了", "哇", "哇哦", "666",
}


def classify_reply(text: str | None) -> str:
    """'engaged' | 'neutral' | 'disengaged'.

    Only bare acknowledgements ("嗯/哦/好/行"), empty replies count as
    disengaged. Laughing, quick warm reactions and questions back are neutral or
    engaged, so a short but friendly reply does not cool a topic.
    """
    raw = (text or "").strip()
    cleaned = _STRIP_RE.sub("", raw.lower())
    if cleaned in _WARM_SHORT:
        return "neutral"
    if is_engaged_reply(raw):
        return "engaged"
    if raw.endswith(("?", "？")):
        return "neutral"
    if not cleaned or cleaned in _ACK_ONLY:
        return "disengaged"
    return "neutral"


# ---------------------------------------------------------------------------
# Restricted-topic guard for generated proactive text
# ---------------------------------------------------------------------------

_STOP_BIGRAMS = {"用户", "户的", "一个", "今天", "明天", "已经", "我们", "你的", "自己", "这个", "那个", "什么", "没有", "可以"}
_CJK_RUN = re.compile(r"[一-鿿]+")


def _bigrams(text: str) -> set[str]:
    grams: set[str] = set()
    # "用户" starts almost every memory summary; it says nothing about the topic.
    for run in _CJK_RUN.findall((text or "").replace("用户", " ")):
        grams.update(run[i:i + 2] for i in range(len(run) - 1))
    words = re.findall(r"[A-Za-z0-9]{3,}", (text or "").lower())
    grams.update(words)
    return grams - _STOP_BIGRAMS


# Words every event summary uses for its outcome or mood; sharing one of them says nothing about
# being the same event ("答辩结束了" and "面试结束了" share 结束).
_OUTCOME_BIGRAMS = {"将于", "参加", "结束", "顺利", "完成", "取消", "延期", "改期", "进展", "感到", "有些", "有点",
                    "紧张", "开心", "难过", "并对", "对此", "过程", "感觉", "还是", "终于", "已结", "束了", "成了"}


def shared_topic_terms(text: str, other: str) -> list[str]:
    """Distinctive word pairs both texts contain (phase 7 B.6.1: a reported outcome and an open event
    that may be the same thing, e.g. 答辩结束了 / 将于10-21参加天文观测课程答辩 -> ["答辩"]).
    Used only to ASK for a decision, never to merge."""
    grams = {g for g in _bigrams(text) & _bigrams(other) if not g.isdigit() and not g.isascii()}
    return sorted(grams - _OUTCOME_BIGRAMS)


# Phase 9.1 (A2): word pairs that say nothing about WHICH memory a message is about -- times, fillers, and pet names
# (a "宝贝晚安" must not pull up intimate memories that happen to contain 宝贝). Used only by user_word_recall.
RECALL_STOP_BIGRAMS = {
    "上午", "下午", "中午", "晚上", "早上", "周末", "今晚", "明晚", "昨晚", "昨天", "前天", "后天", "最近", "之前", "以后",
    "以前", "上次", "下次", "时候", "现在", "刚才", "刚刚", "一下", "一起", "觉得", "知道", "就是", "不是", "真的",
    "因为", "所以", "但是", "然后", "怎么", "为什么", "东西", "事情", "这样", "那样", "一点", "一些", "还有", "这么",
    "那么", "好像", "可能", "应该", "开始", "起来", "出来", "一直", "今年", "明年", "去年", "下周", "上周", "这周",
    "星期", "个月", "一天", "两天", "几天", "我的", "他的", "她的", "你们", "他们", "她们", "大家", "这些", "那些",
    "哈哈", "嘻嘻", "呜呜", "嘿嘿", "宝贝", "宝宝", "亲爱", "爱的", "亲亲", "抱抱", "晚安", "早安", "记得", "还记",
    "说过", "提过", "告诉", "跟你", "和你", "给你", "你说", "我说", "说的", "过的", "的那", "那个", "这个", "什么",
    "一次", "那次", "那天", "这次", "对了", "好的", "是的", "没事", "不过", "而且", "如果", "的话", "已经", "还是",
}


def recall_terms(text: str, content: str) -> list[str]:
    """Distinctive words a user message and a memory share (phase 9.1, A2): shared_topic_terms minus the stop list."""
    return [t for t in shared_topic_terms(text, content) if t not in RECALL_STOP_BIGRAMS]


def touches_topic(text: str, topic_content: str) -> bool:
    """Whether ``text`` clearly talks about ``topic_content`` (used only to BLOCK sending)."""
    topic = _bigrams(topic_content)
    if not topic:
        return False
    hits = topic & _bigrams(text)
    ratio = len(hits) / len(topic)
    return len(hits) >= 2 and ratio >= env_float("VIOLET_TOPIC_GUARD_MIN_RATIO", 0.25)


def mentions_topic(text: str, topic_content: str) -> bool:
    """Lenient check that ``text`` is about ``topic_content`` (used to notice the USER raised it).

    Short topics ("在学吉他") have few distinctive bigrams, so one shared bigram
    is enough for them; longer topics use the stricter touches_topic rule.
    """
    topic = _bigrams(topic_content)
    if not topic:
        return False
    hits = topic & _bigrams(text)
    if len(topic) <= env_int("VIOLET_SHORT_TOPIC_MAX_BIGRAMS", 4):
        return len(hits) >= 1
    return touches_topic(text, topic_content)


def commitment_default_time() -> str:
    return env_str("VIOLET_COMMITMENT_DEFAULT_TIME", "09:00")


# ---------------------------------------------------------------------------
# Explicit "提醒我…" requests settled before the reply (phase 7 B.10.1)
# ---------------------------------------------------------------------------
# Only one narrow phrasing is settled before the reply: "提醒我" with a date word. The reply
# can then say what really happened (saved, already there, or not saved and why). Every other explicit
# request word (催我 / 问问我 / 记得叫我 …) stays with MemoryAgent after the reply, as before.

REMINDER_TRIGGER_RE = re.compile(r"提醒(?:一下)?我")
_REMINDER_PAST_RE = re.compile(r"提醒(?:一下)?我(?:了|过)|提醒(?:了|过)我|没(?:有)?提醒")
_REMINDER_PAST_DATE_RE = re.compile(r"昨天|前天|上次|刚才")
_CLAUSE_SPLIT_RE = re.compile(r"[，,。!！?？;；\n]+")
_REMINDER_DATES = {"今天": ("今天", None), "今日": ("今天", None), "今早": ("今天", "早上"), "今晚": ("今天", "晚上"),
                   "明天": ("明天", None), "明日": ("明天", None), "明早": ("明天", "早上"), "明晚": ("明天", "晚上"),
                   "后天": ("后天", None)}
_REMINDER_DATE_RE = re.compile("|".join(_REMINDER_DATES))
_DAY_PART_RE = r"凌晨|早上|早晨|清早|上午|中午|下午|傍晚|晚上|夜里"
_CN_NUM = r"[0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3}"
_REMINDER_CLOCK_RE = re.compile(
    rf"(?P<part>{_DAY_PART_RE})?\s*(?:(?P<h24>[0-9]{{1,2}})[:：](?P<m24>[0-9]{{2}})"
    rf"|(?P<hour>{_CN_NUM})\s*[点时](?:\s*(?P<half>半)|\s*(?P<quarter>一刻|三刻)|\s*(?P<minute>{_CN_NUM})\s*分?)?)")
_DAY_PART_ONLY_RE = re.compile(_DAY_PART_RE)
_PART_DEFAULTS = {"凌晨": "05:00", "早晨": "08:00", "清早": "07:00"}
_LEADING_FILLER_RE = re.compile(r"^(?:我|你|要|得|需要|该|去|就|再|能|可以|可不可以|能不能|麻烦|请|记得|帮我|到时候?)+")
_TRAILING_FILLER_RE = re.compile(r"(?:一下|吧|哦|噢|啊|呀|哈|啦|嘛|呗|喔|哟|吗|么|呢|好吗|好不好|行吗)+$")
_REMINDER_REASONS = {
    "no_date": "没能确定是哪一天（现在只能自动设“今天 / 明天 / 后天”这类说法的提醒）",
    "ambiguous_time": "时间的说法不止一个或说不通，没法确定",
    "no_content": "没听出要提醒什么事",
    "too_long": "这句话太长，没法确定要提醒的到底是哪件事",
    "time_passed": "这个时间已经过去了",
    "several_requests": "一句话里有好几个提醒",
    "storage_error": "保存的时候出错了",
}


def reminder_before_reply_enabled() -> bool:
    return env_bool("VIOLET_REMINDER_BEFORE_REPLY", True)


def reminder_failure_text(reason: str) -> str:
    return _REMINDER_REASONS.get(reason, "没能确定提醒的内容和时间")


def _cn_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "十" in text:
        tens, _, ones = text.partition("十")
        if (tens and tens not in digits) or (ones and ones not in digits):
            return None
        return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
    return digits[text] if len(text) == 1 and text in digits else None


def _clock_of(match: re.Match, part: str | None) -> tuple[int, int] | None:
    """(hour, minute) of one clock expression; None when it does not make sense."""
    if match.group("h24"):
        hour, minute = int(match.group("h24")), int(match.group("m24"))
    else:
        hour = _cn_number(match.group("hour"))
        if hour is None:
            return None
        minute = 30 if match.group("half") else {"一刻": 15, "三刻": 45}.get(match.group("quarter") or "", 0)
        if match.group("minute"):
            minute = _cn_number(match.group("minute"))
            if minute is None:
                return None
    if part in ("下午", "傍晚", "晚上", "夜里"):
        if hour == 12 or hour > 23:
            return None
        hour = hour + 12 if hour < 12 else hour
    elif part == "中午":
        hour = hour + 12 if hour in (1, 2) else hour
        if hour not in (11, 12, 13, 14):
            return None
    elif part in ("凌晨", "早上", "早晨", "清早", "上午") and hour > 12:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_reminder_request(text: str | None, now: datetime | None = None) -> dict:
    """Deterministic reading of an explicit "提醒我…" request. Pure: no DB, no LLM.

    requested -- the message is such a request (the step before the reply then owns it, whatever the result);
    ok        -- content, due time and evidence were all found and the due time is in the future.
    Anything unclear is ``ok=False`` with a reason: the caller must then NOT set a reminder."""
    text = (text or "").strip()
    result = {"requested": False, "ok": False, "reason": "not_a_request", "content": None, "due_at": None,
              "evidence": None}
    triggers = REMINDER_TRIGGER_RE.findall(text)
    if not triggers:
        return result
    clause = next(c.strip() for c in _CLAUSE_SPLIT_RE.split(text) if REMINDER_TRIGGER_RE.search(c))
    explicit, why = check_explicit_request(clause, text)
    if not explicit:
        return {**result, "reason": why}
    if _REMINDER_PAST_RE.search(text) or _REMINDER_PAST_DATE_RE.search(text):
        return {**result, "reason": "refers_to_the_past"}
    result.update(requested=True, evidence=clause)
    if len(triggers) > 1:
        return {**result, "reason": "several_requests"}
    dates = {_REMINDER_DATES[d] for d in _REMINDER_DATE_RE.findall(text)}
    if not dates:
        return {**result, "reason": "no_date"}
    clocks = [m for m in _REMINDER_CLOCK_RE.finditer(text) if m.group("h24") or m.group("hour")]
    parts = set(_DAY_PART_ONLY_RE.findall(text)) | {p for _, p in dates if p}
    if len({d for d, _ in dates}) > 1 or len(clocks) > 1 or len(parts) > 1:
        return {**result, "reason": "ambiguous_time"}
    day_word = next(iter(dates))[0]
    part = next(iter(parts)) if parts else None
    if clocks:
        hm = _clock_of(clocks[0], clocks[0].group("part") or part)
        if hm is None:
            return {**result, "reason": "ambiguous_time"}
        clock = f"{hm[0]:02d}:{hm[1]:02d}"
    elif part:
        clock = _PART_DEFAULTS.get(part) or _PART_OF_DAY[part]
    else:
        clock = commitment_default_time()
    now = now or now_china()
    due_at = resolve_time(None, day_word, clock, now=now, default_time=commitment_default_time())
    if not due_at:
        return {**result, "reason": "ambiguous_time"}
    rest = text
    for m in sorted(clocks, key=lambda m: m.start(), reverse=True):
        rest = rest[:m.start()] + " " + rest[m.end():]
    rest = _DAY_PART_ONLY_RE.sub(" ", _REMINDER_DATE_RE.sub(" ", rest))
    rest = re.sub(r"(?:记得|帮我|麻烦你?|请你?|你)?提醒(?:一下)?我(?:一下)?", " ", rest)
    pieces = [re.sub(r"\s+", "", c) for c in _CLAUSE_SPLIT_RE.split(rest)]
    pieces = [_TRAILING_FILLER_RE.sub("", _LEADING_FILLER_RE.sub("", p)) for p in pieces if p]
    content = _TRAILING_FILLER_RE.sub("", _LEADING_FILLER_RE.sub("", "".join(pieces)))
    if not content:
        return {**result, "reason": "no_content"}
    if len(content) > env_int("VIOLET_REMINDER_MAX_CONTENT_CHARS", 20):
        return {**result, "reason": "too_long"}
    if parse_time(due_at) <= now:
        return {**result, "content": content, "due_at": due_at, "reason": "time_passed"}
    return {**result, "ok": True, "reason": "parsed", "content": content, "due_at": due_at}
