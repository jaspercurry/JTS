# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import random
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from openai.types.live.client_event_param import ClientEventParam
from pydantic import TypeAdapter

from jasper.tools import ToolRegistry, tool
from jasper.voice import _base, openai_live_session
from jasper.voice._supervisor import CANT_CONNECT_CUE_SLUG, NEEDS_ATTENTION_CUE_SLUG, OUT_OF_CREDIT_CUE_SLUG, is_transient
from jasper.voice.conversation import END_CONVERSATION_TOOL, register_conversation_tools
from jasper.voice.openai_live_session import SILENCE_BRIDGE_SEC, OpenAILiveConnection
from jasper.voice.session import ConnectionState
from jasper.voice.turn_playback import PlaybackReport, play_responses
from tests._async_wait import wait_signalled, wait_until
from tests._log_events import event_field_maps, event_fields, event_records
from tests._playout import FakeTts


CLIENT_EVENT: TypeAdapter[ClientEventParam] = TypeAdapter(ClientEventParam)


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
        if event["type"] == "session.start":
            tools = event["session"]["delegation"]["responses"]["tools"]
            assert tools.count({"type": "web_search"}) == 1
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


@pytest.mark.parametrize("state", ["fresh", "completed", "pending", "lost"])
async def test_backend_nudge_only_sends_when_no_delegation_is_in_flight(caplog, state):
    caplog.set_level(logging.INFO)
    async with live_turn() as turn:
        if state in {"completed", "pending"}:
            await turn.on_event({"type": "session.delegation.created", "delegation": {"id": "d1"}})
            if state == "completed":
                await turn.on_event(backend("d1", "response.completed", response={"id": "r1"}))
        elif state == "lost":
            turn._on_connection_lost()
        socket = turn._conn._session
        socket.sent.clear()
        allowed = state in {"fresh", "completed"}
        assert await turn.nudge_backend(silence_ms=2000) is allowed
        assert socket.sent == ([{"type": "response.create"}] if allowed else [])
        assert event_field_maps(caplog, "provider.backend_nudged") == (
            [{"provider": "openai_live", "silence_ms": "2000"}] if allowed else []
        )


async def test_user_transcript_timestamp_uses_monotonic_time(monkeypatch):
    clock = FrozenClock()
    monkeypatch.setattr(openai_live_session, "time", clock)
    async with live_turn() as turn:
        assert turn.last_user_transcript_at() == 0.0
        for direction in ("input", "output", "input"):
            clock.now += 1
            before = turn.last_user_transcript_at()
            await turn.on_event({
                "type": f"session.{direction}_transcript.delta", "delta": "okay",
                "start_ms": 0, "end_ms": 1000,
            })
            assert turn.last_user_transcript_at() == (clock.now if direction == "input" else before)


async def delegate(turn, delegation, response_id, name, args):
    await turn.on_event({"type": "session.delegation.created", "delegation": {"id": delegation, "target": "responses"}})
    await turn.on_event(backend(delegation, "response.created", response={"id": response_id}))
    await turn.on_event(backend(delegation, "response.output_item.done", item={
        "type": "function_call", "call_id": response_id + "_call", "name": name, "arguments": json.dumps(args),
    }))
    await turn.on_event(backend(delegation, "response.completed", response={
        "id": response_id, "output": [], "usage": {"input_tokens": 20, "output_tokens": 5},
    }))


@pytest.mark.parametrize("failure_stage", [None, "construct", "bind"])
async def test_sdk_prepares_before_wake_without_dialling_and_retries_preparation_failure(
    monkeypatch, caplog, failure_stage,
):
    key = "private-test-credential"
    steps = []
    failed = False

    def step(stage):
        nonlocal failed
        steps.append(stage)
        if stage == failure_stage and not failed:
            failed = True
            raise ValueError(f"SDK preparation failed for {key}")

    class Client:
        def __init__(self, *, api_key):
            assert api_key == key
            step("construct")

        @property
        def live(self):
            step("bind")
            return self

        def connect(self):
            step("dial")
            return LiveSocket()

        async def close(self):
            step("close")

    monkeypatch.setattr("openai.AsyncOpenAI", Client)
    conn = OpenAILiveConnection(api_key=key)
    cues = []

    async def cue_cb(slug):
        cues.append(slug)

    conn.set_failure_escalation_cb(cue_cb)
    try:
        await conn.start(ToolRegistry(), "Be brief.")
        assert steps == (["construct"] if failure_stage == "construct" else ["construct", "bind"])
        assert not conn.is_paused()
        if failure_stage:
            assert conn._state is ConnectionState.FAILED
            assert conn.last_failure_detail() and key not in conn.last_failure_detail()
            assert key not in caplog.text
            assert conn.wake_cue() == CANT_CONNECT_CUE_SLUG
        await asyncio.sleep(0)
        assert cues == []
        turn = await conn.acquire_turn()
        assert steps == {
            None: ["construct", "bind", "dial"],
            "construct": ["construct", "construct", "bind", "dial"],
            "bind": ["construct", "bind", "bind", "dial"],
        }[failure_stage]
        assert not turn.turn_lost()
        assert conn.last_failure_detail() is None
        await turn.release()
    finally:
        await conn.stop()
        await conn.stop()
    assert steps.count("close") == 1


