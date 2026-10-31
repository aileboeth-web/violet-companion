"""Her own mood (2026-10-30, user: 人设更有灵动性; idea from dsh-aurabot-qq).

One number, -1 (upset) .. +1 (happy), and what caused it. Words said to her move it -- praise and thanks up, scolding
and insults down (the owner counts more than someone in a group) -- and it drifts back to calm with a half-life
(VIOLET_MOOD_HALF_LIFE_HOURS, 3). Keyword detection only: no model call.

What it changes, only while it is clearly there (|mood| >= 0.3):
  * a short line in the owner's chat and in the social prompts: how she feels and why, and that it may colour her
    tone (never an attack on anyone, never a reason to refuse help);
  * stickers: in a good mood she is told she may use one more readily;
  * groups: the chance that she starts talking by herself in a quiet group goes up / down.
Off: VIOLET_MOOD=0. Test mode never reads or writes it.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from ai.settings import env_bool, env_float

SCHEMA = """
CREATE TABLE IF NOT EXISTS mood_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value REAL NOT NULL DEFAULT 0,
    cause TEXT,
    updated_at TEXT NOT NULL
);
"""

PRAISE = re.compile(r"(谢谢|谢啦|多谢|感谢|辛苦了|真棒|好棒|太棒|厉害|好厉害|真聪明|聪明|可爱|好可爱|喜欢你|爱你|想你|真好|好乖|"
                    r"么么|抱抱|贴贴|夸夸|你最好|做得好|干得好|nice|awesome|love you|thank)", re.I)
SCOLD = re.compile(r"(笨蛋|蠢|傻[逼瓜子]|白痴|弱智|废物|垃圾|滚|闭嘴|烦死|好烦|讨厌你|恶心|没用|有病|脑残|别吵|闭麦|"
                   r"shut up|stupid|idiot)", re.I)
SAD = re.compile(r"(难过|伤心|好累|崩溃|想哭|哭了|失眠|好烦啊|压力好大|不开心|郁闷)")
THRESHOLD = 0.3


def enabled() -> bool:
    return env_bool("VIOLET_MOOD", True)


def _row(db) -> dict[str, Any]:
    return db.fetch_one("SELECT * FROM mood_state WHERE id = 1") or {"value": 0.0, "cause": None, "updated_at": db.now()}


def _decayed(value: float, since: str, now: str) -> float:
    try:
        hours = (datetime.fromisoformat(now[:19]) - datetime.fromisoformat(str(since)[:19])).total_seconds() / 3600
    except ValueError:
        return value
    half = max(0.1, env_float("VIOLET_MOOD_HALF_LIFE_HOURS", 3.0))
    return value * math.pow(0.5, max(0.0, hours) / half)


def current(db) -> dict[str, Any]:
    """{"value": -1..1, "cause": str | None} as of now."""
    row = _row(db)
    return {"value": round(_decayed(float(row["value"]), row["updated_at"], db.now()), 3), "cause": row.get("cause")}


def feel(db, text: str, who: str = "主人", weight: float = 1.0) -> float | None:
    """Words said to her move her mood. Returns the new value, or None when nothing moved it."""
    from ai import test_mode
    if not enabled() or test_mode.active() or not text:
        return None
    praise, scold, sad = bool(PRAISE.search(text)), bool(SCOLD.search(text)), bool(SAD.search(text))
    if not (praise or scold or sad):
        return None
    delta, cause = 0.0, None
    if scold:
        delta, cause = -0.35, f"{who}凶了你"
    elif praise:
        delta, cause = 0.3, f"{who}夸了你 / 谢了你"
    elif sad and who == "主人":
        delta, cause = -0.15, "主人心情不好，你有点担心"
    if not delta:
        return None
    value = max(-1.0, min(1.0, current(db)["value"] + delta * weight))
    db.execute("INSERT INTO mood_state (id, value, cause, updated_at) VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
               "value = excluded.value, cause = excluded.cause, updated_at = excluded.updated_at", (value, cause, db.now()))
    from ai import trace
    trace.step("心情", 变化=round(delta * weight, 2), 现在=round(value, 2), 原因=cause)
    return value


def _words(value: float) -> str:
    if value >= 0.65:
        return "很开心"
    if value >= THRESHOLD:
        return "心情不错"
    if value <= -0.65:
        return "很不高兴，有点委屈"
    return "有点不高兴"


def note(db) -> str:
    """The line for the prompts, or "" when she is calm."""
    from ai import test_mode
    if not enabled() or test_mode.active():
        return ""
    mood = current(db)
    value = mood["value"]
    if abs(value) < THRESHOLD:
        return ""
    why = f"（因为{mood['cause']}）" if mood.get("cause") else ""
    if value > 0:
        how = "说话可以更轻快、更爱开玩笑一点，也更愿意配个表情包。"
    else:
        how = "说话可以短一点、淡一点，或者直说你不太高兴；但不攻击任何人，该帮的忙照样帮，对方哄你或道歉就慢慢好起来。"
    return f"【你现在的心情】{_words(value)}{why}。{how}心情会自己慢慢平复，不用一直提。"


def initiative_factor(db) -> float:
    """For starting a topic in a quiet group: 0.5 (upset) .. 1.5 (happy)."""
    if not enabled():
        return 1.0
    return 1.0 + 0.5 * current(db)["value"]
