"""Forgetting one event memory (phase 9.1.1) -- and how it differs from deleting it.

delete  (Control Center 删除, MemoryAgent retract_memory): the memory row becomes 'deleted'. No retrieval path returns it
        and no context shows it; the revision keeps the old text. The chat history it came from is NOT touched, so the
        user's own words stay in the recent history Chat reads (24 turns / 4000 characters) until they scroll out.
forget  (Control Center 遗忘, retract_memory with forget=true on the user's explicit request): Violet should not know it
        any more, from any path:
          1. in the turn the memory came from (its source_message_ids, or the turn's memory job), the clauses of the
             user's message that are about it, and Violet's replies of that same turn that are about it, are replaced by
             REDACTED -- in messages (history, recent-message notes, MemoryAgent's reference) and in that turn's
             memory job. "About it" = sharing a distinctive word with the memory (recall_terms); other clauses of the
             same message ("我家养了只柯基叫豆豆") and other conversations are left alone -- no global keyword filter.
          2. the memory itself is hard-deleted with its revisions (profile_store.purge_event_memory).
          3. the audit log keeps metadata only (ids and counts, never the text).
        Not reached: a source that was never recorded (rows from before phase 9.1: the report says so), the same thing
        said again in another turn, Violet's later replies in other turns, messages already delivered to WeChat, backups.

Phase 9.1.2 (D-P911-R2): in p911-real-1 Intent sent "前男友那件事你就忘掉吧" to the Performer (no memory tools: a failed
task holding the memory's text), Chat told the user it could not be erased, and the background MemoryAgent then
forgot it. Now an explicit request to forget is settled BEFORE the reply (settle_forget_request, like reminders):
the target is the one memory the user's words point at; it is forgotten (or, when no single memory is certain, nothing
is) and Chat is told the real outcome (forget_note); the turn never becomes a Performer task; the MemoryAgent of the
same turn defers to that outcome (settled_forget). The request's clauses are also cleaned from task_sessions and the
raw inbound / outbound rows, and the memory's exact text from task records (profile_store._REDACT_COLUMNS).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ai.memory_model import recall_terms
from ai.memory_sources import memory_source
from ai.profile_store import REDACTED, ProfileAudit, checkpoint, purge_event_memory

# The user's own words asking Violet to forget something. Not "我忘了带伞" / "别忘了提醒我" / "那张照片删掉吧": a bare 忘了 or
# 删掉 is not enough, and a negation right before 忘 (别 / 不 / 没 / 要 / 会) is not a request.
FORGET_REQUEST_RE = re.compile(
    r"(?<![别不没要会])(?:忘掉|忘了吧|忘记吧|忘了它)|别记(?:住|着|得)?|不要记(?:住|着|得)?|别再记|不想让你记|当我没说"
    r"|(?:删掉|删了|删除).{0,4}(?:记忆|记录|这件事|那件事)|(?:记忆|记录|这件事|那件事).{0,4}(?:删掉|删了|删除)")
FORGET_WORDS_RE = re.compile(r"忘掉|忘了吧|忘记吧|忘了它|别记(?:住|着|得)?|不要记(?:住|着|得)?|别再记|不想让你记|当我没说|删掉|删了|删除")
_CLAUSE_RE = re.compile(r"[^，,。.!！?？；;：:\n~～…]+[，,。.!！?？；;：:\n~～…]*")
_PUNCT = "，,。.!！?？；;：:\n~～… "
_REPEATED = re.compile(re.escape(REDACTED) + r"(?:[，,；;\s]*" + re.escape(REDACTED) + r")+")


def _about(text: str, topic: str) -> bool:
    return bool(text and topic and recall_terms(text, topic))


def _redact_clauses(text: str, topic: str) -> tuple[str, list[str]]:
    """(the text with the clauses about `topic` replaced, the clause bodies that were replaced)."""
    removed, parts = [], []
    for clause in _CLAUSE_RE.findall(text or "") or [text or ""]:
        body = clause.rstrip(_PUNCT)
        if body.strip() and _about(body, topic):
            removed.append(body)
            parts.append(REDACTED + clause[len(body):])
        else:
            parts.append(clause)
    return _REPEATED.sub(REDACTED, "".join(parts)), removed


def plan_forget(db, memory: dict[str, Any]) -> dict[str, Any]:
    """What forgetting `memory` would change in the chat history, without changing anything."""
    source = memory_source(db, memory)
    topic = memory.get("content") or ""
    changes: list[dict[str, Any]] = []
    for message_id in source["message_ids"]:
        row = db.fetch_one("SELECT id, role, content FROM messages WHERE id = ?", (message_id,))
        if not row or row["role"] != "user":
            continue
        new_text, removed = _redact_clauses(row["content"] or "", topic)
        if removed:
            changes.append({"id": row["id"], "role": "user", "old": row["content"], "new": new_text, "removed": removed})
        said = " ".join(removed)
        later = db.fetch_one("SELECT MIN(id) AS id FROM messages WHERE role = 'user' AND id > ?", (message_id,))
        end = later["id"] if later and later["id"] is not None else None
        replies = db.fetch_all(
            "SELECT id, content FROM messages WHERE role = 'assistant' AND id > ? AND (? IS NULL OR id < ?) ORDER BY id",
            (message_id, end, end))
        for reply in replies:                      # Violet's lines of the same turn: one line is one thought
            if _about(reply["content"] or "", topic) or (said and _about(reply["content"] or "", said)):
                changes.append({"id": reply["id"], "role": "assistant", "old": reply["content"], "new": REDACTED,
                                "removed": [reply["content"]]})
    return {"source": source, "changes": changes}


# Copies of what was said that no context reads but that are still stored (phase 9.1.2): the forgotten clauses are
# replaced there too when they are long enough to be that exact sentence (>= 4 characters).
_COPY_COLUMNS = (("task_sessions", "task_name"), ("task_sessions", "task_description"), ("task_sessions", "task_info"),
                 ("task_sessions", "last_question"), ("task_sessions", "result"), ("task_sessions", "error_message"),
                 ("inbound_messages", "content"), ("outbound_messages", "content"))


def forget_event_memory(db, memory_id: int, actor: str = "dashboard", turn_id: str | None = None) -> dict[str, Any]:
    """Forget one event memory (see the module docstring). One transaction: all of it happens, or none of it."""
    memory = db.get_event_memory(memory_id)
    if not memory:
        raise ValueError(f"memory {memory_id} not found")
    plan = plan_forget(db, memory)
    by_id = {c["id"]: c for c in plan["changes"]}
    copies = 0
    with db.transaction():
        phrases = {p for c in plan["changes"] for p in c["removed"] if len(re.sub(r"\s+", "", p)) >= 4}
        for table, column in _COPY_COLUMNS:
            for phrase in phrases:
                try:
                    copies += db.execute(f"UPDATE {table} SET {column} = REPLACE({column}, ?, ?) WHERE {column} IS NOT NULL "
                                         f"AND instr({column}, ?) > 0", (phrase, REDACTED, phrase)).rowcount
                except Exception:                        # a table this database does not have
                    break
        for change in plan["changes"]:
            db.execute("UPDATE messages SET content = ? WHERE id = ?", (change["new"], change["id"]))
        for job in db.fetch_all(
                f"SELECT id, user_message, assistant_reply FROM memory_jobs WHERE user_message_id IN "
                f"({', '.join('?' for _ in plan['source']['message_ids']) or 'NULL'})", plan["source"]["message_ids"]):
            user_text, reply = job["user_message"] or "", job["assistant_reply"] or ""
            for change in by_id.values():
                for phrase in change["removed"]:
                    if change["role"] == "user":
                        user_text = user_text.replace(phrase, REDACTED)
                    reply = reply.replace(phrase, REDACTED)
            db.execute("UPDATE memory_jobs SET user_message = ?, assistant_reply = ? WHERE id = ?",
                       (_REPEATED.sub(REDACTED, user_text), _REPEATED.sub(REDACTED, reply), job["id"]))
        purged = purge_event_memory(db, memory_id, actor=actor)
        ProfileAudit(db).log("event_memory", memory_id, "forget", actor, detail={
            "source": plan["source"]["state"], "messages_changed": sorted(by_id),
            "clauses_removed": sum(len(c["removed"]) for c in plan["changes"]),
            "redacted_rows": purged["redacted_rows"], "copies_redacted": copies, "turn_id": turn_id})
    checkpoint(db)
    return {"forgotten_memory": memory_id, "source": plan["source"]["state"],
            "messages_changed": sorted(by_id), "history_note": None if by_id else (
                "找不到这条记忆的来源消息，聊天记录没有改动" if plan["source"]["state"] != "known"
                else "来源消息里没有找到和它有关的句子，聊天记录没有改动")}


# ----------------------------------------------------------------------------------------------- before the reply

def is_forget_request(text: str | None) -> bool:
    return bool(FORGET_REQUEST_RE.search(text or ""))


def forget_target(db, user_message: str) -> dict[str, Any]:
    """The one event memory the request points at: memories sharing a distinctive word with the user's words (the
    forget phrases themselves removed); the best one only when it shares strictly more words than the next."""
    from ai.recall_search import user_word_recall
    about = FORGET_WORDS_RE.sub(" ", user_message or "")
    found = user_word_recall(db, about, limit=5)
    if not found:
        return {"status": "not_found", "memory_id": None}
    scored = sorted(found, key=lambda m: len(m["retrieval"]["matched_terms"]), reverse=True)
    if len(scored) > 1 and len(scored[0]["retrieval"]["matched_terms"]) == len(scored[1]["retrieval"]["matched_terms"]):
        return {"status": "ambiguous", "memory_id": None, "candidate_ids": [m["id"] for m in scored]}
    return {"status": "found", "memory_id": scored[0]["id"]}


def settle_forget_request(db, user_message: str, turn_id: str | None) -> dict[str, Any] | None:
    """Phase 9.1.2: an explicit request to forget is carried out BEFORE the reply. None when the message is not one.
    status forgotten (done: memory and its source words) / not_found / ambiguous (nothing deleted) / failed."""
    if not is_forget_request(user_message):
        return None
    target = forget_target(db, user_message)
    outcome = {"status": target["status"], "memory_id": target["memory_id"], "messages_changed": []}
    if target["status"] == "found":
        try:
            report = forget_event_memory(db, target["memory_id"], actor="before_reply", turn_id=turn_id)
            return {**outcome, "status": "forgotten", "messages_changed": report["messages_changed"],
                    "history_note": report["history_note"]}
        except Exception:
            outcome["status"] = "failed"
    # nothing was deleted: recorded, so the MemoryAgent of this turn does not delete behind the reply's back
    ProfileAudit(db).log("event_memory", None, "forget_unresolved", "before_reply",
                         detail={"status": outcome["status"], "turn_id": turn_id,
                                 "candidates": target.get("candidate_ids") or []})
    return outcome


def settled_forget(db, turn_id: str | None) -> dict[str, Any] | None:
    """What settle_forget_request did in this turn (from the audit log), or None."""
    if not turn_id:
        return None
    for row in db.fetch_all("SELECT action, entity_id, detail FROM profile_audit_log WHERE action IN "
                            "('forget', 'forget_unresolved') AND actor = 'before_reply' ORDER BY id DESC LIMIT 20"):
        try:
            detail = json.loads(row["detail"] or "{}")
        except ValueError:
            continue
        if detail.get("turn_id") == turn_id:
            return {"status": "forgotten" if row["action"] == "forget" else detail.get("status"),
                    "memory_id": row["entity_id"]}
    return None


def forget_note(outcome: dict[str, Any]) -> str:
    """What the reply must know about this turn's request to forget (facts only)."""
    if outcome["status"] == "forgotten":
        return ("【遗忘】用户要你忘掉的那件事已经处理好了（系统里的真实结果）：那条记忆已经删除，它最早被说出的那一轮聊天里"
                "关于这件事的原话也已抹掉。之后的聊天里再提到过的话、已经发出去的消息不在处理范围内。"
                "回复里简短确认就行，不要复述那件事的内容，也不要说做不到。")
    reason = {"not_found": "记忆库里没有找到能确定是哪一条的记录", "ambiguous": "找到好几条可能相关的记录，不能确定是哪一条",
              "failed": "处理时出错了"}.get(outcome["status"], "没能处理")
    return (f"【遗忘】用户像是要你忘掉某件事，但{reason}，这次什么都没有删除。不要说已经忘掉了或删掉了；"
            "可以请用户说得具体一点是哪件事，也可以告诉用户在管理后台删除。如果用户其实不是要你忘掉什么，就当没有这条。")
