"""Is there a newer Violet release, and the automatic updates (for the Control Center).

The installed version is the program's VERSION file; the newest release comes from GitHub Releases of
``tools.violet_installer.update_repo()`` (or the installation's own release feed), unauthenticated, cached for 6 hours
unless the admin asks again.

Update mode (setting VIOLET_UPDATE_MODE, Violet stage 3):
  auto    checked automatically; a newer release is installed automatically
  notify  checked automatically; installing waits for "update now" (or `sudo <root>/violet update`)
  manual  nothing is fetched until the admin presses "check for updates"

Violet runs as an unprivileged user and never replaces its own program. A root + systemd install has a root update
service (tools/violet_installer.py: updater_units / auto_update); all this module can do is ask it to run, by creating
<data>/control/update-request -- the service decides what to install (always the official newest release) and writes
the outcome into <root>/install.json, which is read here. Other installs show the command to run instead.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ai.settings import PROJECT_ROOT, env_bool, env_str

CACHE_SECONDS = 6 * 3600
MODES = ("auto", "notify", "manual")
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


def install_state(root: Path = PROJECT_ROOT) -> dict:
    installed = install_root(root)
    if installed is None:
        return {}
    try:
        data = json.loads((installed / "install.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_command(root: Path = PROJECT_ROOT) -> str:
    installed = install_root(root)
    if installed is None:
        return "这份程序不是用安装器装的：下载新版本后在其目录里运行 sudo bash install.sh upgrade"
    return f"sudo {installed / 'violet'} update"


def mode() -> str:
    value = env_str("VIOLET_UPDATE_MODE", "").lower()
    if value in MODES:
        return value
    return "auto" if env_bool("VIOLET_UPDATE_CHECK", True) else "manual"      # the 0.2.0 switch


def request_path() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "control" / "update-request"


def automatic(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Can this installation update itself, and what happened the last time."""
    state = install_state(root)
    last = state.get("auto_update") or {}
    available = state.get("auto_update_agent") == "systemd"
    return {"available": available, "requested": request_path().exists(),
            "reason": None if available else
            ("这种安装方式（不是 root + systemd）不能自动更新，请在服务器上执行下面的命令" if install_root(root)
             else "这份程序不是用安装器装的，不能自动更新"),
            "last_run_at": last.get("last_run_at"), "last_result": last.get("last_result"),
            "last_message": last.get("last_message"), "failed_version": last.get("failed_version")}


def request_update(by: str = "manual", root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Ask the root update service to run now. Raises PermissionError when this installation has none."""
    if not automatic(root)["available"]:
        raise PermissionError(automatic(root)["reason"])
    path = request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("auto" if by == "auto" else "manual", encoding="utf-8")
    os.replace(tmp, path)
    return {"requested": True}


def _fetch_default(root: Path) -> Callable[[], dict]:
    state = install_state(root)

    def fetch() -> dict:
        # the same source the root update service uses (install.json only -- never this process's environment)
        from tools.violet_installer import UPDATE_REPO, feed_release, latest_release
        if state.get("update_feed"):
            return feed_release(state["update_feed"])
        return latest_release(state.get("update_repo") or UPDATE_REPO)
    return fetch


def check(refresh: bool = False, fetch: Callable[[], dict] | None = None, now: Callable[[], float] = time.time,
          root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """{mode, enabled, current, latest, newer, url, published_at, notes, command, checked_at, error, auto}.
    In "manual" mode GitHub is only asked when ``refresh`` (the admin pressed the button); a result fetched earlier
    is still shown."""
    current, chosen = current_version(root), mode()
    base = {"mode": chosen, "enabled": chosen != "manual", "current": current, "command": update_command(root),
            "auto": automatic(root)}
    with _LOCK:
        cached = _CACHE.get("result")
        fresh = cached and now() - cached["checked_at"] < CACHE_SECONDS
        if cached and not refresh and (fresh or chosen == "manual"):
            return {**base, **{k: v for k, v in cached.items() if k not in base}, "newer": _newer(cached, current)}
    if chosen == "manual" and not refresh:
        return {**base, "latest": None, "newer": False, "url": None, "published_at": None, "notes": "",
                "checked_at": None, "error": None}
    from tools.violet_installer import InstallError
    try:
        release = (fetch or _fetch_default(root))()
        url = str(release.get("url") or "")
        found = {"latest": release["version"], "url": url if url.startswith("https://github.com/") else None,
                 "published_at": release.get("published_at"),
                 "notes": release.get("notes") or "", "error": None, "checked_at": now()}
    except InstallError as exc:
        found = {"latest": None, "url": None, "published_at": None, "notes": "", "error": str(exc), "checked_at": now()}
    with _LOCK:
        _CACHE["result"] = found
    result = {**base, **found, "newer": _newer(found, current)}
    if result["newer"] and chosen == "auto" and base["auto"]["available"] and not base["auto"]["requested"]:
        # "detected -> updated": do not wait for the next timer run. A release that failed is left to the service's
        # own 24-hour back-off (an "auto" request does not bypass it).
        try:
            request_update("auto", root)
            result["auto"] = automatic(root)
        except (OSError, PermissionError):
            pass
    return result


def _newer(found: dict, current: str) -> bool:
    from tools.violet_installer import is_newer
    return bool(found.get("latest")) and is_newer(found["latest"], current)


def reset_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()
