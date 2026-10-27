"""Restarting Violet from the Control Center (Violet stage 3).

main.py exits with RESTART_EXIT_CODE; the service manager starts it again: systemd because the unit says
Restart=on-failure (any non-zero exit), tools/violet_service.py because it restarts every exit that was not a stop.
Run by hand (``python main.py``) nothing would start it again, so the page does not offer the button then.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

from flask import Flask, current_app, jsonify, request

RESTART_EXIT_CODE = 75          # EX_TEMPFAIL


def supervised(environ: dict | None = None) -> bool:
    """True under systemd (it sets INVOCATION_ID for every service) or Violet's own supervisor."""
    environ = os.environ if environ is None else environ
    return bool(environ.get("INVOCATION_ID") or environ.get("VIOLET_SUPERVISED"))


def restart_info(app: Flask | None = None) -> dict[str, Any]:
    entry = (app or current_app).extensions.get("violet_restart") or {}
    available = bool(entry.get("restart")) and bool(entry.get("supervised"))
    reason = None if available else ("Violet 不是由系统服务运行的（例如在命令行里直接启动），网页重启后没有程序会再启动它；"
                                     "请在服务器上重新启动")
    return {"available": available, "reason": reason}


def register_restart_routes(app: Flask, delay: float = 0.5, timer: Callable[..., Any] = threading.Timer) -> None:
    @app.get("/api/control/restart")
    def control_restart_info():
        return jsonify({"ok": True, "data": restart_info()})

    @app.post("/api/control/restart")
    def control_restart():
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return jsonify({"ok": False, "error": "重启需要确认（confirm: true）"}), 400
        info = restart_info()
        if not info["available"]:
            return jsonify({"ok": False, "error": info["reason"]}), 409
        # answer first, then stop: the page shows "restarting" and polls /healthz until Violet is back
        timer(delay, current_app.extensions["violet_restart"]["restart"]).start()
        return jsonify({"ok": True, "data": {"restarting": True}})
