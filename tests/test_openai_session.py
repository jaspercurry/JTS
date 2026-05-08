"""Contract tests for the OpenAI Realtime adapter.

Mirrors test_gemini_connection.py: a fake ``connect_factory`` stands in
for ``client.realtime.connect`` so the tests drive event flow without
touching the network. Each test pins one piece of wire-format
behaviour the daemon depends on (manual VAD, tool round-trip, response
done, reconnect) so a future SDK upgrade or model rollout can't
silently break the production path.

The same fakes exercise ``GrokRealtimeConnection`` since Grok inherits
the OpenAI adapter.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from jasper.tools import ToolRegistry, tool
from jasper.voice.openai_session import (
    ConnectionState,
    OpenAIRealtimeConnection,
    _is_transient,
    _upsample_16k_to_24k,
)
from jasper.voice.grok_session import GROK_WEBSOCKET_BASE_URL, GrokRealtimeConnection


# ---------------------------------------------------------------------------
# Fake SDK plumbing.
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal substitute for the openai SDK's AsyncRealtimeConnection.

    Tracks every ``send(event)`` call so tests can assert which client
    events were issued (session.update, input_audio_buffer.append,
    input_audio_buffer.commit, response.create, conversation.item.create,
    etc.). ``feed(event)`` queues a server-event dict for the receive
    loop to pick up; ``feed_error(exc)`` simulates a WebSocket drop."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, event: dict) -> None:
        self.sent.append(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if isinstance(item, _IterStop):
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    # Test helpers.
    def feed(self, event: dict) -> None:
        self._inbox.put_nowait(event)

    def feed_error(self, exc: BaseException) -> None:
        self._inbox.put_nowait(exc)

    def feed_iter_stop(self) -> None:
        self._inbox.put_nowait(_IterStop())


class _IterStop:
    pass


class _FakeAsyncCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeConnectFactory:
    """Stand-in for ``client.realtime.connect`` (callable as
    ``factory(model="...")`` and returning an async context manager)."""

    def __init__(self) -> None:
        self.conns: list[_FakeConn] = []
        self.models: list[str] = []
        # Optional queue of exceptions: each call pops one and raises it.
        self.next_exceptions: list[BaseException] = []

    def __call__(self, *, model: str) -> _FakeAsyncCM:
        if self.next_exceptions:
            exc = self.next_exceptions.pop(0)
            raise exc
        self.models.append(model)
        c = _FakeConn()
        self.conns.append(c)
        return _FakeAsyncCM(c)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_conn(
    *,
    backoff_schedule=(0.0, 0.0),
    context_reset_sec: float = 9999.0,
    model: str = "gpt-realtime-2",
    voice: str = "marin",
    reasoning_effort: str = "low",
) -> tuple[OpenAIRealtimeConnection, _FakeConnectFactory]:
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        model=model,
        voice=voice,
        context_reset_sec=context_reset_sec,
        reasoning_effort=reasoning_effort,
        backoff_schedule=backoff_schedule,
        connect_factory=factory,
    )
    return conn, factory


async def _wait_until(predicate, timeout: float = 2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"predicate never became true within {timeout}s")


def _b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def _find_event(sent: list[dict], event_type: str) -> dict | None:
    for e in sent:
        if e.get("type") == event_type:
            return e
    return None


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def test_upsample_16k_to_24k_produces_correct_length():
    """80 ms of 16 kHz mono int16 → 80 ms of 24 kHz mono int16. The
    polyphase ratio is 3:2, so output samples = input samples * 3 / 2.
    Checks the math, not just that the call succeeds."""
    # 80 ms * 16000 Hz = 1280 samples * 2 bytes = 2560 bytes.
    pcm_16k = b"\x00\x00" * 1280
    out, state = _upsample_16k_to_24k(pcm_16k, None)
    assert len(out) > 0
    # 80 ms * 24000 Hz = 1920 samples * 2 bytes = 3840 bytes ± 1 sample
    # of edge effect. ratecv may emit slightly fewer on the very first
    # call as the filter warms up; allow a small slack.
    assert 3800 <= len(out) <= 3840


def test_upsample_state_continuity_across_chunks():
    """State persistence is the whole point — passing None in for every
    frame causes audible discontinuities at frame boundaries. Two
    successive 40 ms chunks with state should yield ~80 ms total."""
    pcm = b"\x00\x00" * 640  # 40 ms @ 16 kHz
    out1, s1 = _upsample_16k_to_24k(pcm, None)
    out2, _ = _upsample_16k_to_24k(pcm, s1)
    assert len(out1) + len(out2) >= 3700


def test_is_transient_classifies_correctly():
    """Auth/config errors don't retry; network/5xx do."""
    class _Auth:
        status_code = 401
    class _Conflict:
        status_code = 409
    class _RateLimit:
        status_code = 429
    class _ServerError:
        status_code = 502

    assert _is_transient(_Auth()) is False
    assert _is_transient(_Conflict()) is True
    assert _is_transient(_RateLimit()) is True
    assert _is_transient(_ServerError()) is True
    # Generic errors with no status: assume transient.
    assert _is_transient(OSError("network blip")) is True
    # Local-validation errors: never retry.
    assert _is_transient(ValueError("bad config")) is False
    assert _is_transient(TypeError("wrong shape")) is False


