"""Flask app factory — 所有 HTTP 路由集中在此文件。

除静态资源、/auth/login 和 /healthz 外，所有路由都需要管理员认证（见 auth.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import os

from flask import Flask, Response, jsonify, redirect, render_template, request

from ai.settings import env_bool
from .auth import install_auth, resolve_admin_token

if TYPE_CHECKING:
    from .client import ILinkClient
    from ai.proactive_scheduler import ProactiveScheduler
    from ai.agents.base_agent import BaseAgent
    from ai.debug_visualizer import reload_debug_mode as _RDM
    from ai.chat import VioletDatabase


def create_app(
    client: "ILinkClient",
    violetdb: "VioletDatabase | None" = None,
    proactive_scheduler: "ProactiveScheduler | None" = None,
    all_agents: "list[BaseAgent] | None" = None,
    reload_debug_mode: "Callable | None" = None,
    extra_reload_hooks: "list[Callable] | None" = None,
    runtime=None,
    admin_token: str | None = None,
) -> Flask:
    """创建并返回配置好的 Flask 应用。

    Args:
        client: ILinkClient 实例（必须）。
        violetdb: VioletDatabase 实例，用于主动调度接口。
        proactive_scheduler: ProactiveScheduler 实例。
        all_agents: 所有 Agent 实例列表，用于 /reload-env。
        reload_debug_mode: debug 模式重载函数。
        runtime: VioletRuntime 实例，用于 /api/health。
        admin_token: 管理令牌；为空时从 VIOLET_ADMIN_TOKEN 或 <data_dir>/admin_token 解析。
    """
    _here = os.path.dirname(__file__)
    app = Flask(
        "violet",
        template_folder=os.path.join(_here, "templates"),
        static_folder=os.path.join(_here, "static"),
        static_url_path="/static",
    )
    data_dir = getattr(client, "data_dir", "./data")
    if not admin_token:
        admin_token, _source = resolve_admin_token(data_dir)
    install_auth(app, admin_token, data_dir)

    # ------------------------------------------------------------------
    # ILink / 登录相关路由
    # ------------------------------------------------------------------

    def _status() -> dict:
        """The WeChat client's state; "queue_size" is the number of user turns waiting for the runtime's worker."""
        state = client.get_status()
        if runtime is not None:
            try:
                state["queue_size"] = int(runtime.turn_queue.unfinished_tasks)
            except Exception:
                pass
        return state

    @app.get("/")
    def index() -> str:
        autostart = request.args.get("autostart") == "1"
        if autostart and not client.get_status().get("logged_in"):
            client.start_login_async()
        return render_template("dashboard.html", state=_status())

    @app.post("/login")
    def trigger_login() -> Response:
        force_refresh = request.form.get("force_refresh") == "1"
        client.start_login_async(force_refresh=force_refresh)
        return redirect("/")

    @app.post("/logout")
    def trigger_logout() -> Response:
        client.clear_credentials()
        return redirect("/")

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(_status())

    @app.get("/memories")
    def memories_page() -> str:
        return render_template("memories.html")

    @app.get("/api/memories")
    def api_memories() -> Response:
        if not violetdb:
            return jsonify({"error": "violetdb not configured"}), 503
        import json as _json

        # --- messages ---
        recent_msgs = violetdb.get_recent_messages(limit=50)
        msg_stats = violetdb.get_message_stats()

        # --- task_sessions ---
        tasks = violetdb.list_task_sessions(limit=30)
        active_task = violetdb.get_active_task_session()

        # --- core_memories ---
        core_mems = violetdb.list_core_memories(limit=100)
        core_cats = list({m["category"] for m in core_mems})

        # --- event_memories ---
        event_mems = violetdb.list_event_memories(limit=100)
        event_status_counts: dict = {}
        for em in event_mems:
            s = em.get("status", "unknown")
            event_status_counts[s] = event_status_counts.get(s, 0) + 1

        # --- proactive_events ---
        proactive_evts = violetdb.fetch_all(
            "SELECT * FROM proactive_events ORDER BY created_at DESC LIMIT 30"
        )
        for pe in proactive_evts:
            try:
                pe["decision"] = _json.loads(pe["decision"]) if pe.get("decision") else None
            except Exception:
                pass
            try:
                pe["sent_messages"] = _json.loads(pe["sent_messages"]) if pe.get("sent_messages") else []
            except Exception:
                pe["sent_messages"] = []

        # --- proactive_settings ---
        ps_rows = violetdb.fetch_all("SELECT key, value, updated_at FROM proactive_settings")
        proactive_settings = {r["key"]: r for r in ps_rows}

        return jsonify({
            "msg_stats": msg_stats,
            "recent_messages": recent_msgs,
            "active_task": active_task,
            "tasks": tasks,
            "core_memories": core_mems,
            "core_categories": sorted(core_cats),
            "event_memories": event_mems,
            "event_status_counts": event_status_counts,
            "proactive_events": proactive_evts,
            "proactive_settings": proactive_settings,
        })

    # ------------------------------------------------------------------
    # 系统管理路由
    # ------------------------------------------------------------------

    def reload_all() -> None:
        """.env again, the Control Center's settings on top, then every agent / the scheduler / hooks reload."""
        from ai.control_config import reload_env
        reload_env()
        if all_agents:
            for agent in all_agents:
                agent.reload()
        if proactive_scheduler:
            proactive_scheduler.reload()
        if reload_debug_mode:
            reload_debug_mode()
        for hook in (extra_reload_hooks or []):
            hook()

    @app.post("/reload-env")
    def trigger_reload_env() -> Response:
        reload_all()
        reloaded = [a.__class__.__name__ for a in (all_agents or [])]
        return jsonify({"status": "ok", "reloaded_agents": reloaded})

    # ------------------------------------------------------------------
    # 主动调度路由
    # ------------------------------------------------------------------

    @app.get("/proactive/status")
    def proactive_status() -> Response:
        if not proactive_scheduler:
            return jsonify({"error": "proactive_scheduler not configured"}), 503
        return jsonify(proactive_scheduler.status_snapshot())

    @app.post("/proactive/test")
    def proactive_test() -> Response:
        if not proactive_scheduler:
            return jsonify({"error": "proactive_scheduler not configured"}), 503
        payload = request.get_json(silent=True) or {}
        send = bool(payload.get("send", False))
        if send and not env_bool("VIOLET_ALLOW_MANUAL_PROACTIVE_SEND", False):
            return jsonify({
                "error": "manual proactive sending is disabled; set VIOLET_ALLOW_MANUAL_PROACTIVE_SEND=1 to allow it",
            }), 403
        event_context_patch = payload.get("event_context") or {}
        force_task = payload.get("force_task")
        try:
            pipeline = proactive_scheduler.force_run(
                send=send,
                event_context_patch=event_context_patch,
                force_task=force_task,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({
            "ok": True,
            "send": send,
            "event_id": pipeline.get("event_id"),
            "decision": pipeline.get("proactive_decision"),
            "has_performer": bool(pipeline.get("performer")),
            "chat_content": (pipeline.get("chat") or {}).get("content"),
            "error": pipeline.get("error"),
        })

    @app.post("/proactive/schedule-now")
    def proactive_schedule_now() -> Response:
        if not violetdb:
            return jsonify({"error": "violetdb not configured"}), 503
        violetdb.set_proactive_setting("next_proactive_at", violetdb.now())
        return jsonify({"ok": True, "next_proactive_at": violetdb.get_proactive_setting("next_proactive_at")})

    @app.post("/shutdown")
    def trigger_shutdown() -> Response:
        import signal
        os.kill(os.getpid(), signal.SIGTERM)
        return jsonify({"status": "shutting_down"})

    if violetdb is not None:
        from .profile_routes import register_profile_routes
        register_profile_routes(app, violetdb)
        from .persona_routes import register_persona_routes
        register_persona_routes(app, violetdb)
        from .web_routes import register_web_routes
        register_web_routes(app, violetdb)
        from .image_routes import register_image_routes
        register_image_routes(app, violetdb)
        from .ops_routes import register_ops_routes
        register_ops_routes(app, violetdb)
        from .control_routes import register_control_routes          # phase 9.0: Violet Control Center
        register_control_routes(app, violetdb, client=client, proactive_scheduler=proactive_scheduler,
                                all_agents=all_agents, runtime=runtime, reload_all=reload_all)
        from .schedule_routes import register_schedule_routes      # phase 9.4.3: Control Center timetable page
        register_schedule_routes(app, violetdb)
        from .restart import register_restart_routes               # Violet stage 3: restart from the page
        register_restart_routes(app)
        from .ops_page_routes import register_ops_page_routes       # Violet stage 3: WeChat login, run log
        register_ops_page_routes(app, client)
    if runtime is not None:
        from .channel_routes import register_channel_routes        # phase 10: chat channels
        register_channel_routes(app, runtime)
    from .setup_routes import register_setup_routes                # phase 10.3: first-run setup
    register_setup_routes(app, all_agents=all_agents, runtime=runtime)

    def _reload_agents() -> None:
        for agent in all_agents or []:
            agent.reload()
    from .mcp_routes import register_mcp_routes                    # phase 10.5: MCP servers and tool permissions
    register_mcp_routes(app, reload_agents=_reload_agents)
    from .phone_mcp_routes import register_phone_mcp_routes        # Violet stage 3: phone relay pairing values
    register_phone_mcp_routes(app, reload_agents=_reload_agents)

    @app.get("/api/health")
    def api_health() -> Response:
        if runtime is None:
            return jsonify({"error": "runtime not configured"}), 503
        return jsonify(runtime.health())

    @app.get("/api/pipeline-status")
    def api_pipeline_status() -> Response:
        import ai.pipeline_state as _ps
        return jsonify(_ps.get_state())

    return app