@pytest.mark.parametrize("interruption", [None, "start", "wake", "stop", "stop_cancel"])
async def test_sdk_preparation_keeps_loop_responsive_and_one_owner(monkeypatch, interruption):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    constructing = asyncio.Event()
    finish_construct = threading.Event()
    steps = []

    class Client:
        def __init__(self, **_kwargs):
            assert threading.get_ident() != loop_thread
            steps.append("construct")
            loop.call_soon_threadsafe(constructing.set)
            assert finish_construct.wait(timeout=2.0)

        @property
        def live(self):
            assert threading.get_ident() != loop_thread
            steps.append("bind")
            return self

        def connect(self):
            steps.append("dial")
            return LiveSocket()

        async def close(self):
            steps.append("close")

    monkeypatch.setattr("openai.AsyncOpenAI", Client)
    conn = OpenAILiveConnection(api_key="test")
    starting = asyncio.create_task(conn.start(ToolRegistry(), "Be brief."))
    tasks = [starting]
    try:
        await wait_signalled(constructing, "SDK worker", producer=starting)
        assert conn._state is ConnectionState.CONNECTING
        assert steps == ["construct"]
        acquiring = asyncio.create_task(conn.acquire_turn())
        tasks.append(acquiring)
        await wait_until(lambda: conn._active_turn is not None)
        assert not starting.done()
        assert not acquiring.done()
        if interruption == "start":
            starting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await starting
        elif interruption == "wake":
            acquiring.cancel()
            with pytest.raises(asyncio.CancelledError):
                await acquiring
            acquiring = asyncio.create_task(conn.acquire_turn())
            tasks.append(acquiring)
            await wait_until(lambda: conn._active_turn is not None)
        elif interruption in {"stop", "stop_cancel"}:
            stopping = asyncio.create_task(conn.stop())
            tasks.append(stopping)
            await wait_until(conn._stopping.is_set)
            if interruption == "stop_cancel":
                for _ in range(2):
                    stopping.cancel()
                    await asyncio.sleep(0)
            assert not stopping.done()
        assert steps == ["construct"]
        finish_construct.set()
        if interruption in {"stop", "stop_cancel"}:
            with pytest.raises(RuntimeError):
                await acquiring
            if interruption == "stop_cancel":
                with pytest.raises(asyncio.CancelledError):
                    await stopping
            else:
                await stopping
            assert conn._state is ConnectionState.CLOSED
            assert steps == ["construct", "bind", "close"]
        else:
            turn = await acquiring
            assert not turn.turn_lost()
            assert conn._state is ConnectionState.IN_TURN
            await turn.release()
        if not starting.cancelled():
            await starting
    finally:
        finish_construct.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await conn.stop()
        await conn.stop()
    assert steps.count("construct") == 1
    assert steps.count("bind") == 1
    assert steps.count("close") == 1


async def test_live_opens_on_wake_dispatches_local_tools_and_finalizes_usage():
    socket = LiveSocket()
    registry = ToolRegistry()
    calls = []

    @tool()
    async def timer(seconds: int) -> dict:
        """Set a timer."""
        calls.append(seconds)
        return {"seconds": seconds}

    @tool()
    def broken_tool() -> dict:
        """Returns something that can't be JSON-encoded."""
        return {"bad": object()}

    registry.register(timer)
    registry.register(broken_tool)
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

        # A malformed item (missing `name`) must not crash the reader, and
        # must not block a well-formed item ahead of it in the same round.
        await turn.on_event(backend("d1", "response.created", response={"id": "r2"}))
        await turn.on_event(backend("d1", "response.output_item.done", item={
            "type": "function_call", "call_id": "r2_call", "name": "timer", "arguments": json.dumps({"seconds": 5}),
        }))
        await turn.on_event(backend("d1", "response.output_item.done", item={
            "type": "function_call", "call_id": "r2_bad_call", "arguments": "{}",
        }))
        await turn.on_event(backend("d1", "response.completed", response={"id": "r2", "output": [], "usage": {}}))
        await wait_until(lambda: any(
            e["type"] == "response.item.create" and e["item"]["call_id"] == "r2_bad_call" for e in socket.sent
        ))
        assert calls == [30, 5]
        outputs = {
            e["item"]["call_id"]: json.loads(e["item"]["output"])
            for e in socket.sent if e["type"] == "response.item.create"
        }
        assert outputs["r2_bad_call"] == {"error": "unknown tool "}
        assert not turn.turn_lost()

        # A tool result that can't be JSON-encoded must not raise out of
        # the round (which would otherwise kill the reader) — the wire
        # carries a synthetic error output for that call_id instead, and
        # the round still finishes.
        before = len(socket.sent)
        await turn.on_event(backend("d1", "response.created", response={"id": "r3"}))
        await turn.on_event(backend("d1", "response.output_item.done", item={
            "type": "function_call", "call_id": "r3_call", "name": "broken_tool", "arguments": "{}",
        }))
        await turn.on_event(backend("d1", "response.completed", response={"id": "r3", "output": [], "usage": {}}))
        await wait_until(lambda: any(
            e["type"] == "response.item.create" and e["item"]["call_id"] == "r3_call"
            for e in socket.sent[before:]
        ))
        r3_output = next(
            json.loads(e["item"]["output"]) for e in socket.sent[before:]
            if e["type"] == "response.item.create" and e["item"]["call_id"] == "r3_call"
        )
        assert r3_output == {"error": "tool result not serializable: TypeError"}
        await wait_until(lambda: any(e["type"] == "response.create" for e in socket.sent[before:]))
        assert not turn.turn_lost()
    finally:
        await turn.release()
        await conn.stop()
    assert turn.usage().breakdown == {"seconds": 12.5, "finalized": True}
    assert socket.closed


