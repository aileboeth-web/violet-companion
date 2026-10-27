"""Phase 8.9: Violet's ordinary day -- small, mundane things beside the long life threads (ai/life_engine.py).

Without it Violet has nothing recorded on most days, so "你今天干嘛了" is answered from the persona summary
("听了会儿音乐，也翻了几页书") -- an unrecorded experience the reply guard cannot see (no subject, no "我").

* No model: items come from persona/life_config.json "daily" and a seeded RNG (salt + date), so a day is the
  same every time it is looked at. 0-3 items per day, in fixed time slots; a quiet day is allowed.
* An item becomes a recorded event (source life_daily) only once its start time has passed, through the same
  ConsistencyGuard.check_event as every life event (time conflicts, the user's own experiences, persona rules);
  a rejected item is simply not part of the day. Items still ahead are plans of today, never stored as done.
* Lazily materialised when Violet's context is built (today and the last ``backfill_days``), oldest day first,
  so what she says about yesterday stays the same tomorrow. Idempotent by uid ``daily:<date>:<slot>``.
* Not counted in the life engine's weekly budget, never offered as a proactive share (too small to send).
"""
from __future__ import annotations

import logging
import random
import re
from datetime import date, datetime, timedelta
from typing import Any

from ai.consistency import ConsistencyGuard
from ai.life_store import LifeStore
from ai.memory_model import mentions_topic, parse_time
from ai.persona import life_config
from ai.settings import env_bool

logger = logging.getLogger("violet.life.daily")

SOURCE = "life_daily"
_TITLE_RE = re.compile(r"《([^》]{1,30})》")
_QUANTIFIER_RE = re.compile(r"^(几首|几条|几本|一张|一位|一些|一首|一本)")


