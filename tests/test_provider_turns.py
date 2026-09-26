# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared turn lifecycle and billing across provider transports."""
import asyncio

from jasper.tools import ToolRegistry
from jasper.voice.session import ConnectionState
from tests._live_turn_fake import RecordingMeter, drain_audio_chunks
from tests._provider_fakes import (
    persistent_provider as persistent_provider,
    provider as provider,
)


async def test_stop_is_idempotent(provider):
    conn, _ = provider()
    await conn.start(ToolRegistry(), "system")
    await conn.stop()
    await conn.stop()
    assert conn._state is ConnectionState.CLOSED


async def test_connection_lost_marks_active_turn_lost(provider):
    conn, _ = provider()
    await conn.start(ToolRegistry(), "system")
    try:
        turn = await conn.acquire_turn()
        consumer = asyncio.create_task(drain_audio_chunks(turn))
        conn._session.feed_error(ConnectionError("abnormal closure"))
        await asyncio.wait_for(consumer, timeout=3.0)
        assert turn.turn_lost() is True
    finally:
        await conn.stop()


async def test_activity_meter_hooks_fire_on_turn_acquire_and_release(persistent_provider):
    conn, _ = persistent_provider()
    meter = RecordingMeter()
    conn.set_billable_activity_meter(meter)
    await conn.start(ToolRegistry(), "system")
    try:
        assert meter.marks == []
        turn = await conn.acquire_turn()
        assert meter.marks == ["started"]
        assert conn._active_turn is turn
        assert conn._state is ConnectionState.IN_TURN
        await turn.release()
        assert meter.marks == ["started", ("ended", None)]
        assert conn._active_turn is None
        assert conn._state is ConnectionState.CONNECTED
    finally:
        await conn.stop()
    assert meter.marks == ["started", ("ended", None)]


async def test_no_activity_meter_by_default_is_safe(provider):
    conn, _ = provider()
    assert conn._billable_activity_meter is None
    await conn.start(ToolRegistry(), "system")
    try:
        turn = await conn.acquire_turn()
        await turn.release()
    finally:
        await conn.stop()
