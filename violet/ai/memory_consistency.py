"""One corrected fact, several stored copies (phase 9.1.2, D-P911-R1).

The real session p911-real-1: the memory model saved "猫叫年糕" twice -- as a profile fact and as an event memory of
kind=fact. The user corrected it to 汤圆; the profile fact was superseded, the event memory stayed active, and ten days
later Chat got both names.

After a correction, the copies of the SAME fact elsewhere are brought in line -- only where that is certain:
  * the change must be one short replacement ("年糕" -> "汤圆", "豆豆" -> "豆包"); a rewritten value changes nothing
    elsewhere (changed_segment returns None);
  * a copy must contain the old part, not already the new one, and share at least one other distinctive word with the
    old value (it is about the same thing -- "用户喜欢吃年糕" is not about the cat);
  * event memories of kind=fact (a stable fact, like the profile) get the old part replaced in place, with a revision
    (source user_confirmed / dashboard, like the correction itself);
  * event / background / legacy memories and core memories are records of what happened or old free text: they are
    NOT rewritten. A 'correction_conflict' revision records the pending conflict; recall lines and the legacy section
    then say "其中“年糕”已被用户纠正为“汤圆”" until someone resolves it (dashboard edit or a later rewrite);
  * a profile fact holding the old part is corrected through ProfileStore.correct when the correction started from an
    event memory (correct_memory), with the user's words of that turn.
No new tables or columns. find_conflicts() is the read-only check of older data (tools/memory_conflicts.py).
"""

from __future__ import annotations

import difflib
from typing import Any

from ai.memory_model import _bigrams, shared_topic_terms

CONFLICT_ACTION = "correction_conflict"
_PUNCT = set("，,。.!！?？；;：:、 \n\t（）()“”\"'")
MAX_SEGMENT = 12


def changed_segment(old: str | None, new: str | None) -> tuple[str, str] | None:
    """(old part, new part) when `new` differs from `old` by ONE short replacement, widened with unchanged neighbours
    to at least two characters ("豆豆" / "豆包" differ in one character: the pair is ("豆豆", "豆包")). None otherwise."""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        return None
    ops = [op for op in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if op[0] != "equal"]
    if len(ops) != 1 or ops[0][0] != "replace":
        return None
    _, i1, i2, j1, j2 = ops[0]
    while i2 - i1 < 2:                                # outside the replaced part both strings are equal
        if i1 > 0 and old[i1 - 1] not in _PUNCT:
            i1, j1 = i1 - 1, j1 - 1
        elif i2 < len(old) and old[i2] not in _PUNCT:
            i2, j2 = i2 + 1, j2 + 1
        else:
            return None
    old_part, new_part = old[i1:i2], new[j1:j2]
    if any(ch in _PUNCT for ch in old_part + new_part) or len(old_part) > MAX_SEGMENT or not new_part:
        return None
    return old_part, new_part


# Characters that say nothing about what a text is about (particles, pronouns, "用户", naming words).
_FUNCTION_CHARS = set("的了是在有和与很也都就还又再一个这那些我你他她它们用户叫名说着过把被给对向从到为以及而且但或吧呢啊吗呀哦嗯"
                      "其实之前现在已经今天")


def about_same(old_value: str, content: str, old_part: str) -> bool:
    """Strong: `content` shares a distinctive two-character word with `old_value` besides the changed part. Required
    before a stored fact is rewritten."""
    return bool(set(shared_topic_terms(old_value, content)) - _bigrams(old_part))


def maybe_same(old_value: str, content: str, old_part: str) -> bool:
    """Weak: they share a content character besides the changed part ("用户的猫年糕把花盆打翻了" and "养了一只猫，名叫年糕"
    share 猫). Enough to FLAG a conflict (nothing is rewritten on this)."""
    def chars(text: str) -> set[str]:
        return {c for c in text.replace(old_part, " ") if "一" <= c <= "鿿"} - _FUNCTION_CHARS
    return about_same(old_value, content, old_part) or bool(chars(old_value) & chars(content))


def stale_copy(old_value: str, content: str | None, seg: tuple[str, str], strong: bool = True) -> bool:
    old_part, new_part = seg
    content = content or ""
    if old_part not in content or new_part in content:
        return False
    return (about_same if strong else maybe_same)(old_value, content, old_part)


def pending_conflicts(db, table: str, memory_id: int, content: str | None) -> list[tuple[str, str]]:
    """Conflicts recorded for one memory that still apply (its text still has the old part, not the new one)."""
    out = []
    for r in db.fetch_all("SELECT old_content, new_content FROM memory_revisions WHERE memory_table = ? AND memory_id = ? "
                          "AND action = ? ORDER BY id", (table, memory_id, CONFLICT_ACTION)):
        old_part, new_part = r["old_content"] or "", r["new_content"] or ""
        if old_part and old_part in (content or "") and new_part not in (content or "") \
                and (old_part, new_part) not in out:
            out.append((old_part, new_part))
    return out


def conflict_tag(conflicts: list[tuple[str, str]]) -> str:
    return "；".join(f"其中“{o}”已被用户纠正为“{n}”" for o, n in conflicts)


