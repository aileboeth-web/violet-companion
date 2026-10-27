"""Persistence of Violet's virtual life (phase 4).

Three separate status vocabularies (never mixed):

  thread  (a long-running life line, e.g. "reading 《边城》")
      active -> paused -> active
      active|paused -> completed | abandoned | cancelled        (terminal)
  plan    (something intended, not yet happened)
      planned -> active -> completed      (completed REQUIRES a recorded event)
      planned|active <-> paused
      planned|active|paused -> cancelled | abandoned            (terminal)
  event   (something that is recorded as having happened to Violet)
      happened -> retracted                                     (corrections keep 'happened' + a revision)

Only 'happened' events with is_virtual=1 may be cited as Violet's past.
Events are idempotent by uid (a restart can never duplicate them). Every
change writes a life_revisions row.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Iterable

from ai.memory_model import parse_time

THREAD_TRANSITIONS = {
    "active": {"paused", "completed", "abandoned", "cancelled"},
    "paused": {"active", "abandoned", "cancelled"},
}
PLAN_TRANSITIONS = {
    "planned": {"active", "paused", "completed", "cancelled", "abandoned"},
    "active": {"paused", "completed", "cancelled", "abandoned"},
    "paused": {"planned", "active", "cancelled", "abandoned"},
}
EVENT_TRANSITIONS = {"happened": {"retracted"}}


class TransitionError(ValueError):
    pass


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class LifeStore:
    def __init__(self, db):
        self.db = db

    # -- revisions / flags ---------------------------------------------------------
    def revise(self, entity: str, entity_id: int, action: str, old: Any, new: Any, reason: str | None,
               actor: str) -> None:
        self.db.execute(
            "INSERT INTO life_revisions (entity, entity_id, action, old_value, new_value, reason, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entity, entity_id, action, _dumps(old), _dumps(new), reason, actor, self.db.now()),
        )

    def revisions(self, entity: str, entity_id: int) -> list[dict[str, Any]]:
        return self.db.fetch_all("SELECT * FROM life_revisions WHERE entity = ? AND entity_id = ? ORDER BY id",
                                 (entity, entity_id))

    def flag(self, entity: str, entity_ref: str | None, check_name: str, detail: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO consistency_flags (entity, entity_ref, check_name, detail, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)", (entity, entity_ref, check_name, detail[:500], self.db.now()))
        return int(cursor.lastrowid)

    def flags(self, status: str | None = "open") -> list[dict[str, Any]]:
        if status:
            return self.db.fetch_all("SELECT * FROM consistency_flags WHERE status = ? ORDER BY id", (status,))
        return self.db.fetch_all("SELECT * FROM consistency_flags ORDER BY id")

    def resolve_flag(self, flag_id: int) -> None:
        self.db.execute("UPDATE consistency_flags SET status = 'resolved', resolved_at = ? WHERE id = ?",
                        (self.db.now(), flag_id))

    # -- threads ---------------------------------------------------------------------
    def thread(self, thread_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM life_threads WHERE id = ?", (thread_id,))

    def threads(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        statuses = list(statuses or [])
        if statuses:
            return self.db.fetch_all(
                f"SELECT * FROM life_threads WHERE status IN ({', '.join('?' for _ in statuses)}) ORDER BY id", statuses)
        return self.db.fetch_all("SELECT * FROM life_threads ORDER BY id")

    def create_thread(self, thread_key: str, interest_key: str, title: str, item: str | None, started_at: str,
                      source: str = "life_engine") -> int | None:
        now = self.db.now()
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO life_threads (thread_key, interest_key, title, item, status, source, started_at, "
            "last_progress_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (thread_key, interest_key, title, item, source, started_at, started_at, now, now))
        if cursor.rowcount == 0:
            return None
        thread_id = int(cursor.lastrowid)
        self.revise("thread", thread_id, "create", None, {"title": title, "status": "active"}, None, source)
        return thread_id

    def set_thread_status(self, thread_id: int, status: str, reason: str | None, actor: str,
                          at: str | None = None) -> dict[str, Any]:
        thread = self.thread(thread_id)
        if not thread:
            raise TransitionError(f"thread {thread_id} not found")
        if status not in THREAD_TRANSITIONS.get(thread["status"], set()):
            raise TransitionError(f"thread cannot go from {thread['status']} to {status}")
        ended = (at or self.db.now()) if status in ("completed", "abandoned", "cancelled") else None
        self.db.execute("UPDATE life_threads SET status = ?, ended_at = ?, updated_at = ? WHERE id = ?",
                        (status, ended, self.db.now(), thread_id))
        self.revise("thread", thread_id, "status", thread["status"], status, reason, actor)
        return self.thread(thread_id)

    def bump_thread(self, thread_id: int, at: str) -> None:
        self.db.execute("UPDATE life_threads SET progress_steps = progress_steps + 1, last_progress_at = ?, updated_at = ? "
                        "WHERE id = ?", (at, self.db.now(), thread_id))

    # -- plans -------------------------------------------------------------------------
    def plan(self, plan_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM life_plans WHERE id = ?", (plan_id,))

    def plans(self, statuses: Iterable[str] | None = None, thread_id: int | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        statuses = list(statuses or [])
        if statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if thread_id is not None:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.db.fetch_all(f"SELECT * FROM life_plans {where} ORDER BY planned_for, id", params)

    def create_plan(self, thread_id: int, content: str, planned_for: str, source: str = "life_engine") -> int:
        now = self.db.now()
        cursor = self.db.execute(
            "INSERT INTO life_plans (thread_id, content, planned_for, status, source, created_at, updated_at) "
            "VALUES (?, ?, ?, 'planned', ?, ?, ?)", (thread_id, content, planned_for, source, now, now))
        plan_id = int(cursor.lastrowid)
        self.revise("plan", plan_id, "create", None, {"content": content, "planned_for": planned_for}, None, source)
        return plan_id

    def set_plan_status(self, plan_id: int, status: str, reason: str | None, actor: str,
                        resolution_event_id: int | None = None) -> dict[str, Any]:
        plan = self.plan(plan_id)
        if not plan:
            raise TransitionError(f"plan {plan_id} not found")
        if status not in PLAN_TRANSITIONS.get(plan["status"], set()):
            raise TransitionError(f"plan cannot go from {plan['status']} to {status}")
        if status == "completed":
            event = self.event(resolution_event_id) if resolution_event_id else None
            if not event or event["status"] != "happened" or event["plan_id"] != plan_id:
                raise TransitionError("a plan can only be completed by a recorded event that happened")
        resolved = self.db.now() if status in ("completed", "cancelled", "abandoned") else None
        self.db.execute("UPDATE life_plans SET status = ?, resolution_event_id = ?, resolved_at = ?, updated_at = ? "
                        "WHERE id = ?", (status, resolution_event_id, resolved, self.db.now(), plan_id))
        self.revise("plan", plan_id, "status", plan["status"], status, reason, actor)
        return self.plan(plan_id)

    # -- events --------------------------------------------------------------------------
    def event(self, event_id: int | None) -> dict[str, Any] | None:
        if event_id is None:
            return None
        return self.db.fetch_one("SELECT * FROM life_events WHERE id = ?", (event_id,))

    def event_by_uid(self, uid: str) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM life_events WHERE uid = ?", (uid,))

    def events(self, statuses: Iterable[str] | None = ("happened",), thread_id: int | None = None,
               since: str | None = None, until: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clauses, params = [], []
        statuses = list(statuses or [])
        if statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if thread_id is not None:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        rows = self.db.fetch_all(
            f"SELECT * FROM life_events {'WHERE ' + ' AND '.join(clauses) if clauses else ''} ORDER BY occurred_at, id LIMIT ?",
            (*params, limit))
        if since:
            rows = [r for r in rows if parse_time(r["occurred_at"]) >= parse_time(since)]
        if until:
            rows = [r for r in rows if parse_time(r["occurred_at"]) <= parse_time(until)]
        return rows

    def insert_event(self, *, uid: str, thread_id: int | None, plan_id: int | None, event_type: str, content: str,
                     occurred_at: str, duration_minutes: int, source: str, source_detail: Any = None,
                     related_event_id: int | None = None, planned_at: str | None = None,
                     actor: str = "life_engine") -> int | None:
        """Insert a validated event. Returns None if this uid already exists (idempotent)."""
        now = self.db.now()
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO life_events (uid, thread_id, plan_id, event_type, content, planned_at, occurred_at,
                duration_minutes, status, source, source_detail, related_event_id, is_virtual, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'happened', ?, ?, ?, 1, ?, ?)
            """,
            (uid, thread_id, plan_id, event_type, content, planned_at, occurred_at, duration_minutes, source,
             _dumps(source_detail) if source_detail is not None else None, related_event_id, now, now))
        if cursor.rowcount == 0:
            return None
        event_id = int(cursor.lastrowid)
        self.revise("event", event_id, "create", None, {"content": content, "occurred_at": occurred_at}, None, actor)
        return event_id

    def correct_event(self, event_id: int, content: str, reason: str, actor: str = "dashboard") -> dict[str, Any]:
        event = self.event(event_id)
        if not event or event["status"] != "happened":
            raise TransitionError("only happened events can be corrected")
        self.db.execute("UPDATE life_events SET content = ?, updated_at = ? WHERE id = ?", (content, self.db.now(), event_id))
        self.revise("event", event_id, "correct", event["content"], content, reason, actor)
        return self.event(event_id)

    def retract_event(self, event_id: int, reason: str, actor: str = "dashboard") -> dict[str, Any]:
        event = self.event(event_id)
        if not event or "retracted" not in EVENT_TRANSITIONS.get(event["status"], set()):
            raise TransitionError("only happened events can be retracted")
        self.db.execute("UPDATE life_events SET status = 'retracted', updated_at = ? WHERE id = ?", (self.db.now(), event_id))
        self.revise("event", event_id, "retract", "happened", "retracted", reason, actor)
        return self.event(event_id)

    def mark_shared(self, event_id: int) -> None:
        self.db.execute("UPDATE life_events SET share_count = share_count + 1, shared_with_user_at = ?, updated_at = ? "
                        "WHERE id = ?", (self.db.now(), self.db.now(), event_id))

    def recent_happened(self, days: float, now: str | None = None) -> list[dict[str, Any]]:
        now_dt = parse_time(now or self.db.now())
        return [e for e in self.events(("happened",), since=(now_dt - timedelta(days=days)).isoformat())
                if parse_time(e["occurred_at"]) <= now_dt]

    # -- engine runs ------------------------------------------------------------------------
    def claim_run(self, run_key: str) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO life_engine_runs (run_key, outcome, ran_at) VALUES (?, 'running', ?)",
            (run_key, self.db.now()))
        return cursor.rowcount == 1

    def finish_run(self, run_key: str, outcome: str, detail: Any) -> None:
        self.db.execute("UPDATE life_engine_runs SET outcome = ?, detail = ? WHERE run_key = ?",
                        (outcome, _dumps(detail), run_key))
