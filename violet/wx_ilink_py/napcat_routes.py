"""Control Center routes of the channel "QQ" (a personal account through NapCat) and of Violet's social life
(2026-10-29). Behind the admin authentication like every route.

  GET  /api/control/napcat                    installed? login state + QR code, the channel, the install progress
  POST /api/control/napcat/<install|uninstall|restart>   root task (one-click install from the page)
  POST /api/control/napcat/refresh-qr         a new QR code
  GET  /api/control/social                    groups (NapCat's list + what Violet stored), private chats, settings
  POST /api/control/social/conversation/<id>  {mode, daily_message_limit, daily_decision_limit, notes}
  POST /api/control/social/group              {group_id, mode}: a group from NapCat's list (created on first use)
  GET  /api/control/social/conversation/<id>/decisions   her last decisions there (why she spoke / stayed silent)
  GET  /api/control/social/people             the people she knows; POST .../people/<id> {impression}
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, Response, jsonify, request

logger = logging.getLogger("violet.napcat")
CHANNEL_ID = "napcat"


def register_napcat_routes(app: Flask, violetdb, runtime) -> None:
    from ai.social.store import GROUP_MODES, MODE_TEXT, SocialStore

    def ok(data: Any = None, status: int = 200) -> Response:
        return jsonify({"ok": True, "data": data}), status

    def bad(message: str, status: int = 400) -> Response:
        return jsonify({"ok": False, "error": message}), status

    def manager():
        return getattr(runtime, "channels", None) if runtime is not None else None

    def adapter():
        mgr = manager()
        return mgr.get(CHANNEL_ID) if mgr is not None and mgr.entry(CHANNEL_ID) else None

    def ensure_channel() -> None:
        """Logged in: the channel exists and is on (created the first time, like the other cards do on save)."""
        mgr = manager()
        if mgr is None:
            return
        if mgr.entry(CHANNEL_ID) is None:
            mgr.add("napcat", CHANNEL_ID, "QQ", {}, {}, enabled=True, actor="system:napcat-login")
        elif mgr.entry(CHANNEL_ID).get("enabled", True) and adapter() is not None and not adapter().is_running() \
                and mgr.runtime_started():
            mgr.apply(CHANNEL_ID)

    @app.get("/api/control/napcat")
    def control_napcat():
        from ai import root_tasks
        info = root_tasks.napcat_export()
        progress = root_tasks.read_progress("task")
        out: dict[str, Any] = {"available": root_tasks.available(), "installed": info is not None,
                               "progress": progress if str(progress.get("task") or "").startswith("napcat") else None,
                               "install_command": "/opt/violet/violet napcat install"}
        if info is not None:
            from ai.channels import napcat_webui
            try:
                state = napcat_webui.login_state(info)
                out["login"] = state
                if state.get("logged_in"):
                    ensure_channel()
                    if state.get("uin"):
                        try:
                            napcat_webui.remember_account(info, state["uin"])
                        except napcat_webui.WebUIError:
                            pass
            except napcat_webui.WebUIError as exc:
                out["login"] = {"logged_in": False, "error": str(exc), "starting": True}
        mgr = manager()
        if mgr is not None and mgr.entry(CHANNEL_ID) is not None:
            out["channel"] = mgr.describe_one(CHANNEL_ID)
        return ok(out)

    @app.post("/api/control/napcat/<action>")
    def control_napcat_action(action: str):
        from ai import root_tasks
        if action == "refresh-qr":
            from ai.channels import napcat_webui
            info = root_tasks.napcat_export()
            if info is None:
                return bad("还没有安装 QQ（NapCat）")
            try:
                napcat_webui.refresh_qr(info)
            except napcat_webui.WebUIError as exc:
                return bad(str(exc))
            return ok({"refreshed": True})
        if action not in ("install", "uninstall", "restart"):
            return bad("不支持的操作", 404)
        if action == "uninstall" and (request.get_json(silent=True) or {}).get("confirm") is not True:
            return bad("卸载需要确认（confirm: true）")
        try:
            entry = root_tasks.request_task(f"napcat-{action}")
        except PermissionError as exc:
            return bad(str(exc) + f"：/opt/violet/violet napcat {action}", 409)
        except (ValueError, RuntimeError) as exc:
            return bad(str(exc), 409)
        if action == "uninstall":
            mgr = manager()
            if mgr is not None and mgr.entry(CHANNEL_ID) is not None:
                try:
                    mgr.update(CHANNEL_ID, enabled=False, actor="system:napcat-uninstall")
                except Exception:
                    logger.exception("switching the QQ channel off failed")
        return ok(entry)

    # ------------------------------------------------------------------ social life
    def conv_public(c: dict[str, Any], social: SocialStore) -> dict[str, Any]:
        today = social.today(c["id"])
        return {"id": c["id"], "kind": c["kind"], "peer_id": c["peer_id"], "title": c.get("title") or c["peer_id"],
                "mode": c["mode"], "mode_text": MODE_TEXT.get(c["mode"], c["mode"]),
                "daily_message_limit": c["daily_message_limit"], "daily_decision_limit": c["daily_decision_limit"],
                "notes": c.get("notes") or "", "last_message_at": c.get("last_message_at"),
                "message_count": c.get("message_count"), "today": today, "next_wake_at": c.get("next_wake_at")}

    @app.get("/api/control/social")
    def control_social():
        from ai.social.engine import social_settings
        social = SocialStore(violetdb)
        stored = {c["peer_id"]: c for c in social.conversations(channel=CHANNEL_ID, kind="group")}
        groups, error = [], None
        a = adapter()
        if a is not None and a.is_running():
            try:
                for g in a.groups():
                    known = stored.pop(g["group_id"], None) or social.conversation(CHANNEL_ID, "group", g["group_id"],
                                                                                   g["group_name"])
                    if g["group_name"] and known.get("title") != g["group_name"]:
                        social.update(known["id"], title=g["group_name"])
                        known["title"] = g["group_name"]
                    groups.append({**conv_public({**known, "message_count": None}, social),
                                   "member_count": g.get("member_count"), "in_group": True})
            except Exception as exc:                 # noqa: BLE001 -- the page still shows what is stored
                error = str(exc)
        else:
            error = "QQ 没有连接：登录后这里会列出小号所在的群"
        # not in NapCat's list: left the group -- or the list could not be read (then nothing is claimed)
        groups += [{**conv_public(c, social), "in_group": False if error is None else None} for c in stored.values()]
        privates = [conv_public(c, social) for c in social.conversations(channel=CHANNEL_ID, kind="private")]
        return ok({"groups": groups, "privates": privates, "error": error, "modes": [
            {"value": m, "text": MODE_TEXT[m]} for m in GROUP_MODES], "settings": social_settings()})

    def _limit(value: Any) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("上限要填 0 或正整数（0 = 不限）") from None
        if n < 0 or n > 100000:
            raise ValueError("上限要填 0 或正整数（0 = 不限）")
        return n

    @app.post("/api/control/social/conversation/<int:conversation_id>")
    def control_social_conversation(conversation_id: int):
        social = SocialStore(violetdb)
        conv = social.get(conversation_id)
        if conv is None:
            return bad("没有这个会话", 404)
        body = request.get_json(silent=True) or {}
        fields: dict[str, Any] = {}
        try:
            if "mode" in body:
                allowed = GROUP_MODES if conv["kind"] == "group" else ("off", "auto")
                if body["mode"] not in allowed:
                    return bad("没有这个模式")
                fields["mode"] = body["mode"]
            for key in ("daily_message_limit", "daily_decision_limit"):
                if key in body:
                    fields[key] = _limit(body[key])
        except ValueError as exc:
            return bad(str(exc))
        if "notes" in body:
            fields["notes"] = str(body["notes"] or "")[:600]
        social.update(conversation_id, **fields)
        return ok(conv_public({**social.get(conversation_id), "message_count": None}, social))

    @app.get("/api/control/social/conversation/<int:conversation_id>/decisions")
    def control_social_decisions(conversation_id: int):
        social = SocialStore(violetdb)
        if social.get(conversation_id) is None:
            return bad("没有这个会话", 404)
        return ok({"decisions": social.decisions(conversation_id, limit=40)})

    @app.get("/api/control/social/people")
    def control_social_people():
        return ok({"people": SocialStore(violetdb).people(channel=CHANNEL_ID)})

    @app.post("/api/control/social/people/<int:person_id>")
    def control_social_person(person_id: int):
        row = violetdb.fetch_one("SELECT * FROM social_people WHERE id = ?", (person_id,))
        if row is None:
            return bad("没有这个人", 404)
        body = request.get_json(silent=True) or {}
        if "impression" in body:
            violetdb.execute("UPDATE social_people SET impression = ? WHERE id = ?",
                             (str(body["impression"] or "")[:400] or None, person_id))
        if body.get("forget") is True:
            violetdb.execute("UPDATE social_people SET impression = NULL WHERE id = ?", (person_id,))
        return ok(violetdb.fetch_one("SELECT * FROM social_people WHERE id = ?", (person_id,)))