@pytest.mark.parametrize("deltas, user_text, assistant_text, transcript_intervals", [
    ([], None, None, {"user": [], "assistant": []}),
    (
        [("input", "turn on ", 0, 500), ("input", "the lights", 500, 900)],
        "turn on the lights", None,
        {
            "user": [
                {"delta": "turn on ", "start_ms": 0, "end_ms": 500},
                {"delta": "the lights", "start_ms": 500, "end_ms": 900},
            ],
            "assistant": [],
        },
    ),
    (
        [("input", "hi", 0, 200), ("output", "Hello", 300, 700), ("output", " there", 700, 900)],
        "hi", "Hello there",
        {
            "user": [{"delta": "hi", "start_ms": 0, "end_ms": 200}],
            "assistant": [
                {"delta": "Hello", "start_ms": 300, "end_ms": 700},
                {"delta": " there", "start_ms": 700, "end_ms": 900},
            ],
        },
    ),
])
async def test_capture_joins_transcript_deltas_per_speaker_beside_their_intervals(
    deltas, user_text, assistant_text, transcript_intervals,
):
    async with live_turn() as turn:
        for direction, delta, start_ms, end_ms in deltas:
            await turn.on_event({
                "type": f"session.{direction}_transcript.delta",
                "delta": delta, "start_ms": start_ms, "end_ms": end_ms,
            })
        capture = turn.capture()
    assert (capture.user_text, capture.assistant_text) == (user_text, assistant_text)
    assert capture.data["transcript_intervals"] == transcript_intervals


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
        await wait_signalled(entered, "slow_lookup entered", producer=turn._tool_task)
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


