"""Token usage of every model call (2026-10-30, rework step 2: "token 用量" -- measure first, then cut).

One row per call in llm_usage: which part of Violet asked (Intent / Chat / Memory / 群聊决定 / 识图 ...), the model, the
prompt / completion tokens and how many prompt tokens the provider served from its cache (DeepSeek:
prompt_cache_hit_tokens; OpenAI-style: prompt_tokens_details.cached_tokens), and the conversation turn it belonged
to. The overview page shows today's totals, per part, per owner turn, and the cache hit rate. Never raises.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("violet.usage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    turn_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
"""

LABELS = {"IntentAgent": "意图识别", "ChatAgent": "回复", "MemoryAgent": "记忆整理", "PerformerAgent": "执行任务",
          "ProactiveAgent": "主动联系", "social": "群聊 / 别人私聊", "vision": "识图", "aux": "辅助（复核、润色等）",
          "qzone": "QQ 动态", "explorer": "自主探索"}


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def numbers(usage: Any) -> tuple[int, int, int]:
    """(prompt, completion, cached) from an SDK usage object or a dict; zeros when missing."""
    prompt = int(_get(usage, "prompt_tokens") or 0)
    completion = int(_get(usage, "completion_tokens") or 0)
    cached = _get(usage, "prompt_cache_hit_tokens")
    if cached is None:
        cached = _get(_get(usage, "prompt_tokens_details"), "cached_tokens")
    return prompt, completion, int(cached or 0)


def record(source: str, model: str | None, usage: Any, db=None) -> None:
    if usage is None:
        return
    try:
        if db is None:
            from ai.chat import violetdb as db
        from ai.settings import CURRENT_TURN
        prompt, completion, cached = numbers(usage)
        db.execute("INSERT INTO llm_usage (created_at, source, model, prompt_tokens, completion_tokens, cached_tokens, "
                   "turn_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (db.now(), source, (model or "")[:120] or None, prompt, completion, cached, CURRENT_TURN.get()))
    except Exception:                                    # noqa: BLE001 -- metering never breaks a conversation
        logger.debug("usage not recorded", exc_info=True)


def summary(db, day: str | None = None) -> dict[str, Any]:
    """Today's (or `day`'s) totals, per part, and per owner turn."""
    day = day or db.now()[:10]
    rows = db.fetch_all("SELECT source, COUNT(*) AS calls, SUM(prompt_tokens) AS prompt, SUM(completion_tokens) AS completion, "
                        "SUM(cached_tokens) AS cached FROM llm_usage WHERE substr(created_at, 1, 10) = ? GROUP BY source "
                        "ORDER BY SUM(prompt_tokens + completion_tokens) DESC", (day,))
    parts = [{"source": r["source"], "label": LABELS.get(r["source"], r["source"]), "calls": int(r["calls"] or 0),
              "prompt": int(r["prompt"] or 0), "completion": int(r["completion"] or 0), "cached": int(r["cached"] or 0)}
             for r in rows]
    prompt = sum(p["prompt"] for p in parts)
    completion = sum(p["completion"] for p in parts)
    cached = sum(p["cached"] for p in parts)
    turns = db.fetch_one("SELECT COUNT(DISTINCT turn_id) AS n, SUM(prompt_tokens + completion_tokens) AS t FROM llm_usage "
                         "WHERE substr(created_at, 1, 10) = ? AND turn_id IS NOT NULL", (day,))
    n = int(turns["n"] or 0)
    calls = sum(p["calls"] for p in parts)
    return {"day": day, "calls": calls, "prompt": prompt, "completion": completion, "total": prompt + completion,
            "cached": cached, "cache_rate": round(cached / prompt, 3) if prompt else None, "parts": parts,
            "turns": n, "per_turn": int((turns["t"] or 0) / n) if n else None,
            "calls_per_turn": round(db.fetch_one("SELECT COUNT(*) AS c FROM llm_usage WHERE substr(created_at, 1, 10) = ? "
                                                 "AND turn_id IS NOT NULL", (day,))["c"] / n, 1) if n else None}


def turn(db, turn_id: str) -> dict[str, Any]:
    """One turn's calls (for the test-mode diagnosis)."""
    rows = db.fetch_all("SELECT source, prompt_tokens, completion_tokens, cached_tokens FROM llm_usage WHERE turn_id = ?",
                        (turn_id,))
    return {"calls": len(rows), "prompt": sum(r["prompt_tokens"] for r in rows),
            "completion": sum(r["completion_tokens"] for r in rows), "cached": sum(r["cached_tokens"] for r in rows),
            "sources": [LABELS.get(r["source"], r["source"]) for r in rows]}
