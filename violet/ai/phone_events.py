"""The phone's events as they happen (2026-11-03, user: 手机主动推送 -- e.g. the first unlock in the morning).

Violet MCP 1.4.1 keeps a short in-memory list of what happened on the phone (unlock after the screen was off,
charger plugged / unplugged, headphones, arriving at / leaving a Wi-Fi place the owner named). Violet asks for the new
ones once a minute (tool phone_events, `after` = the last seq seen) -- no relay change, nothing piles up while the
phone is offline.

Two uses:
  * recent_text(): the last 30 minutes for the phone line (ai/phone_state.py), e.g. "07:12 解锁（之前熄屏 7 小时）".
  * a "moment": an event worth a word now -- the first unlock of the morning after a long night, arriving at a named
    place. The scheduler then runs the proactive decision right away (instead of at its random time), with the
    moment in the context; every other rule (quiet hours, daily cap, busy, cold streak, ...) still applies and the
    decision may still be "not now". A moment counts for 20 minutes.

VIOLET_PHONE_EVENTS (default on, needs the phone's "手机上的动静" switch). When the phone has no such tool the poll
pauses for 30 minutes.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from ai.settings import env_bool

logger = logging.getLogger("violet.phone_events")

POLL_SECONDS = 60
NO_TOOL_PAUSE = 30 * 60
MOMENT_MINUTES = 20
RECENT_MINUTES = 30
MORNING = range(5, 12)
LONG_NIGHT_MINUTES = 240

_state: dict[str, Any] = {"epoch": None, "seq": 0, "next_poll": 0.0, "recent": [], "moment": None, "primed": False}
_lock = threading.Lock()


def enabled() -> bool:
    return env_bool("VIOLET_PHONE_EVENTS", True)


def _at(event: dict[str, Any], now: datetime) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(((event.get("at") or {}).get("iso")) or ""))
    except (TypeError, ValueError):
        return None
    return at.astimezone(now.tzinfo) if now.tzinfo else at


def describe(event: dict[str, Any], at: datetime) -> str:
    detail = event.get("detail") or {}
    hhmm = at.strftime("%H:%M")
    kind = event.get("type")
    if kind == "unlock":
        off = detail.get("screenOffMinutes")
        if isinstance(off, int) and off >= 60:
            return f"{hhmm} 解锁手机（之前熄屏约 {off // 60} 小时）"
        return f"{hhmm} 解锁手机"
    words = {"charger_connected": "插上充电器", "charger_disconnected": "拔了充电器",
             "headphones_connected": "戴上耳机", "headphones_disconnected": "摘下耳机"}
    if kind in words:
        return f"{hhmm} {words[kind]}"
    if kind == "place_arrived":
        return f"{hhmm} 到了「{detail.get('place')}」"
    if kind == "place_left":
        return f"{hhmm} 离开了「{detail.get('place')}」"
    return ""                                          # screen_on / screen_off: too frequent to be worth a line


def moment_of(event: dict[str, Any], at: datetime) -> str | None:
    """An event worth considering a message for right now (None: only context)."""
    detail = event.get("detail") or {}
    if event.get("type") == "unlock" and at.hour in MORNING:
        off = detail.get("screenOffMinutes")
        if isinstance(off, int) and off >= LONG_NIGHT_MINUTES:
            return f"主人刚起床：熄屏约 {off // 60} 小时后，{at.strftime('%H:%M')} 第一次解锁手机。可以考虑道个早安（不要说你看到了手机）。"
    if event.get("type") == "place_arrived" and detail.get("place"):
        return f"主人 {at.strftime('%H:%M')} 刚到「{detail['place']}」。可以考虑问候一句（不要说你看到了手机）。"
    return None


def take(events: list[dict[str, Any]], now: datetime) -> None:
    """Fold newly fetched events into the recent list and the pending moment."""
    with _lock:
        for e in events:
            at = _at(e, now)
            if at is None or now - at > timedelta(minutes=RECENT_MINUTES):
                continue
            text = describe(e, at)
            if text:
                _state["recent"].append((at, text))
            moment = moment_of(e, at)
            if moment and now - at <= timedelta(minutes=MOMENT_MINUTES):
                _state["moment"] = (at, moment)
        _state["recent"] = [(a, t) for a, t in _state["recent"] if now - a <= timedelta(minutes=RECENT_MINUTES)][-8:]


def recent_text(now: datetime) -> str:
    with _lock:
        items = [t for a, t in _state["recent"] if now - a <= timedelta(minutes=RECENT_MINUTES)]
    return ("最近的动静：" + "、".join(items[-5:])) if items else ""


def pending_moment(now: datetime) -> str | None:
    with _lock:
        m = _state["moment"]
        if m and now - m[0] > timedelta(minutes=MOMENT_MINUTES):
            _state["moment"] = m = None
        return m[1] if m else None


def consume_moment() -> None:
    with _lock:
        _state["moment"] = None


def poll(now: datetime, force: bool = False) -> int:
    """Fetch the new events (at most once a minute). Returns how many came. Never raises."""
    if not enabled():
        return 0
    with _lock:
        if not force and time.monotonic() < _state["next_poll"]:
            return 0
        _state["next_poll"] = time.monotonic() + POLL_SECONDS
        after = _state["seq"]
    from ai.phone_state import _read
    try:
        data = _read({"phone_events": {"after": after, "limit": 100}}).get("phone_events")
    except Exception:                                   # noqa: BLE001 -- the phone is optional
        data = None
    if not isinstance(data, dict):
        with _lock:
            _state["next_poll"] = time.monotonic() + NO_TOOL_PAUSE
        return 0
    with _lock:
        known = _state["epoch"]
    if known is not None and data.get("epoch") != known and after:
        # the phone's service restarted and numbers from 1 again: everything it has now is new to us
        try:
            data = _read({"phone_events": {"after": 0, "limit": 100}}).get("phone_events") or data
        except Exception:                               # noqa: BLE001
            pass
    events = [e for e in data.get("events") or [] if isinstance(e, dict)]
    with _lock:
        _state.update(epoch=data.get("epoch"), primed=True)
        try:
            _state["seq"] = int(data.get("latestSeq") or 0)
        except (TypeError, ValueError):
            pass
    take(events, now)                                    # old ones (>30 min) are dropped by take()
    if events:
        logger.info("phone events: %d new", len(events))
    return len(events)


def reset() -> None:
    with _lock:
        _state.update(epoch=None, seq=0, next_poll=0.0, recent=[], moment=None, primed=False)