# ---------------------------------------------------------------------------
# Tests against a live (faked) connection.
# ---------------------------------------------------------------------------


async def test_session_update_sent_on_connect_with_manual_vad():
    """The very first event we send after the WebSocket handshake is
    ``session.update`` with ``turn_detection: None`` (manual VAD),
    ``audio.input.format`` = audio/pcm @ 24 kHz, the configured voice,
    and the tool list. Covers a critical wire-format expectation —
    messing this up means the server picks up server VAD and the
    daemon's wake/silence detector does nothing."""
    conn, factory = _make_conn()
    registry = ToolRegistry()

    @tool()
    def get_volume() -> dict:
        """Return current volume."""
        return {"percent": 50}
    registry.register(get_volume)

    await conn.start(registry, "system instruction text")
    try:
        sess = factory.conns[0]
        # Find session.update among sent events.
        upd = _find_event(sess.sent, "session.update")
        assert upd is not None
        sess_payload = upd["session"]
        assert sess_payload["model"] == "gpt-realtime-2"
        assert sess_payload["instructions"] == "system instruction text"
        # Voice belongs INSIDE audio.output.voice. Putting it at the
        # session top level was the live-deploy bug — OpenAI rejected
        # session.update with `Unknown parameter: 'session.voice'`,
        # which silently nuked the entire session config (no tools, no
        # voice config) and the model auto-responded with defaults
        # without ever calling tools. Pin BOTH the correct location
        # AND the absence of the wrong location.
        assert sess_payload["audio"]["output"]["voice"] == "marin"
        assert "voice" not in sess_payload, (
            "voice MUST NOT be at the session top level — the Realtime "
            "schema rejects it there. It belongs in audio.output.voice."
        )
        # Manual VAD is the canonical Python None / JSON null.
        assert sess_payload["audio"]["input"]["turn_detection"] is None
        assert sess_payload["audio"]["input"]["format"] == {
            "type": "audio/pcm", "rate": 24000,
        }
        assert sess_payload["audio"]["output"]["format"] == {
            "type": "audio/pcm", "rate": 24000,
        }
        # `temperature` was REMOVED from the Realtime 2 session schema.
        # Sending it doesn't currently error (server seems to ignore)
        # but the SDK type doesn't list it and it may start erroring in
        # a future release.
        assert "temperature" not in sess_payload, (
            "temperature is not in the Realtime 2 session schema; "
            "the model has its own defaults"
        )
        # Tools serialised in the OpenAI Realtime flat shape.
        assert sess_payload["tools"] == [{
            "type": "function",
            "name": "get_volume",
            "description": "Return current volume.",
            "parameters": {"type": "object", "properties": {}},
        }]
        # Reasoning effort for gpt-realtime-2.
        assert sess_payload["reasoning"] == {"effort": "low"}
    finally:
        await conn.stop()


