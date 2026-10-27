"""User profile, situations and interaction preferences (phase 3).

Rules enforced here (not by prompts):

Facts (user_profile_facts)
  * explicit  = the user's own words, verified verbatim in the user message, or
                entered on the dashboard. Only explicit facts become 'active'.
  * inferred  = anything else (model guesses, legacy core memories) -> 'candidate',
                never injected into model context until confirmed.
  * single-valued keys: a newer explicit statement supersedes the active value
    (kept as 'superseded' history); an older statement never overrides a newer
    one; an inference never overrides an explicit fact.
  * sensitive facts are not persisted unless VIOLET_PROFILE_STORE_SENSITIVE=1.
  * disable  = keep the row, never use it (and mask its value in context).
  * delete   = hard delete of the fact and its history, redaction of the
    evidence/value from chat history, inbox, memory jobs and revisions,
    deletion of memories derived from it, WAL checkpoint. The audit log keeps
    only metadata (key, counts, actor, time) -- never the value.

Situations (user_situations): short-lived, always expire, superseded per kind.
  Location/activity/availability/mood only from the user's own words (or the
  dashboard); external tools may only report the 'device' kind, with low
  confidence and a short TTL -- never a location.

Preferences (interaction_preferences): explicit (user said so) beats implicit.
  Implicit values need several consistent observations inside a time window,
  are capped at low confidence and change back when behaviour changes.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Iterable

from ai.memory_model import parse_time
from ai.profile_schema import (
    ASK_ORDER, EXTERNAL_MAX_CONFIDENCE, EXTERNAL_MAX_TTL_HOURS, IMPLICIT_PREF_KEYS, PREF_VALUES,
    PROFILE_KEYS, SITUATION_KINDS, sensitivity_for, spec_for,
)
from ai.settings import env_bool, env_float, env_int, local_timezone

logger = logging.getLogger("violet.profile")

REDACTED = "[已删除的个人信息]"
INFERRED_MAX_CONFIDENCE = 0.6


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "")


def _norm(text: str | None) -> str:
    return _squash(text).lower()


def evidence_in_message(evidence: str | None, user_message: str | None) -> bool:
    ev = _squash(evidence)
    return bool(ev) and ev in _squash(user_message)


class ProfileAudit:
    def __init__(self, db):
        self.db = db

    def log(self, entity: str, entity_id: int | None, action: str, actor: str,
            fact_key: str | None = None, detail: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO profile_audit_log (entity, entity_id, action, actor, fact_key, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity, entity_id, action, actor, fact_key, json.dumps(detail or {}, ensure_ascii=False), self.db.now()),
        )

    def entries(self, entity: str | None = None, entity_id: int | None = None, fact_key: str | None = None,
                limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, value in (("entity", entity), ("entity_id", entity_id), ("fact_key", fact_key)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.db.fetch_all(f"SELECT * FROM profile_audit_log {where} ORDER BY id DESC LIMIT ?", (*params, limit))


# ---------------------------------------------------------------------------
# Redaction / purge helpers
# ---------------------------------------------------------------------------

_REDACT_COLUMNS = (
    ("messages", "content"),
    ("inbound_messages", "content"),
    ("outbound_messages", "content"),
    ("memory_jobs", "user_message"),
    ("memory_jobs", "assistant_reply"),
    ("memory_revisions", "old_content"),
    ("memory_revisions", "new_content"),
    ("event_memories", "content"),
    ("commitments", "evidence"),
    ("user_profile_facts", "evidence"),
    ("user_situations", "evidence"),
    ("interaction_preferences", "evidence"),
    ("consistency_flags", "detail"),
    ("web_requests", "target"),
    # phase 9.1.2: a task may quote a memory or a profile value word for word (p911-real-1 task_info.target_memories)
    ("task_sessions", "task_description"),
    ("task_sessions", "task_info"),
    ("task_sessions", "result"),
    ("task_sessions", "error_message"),
    ("image_tasks", "prompt"),
    ("image_tasks", "result_text"),
    ("media_assets", "generated_prompt"),
    ("media_assets", "caption"),
)


DELETION_LIMITS = ("A deletion is one SQLite transaction: either everything below is removed / redacted, or "
                   "nothing is and it can simply be retried. It cannot reach messages already delivered to WeChat, "
                   "files outside the database (media are deleted separately), provider logs or database backups.")


def redact_everywhere(db, phrases: Iterable[str]) -> int:
    """Replace every occurrence of each phrase (>= 2 chars) in stored text. Returns rows touched.

    Call it inside db.transaction() together with the deletion it belongs to."""
    touched = 0
    for phrase in {p.strip() for p in phrases if p and len(p.strip()) >= 2}:
        for table, column in _REDACT_COLUMNS:
            cursor = db.execute(
                f"UPDATE {table} SET {column} = REPLACE({column}, ?, ?) WHERE {column} IS NOT NULL AND instr({column}, ?) > 0",
                (phrase, REDACTED, phrase),
            )
            touched += cursor.rowcount
    return touched


def checkpoint(db) -> None:
    try:
        with db._lock:
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        logger.warning("WAL checkpoint after deletion failed", exc_info=True)


def purge_event_memory(db, memory_id: int, extra_phrases: Iterable[str] = (), actor: str = "dashboard") -> dict[str, Any]:
    memory = db.get_event_memory(memory_id)
    if not memory:
        raise ValueError(f"memory {memory_id} not found")
    phrases = [memory["content"], *extra_phrases]
    with db.transaction():
        # redact first, delete the locating row last: a failure anywhere rolls everything back
        touched = redact_everywhere(db, phrases)
        db.execute("DELETE FROM memory_revisions WHERE memory_table = 'event_memories' AND memory_id = ?", (memory_id,))
        db.execute("DELETE FROM topic_mentions WHERE memory_id = ?", (memory_id,))
        db.execute("DELETE FROM event_memories WHERE id = ?", (memory_id,))
        ProfileAudit(db).log("event_memory", memory_id, "delete", actor, detail={"redacted_rows": touched})
    if not db._tx_depth:
        checkpoint(db)
    return {"deleted_memory": memory_id, "redacted_rows": touched}


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

class ProfileStore:
    ACTIVE = "active"

    def __init__(self, db):
        self.db = db
        self.audit = ProfileAudit(db)

    # -- reads -------------------------------------------------------------------
    def get_fact(self, fact_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM user_profile_facts WHERE id = ?", (fact_id,))

    def list_facts(self, statuses: Iterable[str] | None = None, fact_key: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        statuses = list(statuses or [])
        if statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if fact_key:
            clauses.append("fact_key = ?")
            params.append(fact_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.db.fetch_all(f"SELECT * FROM user_profile_facts {where} ORDER BY fact_key, id", params)

    def _expire(self) -> None:
        now = self.db.now()
        for row in self.list_facts(["active"]):
            valid_until = parse_time(row.get("valid_until"))
            if valid_until and valid_until < parse_time(now):
                self.db.execute("UPDATE user_profile_facts SET status = 'expired', updated_at = ? WHERE id = ?", (now, row["id"]))

    def active_facts(self, keys: Iterable[str] | None = None, for_context: bool = True) -> list[dict[str, Any]]:
        """Facts that may be used now: active, not expired, allowed, not sensitive (for context)."""
        self._expire()
        rows = self.list_facts(["active"])
        if keys is not None:
            keys = set(keys)
            rows = [r for r in rows if r["fact_key"] in keys]
        if for_context:
            rows = [r for r in rows if r["use_in_context"] and r["sensitivity"] != "sensitive"]
        return rows

    def known_keys(self) -> set[str]:
        """Keys Violet already knows (active or awaiting confirmation) -- never asked again."""
        return {r["fact_key"] for r in self.list_facts(["active", "candidate", "disabled"])}

    def disabled_values(self) -> list[str]:
        return [r["value"] for r in self.list_facts(["disabled"]) if len(_squash(r["value"])) >= 2]

    # -- writes ----------------------------------------------------------------------
    def _insert(self, **fields: Any) -> int:
        now = self.db.now()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        cursor = self.db.execute(f"INSERT INTO user_profile_facts ({columns}) VALUES ({placeholders})", tuple(fields.values()))
        return int(cursor.lastrowid)

    def _set(self, fact_id: int, **fields: Any) -> None:
        fields["updated_at"] = self.db.now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE user_profile_facts SET {sets} WHERE id = ?", (*fields.values(), fact_id))

    def remember(
        self,
        key: str,
        value: str,
        *,
        evidence: str | None = None,
        user_message: str | None = None,
        source: str = "user_chat",
        actor: str = "memory_agent",
        source_turn_id: str | None = None,
        source_message_id: int | None = None,
        stated_at: str | None = None,
        valid_until: str | None = None,
        correction: bool = False,
        replaces_value: str | None = None,
    ) -> dict[str, Any]:
        spec = spec_for(key)
        value = (value or "").strip()
        if spec is None:
            return {"action": "rejected", "reason": "unknown_key", "allowed_keys": sorted(PROFILE_KEYS)}
        if not value:
            return {"action": "rejected", "reason": "empty_value"}
        sensitivity = sensitivity_for(key, value)
        if sensitivity == "sensitive" and not env_bool("VIOLET_PROFILE_STORE_SENSITIVE", False):
            self.audit.log("profile_fact", None, "not_stored_sensitive", actor, fact_key=key)
            return {"action": "not_stored", "reason": "sensitive"}

        if source == "dashboard":
            explicit = True
        else:
            explicit = evidence_in_message(evidence, user_message)
        now = self.db.now()
        stated_at = stated_at or now
        base = dict(
            fact_key=key, category=spec.category, value=value, source=source, source_turn_id=source_turn_id,
            source_message_id=source_message_id, evidence=evidence, stated_at=stated_at,
            valid_until=valid_until, sensitivity=sensitivity,
        )
        same_value = [r for r in self.list_facts(["active", "candidate", "disabled"], key) if _norm(r["value"]) == _norm(value)]

        if any(r["status"] == "disabled" for r in same_value):
            return {"action": "kept_disabled", "reason": "the user disabled this information"}

        if not explicit:
            if any(r["status"] == "active" for r in same_value):
                return {"action": "already_known", "fact": next(r for r in same_value if r["status"] == "active")}
            fact_id = self._insert(**base, origin="inferred", confidence=INFERRED_MAX_CONFIDENCE, status="candidate",
                                   use_in_context=0)
            self.audit.log("profile_fact", fact_id, "create_candidate", actor, fact_key=key)
            return {"action": "candidate", "fact": self.get_fact(fact_id),
                    "reason": "not stated by the user verbatim; kept as an unconfirmed candidate"}

        # explicit statement
        for row in same_value:
            if row["status"] == "active":
                self._set(row["id"], confirmed_at=now, confidence=1.0)
                self.mark_question(key, "answered")
                return {"action": "confirmed", "fact": self.get_fact(row["id"])}
        for row in same_value:            # candidate confirmed by the user's own words
            if row["status"] == "candidate":
                self._set(row["id"], status="superseded")

        previous = []
        if not spec.multi:
            previous = self.list_facts(["active"], key)
        elif replaces_value:
            previous = [r for r in self.list_facts(["active"], key) if _norm(r["value"]) == _norm(replaces_value)]
        newer = [r for r in previous if (parse_time(r.get("stated_at")) or parse_time(r["created_at"])) > parse_time(stated_at)]
        if newer and not correction:
            fact_id = self._insert(**base, origin="explicit", confidence=1.0, confirmed_at=now, status="superseded",
                                   use_in_context=1, superseded_by=newer[0]["id"])
            self.audit.log("profile_fact", fact_id, "older_statement_kept_as_history", actor, fact_key=key)
            return {"action": "older_statement", "fact": self.get_fact(fact_id)}
        fact_id = self._insert(**base, origin="dashboard" if source == "dashboard" else "explicit", confidence=1.0,
                               confirmed_at=now, status="active", use_in_context=1,
                               supersedes_id=previous[0]["id"] if previous else None)
        for row in previous:
            self._set(row["id"], status="superseded", superseded_by=fact_id)
        self.audit.log("profile_fact", fact_id, "correct" if correction else "create", actor, fact_key=key,
                       detail={"superseded": [r["id"] for r in previous]})
        self.mark_question(key, "answered")
        return {"action": "superseded_previous" if previous else "created", "fact": self.get_fact(fact_id),
                "superseded": [r["id"] for r in previous]}

    # -- corrections (phase 9.1.1) ----------------------------------------------------------
    def _candidate_view(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": r["id"], "key": r["fact_key"], "value": r["value"]} for r in rows]

    def _correction_target(self, key: str, spec, fact_id: int | None,
                           old_value: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(the active fact being corrected, None) or (None, the outcome when it cannot be decided).

        fact_id names it directly. old_value may be any part of the stored text ("豆豆" for "家里养了一只柯基，名叫豆豆…");
        it decides only when exactly one active fact of the key matches. Without either, a single-valued key has at
        most one current value; a multi-valued key never guesses -- the candidates go back to the caller."""
        if fact_id is not None:
            fact = self.get_fact(int(fact_id))
            if not fact:
                return None, {"action": "failed", "reason": "fact_not_found"}
            if fact["status"] != "active":
                return None, {"action": "failed", "reason": f"fact_not_active:{fact['status']}", "fact": fact}
            return fact, None
        actives = self.list_facts(["active"], key)
        if old_value and _norm(old_value):
            wanted = _norm(old_value)
            exact = [r for r in actives if _norm(r["value"]) == wanted]
            partial = exact or [r for r in actives if len(wanted) >= 2 and (wanted in _norm(r["value"])
                                                                           or _norm(r["value"]) in wanted)]
            if len(partial) == 1:
                return partial[0], None
            if not actives:
                return None, {"action": "failed", "reason": "old_value_not_found"}
            return None, {"action": "ambiguous", "reason": "old_value_matches_several" if partial else "old_value_not_found",
                          "candidates": self._candidate_view(partial or actives)}
        if not actives:
            return None, None                         # nothing stored under this key: a plain new fact
        if not spec.multi and len(actives) == 1:
            return actives[0], None
        return None, {"action": "ambiguous", "reason": "which_fact_unknown", "candidates": self._candidate_view(actives)}

    def correct(
        self,
        key: str,
        new_value: str,
        *,
        fact_id: int | None = None,
        old_value: str | None = None,
        evidence: str | None = None,
        user_message: str | None = None,
        source: str = "user_chat",
        actor: str = "memory_agent",
        source_turn_id: str | None = None,
        source_message_id: int | None = None,
        propagate: bool = True,
    ) -> dict[str, Any]:
        """Replace one stored fact with the corrected value (phase 9.1.1, D-P91-2).

        propagate (phase 9.1.2, D-P911-R1): after a correction, the copies of the same fact in event memories, core
        memories and other profile facts are brought in line where that is certain (ai/memory_consistency.py);
        result["propagated"] says what changed and which conflicts were only recorded.

        action: corrected (the old fact is now 'superseded' and points at the new one), created (nothing was stored
        under the key, the value is saved as a new fact), confirmed (the corrected value was already the current
        one), ambiguous (which fact is meant is not certain: nothing is written, candidates are returned) or failed
        (nothing is written, reason says why). The audit log records the same outcome -- never "correct" for a write
        that replaced nothing."""
        new_value = (new_value or "").strip()
        target, outcome = None, None
        if fact_id is not None:
            target, outcome = self._correction_target(key, None, fact_id, None)
            if target is not None:
                key = target["fact_key"]              # the fact being corrected decides the key
        spec = spec_for(key or "")
        if outcome is None and spec is None:
            outcome = {"action": "failed", "reason": "unknown_key", "allowed_keys": sorted(PROFILE_KEYS)}
        elif outcome is None and not new_value:
            outcome = {"action": "failed", "reason": "empty_value"}
        elif outcome is None and source != "dashboard" and not evidence_in_message(evidence, user_message):
            outcome = {"action": "failed", "reason": "evidence_not_in_user_message"}
        elif outcome is None and sensitivity_for(key, new_value) == "sensitive" \
                and not env_bool("VIOLET_PROFILE_STORE_SENSITIVE", False):
            outcome = {"action": "failed", "reason": "sensitive"}
        elif outcome is None and fact_id is None:
            target, outcome = self._correction_target(key, spec, None, old_value)
        if outcome is not None:
            if outcome["action"] == "failed" and str(outcome.get("reason", "")).startswith("fact_not_active") \
                    and self._already_corrected(outcome.get("fact"), new_value):
                outcome = {"action": "confirmed", "reason": "already_corrected",
                           "fact": self._already_corrected(outcome.get("fact"), new_value)}
            self.audit.log("profile_fact", (outcome.get("fact") or {}).get("id") or (target or {}).get("id"),
                           f"correct_{outcome['action']}", actor, fact_key=key if spec else None,
                           detail={"reason": outcome.get("reason"),
                                   "candidates": [c["id"] for c in outcome.get("candidates") or []]})
            return outcome

        now = self.db.now()
        base = dict(fact_key=key, category=spec.category, value=new_value, source=source, source_turn_id=source_turn_id,
                    source_message_id=source_message_id, evidence=evidence, stated_at=now,
                    sensitivity=sensitivity_for(key, new_value))
        if target is not None and _norm(target["value"]) == _norm(new_value):
            self._set(target["id"], confirmed_at=now, confidence=1.0)
            self.audit.log("profile_fact", target["id"], "correct_confirmed", actor, fact_key=key,
                           detail={"reason": "same_value"})
            return {"action": "confirmed", "reason": "same_value", "fact": self.get_fact(target["id"]), "superseded": []}
        replaced = [target] if target is not None else []
        if target is not None and not spec.multi:     # a single-valued key keeps one current value
            replaced += [r for r in self.list_facts(["active"], key) if r["id"] != target["id"]]
        same = next((r for r in self.list_facts(["active"], key)
                     if _norm(r["value"]) == _norm(new_value) and r["id"] not in {x["id"] for x in replaced}), None)
        if same is not None:                          # the corrected value is already stored: link it, do not duplicate
            new_id = same["id"]
            self._set(new_id, confirmed_at=now, confidence=1.0,
                      supersedes_id=same.get("supersedes_id") or (target or {}).get("id"))
        else:
            new_id = self._insert(**base, origin="dashboard" if source == "dashboard" else "explicit", confidence=1.0,
                                  confirmed_at=now, status="active", use_in_context=1,
                                  supersedes_id=target["id"] if target is not None else None)
        for row in replaced:
            self._set(row["id"], status="superseded", superseded_by=new_id)
        action = "corrected" if replaced else "created"
        propagated = None
        if replaced and propagate:
            from ai.memory_consistency import propagate_correction
            propagated = propagate_correction(
                self.db, target["value"], new_value, source="dashboard" if source == "dashboard" else "user_confirmed",
                origin=f"档案 {target['id']} → {new_id}", turn_id=source_turn_id, profile=self, evidence=evidence,
                user_message=user_message, exclude_fact_id=new_id)
        self.audit.log("profile_fact", new_id, "correct" if replaced else "correct_created", actor, fact_key=key,
                       detail={"superseded": [r["id"] for r in replaced], "reused_existing": same is not None,
                               "via": "fact_id" if fact_id is not None else "old_value" if old_value else "key",
                               **({"propagated": {k: propagated[k] for k in ("updated", "conflicts", "profile")}}
                                  if propagated and any(propagated[k] for k in ("updated", "conflicts", "profile"))
                                  else {})})
        self.mark_question(key, "answered")
        return {"action": action, "fact": self.get_fact(new_id), "superseded": [r["id"] for r in replaced],
                "propagated": propagated}

    def _already_corrected(self, fact: dict[str, Any] | None, new_value: str) -> dict[str, Any] | None:
        """The active fact a superseded one was replaced by, when it already holds the corrected value."""
        seen = set()
        while fact and fact.get("status") == "superseded" and fact.get("superseded_by") and fact["id"] not in seen:
            seen.add(fact["id"])
            fact = self.get_fact(fact["superseded_by"])
        if fact and fact.get("status") == "active" and _norm(fact["value"]) == _norm(new_value):
            return fact
        return None

    def retract(self, key: str, *, evidence: str | None, user_message: str | None, value: str | None = None,
                actor: str = "memory_agent") -> dict[str, Any]:
        if not evidence_in_message(evidence, user_message):
            return {"action": "rejected", "reason": "evidence_not_in_user_message"}
        rows = self.list_facts(["active", "candidate"], key)
        if value:
            rows = [r for r in rows if _norm(r["value"]) == _norm(value)]
        for row in rows:
            self._set(row["id"], status="retracted", use_in_context=0)
            self.audit.log("profile_fact", row["id"], "retract", actor, fact_key=key)
        return {"action": "retracted", "fact_ids": [r["id"] for r in rows]}

    def set_enabled(self, fact_id: int, enabled: bool, actor: str = "dashboard") -> dict[str, Any]:
        fact = self.get_fact(fact_id)
        if not fact:
            raise ValueError(f"fact {fact_id} not found")
        if enabled:
            status = "active"
            spec = spec_for(fact["fact_key"])
            if spec and not spec.multi and any(r["id"] != fact_id for r in self.list_facts(["active"], fact["fact_key"])):
                status = "superseded"
            if fact["status"] == "candidate":
                status = "candidate"
            self._set(fact_id, status=status, use_in_context=1 if status == "active" else 0)
        else:
            self._set(fact_id, status="disabled", use_in_context=0)
        self.audit.log("profile_fact", fact_id, "enable" if enabled else "disable", actor, fact_key=fact["fact_key"])
        return self.get_fact(fact_id)

    def confirm_candidate(self, fact_id: int, key: str | None = None, value: str | None = None,
                          actor: str = "dashboard") -> dict[str, Any]:
        fact = self.get_fact(fact_id)
        if not fact or fact["status"] != "candidate":
            raise ValueError(f"fact {fact_id} is not a candidate")
        result = self.remember(key or fact["fact_key"], value or fact["value"], source="dashboard", actor=actor)
        if result["action"] in ("created", "superseded_previous", "confirmed"):
            self._set(fact_id, status="superseded", superseded_by=result["fact"]["id"])
        return result

    def correct_from_dashboard(self, fact_id: int, value: str, actor: str = "dashboard") -> dict[str, Any]:
        fact = self.get_fact(fact_id)
        if not fact:
            raise ValueError(f"fact {fact_id} not found")
        return self.correct(fact["fact_key"], value, fact_id=fact_id, source="dashboard", actor=actor)

    def delete(self, fact_id: int, actor: str = "dashboard", cascade: bool = True) -> dict[str, Any]:
        """Hard delete the fact, its history with the same value, its evidence and derived memories."""
        fact = self.get_fact(fact_id)
        if not fact:
            raise ValueError(f"fact {fact_id} not found")
        value = fact["value"]
        related = [r for r in self.list_facts(fact_key=fact["fact_key"]) if _norm(r["value"]) == _norm(value)]
        phrases = [value] + [r["evidence"] for r in related if r.get("evidence")]
        report = {"facts_deleted": [r["id"] for r in related], "memories_deleted": [], "core_memories_deleted": []}
        with self.db.transaction():
            self._delete_locked(fact, related, phrases, report, cascade, actor)
        checkpoint(self.db)
        return report

    def _delete_locked(self, fact, related, phrases, report, cascade, actor) -> None:
        """Runs inside one transaction. Derived memories are found (by the value) and purged first, then the
        value / evidence is redacted everywhere, and the fact rows that locate the data are deleted last."""
        value = fact["value"]
        if cascade:
            norm_value = _norm(value)
            if len(norm_value) >= 2:
                for memory in self.db.fetch_all("SELECT id, content FROM event_memories"):
                    if norm_value in _norm(memory["content"]):
                        purge_event_memory(self.db, memory["id"], actor=actor)
                        report["memories_deleted"].append(memory["id"])
                for core in self.db.fetch_all("SELECT id, content FROM core_memories"):
                    if norm_value in _norm(core["content"]):
                        report["core_memories_deleted"].append(core["id"])
            for r in related:
                ref = r.get("source_ref") or ""
                if ref.startswith("core:"):
                    report["core_memories_deleted"].append(int(ref.split(":", 1)[1]))
            for core_id in set(report["core_memories_deleted"]):
                self.db.execute("DELETE FROM memory_revisions WHERE memory_table = 'core_memories' AND memory_id = ?", (core_id,))
                self.db.delete_core_memory(core_id)
        report["redacted_rows"] = redact_everywhere(self.db, phrases)
        for r in related:
            self.db.execute("DELETE FROM user_profile_facts WHERE id = ?", (r["id"],))
        self.audit.log("profile_fact", fact["id"], "delete", actor, fact_key=fact["fact_key"],
                       detail={k: (len(v) if isinstance(v, list) else v) for k, v in report.items()})

    def source_of(self, fact_id: int) -> dict[str, Any]:
        fact = self.get_fact(fact_id)
        if not fact:
            raise ValueError(f"fact {fact_id} not found")
        message = self.db.get_message(fact["source_message_id"]) if fact.get("source_message_id") else None
        chain = [r for r in self.list_facts(fact_key=fact["fact_key"])
                 if r["id"] in (fact.get("supersedes_id"), fact.get("superseded_by"))]
        return {
            "fact": fact,
            "evidence": fact.get("evidence"),
            "origin": fact["origin"],
            "source": fact["source"],
            "source_ref": fact.get("source_ref"),
            "source_message": message,
            "related_versions": chain,
            "audit": self.audit.entries(entity="profile_fact", entity_id=fact_id),
        }

    # -- onboarding bookkeeping ---------------------------------------------------------
    def mark_question(self, key: str, outcome: str, turn_id: str | None = None) -> None:
        now = self.db.now()
        if outcome == "offered":
            self.db.execute("INSERT INTO profile_questions (fact_key, outcome, turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                            (key, outcome, turn_id, now, now))
            return
        self.db.execute("UPDATE profile_questions SET outcome = ?, updated_at = ? WHERE fact_key = ? AND outcome = 'offered'",
                        (outcome, now, key))

    def declined_keys(self) -> set[str]:
        return {r["fact_key"] for r in self.db.fetch_all("SELECT fact_key FROM profile_questions WHERE outcome = 'declined'")}

    # -- legacy ---------------------------------------------------------------------------
    _LEGACY_KEY_MAP = {
        "nickname_preference": "address_name",
        "nickname": "address_name",
        "user_profile": "identity.background",
        "user_preferences": "interest",
    }

    def import_legacy_core_memories(self) -> int:
        """Old core memories become *candidates* (never confirmed facts). Idempotent."""
        imported = 0
        for core in self.db.fetch_all("SELECT * FROM core_memories"):
            ref = f"core:{core['id']}"
            if self.db.fetch_one("SELECT id FROM user_profile_facts WHERE source_ref = ?", (ref,)):
                continue
            key = self._LEGACY_KEY_MAP.get(core["memory_key"]) or self._LEGACY_KEY_MAP.get(core["category"]) or "other"
            value = (core["content"] or "").strip()[:300]
            if not value:
                continue
            fact_id = self._insert(
                fact_key=key, category=PROFILE_KEYS[key].category, value=value, origin="legacy", source="legacy_core",
                source_ref=ref, confidence=min(INFERRED_MAX_CONFIDENCE, float(core.get("confidence") or 0.5)),
                stated_at=core["updated_at"], sensitivity=sensitivity_for(key, value), status="candidate",
                use_in_context=0,
            )
            self.audit.log("profile_fact", fact_id, "import_legacy_candidate", "system", fact_key=key,
                           detail={"core_memory_id": core["id"]})
            imported += 1
        return imported

    def excluded_core_memory_ids(self) -> set[int]:
        """Legacy core memories the user disabled through their imported candidate."""
        ids = set()
        for row in self.db.fetch_all("SELECT source_ref FROM user_profile_facts WHERE status = 'disabled' AND source_ref LIKE 'core:%'"):
            ids.add(int(row["source_ref"].split(":", 1)[1]))
        return ids


# ---------------------------------------------------------------------------
# Situations
# ---------------------------------------------------------------------------

def _end_of_day(day: datetime) -> datetime:
    return day.replace(hour=23, minute=59, second=0, microsecond=0)


def resolve_until(hint: str | None, now: datetime, default_hours: float) -> datetime:
    if hint:
        h = hint.strip().lower()
        if h in ("today", "今天", "tonight", "今晚"):
            return _end_of_day(now)
        if h in ("tomorrow", "明天"):
            return _end_of_day(now + timedelta(days=1))
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|小时)", h)
        if match:
            return now + timedelta(hours=float(match.group(1)))
        parsed = parse_time(hint)
        if parsed and parsed > now:
            return parsed
    return now + timedelta(hours=default_hours)