class DailyLife:
    def __init__(self, db, config: dict[str, Any] | None = None):
        self.db = db
        self.config = config or life_config()
        self.daily = self.config.get("daily") or {}
        self.store = LifeStore(db)

    def enabled(self) -> bool:
        return bool(self.daily.get("enabled", False) and self.daily.get("slots")) and env_bool("VIOLET_DAILY_LIFE", True)

    # ------------------------------------------------------------------
    def plan_for(self, day: date, tz) -> list[dict[str, Any]]:
        """The whole day, decided from the date (and what the days before it already had)."""
        # fixed random-seed string (kept from before the rename to Violet, so a given date keeps its day)
        rng = random.Random(f"{self.daily.get('seed_salt', 'eve-day')}:{day.isoformat()}")
        if rng.random() < float(self.daily.get("quiet_day_probability", 0.15)):
            return []
        slots = list(self.daily["slots"].items())
        lo, hi = (list(self.daily.get("items_per_day") or [1, 3]) + [3])[:2]
        count = min(rng.randint(int(lo), int(hi)), len(slots))
        day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
        window = timedelta(days=float(self.daily.get("repeat_window_days", 5)))
        recent = {e["content"] for e in self.store.events(("happened",), since=(day_start - window).isoformat(),
                                                          until=day_start.isoformat())
                  if e["source"] == SOURCE}
        minutes = int(self.daily.get("duration_minutes", 45))
        items = []
        for index in sorted(rng.sample(range(len(slots)), count)):
            name, spec = slots[index]
            options = [i for i in spec.get("items") or [] if i["done"] not in recent] or list(spec.get("items") or [])
            if not options:
                continue
            choice = rng.choice(options)
            first, last = spec.get("hours") or [9, 21]
            start = day_start.replace(hour=rng.randint(int(first), int(last) - 1), minute=rng.choice((0, 15, 30, 45)))
            items.append({"slot": name, "label": spec.get("label", name), "done": choice["done"],
                          "doing": choice.get("doing") or choice["done"], "start": start, "minutes": minutes,
                          "uid": f"daily:{day.isoformat()}:{name}"})
        return items

    def _messages(self, day: date) -> list[tuple[datetime, str]]:
        """(time, role) of the chat messages of ``day`` -- Violet's real activity that day."""
        rows = self.db.fetch_all("SELECT role, created_at FROM messages WHERE substr(created_at, 1, 10) = ?",
                                 (day.isoformat(),))
        return sorted((parse_time(r["created_at"]), r["role"]) for r in rows if r.get("created_at"))

    def chatting_at(self, start: datetime, messages: list[tuple[datetime, str]]) -> bool:
        """Phase 8.9.1: Violet was in the middle of a conversation when the item would start (messages shortly before
        AND shortly after it), so she was not off napping or at the café then."""
        gap = timedelta(minutes=float(self.daily.get("chat_gap_minutes", 20)))
        return (any(start - gap <= t < start for t, _ in messages)
                and any(start <= t <= start + gap for t, _ in messages))

    def day(self, day: date, now: datetime) -> dict[str, Any]:
        """{'day', 'done': [event rows], 'doing': item | None, 'later': [items]} -- stores what has started."""
        view: dict[str, Any] = {"day": day, "done": [], "doing": None, "later": [], "quiet": False}
        plan = self.plan_for(day, now.tzinfo)
        view["quiet"] = not plan
        messages = self._messages(day)
        # the day is only looked at while a user message is being answered (ContextEngine), and that message may
        # not be in the log yet (the IntentAgent's context is built first): "now" is chat activity too
        activity = messages + ([(now, "user")] if now.date() == day else [])
        for item in plan:
            end = item["start"] + timedelta(minutes=item["minutes"])
            stored = self.store.event_by_uid(item["uid"])
            if stored is None and item["start"] <= now:
                # an item that would start while she was chatting did not happen (never stored; already stored
                # events are history and are kept as they are)
                if self.chatting_at(item["start"], activity):
                    logger.info("daily item %s skipped: Violet was chatting at %s", item["uid"], item["start"])
                    continue
                stored = self._store(item, now)
                if stored is None:
                    continue                                   # rejected: simply not part of the day
            if stored is not None:
                if stored["status"] != "happened":
                    continue
                # once Violet has replied since it started she has stopped to chat: it is done, not "正在做"
                replied = any(item["start"] <= t <= now and role != "user" for t, role in messages)
                if now < end and not replied:
                    view["doing"] = {**item, "event": stored}
                else:
                    view["done"].append({**stored, "slot_label": item["label"]})
            else:
                view["later"].append(item)
        return view

    def recent(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Today and the days before it (oldest first); empty when disabled."""
        if not self.enabled():
            return []
        now = now or parse_time(self.db.now())
        days = int(self.daily.get("backfill_days", 2))
        out = []
        for back in range(days, -1, -1):
            try:
                out.append(self.day(now.date() - timedelta(days=back), now))
            except Exception:
                logger.exception("daily life for %s unavailable", now.date() - timedelta(days=back))
        return out

    def _store(self, item: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        proposal = {"thread_id": None, "plan_id": None, "event_type": "daily", "content": item["done"],
                    "occurred_at": item["start"].isoformat(), "duration_minutes": item["minutes"],
                    "source": SOURCE, "source_detail": {"slot": item["slot"]}, "completes_plan": False}
        violations = ConsistencyGuard(self.db, config=self.config).check_event(proposal, now.isoformat())
        if violations:
            logger.info("daily item %s skipped: %s", item["uid"], [v.check for v in violations])
            return None
        self.store.insert_event(uid=item["uid"], thread_id=None, plan_id=None, event_type="daily", content=item["done"],
                                occurred_at=proposal["occurred_at"], duration_minutes=item["minutes"], source=SOURCE,
                                source_detail=proposal["source_detail"], actor=SOURCE)
        return self.store.event_by_uid(item["uid"])


# ----------------------------------------------------------------------
# What a life line is "about": book titles, the configured item ("钢琴曲") and its details ("《月光》")
# ----------------------------------------------------------------------
def item_details(content: str, config: dict[str, Any] | None = None) -> str:
    """Concrete details configured for an item mentioned in ``content`` ("几首钢琴曲" -> 德彪西的《月光》…)."""
    config = config if config is not None else life_config()
    for spec in (config.get("interests") or {}).values():
        for item, detail in (spec.get("details") or {}).items():
            if item in (content or ""):
                return detail
    return ""


def life_terms(content: str, config: dict[str, Any] | None = None) -> list[str]:
    config = config if config is not None else life_config()
    content = content or ""
    terms = _TITLE_RE.findall(content)
    for spec in (config.get("interests") or {}).values():
        for item in spec.get("items") or []:
            if item in content:
                bare = _QUANTIFIER_RE.sub("", item)
                terms.append(bare if len(bare) >= 2 else item)
    terms += _TITLE_RE.findall(item_details(content, config))
    return [t for t in dict.fromkeys(terms) if len(t) >= 2]


def mentions_life(text: str, content: str, config: dict[str, Any] | None = None) -> bool:
    """``text`` talks about this life line: a shared title / item, or the usual topic overlap."""
    text = text or ""
    if not text or not content:
        return False
    return any(t in text for t in life_terms(content, config)) or mentions_topic(text, content)


# ----------------------------------------------------------------------
# Phase 8.9.1: what earlier conversations already covered (derived from the message log, nothing stored)
# ----------------------------------------------------------------------
# The user asking about "your book" while Violet had exactly one book going is about that book.
YOUR_THING_RE = {"reading": re.compile(r"你.{0,6}(书|小说)"), "music": re.compile(r"你.{0,6}(曲子|钢琴曲|歌|专辑)"),
                 "notes": re.compile(r"你.{0,6}(笔记|本子)")}
REPLY_WITHIN = timedelta(hours=3)


def earlier_messages(db, before: datetime, limit: int = 300) -> list[dict[str, Any]]:
    """Messages before ``before`` (oldest first) as {'at', 'role', 'text'}."""
    rows = db.fetch_all("SELECT role, content, created_at FROM messages ORDER BY created_at DESC, id DESC LIMIT ?",
                        (limit,))
    out = [{"at": parse_time(r["created_at"]), "role": r["role"], "text": r["content"] or ""}
           for r in reversed(rows) if r.get("created_at")]       # phase 8.9.8: equal times keep the order they were sent in
    return sorted((m for m in out if m["at"] < before), key=lambda m: m["at"])


TALK_WITHIN = timedelta(days=2)
TALK_GAP = timedelta(hours=3)


def last_talk(db, content: str, now: datetime, config: dict[str, Any] | None = None, tail: int = 4) -> dict[str, Any] | None:
    """Phase 8.9.8: how the latest conversation about one of Violet's plans ended, from the message log (nothing stored,
    nothing inferred about it having happened). The conversation is the one with the latest message that mentions the
    plan; it runs on while messages follow each other within TALK_GAP; its last ``tail`` messages are returned.

    In the real free chat (local/free_chat/cys-close-1) the plan "周末想去书店挑一本新书" was talked over until "那正好，
    周末一起找吧 / 我去书店的时候拍封面给你"; the next morning Violet wrote "一直没定下来到底去不去"."""
    rows = [m for m in earlier_messages(db, now + timedelta(seconds=1)) if m["at"] >= now - TALK_WITHIN]
    hits = [i for i, m in enumerate(rows) if mentions_life(m["text"], content, config)]
    if not hits:
        return None
    end = hits[-1]
    while end + 1 < len(rows) and rows[end + 1]["at"] - rows[end]["at"] < TALK_GAP:
        end += 1
    part = rows[hits[-1]:end + 1][-tail:]
    return {"at": part[-1]["at"], "lines": [(m["role"], m["text"]) for m in part]}


