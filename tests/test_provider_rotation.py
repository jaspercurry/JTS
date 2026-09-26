# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Context reset and deferred rotation through the provider supervisor."""
import asyncio

import pytest

from jasper.tools import ToolRegistry
from jasper.voice.session import ConnectionState
from tests._async_wait import wait_until
from tests._provider_fakes import release_turn, persistent_provider as persistent_provider


async def test_idle_context_reset_reopens_through_the_supervisor(persistent_provider):
    conn, factory = persistent_provider(context_reset_sec=0.01)
    state_at_open = []

    def recording_factory(**kwargs):
        state_at_open.append(conn._state)
        cm = factory(**kwargs)
        if len(factory.sessions) == 2:
            conn._reconnect_event.set()
        return cm

    conn._connect_factory = recording_factory
    await conn.start(ToolRegistry(), "system")
    try:
        turn1 = await conn.acquire_turn()
        await release_turn(turn1, factory.sessions[0])
        await asyncio.sleep(0.05)

        turn2 = await asyncio.wait_for(conn.acquire_turn(), timeout=5.0)
        await release_turn(turn2, factory.sessions[-1])
        await wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        assert state_at_open[0] is ConnectionState.CONNECTING
        assert state_at_open[1:] and all(
            state is ConnectionState.PAUSED_FOR_BACKOFF for state in state_at_open[1:]
        )
        assert len(factory.sessions) >= 2
        assert [s for s in factory.sessions if not s.closed] == [conn._session]
        turn3 = await conn.acquire_turn()
        await turn3.release()
    finally:
        await conn.stop()


async def test_rotation_defers_until_the_active_turn_ends(persistent_provider):
    conn, factory = persistent_provider(watchdog_sec=0.05)
    await conn.start(ToolRegistry(), "system")
    try:
        turn = await conn.acquire_turn()
        await wait_until(lambda: conn._deferred_reconnect.pending, timeout=3.0)
        assert len(factory.sessions) == 1
        assert conn._deferred_reconnect.pending is True
        assert turn.turn_lost() is False
        await turn.release()
        await wait_until(lambda: len(factory.sessions) >= 2, timeout=2.0)
        assert conn._deferred_reconnect.pending is False
    finally:
        await conn.stop()


@pytest.mark.parametrize("pending", ["requested", "deferred"])
async def test_unplanned_drop_does_not_inherit_the_rotation_zero_backoff(persistent_provider, pending):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    conn, factory = persistent_provider(
        sleep=sleep, watchdog_sec=0.05 if pending == "deferred" else None,
    )
    await conn.start(ToolRegistry(), "system")
    try:
        turn = await conn.acquire_turn() if pending == "deferred" else None
        if turn is not None:
            await wait_until(lambda: conn._deferred_reconnect.pending, timeout=3.0)
        conn._planned_rotate = True
        factory.sessions[0].feed_error(ConnectionError("abnormal closure"))
        await wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        assert delays and delays[0] > 0.0, delays
        if turn is not None:
            await turn.release()
    finally:
        await conn.stop()


async def test_watchdog_cancelled_on_teardown(persistent_provider):
    conn, factory = persistent_provider(watchdog_sec=10.0)
    await conn.start(ToolRegistry(), "system")
    task = conn._proactive_watchdog_task
    assert task is not None
    assert not task.done()
    await conn.stop()
    assert task.done()
    assert len(factory.sessions) == 1
