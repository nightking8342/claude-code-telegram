"""Polling recovery: error classification, reconnect ladder, liveness watchdog.

Prevents bot hangs when Telegram long-polling gets stuck on network errors
(ConnectTimeout, stale TCP connections, etc.) by:
1. Classifying errors as recoverable vs fatal
2. Exponential-backoff reconnection (stop → drain pool → restart)
3. Background watchdog detecting stuck polling even without error signals
"""

import asyncio
import random
import time
from dataclasses import dataclass, field

import structlog
from telegram.error import NetworkError, TimedOut

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WATCHDOG_INTERVAL_S = 30
_STALL_THRESHOLD_S = 120
_MAX_RECONNECT_RETRIES = 10
_INITIAL_BACKOFF_S = 5.0
_MAX_BACKOFF_S = 60.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_JITTER = 0.5
_RECONNECT_STOP_TIMEOUT_S = 15.0
_RECONNECT_DRAIN_S = 2.0


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _is_polling_conflict(exc: BaseException) -> bool:
    """Detect 'Conflict: terminated by other getUpdates'."""
    msg = str(exc).lower()
    return "conflict" in msg or "terminated by other getupdates" in msg


def _is_network_error(exc: BaseException) -> bool:
    """Detect recoverable network errors (excluding ConnectTimeout)."""
    if isinstance(exc, (TimedOut, ConnectionError, OSError)):
        return True
    if isinstance(exc, NetworkError):
        if _is_connect_timeout(exc):
            return False
        return True
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


# ---------------------------------------------------------------------------
# Reconnect ladder (exponential backoff)
# ---------------------------------------------------------------------------


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
            self.initial_backoff * (self.factor ** self.retries),
            self.max_backoff,
        )
        spread = base * self.jitter
        delay = base + random.uniform(-spread, spread)
        self.retries += 1
        return max(0.5, delay)

    def reset(self) -> None:
        self.retries = 0


# ---------------------------------------------------------------------------
# Liveness tracker
# ---------------------------------------------------------------------------


@dataclass
class _LivenessTracker:
    """Track polling activity timestamps for stall detection."""

    last_activity: float = field(default_factory=time.monotonic)

    def note_activity(self) -> None:
        self.last_activity = time.monotonic()

    def stalled(self, threshold_s: float = _STALL_THRESHOLD_S) -> bool:
        return (time.monotonic() - self.last_activity) > threshold_s

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity


# ---------------------------------------------------------------------------
# PollingRecoveryManager — public facade
# ---------------------------------------------------------------------------


class PollingRecoveryManager:
    """Manages polling recovery lifecycle.

    Usage::

        recovery = PollingRecoveryManager(app)
        # During Application builder (before build):
        builder.post_init(recovery.on_post_init)
        # On shutdown:
        await recovery.shutdown()
    """

    def __init__(self) -> None:
        self._app = None
        self._ladder = _ReconnectLadder()
        self._liveness = _LivenessTracker()
        self._watchdog_task: asyncio.Task | None = None
        self._reconnecting = False

    async def on_post_init(self, app) -> None:
        """Called by PTB after Application.initialize()."""
        self._app = app
        app.add_error_handler(self._error_handler)
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info(
            "Polling recovery active",
            stall_threshold_s=_STALL_THRESHOLD_S,
            watchdog_interval_s=_WATCHDOG_INTERVAL_S,
        )

    async def shutdown(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

    # --- error handler (registered via add_error_handler) ---

    async def _error_handler(self, update, context) -> None:
        exc = context.error
        if exc is None:
            return

        # Log every error
        logger.error(
            "Polling error",
            error=str(exc),
            error_type=type(exc).__name__,
            update_type=type(update).__name__ if update else None,
        )

        # Classify
        if _is_polling_conflict(exc):
            logger.warning("Polling conflict — another instance?")
            return

        if _is_connect_timeout(exc):
            logger.warning("ConnectTimeout detected, scheduling reconnect")
            self._liveness.note_activity()  # reset watchdog timer
            asyncio.create_task(self._reconnect("connect_timeout"))
            return

        if _is_network_error(exc):
            logger.warning("Network error detected, scheduling reconnect")
            self._liveness.note_activity()
            asyncio.create_task(self._reconnect("network_error"))
            return

        # Fatal / unknown — just log (done above), no reconnect
        logger.error("Non-recoverable polling error", error=str(exc))

    # --- reconnect ---

    async def _reconnect(self, reason: str) -> None:
        if self._reconnecting:
            return
        self._reconnecting = True

        try:
            if self._ladder.exhausted:
                logger.error(
                    "Reconnect retries exhausted, restarting process",
                    retries=self._ladder.retries,
                )
                import os

                os._exit(1)

            delay = self._ladder.next_backoff()
            logger.info(
                "Reconnecting polling",
                reason=reason,
                attempt=self._ladder.retries,
                backoff_s=f"{delay:.1f}",
            )
            await asyncio.sleep(delay)

            updater = self._app.updater
            if updater is None:
                return

            # Stop
            try:
                await asyncio.wait_for(
                    updater.stop(), timeout=_RECONNECT_STOP_TIMEOUT_S
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("Error stopping updater during reconnect", error=str(e))

            # Drain stale connections
            await _drain_httpx_pool(self._app)

            # Restart
            await updater.start_polling(
                allowed_updates=None,
                drop_pending_updates=False,
            )
            self._liveness.note_activity()
            logger.info("Polling restarted successfully")
        except Exception:
            logger.exception("Reconnect failed")
        finally:
            self._reconnecting = False

    # --- watchdog ---

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL_S)
            try:
                await self._check_liveness()
            except Exception:
                logger.exception("Watchdog check failed")

    async def _check_liveness(self) -> None:
        updater = self._app.updater
        if updater is None or not updater.running:
            return

        if self._reconnecting:
            return

        if self._liveness.stalled():
            logger.warning(
                "Polling stall detected",
                idle_s=f"{self._liveness.idle_seconds:.0f}",
                threshold_s=_STALL_THRESHOLD_S,
            )
            asyncio.create_task(self._reconnect("stall_detected"))
            return

        # Heartbeat: verify long-poll task is alive
        if hasattr(updater, "_polling_task") and updater._polling_task is not None:
            if updater._polling_task.done():
                logger.warning("Long-poll task is dead, restarting")
                asyncio.create_task(self._reconnect("poll_task_dead"))


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
