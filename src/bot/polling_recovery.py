"""Unified Telegram polling recovery for manually managed bot lifecycles."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog
from telegram.error import NetworkError, RetryAfter, TimedOut

logger = structlog.get_logger()

_WATCHDOG_INTERVAL_S = 30
_MAX_RECONNECT_RETRIES = 10
_INITIAL_BACKOFF_S = 5.0
_MAX_BACKOFF_S = 60.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_JITTER = 0.5
_RECONNECT_STOP_TIMEOUT_S = 15.0
_RECONNECT_DRAIN_S = 2.0
_SLOW_RETRY_INTERVAL_S = 30.0


def _is_polling_conflict(exc: BaseException) -> bool:
    """Detect 'Conflict: terminated by other getUpdates'."""
    msg = str(exc).lower()
    return "conflict" in msg or "terminated by other getupdates" in msg


def _is_network_error(exc: BaseException) -> bool:
    """Detect recoverable network errors, excluding connect timeouts."""
    if isinstance(exc, (TimedOut, ConnectionError, OSError)):
        return True
    if isinstance(exc, NetworkError):
        return not _is_connect_timeout(exc)

    name = type(exc).__name__.lower()
    if name in ("timedout", "timeouterror", "connectionerror"):
        return True

    msg = str(exc).lower()
    if "timeout" in msg or "socket hang up" in msg:
        return True
    return False


def _is_connect_timeout(exc: BaseException) -> bool:
    """Walk the exception chain looking for ConnectTimeout signatures."""
    current: BaseException | None = exc
    while current is not None:
        name = type(current).__name__.lower()
        msg = str(current).lower()
        if "connecttimeout" in name:
            return True
        if "connect timeout" in msg or "connect timed out" in msg:
            return True
        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
    return False


@dataclass
class _ReconnectLadder:
    """Track consecutive reconnection attempts with exponential backoff."""

    retries: int = 0
    max_retries: int = _MAX_RECONNECT_RETRIES
    initial_backoff: float = _INITIAL_BACKOFF_S
    max_backoff: float = _MAX_BACKOFF_S
    factor: float = _BACKOFF_FACTOR
    jitter: float = _BACKOFF_JITTER

    @property
    def exhausted(self) -> bool:
        return self.retries >= self.max_retries

    def next_backoff(self) -> float:
        base = min(
            self.initial_backoff * (self.factor**self.retries),
            self.max_backoff,
        )
        spread = base * self.jitter
        delay = base + random.uniform(-spread, spread)
        self.retries += 1
        return max(0.5, delay)

    def reset(self) -> None:
        self.retries = 0


@dataclass
class _LivenessTracker:
    """Small timestamp helper kept for diagnostics and focused tests."""

    last_activity: float = field(default_factory=time.monotonic)

    def note_activity(self) -> None:
        self.last_activity = time.monotonic()

    def stalled(self, threshold_s: float) -> bool:
        return (time.monotonic() - self.last_activity) > threshold_s

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity


class PollingRecoveryManager:
    """Recover Telegram long-polling transport failures in one place."""

    def __init__(self) -> None:
        self._app = None
        self._start_polling: Callable[..., Awaitable[None]] | None = None
        self._ladder = _ReconnectLadder()
        self._liveness = _LivenessTracker()
        self._watchdog_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._error_handler_registered = False
        self._reconnecting = False
        self._slow_retrying = False
        self._slow_retry_count = 0
        self._next_retry_at: float | None = None
        self._last_reason: str | None = None
        self._last_error: str | None = None
        self._last_error_type: str | None = None
        self._last_recovered_at: float | None = None

    async def start(
        self,
        app,
        *,
        start_polling: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        """Attach recovery to an initialized Application."""
        self._app = app
        self._start_polling = start_polling

        if not self._error_handler_registered:
            app.add_error_handler(self.handle_application_error)
            self._error_handler_registered = True

        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        logger.info(
            "Polling recovery active",
            watchdog_interval_s=_WATCHDOG_INTERVAL_S,
        )

    async def on_post_init(self, app) -> None:
        """Compatibility hook for PTB run_polling/run_webhook users."""
        await self.start(app)

    async def shutdown(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

    def handle_transport_error(self, exc: BaseException) -> None:
        """Schedule recovery for Updater.start_polling transport faults."""
        logger.warning(
            "Polling transport error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        self._schedule_reconnect(exc)

    async def handle_application_error(self, update, context) -> None:
        """Observe Application errors and recover only when they are transport-like."""
        exc = context.error
        if exc is None:
            return

        logger.error(
            "Application polling error",
            error=str(exc),
            error_type=type(exc).__name__,
            update_type=type(update).__name__ if update else None,
        )
        self._schedule_reconnect(exc)

    def _schedule_reconnect(self, exc: BaseException) -> None:
        reason = self._classify_recovery_reason(exc)
        if reason is None:
            logger.debug(
                "Ignoring non-recoverable polling error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        self._liveness.note_activity()
        self._last_reason = reason
        self._last_error = str(exc)
        self._last_error_type = type(exc).__name__
        if self._reconnect_task and not self._reconnect_task.done():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Cannot recover polling: no running event loop")
            return

        self._reconnect_task = loop.create_task(self._reconnect(reason, exc))

    def _classify_recovery_reason(self, exc: BaseException) -> str | None:
        if isinstance(exc, RetryAfter):
            return "RetryAfter"
        if _is_polling_conflict(exc):
            return "Conflict"
        if _is_connect_timeout(exc):
            return "connect_timeout"
        if _is_network_error(exc):
            return "NetworkError"

        msg = str(exc).lower()
        if (
            "connecterror" in msg
            or "networkerror" in msg
            or "remoteprotocolerror" in msg
            or "polling task dead" in msg
            or "polling updater stopped" in msg
            or "server disconnected without sending a response" in msg
        ):
            return "NetworkError"
        return None

    async def _reconnect(
        self, reason: str, exc: BaseException | None = None
    ) -> None:
        if self._reconnecting:
            return
        self._reconnecting = True
        self._last_reason = reason
        if exc is not None:
            self._last_error = str(exc)
            self._last_error_type = type(exc).__name__

        try:
            await self._initial_reconnect_delay(reason, exc)
            while not self._ladder.exhausted:
                attempt = self._ladder.retries + 1
                logger.warning(
                    "Recovering Telegram polling",
                    reason=reason,
                    attempt=attempt,
                    max_attempts=self._ladder.max_retries,
                )

                if await self._try_restart_polling(reason):
                    self._ladder.reset()
                    self._slow_retrying = False
                    self._slow_retry_count = 0
                    self._last_recovered_at = time.time()
                    logger.info(
                        "Telegram polling recovered",
                        reason=reason,
                        attempt=attempt,
                    )
                    return

                delay = self._ladder.next_backoff()
                self._next_retry_at = time.monotonic() + delay
                logger.warning(
                    "Telegram polling recovery attempt failed",
                    reason=reason,
                    attempt=attempt,
                    backoff_s=f"{delay:.1f}",
                )
                await asyncio.sleep(delay)
                self._next_retry_at = None

            logger.error(
                "Failed to recover Telegram polling; entering slow retry",
                reason=reason,
                attempts=self._ladder.retries,
                slow_retry_interval_s=_SLOW_RETRY_INTERVAL_S,
            )

            self._slow_retrying = True
            while True:
                self._next_retry_at = time.monotonic() + _SLOW_RETRY_INTERVAL_S
                await asyncio.sleep(_SLOW_RETRY_INTERVAL_S)
                self._next_retry_at = None
                self._slow_retry_count += 1

                logger.warning(
                    "Slow retrying Telegram polling",
                    reason=reason,
                    slow_attempt=self._slow_retry_count,
                )
                if await self._try_restart_polling(reason):
                    self._ladder.reset()
                    self._slow_retrying = False
                    self._slow_retry_count = 0
                    self._last_recovered_at = time.time()
                    logger.info(
                        "Telegram polling recovered",
                        reason=reason,
                        attempt="slow",
                    )
                    return
        finally:
            self._reconnecting = False
            self._slow_retrying = False
            self._next_retry_at = None
            self._reconnect_task = None

    def get_status(self) -> dict:
        """Return a serializable snapshot of polling recovery state."""
        updater = self._app.updater if self._app is not None else None
        polling_running = (
            bool(getattr(updater, "running", False)) if updater else False
        )
        polling_task = getattr(updater, "_polling_task", None) if updater else None
        polling_task_done = (
            bool(polling_task.done()) if polling_task is not None else None
        )
        reconnect_task_running = bool(
            self._reconnect_task and not self._reconnect_task.done()
        )
        next_retry_seconds = None
        if self._next_retry_at is not None:
            next_retry_seconds = max(0, round(self._next_retry_at - time.monotonic()))

        if self._slow_retrying:
            recovery_state = "slow_retrying"
        elif self._reconnecting:
            recovery_state = "reconnecting"
        else:
            recovery_state = "idle"

        return {
            "polling_running": polling_running,
            "polling_task_done": polling_task_done,
            "recovery_state": recovery_state,
            "reconnect_task_running": reconnect_task_running,
            "retries": self._ladder.retries,
            "max_retries": self._ladder.max_retries,
            "exhausted": self._ladder.exhausted,
            "slow_retrying": self._slow_retrying,
            "slow_retry_count": self._slow_retry_count,
            "slow_retry_interval_seconds": _SLOW_RETRY_INTERVAL_S,
            "next_retry_seconds": next_retry_seconds,
            "last_reason": self._last_reason,
            "last_error": self._last_error,
            "last_error_type": self._last_error_type,
            "last_recovered_at": self._last_recovered_at,
        }

    async def _initial_reconnect_delay(
        self, reason: str, exc: BaseException | None
    ) -> None:
        if isinstance(exc, RetryAfter):
            wait_seconds = int(exc.retry_after) + 2
            logger.warning(
                "Telegram rate limit - waiting before recovery",
                retry_after_s=exc.retry_after,
                total_wait_s=wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
        elif reason == "Conflict":
            await asyncio.sleep(6)
        else:
            await asyncio.sleep(3)

    async def _try_restart_polling(self, reason: str) -> bool:
        updater = self._app.updater if self._app is not None else None
        if updater is None:
            return False

        try:
            if updater.running:
                await asyncio.wait_for(
                    updater.stop(), timeout=_RECONNECT_STOP_TIMEOUT_S
                )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Error stopping updater during reconnect", error=str(exc))

        await _drain_httpx_pool(self._app)

        try:
            if self._start_polling is not None:
                await self._start_polling(drop_pending_updates=True)
            else:
                await updater.start_polling(
                    allowed_updates=None,
                    drop_pending_updates=(reason != "Conflict"),
                    error_callback=self.handle_transport_error,
                )
            return True
        except Exception as exc:
            logger.warning("Error starting updater during reconnect", error=str(exc))
            return False

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL_S)
            try:
                await self._check_liveness()
            except Exception:
                logger.exception("Watchdog check failed")

    async def _check_liveness(self) -> None:
        updater = self._app.updater if self._app is not None else None
        if updater is None or self._reconnecting:
            return

        if not updater.running:
            logger.warning("Polling updater is stopped, restarting")
            self._schedule_reconnect(RuntimeError("polling updater stopped"))
            return

        polling_task = getattr(updater, "_polling_task", None)
        if polling_task is not None and polling_task.done():
            logger.warning("Long-poll task is dead, restarting")
            self._schedule_reconnect(RuntimeError("polling task dead"))


async def _drain_httpx_pool(app) -> None:
    """Close idle httpx connections to clear stale sockets."""
    try:
        if hasattr(app, "bot") and hasattr(app.bot, "_local"):
            local = app.bot._local
            if hasattr(local, "_http_session"):
                session = local._http_session
                if session and not session.is_closed:
                    await session.aclose()
                    logger.debug("Drained httpx connection pool")
        await asyncio.sleep(_RECONNECT_DRAIN_S)
    except Exception:
        logger.debug("Connection drain skipped", exc_info=True)
