# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reconnect and outage behaviour of persistent provider connections."""
import asyncio
import contextlib

import pytest

from jasper.backoff import (
    RECONNECT_BACKOFF_JITTER_FRACTION,
    RECONNECT_INITIAL_BACKOFF_SEC,
    RECONNECT_MAX_BACKOFF_SEC,
    TERMINAL_POLL_INTERVAL_SEC,
)
from jasper.tools import ToolRegistry
from jasper.voice._supervisor import (
    CANT_CONNECT_CUE_SLUG,
    NEEDS_ATTENTION_CUE_SLUG,
    run_reconnect_with_backoff,
)
from jasper.voice.session import ConnectionState
from tests._async_wait import wait_until as _wait_until
from tests._provider_fakes import never_elapses, stop_after
from tests._provider_fakes import persistent_provider as persistent_provider


async def test_reconnect_with_backoff_eventually_succeeds(persistent_provider):
    conn, factory = persistent_provider()
    await conn.start(ToolRegistry(), "system")
    try:
        factory.sessions[0].feed_error(ConnectionError("abnormal closure"))
        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        turn = await conn.acquire_turn()
        await turn.release()
    finally:
        await conn.stop()


async def test_drop_signalled_during_a_reconnect_is_not_swallowed(persistent_provider):
    conn, factory = persistent_provider()

    def signalling_factory(**kwargs):
        cm = factory(**kwargs)
        if len(factory.sessions) == 2:
            conn._reconnect_event.set()
        return cm

    conn._connect_factory = signalling_factory
    await conn.start(ToolRegistry(), "system")
    try:
        factory.sessions[0].feed_error(ConnectionError("abnormal closure"))
        await _wait_until(lambda: len(factory.sessions) >= 3, timeout=3.0)
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        turn = await conn.acquire_turn()
        await turn.release()
    finally:
        await conn.stop()


async def test_acquire_raises_when_the_connection_never_returns(persistent_provider, monkeypatch):
    """The raise sends the wake down its failure path, which plays a cue,
    instead of hanging it (non-negotiable 6)."""
    monkeypatch.setattr("jasper.voice._supervisor.AWAIT_CONNECTED_TIMEOUT_SEC", 0.05)
    conn, factory = persistent_provider(sleep=never_elapses)
    await conn.start(ToolRegistry(), "system")
    try:
        factory.sessions[0].feed_error(ConnectionError("abnormal closure"))
        await _wait_until(lambda: conn._state is ConnectionState.PAUSED_FOR_BACKOFF, timeout=3.0)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(conn.acquire_turn(), timeout=3.0)
        assert conn.is_paused()
    finally:
        await conn.stop()


@pytest.mark.parametrize("attr,value", [("status_code", 403), ("code", 1007), (None, None)])
async def test_initial_failure_stays_up_and_heals(persistent_provider, attr, value):
    conn, factory = persistent_provider()
    exc = (type("Rejected", (Exception,), {attr: value})("rejected") if attr
           else OSError(-3, "Temporary failure in name resolution"))
    factory.next_exceptions = [exc]
    cue_calls = []

    async def cue_cb(slug):
        cue_calls.append(slug)

    conn.set_failure_escalation_cb(cue_cb)
    await conn.start(ToolRegistry(), "system")
    try:
        assert conn._supervisor_task is not None
        assert conn.is_paused()
        assert isinstance(conn.last_failure_detail(), str)
        if attr:
            await _wait_until(lambda: cue_calls == [NEEDS_ATTENTION_CUE_SLUG])
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        assert not conn.is_paused()
        assert conn.last_failure_detail() is None
        assert cue_calls == ([NEEDS_ATTENTION_CUE_SLUG] if attr else [])
    finally:
        await conn.stop()


async def test_reconnect_escalation_cue_fires_once_per_outage(persistent_provider):
    conn, factory = persistent_provider()
    cue_calls: list[str] = []

    async def cue_cb(slug: str) -> None:
        cue_calls.append(slug)

    class _Terminal(Exception):
        status_code = 403

    class _Drop(Exception):
        pass

    conn.set_failure_escalation_cb(cue_cb)
    registry = ToolRegistry()
    await conn.start(registry, "system")

    async def _outage(exc_factory, opens: int) -> None:
        factory.next_exceptions = [exc_factory(), exc_factory()]
        factory.sessions[-1].feed_error(_Drop("active socket dropped"))
        await _wait_until(
            lambda: (
                len(factory.sessions) >= opens
                and conn._state is ConnectionState.CONNECTED
            ),
            timeout=3.0,
        )
        await asyncio.sleep(0.05)

    try:
        await _outage(_Terminal, opens=2)
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG]
        assert conn._outage.cue is None

        await _outage(_Terminal, opens=3)
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG] * 2

        await _outage(lambda: RuntimeError("transient"), opens=4)
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG] * 2

        stop_after(conn, 4)
        factory.next_exceptions = [_Terminal() for _ in range(8)]
        factory.sessions[-1].feed_error(_Drop("active socket dropped"))
        await _wait_until(conn._stopping.is_set, timeout=3.0)
        assert conn.wake_cue() == NEEDS_ATTENTION_CUE_SLUG
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG] * 3
    finally:
        await conn.stop()
    assert conn._state is ConnectionState.CLOSED


