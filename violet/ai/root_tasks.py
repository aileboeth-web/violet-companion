"""Work only root can do, asked for from the Control Center, and its progress (Violet 0.7.0).

Violet runs as an unprivileged account. A root + systemd installation has root helpers (tools/violet_installer.py:
updater_units) that watch files in <data>/control:

  update-request  -> violet-update.service (`violet auto-update`): install the newest release
  task-request    -> violet-task.service (`violet root-task`): one word of TASKS, e.g. install the phone relay

The helpers write what they are doing to <data>/control/<name>-progress.json ({state, percent, step, message, ...};
state queued | running | done | failed). The page polls it through /api/control/progress/<name> and shows a dialog
with a progress bar. This module only ever writes a request word and the "queued" entry; root decides the rest.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

TASKS = ("relay-install", "relay-set-port", "searxng-install", "searxng-uninstall", "searxng-tune")
NAMES = ("task", "update")
STALE_SECONDS = 1800           # a running entry this old belongs to a helper that was killed


def control_dir() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "control"


def progress_file(name: str) -> Path:
    if name not in NAMES:
        raise ValueError(name)
    return control_dir() / f"{name}-progress.json"


def read_progress(name: str, now: float | None = None) -> dict[str, Any]:
    """The newest entry, or {"state": "none"}. A queued / running entry not touched for STALE_SECONDS is "stale"."""
    try:
        data = json.loads(progress_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "none"}
    if not isinstance(data, dict):
        return {"state": "none"}
    out = {k: data.get(k) for k in ("task", "state", "percent", "step", "message", "at", "started_at", "version",
                                    "device_port", "notes", "url")}
    if out["state"] in ("queued", "running") and (now or time.time()) - float(out.get("at") or 0) > STALE_SECONDS:
        out["state"] = "stale"
        out["message"] = "很久没有进度了：服务器上的任务可能已经中断，可以再试一次"
    return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".page.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def mark_queued(name: str, task: str, step: str = "已提交，等待服务器开始…") -> dict[str, Any]:
    now = int(time.time())
    entry = {"task": task, "state": "queued", "percent": 0, "step": step, "at": now, "started_at": now}
    _write(progress_file(name), json.dumps(entry, ensure_ascii=False))
    return entry


def available() -> bool:
    """A root helper exists (root + systemd installation made by the installer)."""
    from ai.updates import install_state
    return install_state().get("auto_update_agent") == "systemd"


def busy(name: str = "task") -> bool:
    request = control_dir() / ("task-request" if name == "task" else "update-request")
    return request.exists() or read_progress(name)["state"] in ("queued", "running")


def searxng_export() -> dict[str, Any] | None:
    """What root wrote after installing the private SearXNG (<data>/control/searxng.json): {url, port, ...}."""
    try:
        data = json.loads((control_dir() / "searxng.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = str(data.get("url") or "") if isinstance(data, dict) else ""
    return data if url.startswith("http://127.0.0.1:") else None


def request_task(task: str) -> dict[str, Any]:
    """Ask the root task service to run `task`. PermissionError: no root helper; ValueError: unknown task;
    RuntimeError: one is already waiting or running."""
    if task not in TASKS:
        raise ValueError("不支持的操作")
    if not available():
        raise PermissionError("这种安装方式（不是 root + systemd）不能在网页上执行，请在服务器上运行命令")
    if busy("task"):
        raise RuntimeError("上一个操作还没有完成")
    entry = mark_queued("task", task)
    _write(control_dir() / "task-request", task)
    return entry
