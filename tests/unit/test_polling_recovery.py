"""Tests for polling recovery: error classification, reconnect ladder, liveness."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import NetworkError, TimedOut

from src.bot.polling_recovery import (
    PollingRecoveryManager,
    _ReconnectLadder,
    _LivenessTracker,
    _is_connect_timeout,
    _is_network_error,
    _is_polling_conflict,
)


# --- Error classification ---


class TestIsPollingConflict:
    def test_conflict_message(self):
        exc = Exception("Conflict: terminated by other getUpdates request")
        assert _is_polling_conflict(exc) is True

    def test_conflict_lowercase(self):
        exc = Exception("conflict: another instance")
        assert _is_polling_conflict(exc) is True

    def test_not_conflict(self):
        exc = Exception("Connection reset")
        assert _is_polling_conflict(exc) is False


class TestIsConnectTimeout:
    def test_direct_connect_timeout_class(self):
        class ConnectTimeout(Exception):
            pass

        exc = ConnectTimeout("timed out")
        assert _is_connect_timeout(exc) is True

    def test_class_name_match(self):
        class MyConnectTimeoutError(Exception):
            pass

        exc = MyConnectTimeoutError("bad")
        assert _is_connect_timeout(exc) is True

    def test_message_match(self):
        exc = Exception("Connect timeout occurred")
        assert _is_connect_timeout(exc) is True

    def test_cause_chain(self):
        inner = Exception("connect timed out")
        try:
            raise NetworkError("wrapper") from inner
        except NetworkError as outer:
            assert _is_connect_timeout(outer) is True

    def test_no_match(self):
        exc = Exception("Connection reset by peer")
        assert _is_connect_timeout(exc) is False


class TestIsNetworkError:
    def test_timed_out(self):
        assert _is_network_error(TimedOut()) is True

    def test_connection_error(self):
        assert _is_network_error(ConnectionError()) is True

    def test_os_error(self):
        assert _is_network_error(OSError("reset")) is True

    def test_network_error(self):
        exc = NetworkError("something")
        assert _is_network_error(exc) is True

    def test_network_error_connect_timeout_excluded(self):
        """NetworkError wrapping ConnectTimeout should be classified as
        connect_timeout, not generic network_error."""

        class ConnectTimeout(Exception):
            pass

        inner = ConnectTimeout("timed out")
        try:
            raise NetworkError("wrapper") from inner
        except NetworkError as exc:
            # _is_connect_timeout returns True, so _is_network_error returns False
            assert _is_network_error(exc) is False

    def test_timeout_in_message(self):
        exc = Exception("request timeout")
        assert _is_network_error(exc) is True

    def test_socket_hang_up(self):
        exc = Exception("socket hang up")
        assert _is_network_error(exc) is True

    def test_non_network(self):
        exc = ValueError("bad value")
        assert _is_network_error(exc) is False


# --- Reconnect ladder ---


class TestReconnectLadder:
    def test_initial_state(self):
        ladder = _ReconnectLadder()
        assert ladder.retries == 0
        assert ladder.exhausted is False

    def test_exhausted_after_max(self):
        ladder = _ReconnectLadder(max_retries=3)
        ladder.retries = 3
        assert ladder.exhausted is True

    def test_backoff_increases(self):
        ladder = _ReconnectLadder(
            initial_backoff=5.0, factor=2.0, jitter=0.0, max_backoff=60.0
        )
        b1 = ladder.next_backoff()
        b2 = ladder.next_backoff()
        b3 = ladder.next_backoff()
        assert b1 == 5.0
        assert b2 == 10.0
        assert b3 == 20.0
        assert ladder.retries == 3

    def test_backoff_capped(self):
        ladder = _ReconnectLadder(
            initial_backoff=5.0, factor=2.0, jitter=0.0, max_backoff=30.0
        )
        for _ in range(10):
            b = ladder.next_backoff()
        assert b <= 30.0

    def test_reset(self):
        ladder = _ReconnectLadder()
        ladder.retries = 5
        ladder.reset()
        assert ladder.retries == 0


# --- Liveness tracker ---


class TestLivenessTracker:
    def test_not_stalled_initially(self):
        tracker = _LivenessTracker()
        assert tracker.stalled(threshold_s=60) is False

    def test_stalled_after_threshold(self):
        tracker = _LivenessTracker()
        tracker.last_activity = time.monotonic() - 200
        assert tracker.stalled(threshold_s=120) is True

    def test_note_activity_resets(self):
        tracker = _LivenessTracker()
        tracker.last_activity = time.monotonic() - 200
        tracker.note_activity()
        assert tracker.stalled(threshold_s=120) is False

    def test_idle_seconds(self):
        tracker = _LivenessTracker()
        tracker.last_activity = time.monotonic() - 50
        assert tracker.idle_seconds >= 49
        assert tracker.idle_seconds < 52


# --- PollingRecoveryManager integration ---


class TestPollingRecoveryManager:
    @pytest.mark.asyncio
    async def test_on_post_init_registers_handler(self):
        manager = PollingRecoveryManager()
        app = MagicMock()
        app.add_error_handler = MagicMock()

        await manager.on_post_init(app)

        app.add_error_handler.assert_called_once_with(manager._error_handler)
        assert manager._app is app

    @pytest.mark.asyncio
    async def test_shutdown_cancels_watchdog(self):
        manager = PollingRecoveryManager()
        app = MagicMock()
        app.add_error_handler = MagicMock()
        await manager.on_post_init(app)

        assert manager._watchdog_task is not None
        assert not manager._watchdog_task.done()

        await manager.shutdown()
        assert manager._watchdog_task is None

    @pytest.mark.asyncio
    async def test_shutdown_safe_when_no_watchdog(self):
        manager = PollingRecoveryManager()
        await manager.shutdown()  # should not raise

    @pytest.mark.asyncio
    async def test_error_handler_schedules_reconnect_on_connect_timeout(self):
        manager = PollingRecoveryManager()

        class ConnectTimeout(Exception):
            pass

        exc = ConnectTimeout("timed out")
        context = MagicMock()
        context.error = exc

        with patch("src.bot.polling_recovery.asyncio.create_task") as mock_task:
            await manager._error_handler(None, context)
            mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_handler_schedules_reconnect_on_network_error(self):
        manager = PollingRecoveryManager()
        exc = TimedOut()
        context = MagicMock()
        context.error = exc

        with patch("src.bot.polling_recovery.asyncio.create_task") as mock_task:
            await manager._error_handler(None, context)
            mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_handler_ignores_polling_conflict(self):
        manager = PollingRecoveryManager()
        exc = Exception("Conflict: terminated by other getUpdates")
        context = MagicMock()
        context.error = exc

        with patch("src.bot.polling_recovery.asyncio.create_task") as mock_task:
            await manager._error_handler(None, context)
            mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_handler_ignores_fatal_error(self):
        manager = PollingRecoveryManager()
        exc = ValueError("something unrelated")
        context = MagicMock()
        context.error = exc

        with patch("src.bot.polling_recovery.asyncio.create_task") as mock_task:
            await manager._error_handler(None, context)
            mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnect_guard_prevents_concurrent(self):
        manager = PollingRecoveryManager()
        manager._reconnecting = True
        # Should return immediately, not attempt reconnect
        await manager._reconnect("test")
        # No assertion needed — just verify it doesn't error

    @pytest.mark.asyncio
    async def test_reconnect_exhausted_exits(self):
        manager = PollingRecoveryManager()
        manager._ladder.retries = 10  # exhausted

        with patch("os._exit") as mock_exit:
            await manager._reconnect("test")
            mock_exit.assert_called_once_with(1)
