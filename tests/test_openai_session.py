# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
import logging

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
    model: str = "gpt-realtime-2",
    voice: str = "marin",
    reasoning_effort: str = "low",
    noise_reduction: str = "off",
) -> tuple[OpenAIRealtimeConnection, _FakeConnectFactory]:
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        model=model,
        voice=voice,
        reasoning_effort=reasoning_effort,
        noise_reduction=noise_reduction,
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


def test_invalid_noise_reduction_rejected_at_construction():
    with pytest.raises(RuntimeError, match="OpenAI noise_reduction"):
        _make_conn(noise_reduction="potato")


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
        # `truncation: "auto"` lets the server prune old conversation
        # items as context fills, preserving the prompt-cache prefix.
        # Replaces our previous strategy of tearing down the session
        # every ~5 minutes idle (removed 2026-05-09). Required for
        # long-lived sessions on the smart-speaker workload.
        assert sess_payload["truncation"] == "auto", (
            "truncation:auto must be set so the server manages "
            "context drift natively — without this, sessions either "
            "bloat unboundedly or we have to reconnect (which "
            "re-bills the system prompt at the uncached rate)"
        )
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


async def test_send_text_context_adds_text_item_without_response_create():
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        baseline = len(sess.sent)

        await turn.send_text_context("Answer yes or no about research job abc.")

        new = sess.sent[baseline:]
        assert new == [{
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "Answer yes or no about research job abc.",
                }],
            },
        }]
        await turn.release()
    finally:
        await conn.stop()


async def test_release_without_commit_does_not_cancel_response():
    """No-speech aborts may have streamed audio but never committed input.

    Releasing that shape must not send response.cancel: there is no active
    response yet, and the server reports a noisy response_cancel_not_active
    error.
    """
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await turn.send_audio(b"\x00\x00" * 1280)

        baseline = len(sess.sent)
        await turn.release()
        assert "response.cancel" not in {
            event["type"] for event in sess.sent[baseline:]
        }
    finally:
        await conn.stop()


async def test_release_after_commit_cancels_unfinished_response():
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await turn.send_audio(b"\x00\x00" * 1280)
        await turn.end_input()

        baseline = len(sess.sent)
        await turn.release()
        assert "response.cancel" in {
            event["type"] for event in sess.sent[baseline:]
        }
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Barge-in capability seam (OpenAI reference pack — PR-4).
#
# cancel_response -> response.cancel (guarded to an in-progress response);
# truncate_assistant_audio -> conversation.item.truncate with the playout
# ledger's played-ms as audio_end_ms, guarded to never truncate on a 0/None
# played-ms (which would over-count vs. what was heard and error server-side).
# ---------------------------------------------------------------------------


async def test_truncate_assistant_audio_sends_item_truncate_with_played_ms():
    """The reference-pack truncate sends conversation.item.truncate with
    content_index 0 and audio_end_ms == the played-ms it was handed (the
    flush ack's max_audio_played_ms). audio_end_ms is the heard boundary;
    OpenAI deletes the unspoken transcript past it so context stays aligned."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()

        baseline = len(sess.sent)
        await turn.truncate_assistant_audio("item_xyz", 4321)

        trunc = _find_event(sess.sent[baseline:], "conversation.item.truncate")
        assert trunc is not None, (
            "truncate_assistant_audio did not send conversation.item.truncate"
        )
        assert trunc["item_id"] == "item_xyz"
        assert trunc["content_index"] == 0
        assert trunc["audio_end_ms"] == 4321
    finally:
        await conn.stop()


async def test_truncate_falls_back_to_last_assistant_item_id():
    """When the daemon spine passes provider_item_id=None (it carries no
    provider id), the adapter targets its own `_last_assistant_item_id`,
    captured from response.output_item.added. This is the production path —
    `_flush_for_interrupt` always passes None."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        # The connection's receive loop sets _last_assistant_item_id from
        # this event in production.
        sess.feed({
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "item_from_server"},
        })
        await _wait_until(
            lambda: turn._last_assistant_item_id == "item_from_server",
        )

        baseline = len(sess.sent)
        await turn.truncate_assistant_audio(None, 1500)

        trunc = _find_event(sess.sent[baseline:], "conversation.item.truncate")
        assert trunc is not None
        assert trunc["item_id"] == "item_from_server"
        assert trunc["audio_end_ms"] == 1500
    finally:
        await conn.stop()


async def test_truncate_fires_even_after_server_turn_complete():
    """The most common OpenAI barge-in window: burst delivery finishes the
    response server-side (server_turn_complete True) while audio is still
    draining locally, then the user talks over the tail. Truncate MUST still
    align history to the heard boundary — unlike cancel, it does NOT gate on
    completion (there is nothing to *cancel*, but there is still unspoken
    transcript to *trim*)."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        turn._last_assistant_item_id = "item_drain"
        turn._server_turn_complete = True  # response already done server-side

        baseline = len(sess.sent)
        await turn.truncate_assistant_audio(None, 3200)

        trunc = _find_event(sess.sent[baseline:], "conversation.item.truncate")
        assert trunc is not None, (
            "truncate must fire during the drain tail even after the server "
            "completed the response — this is the primary barge-in window"
        )
        assert trunc["item_id"] == "item_drain"
        assert trunc["audio_end_ms"] == 3200
    finally:
        await conn.stop()


async def test_truncate_noop_and_warns_on_zero_played_ms(caplog):
    """CRITICAL GUARD: a 0 played-ms (the production fan-in ack can return
    max_audio_played_ms=0) means the ledger saw no rendered audio. Truncating
    on bytes-received instead would push audio_end_ms past the heard boundary
    and the server errors. So 0 is a no-op + WARN, never a guess."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        turn._last_assistant_item_id = "item_present"  # a real target exists

        baseline = len(sess.sent)
        with caplog.at_level(
            logging.WARNING, logger="jasper.voice.openai_session",
        ):
            await turn.truncate_assistant_audio(None, 0)

        assert _find_event(
            sess.sent[baseline:], "conversation.item.truncate",
        ) is None, "must NOT truncate when the ledger reports 0 played-ms"
        skipped = [
            r for r in caplog.records
            if "barge.truncate_skipped" in r.getMessage()
            and "zero_played_ms" in r.getMessage()
        ]
        assert len(skipped) == 1, (
            "a 0-played-ms truncate must WARN once, never silently no-op"
        )
    finally:
        await conn.stop()