def propagate_correction(db, old_value: str, new_value: str, *, source: str, origin: str,
                         turn_id: str | None = None, exclude_memory_id: int | None = None,
                         profile=None, evidence: str | None = None, user_message: str | None = None,
                         exclude_fact_id: int | None = None) -> dict[str, Any]:
    """Bring the other stored copies of a corrected fact in line (see the module docstring). Returns
    {"segment": {"old", "new"} | None, "updated": [...], "conflicts": [...], "profile": [...]}."""
    seg = changed_segment(old_value, new_value)
    report: dict[str, Any] = {"segment": {"old": seg[0], "new": seg[1]} if seg else None,
                              "updated": [], "conflicts": [], "profile": []}
    if seg is None:
        return report
    old_part, new_part = seg
    reason = f"同一事实已纠正（{origin}）：“{old_part}”→“{new_part}”"
    now = db.now()
    for m in db.fetch_all("SELECT id, kind, content FROM event_memories WHERE status != 'deleted' ORDER BY id"):
        if m["id"] == exclude_memory_id or not stale_copy(old_value, m["content"], seg, strong=False):
            continue
        if m["kind"] == "fact" and stale_copy(old_value, m["content"], seg):     # certain: rewrite in place
            new_content = m["content"].replace(old_part, new_part)
            db.execute("UPDATE event_memories SET content = ?, updated_at = ? WHERE id = ?", (new_content, now, m["id"]))
            db.add_memory_revision("event_memories", m["id"], "correct", old_content=m["content"],
                                   new_content=new_content, reason=reason, source=source, source_turn_id=turn_id)
            report["updated"].append({"table": "event_memories", "id": m["id"]})
        elif not pending_conflicts(db, "event_memories", m["id"], m["content"]):
            db.add_memory_revision("event_memories", m["id"], CONFLICT_ACTION, old_content=old_part,
                                   new_content=new_part, reason=reason + "；不能确定是同一事实，或这条是事件记录，没有自动改写",
                                   source=source, source_turn_id=turn_id)
            report["conflicts"].append({"table": "event_memories", "id": m["id"]})
    for c in db.fetch_all("SELECT id, content FROM core_memories ORDER BY id"):
        if stale_copy(old_value, c["content"], seg, strong=False) \
                and not pending_conflicts(db, "core_memories", c["id"], c["content"]):
            db.add_memory_revision("core_memories", c["id"], CONFLICT_ACTION, old_content=old_part, new_content=new_part,
                                   reason=reason + "；旧核心记忆没有自动改写", source=source, source_turn_id=turn_id)
            report["conflicts"].append({"table": "core_memories", "id": c["id"]})
    if profile is not None:
        for f in profile.list_facts(["active"]):
            if f["id"] == exclude_fact_id or not stale_copy(old_value, f["value"], seg):
                continue
            result = profile.correct(f["fact_key"], f["value"].replace(old_part, new_part), fact_id=f["id"],
                                     evidence=evidence, user_message=user_message,
                                     source="dashboard" if source == "dashboard" else "user_chat",
                                     actor="memory_consistency", source_turn_id=turn_id, propagate=False)
            report["profile"].append({"fact_id": f["id"], "action": result["action"]})
    return report


# ----------------------------------------------------------------------------------------------- check of older data

def find_conflicts(db) -> list[dict[str, Any]]:
    """Read-only. What older data may hold (nothing is changed):
      duplicate_active_facts  two active profile facts of one key that differ by one short part (豆豆 / 豆包)
      stale_copy              a superseded fact's old part still in an active memory / core memory / profile fact
      pending_conflict        a correction_conflict revision that still applies"""
    out: list[dict[str, Any]] = []
    facts = db.fetch_all("SELECT id, fact_key, value, status, superseded_by FROM user_profile_facts ORDER BY id")
    active = [f for f in facts if f["status"] == "active"]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            seg = changed_segment(a["value"], b["value"]) if a["fact_key"] == b["fact_key"] else None
            if seg:
                out.append({"kind": "duplicate_active_facts", "fact_ids": [a["id"], b["id"]], "key": a["fact_key"],
                            "old": seg[0], "new": seg[1]})
    by_id = {f["id"]: f for f in facts}
    memories = db.fetch_all("SELECT id, kind, status, content FROM event_memories WHERE status != 'deleted' ORDER BY id")
    cores = db.fetch_all("SELECT id, content FROM core_memories ORDER BY id")
    for f in facts:
        successor = by_id.get(f["superseded_by"]) if f["status"] == "superseded" else None
        seen = set()
        while successor is not None and successor["status"] == "superseded" and successor["id"] not in seen:
            seen.add(successor["id"])
            successor = by_id.get(successor["superseded_by"])
        seg = changed_segment(f["value"], successor["value"]) if successor else None
        if not seg:
            continue
        for m in memories:
            if stale_copy(f["value"], m["content"], seg, strong=False):
                out.append({"kind": "stale_copy", "fact_id": f["id"], "table": "event_memories", "id": m["id"],
                            "memory_kind": m["kind"], "old": seg[0], "new": seg[1]})
        for c in cores:
            if stale_copy(f["value"], c["content"], seg, strong=False):
                out.append({"kind": "stale_copy", "fact_id": f["id"], "table": "core_memories", "id": c["id"],
                            "old": seg[0], "new": seg[1]})
        for a in active:
            if a["id"] != successor["id"] and stale_copy(f["value"], a["value"], seg):
                out.append({"kind": "stale_copy", "fact_id": f["id"], "table": "user_profile_facts", "id": a["id"],
                            "old": seg[0], "new": seg[1]})
    contents = {("event_memories", m["id"]): m["content"] for m in memories}
    contents.update({("core_memories", c["id"]): c["content"] for c in cores})
    for r in db.fetch_all("SELECT DISTINCT memory_table, memory_id FROM memory_revisions WHERE action = ?", (CONFLICT_ACTION,)):
        key = (r["memory_table"], r["memory_id"])
        if key not in contents:
            continue
        for o, n in pending_conflicts(db, *key, contents[key]):
            out.append({"kind": "pending_conflict", "table": key[0], "id": key[1], "old": o, "new": n})
    return out
