"""Violet's life runs on its own clock (phase 5A), separate from proactive chat.

* One daemon thread in the main process (no extra service). It only calls
  LifeEngine.run_day(), which is idempotent per local day (VIOLET_TIMEZONE), so a
  restart never repeats a day and downtime is never backfilled.
* It never sends anything: it has no access to the WeChat client or outbox.
  Life events reach the user only as optional proactive candidates.
* Modes:
    off     VIOLET_LIFE_ENGINE=0 or life_config.enabled=false (thread not started)
    paused  set from the dashboard; stored in life_settings, survives restarts
    on      default
  Turning proactive chat off has no effect on any of this.
* Phase 5B: the same thread also gives WebExplorer its (at most daily) turn
  when it is authorised and switched on; it never sends anything either.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable

from ai.settings import env_bool, env_float, now_local

logger = logging.getLogger("violet.life")

PAUSED_KEY = "engine_paused"


class LifeControl:
    """Mode and pause switch, stored in the database (usable without a running thread)."""

    def __init__(self, db):
        self.db = db

    def _get(self, key: str) -> str | None:
        row = self.db.fetch_one("SELECT value FROM life_settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO life_settings (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                        (key, value, self.db.now()))

    def configured(self) -> bool:
        try:
            from ai.persona import life_config
            enabled = bool(life_config().get("enabled", True))
        except Exception:
            logger.exception("[life] life_config.json unavailable")
            return False
        return enabled and env_bool("VIOLET_LIFE_ENGINE", True)

    def mode(self) -> str:
        if not self.configured():
            return "off"
        return "paused" if self._get(PAUSED_KEY) == "1" else "on"

    def set_paused(self, paused: bool, actor: str = "dashboard", reason: str | None = None) -> str:
        before = self.mode()
        self._set(PAUSED_KEY, "1" if paused else "0")
        from ai.life_store import LifeStore
        LifeStore(self.db).revise("engine", 0, "pause" if paused else "resume", before, self.mode(), reason, actor)
        return self.mode()

    def status(self) -> dict[str, Any]:
        last = self.db.fetch_one("SELECT * FROM life_engine_runs ORDER BY id DESC LIMIT 1")
        return {"mode": self.mode(), "last_run": last}


class LifeScheduler:
    def __init__(self, db, clock: Callable[[], datetime] = now_local, engine_factory: Callable[[], Any] | None = None,
                 interval_seconds: float | None = None, explorer_factory: Callable[[], Any] | None = None):
        self.db = db
        self.clock = clock
        self.control = LifeControl(db)
        self.engine_factory = engine_factory
        self.explorer_factory = explorer_factory
        self.interval = interval_seconds if interval_seconds is not None else env_float("VIOLET_LIFE_CHECK_INTERVAL_SECONDS", 600)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _engine(self):
        if self.engine_factory is not None:
            return self.engine_factory()
        from ai.life_engine import LifeEngine
        return LifeEngine(self.db)

    def _explorer(self):
        if self.explorer_factory is not None:
            return self.explorer_factory()
        from ai.web.explorer import WebExplorer
        return WebExplorer(self.db)

    def explore(self) -> dict[str, Any] | None:
        from ai.web.explorer import WebExplorer
        if not WebExplorer.authorized():
            return None
        try:
            result = self._explorer().run(self.clock())
        except Exception as exc:
            logger.exception("[explorer] tick failed")
            return {"outcome": "error", "error": f"{exc.__class__.__name__}: {exc}"}
        if result.get("outcome") not in ("already_ran", "too_early", "disabled", "off"):
            logger.info("[explorer] %s", result)
        return result

    def tick(self) -> dict[str, Any]:
        mode = self.control.mode()
        if mode != "on":
            result = {"outcome": mode}
        else:
            try:
                result = self._engine().run_day(self.clock())
            except Exception as exc:
                logger.exception("[life] tick failed")
                result = {"outcome": "error", "error": f"{exc.__class__.__name__}: {exc}"}
            if result.get("outcome") not in ("already_ran", "too_early", "disabled"):
                logger.info("[life] %s", result)
        explored = self.explore()
        if explored is not None and explored.get("outcome") not in ("off",):
            result = {**result, "explore": explored}
        return result

    def start(self) -> bool:
        from ai.web.explorer import WebExplorer
        if self.control.mode() == "off" and not WebExplorer.authorized():
            logger.info("[life] life engine is off (VIOLET_LIFE_ENGINE / life_config.enabled) and WebExplorer is not authorised")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="violet-life", daemon=True)
        self._thread.start()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(max(5.0, self.interval))

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
