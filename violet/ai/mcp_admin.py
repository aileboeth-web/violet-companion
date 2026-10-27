"""MCP administration for the Control Center (phase 10.5): what a normal user needs to connect an MCP server and
make its tools usable -- without editing .env or mcp_tool_policy.json, and without weakening the permission model.

  * servers added in the Control Center (data/control/tools.json "custom_servers"; header credentials write-only in
    mcp_secrets.json). Only network transports (streamable HTTP / SSE): a command line (stdio) runs a program on the
    server and stays in .env. Servers from .env / built-in ones are listed too; they can be switched off and their
    tools classified here, but not deleted or re-pointed.
  * per-tool decisions by the admin: 只读 (read) / 写入 (write) / 高风险 (high_risk) / 拒绝 (deny). A tool nobody
    classified stays HIDDEN AND REFUSED, as before -- there is no "allow everything". Write / high-risk tools need a
    second, separate confirmation (allow_tools) before any agent can call them; a tool that declares itself
    destructive is still escalated to high_risk whatever it was classified as (ai.mcp.classify_tool_permission).
  * diagnosis of a server in plain words (server_diagnosis): not connected and why (address, credential, timeout,
    https required), connected but tools unclassified -- including THE common case: the server's name matches no
    entry of mcp_tool_policy.json and it has no tool_profile, so all of its tools are refused. When the discovered
    tool names fit a template of the policy file, the fix (use that template) is offered as one click; it classifies
    only the tools that template lists.
  * a tool test: one call of a READ tool that is available, with arguments checked against its schema; returns the
    (truncated, untrusted-wrapped) result or the error category. Write / high-risk tools are never test-called.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from ai.tool_manager import AGENTS, tool_store

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")
LEVELS = ("read", "write", "high_risk", "deny")
TRANSPORTS = ("streamable_http", "sse")
ERROR_WORDS = {
    "MCP_CONNECT_FAILED": "连不上这个地址：检查地址是否正确、服务是否在运行、服务器能否访问这个网络",
    "MCP_AUTH_FAILED": "服务器拒绝了凭据（401 / 403）：检查访问令牌 / 请求头",
    "MCP_TIMEOUT": "服务器没有在时限内回应：可能太慢或网络不通，可以调大连接超时",
    "MCP_PROTOCOL_ERROR": "连上了，但对方说的不是 MCP（地址路径不对？常见是少了 /mcp 或用错了 SSE / HTTP）",
    "MCP_UNAVAILABLE": "服务器在，但它后面的设备 / 服务不在线",
    "MCP_CIRCUIT_OPEN": "连续失败，暂停尝试一会儿（点“检查连接”会立即重试）",
    "MCP_CONFIG_ERROR": "配置不完整或地址不被允许（不在本机的地址必须用 https）",
    "MCP_SDK_MISSING": "缺少 MCP 组件（重新运行安装程序修复）",
}


class MCPAdminError(ValueError):
    def __init__(self, problems: dict[str, str] | str):
        self.problems = problems if isinstance(problems, dict) else {"_": problems}
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.problems.items()))


# ------------------------------------------------------------------------------------------------ servers

def custom_servers() -> list[dict[str, Any]]:
    return [dict(s) for s in tool_store().custom_servers() if isinstance(s, dict)]


def _url_problem(url: Any) -> str | None:
    if not isinstance(url, str) or len(url) > 500 or not re.match(r"^https?://[^\s@]+$", url.strip()):
        return "地址要以 https:// 开头（本机的服务可以用 http://127.0.0.1），不能带用户名密码"
    from ai.mcp import remote_problem
    if remote_problem({"transport": "streamable_http", "url": url.strip()}):
        return "不在本机的 MCP 服务器必须用 https（访问令牌会随每个请求发送）"
    if urlsplit(url.strip()).query:
        return "地址里不要放查询参数（令牌请填在“请求头”里，只会加密保存）"
    return None


def validate_server(body: dict[str, Any], existing_names: set[str], creating: bool) -> dict[str, Any]:
    problems: dict[str, str] = {}
    out: dict[str, Any] = {}
    name = str(body.get("name") or "").strip()
    if creating:
        if not NAME_RE.match(name):
            problems["name"] = "名字只能用小写字母、数字、- 和 _（2–31 个字符），会出现在工具名里"
        elif name in existing_names:
            problems["name"] = "已经有同名的服务器（包括 .env 和内置的）"
        out["name"] = name
    if creating or "url" in body:
        problem = _url_problem(body.get("url"))
        if problem:
            problems["url"] = problem
        else:
            out["url"] = str(body["url"]).strip()
    if creating or "transport" in body:
        transport = str(body.get("transport") or "streamable_http")
        if transport not in TRANSPORTS:
            problems["transport"] = "只能是 streamable_http 或 sse（运行本地命令的 stdio 服务器只能在 .env 里配置）"
        out["transport"] = transport
    if "agents" in body:
        agents = body.get("agents")
        if not isinstance(agents, list) or any(a not in AGENTS for a in agents):
            problems["agents"] = "只能是 chat / performer / proactive 中的几个"
        else:
            out["agents"] = list(dict.fromkeys(agents))
    elif creating:
        out["agents"] = ["chat"]
    if body.get("tool_profile") not in (None, ""):
        from ai.mcp import load_tool_policy
        profiles = set((load_tool_policy().get("servers") or {}).keys())
        if body["tool_profile"] not in profiles:
            problems["tool_profile"] = "没有这个权限模板"
        else:
            out["tool_profile"] = body["tool_profile"]
    if problems:
        raise MCPAdminError(problems)
    return out


def all_server_names() -> set[str]:
    from ai.chat import get_mcp_servers
    return {str(s.get("name")) for s in get_mcp_servers() if isinstance(s, dict) and s.get("name")}


def add_server(body: dict[str, Any], actor: str) -> dict[str, Any]:
    entry = validate_server(body, all_server_names(), creating=True)
    entry.update(enabled=True, created_at=int(time.time()))
    header, value = str(body.get("header") or "").strip(), body.get("header_value")
    store = tool_store()
    if value:
        from ai.tool_manager import SECRET_HEADER_RE
        if not SECRET_HEADER_RE.match(header or "Authorization"):
            raise MCPAdminError({"header": "只能是 Authorization 或 X-… 开头的请求头"})
    store.save_custom_server(entry["name"], entry, actor)
    if value:
        store.replace_secret(entry["name"], header or "Authorization", str(value), actor)
    return entry


def update_server(name: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    current = next((s for s in custom_servers() if s.get("name") == name), None)
    if current is None:
        raise LookupError("只能修改在后台添加的服务器（.env 和内置的只能开关、分类工具）")
    changes = validate_server(body, set(), creating=False)
    return tool_store().save_custom_server(name, {**current, **changes}, actor)


def remove_server(name: str, actor: str) -> None:
    if not any(s.get("name") == name for s in custom_servers()):
        raise LookupError("只能删除在后台添加的服务器")
    tool_store().remove_custom_server(name, actor)


# ------------------------------------------------------------------------------------------------ tool decisions

def classify_tool(server: str, tool: str, level: str, actor: str) -> dict[str, Any]:
    if level not in LEVELS and level is not None:
        raise MCPAdminError({"level": "只能是 read / write / high_risk / deny"})
    return tool_store().set_tool_level(server, tool, level, actor)


def allow_risky(server: str, tool: str, allowed: bool, actor: str) -> dict[str, Any]:
    """The second key for a write / high-risk tool. Only for a tool already classified as such."""
    level = (tool_store().servers().get(server) or {}).get("tool_levels", {}).get(tool)
    if allowed and level not in ("write", "high_risk"):
        from ai.chat import get_mcp_servers
        from ai.mcp import classify_tool_permission
        server_cfg = next((s for s in get_mcp_servers() if s.get("name") == server), {})
        if classify_tool_permission(server_cfg, tool, None, server_name=server) not in ("write", "high_risk"):
            raise MCPAdminError({"tool": "只有被分类为“写入”或“高风险”的工具需要（也才能）单独授权"})
    return tool_store().set_allow_tool(server, tool, allowed, actor)


# ------------------------------------------------------------------------------------------------ diagnosis

def profile_suggestion(server: dict[str, Any], name: str, tool_names: list[str]) -> dict[str, Any] | None:
    """The template mismatch: no tool_profile, no entry of the policy file under this server's name, no own
    classifications -- every tool is refused. Suggest the template whose tools match the discovered ones best."""
    from ai.mcp import load_tool_policy
    profiles = load_tool_policy().get("servers") or {}
    if server.get("tool_profile") or name in profiles or server.get("tools") or server.get("tool_permissions"):
        return None
    if not tool_names:
        return {"kind": "no_template", "best": None, "candidates": sorted(profiles)}
    best, best_share = None, 0.0
    for profile, entry in profiles.items():
        listed = set((entry or {}).get("tools") or {})
        if not listed:
            continue
        share = len(listed & set(tool_names)) / len(set(tool_names))
        if share > best_share:
            best, best_share = profile, share
    if best and best_share >= 0.5:
        return {"kind": "template_match", "best": best, "share": round(best_share, 2),
                "covers": sorted(set(tool_names) & set((profiles[best] or {}).get("tools") or {}))}
    return {"kind": "no_template", "best": None, "candidates": sorted(profiles)}


def server_diagnosis(described: dict[str, Any], server: dict[str, Any]) -> list[dict[str, Any]]:
    """Plain-words findings for one server (the Control Center shows them; the diagnosis page collects them)."""
    findings: list[dict[str, Any]] = []
    name = described["name"]
    status = described["status"]
    if status == "disabled":
        return [{"level": "info", "code": "server_disabled", "message": "已停用：Violet 不会使用它的任何工具",
                 "action": "enable_server"}]
    if status == "unavailable":
        code = described.get("last_error_code") or "MCP_CONNECT_FAILED"
        findings.append({"level": "error", "code": "mcp_not_connected",
                         "message": "MCP 未连接：" + ERROR_WORDS.get(code, f"连接失败（{code}）"), "action": "check"})
        return findings
    if status == "unknown":
        return [{"level": "info", "code": "not_checked", "message": "还没有检查过连接（点“检查连接”）", "action": "check"}]
    tools = described.get("tools") or []
    names = [t["name"] for t in tools]
    unclassified = [t["name"] for t in tools if t["permission"] == "unknown" and t["enabled"]]
    suggestion = profile_suggestion(server, name, names)
    if suggestion and suggestion["kind"] == "template_match":
        findings.append({"level": "error", "code": "profile_mismatch",
                         "message": f"服务器名“{name}”没有对应的权限模板，所以它的 {len(unclassified)} 个工具全部被拒绝。"
                                    f"它的工具和模板“{suggestion['best']}”一致（{len(suggestion['covers'])} 个），"
                                    "可以一键套用这个模板（只放行模板里列出的工具，其余仍然拒绝）",
                         "action": "apply_profile", "profile": suggestion["best"]})
    elif unclassified:
        findings.append({"level": "warn", "code": "tools_unclassified",
                         "message": f"MCP 已连接，但有 {len(unclassified)} 个工具还没有分类，Violet 看不到、也不能调用它们："
                                    + "、".join(unclassified[:8]) + ("…" if len(unclassified) > 8 else "")
                                    + "。请逐个选择“只读 / 写入 / 高风险 / 拒绝”",
                         "action": "classify", "tools": unclassified})
    waiting = [t["name"] for t in tools if t["permission"] in ("write", "high_risk") and t["enabled"]
               and not t.get("allowed_risky")]
    if waiting:
        findings.append({"level": "info", "code": "tools_need_second_key",
                         "message": "这些写入 / 高风险工具还需要单独授权才能使用：" + "、".join(waiting[:8]),
                         "action": "allow_risky", "tools": waiting})
    agents = described.get("agents") or []
    if tools and not agents:
        findings.append({"level": "warn", "code": "no_agent", "message": "没有授权给任何 Agent：工具不会被使用",
                         "action": "agents"})
    if not findings:
        usable = [t["name"] for t in tools if t.get("available")]
        findings.append({"level": "ok", "code": "ok", "message": f"正常：{len(usable)} 个工具可用（授权给 "
                                                                f"{'、'.join(agents) or '—'}）"})
    return findings


# ------------------------------------------------------------------------------------------------ tool test

def test_tool(server_name: str, tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """One call of an available READ tool. Never a write / high-risk / unclassified tool."""
    from ai.chat import get_mcp_servers
    from ai.mcp import MCPToolBridge, STATUS_OF
    from ai.tool_manager import _run_sync
    if arguments is not None and not isinstance(arguments, dict):
        raise MCPAdminError({"arguments": "参数要是一个 JSON 对象"})
    servers = [s for s in get_mcp_servers() if isinstance(s, dict) and s.get("name") == server_name]
    if not servers:
        raise LookupError("没有这个服务器")
    full = f"{server_name}__{tool}"
    started = time.monotonic()

    async def run():
        bridge = MCPToolBridge(servers, agent_name=None)
        try:
            await bridge.connect()
            binding = bridge.tool_to_session.get(full)
            if binding is None:
                reason = ("not_classified" if full in bridge.unclassified_tools else
                          "needs_second_key" if full in bridge.denied_tools else
                          "disabled" if full in bridge.disabled_tools else
                          "unavailable" if bridge.unavailable else "not_found")
                return {"ok": False, "status": reason}
            if binding.permission != "read":
                return {"ok": False, "status": "not_read_only"}
            text = await bridge.call_tool(full, arguments or {})      # validates the arguments against the schema
            return {"ok": True, "result": text}
        finally:
            await bridge.close()

    result = _run_sync(run)
    if result.get("ok"):
        text = str(result.get("result") or "")
        code = None
        try:
            data = json.loads(text)
            code = data.get("error_code") if isinstance(data, dict) else None
        except ValueError:
            pass
        if code:
            result = {"ok": False, "status": STATUS_OF.get(code, "tool_error"), "code": code}
        else:
            result["result"] = text[:3000]
    words = {"not_classified": "这个工具还没有分类（未分类的工具一律拒绝）", "needs_second_key": "写入 / 高风险工具需要单独授权",
             "disabled": "这个工具已停用", "unavailable": "MCP 服务器连不上", "not_found": "服务器上没有这个工具",
             "not_read_only": "只能测试只读工具（写入 / 高风险工具会真的执行操作）",
             "invalid_arguments": "参数不符合工具的要求", "tool_error": "工具运行了但返回错误", "timeout": "工具超时",
             "permission_denied": "没有权限"}
    if not result.get("ok"):
        result["message"] = words.get(result.get("status"), "调用失败")
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result
