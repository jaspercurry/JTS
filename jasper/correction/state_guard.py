"""State-transition guards for room-correction measurement sessions."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from typing import Any, AsyncContextManager, Generic, TypeVar

from ..log_event import log_event

StateT = TypeVar("StateT")


class SessionStateGuard(Generic[StateT]):
    """Owns timeout/reset guards around a session's state transitions."""

    def __init__(
        self,
        *,
        session_id: str,
        capture_timeout_states: Collection[StateT],
        reset_busy_states: Collection[StateT],
        capture_timeout_sec: float,
        get_state: Callable[[], StateT],
        lock_factory: Callable[[], AsyncContextManager[Any]],
        fail: Callable[[str], Awaitable[None]],
        state_label: Callable[[StateT], str],
        logger: logging.Logger,
    ) -> None:
        self.session_id = session_id
        self.capture_timeout_states = frozenset(capture_timeout_states)
        self.reset_busy_states = frozenset(reset_busy_states)
        self.capture_timeout_sec = capture_timeout_sec
        self._get_state = get_state
        self._lock_factory = lock_factory
        self._fail = fail
        self._state_label = state_label
        self._logger = logger
        self._capture_timeout_task: asyncio.Task[None] | None = None

    def is_reset_busy(self, state: StateT) -> bool:
        return state in self.reset_busy_states

    def on_transition(self, state: StateT) -> None:
        """Refresh the stranded-capture watchdog for a new session state."""
        self.cancel_capture_timeout()
        if state in self.capture_timeout_states:
            self._arm_capture_timeout(state)

    def cancel_capture_timeout(self) -> None:
        task = self._capture_timeout_task
        self._capture_timeout_task = None
        if task is not None and not task.done():
            task.cancel()

    def _arm_capture_timeout(self, state: StateT) -> None:
        if self.capture_timeout_sec <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._capture_timeout_task = loop.create_task(
            self._capture_timeout_guard(state, self.capture_timeout_sec)
        )

    async def _capture_timeout_guard(
        self, expected_state: StateT, timeout_sec: float,
    ) -> None:
        try:
            await asyncio.sleep(timeout_sec)
        except asyncio.CancelledError:
            return
        # Detached task: catch and log every failure so asyncio never emits an
        # "exception was never retrieved" warning while the session is wedged.
        try:
            async with self._lock_factory():
                # Own the slot before calling _fail(), whose first step cancels
                # any pending timeout. This mirrors the pre-extraction behavior.
                self._capture_timeout_task = None
                if self._get_state() != expected_state:
                    return
                log_event(
                    self._logger,
                    "correction_capture_timeout",
                    session=self.session_id,
                    state=self._state_label(expected_state),
                    after_sec=f"{timeout_sec:.0f}",
                    level=logging.WARNING,
                )
                await self._fail(
                    "capture never arrived — tap Start to measure again"
                )
        except Exception:  # noqa: BLE001
            self._logger.exception(
                "capture-timeout guard failed (session=%s)", self.session_id,
            )
