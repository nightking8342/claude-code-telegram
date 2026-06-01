"""Selective-concurrency update processor for PTB.

Regular updates (messages, commands) process sequentially -- one at a time.
Priority callbacks (stop:*, auq:*, plan:*) bypass the queue and run immediately so
they can interrupt the currently-running handler or resolve an
AskUserQuestion/plan-mode hook Future.
"""

import asyncio
import re
from typing import Any, Awaitable, Set

from telegram import Update
from telegram.ext._baseupdateprocessor import BaseUpdateProcessor


class StopAwareUpdateProcessor(BaseUpdateProcessor):
    """Update processor that lets priority callbacks bypass sequential processing.

    PTB calls ``process_update(update, coroutine)`` for every incoming update.
    The base class holds a semaphore (max 256) then calls our
    ``do_process_update()``.

    For priority callbacks (``stop:*``, ``auq:*``, ``plan:*``): we just ``await coroutine``
    -- runs immediately.
    For text messages from users in ``auq_other_waiting``: also bypass the lock
    so the answer can resolve the hook Future while Claude is still running.
    For everything else: we acquire ``_sequential_lock`` first -- only one
    runs at a time.
    """

    _PRIORITY_PREFIXES = ("stop:", "auq:", "plan:")
    _BTW_COMMAND_RE = re.compile(
        r"^/btw(?:@[A-Za-z0-9_]+)?(?:$|\s|[^A-Za-z0-9_])",
        re.IGNORECASE,
    )

    # User IDs currently waiting for free-text "Other" answer.
    # Populated by the orchestrator's ``_handle_auq_callback`` when the user
    # clicks the "Other" button; consumed by ``agentic_text`` to resolve
    # the hook Future.  Using a class-level set so the orchestrator and
    # processor share state without a circular import.
    auq_other_waiting: Set[int] = set()
    plan_feedback_waiting: Set[int] = set()

    def __init__(self) -> None:
        # High limit so priority callbacks are never blocked by semaphore
        super().__init__(max_concurrent_updates=256)
        self._sequential_lock = asyncio.Lock()

    @classmethod
    def _is_priority_callback(cls, update: object) -> bool:
        """Return True if the update is a priority callback query."""
        if not isinstance(update, Update):
            return False
        cb = update.callback_query
        return (
            cb is not None
            and cb.data is not None
            and cb.data.startswith(cls._PRIORITY_PREFIXES)
        )

    @classmethod
    def _is_auq_other_reply(cls, update: object) -> bool:
        """Return True if this is a text reply to an 'Other' question."""
        if not isinstance(update, Update):
            return False
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return False
        return (
            hasattr(msg, "text")
            and msg.text is not None
            and user.id in cls.auq_other_waiting
        )

    @classmethod
    def _is_plan_feedback_reply(cls, update: object) -> bool:
        """Return True if this is a text reply to an ExitPlanMode feedback prompt."""
        if not isinstance(update, Update):
            return False
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return False
        return (
            hasattr(msg, "text")
            and msg.text is not None
            and user.id in cls.plan_feedback_waiting
        )

    @classmethod
    def _is_btw_command(cls, update: object) -> bool:
        """Return True for /btw commands that should run immediately."""
        if not isinstance(update, Update):
            return False
        msg = update.effective_message
        text = getattr(msg, "text", None) if msg is not None else None
        return isinstance(text, str) and cls._BTW_COMMAND_RE.match(text) is not None

    async def do_process_update(
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        """Process an update, applying sequential lock for non-priority updates."""
        if (
            self._is_priority_callback(update)
            or self._is_auq_other_reply(update)
            or self._is_plan_feedback_reply(update)
            or self._is_btw_command(update)
        ):
            # Run immediately -- no sequential lock
            await coroutine
        else:
            # One at a time for everything else
            async with self._sequential_lock:
                await coroutine

    async def initialize(self) -> None:
        """Initialize the processor (no-op)."""

    async def shutdown(self) -> None:
        """Shutdown the processor (no-op)."""
