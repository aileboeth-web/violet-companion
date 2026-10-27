"""Conversation state used by proactive behaviour: topic fatigue and engagement.

* Every proactive message records the topic(s) Violet raised (``topic_mentions``,
  source ``violet_proactive`` / ``violet_commitment``); memory-linked topics use the
  key ``memory:<id>`` so rephrasing the same event is still the same topic.
* The next user turn marks those mentions engaged / not engaged
  (``is_engaged_reply``); mentions nobody answered within
  VIOLET_UNANSWERED_AFTER_HOURS count as not engaged.
* A topic is *cooling* when Violet raised it repeatedly without engagement since
  the user last brought it up themselves. There is no fixed mute timer: as
  soon as the user mentions the topic (source ``user``) or engages with it,
  it is no longer cooling.
* The *cold streak* is the number of proactive messages sent since the user's
  last engaged turn; the scheduler uses it to stretch intervals and to stop
  generic proactive chat until the user re-engages.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Iterable

from ai.memory_model import classify_reply, mentions_topic, parse_time, touches_topic
from ai.settings import env_float, env_int

# violet_reply: Violet asked a follow-up question about a remembered topic inside a
# normal reply (phase 3) -- counts like a proactive raise for topic fatigue.
VIOLET_SOURCES = ("violet_proactive", "violet_commitment", "violet_reply", "violet_life", "violet_web")
ENGAGED, DISENGAGED, NEUTRAL = 1, 0, 2
USER_SOURCE = "user"
LAST_ENGAGED_KEY = "last_engaged_user_turn_at"
GENERIC_TOPIC = "general_chat"


def memory_topic_key(memory_id: int) -> str:
    return f"memory:{int(memory_id)}"


# Phase 4: Violet sharing her own life. "violet_life" tracks the habit as a whole
# (does the user care at all?), "life:<id>" one event (never share it twice).
LIFE_TOPIC = "violet_life"
# The user asking about Violet herself ("你最近在干嘛", "你呢" ...).
ASK_ABOUT_VIOLET_RE = re.compile(r"(你最近|你在干嘛|你在干什么|你在做什么|你呢|你今天|你这几天|你周末|你平时|你在看|你在听|你在读|你喜欢|你过得)")


def life_topic_key(event_id: int) -> str:
    return f"life:{int(event_id)}"


# Phase 5B: Violet mentioning something she found on the web.
WEB_TOPIC = "violet_web"


def web_topic_key(finding_id: int) -> str:
    return f"web:{int(finding_id)}"


def normalize_topic_key(value: Any) -> str:
    text = re.sub(r"\s+", "_", str(value or "").strip().lower())
    return text[:48] or GENERIC_TOPIC


class TopicTracker:
    def __init__(self, db):
        self.db = db

    # -- recording -------------------------------------------------------------
    def record_eve_topic(
        self,
        topic_key: str | None,
        memory_ids: Iterable[int] = (),
        proactive_event_id: int | None = None,
        source: str = "violet_proactive",
    ) -> list[int]:
        ids = []
        key = normalize_topic_key(topic_key)
        ids.append(self.db.add_topic_mention(key, source, proactive_event_id=proactive_event_id))
        for memory_id in memory_ids or ():
            ids.append(self.db.add_topic_mention(memory_topic_key(memory_id), source, memory_id=int(memory_id),
                                                 proactive_event_id=proactive_event_id))
        return ids

    def record_user_topic(self, memory_id: int | None = None, topic_key: str | None = None) -> None:
        """The user brought the topic up themselves: it is immediately fine to continue it."""
        if memory_id is not None:
            self.db.add_topic_mention(memory_topic_key(memory_id), USER_SOURCE, memory_id=int(memory_id), user_engaged=1)
        if topic_key:
            self.db.add_topic_mention(normalize_topic_key(topic_key), USER_SOURCE, user_engaged=1)

    def evaluate(self, text: str) -> dict[str, Any]:
        """Classify this user turn against Violet's pending raises.

        * pending raises get 1 (engaged), 0 (bare acknowledgement) or 2 (neutral);
          only 0 counts towards cooling and the cold streak;
        * a turn with nothing pending that starts a new conversation (no Violet
          message in the last VIOLET_USER_INITIATED_GAP_MINUTES) is user-initiated:
          the user came back on their own, so the cold streak is reset.
        """
        category = classify_reply(text)
        pending = [m for m in self.db.list_topic_mentions(sources=VIOLET_SOURCES, limit=200) if m["user_engaged"] is None]
        value = {"engaged": ENGAGED, "disengaged": DISENGAGED, "neutral": NEUTRAL}[category]
        if pending:
            self.db.execute(
                f"UPDATE topic_mentions SET user_engaged = ?, evaluated_at = ? WHERE id IN ({', '.join('?' for _ in pending)})",
                (value, self.db.now(), *[m["id"] for m in pending]),
            )
        user_initiated = not pending and self._starts_new_conversation()
        if category != "disengaged" or user_initiated:
            self.db.set_proactive_setting(LAST_ENGAGED_KEY, self.db.now())
        return {"category": category, "user_initiated": user_initiated,
                "evaluated": [(m["id"], m["source"]) for m in pending]}

    def _starts_new_conversation(self) -> bool:
        last_eve = self.db.get_latest_message_at(role="assistant")
        if not last_eve:
            return True
        gap = (parse_time(self.db.now()) - parse_time(last_eve)).total_seconds() / 60
        return gap >= env_float("VIOLET_USER_INITIATED_GAP_MINUTES", 30)

    def evaluate_user_turn(self, text: str) -> bool:
        """Backward-compatible wrapper: True unless this turn was a brush-off of a pending raise."""
        result = self.evaluate(text)
        return result["category"] != "disengaged" or result["user_initiated"]

    # -- phase 3: who raised a remembered topic ---------------------------------------
    def note_user_mentions(self, user_message: str, recalled: list[dict[str, Any]]) -> list[int]:
        """Recalled memories the user's own words are clearly about -> user-raised right now."""
        raised = []
        for memory in recalled or []:
            if memory.get("id") is None or not memory.get("content"):
                continue
            if mentions_topic(user_message, memory["content"]):
                self.record_user_topic(memory_id=int(memory["id"]))
                raised.append(int(memory["id"]))
        return raised

    def note_eve_reply(self, reply_text: str, recalled: list[dict[str, Any]], user_raised: list[int]) -> list[int]:
        """Violet asked about a remembered topic the user did not raise -> an Violet follow-up."""
        if not reply_text or not any(q in reply_text for q in ("?", "？")):
            return []
        followed = []
        for memory in recalled or []:
            memory_id = memory.get("id")
            if memory_id is None or int(memory_id) in set(user_raised or []) or not memory.get("content"):
                continue
            if touches_topic(reply_text, memory["content"]):
                self.db.add_topic_mention(memory_topic_key(int(memory_id)), "violet_reply", memory_id=int(memory_id))
                followed.append(int(memory_id))
        return followed

    def expire_unanswered(self) -> int:
        """Mentions left unanswered for VIOLET_UNANSWERED_AFTER_HOURS count as not engaged."""
        cutoff = parse_time(self.db.now()) - timedelta(hours=env_float("VIOLET_UNANSWERED_AFTER_HOURS", 6))
        stale = [m["id"] for m in self.db.list_topic_mentions(sources=VIOLET_SOURCES, limit=200)
                 if m["user_engaged"] is None and parse_time(m["created_at"]) and parse_time(m["created_at"]) < cutoff]
        self.db.set_topic_engagement(stale, False)
        return len(stale)

    # -- queries ------------------------------------------------------------------
    def topic_state(self, topic_key: str) -> dict[str, Any]:
        """Count Violet's unengaged raises of this topic since the user last brought it up."""
        cold = raised = 0
        engaged_after = False
        for mention in self.db.list_topic_mentions(topic_key=topic_key, limit=100):
            if mention["source"] == USER_SOURCE:
                break
            if mention["source"] not in VIOLET_SOURCES:
                continue
            if mention["user_engaged"] == NEUTRAL:
                continue        # a short but friendly reaction neither cools nor warms the topic
            raised += 1
            if mention["user_engaged"] == 1:
                engaged_after = True
                break
            if mention["user_engaged"] == 0:
                cold += 1
        threshold = env_int("VIOLET_TOPIC_COOLING_THRESHOLD", 2)
        max_raises = env_int("VIOLET_TOPIC_MAX_UNENGAGED_RAISES", 3)
        cooling = not engaged_after and (cold >= threshold or raised >= max_raises)
        return {
            "topic_key": topic_key,
            "violet_raises_since_user": raised,
            "unengaged": cold,
            "priority": round(0.5 ** cold, 3),
            "cooling": cooling,
        }

    def cooling_topics(self, lookback: int = 300) -> list[dict[str, Any]]:
        seen: dict[str, None] = {}
        for mention in self.db.list_topic_mentions(sources=VIOLET_SOURCES, limit=lookback):
            seen.setdefault(mention["topic_key"], None)
        result = []
        for key in seen:
            state = self.topic_state(key)
            if state["cooling"]:
                if key.startswith("memory:"):
                    memory = self.db.get_event_memory(int(key.split(":", 1)[1]))
                    state["memory_id"] = memory["id"] if memory else None
                    # Phase 9.1 (A5): a memory the user deleted or disabled keeps its cooling count, but its text
                    # never goes back into a model's context through this list.
                    usable = memory and memory.get("status") != "deleted" and memory.get("use_in_context", 1)
                    state["content"] = memory["content"] if usable else None
                result.append(state)
        return result

    def cold_streak(self) -> int:
        """Proactive chat messages sent since the user's last engaged turn."""
        since = self.db.get_proactive_setting(LAST_ENGAGED_KEY)
        params: list[Any] = ["sent", "commitment_reminder"]
        clause = ""
        if since:
            clause = "AND created_at > ?"
            params.append(since)
        row = self.db.fetch_one(
            f"SELECT COUNT(*) AS c FROM proactive_events WHERE status = ? AND event_type != ? {clause}",
            params,
        )
        return int(row["c"]) if row else 0
