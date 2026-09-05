# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the shared voice connection supervisor.

The loops in `jasper.voice._supervisor` are provider-agnostic, so they
are pinned here against a stub connection rather than through an
adapter: the provider files keep only the wiring pin that shows they
call in.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from jasper.voice._supervisor import (
    DEFAULT_INITIAL_CONNECT_BUDGET_SEC,
    run_initial_connect,
)
from tests._log_events import event_field_maps, event_fields

LOGGER_NAME = "jasper.voice._supervisor"


class _StubConnection:
    """The slice of `SupervisedConnection` the initial connect reads.

    Its clock only moves when the loop sleeps, so a ten-minute budget
    is exhausted in no wall time."""

    PROVIDER_NAME = "stub"
    _log_tag = "stub connection:"

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self._failures = list(failures or [])
        self._stopping = asyncio.Event()
        self._monotonic_now = 1_000_000.0
        self.delays: list[float] = []
        self.attempts = 0
        # Called after each recorded sleep, so a test can stop the
        # connection mid-wait.
        self.on_sleep = lambda: None

    def _monotonic(self) -> float:
        return self._monotonic_now

    async def _sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self._monotonic_now += delay
        self.on_sleep()
        await asyncio.sleep(0)

    async def _open_session(self) -> None:
        self.attempts += 1
        if self._failures:
            raise self._failures.pop(0)


def _transient() -> Exception:
    """A DNS failure: no HTTP status, so `is_transient` says retry."""
    return OSError(-3, "Temporary failure in name resolution")


async def test_first_attempt_that_connects_ends_the_loop():
    conn = _StubConnection()
    await run_initial_connect(conn, DEFAULT_INITIAL_CONNECT_BUDGET_SEC)
    assert conn.attempts == 1
    assert conn.delays == []


async def test_transient_failures_retry_on_the_exponential_schedule(caplog):
    """A link that comes back mid-budget connects, and the waits ramp on
    the shared 1/2/4 s ±25 % schedule."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    conn = _StubConnection([_transient() for _ in range(3)])
    await run_initial_connect(conn, DEFAULT_INITIAL_CONNECT_BUDGET_SEC)
    assert conn.attempts == 4
    assert [round(d, 3) for d in conn.delays] == pytest.approx(
        [1.0, 2.0, 4.0], rel=0.25,
    )
    retries = event_field_maps(caplog, "voice.initial_connect.retry")
    assert [r["attempt"] for r in retries] == ["1", "2", "3"]
    assert {r["provider"] for r in retries} == {"stub"}
    success = event_fields(caplog, "voice.initial_connect.success")
    assert success["provider"] == "stub"
    assert success["attempt"] == "4"


async def test_budget_exhaustion_raises_without_ever_oversleeping(caplog):
    """The budget is wall time, so the last wait is clamped to what is
    left of it rather than overshooting and retrying past the deadline."""
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    conn = _StubConnection([_transient() for _ in range(100)])
    with pytest.raises(RuntimeError, match="budget of .* exhausted"):
        await run_initial_connect(conn, 2.0)
    assert conn.attempts > 1
    assert round(sum(conn.delays), 6) <= 2.0
    exhausted = event_fields(caplog, "voice.initial_connect.exhausted")
    assert exhausted["provider"] == "stub"
    assert exhausted["attempt"] == str(conn.attempts)


async def test_zero_budget_is_a_single_attempt():
    conn = _StubConnection([_transient()])
    with pytest.raises(RuntimeError, match="budget"):
        await run_initial_connect(conn, 0.0)
    assert conn.attempts == 1
    assert conn.delays == []


@pytest.mark.parametrize("attr, value", [("status_code", 403), ("code", 1007)])
async def test_terminal_failure_raises_at_once_without_waiting(
    attr, value, caplog,
):
    """A rejected key and a refused setup message cannot be waited out:
    the budget is never entered and the failure reaches the caller."""
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    exc = type("_Terminal", (Exception,), {attr: value})("rejected")
    conn = _StubConnection([exc])
    with pytest.raises(Exception) as caught:
        await run_initial_connect(conn, DEFAULT_INITIAL_CONNECT_BUDGET_SEC)
    assert caught.value is exc
    assert conn.attempts == 1
    assert conn.delays == []
    fatal = event_fields(caplog, "voice.initial_connect.fatal")
    assert fatal["provider"] == "stub"
    assert fatal["attempt"] == "1"


async def test_a_stop_ends_the_wait_and_the_loop():
    """`stop()` during a boot-time outage must not be held for the rest
    of the budget — systemd SIGKILLs at TimeoutStopSec."""
    conn = _StubConnection([_transient() for _ in range(100)])
    conn.on_sleep = conn._stopping.set
    await asyncio.wait_for(
        run_initial_connect(conn, DEFAULT_INITIAL_CONNECT_BUDGET_SEC),
        timeout=1.0,
    )
    assert conn.attempts == 1


async def test_the_exhaustion_message_carries_no_credential():
    """The message reaches journald through the caller's traceback, and
    a refused handshake can quote the connect URL. Non-negotiable 3."""
    key = "AIzaSyFAKE0123456789abcdefghijklmnop"
    conn = _StubConnection([
        OSError(f"connect wss://provider.example/live?key={key} refused")
        for _ in range(100)
    ])
    with pytest.raises(RuntimeError) as caught:
        await run_initial_connect(conn, 1.0)
    assert key not in str(caught.value)
