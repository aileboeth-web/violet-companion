"""Control Center: WeChat login in the channels page, and the run log (Violet stage 3).

  GET  /api/control/wechat            login state; while logging in, the QR code (a data: URL image) and its status
  POST /api/control/wechat/login      {force_refresh?}: start the QR login (nothing happens when already logged in)
  POST /api/control/wechat/logout     {confirm: true}: forget the saved WeChat credentials
  GET  /api/control/logs              ?level=&q=&turn=&logger=&limit=  newest records first, secrets removed
  GET  /api/control/logs/download     the newest part of the log as a text file, secrets removed

The same admin authentication as every route (auth.py). The QR code is shown only to a logged-in admin; the saved
credentials themselves are never returned.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import Flask, Response, jsonify, request

QR_STATUS = {"wait": "等待扫码", "scaned": "已扫码，请在手机上确认", "scaned_but_redirect": "已扫码，正在切换服务器",
             "expired": "二维码已过期，正在换新的", "confirmed": "已确认，正在登录", "idle": "未开始"}


def _mask(value: str) -> str:
    value = value or ""
    return value if len(value) <= 8 else value[:4] + "…" + value[-4:]


def register_ops_page_routes(app: Flask, client) -> None:
    def ok(data: Any = None) -> Response:
        return jsonify({"ok": True, "data": data})

    def bad(message: str, status: int = 400) -> Response:
        return jsonify({"ok": False, "error": message}), status

    # ------------------------------------------------------------------ WeChat login
    @app.get("/api/control/wechat")
    def control_wechat():
        s = client.get_status()
        qr = s.get("qr_state") or {}
        logging_in = s.get("login_mode") == "logging_in" and not s.get("logged_in")
        return ok({
            "logged_in": bool(s.get("logged_in")), "account": _mask(str(s.get("account_id") or "")),
            "login_mode": s.get("login_mode"), "logging_in": logging_in,
            "error": s.get("last_error") or None,
            "qr": {"status": qr.get("status") or "idle", "status_text": QR_STATUS.get(qr.get("status") or "idle", qr.get("status")),
                   "image": qr.get("qrcode_img_content") if logging_in else None,
                   "url": qr.get("qrcode_url") if logging_in else None,
                   "refreshes": qr.get("expires_refresh_count") or 0} if logging_in else None,
        })

    @app.post("/api/control/wechat/login")
    def control_wechat_login():
        body = request.get_json(silent=True) or {}
        client.start_login_async(force_refresh=bool(body.get("force_refresh")))
        return ok({"started": True})

    @app.post("/api/control/wechat/logout")
    def control_wechat_logout():
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            return bad("退出微信登录需要确认（confirm: true）")
        client.clear_credentials()
        return ok({"logged_in": False})

    # ------------------------------------------------------------------ run log
    @app.get("/api/control/logs")
    def control_logs():
        from ai.log_viewer import read
        args = request.args
        try:
            limit = int(args.get("limit") or 300)
        except ValueError:
            limit = 300
        from ai.trace import enabled
        result = read(level=args.get("level") or "INFO", query=(args.get("q") or "")[:200],
                      turn=(args.get("turn") or "")[:64], logger=(args.get("logger") or "")[:100], limit=limit)
        return ok({**result, "trace_enabled": enabled()})

    @app.get("/api/control/logs/download")
    def control_logs_download():
        from ai.log_viewer import download_text
        name = f"violet-log-{datetime.now():%Y%m%d-%H%M}.txt"
        return Response(download_text(), mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
