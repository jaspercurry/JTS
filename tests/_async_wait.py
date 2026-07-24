# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded `asyncio.Event` waits for race and cancellation tests.

Concurrency tests hand control between the test body and a task it
started by way of an `asyncio.Event`: the task signals "I am at the
interesting point", the test body waits for that signal, then drives the
race. The wait is written `await started.wait()`, which is unbounded.

That is fine only while the producing task is guaranteed to reach its
`set()`. It is not guaranteed. A producer that dies first — a
`dsp_writer_lock(timeout_s=0.5)` whose budget is missed because the box
is loaded, say — never signals, so the test body blocks forever. Two
things then go wrong at once: the suite hangs with no failing test to
point at, and the producer's real exception is swallowed, because it is
held on a task nobody ever awaits.

That is not hypothetical. `test_cancelling_contended_owner_...` in
tests/test_dsp_apply.py hangs exactly this way when its contending owner
loses the 0.5 s lock race, which is reachable on a loaded macOS dev box
and not on CI's quieter Linux runners.

`wait_signalled` bounds the wait and, when a producer is named, reports
that producer's exception as the cause — so the failure says
"DspWriterLockTimeout" instead of hanging, or timing out with nothing
but a bare `TimeoutError` to go on.

The timeout is a hang-breaker, not a timing assertion. Keep it far above
any budget the test is actually asserting; the test's own assertions are
what pin timing promises.
"""

from __future__ import annotations

import asyncio

#: Generous by design — every coordination point these tests use settles
#: in milliseconds when it settles at all, so anything near this bound
#: means "stranded", not "slow". Large enough that a loaded box does not
#: turn a passing test red.
DEFAULT_SIGNAL_TIMEOUT_S = 10.0


def _producer_failure(producer: asyncio.Task | None) -> BaseException | None:
    """Return the exception a stranded wait's producer died with, if any."""

    if producer is None or not producer.done() or producer.cancelled():
        return None
    return producer.exception()


async def wait_signalled(
    event: asyncio.Event,
    what: str,
    *,
    timeout: float = DEFAULT_SIGNAL_TIMEOUT_S,
    producer: asyncio.Task | None = None,
) -> None:
    """Await `event`, failing fast instead of hanging if it never fires.

    `what` names the signal in the failure message. `producer` is the
    task expected to signal it; when given, its exception is reported as
    the cause so the failure names the real fault.
    """

    try:
        await asyncio.wait_for(event.wait(), timeout)
    except TimeoutError:
        cause = _producer_failure(producer)
        detail = f"{what} was never signalled within {timeout}s"
        if cause is not None:
            detail += f"; the task expected to signal it died first: {cause!r}"
        raise AssertionError(detail) from cause
