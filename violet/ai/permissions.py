"""What Violet may do by herself (2026-10-30, rework step 4; user: "在可以控制的范围里面多给他权限").

Every ability with consequences has one of three levels, set on the settings page:

  self     she does it by herself
  ask      she asks the owner first: an approval waits (table approvals), the owner answers in the chat
           ("同意 3" / "不同意 3") or on the page; only an approval carries it out
  forbid   she does not do it

  qzone_post    posting on QQ 空间 by herself (the "发 QQ 动态" switch still has to be on)       default self
  risky_tools   MCP tools that change something (write / high risk), beyond the ones allowed one by one on the
                tools page (those always run). ask: they are offered too, every call waits for the owner. default forbid

An approval expires after 24 hours. Carrying it out is the ability's handler (registered here); the outcome goes
back to the owner through the runtime's system message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("violet.permissions")

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ability TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
"""

LEVELS = {"self": "自己做", "ask": "先问你", "forbid": "不允许"}
ABILITIES = {
    "qzone_post": ("自己发 QQ 动态", "她觉得想发时就发（还要打开“发 QQ 动态”）。先问你：她会私聊你要发的内容，你同意才发。", "self"),
    "risky_tools": ("会改动东西的工具", "手机上的操作这类“写入 / 高风险”的工具（工具页里单独放行的那些照常直接用）。"
                    "先问你：这些工具她都能用，但每次调用都要你同意；不允许：只有单独放行的能用。", "forbid"),
}
EXPIRE_HOURS = 24
COMMAND_RE = re.compile(r"^\s*(同意|批准|可以|好的?|不同意|拒绝|不要|不行|算了)\s*#?(\d{1,6})\s*$")
_HANDLERS: dict[str, Callable[[Any, dict[str, Any]], str]] = {}
HANDLER_MODULES = {"qzone_post": "ai.social.qzone"}
_RUNTIME: dict[str, Any] = {}
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "control" / "permissions.json"


def levels() -> dict[str, str]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {key: data.get(key) if data.get(key) in LEVELS else default for key, (_, _, default) in ABILITIES.items()}


def level(ability: str) -> str:
    return levels().get(ability, "forbid")


def set_level(ability: str, value: str) -> dict[str, str]:
    if ability not in ABILITIES or value not in LEVELS:
        raise ValueError("没有这项能力或这个级别")
    data = levels()
    data[ability] = value
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return levels()


def describe() -> list[dict[str, Any]]:
    current = levels()
    return [{"key": k, "label": label, "help": help_text, "level": current[k], "default": default}
            for k, (label, help_text, default) in ABILITIES.items()]


# ------------------------------------------------------------------------------------------------ approvals

def register(ability: str, handler: Callable[[Any, dict[str, Any]], str]) -> None:
    """handler(db, payload) -> what happened (told to the owner). Raises on failure."""
    _HANDLERS[ability] = handler


def attach_runtime(runtime) -> None:
    """The runtime that can tell the owner (send_system_text) -- set by VioletRuntime."""
    _RUNTIME["rt"] = runtime


def _tell_owner(text: str) -> None:
    rt = _RUNTIME.get("rt")
    if rt is None:
        return
    try:
        rt.send_system_text(text)
    except Exception:                                    # noqa: BLE001
        logger.exception("could not tell the owner about an approval")


def request(db, ability: str, summary: str, payload: dict[str, Any], tell: bool = True) -> int:
    """A pending approval; the owner is told how to answer. Returns its number."""
    cursor = db.execute("INSERT INTO approvals (ability, summary, payload, created_at) VALUES (?, ?, ?, ?)",
                        (ability, summary[:300], json.dumps(payload, ensure_ascii=False), db.now()))
    number = int(cursor.lastrowid)
    from ai import trace
    trace.step("请主人批准", 编号=number, 能力=ABILITIES.get(ability, (ability,))[0], 内容=summary[:120])
    if tell:
        _tell_owner(f"{summary}\n回复“同意 {number}”我就去做，回复“不同意 {number}”就算了（{EXPIRE_HOURS} 小时内有效）。")
    return number


def pending(db) -> list[dict[str, Any]]:
    expire(db)
    return db.fetch_all("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id DESC LIMIT 50")


