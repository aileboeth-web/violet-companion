"""Optional LLM wording for life-event proposals (phase 5A).

Off by default (life_config.generation.mode = "template"). When enabled:

* The model only writes the *wording* of one small progress step on an
  existing, active thread. Which day, which thread, starting / finishing
  threads, plans and times stay in code (LifeEngine), so the model cannot
  invent a book, a trip, a person or a finished plan.
* It may answer {"propose": false}: no event that day is a normal outcome.
* Its text is checked here (length, on-thread, no completion, no time words,
  no drama, no external material, not repetitive) and then again by
  ConsistencyGuard.check_event before anything is stored. Rejections are
  flagged; nothing is written.
* Strict budget (AuxLLM): few calls per day/week, a token cap, a short
  deadline. Unavailable / over budget / failed -> template wording (or quiet,
  per config). Chat never depends on this.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ai.aux_llm import AuxLLM, AuxLLMConfig, AuxLLMError
from ai.consistency import COMPLETION_RE, DRAMATIC_RE
from ai.life_engine import TemplateProposer
from ai.memory_model import _bigrams, mentions_topic
from ai.settings import env_str

logger = logging.getLogger("violet.life")

TIME_WORDS_RE = re.compile(r"(昨天|今天|明天|前天|后天|上周|下周|刚才|刚刚|早上|晚上|凌晨|\d+点)")
EXTERNAL_RE = re.compile(r"(https?://|www\.|新闻|报道|据说|网上|热搜|微博|公众号)")
DEFAULT_LLM = {
    "max_calls_per_day": 1, "max_calls_per_week": 4, "max_tokens_per_day": 3000,
    "max_output_tokens": 120, "timeout_seconds": 20, "temperature": 0.7, "max_attempts": 2,
    "max_content_chars": 30, "similarity_threshold": 0.5, "avoid_recent": 20, "fallback": "template",
}


def generation_mode(config: dict[str, Any]) -> str:
    return (env_str("VIOLET_LIFE_GENERATION_MODE", "") or (config.get("generation") or {}).get("mode") or "template").lower()


def similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


class LLMProposer(TemplateProposer):
    source = "life_engine:template"

    def __init__(self, config: dict[str, Any], db, core):
        super().__init__(config)
        self.db = db
        self.core = core
        llm = {**DEFAULT_LLM, **((config.get("generation") or {}).get("llm") or {})}
        self.settings = llm
        self.llm = AuxLLM(db, AuxLLMConfig(
            purpose="life_proposal", model=env_str("VIOLET_LIFE_LLM_MODEL", ""),
            timeout_seconds=float(llm["timeout_seconds"]), max_output_tokens=int(llm["max_output_tokens"]),
            temperature=float(llm["temperature"]), max_attempts=int(llm["max_attempts"]),
            max_calls_per_day=int(llm["max_calls_per_day"]), max_tokens_per_day=int(llm["max_tokens_per_day"]),
            max_calls_per_week=int(llm["max_calls_per_week"])))
        self.last_detail: dict[str, Any] | None = None

    # -- helpers -------------------------------------------------------------------
    def _flag(self, check: str, detail: str) -> None:
        from ai.life_store import LifeStore
        LifeStore(self.db).flag("llm_life_proposal", None, check, detail)

    def _recent_contents(self) -> list[str]:
        rows = self.db.fetch_all("SELECT content FROM life_events ORDER BY id DESC LIMIT ?",
                                 (int(self.settings["avoid_recent"]),))
        return [r["content"] for r in rows]

    def _fallback(self, thread, rng, exclude, reason: str):
        self.source, self.last_detail = "life_engine:template", {"llm_fallback": reason}
        if self.settings.get("fallback") == "quiet":
            return None
        return super().progress(thread, rng, exclude)

    def _messages(self, thread: dict[str, Any], recent: list[str]) -> list[dict[str, str]]:
        d = self.core.data
        rules = (
            "你为虚拟人物 Violet 的生活记录提出【一条】候选的小进展，系统会再校验，不一定采用。\n"
            "只输出 JSON：{\"propose\": true, \"content\": \"...\"} 或 {\"propose\": false}。\n"
            f"content 要求：不超过 {self.settings['max_content_chars']} 个字；不写主语“我”；不写时间；"
            "只写这条生活线上一个平淡、具体的小进展；不能说读完/听完/完成；"
            "不出现任何人物（家人、朋友、恋人、同事、同学）；不出现地点变化、旅行、事故、疾病、新闻或网络内容；"
            "不要和 avoid 里的说法重复或相似；如果想不到合适的，就 {\"propose\": false}。"
        )
        payload = {
            "violet": {"personality": d.get("personality", []), "living_constraints": d.get("living_constraints", [])},
            "thread": {"title": thread["title"], "item": thread.get("item")},
            "avoid": recent,
        }
        return [{"role": "system", "content": rules},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def validate(self, content: str, thread: dict[str, Any], recent: list[str]) -> str | None:
        """Return the name of the failed check, or None."""
        if not content or "\n" in content or len(content) > int(self.settings["max_content_chars"]) or len(content) < 4:
            return "llm_format"
        if not ((thread.get("item") and thread["item"] in content) or mentions_topic(content, thread["title"])):
            return "llm_off_thread"
        if COMPLETION_RE.search(content):
            return "llm_completion"
        if TIME_WORDS_RE.search(content):
            return "llm_time_words"
        if DRAMATIC_RE.search(content):
            return "too_dramatic"
        if EXTERNAL_RE.search(content):
            return "external_as_experience"
        threshold = float(self.settings["similarity_threshold"])
        if any(content == r or similarity(content, r) >= threshold for r in recent):
            return "llm_repetitive"
        return None

    # -- Proposer ----------------------------------------------------------------------
    def progress(self, thread, rng, exclude):
        self.source, self.last_detail = "life_engine:template", None
        if not self.llm.config.model:
            return self._fallback(thread, rng, exclude, "no_model")
        if not self.llm.budget_left():
            return self._fallback(thread, rng, exclude, "budget")
        recent = self._recent_contents()
        try:
            out = self.llm.complete_json(self._messages(thread, recent))
        except AuxLLMError as exc:
            return self._fallback(thread, rng, exclude, f"llm_error: {exc}"[:120])
        if out.get("propose") is not True:
            self.last_detail = {"llm": "declined"}
            return None                       # nothing worth recording today
        content = str(out.get("content") or "").strip().strip("。.")
        problem = self.validate(content, thread, recent + sorted(exclude))
        if problem:
            self._flag(problem, f"{thread['title']} | {content}")
            self.last_detail = {"llm": "rejected", "check": problem}
            return None
        self.source, self.last_detail = "life_engine:llm", {"origin": "llm", "model": self.llm.config.model}
        return content


def make_proposer(config: dict[str, Any], db, core):
    if generation_mode(config) == "llm":
        return LLMProposer(config, db, core)
    return TemplateProposer(config)
