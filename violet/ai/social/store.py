"""Tables and queries of Violet's social life (see ai/social/__init__.py). Created with the other tables by
VioletDatabase; every query goes through the given database object (its lock and connection)."""

from __future__ import annotations

from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS social_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('private', 'group')),
    peer_id TEXT NOT NULL,
    title TEXT,
    mode TEXT NOT NULL DEFAULT 'off',
    daily_message_limit INTEGER NOT NULL DEFAULT 0,
    daily_decision_limit INTEGER NOT NULL DEFAULT 0,
    watermark INTEGER NOT NULL DEFAULT 0,
    next_wake_at TEXT,
    notes TEXT,
    member_count INTEGER,
    last_message_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (channel, kind, peer_id)
);
CREATE TABLE IF NOT EXISTS social_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    platform_msg_id TEXT,
    sender_id TEXT,
    sender_name TEXT,
    is_self INTEGER NOT NULL DEFAULT 0,
    message_type TEXT NOT NULL DEFAULT 'text',
    content TEXT,
    reply_to TEXT,
    mentions_self INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_messages_conv ON social_messages(conversation_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_social_messages_platform ON social_messages(conversation_id, platform_msg_id)
    WHERE platform_msg_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS social_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    user_id TEXT NOT NULL,
    nickname TEXT,
    impression TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (channel, user_id)
);
CREATE TABLE IF NOT EXISTS social_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    trigger TEXT,
    action TEXT NOT NULL,
    reply_to TEXT,
    text TEXT,
    wait_minutes REAL,
    reason TEXT,
    model TEXT,
    latency_ms INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_decisions_conv ON social_decisions(conversation_id, id);