async def test_speech_buffered_during_the_dial_catches_up_and_live_input_stays_paced(
    monkeypatch, caplog,
):
    """The dial costs seconds the room does not wait through."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(openai_live_session, "time", FrozenClock())
    frame = b"\xff\x7f" * 1280  # 80 ms at 16 kHz: one pacing quantum
    backlog = 16  # the sender's whole input queue, 1.28 s of speech
    socket = LiveSocket()
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()

    def speech_appends():
        return sum(
            1 for e in socket.sent
            if e["type"] == "session.input_audio.append" and any(base64.b64decode(e["audio"]))
        )

    try:
        started = time.monotonic()
        for _ in range(backlog):
            await turn.send_audio(frame)
        await wait_until(lambda: speech_appends() == backlog, timeout=3.0)
        caught_up = time.monotonic() - started
        drained_at = len(socket.sent)
        await asyncio.sleep(0.25)
        after_catch_up = len(socket.sent) - drained_at
    finally:
        await turn.release()
        await conn.stop()

    # A quantum each would cost the 15 stale frames 1.2 s.
    assert caught_up < 0.4
    # 0.25 s of synthesized quiet at 1x is ~3 appends, never a free run.
    assert 1 <= after_catch_up <= 6
    assert int(event_fields(caplog, "provider.turn_ended")["input_catchup_ms"]) == 1200


async def test_new_capture_reaches_the_wire_in_order_without_waiting_for_a_pacing_sleep(monkeypatch):
    pacing = asyncio.Event()
    first_sent, all_sent = asyncio.Event(), asyncio.Event()
    samples = []

    async def hold_pacing(_):
        await pacing.wait()

    monkeypatch.setattr(openai_live_session, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": hold_pacing})))

    class ObservedSocket(LiveSocket):
        async def send(self, event):
            await super().send(event)
            if event["type"] == "session.input_audio.append":
                sample = int.from_bytes(base64.b64decode(event["audio"])[-2:], "little", signed=True)
                if sample:
                    samples.append(sample)
                    first_sent.set()
                    if len(samples) == 3:
                        all_sent.set()

    conn = OpenAILiveConnection(api_key="test", connect=ObservedSocket)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    try:
        await turn.send_audio((1000).to_bytes(2, "little") * 1280)
        await wait_signalled(first_sent, "first captured frame", producer=turn._sender)
        for sample in (2000, 3000):
            await turn.send_audio(sample.to_bytes(2, "little") * 1280)
        await wait_signalled(all_sent, "new captured frames", producer=turn._sender)
        assert samples == [1000, 2000, 3000]
    finally:
        pacing.set()
        await turn.release()
        await conn.stop()


async def test_a_mid_burst_discard_does_not_crash_the_sender():
    """A mid-burst mute must not be reported as a lost connection."""
    frame = b"\xff\x7f" * 1280  # 80 ms at 16 kHz
    backlog = 16  # the sender's whole input queue

    class DiscardMidBurst(LiveSocket):
        turn = None

        async def send(self, event):
            await super().send(event)
            if event["type"] == "session.input_audio.append" and len(self.sent) == 2:
                self.turn.discard_input()

    socket = DiscardMidBurst()
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    socket.turn = turn
    try:
        for _ in range(backlog):
            await turn.send_audio(frame)
        await wait_until(lambda: len(socket.sent) >= 2)
        await asyncio.sleep(0.05)
        assert not turn.turn_lost()
    finally:
        await turn.release()
        await conn.stop()
    assert turn._sender.cancelled()


async def test_jitter_around_the_mic_cadence_never_splices_a_synthesized_frame():
    """Ordinary capture jitter must not insert silence inside speech."""
    rng = random.Random(20260911)
    frame_count = 25
    socket = LiveSocket()
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()

    async def produce():
        start = time.monotonic()
        for i in range(1, frame_count + 1):
            target = start + i * 0.080 + rng.uniform(-0.008, 0.008)
            await asyncio.sleep(max(0.0, target - time.monotonic()))
            await turn.send_audio(b"\xff\x7f" * 1280)

    try:
        await produce()
        await asyncio.sleep(0.1)
    finally:
        await turn.release()
        await conn.stop()

    def is_real(event):
        # A synthesized frame right after a real one carries the
        # resampler's one-sample ring-down, so classify on level, not
        # on whether any byte is nonzero.
        return audioop.rms(base64.b64decode(event["audio"]), 2) > 16000

    appends = [e for e in socket.sent if e["type"] == "session.input_audio.append"]
    real = [is_real(e) for e in appends]
    spliced = [i for i in range(1, len(real) - 1) if not real[i] and real[i - 1] and real[i + 1]]
    assert spliced == []


async def test_a_terminal_connect_failure_reports_the_outage_and_its_remedy():
    """A wake that cannot open a session tells the household why.

    Live holds no socket between conversations, so the acquire is where
    its outage is observed: the redacted cause reaches `/state` through
    `last_failure_detail()`, the remedy cue is announced once, and the
    connection stays takeable so the next wake still dials.
    """
    key = "private-test-credential"
    attempts = 0

    class _Terminal(Exception):
        status_code = 403

    def connect():
        nonlocal attempts
        attempts += 1
        raise _Terminal(f"insufficient_quota for {key}")

    cues: list[str] = []

    async def cue_cb(slug: str) -> None:
        cues.append(slug)

    conn = OpenAILiveConnection(api_key=key, connect=connect)
    conn.set_failure_escalation_cb(cue_cb)
    await conn.start(ToolRegistry(), "Be concise.")
    with pytest.raises(RuntimeError) as raised:
        await conn.acquire_turn()

    assert key not in str(raised.value)
    detail = conn.last_failure_detail()
    assert isinstance(detail, str) and key not in detail
    assert conn.wake_cue() == OUT_OF_CREDIT_CUE_SLUG
    await wait_until(lambda: cues == [OUT_OF_CREDIT_CUE_SLUG])
    # Retrying a rejected account cannot help, so the wake pays for one dial.
    assert attempts == 1
    # Live has no supervisor to clear a pause, so a paused connection would
    # refuse every later wake without dialling — `WakeLoop._await_connection`
    # waits out its bound and cues instead of opening a turn.
    assert not conn.is_paused()
    await conn.stop()


async def test_one_transient_failure_then_success_still_gets_the_turn(monkeypatch):
    """The wake's own acquire is the only retry Live has."""
    monkeypatch.setattr(openai_live_session, "reconnect_delay", lambda *a, **k: 0.0)
    socket = LiveSocket()
    attempts = 0

    def connect():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("live session 409: prior session still closing")
        return socket

    conn = OpenAILiveConnection(api_key="test", connect=connect)
    turn = None
    try:
        await conn.start(ToolRegistry(), "Be concise.")
        turn = await conn.acquire_turn()
        assert attempts == 2
        assert not turn.turn_lost()
        # The open that took clears the outage the failed one recorded.
        assert conn.last_failure_detail() is None
    finally:
        if turn is not None:
            await turn.release()
        await conn.stop()


