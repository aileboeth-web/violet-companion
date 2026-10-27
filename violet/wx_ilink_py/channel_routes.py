"""Control Center: chat channels (phase 10).

Admin routes (behind the dashboard authentication like every /api/control route):
  GET  /api/control/channels                     every channel with its status, the addable types, proactive channel
  POST /api/control/channels                     add one {type, id, label, config, secrets, enabled}
  POST /api/control/channels/<id>                change label / enabled / config / secrets (empty secret = keep)
  POST /api/control/channels/<id>/delete         {confirm: true}
  POST /api/control/channels/<id>/test           check the platform connection now
  POST /api/control/channels/<id>/regenerate     new webhook secret (shown once) {confirm: true}
  GET  /api/control/channels/<id>/peers          the platform accounts seen on it (bound / pending / blocked)
  POST /api/control/channels/peers/<int:id>      {status: bound | pending | blocked}
  POST /api/control/channels/pairing-code        a one-time code {channel?}: send "/bind <code>" from the new account
  POST /api/control/channels/proactive           {channel: "auto" | <id>}

Public route (no admin session -- it is called by the chat platform, and checks its own signature first):
  POST /channels/hook/<id>                       events of a webhook channel (OneBot v11)

Secrets are write-only: responses carry "is set" and a fingerprint, never a value (except a generated webhook secret,
once, in the response that created it).
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, Response, jsonify, request

logger = logging.getLogger("violet.channels")


def register_channel_routes(app: Flask, runtime) -> None:
    def ok(data: Any = None, status: int = 200) -> Response:
        return jsonify({"ok": True, "data": data}), status

    def bad(message: str, status: int = 400, **extra) -> Response:
        return jsonify({"ok": False, "error": message, **extra}), status

    def actor() -> str:
        return "admin:" + str(request.environ.get("violet.auth") or "unknown")

    def manager():
        return getattr(runtime, "channels", None) if runtime is not None else None

    def guarded(fn):
        """Common errors -> JSON answers (validation problems per field, unknown channel = 404)."""
        from ai.channels.manager import ValidationProblems

        def wrapper(*args, **kwargs):
            mgr = manager()
            if mgr is None:
                return bad("渠道管理没有启动", 503)
            try:
                return fn(mgr, *args, **kwargs)
            except ValidationProblems as exc:
                return bad("保存失败，没有改动", problems=exc.problems)
            except LookupError as exc:
                return bad(str(exc) or "not found", 404)
            except ValueError as exc:
                return bad(str(exc))
        wrapper.__name__ = fn.__name__
        return wrapper

    @app.get("/api/control/channels")
    @guarded
    def control_channels(mgr):
        from ai.channels.manager import channel_types
        types = []
        for kind, spec in channel_types().items():
            types.append({"type": kind, "label": spec["label"], "help": spec["help"], "defaults": spec["defaults"],
                          "config": list(spec["config"]),
                          "secrets": [{"name": n, "required": req} for n, (req, _c) in spec["secrets"].items()]})
        return ok({"channels": mgr.describe(), "types": types, "proactive_channel": mgr.proactive_channel(),
                   "history": mgr.store.history(limit=20)})

    @app.post("/api/control/channels")
    @guarded
    def control_channel_add(mgr):
        body = request.get_json(silent=True) or {}
        result = mgr.add(str(body.get("type") or ""), str(body.get("id") or "").strip(), str(body.get("label") or ""),
                         body.get("config") if isinstance(body.get("config"), dict) else {},
                         body.get("secrets") if isinstance(body.get("secrets"), dict) else {},
                         enabled=body.get("enabled", True) is not False, actor=actor())
        return ok(result, 201)

    @app.post("/api/control/channels/<channel_id>")
    @guarded
    def control_channel_update(mgr, channel_id: str):
        body = request.get_json(silent=True) or {}
        if not any(k in body for k in ("config", "secrets", "label", "enabled")):
            return bad("没有要保存的修改")
        config = body.get("config")
        secrets = body.get("secrets")
        if config is not None and not isinstance(config, dict) or secrets is not None and not isinstance(secrets, dict):
            return bad("config / secrets 要是对象")
        label = body.get("label")
        return ok(mgr.update(channel_id, config=config, secrets=secrets,
                             label=str(label) if label is not None else None, enabled=body.get("enabled"),
                             actor=actor()))

    @app.post("/api/control/channels/<channel_id>/delete")
    @guarded
    def control_channel_delete(mgr, channel_id: str):
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            return bad("删除需要确认（confirm: true）")
        mgr.remove(channel_id, actor=actor())
        return ok({"removed": channel_id})

    @app.post("/api/control/channels/<channel_id>/test")
    @guarded
    def control_channel_test(mgr, channel_id: str):
        return ok(mgr.test(channel_id))

    @app.post("/api/control/channels/<channel_id>/regenerate")
    @guarded
    def control_channel_regenerate(mgr, channel_id: str):
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("需要确认（confirm: true）：旧的签名密钥会立即失效")
        return ok({"shown_once": mgr.regenerate_secret(channel_id, str(body.get("name") or "webhook_secret"),
                                                       actor=actor())})

    @app.get("/api/control/channels/<channel_id>/peers")
    @guarded
    def control_channel_peers(mgr, channel_id: str):
        if mgr.entry(channel_id) is None:
            raise LookupError("没有这个渠道")
        rows = runtime.identities.list(channel_id)
        return ok([{k: r.get(k) for k in ("id", "channel", "peer_id", "display_name", "status", "first_seen_at",
                                          "last_seen_at", "bound_at", "bound_by", "message_count")} for r in rows])

    @app.post("/api/control/channels/peers/<int:identity_id>")
    @guarded
    def control_channel_peer_status(mgr, identity_id: int):
        status = str((request.get_json(silent=True) or {}).get("status") or "")
        row = runtime.identities.set_status(identity_id, status, actor=actor())
        logger.info("channel peer #%s set to %s by %s", identity_id, status, actor())
        return ok({k: row.get(k) for k in ("id", "channel", "peer_id", "display_name", "status", "bound_at")})

    @app.post("/api/control/channels/pairing-code")
    @guarded
    def control_channel_pairing(mgr):
        channel = (request.get_json(silent=True) or {}).get("channel") or None
        if channel is not None and (mgr.entry(str(channel)) is None or channel == "wechat"):
            return bad("这个渠道不需要绑定码", 400)
        return ok(runtime.identities.create_pairing_code(channel))

    @app.post("/api/control/channels/proactive")
    @guarded
    def control_channel_proactive(mgr):
        value = str((request.get_json(silent=True) or {}).get("channel") or "")
        if value != "auto" and mgr.entry(value) is None:
            return bad("没有这个渠道")
        mgr.store.set_proactive_channel(value, actor())
        return ok({"proactive_channel": value})

    # -------------------------------------------------------------- public webhook (own authentication)
    @app.post("/channels/hook/<channel_id>")
    def channel_webhook(channel_id: str):
        mgr = manager()
        adapter = mgr.get(channel_id) if mgr is not None else None
        entry = mgr.entry(channel_id) if mgr is not None else None
        handler = getattr(adapter, "handle_webhook", None)
        if adapter is None or handler is None or not entry or not entry.get("enabled", True):
            return Response(status=404)
        body = request.get_data(cache=False)
        if len(body) > 1_000_000:
            return Response(status=413)
        status, reason = handler(body, request.headers.get("X-Signature"), request.remote_addr)
        if status >= 400:
            logger.warning("webhook %s refused: %s (%s)", channel_id, status, reason)
        return Response(status=status)
