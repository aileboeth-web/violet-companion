"""Violet's own supervisor (phase 10.4) for systems without systemd (Windows, macOS, containers, minimal Linux).

    python tools/violet_service.py --root <install root>

Runs ``main.py`` of <root>/app with <root>/config/violet.env, restarts it when it exits unexpectedly (backoff 2 s ->
60 s, reset after 10 minutes of running), and stops it gracefully when <root>/violet.stop appears (the installer's
``stop``). Writes its own pid to <root>/violet.pid and a line per start / exit to <root>/logs/supervisor.log.
Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def log(root: Path, text: str) -> None:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    with open(root / "logs" / "supervisor.log", "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")


def terminate(process: subprocess.Popen, timeout: float = 15.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)          # main.py's KeyboardInterrupt path
        else:
            process.terminate()                                   # SIGTERM: main.py shuts the server down
        process.wait(timeout)
    except Exception:
        pass
    if process.poll() is None:
        process.kill()
        process.wait(5)


def run(root: Path, python: str, max_restarts: int | None = None) -> int:
    app = root / "app"
    stop_flag = root / "violet.stop"
    stop_flag.unlink(missing_ok=True)
    (root / "violet.pid").write_text(str(os.getpid()), encoding="utf-8")
    env = {**os.environ, "VIOLET_ENV_FILE": str(root / "config" / "violet.env"), "PYTHONUNBUFFERED": "1"}
    backoff, restarts = 2.0, 0
    try:
        while True:
            started = time.monotonic()
            kwargs = {"cwd": str(app), "env": env, "stdin": subprocess.DEVNULL,
                      "stdout": open(root / "logs" / "startup.log", "ab"), "stderr": subprocess.STDOUT}
            if os.name == "nt":
                kwargs["creationflags"] = 0x00000200                 # CREATE_NEW_PROCESS_GROUP (for CTRL_BREAK)
            process = subprocess.Popen([python, "-u", "main.py"], **kwargs)
            log(root, f"started main.py pid={process.pid}")
            while process.poll() is None:
                if stop_flag.exists():
                    log(root, "stop requested")
                    terminate(process)
                    return 0
                time.sleep(0.5)
            code = process.returncode
            log(root, f"main.py exited with {code}")
            if stop_flag.exists():
                return 0
            restarts += 1
            if max_restarts is not None and restarts > max_restarts:
                return code or 1
            if time.monotonic() - started > 600:
                backoff = 2.0
            log(root, f"restarting in {backoff:.0f}s (restart #{restarts})")
            deadline = time.monotonic() + backoff
            while time.monotonic() < deadline:
                if stop_flag.exists():
                    return 0
                time.sleep(0.5)
            backoff = min(60.0, backoff * 2)
    finally:
        (root / "violet.pid").unlink(missing_ok=True)
        stop_flag.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="violet_service")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--max-restarts", type=int)
    args = parser.parse_args(argv)
    return run(args.root.resolve(), args.python, args.max_restarts)


if __name__ == "__main__":
    sys.exit(main())