async def test_stop_releases_the_active_turn_and_closes_the_session_once():
    exits = 0

    class CountingSocket(LiveSocket):
        async def __aexit__(self, *args):
            nonlocal exits
            exits += 1
            await super().__aexit__(*args)

    socket = CountingSocket()
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(ToolRegistry(), "Be concise.")
    turn = await conn.acquire_turn()
    await conn.stop()

    assert turn._released
    assert conn._active_turn is None
    assert [e["type"] for e in socket.sent].count("session.close") == 1
    assert exits == 1
    assert conn._state is ConnectionState.CLOSED


class HangingDial(LiveSocket):
    """A socket whose dial parks until `release` fires — a wake that is
    still opening its session when something else stops the connection.

    `close_sec` slows the transport unwind the teardown runs before the
    release reaches the turn lock.
    """

    def __init__(
        self,
        dialling: asyncio.Event,
        release: asyncio.Event,
        close_sec: float = 0.0,
    ):
        super().__init__()
        self._dialling = dialling
        self._release = release
        self._close_sec = close_sec
        self.exits = 0

    async def __aenter__(self):
        self._dialling.set()
        await self._release.wait()
        return await super().__aenter__()

    async def __aexit__(self, *args):
        self.exits += 1
        await asyncio.sleep(self._close_sec)
        return await super().__aexit__(*args)


async def test_a_stop_racing_an_in_flight_open_is_not_an_outage(monkeypatch):
    """The open owns its turn, so a concurrent stop is not a local defect.

    `stop()` nulls the shared active-turn field; an open reading it back
    after its own await used to raise an AttributeError, which classifies
    TERMINAL and latched the needs-attention remedy — suppressing the
    announcement of a real one later.
    """
    monkeypatch.setattr(openai_live_session, "SESSION_CLOSE_TIMEOUT_SEC", 0.2)
    dialling, release = asyncio.Event(), asyncio.Event()
    socket = HangingDial(dialling, release)
    dials = 0

    def connect():
        nonlocal dials
        dials += 1
        return socket

    conn = OpenAILiveConnection(api_key="test", connect=connect)
    await conn.start(ToolRegistry(), "Be concise.")

    acquire = asyncio.create_task(conn.acquire_turn())
    await wait_signalled(dialling, "the dial starting", producer=acquire)
    await conn.stop()
    release.set()
    with pytest.raises(RuntimeError):
        await acquire

    assert conn.wake_cue() == CANT_CONNECT_CUE_SLUG
    # A stop is not the transient the second attempt exists for.
    assert dials == 1
    assert socket.closed


@pytest.mark.parametrize("close_sec", [0.0, 0.3])
async def test_stop_does_not_wait_out_a_dial_that_is_still_hanging(
    monkeypatch, caplog, close_sec,
):
    """`stop()` returns inside the close bound, whatever the dial does.

    The release ends by taking the turn lock the acquire holds for its
    whole open budget — longer than the unit's `TimeoutStopSec`, so an
    unbounded release means SIGKILL on a restart mid-dial. `stop()`
    spends one cancel on the release, so the bound has to hold wherever
    that cancel lands: on the lock itself, or — with a transport whose
    unwind is slow — inside the close the release runs first.

    The cancellation that produces `provider.close_failed phase=release`
    must not also swallow `provider.turn_ended`: `_log_release()` runs
    synchronously before the awaited `_on_turn_released`, so the turn
    still leaves a record even when that await is where the cancel lands.
    """
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(openai_live_session, "SESSION_OPEN_BUDGET_SEC", 5.0)
    # The release bound fires inside the slow unwind; the base bound is
    # both the close's own ceiling and the release's wait for the lock.
    monkeypatch.setattr(openai_live_session, "SESSION_CLOSE_TIMEOUT_SEC", 0.1)
    monkeypatch.setattr(_base, "SESSION_CLOSE_TIMEOUT_SEC", 0.5)
    dialling, release = asyncio.Event(), asyncio.Event()
    socket = HangingDial(dialling, release, close_sec=close_sec)
    conn = OpenAILiveConnection(api_key="test", connect=lambda: socket)
    await conn.start(ToolRegistry(), "Be concise.")

    acquire = asyncio.create_task(conn.acquire_turn())
    await wait_signalled(dialling, "the dial starting", producer=acquire)
    started = time.monotonic()
    await conn.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert conn._state is ConnectionState.CLOSED
    # The socket is still handed to its own unwind on the way out.
    assert socket.exits == 1
    assert event_fields(caplog, "provider.close_failed")["phase"] == "release"
    assert event_fields(caplog, "provider.turn_ended")["provider"] == "openai_live"
    release.set()
    with pytest.raises(RuntimeError):
        await acquire


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
    fields = event_fields(caplog, "provider.turn_ended")
    assert fields["provider"] == "openai_live"
    assert int(fields["chunks_received"]) == 1
    assert int(fields["quiet_played"]) == played
    assert int(fields["quiet_discarded"]) == discarded


