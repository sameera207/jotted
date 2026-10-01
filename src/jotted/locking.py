"""One source job at a time, across threads and processes.

`jotted serve` checks the source in the background while a CLI command may collect or
publish by hand; two rmapi processes at once block each other. A lock file next to the
database is held (flock on macOS and Linux, msvcrt on Windows) for the length of a job.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


class Busy(Exception):
    """The lock wasn't free in time: another job (maybe another Jotted process) is running."""


class SourceLock:
    def __init__(self, path: Path):
        self.path = path
        self._thread = threading.Lock()
        self._fd: int | None = None

    def acquire(self, timeout: float | None = None) -> bool:
        """Wait up to `timeout` seconds (None: forever, 0: don't wait). False if still taken."""
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._thread.acquire(timeout=-1 if timeout is None else max(0.0, timeout)):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            try:
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                self._fd = fd
                return True
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(fd)
                    self._thread.release()
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)
        self._thread.release()

    def __enter__(self) -> "SourceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def held(self, timeout: float | None, what: str = "The source") -> "_Held":
        """`with lock.held(60):` raises Busy instead of waiting forever (None: wait forever)."""
        return _Held(self, timeout, what)


class _Held:
    def __init__(self, lock: SourceLock, timeout: float | None, what: str):
        self.lock, self.timeout, self.what = lock, timeout, what

    def __enter__(self) -> SourceLock:
        if not self.lock.acquire(self.timeout):
            raise Busy(f"{self.what} is busy with another job (maybe `jotted serve` checking it); "
                       "try again in a moment")
        return self.lock

    def __exit__(self, *exc) -> None:
        self.lock.release()