async def test_reasoning_effort_skipped_for_non_dash2_models():
    """``reasoning.effort`` is only meaningful on reasoning-capable
    models (gpt-realtime-2). On gpt-realtime-mini it must be omitted —
    the SDK rejects unknown fields. Adapter checks via "-2" substring."""
    conn, factory = _make_conn(model="gpt-realtime-mini")
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        upd = _find_event(factory.conns[0].sent, "session.update")
        assert upd is not None
        assert "reasoning" not in upd["session"]
    finally:
        await conn.stop()


async def test_provider_locked_tools_filtered_from_session_update():
    """A tool tagged ``providers={"gemini"}`` must not appear in the
    OpenAI session.update tool list — the model literally cannot see
    it. Same registry powers all three providers, so this is the
    front-line guarantee that hidden tools stay hidden."""
    conn, factory = _make_conn()
    registry = ToolRegistry()

    @tool(providers={"gemini"})
    def gemini_only() -> dict:
        """."""
        return {}

    @tool()
    def universal() -> dict:
        """."""
        return {}

    registry.register(gemini_only)
    registry.register(universal)

    await conn.start(registry, "")
    try:
        upd = _find_event(factory.conns[0].sent, "session.update")
        names = {t["name"] for t in upd["session"]["tools"]}
        assert names == {"universal"}
    finally:
        await conn.stop()


async def test_send_audio_emits_input_audio_buffer_append_with_base64_pcm():
    """Each turn.send_audio call must produce one
    ``input_audio_buffer.append`` event with base64-encoded 24 kHz PCM
    (the input arrived as 16 kHz; the adapter must upsample)."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        before = len([e for e in sess.sent if e.get("type") == "input_audio_buffer.append"])
        turn = await conn.acquire_turn()
        # 80 ms of silence at 16 kHz = 2560 bytes.
        await turn.send_audio(b"\x00\x00" * 1280)
        after = [e for e in sess.sent if e.get("type") == "input_audio_buffer.append"]
        assert len(after) == before + 1
        chunk = after[-1]
        # Base64 round-trips to non-empty bytes.
        decoded = base64.b64decode(chunk["audio"])
        assert len(decoded) > 0
        # bytes_sent counts ORIGINAL 16 kHz size — it's the daemon's
        # silent-failure heuristic, sized in mic-frame bytes.
        assert turn.bytes_sent() == 2560
        await turn.release()
    finally:
        await conn.stop()


async def test_end_input_sends_commit_and_response_create_in_order():
    """Manual-VAD turn close: ``input_audio_buffer.commit`` then
    ``response.create``. Order matters — sending response.create before
    commit is a no-op on an empty buffer.

    Also confirms idempotence: calling end_input twice doesn't double-
    send."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await turn.send_audio(b"\x00\x00" * 1280)

        baseline = len(sess.sent)
        await turn.end_input()
        new = sess.sent[baseline:]
        types_in_order = [e["type"] for e in new]
        assert "input_audio_buffer.commit" in types_in_order
        assert "response.create" in types_in_order
        assert types_in_order.index("input_audio_buffer.commit") < types_in_order.index("response.create")

        # Idempotent.
        before_second = len(sess.sent)
        await turn.end_input()
        assert len(sess.sent) == before_second
        await turn.release()
    finally:
        await conn.stop()


