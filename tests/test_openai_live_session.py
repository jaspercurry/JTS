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
from jasper.voice import _base, openai_live_session
from jasper.voice._supervisor import CANT_CONNECT_CUE_SLUG, OUT_OF_CREDIT_CUE_SLUG
from jasper.voice.conversation import END_CONVERSATION_TOOL, register_conversation_tools
from jasper.voice.openai_live_session import SILENCE_BRIDGE_SEC, OpenAILiveConnection
from jasper.voice.session import ConnectionState
from tests._async_wait import wait_signalled, wait_until
from tests._log_events import event_field_maps, event_fields


CLIENT_EVENT = TypeAdapter(ClientEventParam)


@pytest.fixture(autouse=True)
def _warm_toggle_off_by_default(tmp_path, monkeypatch):
    """Point the warm-session toggle's SSOT file at an absent path.

    The adapter reads it fresh per conversation, so without this a host
    whose real `/var/lib/jasper/voice_provider.env` has the toggle on
    would send every test in this file down the warm path.
    """
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(tmp_path / "absent.env"))


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


# --- The warm session (ADR-0295) --------------------------------------


class _Meter:
    """The billable-activity meter, recording the intervals it is told to open."""

    def __init__(self):
        self.events = []

    def mark_started(self) -> None:
        self.events.append("open")

    def mark_ended(self, *, seconds=None) -> None:
        self.events.append(("close", seconds))


def _live(socket, *, warm: bool) -> OpenAILiveConnection:
    return OpenAILiveConnection(
        api_key="test",
        connect=(socket if callable(socket) else (lambda: socket)),
        warm_session=lambda: warm,
    )


@pytest.mark.parametrize("warm, closed_on_release", [(False, True), (True, False)])
async def test_the_toggle_decides_whether_a_conversation_closes_its_session(
    warm, closed_on_release,
):
    socket = LiveSocket()
    conn = _live(socket, warm=warm)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    await turn.release()
    try:
        sent_close = "session.close" in [e["type"] for e in socket.sent]
        assert sent_close is closed_on_release
        assert socket.closed is closed_on_release
        assert (conn.warm_session_until() is None) is closed_on_release
    finally:
        await conn.stop()


async def test_a_wake_inside_the_window_reuses_the_session_and_pays_one_interval(caplog):
    """The window buys back the dial, and costs one billable interval.

    Nothing reaches the provider between the two conversations — not even
    the sender's own silence — and the session the second wake answers on
    is the one the first left open, so no second `session.start` goes out.
    """
    caplog.set_level(logging.INFO)
    socket = LiveSocket()
    meter = _Meter()
    conn = _live(socket, warm=True)
    conn.set_billable_activity_meter(meter)
    await conn.start(ToolRegistry(), "Be brief.")

    first = await conn.acquire_turn()
    await first.send_audio(b"\x00\x40" * 640)
    await wait_until(lambda: any(e["type"] == "session.input_audio.append" for e in socket.sent))
    await first.release()
    assert conn.warm_session_until() is not None

    quiet_at = len(socket.sent)
    await first.send_audio(b"\x00\x40" * 640)
    # Several sender ticks (80 ms each) of warm time with a live mic.
    await asyncio.sleep(0.25)
    assert len(socket.sent) == quiet_at

    second = await conn.acquire_turn()
    try:
        assert [e["type"] for e in socket.sent].count("session.start") == 1
        assert conn.warm_session_until() is None
        assert not second.turn_lost()
        # The reused session still reaches the turn that now owns it.
        await socket.events.put(output_audio(AUDIBLE_PCM))
        await wait_until(lambda: second.chunks_received() == 1)
    finally:
        await second.release()
        await conn.stop()

    reused = [f["session_reused"] for f in event_field_maps(caplog, "provider.turn_ended")]
    assert reused == ["false", "true"]
    # ONE interval for the whole session, closed on the server's own meter.
    assert meter.events == ["open", ("close", 12.5)]


async def test_a_warm_session_nobody_comes_back_for_closes_itself(monkeypatch):
    """The idle close is the turn's own close, just later."""
    monkeypatch.setattr(openai_live_session, "WARM_SESSION_SEC", 0.05)
    socket = LiveSocket()
    meter = _Meter()
    conn = _live(socket, warm=True)
    conn.set_billable_activity_meter(meter)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    await turn.release()

    await wait_until(lambda: socket.closed)
    try:
        assert [e["type"] for e in socket.sent].count("session.close") == 1
        assert conn.warm_session_until() is None
        assert meter.events == ["open", ("close", 12.5)]
        assert conn._state is ConnectionState.CONNECTED
    finally:
        await conn.stop()


@pytest.mark.parametrize("server_event", [
    {"type": "session.closed", "usage": {"seconds": 7.0}},
    {"type": "error"},
])
async def test_a_session_the_server_closed_while_warm_costs_the_next_wake_a_dial(
    monkeypatch, server_event,
):
    """Losing a warm session is not an outage — the next wake just dials.

    No cue, no recorded failure, no lost turn: the household cannot tell
    this wake from one that never had a warm session to reuse.
    """
    monkeypatch.setattr(openai_live_session, "CLOSE_ACK_TIMEOUT_SEC", 0.05)
    sockets = []
    cues = []

    def connect():
        sockets.append(LiveSocket())
        return sockets[-1]

    async def cue_cb(slug: str) -> None:
        cues.append(slug)

    conn = _live(connect, warm=True)
    conn.set_failure_escalation_cb(cue_cb)
    await conn.start(ToolRegistry(), "Be brief.")
    first = await conn.acquire_turn()
    await first.release()

    await sockets[0].events.put(server_event)
    await wait_until(lambda: conn._receive_task.done())

    second = await conn.acquire_turn()
    try:
        assert len(sockets) == 2
        assert [e["type"] for e in sockets[1].sent].count("session.start") == 1
        assert not second.turn_lost()
        assert cues == []
        assert conn.last_failure_detail() is None
    finally:
        await second.release()
        await conn.stop()


async def test_stop_closes_a_session_left_warm():
    """Shutdown is the last chance to take a warm session off the meter."""
    socket = LiveSocket()
    meter = _Meter()
    conn = _live(socket, warm=True)
    conn.set_billable_activity_meter(meter)
    await conn.start(ToolRegistry(), "Be brief.")
    turn = await conn.acquire_turn()
    await turn.release()
    assert conn.warm_session_until() is not None

    await conn.stop()

    assert [e["type"] for e in socket.sent].count("session.close") == 1
    assert socket.closed
    assert conn.warm_session_until() is None
    assert meter.events == ["open", ("close", 12.5)]
    assert conn._state is ConnectionState.CLOSED
