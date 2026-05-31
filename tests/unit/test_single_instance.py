"""Tests for the process single-instance guard."""

import os

from src.utils.single_instance import SingleInstanceGuard


def test_single_instance_guard_blocks_second_holder(tmp_path):
    lock_path = tmp_path / "bot.lock"
    pid_path = tmp_path / "bot.pid"
    first = SingleInstanceGuard(lock_path, pid_path)
    second = SingleInstanceGuard(lock_path, pid_path)

    try:
        assert first.acquire(wait_seconds=0)
        assert pid_path.read_text(encoding="utf-8") == str(os.getpid())
        assert not second.acquire(wait_seconds=0)
        assert second.read_existing_pid() == os.getpid()
    finally:
        second.release()
        first.release()


def test_single_instance_guard_releases_lock_and_pid(tmp_path):
    lock_path = tmp_path / "bot.lock"
    pid_path = tmp_path / "bot.pid"
    first = SingleInstanceGuard(lock_path, pid_path)
    second = SingleInstanceGuard(lock_path, pid_path)

    assert first.acquire(wait_seconds=0)
    first.release()

    try:
        assert not pid_path.exists()
        assert second.acquire(wait_seconds=0)
        assert pid_path.read_text(encoding="utf-8") == str(os.getpid())
    finally:
        second.release()
