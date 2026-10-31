"""Control Center routes of her voice (2026-10-31; ai/voice.py, tools/violet_voice.py). Behind the admin
authentication like every route.

  GET    /api/control/voice                  installed? service health, the recording, the server requirements,
                                             the install progress
  POST   /api/control/voice/<install|uninstall>   root task (one-click install from the page)
  POST   /api/control/voice/reference        {data: base64 recording (optional), text}: the voice to clone
  DELETE /api/control/voice/reference        delete the recording
  POST   /api/control/voice/test             {text}: say it now (about a minute) -> audio/wav, for the page's 试听
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from flask import Flask, Response, jsonify, request


def register_voice_routes(app: Flask) -> None:
    def ok(data: Any = None, status: int = 200) -> Response:
        return jsonify({"ok": True, "data": data}), status

    def bad(message: str, status: int = 400) -> Response:
        return jsonify({"ok": False, "error": message}), status

    @app.get("/api/control/voice")
    def control_voice():
        from ai import root_tasks, voice
        from ai.settings import env_bool
        progress = root_tasks.read_progress("task")
        try:
            from tools.violet_voice import REQUIREMENTS
        except ImportError:
            REQUIREMENTS = []
        return ok({"installed": voice.service() is not None, "health": voice.health(), "reference": voice.reference(),
                   "enabled": env_bool("VIOLET_VOICE", True), "max_chars": voice.max_chars(),
                   "requirements": REQUIREMENTS, "can_install": root_tasks.available(),
                   "progress": progress if str(progress.get("task") or "").startswith("voice-") else None,
                   "command": "/opt/violet/violet voice install"})

    @app.post("/api/control/voice/<action>")
    def control_voice_action(action: str):
        from ai import root_tasks
        if action not in ("install", "uninstall"):
            return bad("不支持的操作", 404)
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            return bad("需要确认（confirm: true）")
        try:
            return ok(root_tasks.request_task("voice-" + action))
        except (PermissionError, RuntimeError) as exc:
            return bad(str(exc), 409)

    @app.post("/api/control/voice/reference")
    def control_voice_reference():
        from ai import voice
        body = request.get_json(silent=True) or {}
        data = None
        raw = str(body.get("data") or "")
        if raw:
            if raw.startswith("data:") and "," in raw:
                raw = raw.split(",", 1)[1]
            try:
                data = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return bad("录音数据不对")
        text = body.get("text")
        try:
            return ok(voice.save_reference(data, str(text) if text is not None else None))
        except voice.VoiceError as exc:
            return bad(str(exc))

    @app.delete("/api/control/voice/reference")
    def control_voice_reference_delete():
        from ai import voice
        voice.delete_reference()
        return ok(voice.reference())

    @app.post("/api/control/voice/test")
    def control_voice_test():
        from ai import voice
        text = " ".join(str((request.get_json(silent=True) or {}).get("text") or "").split())[:120]
        if not text:
            return bad("写一句要她说的话")
        try:
            audio = voice.speak(text, "wav")
        except voice.VoiceError as exc:
            return bad(str(exc), 409)
        return Response(audio, mimetype="audio/wav", headers={"Cache-Control": "no-store"})
