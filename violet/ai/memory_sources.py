"""Where an event memory came from (phase 9.1, A8). Read-only; never invents a message id.

    known      source_message_ids holds the user message(s) it is based on (written since 9.1), or its
               source_turn_id leads to the memory job of that turn, which recorded the user message id
    turn_only  a turn id but no message can be found for it (e.g. inline memory runs, synthetic test rows)
    unknown    neither: rows from before phase 2, or written without a turn -- shown as 来源未知
"""

from __future__ import annotations

from typing import Any


def memory_source(db, memory: dict[str, Any]) -> dict[str, Any]:
    ids = [int(i) for i in memory.get("source_message_ids") or [] if isinstance(i, int)]
    turn_id = memory.get("source_turn_id")
    if not ids and turn_id:
        job = db.fetch_one("SELECT user_message_id FROM memory_jobs WHERE turn_id = ?", (turn_id,))
        if job and job.get("user_message_id") is not None:
            ids = [int(job["user_message_id"])]
    if ids:
        return {"state": "known", "message_ids": ids, "turn_id": turn_id,
                "label": "来自用户消息 " + "、".join(f"#{i}" for i in ids)}
    if turn_id:
        return {"state": "turn_only", "message_ids": [], "turn_id": turn_id, "label": "有对话轮次，找不到原消息"}
    return {"state": "unknown", "message_ids": [], "turn_id": None, "label": "来源未知"}


def source_messages(db, source: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """The user messages a source points at (for the dashboard history view)."""
    ids = source.get("message_ids") or []
    if not ids:
        return []
    ids = ids[-limit:]
    rows = db.fetch_all(f"SELECT id, created_at, content FROM messages WHERE role = 'user' AND id IN "
                        f"({', '.join('?' for _ in ids)}) ORDER BY id", ids)
    return [{"id": r["id"], "created_at": r["created_at"], "content": r["content"]} for r in rows]
