"""Tests for polling recovery classification and restart flow."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import NetworkError, TimedOut

from src.bot.polling_recovery import (
    PollingRecoveryManager,
    _is_connect_timeout,
    _is_network_error,
    _is_polling_conflict,
    _LivenessTracker,
    _ReconnectLadder,
)


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
        class ConnectTimeout(Exception):
            pass

        inner = ConnectTimeout("timed out")
        try:
            raise NetworkError("wrapper") from inner
        except NetworkError as exc:
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
        assert ladder.next_backoff() == 5.0
        assert ladder.next_backoff() == 10.0
        assert ladder.next_backoff() == 20.0
        assert ladder.retries == 3

    def test_backoff_capped(self):
        ladder = _ReconnectLadder(
            initial_backoff=5.0, factor=2.0, jitter=0.0, max_backoff=30.0
        )
        backoff = 0.0
        for _ in range(10):
            backoff = ladder.next_backoff()
        assert backoff <= 30.0

    def test_reset(self):
        ladder = _ReconnectLadder()
        ladder.retries = 5
        ladder.reset()
        assert ladder.retries == 0


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
        assert 49 <= tracker.idle_seconds < 52


class TestPollingRecoveryManager:
    def _app(self):
        app = MagicMock()
        app.add_error_handler = MagicMock()
        app.updater.running = True
        app.updater.stop = AsyncMock()
        return app

    @pytest.mark.asyncio
    async def test_start_registers_handler_and_watchdog(self):
        manager = PollingRecoveryManager()
        app = self._app()
        start_polling = AsyncMock()

        await manager.start(app, start_polling=start_polling)

        app.add_error_handler.assert_called_once_with(
            manager.handle_application_error
        )
        assert manager._app is app
        assert manager._start_polling is start_polling
        assert manager._watchdog_task is not None

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_on_post_init_compatibility_starts_manager(self):
        manager = PollingRecoveryManager()
        app = self._app()

        await manager.on_post_init(app)

        app.add_error_handler.assert_called_once_with(
            manager.handle_application_error
        )
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_watchdog(self):
        manager = PollingRecoveryManager()
        await manager.start(self._app())

        assert manager._watchdog_task is not None
        assert not manager._watchdog_task.done()

        await manager.shutdown()
        assert manager._watchdog_task is None

    @pytest.mark.asyncio
    async def test_shutdown_safe_when_no_watchdog(self):
        manager = PollingRecoveryManager()
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_application_error_schedules_reconnect_on_connect_timeout(self):
        manager = PollingRecoveryManager()
        await manager.start(self._app(), start_polling=AsyncMock())

        class ConnectTimeout(Exception):
            pass

        context = MagicMock()
        context.error = ConnectTimeout("timed out")

        manager._reconnect = AsyncMock()
        await manager.handle_application_error(None, context)

        assert manager._reconnect_task is not None
        await manager._reconnect_task
        manager._reconnect.assert_awaited_once()
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_transport_error_schedules_reconnect_on_network_error(self):
        manager = PollingRecoveryManager()
        await manager.start(self._app(), start_polling=AsyncMock())

        manager._reconnect = AsyncMock()
        manager.handle_transport_error(NetworkError("httpx.ConnectError: "))

        assert manager._reconnect_task is not None
        await manager._reconnect_task
        manager._reconnect.assert_awaited_once()
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_non_recoverable_error_is_ignored(self):
        manager = PollingRecoveryManager()
        await manager.start(self._app(), start_polling=AsyncMock())

        manager.handle_transport_error(ValueError("something unrelated"))

        assert manager._reconnect_task is None
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_reconnect_uses_start_polling_callback(self, monkeypatch):
        manager = PollingRecoveryManager()
        app = self._app()
        start_polling = AsyncMock()
        await manager.start(app, start_polling=start_polling)
        monkeypatch.setattr(
            "src.bot.polling_recovery.asyncio.sleep", AsyncMock()
        )
        monkeypatch.setattr(
            "src.bot.polling_recovery._drain_httpx_pool", AsyncMock()
        )

        await manager._reconnect("NetworkError")

        app.updater.stop.assert_awaited()
        start_polling.assert_awaited_once_with(drop_pending_updates=True)
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_reconnect_retries_start_polling_failure(self, monkeypatch):
        manager = PollingRecoveryManager()
        app = self._app()
        start_polling = AsyncMock(side_effect=[NetworkError("temporary"), None])
        await manager.start(app, start_polling=start_polling)
        monkeypatch.setattr(
            "src.bot.polling_recovery.asyncio.sleep", AsyncMock()
        )
        monkeypatch.setattr(
            "src.bot.polling_recovery._drain_httpx_pool", AsyncMock()
        )

        await manager._reconnect("NetworkError")

        assert start_polling.await_count == 2
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_reconnect_guard_prevents_concurrent(self):
        manager = PollingRecoveryManager()
        manager._reconnecting = True

        await manager._reconnect("NetworkError")

    @pytest.mark.asyncio
    async def test_watchdog_restarts_dead_polling_task(self):
        manager = PollingRecoveryManager()
        app = self._app()
        app.updater._polling_task = MagicMock()
        app.updater._polling_task.done.return_value = True
        await manager.start(app, start_polling=AsyncMock())

        manager._reconnect = AsyncMock()
        await manager._check_liveness()

        assert manager._reconnect_task is not None
        await manager._reconnect_task
        manager._reconnect.assert_awaited_once()
        await manager.shutdown()
