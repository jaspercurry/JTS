# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small, dependency-free retry schedules shared across subsystems."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable


RECONNECT_INITIAL_BACKOFF_SEC = 1.0
RECONNECT_MAX_BACKOFF_SEC = 60.0
RECONNECT_BACKOFF_JITTER_FRACTION = 0.25


def reconnect_backoff_delay(attempt: int) -> float:
    """Return a capped exponential reconnect delay with ±25% jitter.

    ``attempt`` is one-indexed. The exponent is saturated because callers may
    retry forever; after the cap is reached, larger powers cannot affect the
    result and should not grow without bound.
    """
    shift = min(attempt - 1, 32)
    base = min(
        RECONNECT_INITIAL_BACKOFF_SEC * (2 ** shift),
        RECONNECT_MAX_BACKOFF_SEC,
    )
    return _jittered(base)


def _jittered(base: float) -> float:
    """Spread ``base`` by ±``RECONNECT_BACKOFF_JITTER_FRACTION``.

    Speakers sharing one provider key fail together, so every delay in
    this module is jittered — otherwise a household polls in lockstep.
    """
    jitter = base * RECONNECT_BACKOFF_JITTER_FRACTION
    return base + random.uniform(-jitter, jitter)


# A terminal failure (revoked key, blocked account, missing model) cannot
# be fixed by retrying, so hammering the 60 s cap only burns the provider's
# rate budget. Poll slowly instead of parking: an operator who adds credit
# or fixes the key recovers within one interval, with no restart.
# See issue #3855.
TERMINAL_POLL_INTERVAL_SEC = 900.0


def reconnect_delay(attempt: int, *, transient: bool) -> float:
    """Return the delay before the next reconnect attempt.

    ``transient`` is the classification of the failure that ended the
    previous attempt (:func:`jasper.voice._supervisor.is_transient`).
    Transient keeps the exponential schedule; terminal drops to the
    slow poll.
    """
    if not transient:
        return _jittered(TERMINAL_POLL_INTERVAL_SEC)
    return reconnect_backoff_delay(attempt)


class ReconnectNudge:
    """Rate gate for caller-requested early reconnects.

    A wake word while the connection slow-polls a terminal outage is the
    household asking "is it fixed yet?", and should shorten the wait.
    The gate stops repeated asks from retrying the provider faster than
    an ordinary network blip already would (the transient ramp's cap).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: float | None = None

    def allow(self) -> bool:
        """True (and arms the gate) when another early retry is due."""
        now = self._clock()
        if (
            self._last is not None
            and now - self._last < RECONNECT_MAX_BACKOFF_SEC
        ):
            return False
        self._last = now
        return True


async def sleep_or_nudge(
    delay: float,
    nudge: asyncio.Event,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Wait ``delay`` seconds, or until ``nudge`` is set — first wins.

    The caller owns clearing ``nudge``: clearing it here would drop a
    request that arrived while the caller was between waits, which is
    exactly when a long connect attempt is in flight.
    """
    sleeper = asyncio.ensure_future(sleep(delay))
    waiter = asyncio.ensure_future(nudge.wait())
    try:
        await asyncio.wait(
            (sleeper, waiter), return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        sleeper.cancel()
        waiter.cancel()
        await asyncio.gather(sleeper, waiter, return_exceptions=True)
