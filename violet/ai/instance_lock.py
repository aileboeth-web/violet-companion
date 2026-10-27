"""One Violet per data directory (phase 10.7).

Two processes on the same data (a service plus a manual ``python main.py``, an old start.sh next to a new install)
would both poll the chat platforms, answer every message twice and run two proactive schedulers. ``main.py`` takes
an exclusive OS lock on <data_dir>/violet.lock before starting anything; a second process exits with a clear message.
The lock is released by the OS when the process ends, also after a crash (no stale lock file to clean up).
"""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.path = Path(data_dir) / "violet.lock"
        self._fh = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fh.seek(0)
                holder = fh.read().strip()[:20]
            except OSError:
                holder = ""                       # Windows: the locked byte cannot be read by others
            fh.close()
            raise AlreadyRunning(f"另一个 Violet 正在使用这个数据目录（{self.path.parent}，进程 {holder or '未知'}）。"
                                 "同一份数据只能运行一个 Violet：先停止它（violet stop / systemctl stop violet），再启动。") from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