"""

GROUP_MODES = ("off", "observe", "mention", "auto")      # 关闭 / 只看不说 / 被 @ 才说 / 自己决定
MODE_TEXT = {"off": "关闭", "observe": "只看不说", "mention": "被 @ 或回复她时才说", "auto": "自己决定"}


class SocialStore:
    def __init__(self, db):
        self.db = db

    # -- conversations ------------------------------------------------------------------------------------------
    def conversation(self, channel: str, kind: str, peer_id: str, title: str | None = None,
                     default_mode: str | None = None) -> dict[str, Any]:
        """The conversation, created on first sight (groups start "off"; private chats "auto")."""
        row = self.db.fetch_one("SELECT * FROM social_conversations WHERE channel = ? AND kind = ? AND peer_id = ?",
                                (channel, kind, str(peer_id)))
        now = self.db.now()
        if row is None:
            mode = default_mode or ("off" if kind == "group" else "auto")
            self.db.execute("INSERT OR IGNORE INTO social_conversations (channel, kind, peer_id, title, mode, created_at, "
                            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (channel, kind, str(peer_id), title, mode, now, now))
            row = self.db.fetch_one("SELECT * FROM social_conversations WHERE channel = ? AND kind = ? AND peer_id = ?",
                                    (channel, kind, str(peer_id)))
        elif title and title != row.get("title"):
            self.db.execute("UPDATE social_conversations SET title = ?, updated_at = ? WHERE id = ?", (title, now, row["id"]))
            row["title"] = title
        return row

    def get(self, conversation_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM social_conversations WHERE id = ?", (int(conversation_id),))

    def update(self, conversation_id: int, **fields: Any) -> None:
        allowed = {"title", "mode", "daily_message_limit", "daily_decision_limit", "watermark", "next_wake_at", "notes",
                   "member_count"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.db.execute(f"UPDATE social_conversations SET {cols}, updated_at = ? WHERE id = ?",
                        (*sets.values(), self.db.now(), int(conversation_id)))

    def conversations(self, channel: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if channel:
            where.append("c.channel = ?")
            params.append(channel)
        if kind:
            where.append("c.kind = ?")
            params.append(kind)
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM social_messages m WHERE m.conversation_id = c.id) AS message_count, "
               "(SELECT content FROM social_messages m WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_text "
               "FROM social_conversations c" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY COALESCE(c.last_message_at, c.created_at) DESC")
        return self.db.fetch_all(sql, params)

    # -- messages -----------------------------------------------------------------------------------------------
    def add_message(self, conversation_id: int, *, content: str, sender_id: str | None = None,
                    sender_name: str | None = None, is_self: bool = False, message_type: str = "text",
                    platform_msg_id: str | None = None, reply_to: str | None = None, mentions_self: bool = False,
                    created_at: str | None = None) -> int | None:
        """The new row id, or None when this platform message was stored before (redelivery after a reconnect)."""
        at = created_at or self.db.now()
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO social_messages (conversation_id, platform_msg_id, sender_id, sender_name, is_self, "
            "message_type, content, reply_to, mentions_self, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(conversation_id), platform_msg_id, sender_id, sender_name, 1 if is_self else 0, message_type, content,
             reply_to, 1 if mentions_self else 0, at))
        if not cursor.rowcount:
            return None
        self.db.execute("UPDATE social_conversations SET last_message_at = ?, updated_at = ? WHERE id = ?",
                        (at, self.db.now(), int(conversation_id)))
        return int(cursor.lastrowid)

    def messages(self, conversation_id: int, *, after_id: int = 0, before_id: int = 0, limit: int = 50,
                 query: str = "", day: str = "") -> list[dict[str, Any]]:
        """Oldest first. after_id: the unread part; before_id: older pages."""
        where, params = ["conversation_id = ?"], [int(conversation_id)]
        if after_id:
            where.append("id > ?")
            params.append(int(after_id))
        if before_id:
            where.append("id < ?")
            params.append(int(before_id))
        if query:
            where.append("content LIKE ? ESCAPE '\\'")
            params.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if day:
            where.append("substr(created_at, 1, 10) = ?")
            params.append(day)
        order = "ASC" if after_id and not before_id else "DESC"
        rows = self.db.fetch_all(f"SELECT * FROM social_messages WHERE {' AND '.join(where)} ORDER BY id {order} LIMIT ?",
                                 (*params, int(limit)))
        return rows if order == "ASC" else list(reversed(rows))

    def find_message(self, conversation_id: int, platform_msg_id: str) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM social_messages WHERE conversation_id = ? AND platform_msg_id = ?",
                                 (int(conversation_id), str(platform_msg_id)))

    def unread(self, conversation: dict[str, Any], limit: int = 60) -> list[dict[str, Any]]:
        """Messages of others after the watermark (what she has not looked at yet)."""
        return [m for m in self.messages(conversation["id"], after_id=int(conversation.get("watermark") or 0), limit=limit)
                if not m["is_self"]]

    def advance(self, conversation_id: int, upto_id: int) -> None:
        self.db.execute("UPDATE social_conversations SET watermark = MAX(watermark, ?), updated_at = ? WHERE id = ?",
                        (int(upto_id), self.db.now(), int(conversation_id)))

    # -- people ---------------------------------------------------------------------------------------------------
    def person(self, channel: str, user_id: str, nickname: str | None = None, seen: bool = False) -> dict[str, Any]:
        now = self.db.now()
        self.db.execute("INSERT OR IGNORE INTO social_people (channel, user_id, nickname, first_seen_at, last_seen_at) "
                        "VALUES (?, ?, ?, ?, ?)", (channel, str(user_id), nickname, now, now))
        if seen:
            self.db.execute("UPDATE social_people SET last_seen_at = ?, message_count = message_count + 1, "
                            "nickname = COALESCE(?, nickname) WHERE channel = ? AND user_id = ?",
                            (now, nickname, channel, str(user_id)))
        elif nickname:
            self.db.execute("UPDATE social_people SET nickname = ? WHERE channel = ? AND user_id = ?",
                            (nickname, channel, str(user_id)))
        return self.db.fetch_one("SELECT * FROM social_people WHERE channel = ? AND user_id = ?", (channel, str(user_id)))

    def people(self, channel: str | None = None, ids: list[str] | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if channel:
            where.append("channel = ?")
            params.append(channel)
        if ids:
            where.append("user_id IN (" + ",".join("?" * len(ids)) + ")")
            params += [str(i) for i in ids]
        return self.db.fetch_all("SELECT * FROM social_people" + (" WHERE " + " AND ".join(where) if where else "")
                                 + " ORDER BY last_seen_at DESC", params)

    def set_impression(self, channel: str, user_id: str, impression: str) -> None:
        self.db.execute("UPDATE social_people SET impression = ? WHERE channel = ? AND user_id = ?",
                        (impression, channel, str(user_id)))

    # -- decisions ------------------------------------------------------------------------------------------------
    def record_decision(self, conversation_id: int, **fields: Any) -> int:
        cols = ["trigger", "action", "reply_to", "text", "wait_minutes", "reason", "model", "latency_ms", "error"]
        values = [fields.get(c) for c in cols]
        cursor = self.db.execute(
            f"INSERT INTO social_decisions (conversation_id, {', '.join(cols)}, created_at) VALUES (?, {', '.join('?' * len(cols))}, ?)",
            (int(conversation_id), *values, self.db.now()))
        return int(cursor.lastrowid)

    def decisions(self, conversation_id: int, limit: int = 30) -> list[dict[str, Any]]:
        return self.db.fetch_all("SELECT * FROM social_decisions WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                                 (int(conversation_id), int(limit)))

    def today(self, conversation_id: int) -> dict[str, int]:
        """Model decisions and her own messages since midnight (the daily limits)."""
        day = self.db.now()[:10]
        decided = self.db.fetch_one("SELECT COUNT(*) AS n FROM social_decisions WHERE conversation_id = ? AND created_at >= ?",
                                    (int(conversation_id), day))["n"]
        said = self.db.fetch_one("SELECT COUNT(*) AS n FROM social_messages WHERE conversation_id = ? AND is_self = 1 "
                                 "AND created_at >= ?", (int(conversation_id), day))["n"]
        return {"decisions": int(decided), "messages": int(said)}
