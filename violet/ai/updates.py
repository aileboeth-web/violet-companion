"""Is there a newer Violet release? (for the Control Center)

The installed version is the program's VERSION file; the newest release comes from GitHub Releases of
``tools.violet_installer.update_repo()`` (unauthenticated API, checked at most every 6 hours unless the admin asks
again). Nothing is downloaded or installed from here: upgrading replaces the program and restarts the service, which
needs the machine's administrator -- the page shows the one command for that (``sudo <root>/violet update``).
VIOLET_UPDATE_CHECK=0 switches the check off (no request to GitHub at all).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from ai.settings import PROJECT_ROOT, env_bool

CACHE_SECONDS = 6 * 3600
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}


def current_version(root: Path = PROJECT_ROOT) -> str:
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def install_root(root: Path = PROJECT_ROOT) -> Path | None:
    """<root> when this program runs from an installation made by the installer (<root>/app), else None."""
    parent = root.parent
    return parent if root.name == "app" and (parent / "install.json").is_file() else None


def update_command(root: Path = PROJECT_ROOT) -> str:
    installed = install_root(root)
    if installed is None:
        return "这份程序不是用安装器装的：下载新版本后在其目录里运行 sudo bash install.sh upgrade"
    return f"sudo {installed / 'violet'} update"


def check(refresh: bool = False, fetch: Callable[[], dict] | None = None, now: Callable[[], float] = time.time,
          root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """{enabled, current, latest, newer, url, published_at, notes, command, checked_at, error}."""
    current = current_version(root)
    base = {"enabled": env_bool("VIOLET_UPDATE_CHECK", True), "current": current, "command": update_command(root)}
    if not base["enabled"]:
        return {**base, "latest": None, "newer": False, "checked_at": None, "error": None}
    with _LOCK:
        cached = _CACHE.get("result")
        if cached and not refresh and now() - cached["checked_at"] < CACHE_SECONDS:
            return {**base, **{k: v for k, v in cached.items() if k not in base}, "newer": _newer(cached, current)}
    from tools.violet_installer import InstallError, latest_release
    try:
        release = (fetch or latest_release)()
        url = str(release.get("url") or "")
        found = {"latest": release["version"], "url": url if url.startswith("https://github.com/") else None,
                 "published_at": release.get("published_at"),
                 "notes": release.get("notes") or "", "error": None, "checked_at": now()}
    except InstallError as exc:
        found = {"latest": None, "url": None, "published_at": None, "notes": "", "error": str(exc), "checked_at": now()}
    with _LOCK:
        _CACHE["result"] = found
    return {**base, **found, "newer": _newer(found, current)}


def _newer(found: dict, current: str) -> bool:
    from tools.violet_installer import is_newer
    return bool(found.get("latest")) and is_newer(found["latest"], current)


def reset_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()
