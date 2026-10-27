"""Startup reconciliation of work interrupted by a crash (phase 7, D4).

Nothing here re-executes anything. Each interrupted item gets the most precise
status that is actually known:

  outbound_messages  queued   -> cancelled  (never attempted: certainly not sent)
                     sending  -> unknown    (may or may not have reached WeChat)
  media_assets       send_status sending -> unknown (the outbox never resends 'unknown')
  commitments        sending  -> unknown    (the reminder may or may not have gone out)
  image_calls        in_flight -> uncertain (the provider may have processed / charged it)
  image_tasks        running  -> uncertain  if a provider call was in flight
                     running  -> failed     if no provider call had started (nothing charged)

Only rows last touched before `started_before` are considered, so work started by
this process (e.g. a dashboard check right after boot) is never misclassified.
Running it twice changes nothing the second time. The dashboard lists the
'unknown' / 'uncertain' items (GET /api/reconciliation) until a human resolves them.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("violet.reconcile")

EXPLANATIONS = {
    "outbound": "发送过程中程序中断：无法确认这条消息是否已经到达微信。系统不会自动重发，请在微信里确认后标记。",
    "media": "图片发送过程中程序中断：无法确认是否已经到达微信。系统不会自动重发这张图片。",
    "commitment": "提醒发送过程中程序中断：无法确认用户是否收到。请确认后标记为已送达，或重新安排提醒。",
    "image_task": "调用图片服务时程序中断：服务方可能已经处理并计费，但结果没有保存。不会自动重试，请对照服务方账单核对。",
}


def reconcile_interrupted(db, started_before: str) -> dict[str, int]:
    note = "interrupted by a restart"
    counts: dict[str, int] = {}
    with db.transaction():
        cur = db.execute("UPDATE outbound_messages SET status = 'cancelled', error = ?, updated_at = ? "
                         "WHERE status = 'queued' AND updated_at < ?", (f"{note} before sending", db.now(), started_before))
        counts["outbound_cancelled"] = cur.rowcount
        cur = db.execute("UPDATE outbound_messages SET status = 'unknown', error = ?, updated_at = ? "
                         "WHERE status = 'sending' AND updated_at < ?",
                         (f"{note} while sending: delivery unknown", db.now(), started_before))
        counts["outbound_unknown"] = cur.rowcount
        cur = db.execute("UPDATE media_assets SET send_status = 'unknown', send_error = ?, updated_at = ? "
                         "WHERE send_status = 'sending' AND updated_at < ?",
                         (f"{note} while sending: delivery unknown", db.now(), started_before))
        counts["media_unknown"] = cur.rowcount
        cur = db.execute("UPDATE commitments SET status = 'unknown', last_error = ?, updated_at = ? "
                         "WHERE status = 'sending' AND updated_at < ?",
                         (f"{note} while sending: delivery unknown", db.now(), started_before))
        counts["commitments_unknown"] = cur.rowcount
        in_flight = {r["task_id"] for r in db.fetch_all(
            "SELECT task_id FROM image_calls WHERE status = 'in_flight' AND created_at < ?", (started_before,))}
        cur = db.execute("UPDATE image_calls SET status = 'uncertain', accepted = 'unknown', error_code = 'interrupted' "
                         "WHERE status = 'in_flight' AND created_at < ?", (started_before,))
        counts["image_calls_uncertain"] = cur.rowcount
        uncertain = failed = 0
        for task in db.fetch_all("SELECT task_id FROM image_tasks WHERE status = 'running' AND updated_at < ?",
                                 (started_before,)):
            if task["task_id"] in in_flight:
                db.execute("UPDATE image_tasks SET status = 'uncertain', error_code = 'interrupted', error = ?, "
                           "updated_at = ? WHERE task_id = ?",
                           ("程序中断时服务方调用正在进行：可能已处理并计费，等待核对", db.now(), task["task_id"]))
                uncertain += 1
            else:
                db.execute("UPDATE image_tasks SET status = 'failed', error_code = 'interrupted_before_call', error = ?, "
                           "updated_at = ? WHERE task_id = ?",
                           ("程序中断时还没有调用服务方：没有计费", db.now(), task["task_id"]))
                failed += 1
        counts["image_tasks_uncertain"] = uncertain
        counts["image_tasks_failed"] = failed
    if any(counts.values()):
        logger.warning("[reconcile] interrupted work after restart: %s", counts)
    return counts


def pending_items(db) -> dict[str, Any]:
    return {
        "outbound": db.fetch_all("SELECT id, origin, message_type, content, error, updated_at FROM outbound_messages "
                                 "WHERE status = 'unknown' ORDER BY id DESC LIMIT 100"),
        "media": db.fetch_all("SELECT id, task_id, caption, send_error, updated_at FROM media_assets "
                              "WHERE send_status = 'unknown' ORDER BY id DESC LIMIT 100"),
        "commitment": db.fetch_all("SELECT id, content, due_at, last_error, updated_at FROM commitments "
                                   "WHERE status = 'unknown' ORDER BY id DESC LIMIT 100"),
        "image_task": db.fetch_all("SELECT task_id, kind, entry_id, error, updated_at FROM image_tasks "
                                   "WHERE status = 'uncertain' ORDER BY id DESC LIMIT 100"),
        "explanations": EXPLANATIONS,
    }


RESOLUTIONS = {
    "outbound": {"sent": "sent", "not_sent": "failed"},
    "media": {"sent": "sent", "not_sent": "failed"},
    "commitment": {"sent": "sent", "not_sent": "pending"},          # not delivered -> deliver again
    "image_task": {"charged_no_result": "failed", "not_charged": "failed"},
}


def resolve(db, kind: str, item_id: str, resolution: str, actor: str = "dashboard") -> dict[str, Any]:
    """A human records what really happened. Only items that are still unknown / uncertain can be resolved."""
    target = (RESOLUTIONS.get(kind) or {}).get(resolution)
    if target is None:
        raise ValueError(f"resolution must be one of {sorted(RESOLUTIONS.get(kind, {}))}")
    note = f"resolved by {actor}: {resolution}"
    if kind == "outbound":
        cur = db.execute("UPDATE outbound_messages SET status = ?, error = ?, updated_at = ? WHERE id = ? AND status = 'unknown'",
                         (target, note, db.now(), int(item_id)))
    elif kind == "media":
        cur = db.execute("UPDATE media_assets SET send_status = ?, send_error = ?, updated_at = ? "
                         "WHERE id = ? AND send_status = 'unknown'", (target, note, db.now(), int(item_id)))
    elif kind == "commitment":
        cur = db.execute("UPDATE commitments SET status = ?, last_error = ?, next_attempt_at = ?, updated_at = ? "
                         "WHERE id = ? AND status = 'unknown'", (target, note, db.now(), db.now(), int(item_id)))
    else:
        cur = db.execute("UPDATE image_tasks SET status = ?, error_code = ?, error = ?, updated_at = ? "
                         "WHERE task_id = ? AND status = 'uncertain'",
                         (target, f"reconciled_{resolution}", note, db.now(), str(item_id)))
    if cur.rowcount == 0:
        raise LookupError("no such item waiting for reconciliation")
    return {"kind": kind, "id": item_id, "status": target}
