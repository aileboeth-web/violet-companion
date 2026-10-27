"""Optional, skippable get-to-know-you (phase 3).

Violet may *offer* at most one light question per turn, and only when:
  * onboarding is enabled (VIOLET_ONBOARDING=1),
  * the user is not dealing with something important (distress, a long message,
    a question of their own, a task),
  * nothing was offered recently (hours AND user turns since the last offer),
  * the key is not known (active / candidate / disabled) and was never declined.
The answer is stored through the normal profile tools; a refusal ("不想说") in
the next turn marks the key as declined for good (resettable on the dashboard).
The user can introduce themselves at any time; nothing here blocks chatting.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from ai.memory_model import parse_time
from ai.profile_schema import ASK_ORDER
from ai.settings import env_bool, env_float, env_int

DECLINE_RE = re.compile(r"(不想说|不方便说|不太方便|保密|不告诉你|别问了|不想聊这个|不想回答|不说了|就不说了|跳过|不想透露|秘密)")
DISTRESS_RE = re.compile(r"(难过|伤心|烦死|焦虑|崩溃|哭|压力好大|压力大|好累|生病|发烧|疼|着急|急死|出事|吵架|分手|失眠|"
                         r"不开心|害怕|担心|委屈|绝望|心累|紧张|睡不着|难受)")
QUESTION_RE = re.compile(r"[?？]\s*$|(吗|呢|么|嘛|哪|哪儿|哪里|啥|什么|谁|几点|多少|怎么样|怎么办|为什么|为啥)[啊呀呢]?\s*[~～!！。.]*$")


def is_important_moment(text: str, route: str | None = None) -> bool:
    text = (text or "").strip()
    if route == "task":
        return True
    if len(text) >= env_int("VIOLET_ONBOARDING_MAX_MESSAGE_CHARS", 30):
        return True
    return bool(DISTRESS_RE.search(text) or QUESTION_RE.search(text))


class OnboardingPlanner:
    def __init__(self, db, profile_store):
        self.db = db
        self.profile = profile_store

    def _last_offer(self) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM profile_questions ORDER BY id DESC LIMIT 1")

    def note_user_turn(self, text: str) -> str | None:
        """Called for every user turn before the reply; returns a key the user just declined."""
        offer = self._last_offer()
        if not offer or offer["outcome"] != "offered":
            return None
        window = timedelta(minutes=env_float("VIOLET_ONBOARDING_DECLINE_WINDOW_MINUTES", 240))
        if parse_time(self.db.now()) - parse_time(offer["created_at"]) > window:
            return None
        if DECLINE_RE.search(text or ""):
            self.profile.mark_question(offer["fact_key"], "declined")
            return offer["fact_key"]
        return None

    def decline(self, key: str) -> None:
        """Explicit refusal reported by the memory agent."""
        if not self.db.fetch_one("SELECT id FROM profile_questions WHERE fact_key = ? AND outcome = 'declined'", (key,)):
            now = self.db.now()
            self.db.execute("INSERT INTO profile_questions (fact_key, outcome, created_at, updated_at) VALUES (?, 'declined', ?, ?)",
                            (key, now, now))
        self.profile.mark_question(key, "declined")

    def reset(self) -> int:
        return self.db.execute("DELETE FROM profile_questions").rowcount

    def _user_turns_since(self, since: str) -> int:
        row = self.db.fetch_one("SELECT COUNT(*) AS c FROM messages WHERE role = 'user' AND created_at > ?", (since,))
        return int(row["c"]) if row else 0

    # Phase 7 B.5: "上学还是已经工作" (identity.status) is already answered by a confirmed school year or a
    # confirmed job. Only that question is skipped; the fact itself is not created and nothing else changes.
    IMPLIED_ANSWERS = {"identity.status": ("study.grade", "work.role")}

    def _implied_known(self) -> set[str]:
        implied = set()
        for key, evidence_keys in self.IMPLIED_ANSWERS.items():
            for evidence in evidence_keys:
                rows = self.db.fetch_all(
                    "SELECT id FROM user_profile_facts WHERE fact_key = ? AND status = 'active' "
                    "AND origin IN ('explicit', 'dashboard')", (evidence,))
                if rows:
                    implied.add(key)
        return implied

    def next_key(self) -> Any | None:
        known = self.profile.known_keys() | self._implied_known()
        declined = self.profile.declined_keys()
        reask_after = timedelta(days=env_float("VIOLET_ONBOARDING_REASK_DAYS", 14))
        now = parse_time(self.db.now())
        for spec in ASK_ORDER:
            if spec.key in known or spec.key in declined:
                continue
            last = self.db.fetch_one("SELECT * FROM profile_questions WHERE fact_key = ? ORDER BY id DESC LIMIT 1", (spec.key,))
            if last and now - parse_time(last["created_at"]) < reask_after:
                continue
            return spec
        return None

    def hint(self, user_message: str, route: str | None = None, turn_id: str | None = None) -> dict[str, Any] | None:
        if not env_bool("VIOLET_ONBOARDING", True) or is_important_moment(user_message, route):
            return None
        last = self._last_offer()
        if last:
            if parse_time(self.db.now()) - parse_time(last["created_at"]) < timedelta(hours=env_float("VIOLET_ONBOARDING_MIN_HOURS", 12)):
                return None
            if self._user_turns_since(last["created_at"]) < env_int("VIOLET_ONBOARDING_MIN_TURNS", 6):
                return None
        spec = self.next_key()
        if spec is None:
            return None
        self.profile.mark_question(spec.key, "offered", turn_id)
        return {"key": spec.key, "hint": spec.ask_hint}
