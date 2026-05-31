"""Process-wide single-instance guard for the Telegram bot."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import BinaryIO, Optional


if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class SingleInstanceGuard:
    """Hold an OS file lock so only one bot process reaches polling."""

    def __init__(self, lock_path: Path, pid_path: Path) -> None:
        self.lock_path = lock_path
        self.pid_path = pid_path
        self._lock_file: Optional[BinaryIO] = None
        self._acquired = False

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    def acquire(
        self,
        *,
        wait_seconds: float = 30.0,
        poll_interval_seconds: float = 0.25,
    ) -> bool:
        """Acquire the singleton lock, waiting briefly for restarts."""

        deadline = time.monotonic() + max(0.0, wait_seconds)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        while True:
            lock_file = self.lock_path.open("a+b")
            try:
                self._try_lock(lock_file)
            except OSError:
                lock_file.close()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(max(0.05, poll_interval_seconds))
                continue

            self._lock_file = lock_file
            self._acquired = True
            self._write_pid()
            return True

    def release(self) -> None:
        """Release lock and remove the PID file if it belongs to this process."""

        if not self._acquired or self._lock_file is None:
            return

        try:
            self._remove_current_pid_file()
        finally:
            try:
                self._unlock(self._lock_file)
            finally:
                self._lock_file.close()
                self._lock_file = None
                self._acquired = False

    def read_existing_pid(self) -> Optional[int]:
        try:
            raw = self.pid_path.read_text(encoding="utf-8").strip()
            return int(raw) if raw else None
        except Exception:
            return None

    def _write_pid(self) -> None:
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_current_pid_file(self) -> None:
        try:
            if self.read_existing_pid() == os.getpid():
                self.pid_path.unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _try_lock(lock_file: BinaryIO) -> None:
        lock_file.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(lock_file: BinaryIO) -> None:
        lock_file.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