class SituationStore:
    USER_SOURCES = ("user_stated", "dashboard")

    def __init__(self, db):
        self.db = db
        self.audit = ProfileAudit(db)

    def observe(self, kind: str, value: str, source: str, *, evidence: str | None = None,
                user_message: str | None = None, until: str | None = None, confidence: float | None = None,
                turn_id: str | None = None, actor: str = "memory_agent") -> dict[str, Any]:
        spec = SITUATION_KINDS.get(kind)
        value = (value or "").strip()
        if spec is None or not value:
            return {"action": "rejected", "reason": "unknown_kind_or_empty"}
        if spec.sensitive and not env_bool("VIOLET_PROFILE_STORE_SENSITIVE", False):
            return {"action": "not_stored", "reason": "sensitive"}
        now = parse_time(self.db.now())
        if spec.user_only:
            if source not in self.USER_SOURCES:
                return {"action": "rejected", "reason": f"{kind} may only come from the user's own words"}
            if source == "user_stated" and not evidence_in_message(evidence, user_message):
                return {"action": "rejected", "reason": "evidence_not_in_user_message"}
            confidence = 1.0 if confidence is None else min(1.0, float(confidence))
            expires = resolve_until(until, now, spec.default_ttl_hours)
            if kind in ("availability", "activity"):
                # Phase 8.8: "今天一整天都要在实验室" lasts the day, not the kind's 3-hour default. Only the user's
                # own words can lengthen it; an explicit `until` is never shortened.
                from ai.interaction import stated_busy_until
                stated = stated_busy_until(evidence or user_message, now)
                if stated is not None and stated > expires:
                    expires = stated
        else:
            if source not in spec.external_sources:
                return {"action": "rejected", "reason": f"source {source} not allowed for {kind}"}
            confidence = min(EXTERNAL_MAX_CONFIDENCE, float(confidence if confidence is not None else EXTERNAL_MAX_CONFIDENCE))
            expires = min(resolve_until(until, now, spec.default_ttl_hours), now + timedelta(hours=EXTERNAL_MAX_TTL_HOURS))
        current = self.current([kind], include_disabled=True)
        if turn_id:
            # phase 9.4 (r1): a retried memory job writing the same state for the same turn again changes nothing
            # (before: the row was superseded by an identical one with a later observed_at)
            same = next((r for r in current if r.get("source_turn_id") == turn_id
                         and (r.get("value") or "").strip() == value), None)
            if same is not None:
                return {"action": "already_set", "situation": same}
        for row in current:
            self.db.execute("UPDATE user_situations SET status = 'superseded', updated_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
        cursor = self.db.execute(
            """
            INSERT INTO user_situations (kind, value, source, evidence, source_turn_id, confidence, observed_at,
                                         expires_at, sensitivity, status, use_in_context, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)
            """,
            (kind, value, source, evidence, turn_id, confidence, now.isoformat(), expires.isoformat(),
             "sensitive" if spec.sensitive else "normal", now.isoformat(), now.isoformat()),
        )
        situation_id = int(cursor.lastrowid)
        self.audit.log("situation", situation_id, "create", actor, fact_key=kind)
        return {"action": "created", "situation": self.get(situation_id)}

    def get(self, situation_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM user_situations WHERE id = ?", (situation_id,))

    def _expire(self) -> None:
        now = self.db.now()
        for row in self.db.fetch_all("SELECT id, expires_at FROM user_situations WHERE status = 'active'"):
            if parse_time(row["expires_at"]) <= parse_time(now):
                self.db.execute("UPDATE user_situations SET status = 'expired', updated_at = ? WHERE id = ?", (now, row["id"]))

    def current(self, kinds: Iterable[str] | None = None, include_disabled: bool = False) -> list[dict[str, Any]]:
        """Only unexpired situations: an expired one is never asserted as the current state."""
        self._expire()
        rows = self.db.fetch_all("SELECT * FROM user_situations WHERE status = 'active' ORDER BY observed_at DESC")
        if kinds is not None:
            kinds = set(kinds)
            rows = [r for r in rows if r["kind"] in kinds]
        if not include_disabled:
            rows = [r for r in rows if r["use_in_context"]]
        return rows

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        self._expire()
        return self.db.fetch_all("SELECT * FROM user_situations ORDER BY observed_at DESC, id DESC LIMIT ?", (limit,))

    def set_enabled(self, situation_id: int, enabled: bool, actor: str = "dashboard") -> dict[str, Any]:
        self.db.execute("UPDATE user_situations SET use_in_context = ?, status = CASE WHEN ? = 0 THEN 'disabled' ELSE status END, updated_at = ? WHERE id = ?",
                        (1 if enabled else 0, 1 if enabled else 0, self.db.now(), situation_id))
        self.audit.log("situation", situation_id, "enable" if enabled else "disable", actor)
        return self.get(situation_id)

    def delete(self, situation_id: int, actor: str = "dashboard") -> dict[str, Any]:
        row = self.get(situation_id)
        if not row:
            raise ValueError(f"situation {situation_id} not found")
        with self.db.transaction():
            touched = redact_everywhere(self.db, [row.get("evidence") or ""])
            self.db.execute("DELETE FROM user_situations WHERE id = ?", (situation_id,))
            self.audit.log("situation", situation_id, "delete", actor, fact_key=row["kind"], detail={"redacted_rows": touched})
        checkpoint(self.db)
        return {"deleted_situation": situation_id, "redacted_rows": touched}


# ---------------------------------------------------------------------------
# Interaction preferences
# ---------------------------------------------------------------------------

class PreferenceStore:
    def __init__(self, db):
        self.db = db
        self.audit = ProfileAudit(db)

    def _validate(self, key: str, value: str) -> str | None:
        if key not in PREF_VALUES:
            return "unknown_key"
        allowed = PREF_VALUES[key]
        if allowed is not None and value not in allowed:
            return f"value must be one of {allowed}"
        return None

    def set_explicit(self, key: str, value: str, *, evidence: str | None = None, user_message: str | None = None,
                     source: str = "user_chat", turn_id: str | None = None, actor: str = "memory_agent") -> dict[str, Any]:
        value = (value or "").strip()
        problem = self._validate(key, value)
        if problem:
            return {"action": "rejected", "reason": problem}
        if source != "dashboard" and not evidence_in_message(evidence, user_message):
            return {"action": "rejected", "reason": "evidence_not_in_user_message"}
        # A single conversation ending is not a durable preference. Match the whole utterance so an
        # accompanying explicit future instruction (e.g. “以后别主动找我”) keeps its normal meaning.
        if source != "dashboard" and re.fullmatch(
            r"(?:算了|今天|现在|这会儿|暂时|先|我|，|,)*(?:不想聊|不聊|不想说)(?:天)?了[。.!！…]*",
            _squash(user_message),
        ):
            return {"action": "rejected", "reason": "session_ending_not_durable_preference"}
        now = self.db.now()
        if PREF_VALUES[key] is not None:      # single-valued: replace previous explicit value
            self.db.execute("UPDATE interaction_preferences SET status = 'superseded', updated_at = ? WHERE pref_key = ? AND origin = 'explicit' AND status = 'active'",
                            (now, key))
        elif self.db.fetch_one("SELECT id FROM interaction_preferences WHERE pref_key = ? AND origin = 'explicit' AND status = 'active' AND value = ?",
                               (key, value)):
            return {"action": "already_known"}
        cursor = self.db.execute(
            """
            INSERT INTO interaction_preferences (pref_key, value, origin, evidence, source_turn_id, confidence, observations,
                                                 status, created_at, updated_at)
            VALUES (?, ?, 'explicit', ?, ?, 1.0, 0, 'active', ?, ?)
            """,
            (key, value, evidence, turn_id, now, now),
        )
        self.audit.log("preference", int(cursor.lastrowid), "set_explicit", actor, fact_key=key)
        return {"action": "set", "preference_id": int(cursor.lastrowid)}

    def clear_explicit(self, key: str, value: str | None = None, actor: str = "dashboard") -> int:
        params: list[Any] = [self.db.now(), key]
        extra = ""
        if value is not None:
            extra = " AND value = ?"
            params.append(value)
        cursor = self.db.execute(
            f"UPDATE interaction_preferences SET status = 'superseded', updated_at = ? WHERE pref_key = ? AND origin = 'explicit' AND status = 'active'{extra}",
            params,
        )
        self.audit.log("preference", None, "clear_explicit", actor, fact_key=key)
        return cursor.rowcount

    def observe(self, key: str, value: str, source: str, turn_id: str | None = None) -> None:
        if key not in IMPLICIT_PREF_KEYS or self._validate(key, value):
            return
        self.db.execute("INSERT INTO preference_observations (pref_key, value, source, turn_id, observed_at) VALUES (?, ?, ?, ?, ?)",
                        (key, value, source, turn_id, self.db.now()))
        self._recompute(key)

    def _recompute(self, key: str) -> None:
        window_start = (parse_time(self.db.now()) - timedelta(days=env_float("VIOLET_PREF_WINDOW_DAYS", 14))).isoformat()
        rows = self.db.fetch_all(
            "SELECT value FROM preference_observations WHERE pref_key = ? AND observed_at >= ? ORDER BY id DESC LIMIT ?",
            (key, window_start, env_int("VIOLET_PREF_MAX_OBSERVATIONS", 10)),
        )
        now = self.db.now()
        self.db.execute("DELETE FROM interaction_preferences WHERE pref_key = ? AND origin = 'implicit'", (key,))
        if not rows:
            return
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["value"]] = counts.get(row["value"], 0) + 1
        value, count = max(counts.items(), key=lambda kv: kv[1])
        share = count / len(rows)
        cap = env_float("VIOLET_PREF_IMPLICIT_MAX_CONFIDENCE", 0.6)
        established = len(rows) >= env_int("VIOLET_PREF_MIN_OBSERVATIONS", 4) and share >= env_float("VIOLET_PREF_MIN_SHARE", 0.75)
        self.db.execute(
            """
            INSERT INTO interaction_preferences (pref_key, value, origin, confidence, observations, status, created_at, updated_at)
            VALUES (?, ?, 'implicit', ?, ?, ?, ?, ?)
            """,
            (key, value, round(share * cap, 3), len(rows), "active" if established else "tentative", now, now),
        )

    def effective(self) -> dict[str, Any]:
        """{key: {value, origin, confidence, observations}}; explicit always wins. avoid_topic -> list."""
        result: dict[str, Any] = {}
        rows = self.db.fetch_all("SELECT * FROM interaction_preferences WHERE status = 'active' ORDER BY id")
        for row in rows:
            if row["pref_key"] == "avoid_topic":
                result.setdefault("avoid_topic", []).append({"value": row["value"], "origin": row["origin"], "id": row["id"]})
                continue
            current = result.get(row["pref_key"])
            if current and current["origin"] == "explicit" and row["origin"] == "implicit":
                continue
            result[row["pref_key"]] = {"id": row["id"], "value": row["value"], "origin": row["origin"],
                                       "confidence": row["confidence"], "observations": row["observations"]}
        return result

    def list(self) -> list[dict[str, Any]]:
        return self.db.fetch_all("SELECT * FROM interaction_preferences ORDER BY pref_key, id")

    def delete(self, pref_id: int, actor: str = "dashboard") -> None:
        row = self.db.fetch_one("SELECT * FROM interaction_preferences WHERE id = ?", (pref_id,))
        if not row:
            raise ValueError(f"preference {pref_id} not found")
        with self.db.transaction():
            redact_everywhere(self.db, [row.get("evidence") or ""])
            self.db.execute("DELETE FROM interaction_preferences WHERE id = ?", (pref_id,))
            if row["origin"] == "implicit":
                self.db.execute("DELETE FROM preference_observations WHERE pref_key = ?", (row["pref_key"],))
            self.audit.log("preference", pref_id, "delete", actor, fact_key=row["pref_key"])
        checkpoint(self.db)


class Profile:
    """Convenience bundle of the three stores."""

    def __init__(self, db):
        self.db = db
        self.facts = ProfileStore(db)
        self.situations = SituationStore(db)
        self.preferences = PreferenceStore(db)
        self.audit = ProfileAudit(db)