async def test_audio_delta_event_routes_to_active_turn_audio_queue():
    """Server pushes ``response.output_audio.delta`` events with base64-
    encoded PCM. Each one should appear in ``turn.audio_out()`` with
    the bytes decoded."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        # Fake server response: one audio chunk + done.
        sess.feed({
            "type": "response.output_audio.delta",
            "delta": _b64(b"audio_chunk_1"),
            "response_id": "resp_1",
        })
        sess.feed({
            "type": "response.done",
            "response": {"usage": {"input_tokens": 12, "output_tokens": 34}},
        })

        async def consume() -> list[bytes]:
            chunks = []
            async for chunk in turn.audio_out():
                chunks.append(chunk)
                if len(chunks) >= 1:
                    break
            return chunks

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await turn.end_input()
        await turn.release()
        chunks = await asyncio.wait_for(task, timeout=1.0)
        assert chunks == [b"audio_chunk_1"]
        assert turn.server_turn_complete() is True
        assert turn.usage_tokens() == {"input_tokens": 12, "output_tokens": 34}
    finally:
        await conn.stop()


async def test_function_call_round_trip():
    """Server emits ``response.function_call_arguments.done`` with a
    JSON-string ``arguments`` field; daemon must:
      1. Parse the JSON.
      2. Dispatch the registered tool.
      3. Send ``conversation.item.create`` with type
         ``function_call_output`` and the result as a JSON string under
         ``output``.
      4. Send ``response.create`` so the model resumes."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    captured = {}

    @tool()
    def set_volume(percent: int) -> dict:
        """Set volume."""
        captured["percent"] = percent
        return {"ok": True, "percent": percent}
    registry.register(set_volume)

    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()

        # Server fires the function call.
        sess.feed({
            "type": "response.function_call_arguments.done",
            "call_id": "call_abc",
            "name": "set_volume",
            "arguments": json.dumps({"percent": 30}),
            "response_id": "resp_1",
        })
        # Wait for the tool dispatch to complete and the reply events to
        # land in sess.sent.
        await _wait_until(
            lambda: any(
                e.get("type") == "conversation.item.create"
                and e.get("item", {}).get("type") == "function_call_output"
                for e in sess.sent
            ),
            timeout=2.0,
        )
        # Tool actually invoked.
        assert captured == {"percent": 30}

        # Find the function_call_output event.
        item_create = None
        for e in sess.sent:
            if (
                e.get("type") == "conversation.item.create"
                and e.get("item", {}).get("type") == "function_call_output"
            ):
                item_create = e
                break
        assert item_create is not None
        item = item_create["item"]
        assert item["call_id"] == "call_abc"
        # ``output`` is a JSON string per OpenAI's wire format.
        assert json.loads(item["output"]) == {"ok": True, "percent": 30}

        # response.create is fired right after — the model is told to
        # resume and produce a verbal response.
        idx_create = sess.sent.index(item_create)
        post = sess.sent[idx_create + 1:]
        assert any(e.get("type") == "response.create" for e in post)

        await turn.release()
    finally:
        await conn.stop()


async def test_tool_call_response_done_does_NOT_complete_turn():
    """A tool-using turn produces TWO response.done events from
    OpenAI: one closing the tool-call response (no audio), then one
    closing the audio answer. The first MUST NOT flip
    server_turn_complete — if it does, the daemon's idle watchdog
    closes the turn before the actual audio answer streams in, and
    the user hears the model cut off mid-sentence.

    This was the live-deploy bug behind "she keeps cutting out":
    7 audio chunks received per turn, ~175ms after the tool result
    came back. The model did everything right; my dispatcher ended
    the turn too early.

    Drives the full two-response sequence and checks server_turn_complete
    after each step."""
    conn, factory = _make_conn()
    registry = ToolRegistry()

    @tool()
    def get_weather(location: str = "") -> dict:
        """."""
        return {"location": "Brooklyn", "temperature": 62}
    registry.register(get_weather)

    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        assert turn.server_turn_complete() is False

        # ROUND 1: model decides to call the tool.
        sess.feed({
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": "{}",
            "response_id": "resp_1",
        })
        # Wait for our function_call_output to land.
        await _wait_until(
            lambda: any(
                e.get("type") == "conversation.item.create"
                and e.get("item", {}).get("type") == "function_call_output"
                for e in sess.sent
            ),
            timeout=2.0,
        )

        # ROUND 1 close: server emits response.done for the tool-call
        # response. CRITICAL: this MUST NOT flip server_turn_complete.
        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 100, "output_tokens": 8},
            },
        })
        await asyncio.sleep(0.05)
        assert turn.server_turn_complete() is False, (
            "server_turn_complete must remain False after the tool-call "
            "response.done — the audio answer hasn't streamed yet. "
            "Flipping True here is what made the daemon cut off the "
            "model mid-sentence in the live deploy."
        )

        # ROUND 2: server streams the actual audio answer.
        sess.feed({
            "type": "response.output_audio.delta",
            "delta": _b64(b"answer_audio_1"),
            "response_id": "resp_2",
        })
        sess.feed({
            "type": "response.output_audio.delta",
            "delta": _b64(b"answer_audio_2"),
            "response_id": "resp_2",
        })
        # ROUND 2 close: real end of turn — audio answer is complete.
        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_2",
                "usage": {"input_tokens": 50, "output_tokens": 200},
            },
        })

        # Drain audio + wait for completion flag to flip.
        async def consume():
            chunks = []
            async for chunk in turn.audio_out():
                chunks.append(chunk)
                if len(chunks) >= 2:
                    break
            return chunks

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await turn.end_input()
        await turn.release()
        chunks = await asyncio.wait_for(consumer, timeout=1.0)
        assert chunks == [b"answer_audio_1", b"answer_audio_2"]
        # NOW server_turn_complete should be True (set by the second
        # response.done, before release).
        assert turn.server_turn_complete() is True

        # Token usage should ACCUMULATE across both responses, not
        # just report the second one. The spend cap charges the
        # full round-trip.
        usage = turn.usage_tokens()
        assert usage["input_tokens"] == 150  # 100 + 50
        assert usage["output_tokens"] == 208  # 8 + 200
    finally:
        await conn.stop()


