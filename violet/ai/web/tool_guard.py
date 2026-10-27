"""Per-turn limits on web tool use inside one agent run (phase 5B).

The model decides whether to search; code decides what it may do:
  * at most VIOLET_WEB_MAX_CALLS_PER_TURN web tool calls per run;
  * fetch_url only for a URL the USER wrote in this turn or one returned by
    web_search in this run -- a page cannot make Violet open (or send data to)
    an address of its own choosing, and the model cannot build a URL that
    carries data out;
  * once web content has entered the run, no other tool may be called in that
    run (no escalation from web text into phone / ordering / other tools).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ai.settings import env_int
from ai.web.mcp_server import SERVER_NAME

URL_RE = re.compile(r"https?://[^\s\"'<>，。）)\]]+", re.I)
WEB_SEARCH = f"{SERVER_NAME}__web_search"
FETCH_URL = f"{SERVER_NAME}__fetch_url"
WEB_TOOLS = {WEB_SEARCH, FETCH_URL}


def _norm(url: str) -> str:
    return url.strip().rstrip("/.,;").lower()


def _json_payload(result_text: str) -> dict[str, Any] | None:
    start, end = result_text.find("{"), result_text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(result_text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class WebToolGuard:
    def __init__(self, user_message: str):
        self.allowed_urls = {_norm(u) for u in URL_RE.findall(user_message or "")}
        self.max_calls = env_int("VIOLET_WEB_MAX_CALLS_PER_TURN", 3)
        self.calls = 0
        self.tainted = False
        self.finding_ids: list[int] = []

    @staticmethod
    def _deny(code: str, message: str) -> str:
        return json.dumps({"ok": False, "error_code": code, "message": message}, ensure_ascii=False)

    def check(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        if tool_name not in WEB_TOOLS:
            if self.tainted:
                return self._deny("DENIED_AFTER_WEB", "本轮已经读取了外部网页内容，不再调用其他工具")
            return None
        if self.calls >= self.max_calls:
            return self._deny("WEB_TURN_LIMIT", "本轮联网次数已达上限")
        if tool_name == FETCH_URL and _norm(str(arguments.get("url") or "")) not in self.allowed_urls:
            return self._deny("URL_NOT_PROVIDED", "只能读取用户给出的链接或本轮搜索结果里的链接")
        self.calls += 1
        return None

    def observe(self, tool_name: str, result_text: str) -> None:
        if tool_name not in WEB_TOOLS:
            return
        self.tainted = True
        payload = _json_payload(result_text) or {}
        if tool_name == WEB_SEARCH:
            for item in payload.get("results") or []:
                if isinstance(item, dict) and item.get("url"):
                    self.allowed_urls.add(_norm(str(item["url"])))
        for item in (payload.get("results") or []) + [payload]:
            if isinstance(item, dict) and isinstance(item.get("finding_id"), int):
                self.finding_ids.append(item["finding_id"])