async def test_the_first_audible_delta_reports_provider_latency(caplog):
    """Live joins the other adapters on `turn.first_chunk`, and fires it on
    the first AUDIBLE delta: idle PCM is not the model starting to speak."""
    caplog.set_level(logging.INFO)
    async with live_turn() as turn:
        await turn.on_event(output_audio(QUIET_PCM))
        assert event_records(caplog, "turn.first_chunk") == []
        await turn.on_event(output_audio(AUDIBLE_PCM))
        fields = event_fields(caplog, "turn.first_chunk")
    assert fields["provider"] == "openai_live"
    assert int(fields["since_turn_start_ms"]) >= 0


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


@pytest.mark.parametrize("finish", ["filled", "short", "continuation", "late_clause", "interrupt", "cancel", "release", "lost"])
async def test_playout_reserve_is_bounded_and_rearms_after_interrupt(monkeypatch, finish):
    clock = FrozenClock()
    monkeypatch.setattr(openai_live_session, "time", clock)
    if finish in {"cancel", "release", "lost"}:
        monkeypatch.setattr(openai_live_session, "PLAYOUT_RESERVE_SEC", 1.0)
    pcm = AUDIBLE_PCM * 30  # 150 ms: two deltas cross the 250 ms reserve.
    async with live_turn() as turn:
        audio = turn.audio_out_chunks()
        pending = asyncio.create_task(anext(audio))
        release = None
        closing = asyncio.Event()
        try:
            await turn.on_event(output_audio(pcm))
            await asyncio.sleep(0.01)
            assert not pending.done()
            assert turn.audio_chunks_pending() == 1
            if finish in {"cancel", "release", "lost"}:
                if finish == "cancel":
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    if finish == "release":
                        async def close():
                            await closing.wait()

                        monkeypatch.setattr(turn._conn, "_close_live_session", close)
                        release = asyncio.create_task(turn.release())
                    else:
                        turn._on_connection_lost()
                    if finish == "lost":
                        assert (await asyncio.wait_for(pending, 0.2)).pcm == pcm
                        with pytest.raises(StopAsyncIteration):
                            await anext(audio)
                        assert turn.audio_dropped_bytes() == 0
                    else:
                        with pytest.raises(StopAsyncIteration):
                            await asyncio.wait_for(pending, 0.2)
                    if finish == "release":
                        assert not release.done()
                        closing.set()
                        await release
                assert turn._queued_bytes == 0
                return
            if finish == "short":
                assert (await asyncio.wait_for(pending, 0.4)).pcm == pcm
                return
            await turn.on_event(output_audio(pcm))
            assert (await asyncio.wait_for(pending, 0.2)).pcm == pcm
            assert (await anext(audio)).pcm == pcm
            assert turn.audio_chunks_pending() == 0
            if finish in {"continuation", "late_clause", "interrupt"}:
                clock.now += 0.1 if finish == "continuation" else SILENCE_BRIDGE_SEC + 0.1
                if finish == "interrupt":
                    turn.drop_pending_audio()
                else:
                    # A short trailing clause must arrive without a second fill wait.
                    monkeypatch.setattr(openai_live_session, "PLAYOUT_RESERVE_SEC", 10.0)
                pending = asyncio.create_task(anext(audio))
                await asyncio.sleep(0)
                await turn.on_event(output_audio(pcm))
                if finish == "interrupt":
                    await asyncio.sleep(0.01)
                    assert not pending.done()
                    await turn.on_event(output_audio(pcm))
                assert (await asyncio.wait_for(pending, 0.2)).pcm == pcm
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await audio.aclose()
            closing.set()
            if release is not None:
                await release


