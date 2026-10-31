"""Violet Control Center (phase 9.0): overview, chat history, memories, settings.

Every route here is behind the dashboard's admin authentication (auth.py ``before_request``: Bearer token or the admin
session; state changes also need a same-origin request). Violet has ONE conversation partner (the messages table has no
user column): the model is a single administrator looking at that one conversation -- there is no multi-user data
here, and nothing pretends there is.

Chat history is read-only (no edits, no deletion). Memory corrections go through the existing revision-recording
paths (memory_revisions); "delete" of an event memory is the existing soft retract (status = deleted, revision kept);
a core memory is deleted only after its content was written to memory_revisions.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from flask import Flask, Response, jsonify, render_template, request

logger = logging.getLogger("violet.control")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_PAGE = 200
EXPORT_LIMIT = 20_000


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _error_type(text: str | None) -> str | None:
    """Only the kind of an error ("TimeoutError"), never its text (it may quote a message)."""
    if not text:
        return None
    head = str(text).split(":", 1)[0].strip()
    return head[:60] if re.match(r"^[A-Za-z_][\w.]*$", head) else "error"


STYLESHEET_LINK = '<link rel="stylesheet" href="/static/control.css">'


def _inline_stylesheet(html: str, static_folder: str | None) -> str:
    """Violet stage 3: the stylesheet goes into the page itself, like the login page's.

    On the user's computer every script loaded but the one /static/control.css request failed in the browser
    (no status, "provisional headers"; served fine from the server itself) -- the page was plain text. With the
    styles in the page there is no separate request left to lose. The link stays when the file cannot be read.
    """
    if not static_folder or STYLESHEET_LINK not in html:
        return html
    try:
        with open(f"{static_folder}/control.css", encoding="utf-8") as fh:
            css = fh.read()
    except OSError:
        return html
    if "</style" in css.lower():
        return html
    return html.replace(STYLESHEET_LINK, "<style>\n" + css + "\n</style>", 1)


def register_control_routes(app: Flask, violetdb, client=None, proactive_scheduler=None, all_agents=None, runtime=None,
                            reload_all: Callable[[], None] | None = None) -> None:
    from ai.control_config import GROUPS, StoreError, ValidationError, store

    def ok(data: Any = None, status: int = 200) -> Response:
        return jsonify({"ok": True, "data": data}), status

    def bad(message: str, status: int = 400, **extra) -> Response:
        return jsonify({"ok": False, "error": message, **extra}), status

    def actor() -> str:
        return "admin:" + str(request.environ.get("violet.auth") or "unknown")

    def apply_now() -> None:
        if reload_all is not None:
            reload_all()

    # ------------------------------------------------------------------ page
    @app.get("/control")
    def control_page() -> str:
        return _inline_stylesheet(render_template("control.html"), app.static_folder)

    # ------------------------------------------------------------------ overview
    @app.get("/api/control/overview")
    def control_overview():
        agents = []
        for agent in all_agents or []:
            cfg = getattr(agent, "_config", None)
            agents.append({"agent": agent.__class__.__name__, "model": getattr(cfg, "reply_model", None),
                           "fallback_model": getattr(cfg, "fallback_model", None) or None,
                           "temperature": getattr(cfg, "reply_temperature", None)})
        health = None
        if runtime is not None:
            try:
                h = runtime.health()
                health = {k: h.get(k) for k in ("poll_thread_alive", "worker_thread_alive", "memory_worker_alive",
                                                 "last_poll_ok_at", "consecutive_poll_errors", "queued_turns",
                                                 "inbound", "outbound", "memory_jobs", "stats")}
                health["last_poll_error"] = _error_type(h.get("last_poll_error"))
            except Exception as exc:
                health = {"error": exc.__class__.__name__}
        wechat = None
        if client is not None:
            try:
                status = client.get_status()
                wechat = {"logged_in": bool(status.get("logged_in")), "login_in_progress": bool(status.get("login_in_progress"))}
            except Exception:
                wechat = None
        proactive = None
        if proactive_scheduler is not None:
            try:
                snap = proactive_scheduler.status_snapshot()
                proactive = {k: snap.get(k) for k in ("enabled", "is_due", "due_reason", "next_proactive_at",
                                                      "daily_sent_count", "daily_message_limit", "cold_streak",
                                                      "last_proactive_sent_at")}
            except Exception as exc:
                proactive = {"error": exc.__class__.__name__}
        life = None
        try:
            from ai.life_scheduler import LifeControl
            status = LifeControl(violetdb).status()
            last = status.get("last_run") or {}
            life = {"mode": status.get("mode"), "last_run_key": last.get("run_key"), "last_outcome": last.get("outcome")}
        except Exception as exc:
            life = {"error": exc.__class__.__name__}
        since = (datetime.fromisoformat(violetdb.now()) - timedelta(days=1)).isoformat()
        aux = violetdb.fetch_all("SELECT status, COUNT(*) AS n, ROUND(AVG(latency_ms)) AS avg_ms, SUM(total_tokens) AS tokens "
                              "FROM aux_llm_calls WHERE created_at >= ? GROUP BY status", (since,))
        errors = []
        for row in violetdb.fetch_all("SELECT id, status, error, updated_at FROM inbound_messages WHERE status = 'failed' "
                                   "ORDER BY id DESC LIMIT 5"):
            errors.append({"where": "收到的消息", "id": row["id"], "at": row["updated_at"], "type": _error_type(row["error"])})
        for row in violetdb.fetch_all("SELECT id, status, error, updated_at FROM outbound_messages WHERE status = 'failed' "
                                   "ORDER BY id DESC LIMIT 5"):
            errors.append({"where": "发出的消息", "id": row["id"], "at": row["updated_at"], "type": _error_type(row["error"])})
        for row in violetdb.fetch_all("SELECT id, status, last_error, updated_at FROM memory_jobs WHERE status = 'failed' "
                                   "ORDER BY id DESC LIMIT 5"):
            errors.append({"where": "记忆任务", "id": row["id"], "at": row["updated_at"], "type": _error_type(row["last_error"])})
        for row in violetdb.fetch_all("SELECT id, status, reason, updated_at FROM proactive_events WHERE status = 'failed' "
                                   "ORDER BY id DESC LIMIT 5"):
            errors.append({"where": "主动联系", "id": row["id"], "at": row["updated_at"], "type": "failed"})
        errors.sort(key=lambda e: e.get("at") or "", reverse=True)
        # 0.7.5 (user, with QQ bound: the overview showed only "微信 未登录" and WeChat's message counter): every
        # channel's state, and the message totals of the chat history (all channels)
        channels = []
        manager = getattr(runtime, "channels", None) if runtime is not None else None
        if manager is not None:
            try:
                channels = [{"label": c["label"], "type": c["type"], "state": c["status"].get("state")}
                            for c in manager.describe()]
            except Exception:
                channels = []
        totals = {r["role"]: int(r["n"]) for r in violetdb.fetch_all(
            "SELECT role, COUNT(*) AS n FROM messages GROUP BY role")}
        return ok({"agents": agents, "service": health, "wechat": wechat, "proactive": proactive, "life": life,
                   "channels": channels,
                   "message_totals": {"received": totals.get("user", 0), "sent": totals.get("assistant", 0)},
                   "aux_calls_24h": aux, "messages": violetdb.get_message_stats(), "errors": errors[:12],
                   "settings_revision": store().revision(),
                   "usage": _usage_summary(),
                   "notes": {"model_calls": ""}})

    # 2026-10-30: what she may do by herself, and what waits for the owner's approval (ai/permissions.py)
    @app.get("/api/control/permissions")
    def control_permissions():
        from ai import permissions
        return ok({"abilities": permissions.describe(), "levels": permissions.LEVELS,
                   "pending": permissions.pending(violetdb), "recent": permissions.recent(violetdb)})

    @app.post("/api/control/permissions")
    def control_permissions_set():
        from ai import permissions
        body = request.get_json(silent=True) or {}
        try:
            permissions.set_level(str(body.get("ability") or ""), str(body.get("level") or ""))
        except ValueError as exc:
            return bad(str(exc))
        return ok({"abilities": permissions.describe()})

    @app.post("/api/control/approvals/<int:number>")
    def control_approval(number: int):
        from ai import permissions
        body = request.get_json(silent=True) or {}
        if not isinstance(body.get("approve"), bool):
            return bad("approve 要是 true 或 false")
        try:
            return ok(permissions.decide(violetdb, number, body["approve"], tell=True))
        except ValueError as exc:
            return bad(str(exc))

    @app.get("/api/control/last-prompt")
    def control_last_prompt():
        """2026-10-30: what the last reply was built from (rules, persona, context, history, the message)."""
        from ai.agents.chat_agent import LAST_PROMPT
        return ok(dict(LAST_PROMPT) or None)

    def _usage_summary():
        from ai import usage                    # 2026-10-30: tokens of every model call today
        try:
            return usage.summary(violetdb)
        except Exception:                       # noqa: BLE001 -- an old database without the table
            return None

    # ------------------------------------------------------------------ chat history (read only)
    def _proactive_index(day_from: str, day_to: str) -> list[dict]:
        """Violet's sends that were not replies (proactive messages, reminders, system notices), from the outbox."""
        return violetdb.fetch_all(
            "SELECT o.message_id, o.content, o.origin, o.proactive_event_id, o.created_at, pe.event_type "
            "FROM outbound_messages o "
            "LEFT JOIN proactive_events pe ON pe.id = o.proactive_event_id WHERE o.created_at >= ? AND o.created_at < ? "
            "AND (o.proactive_event_id IS NOT NULL OR o.origin != 'reply')", (day_from, day_to))

    def _outbox_kind(o: dict) -> str:
        if o.get("event_type") == "commitment_reminder":
            return "reminder"
        if o.get("proactive_event_id") or o.get("origin") == "proactive":
            return "proactive"
        return "system" if o.get("origin") == "system" else "violet"

    def _kind(row: dict, outbound: list[dict]) -> str:
        """user | violet (a reply) | proactive | reminder | system. The outbox row that recorded this message
        (outbound_messages.message_id) decides; older rows without that link are matched by text and time
        (+-10 min). A message with no outbox match is shown as a reply."""
        if row["role"] == "user":
            return "user"
        for o in outbound:
            if o.get("message_id") == row["id"]:
                return _outbox_kind(o)
        try:
            at = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            return "violet"
        for o in outbound:
            if o.get("message_id") or o["content"] != row["content"]:
                continue
            try:
                if abs((datetime.fromisoformat(o["created_at"]) - at).total_seconds()) <= 600:
                    return _outbox_kind(o)
            except (TypeError, ValueError):
                continue
        return "violet"

    @app.get("/api/control/messages/days")
    def control_message_days():
        channel = (request.args.get("channel") or "").strip()[:64]
        where, params = [], []
        if channel:
            where.append("COALESCE(channel, 'wechat') = ?")
            params.append(channel)
            if "peer" in request.args:                   # 2026-10-30: one account's window
                where.append("COALESCE(peer_id, ?) = ?")
                params += [_fallback_peer(channel), (request.args.get("peer") or "")[:64]]
        rows = violetdb.fetch_all("SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM messages "
                                  + ("WHERE " + " AND ".join(where) + " " if where else "")
                                  + "GROUP BY day ORDER BY day DESC LIMIT 400", params)
        return ok(rows)

    @app.get("/api/control/messages")
    def control_messages():
        day = request.args.get("date") or ""
        if day and not DATE_RE.match(day):
            return bad("date 的格式是 YYYY-MM-DD")
        query = (request.args.get("q") or "").strip()[:100]
        role = request.args.get("role") or ""
        if role not in ("", "user", "assistant"):
            return bad("role 只能是 user 或 assistant")
        limit = _int(request.args.get("limit"), 100, 1, MAX_PAGE)
        before = _int(request.args.get("before_id"), 0, 0, 2**62)
        where, params = [], []
        if day:
            where.append("created_at >= ? AND created_at < ?")
            nxt = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
            params += [day, nxt]
        if query:
            where.append("content LIKE ? ESCAPE '\\'")
            params.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if role:
            where.append("role = ?")
            params.append(role)
        if before:
            where.append("id < ?")
            params.append(before)
        channel = (request.args.get("channel") or "").strip()[:64]
        if channel:                                     # 2026-10-29: the owner's chat on one channel (NULL = WeChat)
            where.append("COALESCE(channel, 'wechat') = ?")
            params.append(channel)
            if "peer" in request.args:                   # 2026-10-30: one bound account's window
                where.append("COALESCE(peer_id, ?) = ?")
                params += [_fallback_peer(channel), (request.args.get("peer") or "")[:64]]
        sql = ("SELECT id, role, direction, message_type, content, created_at FROM messages"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?")
        rows = violetdb.fetch_all(sql, (*params, limit))
        if rows:
            lo = min(r["created_at"] for r in rows if r.get("created_at")) if any(r.get("created_at") for r in rows) else ""
            hi_dt = max((r["created_at"] for r in rows if r.get("created_at")), default="")
            outbound = _proactive_index((lo or "")[:10], (datetime.fromisoformat(hi_dt) + timedelta(days=1)).isoformat()
                                        if hi_dt else "9999")
            for r in rows:
                r["kind"] = _kind(r, outbound)
        rows.reverse()
        return ok({"messages": rows, "next_before_id": rows[0]["id"] if len(rows) == limit else None})

    # ------------------------------------------------------------------ 2026-10-29: chat history per channel
    # The owner's chat ("你", table messages, split by channel) and Violet's other conversations (groups, other
    # people: ai/social) are listed per channel; the page shows channel -> conversations on the left -> messages.
    def _channel_labels() -> dict[str, dict[str, str]]:
        labels: dict[str, dict[str, str]] = {}
        manager = getattr(runtime, "channels", None) if runtime is not None else None
        if manager is not None:
            try:
                for c in manager.describe():
                    labels[c["id"]] = {"label": c.get("label") or c["id"], "type": c.get("type") or ""}
            except Exception:
                pass
        labels.setdefault("wechat", {"label": "微信", "type": "wechat_ilink"})
        return labels

    def _fallback_peer(channel: str) -> str:
        """Older messages of the owner's chat without a sender belong to the account bound first."""
        from ai.channels.identity import IdentityStore
        return IdentityStore(violetdb).first_peer(channel) or ""

    def _names(channel: str) -> dict[str, dict[str, Any]]:
        """peer -> {name, status}: the channel's contacts, then the people she met (QQ nicknames)."""
        out: dict[str, dict[str, Any]] = {}
        for r in violetdb.fetch_all("SELECT user_id, nickname FROM social_people WHERE channel = ?", (channel,)):
            out[str(r["user_id"])] = {"name": r["nickname"], "status": None}
        for r in violetdb.fetch_all("SELECT peer_id, display_name, status FROM channel_identities WHERE channel = ?",
                                    (channel,)):
            old = out.get(str(r["peer_id"]), {})
            out[str(r["peer_id"])] = {"name": r["display_name"] or old.get("name"), "status": r["status"]}
        return out

    ROLE_TEXT = {"bound": "主人", "admin": "管理员"}

    def _owner_windows(channel: str) -> list[dict[str, Any]]:
        """2026-10-30 (user: 全都用 QQ 名称和 QQ 号分类): the owner's chat on a channel, one window per bound account
        (messages.peer_id; older rows without one go to the account bound first)."""
        fallback = _fallback_peer(channel)
        rows = violetdb.fetch_all(
            "SELECT COALESCE(peer_id, ?) AS peer, COUNT(*) AS n, MAX(created_at) AS last_at, MAX(id) AS last_id "
            "FROM messages WHERE COALESCE(channel, 'wechat') = ? GROUP BY COALESCE(peer_id, ?)", (fallback, channel, fallback))
        names = _names(channel)
        number = "QQ " if (_channel_labels().get(channel, {}).get("type") or channel) in ("napcat", "onebot11") else ""
        out = []
        for r in rows:
            peer = str(r["peer"] or "")
            info = names.get(peer, {})
            role = ROLE_TEXT.get(info.get("status") or "", "")
            last = violetdb.fetch_one("SELECT content FROM messages WHERE id = ?", (r["last_id"],))
            out.append({"key": "owner:" + peer if peer else "owner", "kind": "owner", "peer": peer,
                        "title": info.get("name") or (number + peer if peer else "主人"),
                        "subtitle": " · ".join(x for x in [(number + peer) if peer else "", role or "主人"] if x),
                        "last_text": (last or {}).get("content"), "last_at": r["last_at"], "count": r["n"]})
        return out

    @app.get("/api/control/history")
    def control_history():
        from ai.social.store import SocialStore
        labels = _channel_labels()
        owner = {r["ch"]: r for r in violetdb.fetch_all(
            "SELECT COALESCE(channel, 'wechat') AS ch, COUNT(*) AS n, MAX(created_at) AS last_at FROM messages GROUP BY ch")}
        social: dict[str, list[dict[str, Any]]] = {}
        for c in SocialStore(violetdb).conversations():
            social.setdefault(c["channel"], []).append(c)
        out = []
        for ch in [*labels, *[c for c in [*owner, *social] if c not in labels]]:
            if ch in [o["id"] for o in out]:
                continue
            own, others = owner.get(ch), social.get(ch, [])
            if not own and not others and ch not in labels:
                continue
            last = max([x for x in [own and own["last_at"], *[c.get("last_message_at") for c in others]] if x], default=None)
            windows = len(_owner_windows(ch)) if own else 0
            out.append({"id": ch, "label": labels.get(ch, {}).get("label", ch), "type": labels.get(ch, {}).get("type", ""),
                        "owner_messages": own["n"] if own else 0, "owner_windows": windows,
                        "conversations": len(others), "last_at": last})
        return ok({"channels": out})

    @app.get("/api/control/history/<channel>")
    def control_history_channel(channel: str):
        from ai.social.store import MODE_TEXT, SocialStore
        items = _owner_windows(channel)
        names = _names(channel)
        for c in SocialStore(violetdb).conversations(channel=channel):
            info = names.get(str(c["peer_id"]), {}) if c["kind"] == "private" else {}
            role = ROLE_TEXT.get(info.get("status") or "", "")
            items.append({"key": str(c["id"]), "kind": c["kind"],
                          "title": c.get("title") or info.get("name") or ("QQ " + c["peer_id"] if c["kind"] == "private"
                                                                          else c["peer_id"]),
                          "subtitle": ("群 " + c["peer_id"] + " · " + MODE_TEXT.get(c["mode"], c["mode"]))
                          if c["kind"] == "group" else " · ".join(x for x in ["QQ " + c["peer_id"], role] if x),
                          "last_text": c.get("last_text"), "last_at": c.get("last_message_at"), "count": c["message_count"]})
        items.sort(key=lambda i: i.get("last_at") or "", reverse=True)
        return ok({"channel": channel, "items": items})

    @app.get("/api/control/history/<channel>/<int:conversation_id>")
    def control_history_social(channel: str, conversation_id: int):
        from ai.social.store import SocialStore
        social = SocialStore(violetdb)
        conv = social.get(conversation_id)
        if conv is None or conv["channel"] != channel:
            return bad("没有这个会话", 404)
        day = request.args.get("date") or ""
        if day and not DATE_RE.match(day):
            return bad("date 的格式是 YYYY-MM-DD")
        limit = _int(request.args.get("limit"), 100, 1, MAX_PAGE)
        rows = social.messages(conversation_id, before_id=_int(request.args.get("before_id"), 0, 0, 2**62), limit=limit,
                               query=(request.args.get("q") or "").strip()[:100], day=day)
        return ok({"conversation": conv, "messages": rows,
                   "next_before_id": rows[0]["id"] if len(rows) == limit else None})

    @app.get("/api/control/messages/export")
    def control_messages_export():
        day_from, day_to = request.args.get("from") or "", request.args.get("to") or ""
        if not (DATE_RE.match(day_from) and DATE_RE.match(day_to)) or day_from > day_to:
            return bad("请给出 from / to（YYYY-MM-DD，from 不晚于 to）")
        end = (datetime.fromisoformat(day_to) + timedelta(days=1)).date().isoformat()
        rows = violetdb.fetch_all("SELECT id, role, message_type, content, created_at FROM messages WHERE created_at >= ? "
                               "AND created_at < ? ORDER BY id LIMIT ?", (day_from, end, EXPORT_LIMIT))
        outbound = _proactive_index(day_from, end)
        for r in rows:
            r["kind"] = _kind(r, outbound)
        body = json.dumps({"exported_at": violetdb.now(), "from": day_from, "to": day_to, "count": len(rows),
                           "truncated": len(rows) >= EXPORT_LIMIT, "messages": rows}, ensure_ascii=False, indent=1)
        return Response(body, mimetype="application/json", headers={
            "Content-Disposition": f'attachment; filename="violet-chat-{day_from}-{day_to}.json"'})

    # ------------------------------------------------------------------ memories
    def _memory(kind: str, memory_id: int) -> dict | None:
        return violetdb.get_core_memory(memory_id) if kind == "core" else violetdb.get_event_memory(memory_id)

    @app.get("/api/control/memories")
    def control_memories():
        kind = request.args.get("kind") or "event"
        if kind not in ("core", "event"):
            return bad("kind 只能是 core 或 event")
        query = (request.args.get("q") or "").strip()[:100]
        status = request.args.get("status") or ""
        limit = _int(request.args.get("limit"), 50, 1, MAX_PAGE)
        offset = _int(request.args.get("offset"), 0, 0, 10**6)
        table = "core_memories" if kind == "core" else "event_memories"
        where, params = [], []
        if query:
            where.append("content LIKE ? ESCAPE '\\'")
            params.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if status and kind == "event":
            if not re.match(r"^[a-z_]{1,20}$", status):
                return bad("status 不对")
            where.append("status = ?")
            params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = violetdb.fetch_one(f"SELECT COUNT(*) AS n FROM {table}{clause}", tuple(params))["n"]
        rows = violetdb.fetch_all(f"SELECT * FROM {table}{clause} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                               (*params, limit, offset))
        from ai.memory_sources import memory_source
        for r in rows:
            r["revisions"] = violetdb.fetch_one("SELECT COUNT(*) AS n FROM memory_revisions WHERE memory_table = ? "
                                             "AND memory_id = ?", (table, r["id"]))["n"]
            if kind == "event":                  # phase 9.1: where it came from (来源未知 when nothing is recorded)
                r["source"] = memory_source(violetdb, violetdb._decode_event_memory_row(dict(r)))
            r["confirmed_by_user"] = violetdb.confirmed_by_user(table, r["id"])
            from ai.memory_consistency import pending_conflicts    # phase 9.1.2: a corrected value still in this text
            r["pending_conflicts"] = [{"old": o, "new": n} for o, n in pending_conflicts(violetdb, table, r["id"], r["content"])]
        statuses = violetdb.fetch_all("SELECT status, COUNT(*) AS n FROM event_memories GROUP BY status") if kind == "event" else []
        return ok({"memories": rows, "total": total, "statuses": statuses})

    @app.get("/api/control/memories/<kind>/<int:memory_id>/history")
    def control_memory_history(kind: str, memory_id: int):
        if kind not in ("core", "event"):
            return bad("not found", 404)
        table = "core_memories" if kind == "core" else "event_memories"
        memory = _memory(kind, memory_id)
        payload = {"memory": memory, "revisions": violetdb.list_memory_revisions(table, memory_id)}
        if kind == "event" and memory:
            from ai.memory_sources import memory_source, source_messages
            payload["source"] = memory_source(violetdb, memory)
            payload["source_messages"] = source_messages(violetdb, payload["source"])
        return ok(payload)

    @app.post("/api/control/memories/<kind>/<int:memory_id>/correct")
    def control_memory_correct(kind: str, memory_id: int):
        body = request.get_json(silent=True) or {}
        content = str(body.get("content") or "").strip()
        reason = str(body.get("reason") or "").strip()[:200] or "管理后台纠正"
        if kind not in ("core", "event"):
            return bad("not found", 404)
        if not content or len(content) > 1000:
            return bad("内容不能为空，也不能超过 1000 字")
        current = _memory(kind, memory_id)
        if not current:
            return bad("not found", 404)
        if content == current.get("content"):
            return bad("内容没有变化")
        if kind == "event":
            if current.get("status") == "deleted":
                return bad("这条记忆已删除，不能再纠正", 409)
            with violetdb.transaction():                    # the change and its revision commit together
                updated = violetdb.apply_event_update(memory_id, content=content, reason=reason, source="dashboard",
                                                   action="correct")
        else:
            with violetdb.transaction():
                violetdb.add_memory_revision("core_memories", memory_id, "correct", old_content=current["content"],
                                          new_content=content, reason=reason, source="dashboard")
                violetdb.execute("UPDATE core_memories SET content = ?, updated_at = ? WHERE id = ?",
                              (content, violetdb.now(), memory_id))
            updated = violetdb.get_core_memory(memory_id)
        return ok(updated)

    @app.post("/api/control/memories/<kind>/<int:memory_id>/delete")
    def control_memory_delete(kind: str, memory_id: int):
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("删除需要确认（confirm: true）")
        reason = str(body.get("reason") or "").strip()[:200] or "管理后台删除"
        if kind not in ("core", "event"):
            return bad("not found", 404)
        current = _memory(kind, memory_id)
        if not current:
            return bad("not found", 404)
        if kind == "event":
            if current.get("status") == "deleted":
                return bad("这条记忆已经删除了", 409)
            with violetdb.transaction():
                retracted = violetdb.retract_event_memory(memory_id, reason=reason, source="dashboard")
            return ok(retracted)
        # core ids are AUTOINCREMENT (never reused), so a profile fact imported from it ("core:<id>") cannot end up
        # pointing at another memory; that profile entry is a separate record and is reported, not deleted here.
        with violetdb.transaction():                        # the old text is kept only if the delete happens, and vice versa
            violetdb.add_memory_revision("core_memories", memory_id, "delete", old_content=current["content"],
                                      new_content=None, reason=reason, source="dashboard")
            violetdb.delete_core_memory(memory_id)
        linked = violetdb.fetch_one("SELECT COUNT(*) AS n FROM user_profile_facts WHERE source_ref = ?",
                                 (f"core:{memory_id}",)) if _has_table("user_profile_facts") else {"n": 0}
        return ok({"deleted": memory_id, "kept_in_revisions": True, "linked_profile_entries": linked["n"]})

    @app.post("/api/control/memories/event/<int:memory_id>/forget")
    def control_memory_forget(memory_id: int):
        """Phase 9.1.1: forget = the memory AND the user's words about it in the turn it came from (ai/memory_forget.py).
        Delete (above) keeps the chat history; this does not. Irreversible, so it needs confirm: true."""
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("遗忘需要确认（confirm: true）")
        if not violetdb.get_event_memory(memory_id):
            return bad("not found", 404)
        from ai.memory_forget import forget_event_memory
        return ok(forget_event_memory(violetdb, memory_id, actor="dashboard"))

    # ------------------------------------------------------------------ tools (phase 9.2)
    def _tools_applied() -> None:
        """Agents keep the server list in their config: reload them so a change acts on the next call."""
        if reload_all is not None:
            reload_all()
            return
        for agent in all_agents or []:
            try:
                agent.reload()
            except Exception:
                pass

    def _known_server(name: str) -> dict | None:
        from ai.chat import get_mcp_servers
        return next((s for s in get_mcp_servers() if isinstance(s, dict) and s.get("name") == name), None)

    @app.get("/api/control/tools")
    def control_tools():
        from ai.tool_manager import Catalog, describe, tool_store, web_status
        return ok({"servers": describe(Catalog(configured=True)), "history": tool_store().history(limit=20),
                   "web": web_status(violetdb)})

    @app.post("/api/control/tools/web/settings")
    def control_tools_web_settings():
        """Phase 9.3: result count and daily caps of the web tools (the provider and network policy stay in .env)."""
        from ai.tool_manager import tool_store, web_status
        body = request.get_json(silent=True) or {}
        if not body:
            return bad("没有要保存的修改")
        try:
            saved = tool_store().save_web_settings(body, actor())
        except ValueError as exc:
            return bad("保存失败，没有改动", problems=str(exc))
        return ok({"saved": saved, "web": web_status(violetdb)})

    @app.get("/api/control/tools/web/searxng")
    def control_tools_web_searxng_state():
        """0.7.1: the one-click private SearXNG -- installed?, the root helper, the last task, web search's settings."""
        from ai.root_tasks import available, read_progress, searxng_export
        from ai.settings import env_bool, env_str
        export = searxng_export()
        url = env_str("VIOLET_WEB_SEARXNG_URL", "")
        from tools.violet_searxng import CN_ENGINES, ENGINES
        from ai.web.providers import provider_name
        return ok({"installed": export is not None, "url": export and export.get("url"), "available": available(),
                   "provider": provider_name(),
                   "engines_text": export and export.get("engines_text"),
                   "engines": export.get("engines", "default") if export else None,
                   "ipv4": bool(export and export.get("ipv4")), "last_note": export and export.get("last_note"),
                   "catalog": [{"name": k, "label": v[0], "region": v[1]} for k, v in ENGINES.items()],
                   "cn_engines": list(CN_ENGINES),
                   "progress": read_progress("task"), "web_enabled": env_bool("VIOLET_WEB_ENABLED", False),
                   "in_use": bool(export) and url.rstrip("/") == str(export.get("url")).rstrip("/")
                   and env_str("VIOLET_WEB_SEARCH_PROVIDER", "searxng") == "searxng"})

    @app.post("/api/control/tools/web/searxng")
    def control_tools_web_searxng():
        """0.7.1 {action, confirm: true}: install / uninstall -> the root task service (progress: /api/control/progress/
        task); enable -> web search on with the URL root exported (never a URL from the request); disable -> off."""
        from ai.root_tasks import request_task, searxng_export
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("需要确认（confirm: true）")
        action = str(body.get("action") or "")
        if action == "configure":
            # the page's own choice: engine names from the fixed list only, and IPv4 only or not
            from ai.root_tasks import control_dir
            from tools.violet_searxng import ENGINES
            names = body.get("engines")
            if names != "default":
                if not isinstance(names, list) or not names or any(n not in ENGINES for n in names):
                    return bad("请至少选一个搜索引擎（只能从列表里选）")
                names = list(dict.fromkeys(names))
            if searxng_export() is None:
                return bad("还没有安装联网搜索（SearXNG）", 409)
            path = control_dir() / "searxng-request.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"engines": names, "ipv4": body.get("ipv4") is True}), encoding="utf-8")
            import os as _os
            _os.replace(tmp, path)
            try:
                return ok(request_task("searxng-configure"))
            except (PermissionError, RuntimeError) as exc:
                path.unlink(missing_ok=True)
                return bad(str(exc), 409)
        if action in ("install", "uninstall", "tune"):
            try:
                return ok(request_task("searxng-" + action))
            except (PermissionError, RuntimeError) as exc:
                return bad(str(exc), 409)
        if action == "enable":
            export = searxng_export()
            if export is None:
                return bad("还没有安装联网搜索（SearXNG）", 409)
            changes = {"VIOLET_WEB_ENABLED": True, "VIOLET_WEB_SEARCH_PROVIDER": "searxng",
                       "VIOLET_WEB_SEARXNG_URL": export["url"]}
            reason = "一键安装联网搜索：启用"
        elif action == "disable":
            # SearXNG removed: web search goes off only when it was the search service (0.7.4: not when Bing is)
            from ai.web.providers import provider_name
            changes = {"VIOLET_WEB_SEARXNG_URL": None}
            if provider_name() == "searxng":
                changes["VIOLET_WEB_ENABLED"] = False
            reason = "卸载 SearXNG"
        elif action == "bing":
            changes = {"VIOLET_WEB_ENABLED": True, "VIOLET_WEB_SEARCH_PROVIDER": "bing"}
            reason = "联网搜索：改用必应直连"
        else:
            return bad("不支持的操作")
        try:
            result = store().save(changes, actor(), reason, apply=apply_now)
        except ValidationError as exc:
            return bad("保存失败，没有改动任何设置", problems=exc.problems)
        except StoreError as exc:
            return bad(str(exc), 500)
        return ok(result)

    @app.post("/api/control/tools/web/test")
    def control_tools_web_test():
        """0.7.2 "测试联网": one search through the search service Violet really uses (same address checks and
        timeout), the engines that did not answer (a SearXNG on this machine), and one public page through the safe
        fetcher (what "网页阅读" uses). Not counted against the daily limits; nothing is stored."""
        import time as _time
        from ai.settings import env_str
        from ai.web.netguard import SafeFetcher, WebPolicyError
        from ai.web.providers import SearchError, make_provider, provider_name
        out: dict[str, Any] = {"provider": provider_name()}
        query = "今天天气"
        started = _time.monotonic()
        try:
            results = make_provider(SafeFetcher()).search(query, limit=5)
            out["search"] = {"ok": bool(results), "count": len(results), "titles": [r.title for r in results[:3]],
                             "error": None if results else "没有搜到结果"}
        except SearchError as exc:
            out["search"] = {"ok": False, "count": 0, "titles": [], "error": f"{exc.code}：{exc}"}
        out["search"]["latency_ms"] = int((_time.monotonic() - started) * 1000)
        url = env_str("VIOLET_WEB_SEARXNG_URL", "").rstrip("/")
        if out["provider"] == "searxng" and re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}", url):
            # SearXNG on this machine: which engine answered how much, and why the others did not (0.7.3)
            from tools.violet_searxng import test_search
            direct = test_search(int(url.rsplit(":", 1)[1]), query=query)
            out["dead_engines"] = direct["dead"][:12] if direct["answered"] else None
            out["by_engine"] = direct["by_engine"]
        started = _time.monotonic()
        page = "https://www.bing.com/"
        try:
            fetched = SafeFetcher().fetch(page)
            out["fetch"] = {"ok": bool(fetched.text()), "url": page, "error": None}
        except WebPolicyError as exc:
            out["fetch"] = {"ok": False, "url": page, "error": f"{exc.code}：{exc}"[:200]}
        out["fetch"]["latency_ms"] = int((_time.monotonic() - started) * 1000)
        return ok(out)

    @app.post("/api/control/tools/web/secret")
    def control_tools_web_secret():
        """Phase 9.3: replace (or remove with value null) the search provider credential. Write-only."""
        from ai.tool_manager import tool_store
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("替换凭据需要确认（confirm: true）")
        try:
            return ok(tool_store().replace_web_key(body.get("value"), actor()))
        except ValueError as exc:
            return bad(str(exc))

    @app.post("/api/control/tools/refresh")
    def control_tools_refresh():
        """Check the servers now (forget cached tool lists and open circuits, connect again)."""
        from ai.tool_manager import describe, refresh
        body = request.get_json(silent=True) or {}
        name = body.get("server")
        if name is not None and _known_server(str(name)) is None:
            return bad("not found", 404)
        catalog = refresh(str(name) if name else None)
        return ok({"servers": describe(catalog)})

    @app.post("/api/control/tools/servers/<name>")
    def control_tools_server(name: str):
        from ai.tool_manager import tool_store
        if _known_server(name) is None:
            return bad("not found", 404)
        body = request.get_json(silent=True) or {}
        changes = {k: body[k] for k in ("enabled", "agents", "connect_timeout", "call_timeout", "offer") if k in body}
        if not changes:
            return bad("没有要保存的修改")
        try:
            saved = tool_store().save_server(name, changes, actor())
        except ValueError as exc:
            return bad("保存失败，没有改动", problems=str(exc))
        _tools_applied()
        return ok({"server": name, "saved": saved})

    @app.post("/api/control/tools/servers/<name>/tools/<tool>")
    def control_tools_tool(name: str, tool: str):
        from ai.tool_manager import tool_store
        if _known_server(name) is None:
            return bad("not found", 404)
        body = request.get_json(silent=True) or {}
        if not isinstance(body.get("enabled"), bool):
            return bad("enabled 要用 true / false")
        try:
            saved = tool_store().set_tool(name, tool, body["enabled"], actor())
        except ValueError as exc:
            return bad(str(exc))
        _tools_applied()
        return ok({"server": name, "tool": tool, "enabled": body["enabled"], "saved": saved})

    @app.post("/api/control/tools/servers/<name>/secret")
    def control_tools_secret(name: str):
        """Replace (or remove with value null) a credential header. Write-only: the value is never shown again."""
        from ai.tool_manager import tool_store
        server = _known_server(name)
        if server is None:
            return bad("not found", 404)
        if server.get("transport") in ("inprocess", "stdio"):
            return bad("这个服务器不用请求头凭据")
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("替换凭据需要确认（confirm: true）")
        try:
            result = tool_store().replace_secret(name, str(body.get("header") or ""), body.get("value"), actor())
        except ValueError as exc:
            return bad(str(exc))
        _tools_applied()
        return ok(result)

    def _has_table(name: str) -> bool:
        return bool(violetdb.fetch_one("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)))

    # ------------------------------------------------------------------ settings
    def runtime_values() -> dict[str, Any]:
        """What the running objects actually use (phase 9.0.1), so the page never shows a value Violet is not using."""
        from ai.control_config import SETTINGS
        out: dict[str, Any] = {}
        for agent in all_agents or []:
            cfg, key = getattr(agent, "_config", None), str(getattr(agent, "model_env_key", "") or "")
            if cfg is None or not key.endswith("_LLM_MODEL"):
                continue
            role = key[: -len("_LLM_MODEL")]
            out[f"{role}_LLM_MODEL"] = cfg.reply_model
            out[f"{role}_LLM_FALLBACK_MODEL"] = cfg.fallback_model or ""
            out[f"{role}_LLM_MODEL_TEMPERATURE"] = cfg.reply_temperature
            out.setdefault("LLM_API_URL", cfg.api_url)
        proactive = _proactive_config()
        if proactive is not None:
            for s in SETTINGS:
                if s.target != "proactive":
                    continue
                node: Any = proactive
                for part in s.path:
                    node = node.get(part) if isinstance(node, dict) else None
                if node is not None:
                    out[s.key] = node
        return out

    def _proactive_config() -> dict | None:
        try:
            if proactive_scheduler is not None:
                return dict(proactive_scheduler.load_config())
            from ai.proactive_scheduler import DEFAULT_CONFIG, DEFAULT_CONFIG_PATH, deep_merge, load_json
            return deep_merge(load_json(DEFAULT_CONFIG_PATH, DEFAULT_CONFIG), store().proactive_overrides())
        except Exception:
            return None

    def _running_prompts() -> dict[str, str]:
        """Which prompt version file the Chat / Proactive agents are actually running ("" = a .env text that matches
        no version file)."""
        from ai.control_config import PROMPT_DIRS, prompt_versions
        out = {}
        for agent in all_agents or []:
            cfg, key = getattr(agent, "_config", None), str(getattr(agent, "model_env_key", "") or "")
            kind = {"CHAT_LLM_MODEL": "chat", "PROACTIVE_LLM_MODEL": "proactive"}.get(key)
            if cfg is None or kind is None:
                continue
            text = (cfg.reply_prompt or "").strip()
            out[f"prompt.{kind}"] = next(
                (v for v in prompt_versions(kind)
                 if (PROMPT_DIRS[kind] / f"{kind}.{v}.txt").read_text(encoding="utf-8").strip() == text), "")
        return out

    def described() -> list[dict]:
        rows = store().describe(runtime_values())
        running = _running_prompts()
        for row in rows:
            if row["key"] in running:
                row["running_version"] = running[row["key"]]
        return rows

    @app.get("/api/control/settings")
    def control_settings():
        return ok({"groups": GROUPS, "settings": described(), "revision": store().revision()})

    @app.put("/api/control/settings")
    def control_settings_save():
        body = request.get_json(silent=True) or {}
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            return bad("没有要保存的修改")
        # typing delays: the lower bound must not pass the upper one (checked against what is effective now)
        from ai.control_config import BY_KEY
        current = {r["key"]: r.get("value") for r in described()}
        lo = changes.get("VIOLET_TYPING_MIN_DELAY_SECONDS", current.get("VIOLET_TYPING_MIN_DELAY_SECONDS"))
        hi = changes.get("VIOLET_TYPING_MAX_DELAY_SECONDS", current.get("VIOLET_TYPING_MAX_DELAY_SECONDS"))
        try:
            if lo not in (None, "") and hi not in (None, "") and float(lo) > float(hi):
                return bad("保存失败，没有改动任何设置", problems={"VIOLET_TYPING_MIN_DELAY_SECONDS": "下限不能大于上限"})
        except (TypeError, ValueError):
            pass
        try:
            result = store().save(changes, actor(), str(body.get("reason") or ""), apply=apply_now)
        except ValidationError as exc:
            return bad("保存失败，没有改动任何设置", problems=exc.problems)
        except StoreError as exc:
            return bad(str(exc), 500)
        except Exception as exc:
            return bad(f"保存失败，已恢复原来的设置（{exc.__class__.__name__}）", 500)
        effects = {k: BY_KEY[k].effect for k in changes if k in BY_KEY}
        return ok({**result, "applied_now": sorted(k for k, e in effects.items() if e == "now"),
                   "needs_restart": sorted(k for k, e in effects.items() if e == "restart")})

    @app.get("/api/control/settings/history")
    def control_settings_history():
        rows = store().history(limit=_int(request.args.get("limit"), 30, 1, 200))
        for r in rows:
            r.pop("snapshot", None)                    # values are in "changes"; secrets only as fingerprints
        return ok(rows)

    @app.post("/api/control/settings/rollback")
    def control_settings_rollback():
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return bad("回滚需要确认（confirm: true）")
        try:
            revision = int(body.get("revision"))
            return ok(store().rollback(revision, actor(), apply=apply_now))
        except (TypeError, ValueError) as exc:
            return bad(str(exc) or "revision 不对")
        except StoreError as exc:
            return bad(str(exc), 500)
        except Exception as exc:
            return bad(f"回滚失败，已恢复原来的设置（{exc.__class__.__name__}）", 500)

    @app.post("/api/control/settings/restore-secret")
    def control_settings_restore_secret():
        body = request.get_json(silent=True) or {}
        key = str(body.get("key") or "")
        if body.get("confirm") is not True:
            return bad("需要确认（confirm: true）")
        try:
            return ok(store().restore_previous_secret(key, actor(), apply=apply_now))
        except ValueError as exc:
            return bad(str(exc))
        except StoreError as exc:
            return bad(str(exc), 500)
        except Exception as exc:
            return bad(f"恢复失败，已恢复原来的设置（{exc.__class__.__name__}）", 500)

    @app.post("/api/control/reset")
    def control_reset():
        """0.7.9 (user: "清除所有记忆和清除所有配置，二选一或都选"). memory: what she remembers (archived first, like a
        persona switch); config: the dashboard's settings back to default (the model service stays) and every QQ group
        / private chat back to its starting mode. Chat history, channels, the persona and the login stay."""
        body = request.get_json(silent=True) or {}
        memory, config = body.get("memory") is True, body.get("config") is True
        cut = body.get("cut") is True            # 2026-10-30 翻篇: she no longer sees the chat before now
        if not memory and not config and not cut:
            return bad("请至少选一项")
        if body.get("confirm") is not True:
            return bad("清除需要确认（confirm: true）")
        out: dict[str, Any] = {}
        if memory:
            from ai.persona_templates import clear_memory
            try:
                archive, counts = clear_memory(violetdb)
            except Exception as exc:
                logger.exception("clearing the memory failed")
                return bad(f"清除记忆失败（{exc.__class__.__name__}），什么都没有删")
            out["memory"] = {"archive": archive, "removed": counts}
        if config:
            try:
                result = store().reset(actor(), apply=apply_now)
            except StoreError as exc:
                return bad(str(exc), 500, **out)
            except Exception as exc:
                return bad(f"清除配置失败，已恢复原来的设置（{exc.__class__.__name__}）", 500, **out)
            violetdb.execute("UPDATE social_conversations SET mode = CASE kind WHEN 'group' THEN 'off' ELSE 'auto' END, "
                             "daily_message_limit = 0, daily_decision_limit = 0, next_wake_at = NULL")
            out["config"] = result
        if cut:
            from ai import context_cut
            out["cut"] = context_cut.cut(violetdb, "all")
        logger.warning("dashboard reset by %s: memory=%s config=%s cut=%s", actor(), memory, config, cut)
        return ok(out)
