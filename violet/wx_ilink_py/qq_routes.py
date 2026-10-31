"""Control Center routes of the channel "QQ" (a personal account through SnowLuma; NapCat until 2026-10-29) and of
Violet's social life (2026-10-29). Behind the admin authentication like every route.

  GET  /api/control/qq                        installed? logged in (and as whom), the channel, the install progress
  POST /api/control/qq/<install|uninstall|restart>   root task (one-click install from the page)
  GET  /api/control/qq/screen[?full=1]        QQ's window (full=1: the whole screen) as a PNG data URL + where it is
  POST /api/control/qq/click                  {x, y}: a click in QQ's window (screen coordinates)
  GET  /api/control/social                    groups (QQ's list + what Violet stored), private chats, settings
  POST /api/control/social/conversation/<id>  {mode, daily_message_limit, daily_decision_limit, notes}
  POST /api/control/social/group              {group_id, mode}: a group from QQ's list (created on first use)
  GET  /api/control/social/conversation/<id>/decisions   her last decisions there (why she spoke / stayed silent)
  GET  /api/control/social/people             the people she knows; POST .../people/<id> {impression}
  0.7.9:
  GET  /api/control/stickers                  her sticker collection; GET .../<id>/file the picture
  POST /api/control/stickers                  {data: base64 picture, description}: upload one
  POST /api/control/stickers/import-qq        the QQ account's own favourite stickers
  POST /api/control/stickers/<id>             {description, enabled}; DELETE .../<id>
  GET  /api/control/qzone                     her QQ 空间 posts, whether she may post now
  POST /api/control/qzone/draft               let her write one (not posted)
  POST /api/control/qzone/publish             {text, confirm: true}: post it now
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, Response, jsonify, request

logger = logging.getLogger("violet.qq")
CHANNEL_ID = "napcat"                    # the stored id of the channel "QQ" since 0.7.8 (ai/channels/snowluma.py)


def register_qq_routes(app: Flask, violetdb, runtime) -> None:
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
            mgr.add("napcat", CHANNEL_ID, "QQ", {}, {}, enabled=True, actor="system:qq-login")
        elif mgr.entry(CHANNEL_ID).get("enabled", True) and adapter() is not None and not adapter().is_running() \
                and mgr.runtime_started():
            mgr.apply(CHANNEL_ID)

    def login_state(info: dict[str, Any]) -> dict[str, Any]:
        """Logged in when the channel runs, or when SnowLuma answers on its WebSocket (it opens it after the login)."""
        a = adapter()
        if a is not None and a.is_running() and getattr(a, "self_id", ""):
            return {"logged_in": True, "uin": a.self_id, "nick": getattr(a, "nickname", "") or None}
        from ai.channels.snowluma import probe_login
        account = probe_login(info)
        if account:
            ensure_channel()
            return {"logged_in": True, **account}
        from ai.channels.vnc import reachable
        return {"logged_in": False, "desktop": reachable(int(info["vnc_port"]))}

    @app.get("/api/control/qq")
    def control_qq():
        from ai import root_tasks
        from ai.channels.snowluma import connection
        info = connection()
        progress = root_tasks.read_progress("task")
        out: dict[str, Any] = {"available": root_tasks.available(), "installed": info is not None,
                               "progress": progress if str(progress.get("task") or "").startswith("qq-") else None,
                               "install_command": "/opt/violet/violet qq install"}
        if info is not None:
            out["login"] = login_state(info)
        mgr = manager()
        if mgr is not None and mgr.entry(CHANNEL_ID) is not None:
            out["channel"] = mgr.describe_one(CHANNEL_ID)
        return ok(out)

    @app.get("/api/control/qq/screen")
    def control_qq_screen():
        import base64
        from ai.channels import vnc
        from ai.channels.snowluma import connection
        info = connection()
        if info is None:
            return bad("还没有安装 QQ（SnowLuma）")
        try:
            shot = vnc.screenshot(int(info["vnc_port"]), str(info.get("vnc_password") or ""),
                                  crop=request.args.get("full") != "1")
        except vnc.VNCError as exc:
            return bad(str(exc), 503)
        image = "data:image/png;base64," + base64.b64encode(shot.pop("png")).decode("ascii")
        response = jsonify({"ok": True, "data": {**shot, "image": image}})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/control/qq/click")
    def control_qq_click():
        from ai.channels import vnc
        from ai.channels.snowluma import connection
        info = connection()
        if info is None:
            return bad("还没有安装 QQ（SnowLuma）")
        body = request.get_json(silent=True) or {}
        try:
            x, y = int(body.get("x")), int(body.get("y"))
        except (TypeError, ValueError):
            return bad("点击位置不对")
        if not (0 <= x < 10000 and 0 <= y < 10000):
            return bad("点击位置不对")
        try:
            vnc.click(int(info["vnc_port"]), str(info.get("vnc_password") or ""), x, y)
        except vnc.VNCError as exc:
            return bad(str(exc), 503)
        return ok({"clicked": [x, y]})

    @app.post("/api/control/qq/<action>")
    def control_qq_action(action: str):
        from ai import root_tasks
        if action not in ("install", "uninstall", "restart"):
            return bad("不支持的操作", 404)
        if action == "uninstall" and (request.get_json(silent=True) or {}).get("confirm") is not True:
            return bad("卸载需要确认（confirm: true）")
        try:
            entry = root_tasks.request_task(f"qq-{action}")
        except PermissionError as exc:
            return bad(str(exc) + f"：/opt/violet/violet qq {action}", 409)
        except (ValueError, RuntimeError) as exc:
            return bad(str(exc), 409)
        if action == "uninstall":
            mgr = manager()
            if mgr is not None and mgr.entry(CHANNEL_ID) is not None:
                try:
                    mgr.update(CHANNEL_ID, enabled=False, actor="system:qq-uninstall")
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
        if body.get("cut") is True:                  # 2026-10-30 翻篇 for this chat only (the messages stay)
            from ai import context_cut
            context_cut.cut(violetdb, conversation_id=conversation_id)
        if fields:
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

    # ------------------------------------------------------------------ 0.7.9: stickers
    def sticker_public(row: dict[str, Any]) -> dict[str, Any]:
        from ai.stickers import SOURCES
        return {"id": row["id"], "description": row.get("description") or "", "source": row["source"],
                "source_text": SOURCES.get(row["source"], row["source"]), "source_detail": row.get("source_detail"),
                "enabled": bool(row["enabled"]), "use_count": row["use_count"], "last_used_at": row.get("last_used_at"),
                "created_at": row["created_at"], "mime_type": row["mime_type"], "size": row["size"]}

    @app.get("/api/control/stickers")
    def control_stickers():
        from ai import stickers
        from ai.settings import env_bool
        return ok({"stickers": [sticker_public(r) for r in stickers.listing(violetdb)], "enabled": stickers.enabled(),
                   "auto_collect": env_bool("VIOLET_STICKER_AUTO_COLLECT", True),
                   "look": env_bool("VIOLET_STICKER_LOOK", True),
                   "qq_connected": bool(adapter() is not None and adapter().is_running())})

    @app.get("/api/control/stickers/<int:sticker_id>/file")
    def control_sticker_file(sticker_id: int):
        from flask import send_file
        from ai import stickers
        row = stickers.get(violetdb, sticker_id)
        if row is None or not stickers.path_of(row).is_file():
            return bad("没有这个表情包", 404)
        response = send_file(stickers.path_of(row), mimetype=row["mime_type"])
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'"
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    @app.post("/api/control/stickers")
    def control_sticker_upload():
        import base64
        import binascii
        from ai import stickers
        body = request.get_json(silent=True) or {}
        raw = str(body.get("data") or "")
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return bad("图片数据不对")
        description = str(body.get("description") or "").strip() or None
        try:
            row = stickers.add_bytes(violetdb, data, source="upload", description=description)
        except stickers.StickerError as exc:
            return bad(str(exc))
        if not row.get("description"):
            described = stickers.describe(stickers.path_of(row))
            if described:
                row = stickers.update(violetdb, row["id"], description=described) or row
        return ok(sticker_public(row))

    @app.post("/api/control/stickers/import-qq")
    def control_sticker_import():
        from ai import stickers
        a = adapter()
        if a is None or not a.is_running():
            return bad("QQ 没有连接")
        try:                                         # the rest of the descriptions: the automatic sync (6 hours)
            return ok(stickers.import_qq(violetdb, a, describe_limit=8))
        except Exception as exc:                     # noqa: BLE001
            return bad(f"读不到 QQ 收藏的表情：{exc}")

    @app.get("/api/control/stickers/pack")
    def control_sticker_pack_status():
        from ai import sticker_pack
        return ok({**sticker_pack.status(), "missing": len(sticker_pack.missing(violetdb)),
                   "size": len(sticker_pack.PACK)})

    @app.post("/api/control/stickers/pack")
    def control_sticker_pack():                     # 2026-10-31: a starter set drawn with the owner's image model
        from ai import sticker_pack
        try:
            return ok(sticker_pack.start(violetdb, redraw=(request.get_json(silent=True) or {}).get("redraw") is True))
        except ValueError as exc:
            return bad(str(exc))

    @app.post("/api/control/stickers/<int:sticker_id>")
    def control_sticker_update(sticker_id: int):
        from ai import stickers
        if stickers.get(violetdb, sticker_id) is None:
            return bad("没有这个表情包", 404)
        body = request.get_json(silent=True) or {}
        if body.get("describe") is True:
            text = stickers.describe(stickers.path_of(stickers.get(violetdb, sticker_id)))
            if not text:
                return bad("识图没有成功：看看“图片”页的识图测试")
            body["description"] = text
        row = stickers.update(violetdb, sticker_id, description=body.get("description"),
                              enabled_flag=body.get("enabled") if "enabled" in body else None)
        return ok(sticker_public(row))

    @app.delete("/api/control/stickers/<int:sticker_id>")
    def control_sticker_delete(sticker_id: int):
        from ai import stickers
        if not stickers.delete(violetdb, sticker_id):
            return bad("没有这个表情包", 404)
        return ok({"deleted": sticker_id})

    # ------------------------------------------------------------------ 0.7.9: QQ 空间
    @app.get("/api/control/qzone")
    def control_qzone():
        from ai.settings import env_int
        from ai.social import qzone
        posts = violetdb.fetch_all("SELECT * FROM social_posts ORDER BY id DESC LIMIT 30")
        return ok({"enabled": qzone.enabled(), "daily": env_int("VIOLET_QZONE_DAILY", 1), "posts": posts,
                   "waiting": qzone.due(violetdb), "qq_connected": bool(adapter() is not None and adapter().is_running())})

    @app.post("/api/control/qzone/draft")
    def control_qzone_draft():
        from ai.social import qzone
        try:
            idea = qzone.compose(violetdb)
        except qzone.QzoneError as exc:
            return bad(str(exc))
        return ok(idea)

    @app.post("/api/control/qzone/publish")
    def control_qzone_publish():
        from ai.social import qzone
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("发动态需要确认（confirm: true）")
        text = " ".join(str(body.get("text") or "").split())[:1000]
        if not text:
            return bad("动态内容是空的")
        try:
            return ok(qzone.post_now(runtime, text, origin="manual"))
        except qzone.QzoneError as exc:
            return bad(str(exc))