def talk_note(talk: dict[str, Any] | None) -> str:
    """'上次聊到（11-06 23:50）最后是这样说的：…' -- the words, so the model continues from them; or ''."""
    if not talk:
        return ""
    said = " / ".join(f"{'对方' if role == 'user' else '你'}：“{text.strip()[:40]}”" for role, text in talk["lines"])
    return (f"上次聊到（{talk['at']:%m-%d %H:%M}）最后是这样说的：{said}。按聊到的往下说，别当成还没商量过；"
            "还没做的也别说成做了")


def violet_told(content: str, since: datetime, messages: list[dict[str, Any]], config: dict[str, Any] | None = None) -> bool:
    """Violet herself talked about this life line in an earlier message written after it happened."""
    return any(m["role"] != "user" and m["at"] >= since and mentions_life(m["text"], content, config) for m in messages)


def thread_discussed(thread: dict[str, Any], threads: list[dict[str, Any]], messages: list[dict[str, Any]],
                     config: dict[str, Any] | None = None) -> bool:
    """An earlier conversation was about this thread while it was going on: a message names it (title / item), or
    the user asked about "your book" and Violet answered while this was her only thread of that kind."""
    start = parse_time(thread["started_at"])
    end = parse_time(thread["ended_at"]) if thread.get("ended_at") else None
    active = lambda th, t: parse_time(th["started_at"]) <= t and (not th.get("ended_at") or t <= parse_time(th["ended_at"]))
    terms = life_terms(thread["title"], config)
    pattern = YOUR_THING_RE.get(thread["interest_key"])
    for i, m in enumerate(messages):
        if m["at"] < start or (end and m["at"] > end):
            continue
        if any(t in m["text"] for t in terms):
            return True
        if m["role"] == "user" and pattern and pattern.search(m["text"]):
            answered = any(r["role"] != "user" and m["at"] <= r["at"] <= m["at"] + REPLY_WITHIN for r in messages[i + 1:i + 3])
            unique = not any(o["id"] != thread["id"] and o["interest_key"] == thread["interest_key"] and active(o, m["at"])
                             for o in threads)
            if answered and unique:
                return True
    return False
