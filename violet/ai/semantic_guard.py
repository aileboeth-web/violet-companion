"""Optional semantic review of uncertain consistency findings (phase 5A).

The rule checker (ai/consistency.py) stays the first and main line. Its
findings are of two kinds:

  certain    forbidden_backstory, identity_conflict, plan_described_as_done
             -> never sent to a model; handled by rules alone
  uncertain  unrecorded_experience, user_experience_as_own
             -> the rules saw a first-person experience they could not match to
                a recorded event; wording may simply differ ("读了沈从文那本"
                vs. the recorded "又往后读了几章《边城》")

Only uncertain findings may go to the semantic model, and only when
VIOLET_SEMANTIC_GUARD=on and a model is configured. The model returns a verdict
per line plus evidence event ids. It has no write access: its output is parsed
into verdicts and nothing else; "supported" counts only if every cited id is a
recorded, happened, virtual event that was shown to it. Timeouts, malformed
output, budget exhaustion or an "uncertain" verdict leave the finding
uncertain -- the caller then asks for a natural rephrase instead of deleting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ai.aux_llm import AuxLLM, AuxLLMConfig, AuxLLMError
from ai.memory_model import parse_time
from ai.settings import env_float, env_int, env_str

logger = logging.getLogger("violet.semantic_guard")

CERTAIN_CHECKS = {"forbidden_backstory", "identity_conflict", "plan_described_as_done", "external_as_experience",
                  "camera_claim"}
UNCERTAIN_CHECKS = {"unrecorded_experience", "user_experience_as_own"}
VERDICTS = {"supported", "not_a_claim", "unsupported", "user_experience", "uncertain"}


def semantic_mode() -> str:
    return env_str("VIOLET_SEMANTIC_GUARD", "off").lower()


def is_uncertain(violation) -> bool:
    return violation.check in UNCERTAIN_CHECKS and getattr(violation, "semantic", None) not in ("unsupported", "user_experience")


class SemanticGuard:
    def __init__(self, db):
        self.db = db
        self.llm = AuxLLM(db, AuxLLMConfig(
            purpose="semantic_guard", model=env_str("VIOLET_SEMANTIC_GUARD_MODEL", ""),
            timeout_seconds=env_float("VIOLET_SEMANTIC_GUARD_TIMEOUT_SECONDS", 8.0),
            max_output_tokens=env_int("VIOLET_SEMANTIC_GUARD_MAX_OUTPUT_TOKENS", 300), temperature=0.0,
            max_attempts=env_int("VIOLET_SEMANTIC_GUARD_MAX_ATTEMPTS", 2),
            max_calls_per_day=env_int("VIOLET_SEMANTIC_GUARD_MAX_CALLS_PER_DAY", 60),
            max_tokens_per_day=env_int("VIOLET_SEMANTIC_GUARD_MAX_TOKENS_PER_DAY", 60000)))

    def enabled(self) -> bool:
        return semantic_mode() == "on" and bool(self.llm.config.model)

    def _evidence(self, now: str) -> dict[str, Any]:
        events = [e for e in self.db.fetch_all(
            "SELECT id, occurred_at, content FROM life_events WHERE status = 'happened' AND is_virtual = 1 "
            "ORDER BY occurred_at DESC LIMIT 20") if parse_time(e["occurred_at"]) <= parse_time(now)]
        plans = self.db.fetch_all("SELECT content FROM life_plans WHERE status IN ('planned', 'active', 'paused') LIMIT 10")
        user = self.db.fetch_all("SELECT content FROM event_memories WHERE status != 'deleted' "
                                 "AND kind IN ('event', 'legacy') ORDER BY id DESC LIMIT 10")
        return {
            "now": now[:16],
            "violet_recorded_events": [{"id": e["id"], "date": e["occurred_at"][:10], "content": e["content"]} for e in events],
            "violet_plans_not_done": [p["content"] for p in plans],
            "user_experiences": [u["content"][:80] for u in user],
        }

    @staticmethod
    def _messages(lines: list[str], evidence: dict[str, Any]) -> list[dict[str, str]]:
        rules = (
            "你是一致性审核器，不写回复，也不能修改任何记录。判断虚拟人物 Violet 的每句话是否在讲她自己已经做过的具体事情，"
            "以及这件事是否有记录。只输出 JSON：{\"results\": [{\"line_index\": 0, \"verdict\": \"...\", "
            "\"evidence_event_ids\": [], \"reason\": \"简短\"}]}。verdict 取值：\n"
            "supported = 讲的是 violet_recorded_events 里的某件事（必须给出对应 id）；\n"
            "not_a_claim = 不是在讲自己做过的具体事情（感受、看法、打算、客套、在说对方）；\n"
            "unsupported = 讲自己做过某件具体的事，但记录里没有；\n"
            "user_experience = 把 user_experiences 里用户的经历说成了自己的；\n"
            "uncertain = 无法判断。\n只依据给出的资料，不要假设记录之外的事情发生过。"
        )
        payload = {"lines": [{"line_index": i, "text": t} for i, t in enumerate(lines)], **evidence}
        return [{"role": "system", "content": rules}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def review(self, violations: list, now: str | None = None) -> list:
        """Return the findings that still stand; uncertain ones carry .semantic = verdict or reason."""
        candidates = [v for v in violations if v.check in UNCERTAIN_CHECKS and v.line]
        if not candidates or not self.enabled():
            return violations
        now = now or self.db.now()
        lines = list(dict.fromkeys(v.line for v in candidates))
        evidence = self._evidence(now)
        valid_ids = {e["id"] for e in evidence["violet_recorded_events"]}
        try:
            out = self.llm.complete_json(self._messages(lines, evidence))
            results = out.get("results")
            if not isinstance(results, list):
                raise AuxLLMError("results missing")
        except AuxLLMError as exc:
            for v in candidates:
                v.semantic = f"unavailable: {exc}"[:80]
            return violations
        verdicts: dict[int, tuple[str, list[int]]] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("line_index"))
            except (TypeError, ValueError):
                continue
            verdict = str(item.get("verdict") or "").strip()
            ids = item.get("evidence_event_ids") or []
            ids = [int(i) for i in ids if isinstance(i, (int, str)) and str(i).isdigit()] if isinstance(ids, list) else []
            if 0 <= index < len(lines) and verdict in VERDICTS:
                verdicts[index] = (verdict, ids)
        remaining = []
        for v in violations:
            if v not in candidates:
                remaining.append(v)
                continue
            verdict, ids = verdicts.get(lines.index(v.line), ("uncertain", []))
            if verdict == "supported" and (not ids or not set(ids) <= valid_ids):
                verdict = "uncertain"            # evidence that does not exist is no evidence
            if verdict in ("supported", "not_a_claim"):
                continue                         # cleared
            v.semantic = verdict
            if verdict == "user_experience":
                v.check = "user_experience_as_own"
            remaining.append(v)
        return remaining