@pytest.mark.parametrize("arm_when", ["queued", "parked"])
async def test_each_answer_reserves_playout_and_excludes_backend_think_time(monkeypatch, caplog, arm_when):
    clock = FrozenClock()
    monkeypatch.setattr(openai_live_session, "time", clock)
    monkeypatch.setattr(openai_live_session, "PLAYOUT_RESERVE_SEC", 0.2)
    caplog.set_level(logging.INFO)
    pcm = AUDIBLE_PCM * 30
    async with live_turn() as turn:
        @tool()
        def lookup() -> dict:
            """Look up the answer."""
            return {"answer": 42}

        turn._conn._registry.register(lookup)
        audio = turn.audio_out_chunks()
        played = asyncio.Queue()

        async def consume():
            async for chunk in audio:
                played.put_nowait(chunk.pcm)

        consumer = asyncio.create_task(consume())
        try:
            await asyncio.sleep(0)
            for answer in (1, 2):
                if answer == 2:
                    clock.now += SILENCE_BRIDGE_SEC + 0.1
                    await turn.on_event(backend("d1", "response.created", response={"id": "tool_round"}))
                    await turn.on_event(backend("d1", "response.output_item.done", item={
                        "type": "function_call", "call_id": "lookup_call", "name": "lookup", "arguments": "{}",
                    }))
                    await turn.on_event(backend("d1", "response.completed", response={"id": "tool_round"}))
                    await asyncio.wait_for(turn._tool_task, 1.0)
                    assert turn._conn._session.sent[-1] == {"type": "response.create"}
                    assert turn.backend_pending
                else:
                    await turn.on_event({"type": "session.delegation.created", "delegation": {"id": "d1"}})
                await turn.on_event(output_audio(pcm))
                await asyncio.sleep(0)
                assert played.empty()
                await turn.on_event(output_audio(pcm))
                assert await asyncio.wait_for(played.get(), 1.0) == pcm
                assert await asyncio.wait_for(played.get(), 1.0) == pcm
                assert turn.audio_chunks_pending() == 0
            reserves = event_field_maps(caplog, "provider.playout_reserve")
            assert len(reserves) == 2
            assert [fields["result"] for fields in reserves] == ["filled", "filled"]
            assert event_records(caplog, "provider.output_deficit") == []
            monkeypatch.setattr(openai_live_session, "PLAYOUT_RESERVE_SEC", 10.0)
            if arm_when == "queued":
                await turn.on_event(output_audio(pcm))
                await turn.on_event({"type": "session.delegation.created", "delegation": {"id": "d3"}})
                await turn.on_event(backend("d3", "response.created", response={"id": "r3"}))
            clock.now += 0.1
            await turn.on_event(output_audio(pcm))
            expected_chunks = 2 if arm_when == "queued" else 1
            assert turn.audio_chunks_pending() == expected_chunks
            for _ in range(expected_chunks):
                assert await asyncio.wait_for(played.get(), 0.2) == pcm
            assert turn.audio_chunks_pending() == 0
            if arm_when == "queued":
                await turn.on_event(backend("d3", "response.completed", response={"id": "r3"}))
            clock.now += 0.1
            await turn.on_event(output_audio(pcm))
            assert await asyncio.wait_for(played.get(), 0.2) == pcm
            assert event_field_maps(caplog, "provider.playout_reserve") == reserves
            assert turn.audio_chunks_pending() == 0
            clock.now += SILENCE_BRIDGE_SEC + 0.1
            monkeypatch.setattr(openai_live_session, "PLAYOUT_RESERVE_SEC", 0.2)
            if arm_when == "parked":
                await turn.on_event({"type": "session.delegation.created", "delegation": {"id": "d3"}})
            await turn.on_event(output_audio(pcm))
            await asyncio.sleep(0)
            assert played.empty()
            await turn.on_event(output_audio(pcm))
            assert await asyncio.wait_for(played.get(), 1.0) == pcm
            assert await asyncio.wait_for(played.get(), 1.0) == pcm
            reserves = event_field_maps(caplog, "provider.playout_reserve")
            assert len(reserves) == 3
            assert [fields["result"] for fields in reserves] == ["filled", "filled", "filled"]
            assert event_records(caplog, "provider.output_deficit") == []
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await audio.aclose()


@pytest.mark.parametrize("gap_sec, quiet, expected", [
    (0.1, False, 0), (0.2, False, 1), (0.2, True, 1),
    (SILENCE_BRIDGE_SEC, False, 1), (6.0, False, 0), (6.0, True, 0),
])
async def test_output_deficit_excludes_idle_between_answers(monkeypatch, caplog, gap_sec, quiet, expected):
    clock = FrozenClock()
    monkeypatch.setattr(openai_live_session, "time", clock)
    caplog.set_level(logging.INFO)
    async with live_turn() as turn:
        await turn.on_event(output_audio(AUDIBLE_PCM * 20))
        clock.now += gap_sec
        await turn.on_event(output_audio(QUIET_PCM if quiet else AUDIBLE_PCM))
        assert len(event_records(caplog, "provider.output_deficit")) == expected
        if expected:
            fields = event_fields(caplog, "provider.output_deficit")
            assert fields["provider"] == "openai_live"
            assert int(fields["deficit_ms"]) == pytest.approx((gap_sec - 0.1) * 1000, abs=1.1)


