# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Log a steady-state fault on its TRANSITION, not on every poll.

A machine fault that persists -- a lock no account can open, a record that will
not parse -- is one incident, not one per read. A reader polled every second or
two would otherwise turn a single condition into hundreds of identical ERROR
lines, burying the transition that actually happened. This is the mechanism
behind ADR-0196; ``output_topology`` grew the first copy and this is the shared
one it and the crossover level-run poller now both consume, rather than a third
hand-rolled variant.

The gate logs when the ``(key, signature)`` first appears and again only once a
reminder window has lapsed, so a fault that is still true hours later is
re-stated once rather than never. It is thread-safe (the correction web server
is a ``ThreadingHTTPServer``) and bounded (an adversarial spread of keys evicts
the stalest, never the current one).
"""
from __future__ import annotations

import errno
import threading
import time
from typing import Callable


class TransitionLog:
    """Per-key transition-or-due-reminder gate for a persistent fault."""

    def __init__(
        self,
        *,
        reminder_sec: float,
        max_keys: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # reminder_sec: re-state an unchanged fault once this long has passed.
        self._reminder_sec = reminder_sec
        self._max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (signature, monotonic time it was last logged), ordered
        # least-recently-logged first so eviction drops the stalest entry.
        self._state: dict[str, tuple[str, float]] = {}

    def should_log(self, key: str, signature: str) -> bool:
        """Whether this fault is a transition or a due reminder, not a repeat."""

        now = self._clock()
        with self._lock:
            previous = self._state.get(key)
            if (
                previous is not None
                and previous[0] == signature
                and now - previous[1] < self._reminder_sec
            ):
                return False
            # Re-insert rather than assign so dict order stays recency order.
            self._state.pop(key, None)
            self._state[key] = (signature, now)
            while len(self._state) > self._max_keys:
                self._state.pop(next(iter(self._state)))
            return True

    def cleared(self, key: str) -> bool:
        """Whether ``key`` just recovered from a fault that was logged."""

        with self._lock:
            return self._state.pop(key, None) is not None

    def tracked(self) -> int:
        """How many keys are currently held; never exceeds ``max_keys``."""

        with self._lock:
            return len(self._state)


def os_fault_cause(exc: BaseException) -> str:
    """Name a fault the way an operator acts on it: class, errno code, path.

    Walks ``__cause__`` for the deepest OS error -- a permission fault on one
    file arrives several wrapper links down -- and renders ``class:CODE:path``.
    Falls back to the exception's own class when no OS error is under it.
    """

    seen: set[int] = set()
    os_error: OSError | None = None
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, OSError):
            os_error = cursor
        cursor = cursor.__cause__
    if os_error is None:
        return type(exc).__name__
    code = errno.errorcode.get(os_error.errno or 0, str(os_error.errno or ""))
    return f"{type(os_error).__name__}:{code}:{os_error.filename or ''}"
