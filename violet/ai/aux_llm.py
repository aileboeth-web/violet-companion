"""Small, budgeted LLM calls for background helpers (phase 5A).

Used by the optional LLM life-event proposer and the optional semantic
consistency check. Not used on the main chat path, so a slow or missing
model never blocks a reply:

* hard per-call deadline (the call runs in a worker; a hung call is abandoned),
* bounded retries (timeouts, connection errors and malformed JSON),
* per-purpose budgets checked before calling: requests per day / week
  (a retry inside one request is not a new request) and tokens per day
  (every attempt counts),
* every attempt is recorded in aux_llm_calls (no prompt text stored).

The OpenAI client comes from BaseAgent.client_factory when one is installed
(the offline tests install FakeLLM there), otherwise from the usual
LLM_API_URL / LLM_API_KEY settings with SDK retries disabled.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ai.memory_model import parse_time

logger = logging.getLogger("violet.aux_llm")

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="violet-aux-llm")
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


class AuxLLMError(RuntimeError):
    """The helper could not produce a usable answer (caller falls back)."""


class BudgetExceeded(AuxLLMError):
    pass


@dataclass
class AuxLLMConfig:
    purpose: str
    model: str
    timeout_seconds: float = 20.0
    max_output_tokens: int = 300
    temperature: float = 0.3
    max_attempts: int = 2
    max_calls_per_day: int = 10
    max_tokens_per_day: int = 20000
    max_calls_per_week: int | None = None


def parse_json_object(text: str | None) -> dict[str, Any]:
    if not text:
        raise ValueError("empty output")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if not match:
            raise ValueError("no JSON object in output")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("output is not a JSON object")
    return value


class AuxLLM:
    def __init__(self, db, config: AuxLLMConfig):
        self.db = db
        self.config = config

    # -- budget ------------------------------------------------------------------
    def usage(self) -> dict[str, int]:
        now = parse_time(self.db.now())
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.db.fetch_all("SELECT created_at, total_tokens, attempt FROM aux_llm_calls WHERE purpose = ?",
                                 (self.config.purpose,))
        today = [r for r in rows if parse_time(r["created_at"]) >= day_start]
        week = [r for r in rows if parse_time(r["created_at"]) >= now - timedelta(days=7)]
        return {"calls_today": len([r for r in today if r["attempt"] == 1]),
                "tokens_today": sum(int(r["total_tokens"] or 0) for r in today),
                "calls_week": len([r for r in week if r["attempt"] == 1])}

    def budget_left(self, new_request: bool = True) -> bool:
        u = self.usage()
        c = self.config
        if u["tokens_today"] >= c.max_tokens_per_day:
            return False
        if not new_request:
            return True
        if u["calls_today"] >= c.max_calls_per_day:
            return False
        return c.max_calls_per_week is None or u["calls_week"] < c.max_calls_per_week

    def available(self) -> bool:
        return bool(self.config.model) and self.budget_left()

    # -- calls ---------------------------------------------------------------------
    def _client(self):
        from ai.agents.base_agent import BaseAgent
        from ai.chat import LLMConfig
        cfg = LLMConfig()
        cfg.reply_model = self.config.model
        if BaseAgent.client_factory is not None:
            return BaseAgent.client_factory(cfg)
        from openai import OpenAI, Timeout
        return OpenAI(api_key=cfg.api_key, base_url=cfg.api_url,
                      timeout=Timeout(self.config.timeout_seconds, connect=min(10.0, self.config.timeout_seconds)),
                      max_retries=0)

    def _record(self, status: str, latency_ms: int, usage: Any = None, error: str | None = None,
                attempt: int = 1) -> None:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0) or prompt + completion
        if status != "ok" and not total:
            total = 0
        self.db.execute(
            "INSERT INTO aux_llm_calls (purpose, model, status, prompt_tokens, completion_tokens, total_tokens, "
            "attempt, latency_ms, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.config.purpose, self.config.model, status, prompt, completion, total, attempt, latency_ms,
             (error or "")[:300] or None, self.db.now()))

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """Return a parsed JSON object, or raise AuxLLMError / BudgetExceeded."""
        if not self.config.model:
            raise AuxLLMError("no model configured")
        last_error = "no attempt"
        for attempt in range(1, max(1, self.config.max_attempts) + 1):
            if not self.budget_left(new_request=attempt == 1):
                raise BudgetExceeded(f"{self.config.purpose}: daily/weekly budget used up")
            started = time.monotonic()
            from ai.model_params import apply as apply_model_params
            params = apply_model_params({"model": self.config.model, "messages": messages,
                                         "temperature": self.config.temperature,
                                         "max_tokens": self.config.max_output_tokens})
            future = _EXECUTOR.submit(lambda: self._client().chat.completions.create(**params))
            try:
                response = future.result(timeout=self.config.timeout_seconds)
            except FutureTimeout:
                future.cancel()
                last_error = f"timeout after {self.config.timeout_seconds}s"
                self._record("timeout", int((time.monotonic() - started) * 1000), error=last_error, attempt=attempt)
                continue
            except Exception as exc:          # connection errors, 5xx, bad request ...
                last_error = f"{exc.__class__.__name__}: {exc}"
                self._record("error", int((time.monotonic() - started) * 1000), error=last_error, attempt=attempt)
                continue
            latency = int((time.monotonic() - started) * 1000)
            try:
                content = response.choices[0].message.content
                value = parse_json_object(content)
            except Exception as exc:
                last_error = f"invalid output: {exc}"
                self._record("invalid", latency, getattr(response, "usage", None), last_error, attempt=attempt)
                continue
            self._record("ok", latency, getattr(response, "usage", None), attempt=attempt)
            return value
        logger.warning("[aux-llm:%s] giving up: %s", self.config.purpose, last_error)
        raise AuxLLMError(last_error)
