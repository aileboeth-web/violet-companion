"""Control Center: MCP server and tool administration (phase 10.5). Behind the admin authentication.

  POST /api/control/tools/servers                               add a server {name, url, transport, agents, header,
                                                                header_value, tool_profile}
  POST /api/control/tools/servers/<name>/edit                   change url / transport / agents (servers added here)
  POST /api/control/tools/servers/<name>/delete                 {confirm: true} (servers added here)
  POST /api/control/tools/servers/<name>/profile                {profile: "<template>" | null}
  POST /api/control/tools/servers/<name>/tools/<tool>/level     {level: read | write | high_risk | deny | null}
  POST /api/control/tools/servers/<name>/tools/<tool>/allow     {allowed: bool, confirm: true}  (write / high-risk)
  POST /api/control/tools/servers/<name>/tools/<tool>/test      {arguments: {...}}  (read tools only)
  GET  /api/control/tools/profiles                              the permission templates of mcp_tool_policy.json

Settings that already existed (enable, agents, timeouts, offer, single tool switch, header credential) stay in
control_routes.py. Every change acts on the next agent run (settings are applied whenever the server list is read).
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, jsonify, request


def register_mcp_routes(app: Flask, reload_agents=None) -> None:
    def ok(data: Any = None, status: int = 200) -> Response:
        return jsonify({"ok": True, "data": data}), status

    def bad(message: str, status: int = 400, **extra) -> Response:
        return jsonify({"ok": False, "error": message, **extra}), status

    def actor() -> str:
        return "admin:" + str(request.environ.get("violet.auth") or "unknown")

    def applied() -> None:
        if reload_agents is not None:
            try:
                reload_agents()
            except Exception:
                pass

    def known(name: str) -> bool:
        from ai.mcp_admin import all_server_names
        return name in all_server_names()

    def guarded(fn):
        from ai.mcp_admin import MCPAdminError

        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except MCPAdminError as exc:
                return bad("保存失败，没有改动", problems=exc.problems)
            except LookupError as exc:
                return bad(str(exc) or "not found", 404)
            except ValueError as exc:
                return bad(str(exc))
        wrapper.__name__ = fn.__name__
        return wrapper

    @app.get("/api/control/tools/profiles")
    def mcp_profiles():
        from ai.mcp import load_tool_policy
        profiles = load_tool_policy().get("servers") or {}
        return ok([{"name": k, "tools": (v or {}).get("tools") or {}} for k, v in sorted(profiles.items())])

    @app.post("/api/control/tools/servers")
    @guarded
    def mcp_add_server():
        from ai.mcp_admin import add_server
        from ai.tool_manager import refresh
        entry = add_server(request.get_json(silent=True) or {}, actor())
        applied()
        catalog = refresh(entry["name"])            # connect now: the page shows the result of the first check
        from ai.tool_manager import describe
        return ok(next((s for s in describe(catalog) if s["name"] == entry["name"]), entry), 201)

    @app.post("/api/control/tools/servers/<name>/edit")
    @guarded
    def mcp_edit_server(name: str):
        from ai.mcp_admin import update_server
        result = update_server(name, request.get_json(silent=True) or {}, actor())
        applied()
        return ok(result)

    @app.post("/api/control/tools/servers/<name>/delete")
    @guarded
    def mcp_delete_server(name: str):
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            return bad("删除需要确认（confirm: true）")
        from ai.mcp_admin import remove_server
        remove_server(name, actor())
        applied()
        return ok({"removed": name})

    @app.post("/api/control/tools/servers/<name>/profile")
    @guarded
    def mcp_server_profile(name: str):
        if not known(name):
            return bad("not found", 404)
        profile = (request.get_json(silent=True) or {}).get("profile")
        if profile is not None:
            from ai.mcp import load_tool_policy
            if profile not in (load_tool_policy().get("servers") or {}):
                return bad("没有这个权限模板")
        from ai.tool_manager import tool_store
        saved = tool_store().set_tool_profile(name, profile, actor())
        applied()
        return ok({"server": name, "tool_profile": saved.get("tool_profile")})

    @app.post("/api/control/tools/servers/<name>/tools/<tool>/level")
    @guarded
    def mcp_tool_level(name: str, tool: str):
        if not known(name):
            return bad("not found", 404)
        from ai.mcp_admin import classify_tool
        saved = classify_tool(name, tool, (request.get_json(silent=True) or {}).get("level"), actor())
        applied()
        return ok({"server": name, "tool": tool, "level": (saved.get("tool_levels") or {}).get(tool)})

    @app.post("/api/control/tools/servers/<name>/tools/<tool>/allow")
    @guarded
    def mcp_tool_allow(name: str, tool: str):
        body = request.get_json(silent=True) or {}
        if not known(name):
            return bad("not found", 404)
        if not isinstance(body.get("allowed"), bool):
            return bad("allowed 要用 true / false")
        if body["allowed"] and body.get("confirm") is not True:
            return bad("允许写入 / 高风险工具需要确认（confirm: true）：Violet 将能用它真的执行操作")
        from ai.mcp_admin import allow_risky
        saved = allow_risky(name, tool, body["allowed"], actor())
        applied()
        return ok({"server": name, "tool": tool, "allowed": tool in (saved.get("allow_tools") or [])})

    @app.post("/api/control/tools/servers/<name>/tools/<tool>/test")
    @guarded
    def mcp_tool_test(name: str, tool: str):
        if not known(name):
            return bad("not found", 404)
        from ai.mcp_admin import test_tool
        return ok(test_tool(name, tool, (request.get_json(silent=True) or {}).get("arguments") or {}))
