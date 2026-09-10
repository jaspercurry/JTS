# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import asynccontextmanager

import pytest
from openai.types.live.client_event_param import ClientEventParam
from pydantic import TypeAdapter

from jasper.tools import ToolRegistry, tool
from jasper.voice import openai_live_session
from jasper.voice.conversation import END_CONVERSATION_TOOL, register_conversation_tools
from jasper.voice.openai_live_session import SILENCE_BRIDGE_SEC, OpenAILiveConnection
from tests._async_wait import wait_until
from tests._log_events import event_fields


CLIENT_EVENT = TypeAdapter(ClientEventParam)


class LiveSocket:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def send(self, event):
        CLIENT_EVENT.validate_python(event)
        self.sent.append(event)
        if event["type"] == "session.start":
            await self.events.put({"type": "session.started"})
        if event["type"] == "session.close":
            await self.events.put({"type": "session.closed", "usage": {"seconds": 12.5}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()


AUDIBLE_PCM = b"\x00\x40" * 120  # 5 ms of int16 tone at 24 kHz
QUIET_PCM = bytes(240)


class FrozenClock:
    """The adapter reads only `time.monotonic()`; hold it still."""

    def __init__(self, now=10_000.0):
        self.now = now

    def monotonic(self):
        return self.now


def output_audio(pcm):
    return {"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()}


@asynccontextmanager
async def live_turn():
    conn = OpenAILiveConnection(api_key="test", connect=LiveSocket)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    try:
        yield turn
    finally:
        await turn.release()
        await conn.stop()


def backend(delegation, kind, **fields):
    return {"type": "response.event", "delegation_id": delegation, "event": {"type": kind, **fields}}


async def delegate(turn, delegation, response_id, name, args):
    await turn.on_event({"type": "session.delegation.created", "delegation": {"id": delegation, "target": "responses"}})
    await turn.on_event(backend(delegation, "response.created", response={"id": response_id}))
    await turn.on_event(backend(delegation, "response.output_item.done", item={
        "type": "function_call", "call_id": response_id + "_call", "name": name, "arguments": json.dumps(args),
    }))
    await turn.on_event(backend(delegation, "response.completed", response={
        "id": response_id, "output": [], "usage": {"input_tokens": 20, "output_tokens": 5},
    }))


async def test_live_opens_on_wake_dispatches_local_tools_and_finalizes_usage():
    socket = LiveSocket()
    registry = ToolRegistry()
    calls = []

    @tool()
    async def timer(seconds: int) -> dict:
        """Set a timer."""
        calls.append(seconds)
        return {"seconds": seconds}

    registry.register(timer)
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    usage = []
    conn.set_background_usage_recorder(lambda **row: usage.append(row))
    await conn.start(registry, "Use local tools.")
    assert socket.sent == []
    turn = await conn.acquire_turn()
    try:
        await turn.send_text_context("The user wants the timer.")
        await delegate(turn, "d1", "r1", "timer", {"seconds": 30})
        await wait_until(lambda: any(e["type"] == "response.create" for e in socket.sent))
        assert calls == [30]
        result = next(e["item"] for e in socket.sent if e["type"] == "response.item.create")
        assert result["call_id"] == "r1_call"
        assert json.loads(result["output"])["seconds"] == 30
        completed = backend("d1", "response.completed", response={"id": "r1", "output": [], "usage": {"input_tokens": 20}})
        await turn.on_event(completed)
        assert len(usage) == 1
        await turn.on_event({"type": "session.usage.updated", "usage": {"seconds": 4}})
        await turn.on_event({"type": "session.usage.updated", "usage": {"seconds": 10}})
    finally:
        await turn.release()
        await conn.stop()
    assert turn.usage().breakdown == {"seconds": 12.5, "finalized": True}
    assert socket.closed


async def test_correction_discards_stale_tool_results():
    socket = LiveSocket()
    registry = ToolRegistry()
    entered, finish = asyncio.Event(), asyncio.Event()

    @tool()
    async def slow_lookup() -> dict:
        """Look up transit."""
        entered.set()
        await finish.wait()
        return {"bus": 1}

    registry.register(slow_lookup)
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(registry, "Use local tools.")
    turn = await conn.acquire_turn()
    try:
        await delegate(turn, "old", "r1", "slow_lookup", {})
        await entered.wait()
        await turn.on_event({"type": "session.delegation.created", "delegation": {"id": "new", "target": "responses"}})
        finish.set()
        await asyncio.gather(turn._tool_task, return_exceptions=True)
        assert not any(e["type"] == "response.item.create" for e in socket.sent)
    finally:
        finish.set()
        await turn.release()
        await conn.stop()


async def test_silence_does_not_count_as_an_answer_and_mute_discards_buffered_input():
    socket = LiveSocket()
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    try:
        await turn.on_event(output_audio(QUIET_PCM))
        assert turn.chunks_received() == 0
        assert turn.audio_chunks_pending() == 0
        await turn.send_audio(b"\xff\x7f" * 1280)
        turn.discard_input()
        await wait_until(lambda: any(e["type"] == "session.input_audio.append" for e in socket.sent))
        assert all(not any(base64.b64decode(e["audio"])) for e in socket.sent if e["type"] == "session.input_audio.append")
    finally:
        await turn.release()
        await conn.stop()


async def test_failed_start_redacts_key_and_allows_another_wake():
    socket = LiveSocket()
    key = "private-test-credential"
    attempts = 0

    def connect():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(f"rejected {key}")
        return socket

    conn = OpenAILiveConnection(api_key=key, connect=connect)
    await conn.start(ToolRegistry(), "Be concise.")
    try:
        await conn.acquire_turn()
    except RuntimeError as exc:
        assert key not in str(exc)
    else:
        raise AssertionError("connection must fail")
    assert not conn.is_paused()
    turn = await conn.acquire_turn()
    await turn.release()
    await conn.stop()


@pytest.mark.parametrize("gap_sec, played, discarded", [
    (0.0, 1, 0),
    (SILENCE_BRIDGE_SEC, 1, 0),
    (SILENCE_BRIDGE_SEC + 0.05, 0, 1),
    (30.0, 0, 1),
])
async def test_quiet_deltas_play_only_while_the_answer_is_running(
    monkeypatch, caplog, gap_sec, played, discarded,
):
    clock = FrozenClock()
    monkeypatch.setattr(openai_live_session, "time", clock)
    caplog.set_level(logging.INFO)
    async with live_turn() as turn:
        await turn.on_event(output_audio(AUDIBLE_PCM))
        audible_at = turn.last_chunk_at()
        clock.now += gap_sec
        await turn.on_event(output_audio(QUIET_PCM))
        assert turn.audio_chunks_pending() == 1 + played
        # Quiet never counts as an answer, whether it is played or not.
        assert turn.chunks_received() == 1
        assert turn.last_chunk_at() == audible_at
    fields = event_fields(caplog, "live.turn_audio")
    assert int(fields["chunks_received"]) == 1
    assert int(fields["quiet_played"]) == played
    assert int(fields["quiet_discarded"]) == discarded


async def test_an_audible_delta_after_a_long_gap_rearms_silence_bridging(monkeypatch):
    clock = FrozenClock()
    monkeypatch.setattr(openai_live_session, "time", clock)
    async with live_turn() as turn:
        await turn.on_event(output_audio(AUDIBLE_PCM))
        clock.now += SILENCE_BRIDGE_SEC * 4
        await turn.on_event(output_audio(QUIET_PCM))
        assert turn.audio_chunks_pending() == 1
        await turn.on_event(output_audio(AUDIBLE_PCM))
        clock.now += SILENCE_BRIDGE_SEC / 2
        await turn.on_event(output_audio(QUIET_PCM))
        assert turn.audio_chunks_pending() == 3
        assert turn.chunks_received() == 2


async def test_a_dismissal_survives_a_delegation_that_cancels_its_round():
    """A new delegation cancels the in-flight tool round; the user's
    "never mind" is not a request that a later one can make obsolete."""
    socket = LiveSocket()
    registry = ToolRegistry()
    ends = []
    register_conversation_tools(registry, lambda: ends.append("end"))
    dispatching, resume = asyncio.Event(), asyncio.Event()

    async def observer(stage, name):
        if stage == "called" and name == END_CONVERSATION_TOOL:
            dispatching.set()
            await resume.wait()

    registry.set_dispatch_observer(lambda: observer)
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(registry, "Be brief.")
    turn = await conn.acquire_turn()
    try:
        await delegate(turn, "d1", "r1", END_CONVERSATION_TOOL, {})
        await wait_until(dispatching.is_set)
        await turn.on_event({
            "type": "session.delegation.created",
            "delegation": {"id": "d2", "target": "responses"},
        })
        resume.set()
        await wait_until(lambda: ends == ["end"])
    finally:
        resume.set()
        await turn.release()
        await conn.stop()


async def test_a_server_that_never_acks_the_close_does_not_hold_the_release(monkeypatch):
    monkeypatch.setattr(openai_live_session, "CLOSE_ACK_TIMEOUT_SEC", 0.05)

    class SilentSocket(LiveSocket):
        async def send(self, event):
            CLIENT_EVENT.validate_python(event)
            self.sent.append(event)
            if event["type"] == "session.start":
                await self.events.put({"type": "session.started"})

    socket = SilentSocket()
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    started = time.monotonic()
    await turn.release()
    elapsed = time.monotonic() - started
    await conn.stop()

    assert [e["type"] for e in socket.sent].count("session.close") == 1
    assert socket.closed
    assert elapsed < 1.0
