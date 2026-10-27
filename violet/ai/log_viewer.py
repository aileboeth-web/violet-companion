"""The run log in the Control Center (Violet stage 3): the newest part of VIOLET_LOG_FILE (main.log), grouped into
records, filtered, and with every secret removed before it leaves the server.

Read only: at most the last TAIL_BYTES of main.log (plus the rotated main.log.1 when that is not enough), never the
whole history. A record is a line that starts with the log format's timestamp; the lines after it that do not
(tracebacks, multi-line print() output) belong to it.

Redaction, always: every secret value Violet knows -- environment variables whose names say KEY / TOKEN / SECRET /
PASSWORD ..., the Control Center's stored secrets, the chat channels' secrets, the admin token file -- plus generic
patterns (key=..., token=..., Bearer ..., bot tokens, long random strings). The admin may download the same redacted
text; the raw file is never served.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

TAIL_BYTES = 2 * 1024 * 1024
MAX_LIMIT = 1000
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_RECORD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?) (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
                     r"\[([^\]]+)\](?: ?\[turn ([\w.-]+)\])? ?(.*)$")
_LONG_RANDOM = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b")
_BOT_TOKEN = re.compile(r"\bbot\d+:[A-Za-z0-9_-]{20,}")


def log_path() -> Path | None:
    value = (os.environ.get("VIOLET_LOG_FILE") or "").strip()
    return Path(value) if value else None


def known_secrets(data_dir: str | os.PathLike[str] | None = None) -> list[str]:
    """Every secret value this installation has, longest first (so a longer one is replaced before its prefix)."""
    found: set[str] = set()
    try:
        from ai.web.service import environment_secrets
        found.update(environment_secrets())
    except Exception:
        pass
    base = Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data")
    try:
        from ai.control_config import ControlStore
        found.update(v for v in ControlStore(base).secret_values().values() if isinstance(v, str))
    except Exception:
        pass
    try:
        from ai.channels.manager import ChannelStore
        store = ChannelStore(base)
        for cid in store.channels():
            found.update(v for v in store.secrets(cid).values() if isinstance(v, str))
    except Exception:
        pass
    try:
        found.add((base / "admin_token").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    return sorted((s for s in found if s and len(s) >= 6), key=len, reverse=True)


def redact(text: str, secrets: Iterable[str]) -> str:
    from ai.mcp import scrub_secrets
    text = scrub_secrets(text, list(secrets))
    text = _BOT_TOKEN.sub("bot<redacted>", text)
    return _LONG_RANDOM.sub("<redacted>", text)


def _tail(path: Path, limit: int | None = None) -> tuple[str, bool]:
    """The last `limit` bytes of path (and of path.1 before it when path is shorter). (text, more_before)."""
    chunks, remaining, more = [], limit or TAIL_BYTES, False
    for candidate in (path, path.with_name(path.name + ".1")):
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        with candidate.open("rb") as f:
            if size > remaining:
                f.seek(size - remaining)
                more = True
            data = f.read(remaining)
        chunks.insert(0, data)
        remaining -= len(data)
        if remaining <= 0:
            break
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if more and "\n" in text:
        text = text.split("\n", 1)[1]                  # the first line was cut in the middle
    return text, more


def parse(text: str) -> list[dict[str, Any]]:
    """Records in file order: {time, level, logger, turn, message, detail}."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _RECORD.match(line)
        if m:
            records.append({"time": m.group(1).replace(",", "."), "level": m.group(2), "logger": m.group(3),
                            "turn": m.group(4), "message": m.group(5), "detail": []})
        elif records and line.strip():
            records[-1]["detail"].append(line)
        elif line.strip():                             # text before the first record of the window
            records.append({"time": None, "level": "INFO", "logger": "", "turn": None, "message": line, "detail": []})
    return records


def read(*, level: str = "INFO", query: str = "", turn: str = "", logger: str = "", limit: int = 300,
         path: Path | None = None, secrets: list[str] | None = None) -> dict[str, Any]:
    """The newest matching records first, redacted."""
    path = path or log_path()
    if path is None:
        return {"available": False, "reason": "Violet 没有写日志文件（VIOLET_LOG_FILE 未设置）：用安装器安装时会自动设置"}
    if not path.exists():
        return {"available": False, "reason": "日志文件还不存在（Violet 还没有写过日志）", "file": str(path)}
    level = level.upper() if level and level.upper() in LEVELS else "INFO"
    floor = LEVELS.index(level)
    limit = max(1, min(int(limit or 300), MAX_LIMIT))
    secrets = known_secrets() if secrets is None else secrets
    text, more = _tail(path)
    records = parse(text)
    counts = {name: 0 for name in LEVELS}
    for r in records:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    q = (query or "").strip().lower()
    out = []
    for r in reversed(records):
        if LEVELS.index(r["level"]) < floor:
            continue
        if turn and r["turn"] != turn:
            continue
        if logger and not r["logger"].startswith(logger):
            continue
        clean = {**r, "message": redact(r["message"], secrets), "detail": [redact(l, secrets) for l in r["detail"]]}
        if q and q not in (clean["message"] + "\n" + "\n".join(clean["detail"]) + " " + clean["logger"]).lower():
            continue
        out.append(clean)
        if len(out) >= limit:
            break
    stat = path.stat()
    return {"available": True, "file": str(path), "size": stat.st_size, "modified": int(stat.st_mtime),
            "window_bytes": len(text.encode("utf-8")), "older_not_shown": more, "counts": counts,
            "entries": out, "truncated": len(out) >= limit}


def download_text(path: Path | None = None, secrets: list[str] | None = None) -> str:
    """The newest part of the log as plain text, redacted (for "download")."""
    path = path or log_path()
    if path is None or not path.exists():
        return ""
    secrets = known_secrets() if secrets is None else secrets
    text, _more = _tail(path)
    return "\n".join(redact(line, secrets) for line in text.splitlines()) + "\n"
