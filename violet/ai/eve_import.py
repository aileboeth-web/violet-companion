"""Bring Project Eve's data into Violet (2026-11-02, user: 之前有些聊天记录在 Eve，想导进来，可以选聊天渠道；
Eve 的配置文件里手动改过发送频率，也要拿过来).

The user packs the Eve server's data (``eve-export.tar.gz``: Memories.db copied with SQLite's backup API,
proactive_config.json, and env-safe.txt = the .env lines without keys, tokens, passwords, prompts or addresses) and
uploads it on the Control Center's "从 Eve 导入" page. A bare Memories.db or a .zip works too.

Two steps, like the timetable import:
  stage()   saves the upload under <data>/import/<token>/, keeps only the files above (a full .env is read for the
            allowed keys and deleted at once), and returns the preview: counts only, never message text.
  commit()  imports the chosen parts. Messages, core and event memories go in ONE database transaction; the
            settings go through the control store (validated, recorded in 修改记录, can be rolled back).

Messages: Eve's chat is OLDER than anything Violet has, and much of Violet orders the conversation by id. The
imported rows therefore get ids BELOW every existing one (0, -1, -2, ...; SQLite allows that), in time order, so
"newest = highest id" stays true everywhere. They are put on the chosen channel, as the account bound first there.
A row already present (same channel, time, side and text) is skipped, so importing the same file twice adds nothing.
Core memories whose key Violet already has are kept as Violet has them. Event memories keep their links to the
imported messages (source_message_ids are renumbered).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, BinaryIO

logger = logging.getLogger("violet.eve_import")

MAX_UPLOAD = 1024 * 1024 * 1024          # 1 GB: a years-long chat database stays far below
KEEP = {"Memories.db": "db", "proactive_config.json": "proactive", "env-safe.txt": "env", ".env": "env"}
STAGE_TTL = 24 * 3600
SECRET_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PROMPT|URL|MQTT", re.I)
MESSAGE_TYPES = {"text", "image", "file", "voice", "video"}
EVENT_COLUMNS = ("content", "category", "happened_at", "valid_until", "status", "importance", "emotional_weight",
                 "recall_count", "mention_count", "forget_weight", "source_message_ids", "last_recalled_at",
                 "created_at", "updated_at")
CORE_COLUMNS = ("memory_key", "category", "content", "priority", "confidence", "source", "mention_count",
                "created_at", "updated_at")
LEDGER = """
CREATE TABLE IF NOT EXISTS eve_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_sha256 TEXT NOT NULL,
    channel TEXT,
    result TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL
);
"""
# Eve's proactive_config.json keys Violet has no setting for (shown in the preview as "not imported")
PROACTIVE_UNMAPPED = {
    "random_delay_minutes": "每次主动联系前随机等的时间：Violet 用的是同一套默认值（早上 45–120 分钟……），没有单独的设置",
    "check_interval_seconds": "多久检查一次：Violet 固定每分钟检查",
    "allowed_tasks": "主动任务（麦当劳、高德天气）：这些工具在 Violet 里已经去掉了",
}


class ImportError_(ValueError):
    """Something the user can fix (wrong file, expired upload); the message is shown on the page."""


def _base() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "import"


def _dir(token: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{24}", token or ""):
        raise ImportError_("上传编号不对，请重新上传")
    path = _base() / token
    if not path.is_dir():
        raise ImportError_("这个上传已经过期或已经导入过了，请重新上传")
    return path


def _cleanup_old() -> None:
    base = _base()
    if not base.is_dir():
        return
    for child in base.iterdir():
        try:
            if child.is_dir() and time.time() - child.stat().st_mtime > STAGE_TTL:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


# ------------------------------------------------------------------ stage
def stage(stream: BinaryIO, filename: str = "") -> str:
    """Save the upload and keep the Eve files from it; the token for preview() / commit()."""
    _cleanup_old()
    token = secrets.token_hex(12)
    folder = _base() / token
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / "upload.bin"
    digest = hashlib.sha256()
    size = 0
    try:
        with raw.open("wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise ImportError_("文件太大（超过 1 GB）")
                digest.update(chunk)
                out.write(chunk)
        if not size:
            raise ImportError_("没有收到文件")
        _unpack(raw, folder, filename)
        if not (folder / "Memories.db").exists() and not (folder / "proactive_config.json").exists() \
                and not (folder / "env.json").exists():
            raise ImportError_("文件里没有找到 Eve 的数据（Memories.db、proactive_config.json 或 env-safe.txt）")
        (folder / "meta.json").write_text(json.dumps({"sha256": digest.hexdigest(), "size": size,
                                                      "filename": filename[:200], "at": time.time()}),
                                          encoding="utf-8")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        raw.unlink(missing_ok=True)
    return token


def _unpack(raw: Path, folder: Path, filename: str) -> None:
    with raw.open("rb") as fh:
        head = fh.read(16)
    if head.startswith(b"SQLite format 3\x00"):
        os.replace(raw, folder / "Memories.db")
        return
    if head[:2] == b"\x1f\x8b" or tarfile.is_tarfile(raw):
        try:
            with tarfile.open(raw, "r:*") as tar:
                for member in tar:
                    name = PurePosixPath(member.name).name
                    if member.isfile() and name in KEEP and member.size <= MAX_UPLOAD:
                        src = tar.extractfile(member)
                        if src is not None:
                            _keep(name, src, folder)
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise ImportError_(f"压缩包打不开：{exc.__class__.__name__}") from exc
        return
    if zipfile.is_zipfile(raw):
        with zipfile.ZipFile(raw) as zf:
            for info in zf.infolist():
                name = PurePosixPath(info.filename.replace("\\", "/")).name
                if not info.is_dir() and name in KEEP and info.file_size <= MAX_UPLOAD:
                    with zf.open(info) as src:
                        _keep(name, src, folder)
        return
    if filename.endswith(".json") or head.lstrip().startswith(b"{"):
        os.replace(raw, folder / "proactive_config.json")
        return
    raise ImportError_("认不出这个文件：请上传 eve-export.tar.gz、.zip 或 Memories.db")


def _keep(name: str, src: BinaryIO, folder: Path) -> None:
    if KEEP[name] == "env":
        # only the keys Violet may import; a full .env (keys!) never reaches the disk here
        text = src.read(2_000_000).decode("utf-8", "replace")
        old = folder / "env.json"
        values = json.loads(old.read_text(encoding="utf-8")) if old.exists() else {}
        values.update(parse_env(text))
        old.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        return
    with (folder / name).open("wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)


def parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not m or SECRET_RE.search(m.group(1)):
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        if len(value) <= 200 and "\n" not in value:
            out[m.group(1)] = value
    return out


# ------------------------------------------------------------------ reading Eve's files
def _open_eve(folder: Path) -> sqlite3.Connection | None:
    path = folder / "Memories.db"
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        return conn
    except sqlite3.DatabaseError as exc:
        raise ImportError_(f"Memories.db 打不开（{exc.__class__.__name__}）：请用打包命令重新复制一份") from exc


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _proactive(folder: Path) -> dict | None:
    path = folder / "proactive_config.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ImportError_("proactive_config.json 不是正确的 JSON") from exc
    return data if isinstance(data, dict) else None


def _env(folder: Path) -> dict[str, str] | None:
    path = folder / "env.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def settings_plan(folder: Path) -> list[dict[str, Any]]:
    """Every value in Eve's files that maps to a Violet setting: Eve's value, Violet's value now, importable or why
    not. Values are settings (numbers, times, switches) -- never secrets, which the export left out."""
    from ai.control_config import BY_KEY, SETTINGS, store, validate
    now = {row["key"]: row.get("value") for row in store().describe()}
    rows: list[dict[str, Any]] = []

    def add(source: str, eve_key: str, value: Any, key: str | None, why: str | None = None) -> None:
        row = {"source": source, "eve_key": eve_key, "eve": value, "key": key, "label": None, "now": None,
               "ok": False, "same": False, "why": why}
        if key and not why:
            s = BY_KEY[key]
            row.update(label=s.label, now=now.get(key))
            try:
                row["value"] = validate(key, value)
                row["ok"] = row["value"] is not None
                row["same"] = str(row["value"]).lower() == str(row["now"]).lower()
                if not row["ok"]:
                    row["why"] = "空值"
            except ValueError as exc:
                row["why"] = f"Violet 不接受这个值：{exc}"
        rows.append(row)

    proactive = _proactive(folder)
    if proactive is not None:
        for s in SETTINGS:
            if s.target != "proactive":
                continue
            node: Any = proactive
            for part in s.path:
                node = node.get(part) if isinstance(node, dict) else None
            if node is not None:
                add("proactive_config.json", ".".join(s.path), node, s.key)
        for k, why in PROACTIVE_UNMAPPED.items():
            if k in proactive:
                add("proactive_config.json", k, proactive[k], None, why)
    env = _env(folder)
    if env is not None:
        for k, v in sorted(env.items()):
            key = k if k in BY_KEY else (("VIOLET_" + k[4:]) if k.startswith("EVE_") else k)
            s = BY_KEY.get(key)
            if s is None:
                add(".env", k, v, None, "Violet 没有这个设置")
            elif s.kind in ("secret", "url", "prompt_version") or s.target != "env":
                add(".env", k, v, None, "这一项不从文件导入")
            elif s.kind == "model":
                add(".env", k, v, None, "模型名：Violet 在“模型服务”页单独选，不覆盖")
            else:
                add(".env", k, v, key)
    return rows


def preview(token: str, violetdb) -> dict[str, Any]:
    folder = _dir(token)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    out: dict[str, Any] = {"token": token, "file": {"name": meta.get("filename"), "size": meta.get("size")},
                           "messages": None, "core": None, "events": None, "settings": settings_plan(folder),
                           "imported_before": _imported_before(violetdb, meta.get("sha256"))}
    conn = _open_eve(folder)
    if conn is not None:
        try:
            tables = _tables(conn)
            if "messages" in tables:
                stats = conn.execute("SELECT COUNT(*) AS n, SUM(role = 'user') AS mine, SUM(role = 'assistant') AS hers, "
                                     "MIN(created_at) AS first, MAX(created_at) AS last FROM messages").fetchone()
                types = {r["message_type"]: r["n"] for r in conn.execute(
                    "SELECT message_type, COUNT(*) AS n FROM messages GROUP BY message_type")}
                out["messages"] = {"count": stats["n"], "user": stats["mine"] or 0, "assistant": stats["hers"] or 0,
                                   "first": stats["first"], "last": stats["last"], "types": types}
            if "core_memories" in tables:
                keys = [r[0] for r in conn.execute("SELECT memory_key FROM core_memories")]
                have = {r["memory_key"] for r in violetdb.fetch_all("SELECT memory_key FROM core_memories")}
                out["core"] = {"count": len(keys), "existing": sum(1 for k in keys if k in have)}
            if "event_memories" in tables:
                out["events"] = {"count": conn.execute("SELECT COUNT(*) FROM event_memories").fetchone()[0],
                                 "by_status": {r["status"]: r["n"] for r in conn.execute(
                                     "SELECT status, COUNT(*) AS n FROM event_memories GROUP BY status")}}
        finally:
            conn.close()
    return out


def _imported_before(violetdb, sha: str | None) -> list[dict[str, Any]]:
    violetdb.execute(LEDGER)
    return violetdb.fetch_all("SELECT channel, result, created_at FROM eve_imports WHERE file_sha256 = ? ORDER BY id",
                              (sha or "",))


# ------------------------------------------------------------------ commit
def commit(token: str, violetdb, *, channel: str | None, messages: bool, memories: bool, settings: list[str],
           actor: str, apply: Callable[[], None] | None = None) -> dict[str, Any]:
    folder = _dir(token)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {"messages": 0, "messages_skipped": 0, "core": 0, "core_skipped": 0, "events": 0,
                              "settings": [], "channel": channel}
    if messages and not channel:
        raise ImportError_("请选择聊天记录导入到哪个渠道")
    conn = _open_eve(folder) if (messages or memories) else None
    if (messages or memories) and conn is None:
        raise ImportError_("上传的文件里没有 Memories.db")
    try:
        with violetdb.transaction():
            id_map: dict[int, int] = {}
            if messages:
                n, skipped, id_map = _import_messages(conn, violetdb, channel)
                result.update(messages=n, messages_skipped=skipped)
            if memories:
                result.update(_import_memories(conn, violetdb, id_map))
            violetdb.execute(LEDGER)
            violetdb.execute("INSERT INTO eve_imports (file_sha256, channel, result, actor, created_at) VALUES (?, ?, ?, ?, ?)",
                             (meta.get("sha256"), channel, json.dumps(result, ensure_ascii=False), actor, violetdb.now()))
    finally:
        if conn is not None:
            conn.close()
    if settings:
        plan = {row["key"]: row for row in settings_plan(folder) if row.get("key") and row["ok"]}
        changes = {k: plan[k]["value"] for k in settings if k in plan}
        if changes:
            from ai.control_config import store
            store().save(changes, actor, "从 Eve 导入", apply=apply)
            result["settings"] = sorted(changes)
    shutil.rmtree(folder, ignore_errors=True)
    logger.info("Eve import: %s", {k: v for k, v in result.items() if k != "settings"} | {"settings": len(result["settings"])})
    return result


def _import_messages(conn: sqlite3.Connection, violetdb, channel: str) -> tuple[int, int, dict[int, int]]:
    if "messages" not in _tables(conn):
        return 0, 0, {}
    from ai.channels.identity import IdentityStore
    peer = IdentityStore(violetdb).first_peer(channel)
    cols = _columns(conn, "messages")
    media = "media_id" if "media_id" in cols else "NULL AS media_id"
    rows = conn.execute(f"SELECT id, role, message_type, content, {media}, created_at FROM messages "
                        "WHERE role IN ('user', 'assistant') AND created_at IS NOT NULL ORDER BY created_at, id").fetchall()
    have = {(r["created_at"], r["role"], r["content"]) for r in violetdb.fetch_all(
        "SELECT created_at, role, content FROM messages WHERE COALESCE(channel, 'wechat') = ?", (channel,))}
    new = [r for r in rows if (r["created_at"], r["role"], r["content"]) not in have]
    low = violetdb.fetch_one("SELECT MIN(id) AS low FROM messages")["low"]
    start = min(int(low) if low is not None else 1, 1) - len(new)   # ids start..start+len-1, all below every row
    id_map: dict[int, int] = {}
    for i, r in enumerate(new):
        new_id = start + i
        id_map[int(r["id"])] = new_id
        role = r["role"]
        kind = r["message_type"] if r["message_type"] in MESSAGE_TYPES else "text"
        violetdb.execute("INSERT INTO messages (id, role, direction, message_type, content, media_id, created_at, channel, "
                         "peer_id) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                         (new_id, role, "incoming" if role == "user" else "outgoing", kind, r["content"], r["created_at"],
                          channel, peer))
    return len(new), len(rows) - len(new), id_map


def _import_memories(conn: sqlite3.Connection, violetdb, id_map: dict[int, int]) -> dict[str, int]:
    out = {"core": 0, "core_skipped": 0, "events": 0}
    tables = _tables(conn)
    if "core_memories" in tables:
        cols = [c for c in CORE_COLUMNS if c in _columns(conn, "core_memories")]
        have = {r["memory_key"] for r in violetdb.fetch_all("SELECT memory_key FROM core_memories")}
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM core_memories ORDER BY id"):
            if r["memory_key"] in have or not r["content"]:
                out["core_skipped"] += 1
                continue
            row = _with_defaults(dict(r), core=True, now=violetdb.now())
            keys = [c for c in CORE_COLUMNS if c in row]
            violetdb.execute(f"INSERT INTO core_memories ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                             [row[k] for k in keys])
            have.add(r["memory_key"])
            out["core"] += 1
    if "event_memories" in tables:
        cols = [c for c in EVENT_COLUMNS if c in _columns(conn, "event_memories")]
        have = {(r["content"], r["created_at"]) for r in violetdb.fetch_all("SELECT content, created_at FROM event_memories")}
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM event_memories ORDER BY id"):
            if not r["content"] or (r["content"], r["created_at"]) in have:
                continue
            row = _with_defaults(dict(r), core=False, now=violetdb.now())
            row["source_message_ids"] = _remap_sources(row.get("source_message_ids"), id_map)
            if row.get("status") not in ("active", "expired", "archived", "forgotten", "deleted"):
                row["status"] = "active"
            keys = [c for c in EVENT_COLUMNS if c in row]
            violetdb.execute(f"INSERT INTO event_memories ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                             [row[k] for k in keys])
            out["events"] += 1
    return out


def _with_defaults(row: dict[str, Any], core: bool, now: str) -> dict[str, Any]:
    row = {k: v for k, v in row.items() if v is not None}
    row.setdefault("created_at", now)
    row.setdefault("updated_at", row["created_at"])
    if core:
        row.setdefault("category", "general")
        row["source"] = "eve_import"
    return row


def _remap_sources(raw: Any, id_map: dict[int, int]) -> str | None:
    if not raw:
        return None
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(ids, list):
        return None
    mapped = [id_map[int(i)] for i in ids if str(i).lstrip("-").isdigit() and int(i) in id_map]
    return json.dumps(mapped) if mapped else None
