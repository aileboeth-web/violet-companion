"""排查日志 (Violet 0.7.5; user: "做一个日志的功能放在设置里面，默认关闭，尽量写清楚一点，方便我们后续排查问题").

Off by default (setting VIOLET_TRACE). When on, every step of a conversation is one readable line in the run log,
logger "violet.trace", tagged with the turn's correlation id like every other line ("[turn t-...]") -- the Control
Center's "日志" page shows them with "只看排查日志" and "只看这一轮":

  收到消息   channel, sender, kind, the text
  意图识别   route, reply or not, the tool request Intent wrote (tool_query) or "none"
  可用工具   the tools discovered this turn, the servers that are down / switched off
  工具计划   mode (none / chat / performer / blocked), tools, status, reason
  模型调用   agent, model, tools offered and tool_choice, time, tokens, outcome / error kind
  工具调用   tool, arguments, outcome (ok / error code), result length and text
  联网搜索   provider, query, number of results / error
  回复内容   what Chat answered
  发送       channel, outbound id, sent / failed and why
  主动联系   decision of a proactive run

What text is written follows VIOLET_TRACE_CONTENT: short (default, the first 200 characters), full (up to 2000), none
(only the length). Keys and passwords: the log page removes every known secret when it shows or downloads the log;
nothing here writes request headers. Never raises.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ai.settings import env_bool, env_str

logger = logging.getLogger("violet.trace")
logger.setLevel(logging.INFO)          # written when switched on, even with 日志详细程度 = 只记问题
LIMITS = {"short": 200, "full": 2000}


def enabled() -> bool:
    return env_bool("VIOLET_TRACE", False)


def text(value: Any) -> str:
    """User / model / tool text as the setting allows, on one line."""
    if value is None:
        return "—"
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    mode = env_str("VIOLET_TRACE_CONTENT", "short").strip().lower()
    if mode == "none":
        return f"<{len(value)} 字，未记录内容>"
    limit = LIMITS.get(mode, LIMITS["short"])
    flat = value.replace("\r", "").replace("\n", "⏎")
    return flat if len(flat) <= limit else flat[:limit] + f"…（共 {len(value)} 字）"


def _value(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(str(x) for x in v) + "]" if v else "[]"
    if isinstance(v, dict):
        try:
            return json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(v)
    if v is None or v == "":
        return "—"
    return str(v).replace("\n", "⏎")


def step(name: str, **fields: Any) -> None:
    """One line: "工具调用 · tool=violet-web__web_search · 结果=ok · …". Fields with value None are left out.
    2026-10-30: the step is also handed to test mode's diagnosis (ai/test_mode.py), whether or not this log is on."""
    try:
        from ai import test_mode
        test_mode.record(name, {k: v for k, v in fields.items() if v is not None})
    except Exception:
        pass
    if not enabled():
        return
    try:
        parts = [f"{k}={_value(v)}" for k, v in fields.items() if v is not None]
        logger.info("%s · %s", name, " · ".join(parts) if parts else "—")
    except Exception:                                    # tracing must never break a conversation
        pass


def tool_outcome(result_text: str) -> tuple[str, str | None]:
    """("ok" | error code, message) of a tool result."""
    if isinstance(result_text, str) and result_text.lstrip().startswith("{"):
        try:
            data = json.loads(result_text)
        except ValueError:
            return "ok", None
        if isinstance(data, dict) and (data.get("ok") is False or data.get("error_code")):
            return str(data.get("error_code") or "error"), str(data.get("message") or "")[:200] or None
    return "ok", None
