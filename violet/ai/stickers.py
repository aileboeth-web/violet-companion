"""Violet's sticker collection (0.7.9, user: "他不会发表情包，也不能收藏表情包").

Where stickers come from:
  * the owner sends one (QQ marks it as a sticker) -- collected automatically (VIOLET_STICKER_AUTO_COLLECT), with the
    description the picture recognition already made for the chat;
  * someone in a group / private chat sends one and she decides to keep it (the social decision's "collect");
  * the admin uploads one, or imports the QQ account's own favourites (NapCat fetch_custom_face), on the 图片 page.

Sending: in the owner's chat she writes "[[表情:N]]" on its own line (ai/chat_output.py); in groups the social decision
names a number. Only stickers of this collection can be sent -- the number is looked up here, never a path or an URL
from the model. Files live in <data>/media/stickers, one per picture (same bytes = same sticker).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from ai.settings import env_bool

logger = logging.getLogger("violet.stickers")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stickers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    file_key TEXT,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    description TEXT,
    source TEXT NOT NULL,
    source_detail TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stickers_file_key ON stickers(file_key);
"""

MAX_BYTES = 5 * 1024 * 1024
SOURCES = {"owner": "你发的", "group": "群里收藏的", "private": "私聊里收藏的", "qq": "QQ 收藏导入", "upload": "后台上传"}
MARKER_RE = re.compile(r"\[\[表情[:：]\s*(\d+)\]\]")
STICKER_PROMPT = ("这是一张聊天用的表情包。用一句中文（25 字以内）说清：画面是什么、表达什么情绪、适合在什么时候发。"
                  "图里有文字就照抄文字。只输出这一句。")
_MAGIC = ((b"\x89PNG", "image/png", ".png"), (b"\xff\xd8", "image/jpeg", ".jpg"), (b"GIF8", "image/gif", ".gif"))


class StickerError(ValueError):
    pass


def enabled() -> bool:
    return env_bool("VIOLET_STICKERS", True)


def folder() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "media" / "stickers"


def marker(sticker_id: int) -> str:
    return f"[[表情:{int(sticker_id)}]]"


def _kind(data: bytes) -> tuple[str, str]:
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise StickerError("不是能用的图片（只收 PNG / JPG / GIF / WEBP）")


def _clean(text: Any, limit: int = 60) -> str | None:
    value = " ".join(str(text or "").split())[:limit]
    return value or None


def add_bytes(db, data: bytes, *, source: str, description: str | None = None, file_key: str | None = None,
              source_detail: str | None = None) -> dict[str, Any]:
    """Store a sticker (the same picture twice is one sticker; a deleted-then-re-added one comes back enabled)."""
    if not data:
        raise StickerError("图片是空的")
    if len(data) > MAX_BYTES:
        raise StickerError("图片太大了（最多 5 MB）")
    mime, ext = _kind(data)
    digest = hashlib.sha256(data).hexdigest()
    row = db.fetch_one("SELECT * FROM stickers WHERE sha256 = ?", (digest,))
    if row is not None:
        updates, params = ["enabled = 1"], []
        if description and not row.get("description"):
            updates.append("description = ?")
            params.append(_clean(description))
        if file_key and not row.get("file_key"):
            updates.append("file_key = ?")
            params.append(file_key[:120])
        db.execute(f"UPDATE stickers SET {', '.join(updates)} WHERE id = ?", (*params, row["id"]))
        return get(db, row["id"])
    target = folder()
    target.mkdir(parents=True, exist_ok=True)
    filename = digest[:32] + ext
    (target / filename).write_bytes(data)
    cursor = db.execute("INSERT INTO stickers (sha256, file_key, filename, mime_type, size, description, source, "
                        "source_detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (digest, (file_key or "")[:120] or None, filename, mime, len(data), _clean(description),
                         source if source in SOURCES else "upload", _clean(source_detail, 80), db.now()))
    return get(db, int(cursor.lastrowid))


def add_file(db, path: str | os.PathLike[str], **kwargs) -> dict[str, Any]:
    return add_bytes(db, Path(path).read_bytes(), **kwargs)


def get(db, sticker_id: Any) -> dict[str, Any] | None:
    try:
        return db.fetch_one("SELECT * FROM stickers WHERE id = ?", (int(sticker_id),))
    except (TypeError, ValueError):
        return None


def by_file_key(db, file_key: str | None) -> dict[str, Any] | None:
    if not file_key:
        return None
    return db.fetch_one("SELECT * FROM stickers WHERE file_key = ? ORDER BY id LIMIT 1", (file_key[:120],))


def path_of(row: dict[str, Any]) -> Path:
    return folder() / Path(str(row["filename"])).name


def sendable(db, sticker_id: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(sticker, None) or (None, why not)."""
    if not enabled():
        return None, "表情包功能关着"
    row = get(db, sticker_id)
    if row is None:
        return None, "没有这个表情包"
    if not row["enabled"]:
        return None, "这个表情包已经停用"
    if not path_of(row).is_file():
        return None, "表情包文件不见了"
    return row, None


def mark_used(db, sticker_id: int) -> None:
    db.execute("UPDATE stickers SET use_count = use_count + 1, last_used_at = ? WHERE id = ?", (db.now(), int(sticker_id)))


def listing(db, enabled_only: bool = False, limit: int = 500) -> list[dict[str, Any]]:
    where = " WHERE enabled = 1" if enabled_only else ""
    return db.fetch_all(f"SELECT * FROM stickers{where} ORDER BY id DESC LIMIT ?", (int(limit),))


def update(db, sticker_id: int, *, description: Any = None, enabled_flag: Any = None) -> dict[str, Any] | None:
    if description is not None:
        db.execute("UPDATE stickers SET description = ? WHERE id = ?", (_clean(description), int(sticker_id)))
    if enabled_flag is not None:
        db.execute("UPDATE stickers SET enabled = ? WHERE id = ?", (1 if enabled_flag else 0, int(sticker_id)))
    return get(db, sticker_id)


def delete(db, sticker_id: int) -> bool:
    row = get(db, sticker_id)
    if row is None:
        return False
    db.execute("DELETE FROM stickers WHERE id = ?", (row["id"],))
    try:
        path_of(row).unlink(missing_ok=True)
    except OSError:
        logger.warning("sticker file %s could not be removed", row["filename"])
    return True


def prompt_lines(db, limit: int = 30) -> list[str]:
    """"编号 N：description" -- the ones she uses most first, then the newest; only described, enabled ones."""
    if not enabled():
        return []
    rows = db.fetch_all("SELECT id, description FROM stickers WHERE enabled = 1 AND description IS NOT NULL "
                        "AND description != '' ORDER BY use_count DESC, id DESC LIMIT ?", (int(limit),))
    return [f"- {r['id']}：{r['description']}" for r in rows]


def describe(path: str | os.PathLike[str]) -> str | None:
    """One line about the sticker with the picture-recognition model; None when it is not available or fails."""
    try:
        from ai.vision import describe_image
        text = describe_image(path, prompt=STICKER_PROMPT)
    except Exception as exc:                                 # noqa: BLE001 -- recognition is optional here
        logger.info("sticker description failed: %s", exc)
        return None
    from ai.web.content import neutralize
    text, _ = neutralize(text)
    return _clean(text.splitlines()[0] if text else "")


def chat_section(db) -> str:
    """For the owner's chat: what she can send and how."""
    lines = prompt_lines(db)
    if not lines:
        return ""
    return ("【你收藏的表情包】聊到合适的时候可以发一个：单独一行写 [[表情:编号]]。不用每次都发，一次最多一个，"
            "只用下面有的编号。\n" + "\n".join(lines))
