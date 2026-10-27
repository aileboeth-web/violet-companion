"""Opt-in local specialized evidence, observed at tool/search/Chat boundaries.

No model client, no production logging, and no reconstruction from assertions.
"""
from __future__ import annotations

import contextvars
import inspect
import json
import re
from contextlib import contextmanager
from unittest.mock import patch


class MemoryQueryAudit:
    def __init__(self, recorder):
        self.recorder = recorder
        self.db = None
        self.target = None
        self.queries = []
        self.chat_requests = []
        self._active = contextvars.ContextVar("specialized_memory_query", default=None)

    def safe(self, value):
        if isinstance(value, str):
            return self.recorder.scrub(value)
        if isinstance(value, dict):
            return {self.safe(k): self.safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.safe(v) for v in value]
        return value

    def location(self, agent):
        from tools.harness import _RUN
        run = _RUN.get() or {}
        return {"agent": agent, "round": self.recorder.round,
                "stage": getattr(self.recorder, "active_stage", None), "run_id": run.get("run_id")}

    @staticmethod
    def events(rows):
        return [{"id": r.get("id"), "progress": r.get("progress"), "status": r.get("status"),
                 **({"retrieval": r["retrieval"]} if r.get("retrieval") is not None else {})}
                for r in rows if isinstance(r, dict)]

    @contextmanager
    def capture(self, agent, arguments):
        from tools.harness import _LAST_CALL
        call = _LAST_CALL.get()
        entry = {**self.location(agent), "call_no": call.call_no if call else None,
                 "parameters": self.safe(arguments), "searches": [], "status": "失败"}
        self.queries.append(entry)
        original = self.db.search_event_memories
        signature = inspect.signature(original)

        def observed(*args, **kwargs):
            if self._active.get() is not entry:
                return original(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            search = {"effective_parameters": self.safe(dict(bound.arguments)),
                      "normalized_query": self.safe(bound.arguments.get("query")),
                      "storage": "event_memories", "status": "失败"}
            entry["searches"].append(search)
            rows = original(*args, **kwargs)
            search.update(status="通过", returned_count=len(rows), returned_events=self.safe(self.events(rows)))
            return rows

        token = self._active.set(entry)
        try:
            # query_memory handlers are synchronous. ContextVar prevents unrelated
            # background searches from being attributed to this tool invocation.
            with patch.object(self.db, "search_event_memories", observed):
                yield entry
        finally:
            self._active.reset(token)

    def complete(self, entry, result):
        try:
            payload = json.loads(result)
        except (ValueError, TypeError):
            entry["error_code"] = "non_json_tool_result"
            return
        if isinstance(payload, dict) and isinstance(payload.get("memories"), list):
            rows = payload["memories"]
            entry.update(status="通过", returned_count=len(rows), returned_events=self.safe(self.events(rows)))
        else:
            entry["error_code"] = self.safe(payload.get("error_code")) if isinstance(payload, dict) else "invalid_tool_result"

    def observe_chat(self, agent, kwargs):
        if agent != "ChatAgent":
            return
        lines = [line for m in kwargs.get("messages", []) if m.get("role") == "system"
                 for line in str(m.get("content", "")).splitlines() if re.match(r"^- id=\d+; \[", line)]
        target = self.target
        present = bool(target and any(line.startswith(f"- id={target['id']}; ") and target["content"] in line for line in lines))
        self.chat_requests.append({**self.location(agent), "observed_at": "ChatAgent._create_completion.messages",
            "event_lines": self.safe(lines), "target_event_id": target["id"] if target else None,
            "target_event_in_context": present})
