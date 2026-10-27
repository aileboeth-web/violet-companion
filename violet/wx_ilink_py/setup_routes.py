"""Control Center: first-run setup (phase 10.3).

  GET  /api/control/setup               the checklist (what is configured, what is missing, where to fix it)
  POST /api/control/setup/test-model    one short real request with the settings Violet uses now {model?}
  POST /api/control/setup/models        the model names the API offers (GET /models of the configured service)
  GET  /api/control/diagnostics         phase 10.6: findings by area (channels, model, MCP, tools, network, core)
  POST /api/control/diagnostics/fix     {action, target}: the safe one-click fixes the findings offer

Settings themselves are saved through PUT /api/control/settings (validated, recorded, reversible); this module only
reads and checks.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, current_app, jsonify, request


def register_setup_routes(app: Flask, all_agents=None, runtime=None) -> None:
    def ok(data: Any = None, status: int = 200) -> Response:
        return jsonify({"ok": True, "data": data}), status

    def bad(message: str, status: int = 400) -> Response:
        return jsonify({"ok": False, "error": message}), status

    def chat_agent():
        return next((a for a in all_agents or [] if getattr(a, "mcp_agent_name", "") == "chat"), None)

    def data_dir():
        store = current_app.extensions.get("violet_admin_store")
        return store.dir.parent if store is not None else None

    @app.get("/api/control/setup")
    def control_setup():
        from ai.setup_status import checklist
        return ok(checklist(admin_store=current_app.extensions.get("violet_admin_store"), agents=all_agents,
                            runtime=runtime, data_dir=data_dir()))

    @app.post("/api/control/setup/test-model")
    def control_setup_test_model():
        agent = chat_agent()
        if agent is None:
            return bad("Chat agent 没有运行", 503)
        model = (request.get_json(silent=True) or {}).get("model")
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 120):
            return bad("模型名不对")
        from ai.setup_status import test_model
        return ok(test_model(agent, data_dir=data_dir(), model=model))

    # -------------------------------------------------------------- phase 10.6: diagnosis
    @app.get("/api/control/diagnostics")
    def control_diagnostics():
        from ai.diagnostics import report
        return ok(report(runtime=runtime, agents=all_agents, deep=request.args.get("deep") == "1"))

    @app.post("/api/control/diagnostics/fix")
    def control_diagnostics_fix():
        """Only safe, bounded actions: restart one channel, re-check one MCP server, apply the permission template the
        diagnosis found for a server (only the tools it lists), re-test the model, check the outside network. Nothing
        here opens permissions or changes credentials."""
        body = request.get_json(silent=True) or {}
        action, target = str(body.get("action") or ""), str(body.get("target") or "")
        if action == "network_check":
            from ai.diagnostics import run_network_check
            return ok(run_network_check(runtime))
        if action == "retest_model":
            agent = chat_agent()
            if agent is None:
                return bad("Chat agent 没有运行", 503)
            from ai.setup_status import test_model
            return ok(test_model(agent, data_dir=data_dir()))
        if action == "restart_channel":
            manager = getattr(runtime, "channels", None)
            entry = manager.entry(target) if manager is not None else None
            if entry is None:
                return bad("没有这个渠道", 404)
            if not entry.get("enabled", True):
                return bad("这个渠道已经关闭：要用的话先在“渠道”页开启")
            manager.apply(target)
            return ok(manager.describe_one(target))
        if action == "check_mcp":
            from ai.mcp_admin import all_server_names
            if target not in all_server_names():
                return bad("没有这个 MCP 服务器", 404)
            from ai.tool_manager import describe, refresh
            return ok(next((s for s in describe(refresh(target)) if s["name"] == target), None))
        if action == "apply_profile":
            from ai.chat import get_mcp_servers
            from ai.mcp_admin import profile_suggestion
            from ai.tool_manager import describe, refresh, tool_store
            server = next((s for s in get_mcp_servers() if isinstance(s, dict) and s.get("name") == target), None)
            if server is None:
                return bad("没有这个 MCP 服务器", 404)
            current = next((s for s in describe() if s["name"] == target), {})
            suggestion = profile_suggestion(server, target, [t["name"] for t in current.get("tools") or []])
            if not suggestion or suggestion.get("best") != body.get("profile"):
                return bad("这个修复已经不适用（服务器的工具或模板变了），请刷新诊断", 409)
            tool_store().set_tool_profile(target, suggestion["best"], "admin:diagnostics")
            for agent in all_agents or []:
                agent.reload()
            return ok(next((s for s in describe(refresh(target)) if s["name"] == target), None))
        return bad("不支持的修复")

    @app.post("/api/control/setup/models")
    def control_setup_models():
        agent = chat_agent()
        if agent is None:
            return bad("Chat agent 没有运行", 503)
        from ai.setup_status import list_models
        return ok(list_models(agent))