def recent(db, limit: int = 20) -> list[dict[str, Any]]:
    expire(db)
    return db.fetch_all("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (int(limit),))


def expire(db) -> None:
    cutoff = (datetime.fromisoformat(db.now()[:19]) - timedelta(hours=EXPIRE_HOURS)).isoformat(timespec="seconds")
    db.execute("UPDATE approvals SET status = 'expired', decided_at = ? WHERE status = 'pending' AND created_at < ?",
               (db.now(), cutoff))


def decide(db, number: int, approve: bool, tell: bool = False) -> dict[str, Any]:
    """Approve (carried out now) or refuse. Raises ValueError for an unknown / already decided one."""
    with _LOCK:
        expire(db)
        row = db.fetch_one("SELECT * FROM approvals WHERE id = ?", (int(number),))
        if row is None:
            raise ValueError(f"没有编号 {number} 的请求")
        if row["status"] != "pending":
            done = {"approved": "同意过", "denied": "拒绝过", "expired": "过期", "failed": "执行过（失败）"}
            raise ValueError(f"编号 {number} 已经{done.get(row['status'], '处理过')}了")
        if not approve:
            db.execute("UPDATE approvals SET status = 'denied', decided_at = ? WHERE id = ?", (db.now(), row["id"]))
            result = {"id": row["id"], "status": "denied", "result": "好，那就不做了。"}
        else:
            if row["ability"] not in _HANDLERS and row["ability"] in HANDLER_MODULES:
                import importlib                         # its module registers the handler when imported
                importlib.import_module(HANDLER_MODULES[row["ability"]])
            handler = _HANDLERS.get(row["ability"])
            try:
                if handler is None:
                    raise RuntimeError("这项能力现在用不了")
                outcome = handler(db, json.loads(row["payload"]))
                status = "approved"
            except Exception as exc:                     # noqa: BLE001 -- told to the owner, kept in the table
                outcome, status = f"没做成：{str(exc)[:200]}", "failed"
            db.execute("UPDATE approvals SET status = ?, result = ?, decided_at = ? WHERE id = ?",
                       (status, str(outcome)[:500], db.now(), row["id"]))
            result = {"id": row["id"], "status": status, "result": str(outcome)}
    from ai import trace
    trace.step("主人批准", 编号=result["id"], 结果=result["status"], 说明=result["result"][:120])
    if tell:
        _tell_owner(result["result"])
    return result


def owner_command(text: str) -> tuple[bool, int] | None:
    """"同意 3" -> (True, 3); "不同意 3" -> (False, 3); anything else -> None."""
    match = COMMAND_RE.match(text or "")
    if not match:
        return None
    return match.group(1) in ("同意", "批准", "可以", "好", "好的"), int(match.group(2))


# ------------------------------------------------------------------------------------------------ risky tools

def tool_needs_approval(server: dict[str, Any], server_name: str, tool_name: str, permission: str) -> bool:
    """A write / high-risk tool that is not allowed one by one, while risky_tools is "ask" (then it is offered)."""
    from ai.mcp import is_tool_allowed
    return (permission in ("write", "high_risk") and not is_tool_allowed(server, server_name, tool_name, permission)
            and level("risky_tools") == "ask")


def ask_for_tool(db, full_name: str, arguments: dict[str, Any]) -> str:
    """The tool result the model gets instead of running the tool: it waits for the owner."""
    args = json.dumps(arguments, ensure_ascii=False)
    number = request(db, "risky_tools", f"我想用工具 {full_name}（参数 {args[:200]}）", {"tool": full_name, "arguments": arguments})
    return json.dumps({"ok": False, "error_code": "NEEDS_APPROVAL", "approval": number,
                       "message": f"这个操作要主人同意才会执行（已经请主人批准，编号 {number}）。现在还没有执行，告诉对方要等主人同意，"
                                  "不要说已经做了。"}, ensure_ascii=False)


def _run_tool(db, payload: dict[str, Any]) -> str:
    import asyncio
    from ai.chat import get_mcp_servers
    from ai.mcp import MCPToolBridge
    name = str(payload.get("tool") or "")
    server_name = name.split("__", 1)[0]
    servers = [s for s in get_mcp_servers() if s.get("name") == server_name]
    if not servers:
        raise RuntimeError(f"工具服务器 {server_name} 不在了")

    async def call():
        async with MCPToolBridge(servers) as bridge:
            if name not in bridge.tool_to_session:
                raise RuntimeError("这个工具现在用不了（连不上，或者权限改了）")
            return await bridge.call_tool(name, dict(payload.get("arguments") or {}))
    text = asyncio.run(call())
    from ai.trace import tool_outcome
    code, message = tool_outcome(text)
    if code != "ok":
        raise RuntimeError(message or code)
    return f"做好了（{name}）。结果：{text[:300]}"


register("risky_tools", _run_tool)
