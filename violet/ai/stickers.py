"""Violet's sticker collection (0.7.9, user: "他不会发表情包，也不能收藏表情包").

Where stickers come from:
  * the owner sends one (QQ marks it as a sticker) -- collected automatically (VIOLET_STICKER_AUTO_COLLECT), with the
    description the picture recognition already made for the chat;
  * someone in a group / private chat sends one and she decides to keep it (the social decision's "collect");
  * the admin uploads one, or imports the QQ account's own favourites (SnowLuma fetch_custom_face), on the 图片 page;
  * 2026-10-30: those favourites also come in by themselves after QQ connects and every 6 hours (sync_qq).
Only stickers with a description are offered to the model, so missing descriptions are filled a batch at a time.

Sending: in the owner's chat she writes "[[表情:N]]" on its own line (ai/chat_output.py); in groups the social decision
names a number. Only stickers of this collection can be sent -- the number is looked up here, never a path or an URL
from the model. Files live in <data>/media/stickers, one per picture (same bytes = same sticker).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
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
-- 2026-10-30: a deleted sticker is not collected again (the admin's own upload still works)
CREATE TABLE IF NOT EXISTS sticker_blocklist (
    sha256 TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""

MAX_BYTES = 5 * 1024 * 1024
SOURCES = {"owner": "你发的", "group": "群里收藏的", "private": "私聊里收藏的", "qq": "QQ 收藏导入", "upload": "后台上传"}
MARKER_RE = re.compile(r"\[\[表情[:：]\s*(\d+)\]\]")
# 2026-10-30 (from dsh-sticker-library): three short parts -- what it shows (text in it copied exactly, the meaning
# often IS the text), when to send it, keywords -- and a verdict, so vulgar or low-quality ones are kept out at intake
STICKER_PROMPT = ("这是一张聊天用的表情包。只输出四行，不要别的：\n"
                  "画面：画面里是什么（图里有文字就一字不差地照抄）\n"
                  "什么时候发：适合在聊天的什么情况下发，表达什么情绪\n"
                  "关键词：3 到 5 个词，用顿号隔开\n"
                  "判断：正常 / 脏（脏话、性暗示、低俗、恶心、血腥）/ 烂（看不清、截图或普通照片、没有意义的烂梗）")
DESCRIPTION_LIMIT = 120
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


def _clean(text: Any, limit: int = DESCRIPTION_LIMIT) -> str | None:
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
    if db.fetch_one("SELECT 1 FROM sticker_blocklist WHERE sha256 = ?", (digest,)):
        if source != "upload":
            raise StickerError("这个表情包删过，不再收藏")
        db.execute("DELETE FROM sticker_blocklist WHERE sha256 = ?", (digest,))     # the admin wants it back
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
    db.execute("INSERT OR IGNORE INTO sticker_blocklist (sha256, created_at) VALUES (?, ?)", (row["sha256"], db.now()))
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
                        "AND description != '' AND description NOT LIKE '【不雅】%' AND description NOT LIKE '【质量差】%' "
                        "ORDER BY use_count DESC, id DESC LIMIT ?", (int(limit),))
    return [f"- {r['id']}：{r['description']}" for r in rows]


BAD_MARKS = {"dirty": "【不雅】", "junk": "【质量差】"}


def verdict_of(description: str | None) -> str:
    """"ok" / "dirty" / "junk": describe() puts the mark in front of a sticker that should not be used."""
    for kind, mark in BAD_MARKS.items():
        if (description or "").startswith(mark):
            return kind
    return "ok"


def parse_description(text: str) -> tuple[str | None, str]:
    """The recognition's answer -> ("画面…；什么时候发…；关键词…", verdict "ok" / "dirty" / "junk"). An answer in the old
    one-line form is kept as it is (verdict ok)."""
    parts: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-* ")
        for key in ("画面", "什么时候发", "关键词", "判断"):
            for colon in ("：", ":"):
                if line.startswith(key + colon):
                    parts[key] = line[len(key) + 1:].strip()
    if not parts:
        first = (text or "").strip().splitlines()
        return _clean(first[0] if first else ""), "ok"
    verdict = parts.get("判断", "")
    kind = "dirty" if verdict.startswith("脏") else "junk" if verdict.startswith("烂") else "ok"
    body = "；".join(f"{k}：{parts[k]}" for k in ("画面", "什么时候发", "关键词") if parts.get(k))
    return _clean(body), kind


LABEL_WORDS = {"开心": ("开心", "高兴", "笑", "嘿嘿", "好耶"), "卖萌": ("卖萌", "可爱", "撒娇", "萌"),
               "生气": ("生气", "愤怒", "凶", "哼"), "委屈": ("委屈", "难过", "哭", "伤心"),
               "无语": ("无语", "翻白眼", "离谱"), "嘲讽": ("嘲讽", "嫌弃", "得意", "鄙视"),
               "疑惑": ("疑惑", "问号", "不懂", "思考"), "早晚安": ("早安", "晚安", "睡", "困")}


def by_label(db, label: str) -> dict[str, Any] | None:
    """2026-10-31: a usable sticker for a mood label (<sticker:开心> in the Aurabot persona), or None: the one whose
    description matches the label's words best, then the one she used most."""
    if not enabled():
        return None
    words = LABEL_WORDS.get(label.strip(), (label.strip(),))
    best, score = None, 0
    for row in db.fetch_all("SELECT * FROM stickers WHERE enabled = 1 AND description IS NOT NULL AND description != '' "
                            "AND description NOT LIKE '【不雅】%' AND description NOT LIKE '【质量差】%' "
                            "ORDER BY use_count DESC, id DESC LIMIT 300"):
        hit = sum(1 for w in words if w and w in row["description"])
        if hit > score:
            best, score = row, hit
    if best is not None and not path_of(best).is_file():
        return None
    return best