async def test_unknown_tool_call_returns_error_payload():
    """If the model hallucinates a tool name we don't know about, we
    still must reply (otherwise the model hangs waiting for the
    function_call_output). Reply carries a JSON ``error`` field."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        await conn.acquire_turn()
        sess.feed({
            "type": "response.function_call_arguments.done",
            "call_id": "call_x",
            "name": "no_such_tool",
            "arguments": "{}",
        })
        await _wait_until(
            lambda: any(
                e.get("type") == "conversation.item.create" for e in sess.sent
            ),
            timeout=2.0,
        )
        item_create = next(
            e for e in sess.sent
            if e.get("type") == "conversation.item.create"
        )
        body = json.loads(item_create["item"]["output"])
        assert "error" in body
    finally:
        await conn.stop()


async def test_reconnect_with_backoff_eventually_succeeds():
    """A WebSocket drop wakes the supervisor; the next backoff slot
    reopens the connection. State machine cycles through
    RECONNECTING → PAUSED_FOR_BACKOFF → CONNECTING → CONNECTED."""
    conn, factory = _make_conn(backoff_schedule=(0.0, 0.05))
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        first = factory.conns[0]
        # Inject a WebSocket-style close.
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()
        first.feed_error(_Drop())

        await _wait_until(lambda: len(factory.conns) >= 2, timeout=3.0)
        await _wait_until(
            lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0,
        )
        # Connection is usable again.
        turn = await conn.acquire_turn()
        await turn.release()
    finally:
        await conn.stop()


async def test_repeated_failures_exhaust_bounded_schedule_to_failed():
    """A bounded backoff schedule + every reopen failing → FAILED state.
    Production passes None for an infinite schedule; tests pin one to
    observe exhaustion."""
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        connect_factory=factory,
        backoff_schedule=(0.0, 0.0),
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        first = factory.conns[0]
        # Two reopen attempts, both fail.
        factory.next_exceptions = [
            RuntimeError("fail 1"),
            RuntimeError("fail 2"),
        ]
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()
        first.feed_error(_Drop())
        await _wait_until(
            lambda: conn._state is ConnectionState.FAILED, timeout=3.0,
        )
        with pytest.raises(RuntimeError, match="FAILED"):
            await conn.acquire_turn()
        assert conn.is_paused()
    finally:
        await conn.stop()


async def test_non_transient_initial_connect_error_propagates():
    """An auth failure on the first connect must NOT silently retry —
    the daemon should surface FAILED so the user can fix the key."""
    factory = _FakeConnectFactory()

    class _AuthError(Exception):
        status_code = 401

    factory.next_exceptions = [_AuthError("bad key")]
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        connect_factory=factory,
        backoff_schedule=(0.0,),
    )
    registry = ToolRegistry()
    with pytest.raises(_AuthError):
        await conn.start(registry, "")
    assert conn._state is ConnectionState.FAILED


async def test_acquire_turn_blocks_then_raises_when_failed():
    """acquire_turn() while in FAILED raises immediately rather than
    deadlocking on the connected_event."""
    conn, factory = _make_conn(backoff_schedule=(0.0,))
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        first = factory.conns[0]
        factory.next_exceptions = [RuntimeError("dead")]
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "x"
            rcvd = _Rcvd()
        first.feed_error(_Drop())
        await _wait_until(
            lambda: conn._state is ConnectionState.FAILED, timeout=3.0,
        )
        with pytest.raises(RuntimeError):
            await conn.acquire_turn()
    finally:
        await conn.stop()


async def test_stop_is_idempotent():
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    await conn.stop()
    await conn.stop()
    assert conn._state is ConnectionState.CLOSED


async def test_connection_lost_marks_active_turn_lost():
    """If the WebSocket drops mid-turn, the active turn must flip
    turn_lost() to True so the daemon stops waiting and audio_out()'s
    consumer wakes via the sentinel-None."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        first = factory.conns[0]
        turn = await conn.acquire_turn()

        async def consume():
            async for _ in turn.audio_out():
                pass

        consumer = asyncio.create_task(consume())
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "x"
            rcvd = _Rcvd()
        first.feed_error(_Drop())
        await asyncio.wait_for(consumer, timeout=3.0)
        assert turn.turn_lost() is True
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Grok subclass.
# ---------------------------------------------------------------------------