async def test_truncate_clamps_to_item_received_ms(caplog):
    """C1: the playout ledger reports a turn-WIDE max played-ms. On a
    multi-segment (tool-using) turn an earlier item can out-play the
    in-flight one, so the max would exceed THIS item's duration — the
    out-of-range case OpenAI rejects. truncate clamps audio_end_ms to what
    this item actually received."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        turn._last_assistant_item_id = "item_short"
        # This item received only 1000 ms of audio...
        turn._received_ms_by_item["item_short"] = 1000.0
        baseline = len(sess.sent)
        with caplog.at_level(
            logging.DEBUG, logger="jasper.voice.openai_session",
        ):
            # ...but the turn-wide ledger max is 5000 ms (an earlier item
            # out-played this one).
            await turn.truncate_assistant_audio(None, 5000)
        ev = _find_event(sess.sent[baseline:], "conversation.item.truncate")
        assert ev is not None, "truncate must still be sent (clamped, not skipped)"
        assert ev["audio_end_ms"] == 1000, (
            "audio_end_ms must be clamped to the item's received duration, "
            f"not the turn-wide max; got {ev['audio_end_ms']}"
        )
        assert any(
            "barge.truncate_clamped" in r.getMessage() for r in caplog.records
        ), "a clamp must be observable"
    finally:
        await conn.stop()


async def test_truncate_noop_when_no_item_id():
    """No assistant item observed yet (barge-in raced
    response.output_item.added) and no id passed in → nothing to align, so
    no conversation.item.truncate is sent."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        assert turn._last_assistant_item_id is None  # nothing captured yet

        baseline = len(sess.sent)
        await turn.truncate_assistant_audio(None, 2000)

        assert _find_event(
            sess.sent[baseline:], "conversation.item.truncate",
        ) is None
    finally:
        await conn.stop()


