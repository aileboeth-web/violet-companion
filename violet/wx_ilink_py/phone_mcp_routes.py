"""Control Center: the phone (Violet MCP app) pairing card on the tools page (Violet stage 3). Behind admin auth.

  GET  /api/control/phone-mcp           relay values for the phone (address, device secret, certificate pin -- shown
                                        with copy buttons), live relay state, the Violet-side server, the install command
  POST /api/control/phone-mcp/connect   {token}: the access token copied from the phone's "安全" page -> the MCP server
                                        "android-phone" (local relay URL, Authorization: Bearer, the android-phone
                                        permission template, Chat) is added or its token replaced, then checked
  POST /api/control/phone-mcp/task      0.7.0 {action: install | set-port, confirm: true}: the root task service
                                        installs the relay / moves its phone port (progress: /api/control/progress/task)

The relay values come from <data>/control/mcp-relay.json, written by root (`violet mcp-relay export`, run before
every Violet start; tools/violet_relay.py). Unlike other credentials the device secret is shown here on purpose (the
user's request): the admin has to type it into the phone. It is not a Violet credential and never logged.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request

SERVER_NAME = "android-phone"
APP_URL = "https://github.com/aileboeth-web/violet-mcp/releases/latest"
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,512}$")


def relay_file() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "control" / "mcp-relay.json"


def load_relay() -> dict[str, Any] | None:
    try:
        data = json.loads(relay_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("installed") else None


def _local(url: str | None) -> bool:
    try:
        return bool(url) and urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


def relay_health(info: dict[str, Any] | None) -> dict[str, Any]:
    if not info or not _local(info.get("client_url")):
        return {"checked": False}
    import urllib.request
    parts = urlsplit(info["client_url"])
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{parts.scheme}://{parts.netloc}/health", timeout=2) as resp:
            data = json.loads(resp.read(4096).decode("utf-8"))
        return {"checked": True, "reachable": True, "device_connected": bool(data.get("deviceConnected"))}
    except (OSError, ValueError):
        return {"checked": True, "reachable": False, "device_connected": False}


def install_command() -> str | None:
    from ai.updates import install_root
    root = install_root()
    return f"{root / 'violet'} mcp-relay install" if root else None


def _task_info() -> dict[str, Any]:
    """Whether the relay can be installed / moved from the page (a root helper exists), and the last task's progress."""
    from ai.root_tasks import available, read_progress
    return {"available": available(), "progress": read_progress("task")}


def register_phone_mcp_routes(app: Flask, reload_agents=None) -> None:
    def ok(data: Any = None) -> Response:
        return jsonify({"ok": True, "data": data})

    def bad(message: str, status: int = 400) -> Response:
        return jsonify({"ok": False, "error": message}), status

    def actor() -> str:
        return "admin:" + str(request.environ.get("violet.auth") or "unknown")

    def server_entry() -> dict[str, Any] | None:
        from ai.tool_manager import Catalog, describe
        return next((s for s in describe(Catalog(configured=True)) if s["name"] == SERVER_NAME), None)

    @app.get("/api/control/phone-mcp")
    def phone_mcp_status():
        info = load_relay()
        server = server_entry()
        return ok({
            "installed": info is not None, "relay": info, "health": relay_health(info),
            "install_command": install_command(), "app_url": APP_URL, "server_name": SERVER_NAME,
            "set_port_command": (install_command() or "").replace(" install", " set-port") or None,
            "task": _task_info(),
            "server": None if server is None else {
                "status": server.get("status"), "enabled": server.get("enabled"),
                "tools": len(server.get("tools") or []), "url": server.get("url"),
                "token_set": any(h.get("is_set") for h in server.get("headers") or [])},
        })

    @app.post("/api/control/phone-mcp/task")
    def phone_mcp_task():
        """0.7.0: {action: install | set-port, confirm: true} -- asks the root task service (the page then follows
        /api/control/progress/task). Only the action word is passed on."""
        from ai.root_tasks import request_task
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("需要确认（confirm: true）")
        action = {"install": "relay-install", "set-port": "relay-set-port"}.get(str(body.get("action") or ""))
        if action is None:
            return bad("不支持的操作")
        if action == "relay-set-port" and load_relay() is None:
            return bad("还没有安装中继")
        try:
            return ok(request_task(action))
        except PermissionError as exc:
            return bad(str(exc), 409)
        except RuntimeError as exc:
            return bad(str(exc), 409)

    @app.post("/api/control/phone-mcp/connect")
    def phone_mcp_connect():
        from ai.mcp_admin import MCPAdminError, add_server, all_server_names, custom_servers, update_server
        from ai.tool_manager import refresh, tool_store
        token = str((request.get_json(silent=True) or {}).get("token") or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not TOKEN_RE.match(token):
            return bad("访问令牌不对：请在手机 Violet MCP 的“安全”页点“复制”，再粘贴到这里")
        info = load_relay()
        url = (info or {}).get("client_url") or "http://127.0.0.1:8787/mcp"
        try:
            if SERVER_NAME in all_server_names():
                if any(s.get("name") == SERVER_NAME for s in custom_servers()):
                    update_server(SERVER_NAME, {"url": url}, actor())
                tool_store().replace_secret(SERVER_NAME, "Authorization", "Bearer " + token, actor())
            else:
                add_server({"name": SERVER_NAME, "url": url, "transport": "streamable_http", "agents": ["chat"],
                            "header": "Authorization", "header_value": "Bearer " + token,
                            "tool_profile": SERVER_NAME}, actor())
            if not (server_entry() or {}).get("tool_profile"):
                tool_store().set_tool_profile(SERVER_NAME, SERVER_NAME, actor())
        except MCPAdminError as exc:
            return bad("保存失败：" + "；".join(exc.problems.values()))
        except (LookupError, ValueError) as exc:
            return bad(str(exc))
        if reload_agents is not None:
            try:
                reload_agents()
            except Exception:              # noqa: BLE001
                pass
        refresh(SERVER_NAME)
        server = server_entry()
        return ok({"server": None if server is None else {"status": server.get("status"),
                                                          "tools": len(server.get("tools") or [])}})
