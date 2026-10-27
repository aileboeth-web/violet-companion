"""Bounded lexical recall for model-expanded queries; no embeddings or topic dictionary.

The first substantive phrase anchors recall. Later speculative qualifiers can
rank results, but cannot erase that phrase. A repeated compound suffix may be
used only when the query itself supplies multiple variants of the same head.
Only when the first phrase matches no memory at all are the query's later
variants tried, in order (phase 9.1).
"""
from __future__ import annotations

import re

_PREFIX = re.compile(r"^(?:(?:用户|我的|关于|有关|请帮我|帮我|查询|查找|回忆|你还记得|你记得|还记得|上次|之前|以前|先前|(?:提|说|聊)(?:过|到)?)(?:的)?)+")
_SUFFIX = re.compile(r"(?:相关(?:的)?(?:事情|内容|信息)|准备情况|的情况|吗|么)+$")
_NOISE = {"上次", "之前", "之前提到", "提到", "用户", "事情", "相关", "情况", "计划", "时间", "内容", "准备", "准备情况", "结果"}


def query_topics(query: str) -> list[str]:
    """Split explicit phrases, not arbitrary Chinese character pairs inside a topic."""
    from ai.memory_model import recall_search_text
    parts = re.split(r"[\s,，、;；。!?！？/|]+|或者|或是|或", recall_search_text(query).lower())
    topics = []
    for part in parts:
        part = _PREFIX.sub("", part)
        part = _SUFFIX.sub("", part)
        if part and part not in _NOISE and part not in topics:
            topics.append(part)
        if len(topics) == 16:
            break
    return topics


def contains_topic(topic: str, content: str) -> bool:
    if topic.isascii():
        return bool(re.search(r"(?<!\w)" + re.escape(topic) + r"(?!\w)", content.lower()))
    return topic in re.sub(r"\s+", "", content.lower())


def recall_focus(topics: list[str], contents: list[str]) -> str | None:
    if not topics:
        return None
    primary = topics[0]
    if any(contains_topic(primary, c) for c in contents):
        return primary
    # Query-supported variants, e.g. A+head / B+head. Never cut a lone
    # compound into arbitrary prefix matches (course+exam is not any course).
    suffixes = [primary[-size:] for size in range(min(16, len(primary)), 1, -1)
                if any(t != primary and t.endswith(primary[-size:]) for t in topics[1:])]
    shared = next((s for s in suffixes if s not in _NOISE and any(contains_topic(s, c) for c in contents)), None)
    if shared:
        return shared
    # Phase 9.1 (A3): the first phrase has no literal evidence anywhere, so it cannot be "erased" -- it matched
    # nothing. The query's own later variants are then tried in order (the intent prompt asks for synonyms:
    # "失眠 睡不着 焦虑"); before, they could only rank, so a synonym query found nothing. Two characters at least.
    return next((t for t in topics[1:] if len(t) >= 2 and t not in _NOISE
                 and any(contains_topic(t, c) for c in contents)), None)


def user_word_recall(db, user_message: str, exclude_ids=(), limit: int | None = None) -> list[dict]:
    """Event memories the user's own words are about, found by code (phase 9.1, A2).

    Before 9.1 a memory reached Chat only when IntentAgent chose to call query_memory with a matching first word
    (R-M2). This adds, without any model call, at most VIOLET_MEMORY_WORD_RECALL_LIMIT memories that share a
    distinctive word with the message ("面试结束啦" -> "用户下周三下午有一场实习面试"). Deleted and disabled memories
    are never returned; nothing is written. Lexical only: a synonym without a shared word is not found."""
    from ai.memory_model import recall_terms
    from ai.settings import env_bool, env_int
    if not env_bool("VIOLET_MEMORY_WORD_RECALL", True) or len(re.sub(r"\s+", "", user_message or "")) < 2:
        return []
    limit = env_int("VIOLET_MEMORY_WORD_RECALL_LIMIT", 3) if limit is None else limit
    if limit <= 0:
        return []
    exclude = {int(i) for i in exclude_ids if i is not None}
    db.refresh_event_states()               # a plan whose time has passed is shown as 进展未知, not 计划中
    scored = []
    for memory in db.list_event_memories(limit=db.search_window()):
        if memory["id"] in exclude or memory.get("status") == "deleted" or not memory.get("use_in_context", 1):
            continue
        terms = recall_terms(user_message, memory.get("content") or "")
        if terms:
            scored.append((len(terms), memory.get("status") == "active", memory.get("updated_at") or "", memory, terms))
    scored.sort(key=lambda s: s[:3], reverse=True)
    return [{"id": m["id"], "content": m["content"], "category": m.get("category"), "kind": m.get("kind"),
             "progress": m.get("progress"), "event_time": m.get("event_time"), "status": m.get("status"),
             "memory_strength": round(float(m.get("forget_weight") or 0), 2), "similarity": 0.0,
             "retrieval": {"reason": "user_words", "matched_terms": terms}}
            for _, _, _, m, terms in scored[:limit]]


def recall_score(query: str, content: str, topics: list[str], focus: str | None,
                 original_score: float, similarity) -> dict:
    matched = [t for t in topics if contains_topic(t, content)]
    anchor_matched = bool(focus and contains_topic(focus, content))
    topic_score = similarity(focus, content) if anchor_matched else 0.0
    # Keep the old metric for ordering; require lexical evidence when a topic
    # was extracted, so scattered shared characters cannot invent a match.
    accepted_topic = anchor_matched if topics else bool(original_score)
    return {"original_similarity": original_score, "topic_similarity": topic_score,
            "similarity": max(original_score, topic_score) if accepted_topic else 0.0,
            "topics": topics, "focus": focus, "matched_terms": matched,
            "reason": "lexical_anchor" if anchor_matched else "no_lexical_anchor" if topics else "original_similarity"}