async def test_cancel_response_noop_when_no_active_response():
    """response.cancel errors (response_cancel_not_active) when no response
    is generating. An uncommitted turn has no active response, so
    cancel_response must not send anything."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await turn.send_audio(b"\x00\x00" * 1280)  # streamed, NOT committed

        baseline = len(sess.sent)
        await turn.cancel_response("barge_in")
        assert "response.cancel" not in {
            event["type"] for event in sess.sent[baseline:]
        }, "must NOT cancel when there is no active response"
    finally:
        await conn.stop()


async def test_cancel_response_sends_when_response_in_progress():
    """After end_input commits the buffer and asks for a response, a
    response IS in progress (server hasn't completed it), so cancel_response
    sends response.cancel."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await turn.send_audio(b"\x00\x00" * 1280)
        await turn.end_input()  # commit + response.create → response active

        baseline = len(sess.sent)
        await turn.cancel_response("barge_in")
        assert "response.cancel" in {
            event["type"] for event in sess.sent[baseline:]
        }
    finally:
        await conn.stop()


async def test_cancel_response_noop_after_server_turn_complete():
    """Once the server has completed the response, there is no longer an
    active response to cancel — cancel_response is a no-op (idempotent
    against a late/duplicate barge-in)."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await turn.send_audio(b"\x00\x00" * 1280)
        await turn.end_input()
        # Server says the response finished (set by response.done in
        # production; pinned directly here to isolate the guard).
        turn._server_turn_complete = True

        baseline = len(sess.sent)
        await turn.cancel_response("barge_in")
        assert "response.cancel" not in {
            event["type"] for event in sess.sent[baseline:]
        }
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


async def test_output_audio_transcript_logged_at_debug_turn_release(caplog):
    """Assistant transcript content stays out of persistent logs.

    OpenAI's deployed Realtime stream emits
    ``response.output_audio_transcript.delta`` for assistant speech. The
    adapter logs only metadata, because the flight recorder buffers
    DEBUG records and dumps them to journald around failures."""
    caplog.set_level(logging.DEBUG, logger="jasper.voice.openai_session")
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        sess.feed({
            "type": "response.output_audio_transcript.delta",
            "delta": "Transport ",
        })
        sess.feed({
            "type": "response.output_audio_transcript.delta",
            "delta": "error.",
        })
        sess.feed({
            "type": "response.done",
            "response": {"usage": {"input_tokens": 1, "output_tokens": 2}},
        })

        await _wait_until(lambda: turn.server_turn_complete(), timeout=2.0)
        assert turn.assistant_transcript() == "Transport error."
        await turn.release()
        transcript_records = [
            r for r in caplog.records
            if "event=openai.assistant_transcript" in r.getMessage()
        ]
        assert len(transcript_records) == 1
        assert transcript_records[0].levelno == logging.DEBUG
        message = transcript_records[0].getMessage()
        assert "chars=16" in message
        assert "Transport error." not in message
    finally:
        await conn.stop()


async def test_user_audio_transcript_logged_at_debug_not_info(caplog):
    caplog.set_level(logging.DEBUG, logger="jasper.voice.openai_session")
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        sess.feed({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "turn on the kitchen lights",
        })
        await _wait_until(
            lambda: "event=openai.user_transcript" in caplog.text,
            timeout=2.0,
        )
        transcript_records = [
            r for r in caplog.records
            if "event=openai.user_transcript" in r.getMessage()
        ]
        assert len(transcript_records) == 1
        assert transcript_records[0].levelno == logging.DEBUG
        message = transcript_records[0].getMessage()
        assert "chars=26" in message
        assert "turn on the kitchen lights" not in message
    finally:
        await conn.stop()


async def test_user_audio_transcript_is_exposed_on_active_turn(caplog):
    caplog.set_level(logging.DEBUG, logger="jasper.voice.openai_session")
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        sess.feed({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "turn on the kitchen lights",
        })
        await _wait_until(
            lambda: turn.user_transcript() == "turn on the kitchen lights",
            timeout=2.0,
        )
        await turn.release()
    finally:
        await conn.stop()


async def test_audio_chunks_include_openai_provider_item_id():
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        sess.feed({
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_abc123"},
        })
        sess.feed({
            "type": "response.output_audio.delta",
            "delta": _b64(b"audio_chunk_1"),
            "response_id": "resp_1",
        })

        async def consume():
            async for chunk in turn.audio_out_chunks():
                return chunk
            raise AssertionError("expected one audio chunk")

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await turn.end_input()
        await turn.release()
        chunk = await asyncio.wait_for(task, timeout=1.0)
        assert chunk.pcm == b"audio_chunk_1"
        assert chunk.provider_item_id == "msg_abc123"
    finally:
        await conn.stop()


async def test_response_done_pushes_sentinel_so_consumer_drains_then_exits():
    """``response.done`` is the server's "no more audio coming" signal.
    The adapter pushes a sentinel onto the audio queue so the playback
    consumer can drain every queued chunk and then exit naturally,
    instead of relying on the idle watchdog's dequeue-timestamp tail
    timer (which can fire mid-playback when a single tts.write blocks
    longer than the tail timeout).

    Three properties this test pins:
      * The sentinel arrives AFTER all real audio chunks, not before.
      * ``audio_chunks_pending()`` reports the sentinel as pending work
        so the watchdog defers while the consumer drains.
      * The consumer's ``audio_out()`` generator returns cleanly when
        the sentinel is dequeued — no infinite hang."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        for payload in (b"chunk_a", b"chunk_b", b"chunk_c"):
            sess.feed({
                "type": "response.output_audio.delta",
                "delta": _b64(payload),
                "response_id": "resp_1",
            })
        sess.feed({
            "type": "response.done",
            "response": {"usage": {"input_tokens": 1, "output_tokens": 2}},
        })
        await asyncio.sleep(0.05)

        assert turn.server_turn_complete() is True
        assert turn.audio_chunks_pending() == 4

        chunks: list[bytes] = []
        async for chunk in turn.audio_out():
            chunks.append(chunk)
        assert chunks == [b"chunk_a", b"chunk_b", b"chunk_c"]
        assert turn.audio_chunks_pending() == 0
    finally:
        await conn.stop()


async def test_last_chunk_played_at_tracks_consumer_dequeues():
    """The idle watchdog uses ``last_chunk_played_at`` to know when
    the consumer has finished draining the audio queue. The whole
    point of having TWO timestamps (chunk_at vs chunk_played_at) is
    that they diverge: OpenAI Realtime delivers all of a response's
    audio chunks back-to-back over the WebSocket within milliseconds,
    while the consumer plays them at real-time rate via ALSA over
    seconds. Using ``last_chunk_at`` (network arrival) for the tail
    wait ends the turn while the queue still has 5+ seconds of audio
    waiting to play — that was the live-deploy bug the user described
    as "she's still cutting out".

    Pin the invariant: ``last_chunk_played_at`` is 0 until the
    consumer dequeues a chunk, and it advances each time it does."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        # Initially zero — no chunks dequeued yet.
        assert turn.last_chunk_played_at() == 0.0

        # Simulate fast back-to-back network delivery of 5 chunks.
        for i in range(5):
            sess.feed({
                "type": "response.output_audio.delta",
                "delta": _b64(b"\x00\x01" * 200),
                "response_id": "resp_1",
            })
        # Let the dispatcher route those events into audio_q.
        await asyncio.sleep(0.05)

        # Network arrival anchor advanced (chunks have arrived);
        # played-at anchor still zero because the consumer hasn't
        # run. This is the divergence the bug exploited.
        assert turn.last_chunk_at() > 0
        assert turn.last_chunk_played_at() == 0.0

        # Consumer dequeues one. played-at becomes non-zero.
        gen = turn.audio_out()
        await gen.__anext__()
        played_after_one = turn.last_chunk_played_at()
        assert played_after_one > 0

        # Sleep slightly, dequeue another. played-at advances.
        await asyncio.sleep(0.02)
        await gen.__anext__()
        played_after_two = turn.last_chunk_played_at()
        assert played_after_two > played_after_one, (
            "last_chunk_played_at should advance each time the consumer "
            "dequeues a chunk; this is what makes the watchdog wait for "
            "the consumer to drain the queue rather than ending the "
            "turn 1.5 s after the network finished delivering."
        )

        # Cleanup: drain the rest so the consumer task can finish.
        await gen.aclose()
        await turn.release()
    finally:
        await conn.stop()


async def test_function_call_round_trip():
    """Tool dispatch is triggered by ``response.done`` (with a
    ``function_call`` item in ``response.output[]``), NOT by
    ``response.function_call_arguments.done``. Dispatching on the
    latter would race against response 1 still being in-flight on the
    server — sending ``response.create`` mid-response either errors
    with "active response in progress" or gets silently dropped, and
    the audio answer never arrives.

    On response.done with a function_call:
      1. Parse the JSON args.
      2. Invoke the registered tool.
      3. Send ``conversation.item.create`` with type
         ``function_call_output`` and the JSON-stringified result.
      4. Send ONE ``response.create`` after dispatch (regardless of
         how many tools were called this round).
    """
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

        # Server fires response.done containing a function_call.
        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 100, "output_tokens": 8},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_abc",
                        "name": "set_volume",
                        "arguments": json.dumps({"percent": 30}),
                    },
                ],
            },
        })
        # Wait for the tool dispatch to complete and the reply events
        # to land in sess.sent.
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


async def test_response_create_fired_only_once_per_tool_round_with_multiple_calls():
    """If the model emits multiple function_call items in one response
    (parallel_tool_calls), the dispatcher must send the
    function_call_output for EACH and then send EXACTLY ONE
    ``response.create`` to start the audio response. Sending one
    response.create per tool would produce overlapping responses,
    which OpenAI rejects with `Conversation already has an active
    response in progress`."""
    conn, factory = _make_conn()
    registry = ToolRegistry()

    @tool()
    def get_weather(location: str = "") -> dict:
        """."""
        return {"location": "Brooklyn", "temperature": 62}

    @tool()
    def get_volume() -> dict:
        """."""
        return {"percent": 50}

    registry.register(get_weather)
    registry.register(get_volume)

    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        await conn.acquire_turn()

        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 100, "output_tokens": 12},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_w",
                        "name": "get_weather",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_v",
                        "name": "get_volume",
                        "arguments": "{}",
                    },
                ],
            },
        })
        # Wait for both function_call_output items to land.
        await _wait_until(
            lambda: sum(
                1 for e in sess.sent
                if e.get("type") == "conversation.item.create"
                and e.get("item", {}).get("type") == "function_call_output"
            ) >= 2,
            timeout=2.0,
        )
        # Both tool outputs sent.
        outputs = [
            e for e in sess.sent
            if e.get("type") == "conversation.item.create"
            and e.get("item", {}).get("type") == "function_call_output"
        ]
        assert {o["item"]["call_id"] for o in outputs} == {"call_w", "call_v"}
        # Exactly ONE response.create after the tool round.
        creates = [e for e in sess.sent if e.get("type") == "response.create"]
        assert len(creates) == 1, (
            f"expected exactly one response.create after the tool "
            f"round, got {len(creates)}"
        )
    finally:
        await conn.stop()


async def test_function_call_after_turn_aborted_sends_cancelled_output():
    """If the turn is released (e.g. no-speech-detected abort, hard cap)
    BEFORE the model's response.done arrives, and that response carries
    function_calls, the dispatcher must still send synthetic
    ``function_call_output`` items so server-side conversation history
    has matching outputs for each call. Without this, the next turn
    sees a dangling function_call and the model responds with confused
    fallbacks like "It's still starting up" — even when the user is
    asking something brand new.

    Repro of the live bug: voice 'play X' fired wake → 3-sec context-
    reset reconnect made the turn miss the user's speech → VAD aborted
    → response.done arrived 1s later carrying the model's spotify_play
    function_call → dispatcher early-returned because turn was None →
    the call sat unanswered in conversation history → next 'play X'
    attempt got 'It's still starting up' as the model's response.

    Two invariants this test enforces:
      1. Synthetic ``function_call_output`` is sent for every dangling
         call_id, with an "error" payload signalling cancellation.
      2. NO ``response.create`` is fired afterwards — we don't want the
         model to generate an audio answer that has no turn to play
         through.
    """
    conn, factory = _make_conn()
    registry = ToolRegistry()
    invoked = []

    @tool()
    def spotify_play(query: str = "") -> dict:
        """."""
        invoked.append(query)
        return {"ok": True}

    registry.register(spotify_play)

    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        # Simulate the daemon aborting the turn (no-speech detected,
        # connection lost, etc.) BEFORE response.done arrives.
        await turn.release()
        # Keep this assertion scoped to the late dangling call below,
        # independent of any teardown bookkeeping release() performs.
        sess.sent.clear()

        # Server's response.done lands AFTER the turn is gone.
        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 100, "output_tokens": 8},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_dangling",
                        "name": "spotify_play",
                        "arguments": json.dumps({"query": "Release Radar"}),
                    },
                ],
            },
        })

        # The synthetic cancelled output must land.
        await _wait_until(
            lambda: any(
                e.get("type") == "conversation.item.create"
                and e.get("item", {}).get("type") == "function_call_output"
                and e.get("item", {}).get("call_id") == "call_dangling"
                for e in sess.sent
            ),
            timeout=2.0,
        )

        # Tool was NOT actually invoked — we don't want the side effect
        # (e.g. starting playback the user never confirmed).
        assert invoked == []

        # The output payload signals cancellation, so the model on the
        # next turn doesn't "resume" the dangling call.
        outputs = [
            e for e in sess.sent
            if e.get("type") == "conversation.item.create"
            and e.get("item", {}).get("type") == "function_call_output"
        ]
        assert len(outputs) == 1
        body = json.loads(outputs[0]["item"]["output"])
        assert "error" in body

        # No response.create — we deliberately don't want the model to
        # generate an audio answer that nothing's listening for.
        creates = [e for e in sess.sent if e.get("type") == "response.create"]
        assert creates == [], (
            f"expected zero response.create after sending cancelled "
            f"function_call_output, got {len(creates)}: {creates}"
        )
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

        # ROUND 1: server emits response.done containing the
        # function_call. The dispatcher should:
        #   - dispatch the tool
        #   - send function_call_output
        #   - send ONE response.create
        #   - NOT flip server_turn_complete (audio answer still in
        #     flight as response 2)
        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 100, "output_tokens": 8},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "get_weather",
                        "arguments": "{}",
                    },
                ],
            },
        })
        # Wait for the function_call_output to land.
        await _wait_until(
            lambda: any(
                e.get("type") == "conversation.item.create"
                and e.get("item", {}).get("type") == "function_call_output"
                for e in sess.sent
            ),
            timeout=2.0,
        )
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
        # output[] for the audio response contains a `message` item
        # (not `function_call`), so the dispatcher recognises this as
        # the final response and flips server_turn_complete.
        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_2",
                "usage": {"input_tokens": 50, "output_tokens": 200},
                "output": [{"type": "message"}],
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


async def test_tool_round_advances_idle_anchor_so_watchdog_does_not_fire():
    """The idle watchdog measures from ``last_activity_at``. While a
    tool round is in flight (function_call response.done received,
    function_call_output sent, waiting for response 2), no audio has
    arrived yet, so the pre-response branch of the watchdog is what
    governs the turn — and at small ``JASPER_IDLE_TIMEOUT_SEC`` (e.g.
    10 s) it WILL fire mid-dispatch unless the tool round itself
    counts as activity.

    Production symptom (2026-05-21, jasper-voice journal): user asked
    a weather question, tool dispatched in 916 ms, then ``idle timeout
    (pre-response phase, 10.0s); no chunks, ending turn`` fired ~0.6 s
    after the result was sent — the daemon ended the turn one second
    before response 2's audio arrived. The user heard nothing back;
    the orphan-response warning logged 48 dropped audio tokens.

    Fix: when the function_calls branch of ``_handle_response_done``
    runs, advance the turn's ``_last_activity_at`` so the watchdog's
    pre-response timer restarts from the tool dispatch, not from turn
    start."""
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
        anchor_before = turn.last_activity_at()

        # Park briefly so the loop clock advances measurably.
        await asyncio.sleep(0.05)

        # Server sends the function_call response.done.
        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 100, "output_tokens": 8},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "get_weather",
                        "arguments": "{}",
                    },
                ],
            },
        })
        # Wait for the dispatch + response.create to land.
        await _wait_until(
            lambda: any(e.get("type") == "response.create" for e in sess.sent),
            timeout=2.0,
        )
        await asyncio.sleep(0.05)

        anchor_after = turn.last_activity_at()
        assert anchor_after > anchor_before, (
            "tool round must advance last_activity_at so the pre-response "
            "idle watchdog doesn't fire while waiting for response 2"
        )

        await turn.release()
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
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_x",
                        "name": "no_such_tool",
                        "arguments": "{}",
                    },
                ],
            },
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


async def test_clean_iteration_exit_triggers_reconnect():
    """OpenAI Realtime closes the WebSocket with 1001 "going away" when
    the session hits its 60-minute hard cap. ``websockets`` treats
    normal closes (1000/1001) as the end of the iterator and exits
    ``async for`` WITHOUT raising. The receive loop must wake the
    supervisor on this path too — otherwise the daemon sits on a
    dead session and every subsequent wake silently fails in
    ``send_audio``. Real-world symptom: four consecutive wakes within
    90 s, each producing ``send_audio failed (ConnectionClosedOK:
    received 1001 (going away) Your session hit the maximum duration
    of 60 minutes.); turn lost``, with no reconnect."""
    conn, factory = _make_conn(backoff_schedule=(0.0, 0.05))
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        first = factory.conns[0]
        # Simulate the OpenAI 60-min-cap close: server sends 1001 and
        # the iterator ends cleanly (no exception). _FakeConn's
        # feed_iter_stop() raises StopAsyncIteration on the next
        # __anext__, exactly mirroring websockets' clean-close
        # iteration end.
        first.feed_iter_stop()

        await _wait_until(lambda: len(factory.conns) >= 2, timeout=3.0)
        await _wait_until(
            lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0,
        )
        # Connection is usable again on the fresh session.
        turn = await conn.acquire_turn()
        await turn.release()
    finally:
        await conn.stop()


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
# Initial-connect retry budget — startup-resilience under network races.
#
# Covers the 2026-05-23 bug: the daemon raced WiFi recovery from an
# unclean shutdown at boot, the OpenAI WebSocket couldn't resolve DNS
# during the WiFi-down window, the (then) 5-retry cap exhausted in
# ~15 s of wall-time, and the daemon exited permanently. Fix replaced
# the retry count with a time budget so transient network conditions
# can recover before we give up.
# ---------------------------------------------------------------------------


class _FakeClock:
    """Test-only monotonic clock the budget loop can fast-forward.

    The retry loop reads ``self._monotonic()`` to compute elapsed time
    and ``self._sleep(delay)`` to pause between attempts. Wiring a
    fake of each lets the test simulate a 10-minute budget exhausting
    in ~0 wall-time."""

    def __init__(self) -> None:
        self.now = 1_000_000.0  # arbitrary epoch
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        # Drive the budget forward by the requested delay, exactly as
        # asyncio.sleep would on a real clock.
        self.now += float(delay)
        # Yield once so the receive_loop / supervisor tasks make
        # progress between attempts (the real asyncio.sleep gives them
        # the same opportunity).
        await asyncio.sleep(0)


def _make_conn_with_clock(
    *,
    budget_sec: float,
    fail_count: int = 0,
    fail_exc: Exception | None = None,
) -> tuple[OpenAIRealtimeConnection, _FakeConnectFactory, _FakeClock]:
    """Build a connection wired to a fake clock + sleep + connect.

    ``fail_count`` controls how many transient failures the connect
    factory queues before the next call succeeds. ``fail_exc``
    overrides the default ``OSError`` (mirrors a DNS failure shape).
    """
    if fail_exc is None:
        # Mirrors the production stack trace seen on 2026-05-23:
        # OSError [Errno -3] Temporary failure in name resolution.
        # OSError has no `status_code`, so `_is_transient` falls
        # through to the no-status path and returns True.
        fail_exc = OSError(-3, "Temporary failure in name resolution")
    factory = _FakeConnectFactory()
    factory.next_exceptions = [fail_exc for _ in range(fail_count)]
    clock = _FakeClock()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        backoff_schedule=(0.0, 0.0),
        initial_connect_budget_sec=budget_sec,
        connect_factory=factory,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    return conn, factory, clock


async def test_initial_connect_succeeds_first_try():
    """Happy path: first connect attempt succeeds with no backoff
    sleeps and no retries logged."""
    conn, factory, clock = _make_conn_with_clock(budget_sec=600.0)
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        # Exactly one connect was issued.
        assert len(factory.conns) == 1
        # No sleeps fired — the loop took the success branch immediately.
        assert clock.sleeps == []
        assert conn._state is ConnectionState.CONNECTED
    finally:
        await conn.stop()


async def test_initial_connect_retries_with_exponential_backoff():
    """N transient failures then success: the loop attempts N+1 times
    and the sleep delays grow exponentially (the shared
    ``reconnect_backoff_delay`` schedule). Pins the behavior the
    2026-05-23 fix introduces — the old code did at most 5 attempts
    with a fixed schedule capping at ~15 s of total wall-time."""
    conn, factory, clock = _make_conn_with_clock(
        budget_sec=600.0, fail_count=3,
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        # 3 failures + 1 success = 4 connect attempts.
        # The factory's `next_exceptions` is drained by the failed
        # calls; the 4th call lands on the empty list and creates a
        # _FakeConn.
        assert len(factory.conns) == 1, (
            "Only the successful call records a conn in factory.conns; "
            "the failed calls raise before reaching that path."
        )
        # 3 sleeps fired (one between each pair of attempts).
        assert len(clock.sleeps) == 3
        # Exponential growth: each sleep is at least as large as the
        # previous one's BASE (modulo ±25% jitter from the shared
        # helper). Reconnect base schedule is 1, 2, 4, … s; with ±25%
        # jitter that's 0.75-1.25, 1.5-2.5, 3.0-5.0. Assert each delay
        # falls inside its expected range.
        delays = clock.sleeps
        assert 0.75 <= delays[0] <= 1.25, f"attempt 1 delay: {delays[0]}"
        assert 1.5 <= delays[1] <= 2.5, f"attempt 2 delay: {delays[1]}"
        assert 3.0 <= delays[2] <= 5.0, f"attempt 3 delay: {delays[2]}"
        assert conn._state is ConnectionState.CONNECTED
    finally:
        await conn.stop()


async def test_initial_connect_exhausts_budget_and_raises():
    """Every attempt fails transiently and the wall-clock budget
    expires: ``_open_session_with_retry`` raises ``RuntimeError`` so
    the daemon exits non-zero and systemd's outer loop kicks in. The
    budget covers wall-time, NOT a fixed retry count — verified by
    setting a tiny budget so the test runs in O(budget_sec) of fake
    clock-ticks rather than 10 real minutes."""
    # Small budget: the second sleep alone will overshoot the deadline,
    # so the loop exhausts after a couple of attempts.
    conn, factory, clock = _make_conn_with_clock(
        budget_sec=2.0,
        # Effectively infinite: every connect attempt fails.
        fail_count=100,
    )
    registry = ToolRegistry()
    with pytest.raises(RuntimeError, match="budget of .* exhausted"):
        await conn.start(registry, "")
    # State machine landed in FAILED (the _do_initial_connect except
    # branch sets this before re-raising).
    assert conn._state is ConnectionState.FAILED
    # At least 1 attempt was made.
    assert len(factory.next_exceptions) < 100
    # No successful connect.
    assert len(factory.conns) == 0


async def test_initial_connect_non_transient_error_raises_immediately():
    """Auth errors (and other non-transient failures per
    ``_is_transient``) must NOT consume the budget — they propagate
    on the first attempt, no sleep, no retry. Preserves the original
    behaviour from before the budget refactor (was covered by
    ``test_non_transient_initial_connect_error_propagates`` against
    the old schedule-based path)."""
    class _AuthError(Exception):
        status_code = 401

    conn, factory, clock = _make_conn_with_clock(
        budget_sec=600.0,
        fail_count=1,
        fail_exc=_AuthError("bad key"),
    )
    registry = ToolRegistry()
    with pytest.raises(_AuthError):
        await conn.start(registry, "")
    # Critical: NO sleeps fired. A non-transient error must not
    # cost the user a backoff wait.
    assert clock.sleeps == []
    assert conn._state is ConnectionState.FAILED


async def test_initial_connect_zero_budget_is_single_attempt():
    """budget=0 means "single attempt, no retries" — a transient
    failure on the first attempt exhausts immediately. Useful for
    operators who want fast-feedback boot semantics at the cost of
    network-race resilience."""
    conn, factory, clock = _make_conn_with_clock(
        budget_sec=0.0, fail_count=1,
    )
    registry = ToolRegistry()
    with pytest.raises(RuntimeError, match="budget"):
        await conn.start(registry, "")
    # No sleeps — the deadline check happens before any backoff sleep.
    assert clock.sleeps == []


async def test_initial_connect_logs_structured_events(caplog):
    """Per the AGENTS.md PSK rule, the boot-time funnel emits
    ``event=openai.initial_connect.{...}`` lines so journalctl can
    grep the path alongside the rest of the daemon's structured logs.
    Pin three concrete patterns: ``.attempt``, ``.backoff``, and
    ``.exhausted`` (the failure-path triad)."""
    import logging
    caplog.set_level(logging.WARNING, logger="jasper.voice.openai_session")
    conn, factory, clock = _make_conn_with_clock(
        budget_sec=1.5, fail_count=100,
    )
    registry = ToolRegistry()
    with pytest.raises(RuntimeError):
        await conn.start(registry, "")
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "event=openai.initial_connect.attempt" in m for m in messages
    ), f"no .attempt event found in {messages}"
    assert any(
        "event=openai.initial_connect.backoff" in m for m in messages
    ), f"no .backoff event found in {messages}"
    assert any(
        "event=openai.initial_connect.exhausted" in m for m in messages
    ), f"no .exhausted event found in {messages}"


async def test_initial_connect_success_after_retries_logs_success_event(caplog):
    """Recovery path: after one or more transient failures, the next
    successful attempt emits ``event=openai.initial_connect.success``
    with ``elapsed_sec`` so journalctl can see how long the network
    race took to resolve."""
    import logging
    caplog.set_level(logging.INFO, logger="jasper.voice.openai_session")
    conn, factory, clock = _make_conn_with_clock(
        budget_sec=600.0, fail_count=2,
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "event=openai.initial_connect.success" in m for m in messages
        ), f"no .success event found in {messages}"
        # elapsed_sec is attached when attempt > 1.
        success_lines = [
            m for m in messages
            if "event=openai.initial_connect.success" in m
        ]
        assert any("elapsed_sec=" in m for m in success_lines)
    finally:
        await conn.stop()


def test_initial_connect_budget_env_default_when_unset(monkeypatch):
    """Constructing with ``initial_connect_budget_sec=None`` reads
    the env var; missing env var → the module's documented default
    (DEFAULT_INITIAL_CONNECT_BUDGET_SEC = 600 s)."""
    from jasper.voice.openai_session import DEFAULT_INITIAL_CONNECT_BUDGET_SEC
    monkeypatch.delenv(
        "JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC", raising=False,
    )
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        connect_factory=factory,
        backoff_schedule=(0.0,),
    )
    assert conn._initial_connect_budget_sec == DEFAULT_INITIAL_CONNECT_BUDGET_SEC


def test_initial_connect_budget_env_override(monkeypatch):
    """``JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC`` env var overrides
    the default at construction time. Explicit kwarg still wins (covered
    by every other test in this section — they all pass ``budget_sec``
    via the helper)."""
    monkeypatch.setenv("JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC", "42")
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        connect_factory=factory,
        backoff_schedule=(0.0,),
    )
    assert conn._initial_connect_budget_sec == 42.0


def test_initial_connect_budget_env_garbage_falls_back(monkeypatch):
    """Non-numeric / negative env values must not refuse to start the
    daemon — log a warning, fall back to default. Better the daemon
    boots with documented behaviour than refuses over a typo."""
    from jasper.voice.openai_session import DEFAULT_INITIAL_CONNECT_BUDGET_SEC
    for bad in ("not-a-number", "-5"):
        monkeypatch.setenv(
            "JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC", bad,
        )
        factory = _FakeConnectFactory()
        conn = OpenAIRealtimeConnection(
            api_key="fake",
            connect_factory=factory,
            backoff_schedule=(0.0,),
        )
        assert (
            conn._initial_connect_budget_sec
            == DEFAULT_INITIAL_CONNECT_BUDGET_SEC
        ), f"bad value {bad!r} should fall back to default"


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


# ---------------------------------------------------------------------------
# Proactive pre-cap reconnect watchdog.
# ---------------------------------------------------------------------------


async def test_proactive_watchdog_fires_before_cap_when_idle():
    """When the watchdog timer fires and no turn is in flight, it sets
    `_reconnect_event` directly. The supervisor then tears down and
    reconnects — same code path as the reactive 60-min cap recovery,
    just initiated locally and ~5 min early.

    Production uses (3600, 300) → 55-min trigger. Tests use small
    values (0.10, 0.05) → 0.05 s trigger, so the assertion is fast."""
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        session_max_sec=0.10,
        proactive_buffer_sec=0.05,
        backoff_schedule=(0.0,),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        # First connect happened.
        assert len(factory.conns) == 1
        # After ~50 ms the watchdog should set _reconnect_event, the
        # supervisor should reconnect, and a SECOND session should open.
        await _wait_until(lambda: len(factory.conns) >= 2, timeout=2.0)
        assert len(factory.conns) >= 2
    finally:
        await conn.stop()


async def test_proactive_watchdog_defers_when_turn_active():
    """If the watchdog fires mid-turn, it does NOT tear down — it sets
    `_deferred_reconnect` pending and lets `_on_turn_released` fire
    the reconnect after the user's turn ends. We don't want to yank a
    live conversation when we have 5 min of safety margin to wait."""
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        session_max_sec=0.10,
        proactive_buffer_sec=0.05,
        backoff_schedule=(0.0,),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        turn = await conn.acquire_turn()
        # Give the watchdog time to fire while the turn is active.
        await asyncio.sleep(0.12)
        # No new session yet — fire was deferred.
        assert len(factory.conns) == 1
        assert conn._deferred_reconnect.pending is True
        # Release the turn → deferred reconnect fires, supervisor opens
        # session 2.
        await turn.release()
        await _wait_until(lambda: len(factory.conns) >= 2, timeout=2.0)
        assert conn._deferred_reconnect.pending is False
    finally:
        await conn.stop()


async def test_proactive_watchdog_cancelled_on_teardown():
    """Stopping the connection mid-wait must cancel the watchdog cleanly.
    Without this, the task would either fire against a stopped
    connection (harmless but noisy) or leak as an orphaned coroutine
    warning under pytest-asyncio."""
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        # Watchdog wouldn't fire for ~10 s — plenty of time to stop first.
        session_max_sec=20.0,
        proactive_buffer_sec=10.0,
        backoff_schedule=(0.0,),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    task = conn._proactive_watchdog_task
    assert task is not None
    assert not task.done()
    await conn.stop()
    # Stop awaits the cancelled task internally; it must be done now.
    assert task.done()
    # And only one session was ever opened.
    assert len(factory.conns) == 1


async def test_proactive_watchdog_disabled_when_either_knob_zero():
    """Either `session_max_sec=0` OR `proactive_buffer_sec=0` disables
    the watchdog. Default OpenAIRealtimeConnection construction (no
    knobs passed) must NOT spawn a task — test isolation depends on it.
    Also covers the Grok production default (both 0)."""
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        # Explicitly disabled — buffer is 0.
        session_max_sec=3600.0,
        proactive_buffer_sec=0.0,
        backoff_schedule=(0.0,),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        assert conn._proactive_watchdog_task is None
    finally:
        await conn.stop()


async def test_proactive_watchdog_disabled_when_buffer_exceeds_cap():
    """Misconfiguration (buffer ≥ cap) must NOT spawn a task that would
    fire instantly on every reconnect — that's a worse failure than
    just leaving the watchdog off."""
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        session_max_sec=300.0,
        proactive_buffer_sec=500.0,  # > cap
        backoff_schedule=(0.0,),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        assert conn._proactive_watchdog_task is None
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Server-side VAD.
# ---------------------------------------------------------------------------


async def test_noise_reduction_omitted_by_default_in_session_payload():
    """Bare adapter construction omits provider denoising by default."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        upd = _find_event(factory.conns[0].sent, "session.update")
        assert upd is not None
        assert "noise_reduction" not in upd["session"]["audio"]["input"]
    finally:
        await conn.stop()


async def test_noise_reduction_far_field_in_session_payload_when_requested():
    """The resolved provider policy can still request far_field explicitly."""
    conn, factory = _make_conn(noise_reduction="far_field")
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        upd = _find_event(factory.conns[0].sent, "session.update")
        assert upd is not None
        nr = upd["session"]["audio"]["input"].get("noise_reduction")
        assert nr == {"type": "far_field"}
    finally:
        await conn.stop()


async def test_noise_reduction_can_be_disabled_in_session_payload():
    """Chip-AEC streams can opt out of OpenAI-side denoising."""
    conn, factory = _make_conn(noise_reduction="off")
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        upd = _find_event(factory.conns[0].sent, "session.update")
        assert upd is not None
        assert "noise_reduction" not in upd["session"]["audio"]["input"]
    finally:
        await conn.stop()


async def test_supports_server_vad_returns_true():
    """OpenAI adapter declares server_vad capability."""
    conn, _ = _make_conn()
    assert conn.supports_server_vad() is True


async def test_set_turn_detection_sends_session_update():
    """Switching to server_vad sends input_audio_buffer.clear then
    session.update with the mode dict."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        baseline = len(sess.sent)
        mode = {
            "type": "server_vad",
            "threshold": 0.5,
            "silence_duration_ms": 350,
            "create_response": False,
            "interrupt_response": False,
        }
        await conn.set_turn_detection(mode)
        new = sess.sent[baseline:]
        types = [e["type"] for e in new]
        assert "input_audio_buffer.clear" in types
        assert "session.update" in types
        su = [e for e in new if e["type"] == "session.update"][0]
        # OpenAI's API requires `session.type` on every session.update,
        # not just the first. Omitting it returns
        # missing_required_parameter and the switch silently no-ops —
        # observed in production on 2026-05-24 against gpt-realtime-2.
        assert su["session"]["type"] == "realtime"
        td = su["session"]["audio"]["input"]["turn_detection"]
        assert td["type"] == "server_vad"
        assert td["create_response"] is False
        assert td["interrupt_response"] is False
        assert conn._server_vad_active is True
    finally:
        await conn.stop()


async def test_set_turn_detection_null_restores_manual():
    """Switching back to manual VAD sends session.update with
    turn_detection: null and does NOT send input_audio_buffer.clear."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        await conn.set_turn_detection({"type": "server_vad"})
        baseline = len(sess.sent)
        await conn.set_turn_detection(None)
        new = sess.sent[baseline:]
        types = [e["type"] for e in new]
        assert "input_audio_buffer.clear" not in types
        su = [e for e in new if e["type"] == "session.update"][0]
        assert su["session"]["type"] == "realtime"
        assert su["session"]["audio"]["input"]["turn_detection"] is None
        assert conn._server_vad_active is False
    finally:
        await conn.stop()


async def test_end_input_noop_under_server_vad():
    """When server_vad is active, end_input is a no-op — the server
    already committed the buffer."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        await conn.set_turn_detection({"type": "server_vad"})
        turn = await conn.acquire_turn()
        turn._mark_server_vad()
        baseline = len(sess.sent)
        await turn.end_input()
        new_types = [e["type"] for e in sess.sent[baseline:]]
        assert "input_audio_buffer.commit" not in new_types
        assert "response.create" not in new_types
        await turn.release()
    finally:
        await conn.stop()


async def test_server_vad_speech_events_dispatch():
    """speech_started, speech_stopped, committed events are routed to
    the turn when server_vad is active, and the EOU event fires when
    both speech_stopped and committed have arrived."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        await conn.set_turn_detection({"type": "server_vad"})
        turn = await conn.acquire_turn()
        turn._mark_server_vad()

        assert turn.server_speech_started() is False
        assert turn.server_speech_detected() is False

        sess.feed({"type": "input_audio_buffer.speech_started"})
        await asyncio.sleep(0.05)
        assert turn.server_speech_started() is True

        sess.feed({"type": "input_audio_buffer.speech_stopped"})
        await asyncio.sleep(0.05)
        assert turn.server_speech_detected() is False

        sess.feed({"type": "input_audio_buffer.committed"})
        await asyncio.sleep(0.05)
        assert turn.server_speech_detected() is True
        assert turn._committed is True
        assert turn._server_eou_event.is_set()

        await turn.release()
    finally:
        await conn.stop()


async def test_server_vad_events_ignored_under_manual_vad():
    """When manual VAD is active, speech events are logged but not
    dispatched to the turn."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        sess.feed({"type": "input_audio_buffer.speech_started"})
        await asyncio.sleep(0.05)
        assert turn._server_speech_started is False
        await turn.release()
    finally:
        await conn.stop()


async def test_create_response_only_sends_response_create_without_commit():
    """_create_response_only sends just response.create, no commit."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        baseline = len(sess.sent)
        await conn._create_response_only()
        new = sess.sent[baseline:]
        assert len(new) == 1
        assert new[0]["type"] == "response.create"
    finally:
        await conn.stop()


async def test_turn_release_restores_manual_vad():
    """After a server_vad turn is released, the connection restores
    manual VAD via session.update with turn_detection: null."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        await conn.set_turn_detection({"type": "server_vad"})
        assert conn._server_vad_active is True
        turn = await conn.acquire_turn()
        turn._mark_server_vad()
        await turn.release()
        await asyncio.sleep(0.05)
        assert conn._server_vad_active is False
        restore = [
            e for e in sess.sent
            if e.get("type") == "session.update"
            and e.get("session", {}).get("audio", {}).get("input", {}).get("turn_detection") is None
        ]
        assert len(restore) >= 1
    finally:
        await conn.stop()


async def test_committed_stops_send_audio():
    """After the server commits the audio buffer, further send_audio
    calls are no-ops (the buffer is closed)."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        await conn.set_turn_detection({"type": "server_vad"})
        turn = await conn.acquire_turn()
        turn._mark_server_vad()
        await turn.send_audio(b"\x00\x00" * 1280)
        baseline = len(sess.sent)
        turn._on_server_committed()
        await turn.send_audio(b"\x00\x00" * 1280)
        new_appends = [
            e for e in sess.sent[baseline:]
            if e.get("type") == "input_audio_buffer.append"
        ]
        assert len(new_appends) == 0
        await turn.release()
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Connection-uptime meter hooks (time-billed providers, e.g. Grok)
# ---------------------------------------------------------------------------
async def test_uptime_meter_hooks_fire_on_open_and_teardown():
    """The connection must call the wired uptime meter on a successful
    open and on teardown — the bridge that makes time-billed (Grok) cost
    non-zero. Regression guard: if these hooks are dropped, Grok cost
    silently reverts to $0 while every other test still passes. Grok
    inherits this connection wholesale, so the base class covers it."""
    conn, _factory = _make_conn()
    events: list[str] = []

    class _StubMeter:
        def mark_connected(self) -> None:
            events.append("connected")

        def mark_disconnected(self) -> None:
            events.append("disconnected")

    conn.set_uptime_meter(_StubMeter())
    registry = ToolRegistry()
    await conn.start(registry, "")
    # _open_session is awaited inside start(), so the open is recorded
    # deterministically by the time start() returns.
    assert events == ["connected"]
    await conn.stop()
    assert events == ["connected", "disconnected"]


async def test_no_uptime_meter_by_default_is_safe():
    """Token-billed providers (OpenAI/Gemini) never get a meter wired;
    open/teardown must be no-ops on that path, not raise."""
    conn, _factory = _make_conn()
    assert conn._uptime_meter is None
    registry = ToolRegistry()
    await conn.start(registry, "")
    await conn.stop()  # must not raise with no meter set
