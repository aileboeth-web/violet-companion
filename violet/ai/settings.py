"""Shared runtime settings: .env location, typed env readers, logging setup.

Every module that used to compute its own ``.env`` path now goes through
``ENV_PATH`` so tests (and alternative deployments) can point the whole
process at a different file with ``VIOLET_ENV_FILE``.
"""

from __future__ import annotations

import logging
import sys
import os
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_env_path() -> Path:
    override = os.environ.get("VIOLET_ENV_FILE", "").strip()
    return Path(override) if override else PROJECT_ROOT / ".env"


ENV_PATH = _resolve_env_path()


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        logging.getLogger("violet.settings").warning("invalid float for %s=%r, using %s", name, raw, default)
        return default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        logging.getLogger("violet.settings").warning("invalid int for %s=%r, using %s", name, raw, default)
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


_TZ_CACHE: dict[str, tzinfo] = {}
DEFAULT_TIMEZONE = "Asia/Shanghai"


def local_timezone() -> tzinfo:
    """The IANA zone from VIOLET_TIMEZONE (default Asia/Shanghai).

    Falls back to a fixed UTC+08:00 offset (with a warning) when the zone
    database is unavailable, e.g. Windows without the `tzdata` package.
    """
    name = env_str("VIOLET_TIMEZONE", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
    cached = _TZ_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        from zoneinfo import ZoneInfo
        zone: tzinfo = ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError for bad keys
        logging.getLogger("violet.settings").warning(
            "time zone %r unavailable (%s); falling back to UTC+08:00. Install `tzdata` or fix VIOLET_TIMEZONE.",
            name, exc.__class__.__name__,
        )
        zone = timezone(timedelta(hours=8), "UTC+08:00")
    _TZ_CACHE[name] = zone
    return zone


def now_local() -> datetime:
    return datetime.now(local_timezone())


def seconds_until_daily(hour: int, minute: int = 0, now: datetime | None = None) -> float:
    """Seconds from ``now`` (default: now in VIOLET_TIMEZONE) to the next hour:minute on the wall clock of that zone.
    Daily jobs use this instead of the server's own clock, which is often UTC. Computed from timestamps, so a
    daylight-saving change in between is counted correctly."""
    now = now or now_local()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = (target + timedelta(days=1)).replace(hour=hour, minute=minute)
    return max(0.0, target.timestamp() - now.timestamp())


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s]%(turn)s %(message)s"

# phase 10.6: every log line written while a turn is processed carries its correlation id ("[turn t-...]"), the same id
# the inbox / outbox rows and the Control Center's diagnosis show. A record factory (not a handler filter) so that
# every logger and handler gets the attribute and the format can never fail on it.
import contextvars as _contextvars

CURRENT_TURN: "_contextvars.ContextVar[str | None]" = _contextvars.ContextVar("violet_turn", default=None)
_BASE_RECORD_FACTORY = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    record = _BASE_RECORD_FACTORY(*args, **kwargs)
    turn = CURRENT_TURN.get()
    record.turn = f" [turn {turn}]" if turn else ""
    return record


if not getattr(_BASE_RECORD_FACTORY, "_eve_turn", False):
    _record_factory._eve_turn = True                      # type: ignore[attr-defined]
    logging.setLogRecordFactory(_record_factory)


class _LogStream:
    """File-like object that forwards print() / tracebacks to a logger (line by line)."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger, self.level, self._buf = logger, level, ""

    def write(self, text: str) -> int:
        self._buf += str(text)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            self.logger.log(self.level, self._buf.rstrip())
        self._buf = ""

    def isatty(self) -> bool:
        return False


def configure_log_file(path: str | Path, max_bytes: int, backups: int, capture_std: bool,
                       root: logging.Logger | None = None) -> logging.Handler:
    """Size-based rotation (phase 7, R-O1). With capture_std, print() and stderr go through it too."""
    from logging.handlers import RotatingFileHandler
    root = root or logging.getLogger()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=max(1024, max_bytes), backupCount=max(1, backups), encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    if capture_std:
        # console handlers would copy everything into the (unrotated) nohup output: drop them
        for h in list(root.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                root.removeHandler(h)
        sys.stdout = _LogStream(logging.getLogger("violet.stdout"), logging.INFO)
        sys.stderr = _LogStream(logging.getLogger("violet.stderr"), logging.WARNING)
    return handler


def setup_logging() -> None:
    """Configure root logging once (idempotent).

    VIOLET_LOG_FILE set (start.sh does this): rotating file VIOLET_LOG_MAX_BYTES (20 MB) x VIOLET_LOG_BACKUP_COUNT (5),
    print()/stderr captured into it (VIOLET_LOG_CAPTURE_STDOUT=1). Otherwise: console only, as before.
    """
    root = logging.getLogger()
    if getattr(root, "_eve_configured", False):
        return
    level = getattr(logging, env_str("VIOLET_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    log_file = env_str("VIOLET_LOG_FILE", "")
    if log_file:
        configure_log_file(log_file, env_int("VIOLET_LOG_MAX_BYTES", 20 * 1024 * 1024), env_int("VIOLET_LOG_BACKUP_COUNT", 5),
                           env_bool("VIOLET_LOG_CAPTURE_STDOUT", True), root)
    root._eve_configured = True  # type: ignore[attr-defined]