async def _reconnect_delays(persistent_provider, exc, count: int = 4):
    excs = list(exc) if isinstance(exc, list) else [exc]
    raised = 0

    def _factory(**kwargs):
        nonlocal raised
        i = min(raised, len(excs) - 1)
        raised += 1
        raise excs[i]

    conn, _ = persistent_provider()
    conn._connect_factory = _factory
    delays = stop_after(conn, count - 1)
    await asyncio.wait_for(run_reconnect_with_backoff(conn), timeout=10.0)
    return conn, delays


def _terminal_poll_band() -> tuple[float, float]:

    return (
        TERMINAL_POLL_INTERVAL_SEC * (1.0 - RECONNECT_BACKOFF_JITTER_FRACTION),
        TERMINAL_POLL_INTERVAL_SEC * (1.0 + RECONNECT_BACKOFF_JITTER_FRACTION),
    )


class _TerminalRejection(Exception):
    status_code = 403

    def __str__(self) -> str:
        return "403 Forbidden: account has no credits"


class _TransientRejection(Exception):
    status_code = 500

    def __str__(self) -> str:
        return "500 Internal Server Error"


async def test_terminal_reconnect_failure_drops_to_slow_poll(persistent_provider):
    conn, delays = await _reconnect_delays(persistent_provider, _TerminalRejection())
    assert len(delays) == 4
    lo, hi = _terminal_poll_band()
    assert all(lo <= d <= hi for d in delays[1:]), delays
    assert conn._state is ConnectionState.PAUSED_FOR_BACKOFF
    assert conn.last_failure_detail() is not None


async def test_transient_reconnect_failure_keeps_exponential_backoff(persistent_provider):
    _conn, delays = await _reconnect_delays(
        persistent_provider,
        OSError(-3, "Temporary failure in name resolution"), count=6,
    )
    assert all(d <= RECONNECT_MAX_BACKOFF_SEC * 1.25 for d in delays), delays
    assert delays[0] < delays[-1], delays


async def test_recovering_provider_leaves_the_slow_poll_and_restarts_the_ramp(persistent_provider):
    conn, delays = await _reconnect_delays(
        persistent_provider,
        [_TerminalRejection()] * 3 + [_TransientRejection()], count=6,
    )
    lo, hi = _terminal_poll_band()
    assert all(lo <= d <= hi for d in delays[1:4]), delays
    hi_1s = RECONNECT_INITIAL_BACKOFF_SEC * (
        1.0 + RECONNECT_BACKOFF_JITTER_FRACTION
    )
    assert delays[4] <= hi_1s, delays
    assert delays[5] <= hi_1s * 2, delays


class _SlowThenDeadConnect:

    def __init__(self, connecting: asyncio.Event, release: asyncio.Event):
        self.calls = 0
        self._connecting = connecting
        self._release = release

    async def __aenter__(self):
        self.calls += 1
        if self.calls == 1:
            self._connecting.set()
            await self._release.wait()
        raise _TerminalRejection()

    async def __aexit__(self, *exc):
        return None


async def test_wake_during_a_connect_attempt_cuts_the_next_wait_short(persistent_provider):
    delays: list[float] = []
    connecting = asyncio.Event()
    release = asyncio.Event()

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 1:
            return
        await asyncio.sleep(3600)

    connect = _SlowThenDeadConnect(connecting, release)
    conn, _ = persistent_provider(sleep=_sleep)
    conn._connect_factory = lambda **kwargs: connect
    task = asyncio.ensure_future(run_reconnect_with_backoff(conn))
    try:
        await asyncio.wait_for(connecting.wait(), timeout=5.0)
        assert conn.is_paused()
        assert conn.request_reconnect_now() is True
        assert conn.request_reconnect_now() is False
        release.set()
        await _wait_until(lambda: len(delays) == 3, timeout=5.0)
        lo, hi = _terminal_poll_band()
        assert lo <= delays[1] <= hi, delays
        await asyncio.sleep(0.05)
        assert len(delays) == 3, delays
        assert connect.calls == 2
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_cancelling_a_long_backoff_unwinds_at_once(persistent_provider):
    started = asyncio.Event()
    holder: dict = {}

    def _dead(**kwargs):
        raise _TerminalRejection()

    async def _sleep(seconds: float) -> None:
        if started.is_set():
            holder["conn"]._stopping.set()
            return
        started.set()
        await asyncio.sleep(seconds)

    conn, _ = persistent_provider(sleep=_sleep)
    conn._connect_factory = _dead
    holder["conn"] = conn
    task = asyncio.ensure_future(run_reconnect_with_backoff(conn))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert task.cancelled()


async def test_the_first_connect_reads_as_paused_while_it_dials(persistent_provider):
    connecting = asyncio.Event()
    release = asyncio.Event()
    connect = _SlowThenDeadConnect(connecting, release)
    conn, _ = persistent_provider()
    conn._connect_factory = lambda **kwargs: connect
    task = asyncio.ensure_future(conn.start(ToolRegistry(), ""))
    try:
        await asyncio.wait_for(connecting.wait(), timeout=5.0)
        assert conn.is_paused()
        assert conn.wake_cue() == CANT_CONNECT_CUE_SLUG
    finally:
        release.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5.0)
        await conn.stop()


async def test_reconnect_nudge_needs_a_paused_connection_and_is_gated(persistent_provider):
    conn, _factory = persistent_provider()
    await conn.start(ToolRegistry(), "system")
    try:
        assert not conn.is_paused()
        assert conn.request_reconnect_now() is False
    finally:
        await conn.stop()
    conn._state = ConnectionState.PAUSED_FOR_BACKOFF
    assert conn.request_reconnect_now() is True
    assert conn.request_reconnect_now() is False