async def test_grok_uses_grok_provider_filter_and_default_model():
    """The Grok subclass must report PROVIDER_NAME='grok' so the tool
    registry filters apply correctly, default to ``grok-voice-think-fast-1.0``,
    and target xAI's WebSocket endpoint."""
    factory = _FakeConnectFactory()
    conn = GrokRealtimeConnection(
        api_key="xai-fake",
        connect_factory=factory,
        backoff_schedule=(0.0,),
    )
    assert conn.PROVIDER_NAME == "grok"
    assert conn._model == "grok-voice-think-fast-1.0"
    assert conn._voice == "eve"
    assert conn._base_url == GROK_WEBSOCKET_BASE_URL

    registry = ToolRegistry()

    @tool(providers={"grok"})
    def grok_only() -> dict:
        """."""
        return {}

    @tool(providers={"openai"})
    def openai_only() -> dict:
        """."""
        return {}

    @tool()
    def universal() -> dict:
        """."""
        return {}

    registry.register(grok_only)
    registry.register(openai_only)
    registry.register(universal)

    await conn.start(registry, "")
    try:
        upd = _find_event(factory.conns[0].sent, "session.update")
        names = {t["name"] for t in upd["session"]["tools"]}
        assert names == {"grok_only", "universal"}
        # Grok models do not accept reasoning.effort.
        assert "reasoning" not in upd["session"]
    finally:
        await conn.stop()


async def test_grok_text_delta_normalised_to_openai_event_name():
    """Per xAI's docs, Grok emits ``response.text.delta`` instead of
    OpenAI's GA ``response.output_text.delta``. The Grok adapter
    rewrites the event name before dispatch, so a future code path that
    consumes text deltas would see the OpenAI-canonical name on both
    providers.

    Today the daemon only consumes audio deltas, so this test is forward-
    compat — but the xAI claim is a documented behaviour we want pinned
    to a regression test."""
    captured: list[str] = []

    factory = _FakeConnectFactory()
    conn = GrokRealtimeConnection(
        api_key="xai-fake",
        connect_factory=factory,
        backoff_schedule=(0.0,),
    )

    # Tap into the parent dispatcher to observe the normalised etype.
    original = OpenAIRealtimeConnection._dispatch_event

    async def spy(self, etype, event):
        captured.append(etype)
        return await original(self, etype, event)

    OpenAIRealtimeConnection._dispatch_event = spy
    try:
        registry = ToolRegistry()
        await conn.start(registry, "")
        try:
            sess = factory.conns[0]
            sess.feed({"type": "response.text.delta", "delta": "hi"})
            sess.feed({"type": "response.text.done", "text": "hi"})
            await _wait_until(
                lambda: "response.output_text.delta" in captured,
                timeout=2.0,
            )
            assert "response.output_text.delta" in captured
            assert "response.output_text.done" in captured
        finally:
            await conn.stop()
    finally:
        OpenAIRealtimeConnection._dispatch_event = original