def describe(path: str | os.PathLike[str]) -> str | None:
    """About the sticker, with the picture-recognition model; None when it is not available or fails. A vulgar or
    low-quality one starts with 【不雅】 / 【质量差】 (verdict_of)."""
    try:
        from ai.vision import describe_image
        text = describe_image(path, prompt=STICKER_PROMPT)
    except Exception as exc:                                 # noqa: BLE001 -- recognition is optional here
        logger.info("sticker description failed: %s", exc)
        return None
    from ai.web.content import neutralize
    text, _ = neutralize(text)
    description, verdict = parse_description(text or "")
    if description and verdict != "ok":
        description = _clean(BAD_MARKS[verdict] + description)
    return description


QQ_SYNC_SECONDS = 6 * 3600
DESCRIBE_PER_RUN = 24
_synced: dict[str, float] = {}
_sync_lock = threading.Lock()


def describe_pending(db, limit: int = DESCRIBE_PER_RUN) -> int:
    """Stickers without a description get one (only described stickers are offered to the model). Returns how many."""
    done = 0
    for row in db.fetch_all("SELECT * FROM stickers WHERE enabled = 1 AND (description IS NULL OR description = '') "
                            "ORDER BY id LIMIT ?", (int(limit),)):
        if not path_of(row).is_file():
            continue
        text = describe(path_of(row))
        verdict = verdict_of(text)
        if not text:
            break                                            # recognition not available now: try again next time
        # vulgar / low-quality ones stay in the list (the 图片 page shows them) but are switched off; the admin decides
        update(db, row["id"], description=text, enabled_flag=None if verdict == "ok" else False)
        if verdict != "ok":
            from ai import trace
            trace.step("表情包没启用", 编号=row["id"], 原因="不雅" if verdict == "dirty" else "质量差", 说明=text)
        done += 1
    return done


def import_qq(db, adapter, describe_limit: int = DESCRIBE_PER_RUN, count: int = 200) -> dict[str, int]:
    """The QQ account's own favourite stickers (QQ's 收藏的表情) into the collection. One already imported (same
    address without its query, or the same picture) is not downloaded again."""
    from ai.channels.base import Part
    urls = adapter.favourite_stickers(count)
    added = failed = known = 0
    for url in urls[:count]:
        key = "qq:" + hashlib.sha256(url.split("?", 1)[0].encode("utf-8")).hexdigest()[:40]
        if by_file_key(db, key) is not None:
            known += 1
            continue
        try:
            before = db.fetch_one("SELECT COUNT(*) AS n FROM stickers")["n"]
            add_file(db, adapter.download(Part("image", "", {"url": url})), source="qq", file_key=key)
            if db.fetch_one("SELECT COUNT(*) AS n FROM stickers")["n"] > before:
                added += 1
            else:
                known += 1
        except Exception:                                     # noqa: BLE001 -- one bad picture does not stop the rest
            failed += 1
    described = describe_pending(db, describe_limit) if describe_limit else 0
    return {"found": len(urls), "added": added, "known": known, "failed": failed, "described": described}


def sync_qq(db, adapter, force: bool = False, clock=time.time) -> dict[str, int] | None:
    """2026-10-30 (user: she could not use the stickers the QQ account had already collected): after QQ connects and
    then every 6 hours, the favourites come into her collection and get described a batch at a time
    (VIOLET_STICKER_QQ_SYNC). None when it is not time yet, switched off, or QQ did not answer. Never raises."""
    if not enabled() or not env_bool("VIOLET_STICKER_QQ_SYNC", True):
        return None
    key = str(getattr(adapter, "channel_id", "qq"))
    with _sync_lock:
        if not force and key in _synced and clock() - _synced[key] < QQ_SYNC_SECONDS:
            return None
        _synced[key] = clock()
    try:
        result = import_qq(db, adapter)
    except Exception as exc:                                  # noqa: BLE001 -- QQ may be busy; next round tries again
        logger.info("QQ favourite stickers not synced: %s", exc)
        return None
    from ai import trace
    trace.step("QQ 收藏的表情", 找到=result["found"], 新增=result["added"], 补上说明=result["described"],
               失败=result["failed"])
    return result


def chat_section(db) -> str:
    """For the owner's chat: what she can send and how."""
    lines = prompt_lines(db)
    if not lines:
        return ""
    # 2026-10-30: sending is the send_sticker tool (ai/sticker_tools.py); the marker line still works
    return ("【你收藏的表情包】聊到合适的时候可以发一个：调用 send_sticker 填编号（或单独一行写 [[表情:编号]]）。"
            "不用每次都发，一次最多一个；按说明里“什么时候发”挑，没有真正合适的就不发，不要硬配一个意思不对的；表情包只是配语气，不能代替回答。"
            "下面是常用的，全部的用 find_stickers 看。\n" + "\n".join(lines))