async def test_live_barge_in_reports_and_discards_queued_audio(monkeypatch, caplog):
    monkeypatch.setattr(openai_live_session, "PLAYOUT_RESERVE_SEC", 0.0)
    caplog.set_level(logging.INFO)
    async with live_turn() as turn:
        for _ in range(5):
            await turn.on_event(output_audio(AUDIBLE_PCM))

        async def accepted():
            turn.request_local_interrupt()
            await asyncio.Event().wait()

        tts = FakeTts()
        report = PlaybackReport()
        await asyncio.wait_for(play_responses(turn, tts, report=report, on_first_write=accepted), 1.0)
        assert report.stop_reason == "barge_in"
        assert len(tts.writes) == tts.flush_calls == 1
        assert int(event_fields(caplog, "barge.dropped_pending_audio")["chunks"]) == 4
        assert turn.audio_chunks_pending() == turn.audio_dropped_bytes() == 0


@pytest.mark.parametrize("count", [0, 5])
async def test_connection_loss_drains_received_audio_without_waiting_for_a_reserve(monkeypatch, count):
    monkeypatch.setattr(openai_live_session, "PLAYOUT_RESERVE_SEC", 10.0)
    async with live_turn() as turn:
        for _ in range(count):
            await turn.on_event(output_audio(AUDIBLE_PCM))
        turn._on_connection_lost()

        async def drain():
            return [chunk.pcm async for chunk in turn.audio_out_chunks()]

        assert await asyncio.wait_for(drain(), 0.2) == [AUDIBLE_PCM] * count
        assert turn.audio_dropped_bytes() == turn.audio_chunks_pending() == 0


@pytest.mark.parametrize("finish", ["close", "lost", "interrupt"])
async def test_playback_cleanup_preserves_overflow_for_final_accounting(monkeypatch, finish):
    pcm = b"\x00\x40" * 2
    monkeypatch.setattr(_base, "AUDIO_OUT_QUEUE_MAX_BYTES", 2 * len(pcm))
    async with live_turn() as turn:
        audio = turn.audio_out_chunks()
        try:
            await turn.on_event(output_audio(pcm))
            await turn.on_event(output_audio(pcm))
            await turn.on_event(output_audio(pcm))
            assert turn.audio_dropped_bytes() == len(pcm)
            assert (await anext(audio)).pcm == pcm
            if finish == "lost":
                turn._on_connection_lost()
            await audio.aclose()
            assert turn.audio_dropped_bytes() == 2 * len(pcm)
            if finish == "interrupt":
                turn.drop_pending_audio()
            expected = 0 if finish == "interrupt" else 2 * len(pcm)
            await turn.release()
            assert turn.audio_dropped_bytes() == expected
        finally:
            await audio.aclose()


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


@pytest.mark.parametrize("code, transient", [("unknown_parameter", False), ("rate_limit_exceeded", True)])
async def test_startup_rejection_preserves_retry_and_cue(code, transient, caplog):
    caplog.set_level(logging.DEBUG)
    sockets = []

    class RejectedStart(LiveSocket):
        async def send(self, event):
            self.sent.append(event)
            await self.events.put({"type": "error", "error": {
                "code": code, "type": "invalid_request_error",
                "message": "rejected private-test-credential",
            }})

    def connect():
        socket = RejectedStart()
        sockets.append(socket)
        return socket

    async def sleep(_):
        pass

    conn = OpenAILiveConnection(api_key="private-test-credential", connect=connect)
    conn._sleep = sleep
    await conn.start(ToolRegistry(), "Be brief.")
    try:
        with pytest.raises((ValueError, RuntimeError)) as failure:
            await conn.acquire_turn()
        assert is_transient(failure.value) is transient
        assert conn.wake_cue() == (CANT_CONNECT_CUE_SLUG if transient else NEEDS_ATTENTION_CUE_SLUG)
        assert len(sockets) == (openai_live_session.SESSION_OPEN_ATTEMPTS if transient else 1)
        assert all(socket.closed for socket in sockets)
        assert "private-test-credential" not in str(failure.value)
        assert "private-test-credential" not in conn.last_failure_detail()
        assert "private-test-credential" not in caplog.text
    finally:
        await conn.stop()


async def test_a_rejected_command_reports_the_server_error_code_and_type(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(openai_live_session, "CLOSE_ACK_TIMEOUT_SEC", 0.05)
    key = "private-test-credential"
    socket = LiveSocket()
    conn = OpenAILiveConnection(api_key=key, connect=lambda: socket)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    try:
        await socket.events.put({
            "type": "error",
            "event_id": "evt_1",
            "error": {
                "code": "unknown_parameter",
                "type": "invalid_request_error",
                "message": f"Unknown parameter: session.nope (sent with {key})",
            },
        })
        await wait_until(turn.turn_lost)
    finally:
        await turn.release()
        await conn.stop()

    fields = event_fields(caplog, "provider.server_error")
    assert fields["code"] == "unknown_parameter"
    assert fields["error_type"] == "invalid_request_error"
    detail = event_fields(caplog, "provider.session_lost")["detail"]
    assert "unknown_parameter" in detail and "invalid_request_error" in detail
    for record in caplog.records:
        assert key not in record.getMessage()
