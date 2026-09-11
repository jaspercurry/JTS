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
import contextlib
import json
import logging

import pytest
from openai.types.realtime import ResponseDoneEvent

from jasper.tools import ToolRegistry, tool
from jasper.voice import _base
from jasper.voice._base import BaseLiveConnection
from jasper.voice._supervisor import (
    CANT_CONNECT_CUE_SLUG,
    NEEDS_ATTENTION_CUE_SLUG,
    request_planned_reopen,
    run_reconnect_with_backoff,
)
from jasper.voice.openai_session import (
    ConnectionState,
    OpenAIRealtimeConnection,
    _upsample_16k_to_24k,
)
from jasper.voice.grok_session import GROK_WEBSOCKET_BASE_URL, GrokRealtimeConnection
from tests._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_signalled, wait_until
from tests._live_turn_fake import drain_audio_chunks
from tests._log_events import event_fields, event_records


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
        self.response_number = 0
        self.commit_number = 0
        self.response_id = "resp_1"
        self.item_id = "msg_1"

    async def send(self, event: dict) -> None:
        self.sent.append(event)
        if event["type"] == "session.update":
            self._inbox.put_nowait({"type": "session.updated", "session": event["session"]})

        elif event["type"] == "input_audio_buffer.commit":
            self.commit_number += 1
            self.feed({"type": "input_audio_buffer.committed", "item_id": f"user_{self.commit_number}"})
        elif event["type"] == "response.create":
            self.response_number += 1
            self.response_id = f"resp_{self.response_number}"
            self.item_id = f"msg_{self.response_number}"
            self.feed({"type": "response.created", "response": {"id": self.response_id}})
            self.feed({"type": "response.output_item.added", "item": {
                "id": self.item_id, "type": "message", "role": "assistant",
            }})

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
        event = dict(event)
        etype = event["type"]
        if etype.startswith("response."):
            if etype in ("response.done", "response.created"):
                event["response"] = {"id": self.response_id, "status": "completed", **event["response"]}
            else:
                event.setdefault("response_id", self.response_id)
                event.setdefault("item_id", self.item_id)
                if etype == "response.output_item.added":
                    self.item_id = event["item"]["id"]
        elif etype.startswith("conversation.item.input_audio_transcription."):
            event.setdefault("item_id", "user_1")
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


async def _begin_response(conn, wire):
    turn = conn._active_turn
    await turn.end_input()
    await _wait_until(lambda: turn._response_id is not None)
    wire.sent.clear()


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


def test_invalid_noise_reduction_rejected_at_construction():
    with pytest.raises(RuntimeError, match="OpenAI noise_reduction"):
        _make_conn(noise_reduction="potato")


def test_secret_literals_reports_the_api_key():
    """A rejection body echoing the key in a shape `redact_secrets`'s
    prefix patterns don't know still redacts, because the connection
    hands its own key back as a literal (ADR-0243). The base class
    returns none — it holds no secret of its own."""
    conn = OpenAIRealtimeConnection(api_key="plainvalue123")
    assert conn._secret_literals() == ("plainvalue123",)
    assert BaseLiveConnection._secret_literals(conn) == ()


async def test_transport_close_failure_redacts_the_connection_secret(caplog):
    """Every live adapter unwinds its SDK context manager through this one
    bounded helper, so an SDK error echoing the key must not reach the
    journal verbatim."""
    conn = OpenAIRealtimeConnection(api_key="plainvalue123")

    class _Cm:
        async def __aexit__(self, *_exc):
            raise RuntimeError("close rejected: OPENAI_API_KEY=plainvalue123")

    with caplog.at_level(logging.DEBUG, logger="jasper.voice.openai_session"):
        await conn._close_cm_with_timeout(_Cm())

    (record,) = event_records(caplog, "live.transport_close_failed")
    assert record.levelno == logging.WARNING
    assert "plainvalue123" not in event_fields(caplog, "live.transport_close_failed")["detail"]


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


async def test_session_update_failure_redacts_the_connections_own_key(caplog):
    """The retry-triggering warning logged when session.update fails must
    not leak a prefix-less key even when the rejection echoes it back
    verbatim — the connection hands its own key to `failure_detail` as a
    literal (ADR-0243), same as every other failure on this connection."""

    class _FailSessionUpdateConn(_FakeConn):
        async def send(self, event: dict) -> None:
            if event.get("type") == "session.update":
                raise RuntimeError('rejected: {"key":"plainvalue123"}')
            await super().send(event)

    def _connect_factory(*, model: str) -> _FakeAsyncCM:
        return _FakeAsyncCM(_FailSessionUpdateConn())

    conn = OpenAIRealtimeConnection(
        api_key="plainvalue123",
        backoff_schedule=(0.0, 0.0),
        connect_factory=_connect_factory,
    )
    with caplog.at_level(logging.WARNING, logger="jasper.voice.openai_session"):
        with pytest.raises(RuntimeError):
            await conn._open_session_attempt()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "plainvalue123" not in warnings[0].args[1]


@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection])
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "closed", "timeout"])
async def test_setup_acknowledgement_controls_readiness(conn_cls, outcome, monkeypatch, caplog):
    from jasper.voice import openai_session

    class DelayedSetup(_FakeConn):
        async def send(self, event):
            self.sent.append(event)

    wire = DelayedSetup()
    conn = conn_cls(api_key="plainvalue123", connect_factory=lambda **_: _FakeAsyncCM(wire))
    conn._registry = ToolRegistry()
    monkeypatch.setattr(openai_session, "SESSION_SETUP_TIMEOUT_SEC", 0.1)
    opening = asyncio.create_task(conn._open_session())
    await _wait_until(lambda: bool(wire.sent))
    assert not conn._connected_event.is_set()
    assert conn.is_paused()
    wire.feed({"type": "session.created"})
    await asyncio.sleep(0)
    assert not opening.done()
    try:
        if outcome == "accepted":
            wire.feed({"type": "session.updated", "session": wire.sent[0]["session"]})
            await opening
            assert not conn.is_paused()
            await (await conn.acquire_turn()).release()
        else:
            if outcome == "rejected":
                wire.feed({"type": "error", "error": {
                    "type": "invalid_request_error", "code": "invalid_parameter",
                    "message": "rejected plainvalue123",
                }})
            elif outcome == "closed":
                wire.feed_iter_stop()
            with pytest.raises((ValueError, ConnectionError, TimeoutError)):
                await opening
            assert conn.is_paused()
            assert conn.last_failure_detail()
            assert "plainvalue123" not in conn.last_failure_detail()
            assert "plainvalue123" not in caplog.text
            assert wire.closed
    finally:
        await conn.stop()


@pytest.mark.parametrize("error_type, code, transient", [
    ("invalid_request_error", "invalid_parameter", False),
    ("server_error", "server_error", True),
    ("rate_limit_error", "rate_limit_exceeded", True),
    ("invalid_request_error", "rate_limit_exceeded", True),
])
async def test_typed_setup_error_preserves_retry_and_cue(error_type, code, transient, caplog):
    from openai.types.realtime import RealtimeErrorEvent
    from jasper.voice._supervisor import is_transient

    class RejectedSetup(_FakeConn):
        async def send(self, event):
            self._inbox.put_nowait(RealtimeErrorEvent.model_validate({
                "type": "error", "event_id": "setup_error",
                "error": {"type": error_type, "code": code, "message": "setup rejected plainvalue123"},
            }))

    conn = OpenAIRealtimeConnection(
        api_key="plainvalue123", connect_factory=lambda **_: _FakeAsyncCM(RejectedSetup()),
        backoff_schedule=(0.0,),
    )
    with pytest.raises((ValueError, RuntimeError)) as failure:
        await conn._open_session()
    assert is_transient(failure.value) is transient
    assert conn.wake_cue() == (CANT_CONNECT_CUE_SLUG if transient else NEEDS_ATTENTION_CUE_SLUG)
    assert conn.is_paused()
    assert "plainvalue123" not in str(failure.value)
    await run_reconnect_with_backoff(conn)
    assert conn._state is ConnectionState.FAILED
    assert "plainvalue123" not in caplog.text
    assert "plainvalue123" not in conn.last_failure_detail()
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

        await turn.send_text_context("Answer yes or no about the pending question.")

        new = sess.sent[baseline:]
        assert new == [{
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "Answer yes or no about the pending question.",
                }],
            },
        }]
        await turn.release()
    finally:
        await conn.stop()


async def test_send_text_context_failure_marks_turn_lost(
    monkeypatch: pytest.MonkeyPatch,
):
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()

        async def fail(_text: str) -> None:
            raise OSError("socket closed")

        monkeypatch.setattr(factory.conns[0], "send", fail)
        await turn.send_text_context("context")

        assert turn.turn_lost() is True
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


@pytest.mark.parametrize("item, played, complete, expected", [
    ("owned", 4321, False, 4321),
    ("owned", 3200, True, 3200),
    ("owned", 0, False, 0),
    ("owned", 9000, False, 5000),
    ("foreign", 1000, False, None),
    (None, 1500, False, None),
    ("owned", -1, False, None),
    ("owned", "12", False, None),
])
async def test_truncate_requires_owned_item_and_explicit_boundary(item, played, complete, expected):
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        turn._received_ms_by_item["owned"] = 5000
        turn._server_turn_complete = complete
        baseline = len(sess.sent)
        await turn.truncate_assistant_audio(item, played)
        event = _find_event(sess.sent[baseline:], "conversation.item.truncate")
        if expected is None:
            assert event is None
        else:
            assert event == {
                "type": "conversation.item.truncate", "item_id": "owned",
                "content_index": 0, "audio_end_ms": expected,
            }
    finally:
        await conn.stop()


async def test_truncate_failure_redacts_the_connections_own_key(caplog):
    """`barge.truncate_failed`'s `detail` field must not leak a
    prefix-less key even when the rejection body echoes it back
    verbatim — the connection hands its own key to `failure_detail` as
    a literal (ADR-0243)."""
    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="plainvalue123", backoff_schedule=(0.0, 0.0),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        turn._received_ms_by_item["item_present"] = 1000

        async def _raise(event: dict) -> None:
            raise RuntimeError('rejected: {"key":"plainvalue123"}')

        sess.send = _raise

        with caplog.at_level(
            logging.WARNING, logger="jasper.voice.openai_session",
        ):
            await turn.truncate_assistant_audio("item_present", 1000)

        fields = event_fields(caplog, "barge.truncate_failed")
        assert "plainvalue123" not in fields["detail"]
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
        await _begin_response(conn, sess)
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
        usage = turn.usage()
        assert (usage.input_tokens, usage.output_tokens) == (12, 34)
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
        await _begin_response(conn, sess)
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
        (record,) = event_records(caplog, "openai.assistant_transcript")
        assert record.levelno == logging.DEBUG
        assert event_fields(caplog, "openai.assistant_transcript")["chars"] == "16"
        assert "Transport error." not in record.getMessage()
    finally:
        await conn.stop()


async def test_user_audio_transcript_logged_at_debug_not_info(caplog):
    caplog.set_level(logging.DEBUG, logger="jasper.voice.openai_session")
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        await conn.acquire_turn()
        await _begin_response(conn, sess)
        sess.feed({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "turn on the kitchen lights",
        })
        await _wait_until(
            lambda: bool(event_records(caplog, "openai.user_transcript")),
            timeout=2.0,
        )
        (record,) = event_records(caplog, "openai.user_transcript")
        assert record.levelno == logging.DEBUG
        assert event_fields(caplog, "openai.user_transcript")["chars"] == "26"
        assert "turn on the kitchen lights" not in record.getMessage()
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
        await _begin_response(conn, sess)
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


async def test_user_audio_transcript_dedupes_progressive_completions():
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await _begin_response(conn, sess)
        for transcript in (
            "Where's the next?",
            "Where's the next bus?",
            "Where's the next bus?",
        ):
            sess.feed({
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": transcript,
            })
        await _wait_until(
            lambda: turn.user_transcript() == "Where's the next bus?",
            timeout=2.0,
        )
        await turn.release()
    finally:
        await conn.stop()


async def test_user_audio_transcript_preserves_distinct_completions():
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await _begin_response(conn, sess)
        for transcript in (
            "turn on the kitchen lights",
            "set them to fifty percent",
        ):
            sess.feed({
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": transcript,
            })
        await _wait_until(
            lambda: turn.user_transcript()
            == "turn on the kitchen lights set them to fifty percent",
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
        await _begin_response(conn, sess)
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
        await _begin_response(conn, sess)
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
        await _begin_response(conn, sess)

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


async def test_unserializable_tool_result_does_not_kill_the_turn():
    """A tool returning a non-JSON-serializable payload must NOT crash
    the dispatch (which would escalate to _receive_loop's broad except
    and force a full session reconnect). The send is now guarded: it
    emits a synthetic error function_call_output for the same call_id,
    fires the round's response.create, and the connection is unchanged.
    """
    conn, factory = _make_conn()
    registry = ToolRegistry()

    @tool()
    def broken_tool() -> dict:
        """Returns something that can't be JSON-encoded."""
        return {"bad": object()}
    registry.register(broken_tool)

    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await _begin_response(conn, sess)

        sess.feed({
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 10, "output_tokens": 4},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_bad",
                        "name": "broken_tool",
                        "arguments": "{}",
                    },
                ],
            },
        })

        # A function_call_output for call_bad is still sent (with an
        # error output), and response.create still fires — the turn is
        # not silently dropped.
        await _wait_until(
            lambda: any(
                e.get("type") == "conversation.item.create"
                and e.get("item", {}).get("type") == "function_call_output"
                and e.get("item", {}).get("call_id") == "call_bad"
                for e in sess.sent
            ),
            timeout=2.0,
        )
        item_create = next(
            e for e in sess.sent
            if e.get("type") == "conversation.item.create"
            and e.get("item", {}).get("type") == "function_call_output"
        )
        # Output is a valid JSON string carrying an error, not a crash.
        parsed = json.loads(item_create["item"]["output"])
        assert "error" in parsed

        await _wait_until(
            lambda: any(e.get("type") == "response.create" for e in sess.sent),
            timeout=2.0,
        )

        # Crucially: no reconnect happened — still exactly one connection.
        assert len(factory.conns) == 1
        assert not sess.closed

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
        await _begin_response(conn, sess)

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
        await _begin_response(conn, sess)
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
                "usage": {
                    "input_tokens": 100, "output_tokens": 8,
                    "input_token_details": {
                        "audio_tokens": 60, "text_tokens": 40, "cached_tokens": 10,
                    },
                    "output_token_details": {"audio_tokens": 6, "text_tokens": 2},
                },
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
                "usage": {
                    "input_tokens": 50, "output_tokens": 200,
                    "input_token_details": {
                        "audio_tokens": 30, "text_tokens": 20, "cached_tokens": 5,
                    },
                    "output_token_details": {"audio_tokens": 190, "text_tokens": 10},
                },
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
        # full round-trip, and prices each modality bucket separately.
        usage = turn.usage()
        assert (usage.input_tokens, usage.output_tokens) == (150, 208)
        assert usage.breakdown == {
            "input_tokens": 150, "output_tokens": 208,
            "input_token_details": {
                "audio_tokens": 90, "text_tokens": 60, "cached_tokens": 15,
            },
            "output_token_details": {"audio_tokens": 196, "text_tokens": 12},
        }
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
    start.

    Driven through ``_handle_response_done`` rather than the wire: the
    receive loop resets the anchor on any progress event, so feeding
    this as a frame would move the anchor whatever the tool round did
    and the milestone would stop being pinned."""
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
        await _begin_response(conn, sess)
        anchor_before = turn.last_activity_at()

        # Park briefly so the loop clock advances measurably.
        await asyncio.sleep(0.05)

        await conn._handle_response_done({
            "id": "resp_1",
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 8},
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": "{}",
                },
            ],
        }, turn)

        anchor_after = turn.last_activity_at()
        assert anchor_after > anchor_before, (
            "tool round must advance last_activity_at so the pre-response "
            "idle watchdog doesn't fire while waiting for response 2"
        )
        # The round still runs to completion off that same milestone.
        await _wait_until(
            lambda: any(e.get("type") == "response.create" for e in sess.sent),
            timeout=2.0,
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
        await _begin_response(conn, sess)
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


@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection])
async def test_drop_signalled_during_a_reconnect_is_not_swallowed(conn_cls):
    """A set() landing during the reopen must trigger one more cycle (#3915)."""
    factory = _FakeConnectFactory()
    conn = conn_cls(
        api_key="fake",
        backoff_schedule=(0.0, 0.0),
        connect_factory=factory,
    )

    def signalling_factory(*, model: str):
        cm = factory(model=model)
        if len(factory.conns) == 2:
            # The supervisor's reopen is in flight; a fresh drop lands
            # before `_open_session_attempt` finishes.
            conn._reconnect_event.set()
        return cm

    conn._connect_factory = signalling_factory
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()
        factory.conns[0].feed_error(_Drop())

        await _wait_until(lambda: len(factory.conns) >= 3, timeout=3.0)
        await _wait_until(
            lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0,
        )
        turn = await conn.acquire_turn()
        await turn.release()
    finally:
        await conn.stop()


@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection])
async def test_idle_context_reset_reopens_through_the_supervisor(conn_cls):
    """The idle context reset hands its reopen to the supervisor.

    One reopener per connection slot: the reset's session is opened from
    a paused (supervisor-driven) state, and a drop signalled during that
    reopen still settles CONNECTED with every superseded session closed.
    """
    factory = _FakeConnectFactory()
    conn = conn_cls(
        api_key="fake",
        context_reset_sec=0.01,
        backoff_schedule=(0.0, 0.0),
        connect_factory=factory,
    )
    state_at_open: list[ConnectionState] = []

    def recording_factory(*, model: str):
        state_at_open.append(conn._state)
        cm = factory(model=model)
        if len(factory.conns) == 2:
            # A fresh drop lands while the reset's reopen is in flight.
            conn._reconnect_event.set()
        return cm

    conn._connect_factory = recording_factory
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        turn1 = await conn.acquire_turn()
        await turn1.release()
        await asyncio.sleep(0.05)

        turn2 = await asyncio.wait_for(conn.acquire_turn(), timeout=5.0)
        await turn2.release()
        await _wait_until(
            lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0,
        )
        # The initial connect is the turn task's; every reopen after it
        # belongs to the supervisor.
        assert state_at_open[0] is ConnectionState.CONNECTING
        assert state_at_open[1:] and all(
            state is ConnectionState.PAUSED_FOR_BACKOFF
            for state in state_at_open[1:]
        )
        assert len(factory.conns) >= 2
        # No orphan: the live session is the only one left open.
        assert [c for c in factory.conns if not c.closed] == [conn._conn]
        turn3 = await conn.acquire_turn()
        await turn3.release()
    finally:
        await conn.stop()


async def test_reconnect_escalation_cue_fires_once_per_outage():
    """The OpenAI loop, not only the shared helpers, drives the cue.

    A terminal reopen failure speaks once however many times it repeats
    within the outage; a recovery re-arms so the next outage speaks
    again; a transient outage stays silent.
    """
    conn, factory = _make_conn(backoff_schedule=(0.0,) * 4)
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
        factory.conns[-1].feed_error(_Drop("active socket dropped"))
        await _wait_until(
            lambda: (
                len(factory.conns) >= opens
                and conn._state is ConnectionState.CONNECTED
            ),
            timeout=3.0,
        )
        # The cue is fire-and-forget; give a spurious task a turn to run.
        await asyncio.sleep(0.05)

    try:
        await _outage(_Terminal, opens=2)
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG]
        # Recovered: no remedy outstanding.
        assert conn._outage.cue is None

        await _outage(_Terminal, opens=3)
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG] * 2

        await _outage(lambda: RuntimeError("transient"), opens=4)
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG] * 2

        # An outage that never recovers keeps naming its remedy, so a
        # wake landing in the paused window plays it.
        factory.next_exceptions = [_Terminal() for _ in range(8)]
        factory.conns[-1].feed_error(_Drop("active socket dropped"))
        await _wait_until(
            lambda: conn._state is ConnectionState.FAILED, timeout=3.0,
        )
        assert conn.wake_cue() == NEEDS_ATTENTION_CUE_SLUG
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG] * 3
    finally:
        await conn.stop()
    assert conn._state is ConnectionState.CLOSED


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


async def test_terminal_initial_connect_stays_up_and_heals():
    """A terminal first connect (403, account blocked) must not kill the
    daemon — the reconnect path survives the same rejection, so start()
    must too. It returns with the supervisor running, the outage visible
    on the fields `/state` reads, the escalation cue announced once, and
    self-healing once the provider accepts."""
    factory = _FakeConnectFactory()

    class _Blocked(Exception):
        status_code = 403

    factory.next_exceptions = [_Blocked("team blocked")]
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        connect_factory=factory,
        backoff_schedule=(0.0,),
    )
    cue_calls: list[str] = []

    async def cue_cb(slug: str) -> None:
        cue_calls.append(slug)

    registry = ToolRegistry()
    # Wired before the connect, as the daemon does: cue playback is up
    # before `start()` is called at all.
    conn.set_failure_escalation_cb(cue_cb)
    await conn.start(registry, "")
    try:
        # No await between start() and these — the supervisor task is
        # scheduled but has not run, so the post-failure state is intact.
        assert conn._supervisor_task is not None
        assert conn.is_paused()
        assert isinstance(conn.last_failure_detail(), str)

        await _wait_until(lambda: cue_calls == [NEEDS_ATTENTION_CUE_SLUG])
        await _wait_until(
            lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0,
        )
        assert not conn.is_paused()
        assert conn.last_failure_detail() is None
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG]
    finally:
        await conn.stop()


@pytest.mark.parametrize(
    "conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection],
)
async def test_setup_rejection_on_initial_connect_stays_up(conn_cls):
    """A close-1007 setup rejection carries no HTTP status, only a
    provider code — it must still rule terminal so the daemon stays up,
    announces the remedy once and reports the outage on `/state`."""

    class _SetupRejected(Exception):
        code = 1007

    factory = _FakeConnectFactory()
    factory.next_exceptions = [_SetupRejected("setup rejected")]
    conn = conn_cls(
        api_key="fake",
        connect_factory=factory,
        backoff_schedule=(0.0,),
    )
    cue_calls: list[str] = []

    async def cue_cb(slug: str) -> None:
        cue_calls.append(slug)

    conn.set_failure_escalation_cb(cue_cb)
    await conn.start(ToolRegistry(), "")
    try:
        assert conn._supervisor_task is not None
        assert conn.is_paused()
        assert isinstance(conn.last_failure_detail(), str)

        await _wait_until(lambda: cue_calls == [NEEDS_ATTENTION_CUE_SLUG])
    finally:
        await conn.stop()


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
# Initial connect — one attempt, then the supervisor's to retry.
# ---------------------------------------------------------------------------


async def test_transient_first_connect_leaves_the_daemon_up_and_connects():
    """A link that is down at boot must not cost the process: `start()`
    returns paused, and the supervisor's next attempt connects."""
    conn, factory = _make_conn(backoff_schedule=(0.0,))
    factory.next_exceptions = [
        OSError(-3, "Temporary failure in name resolution"),
    ]
    await conn.start(ToolRegistry(), "")
    try:
        # No await between start() and these — the supervisor task is
        # scheduled but has not run, so the post-failure state is intact.
        assert conn._supervisor_task is not None
        assert conn.is_paused()

        await _wait_until(
            lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0,
        )
        assert conn.last_failure_detail() is None
    finally:
        await conn.stop()


async def test_first_connect_records_the_provider_reason():
    """A restart mid-outage must not repeat the 2026-09-01 blind spot.

    websockets renders a refused handshake as a bare "HTTP 403"; the
    reason the household can act on is in the response body. Pin that
    the body reaches `last_failure_detail` (and so the journal and
    /state.voice.connection_error), not just the status line.
    """
    class _RejectedResponse:
        status_code = 403
        body = b'{"error":"Your team has used all available credits."}'

    class _Rejected(Exception):
        def __init__(self) -> None:
            super().__init__(
                "server rejected WebSocket connection: HTTP 403"
            )
            self.response = _RejectedResponse()

    conn, factory = _make_conn(backoff_schedule=(0.0,))
    factory.next_exceptions = [_Rejected()]
    await conn.start(ToolRegistry(), "")
    # Read before any await: the supervisor task is scheduled but has
    # not reconnected yet, so the outage detail still stands.
    detail = conn.last_failure_detail()
    try:
        assert detail is not None
        assert "used all available credits" in detail
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


async def test_unplanned_drop_does_not_inherit_the_rotation_zero_backoff():
    """Twin of the Gemini pin: a queued planned reopen must not hand its
    skip-the-backoff ticket to a real failure that lands first.

    This adapter's watchdog is a cap pre-empt, so the flag is raised
    here the way the idle context reset raises it.
    """
    delays: list[float] = []

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)

    factory = _FakeConnectFactory()
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        backoff_schedule=None,
        connect_factory=factory,
        sleep=_sleep,
    )
    await conn.start(ToolRegistry(), "system")
    try:
        conn._planned_rotate = True

        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal closure"
            rcvd = _Rcvd()

        # The server drops us before the queued rotation fires.
        factory.conns[0].feed_error(_Drop())
        await _wait_until(lambda: len(factory.conns) >= 2, timeout=3.0)
        assert delays and delays[0] > 0.0, delays
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


@pytest.mark.parametrize("intent, wire", [
    ("off", None),  # chip-AEC streams opt out of OpenAI-side denoising
    ("auto", None),  # unresolved intent: omit rather than guess
    ("near", {"type": "near_field"}),
    ("far", {"type": "far_field"}),
])
async def test_session_payload_maps_host_intent_to_the_openai_wire_value(
    intent, wire,
):
    conn, factory = _make_conn(noise_reduction=intent)
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        upd = _find_event(factory.conns[0].sent, "session.update")
        assert upd is not None
        assert upd["session"]["audio"]["input"].get("noise_reduction") == wire
    finally:
        await conn.stop()


async def test_committed_stops_send_audio():
    """Once the user audio buffer is committed, further send_audio calls
    are no-ops (the buffer is closed)."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await turn.send_audio(b"\x00\x00" * 1280)
        await turn.end_input()
        baseline = len(sess.sent)
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
# Billable realtime-activity meter hooks (time-billed providers, e.g. Grok)
# ---------------------------------------------------------------------------
async def test_activity_meter_hooks_fire_on_turn_acquire_and_release():
    """The connection must meter active turns, not idle socket time.

    Regression guard: if these hooks move back to WebSocket open/teardown,
    Grok spend runs ahead of xAI's dashboard and can false-trip the cap.
    Grok inherits this connection wholesale, so the base class covers it.
    """
    conn, _factory = _make_conn()
    events: list[str] = []

    class _StubMeter:
        def mark_started(self) -> None:
            events.append("started")

        def mark_ended(self) -> None:
            events.append("ended")

    conn.set_billable_activity_meter(_StubMeter())
    registry = ToolRegistry()
    await conn.start(registry, "")
    assert events == []
    turn = await conn.acquire_turn()
    assert events == ["started"]
    await turn.release()
    assert events == ["started", "ended"]
    await conn.stop()
    assert events == ["started", "ended"]


async def test_no_activity_meter_by_default_is_safe():
    """Token-billed providers (OpenAI/Gemini) never get a meter wired;
    open/teardown must be no-ops on that path, not raise."""
    conn, _factory = _make_conn()
    assert conn._billable_activity_meter is None
    registry = ToolRegistry()
    await conn.start(registry, "")
    await conn.stop()  # must not raise with no meter set


# ---------------------------------------------------------------------------
# Post-connect reconnect cadence (issue #3855).
# ---------------------------------------------------------------------------


async def _reconnect_delays(exc, count: int = 4):
    """Drive the PRODUCTION (unbounded) reconnect loop against a connect
    factory that always raises.

    ``exc`` is one exception raised on every attempt, or a list raised
    in order with the last one repeating. Returns the connection and the
    delays the loop actually slept for. ``backoff_schedule=None`` is the
    production wiring — the bounded test schedule other tests pass would
    mask the classification.
    """
    excs = list(exc) if isinstance(exc, list) else [exc]
    delays: list[float] = []
    holder: dict = {"raised": 0}

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= count:
            holder["conn"]._stopping.set()

    def _factory(*, model: str):
        i = min(holder["raised"], len(excs) - 1)
        holder["raised"] += 1
        raise excs[i]

    conn = OpenAIRealtimeConnection(
        api_key="fake",
        backoff_schedule=None,
        connect_factory=_factory,
        sleep=_sleep,
    )
    holder["conn"] = conn
    # The loop is unbounded and only exits because `_sleep` sets the
    # stopping flag; a change that stopped reaching that await would
    # otherwise spin to the 300 s global timeout.
    await asyncio.wait_for(run_reconnect_with_backoff(conn), timeout=10.0)
    return conn, delays


def _terminal_poll_band() -> tuple[float, float]:
    """The jittered slow-poll interval's lower and upper bound."""
    from jasper.backoff import (
        RECONNECT_BACKOFF_JITTER_FRACTION,
        TERMINAL_POLL_INTERVAL_SEC,
    )

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


async def test_terminal_reconnect_failure_drops_to_slow_poll():
    """A rejection retrying cannot fix must stop hammering the cap.

    Everything after the first (still-unclassified) delay must be the
    fixed slow poll. See issue #3855.
    """
    conn, delays = await _reconnect_delays(_TerminalRejection())
    assert len(delays) == 4
    lo, hi = _terminal_poll_band()
    assert all(lo <= d <= hi for d in delays[1:]), delays
    # A slow poll, never a park: the loop is still retrying, so an
    # operator who restores the key recovers without a restart.
    assert conn._state is ConnectionState.PAUSED_FOR_BACKOFF
    # The cause survives the whole poll, so /state and the wake cue can
    # still name the remedy however long the outage lasts.
    assert conn.last_failure_detail() is not None


async def test_transient_reconnect_failure_keeps_exponential_backoff():
    """A network blip keeps the ramp; only a terminal failure polls.

    The schedule's own shape is pinned in `test_reconnect_backoff.py`;
    what this owns is which of the two the loop picked.
    """
    from jasper.backoff import RECONNECT_MAX_BACKOFF_SEC

    # OSError carries no status, so `is_transient` returns True.
    _conn, delays = await _reconnect_delays(
        OSError(-3, "Temporary failure in name resolution"), count=6,
    )
    assert all(d <= RECONNECT_MAX_BACKOFF_SEC * 1.25 for d in delays), delays
    assert delays[0] < delays[-1], delays


async def test_recovering_provider_leaves_the_slow_poll_and_restarts_the_ramp():
    """A terminal outage that turns transient is recovering: retry it in
    about a second, not from wherever the slow poll left the counter."""
    from jasper.backoff import (
        RECONNECT_BACKOFF_JITTER_FRACTION,
        RECONNECT_INITIAL_BACKOFF_SEC,
    )

    conn, delays = await _reconnect_delays(
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
    """A connect that hangs on the first attempt, then always fails.

    The hang is the window the household's wake actually lands in: a TCP
    or TLS connect to a dead provider takes tens of seconds, and
    `is_paused()` is true for all of it."""

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


async def test_wake_during_a_connect_attempt_cuts_the_next_wait_short():
    """`request_reconnect_now` must land wherever `is_paused()` is true.

    A bare `sleep(900)` cannot be shortened, so without an interruptible
    wait a household whose credit is restored waits out the interval;
    and a nudge accepted during the connect attempt itself must not be
    dropped before the next wait sees it. See issue #3855.
    """
    delays: list[float] = []
    connecting = asyncio.Event()
    release = asyncio.Event()

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 1:
            return  # the seeded ramp delay
        # Every terminal poll: nothing but a nudge can end this wait.
        await asyncio.sleep(3600)

    connect = _SlowThenDeadConnect(connecting, release)
    conn = OpenAIRealtimeConnection(
        api_key="fake",
        backoff_schedule=None,
        connect_factory=lambda *, model: connect,
        sleep=_sleep,
    )
    task = asyncio.ensure_future(run_reconnect_with_backoff(conn))
    try:
        await asyncio.wait_for(connecting.wait(), timeout=5.0)
        assert conn.is_paused()
        assert conn.request_reconnect_now() is True
        # The gate cannot be bypassed by asking again.
        assert conn.request_reconnect_now() is False
        release.set()
        await _wait_until(lambda: len(delays) == 3, timeout=5.0)
        lo, hi = _terminal_poll_band()
        assert lo <= delays[1] <= hi, delays
        # One nudge buys exactly one extra attempt: the loop is parked
        # on the next poll, not spinning through the schedule.
        await asyncio.sleep(0.05)
        assert len(delays) == 3, delays
        assert connect.calls == 2
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_cancelling_a_long_backoff_unwinds_at_once():
    """Shutdown must not be held hostage by a 15-minute poll.

    `stop()` cancels the supervisor task and the cancellation unwinds
    through the backoff wait. A wait that swallowed it would leave the
    daemon sitting out the poll it was told to abandon.
    """
    started = asyncio.Event()
    holder: dict = {}

    def _dead(*, model: str):
        raise _TerminalRejection()

    async def _sleep(seconds: float) -> None:
        if started.is_set():
            # Only reached if the cancellation was swallowed. End the
            # loop so the assertion below reports it instead of hanging.
            holder["conn"]._stopping.set()
            return
        started.set()
        await asyncio.sleep(seconds)

    conn = OpenAIRealtimeConnection(
        api_key="fake",
        backoff_schedule=None,
        connect_factory=_dead,
        sleep=_sleep,
    )
    holder["conn"] = conn
    task = asyncio.ensure_future(run_reconnect_with_backoff(conn))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert task.cancelled()



async def test_first_chunk_event_reports_latency_since_the_ask(caplog):
    """`since_end_input_ms` is the provider's own latency — the interval
    between asking for a response and the first audio of it coming back.
    The ask is the daemon's `end_input()`; the field is absent only when
    this turn was never asked, because there is then no interval to report.
    `since_turn_start_ms` still spans the user's whole utterance plus local
    endpointing, so it is not a provider figure."""
    caplog.set_level(logging.INFO, logger="jasper.voice.openai_session")
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        sess = factory.conns[0]
        turn = await conn.acquire_turn()
        await _begin_response(conn, sess)
        await asyncio.sleep(0.01)
        sess.feed({
            "type": "response.output_audio.delta",
            "delta": _b64(b"chunk"),
            "response_id": "resp_1",
        })
        await _wait_until(lambda: turn.chunks_received() >= 1)

        fields = event_fields(caplog, "turn.first_chunk")
        assert fields["provider"] == "openai"
        assert int(fields["since_turn_start_ms"]) >= 10
        assert 0 <= int(fields["since_end_input_ms"]) <= int(fields["since_turn_start_ms"])
        await turn.release()
    finally:
        await conn.stop()


@pytest.mark.parametrize(
    "conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection],
)
async def test_the_first_connect_reads_as_paused_while_it_dials(conn_cls):
    """The daemon serves wake while the first connect is still dialling
    (a boot with the WAN down). A wake landing then must take the paused
    path — the bounded re-check and a connection cue — rather than open a
    turn against a socket that does not exist yet."""
    connecting = asyncio.Event()
    release = asyncio.Event()
    connect = _SlowThenDeadConnect(connecting, release)
    conn = conn_cls(
        api_key="fake",
        backoff_schedule=(0.0,),
        connect_factory=lambda *, model: connect,
    )
    task = asyncio.ensure_future(conn.start(ToolRegistry(), ""))
    try:
        await asyncio.wait_for(connecting.wait(), timeout=5.0)
        assert conn.is_paused()
        # No failure recorded yet: the honest cue is the generic
        # "can't connect right now, I'll keep trying".
        assert conn.wake_cue() == CANT_CONNECT_CUE_SLUG
    finally:
        release.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5.0)
        await conn.stop()


# ---------------------------------------------------------------------------
# Playout-queue byte ceiling.
# ---------------------------------------------------------------------------


async def test_playout_queue_ceiling_drops_the_newest_chunk(caplog, monkeypatch):
    """The per-turn playout queue is the only PCM carrier and burst
    providers fill it ahead of realtime, so a wedged consumer would grow it
    without bound. Past the ceiling the INCOMING chunk is dropped (tail
    truncation, the same shape a barge-in flush leaves), the loss is
    counted, and the terminal sentinel still ends the iterator."""
    monkeypatch.setattr(_base, "AUDIO_OUT_QUEUE_MAX_BYTES", 10)
    conn, _factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        with caplog.at_level(
            logging.WARNING, logger="jasper.voice.openai_session",
        ):
            await turn._on_audio_delta(_b64(b"12345"))
            await turn._on_audio_delta(_b64(b"67890"))
            await turn._on_audio_delta(_b64(b"X"))
            await turn._on_audio_delta(_b64(b"YZ"))

        assert turn.audio_chunks_pending() == 2
        assert turn.audio_dropped_bytes() == 3, (
            "both over-ceiling chunks must be counted, not just the first"
        )
        fields = event_fields(caplog, "turn.audio_overflow")
        assert int(fields["queued_bytes"]) == 10
        assert int(fields["dropped_bytes"]) == 1, (
            "only the FIRST drop of the turn logs; later drops just count"
        )

        turn._audio_q.put_nowait(None)
        played = await asyncio.wait_for(drain_audio_chunks(turn), timeout=1.0)
        assert [chunk.pcm for chunk in played] == [b"12345", b"67890"]
    finally:
        await conn.stop()


async def test_dropping_pending_audio_frees_the_ceiling(monkeypatch):
    """A barge-in flush clears the queue, so the accounting the ceiling
    reads must clear with it — otherwise the turn stays permanently full
    and the model's next words are dropped.

    The dropped-byte count clears too. One turn spans several barge-in
    episodes (``play_responses`` keeps going after a flush), and the
    daemon cues ``internal_error`` at teardown on a non-zero count; a
    truncation the household caused by talking over the model must not
    end the turn with that cue. The first drop's ``turn.audio_overflow``
    WARN stays as the journal record."""
    monkeypatch.setattr(_base, "AUDIO_OUT_QUEUE_MAX_BYTES", 10)
    conn, _factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        await turn._on_audio_delta(_b64(b"0123456789"))
        await turn._on_audio_delta(_b64(b"X"))
        assert turn.audio_dropped_bytes() == 1

        turn.drop_pending_audio()
        await turn._on_audio_delta(_b64(b"after"))

        assert turn.audio_chunks_pending() == 1
        assert turn.audio_dropped_bytes() == 0
    finally:
        await conn.stop()


async def test_dropping_pending_audio_drains_behind_the_sentinel():
    """A chunk can be queued BEHIND the terminal sentinel: the socket
    drops mid-reply (``_on_connection_lost`` queues the sentinel) and a
    late audio delta still lands. A drain that stopped at the first
    sentinel would re-queue it AHEAD of that chunk, and the consumer
    would play post-barge audio as if it were pre-interrupt."""
    conn, _factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        turn._on_connection_lost()
        await turn._on_audio_delta(_b64(b"late"))
        assert turn.audio_chunks_pending() == 2

        assert turn.drop_pending_audio() == 1
        played = await asyncio.wait_for(drain_audio_chunks(turn), timeout=1.0)
        assert played == []
    finally:
        await conn.stop()


@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection])
async def test_stale_response_and_input_events_cannot_reach_a_new_turn(conn_cls):
    factory = _FakeConnectFactory()
    conn = conn_cls(api_key="fake", connect_factory=factory, backoff_schedule=(0.0,))
    calls = []
    registry = ToolRegistry()

    @tool()
    def action() -> dict:
        """Run an action."""
        calls.append(True)
        return {}

    registry.register(action)
    await conn.start(registry, "")
    try:
        wire = factory.conns[0]
        old = await conn.acquire_turn()
        await _begin_response(conn, wire)
        done = {"type": "response.done", "response": {
            "id": "resp_1", "status": "completed", "output": [],
        }}
        await conn._dispatch_event(done["type"], done)
        await old.release()
        fresh = await conn.acquire_turn()
        await _begin_response(conn, wire)
        for event in (
            {"type": "response.output_audio.delta", "response_id": "resp_1", "item_id": "msg_1", "delta": _b64(b"old")},
            {"type": "response.output_audio.delta", "response_id": "resp_2", "item_id": "msg_1", "delta": _b64(b"wrong item")},
            {"type": "conversation.item.input_audio_transcription.completed", "item_id": "user_1", "transcript": "old"},
            done,
            {"type": "response.done", "response": {"id": "resp_1", "status": "completed", "output": [
                {"type": "function_call", "call_id": "old_call", "name": "action", "arguments": "{}"},
            ]}},
        ):
            await conn._dispatch_event(event["type"], event)
        assert fresh.chunks_received() == 0
        assert not fresh.server_turn_complete()
        assert not fresh.turn_lost()
        assert fresh.capture() is None
        assert calls == []
        assert wire.sent == []
        await old.cancel_response("late")
        await old.truncate_assistant_audio("msg_1", 20)
        assert wire.sent == []
        await conn._dispatch_event("response.output_audio.delta", {
            "response_id": "resp_2", "item_id": "msg_2", "delta": _b64(b"fresh"),
        })
        assert (await anext(fresh.audio_out_chunks())).pcm == b"fresh"
    finally:
        await conn.stop()


@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection])
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "incomplete", "in_progress"])
@pytest.mark.parametrize("with_tools", [False, True])
async def test_response_status_controls_completion_and_tool_execution(conn_cls, status, with_tools):
    factory = _FakeConnectFactory()
    conn = conn_cls(api_key="fake", connect_factory=factory, backoff_schedule=(0.0,))
    registry = ToolRegistry()
    calls = []

    @tool()
    def action() -> dict:
        """Run an action."""
        calls.append(True)
        return {}

    registry.register(action)
    await conn.start(registry, "")
    try:
        wire = factory.conns[0]
        turn = await conn.acquire_turn()
        await _begin_response(conn, wire)
        event = {"type": "response.done", "response": {
            "id": "resp_1", "status": status,
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "output": [{"type": "function_call", "call_id": "call_1", "name": "action", "arguments": "{}"}] if with_tools else [],
        }}
        await conn._dispatch_event("response.done", event)
        await conn._dispatch_event("response.done", event)
        if status == "completed" and with_tools:
            await _wait_until(lambda: any(e["type"] == "response.create" for e in wire.sent))
        assert calls == ([True] if status == "completed" and with_tools else [])
        assert turn.server_turn_complete() is (status == "completed" and not with_tools)
        assert turn.turn_lost() is (status in ("failed", "cancelled", "incomplete"))
        assert turn.usage().input_tokens == (0 if status == "in_progress" else 2)
        assert sum(e["type"] == "response.create" for e in wire.sent) == len(calls)
    finally:
        await conn.stop()


async def test_requested_cancellation_is_terminal_without_a_failure():
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        await _begin_response(conn, factory.conns[0])
        await turn.cancel_response("barge_in")
        await turn.cancel_response("again")
        await conn._dispatch_event("response.done", {"response": {"id": "resp_1", "status": "cancelled"}})
        assert turn.server_turn_complete()
        assert not turn.turn_lost()
        assert sum(e["type"] == "response.cancel" for e in factory.conns[0].sent) == 1
    finally:
        await conn.stop()


@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection])
@pytest.mark.parametrize("boundary", ["release", "disconnect", "close", "cancel"])
async def test_pending_tool_cannot_block_or_cross_a_turn_boundary(conn_cls, boundary):
    factory = _FakeConnectFactory()
    conn = conn_cls(api_key="fake", connect_factory=factory, backoff_schedule=(0.0,))
    registry = ToolRegistry()
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []

    @tool()
    async def action() -> dict:
        """Run an action."""
        calls.append(True)
        entered.set()
        try:
            await resume.wait()
        except asyncio.CancelledError:
            await resume.wait()
        return {"ok": True}

    registry.register(action)
    await conn.start(registry, "")
    try:
        wire = factory.conns[0]
        old = await conn.acquire_turn()
        await _begin_response(conn, wire)
        wire._inbox.put_nowait(ResponseDoneEvent.model_validate({"type": "response.done", "event_id": "done_1", "response": {
            "id": "resp_1", "status": "completed", "output": [
                {"type": "function_call", "call_id": f"call_{i}", "name": "action", "arguments": "{}"}
                for i in range(2)
            ],
        }}))
        await wait_signalled(entered, "tool executor entered")
        if boundary == "disconnect":
            wire.feed_error(ConnectionError("disconnected"))
            await wait_until(lambda: old.turn_lost(), timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        elif boundary == "close":
            await asyncio.wait_for(conn.stop(), DEFAULT_SIGNAL_TIMEOUT_S)
            assert old.turn_lost()
            resume.set()
            await wait_until(lambda: not conn._tool_tasks and registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
            assert calls == [True]
            assert not any(e.get("item", {}).get("type") == "function_call_output" for e in wire.sent)
            return
        elif boundary == "cancel":
            await asyncio.wait_for(old.cancel_response("barge_in"), DEFAULT_SIGNAL_TIMEOUT_S)
            await old.cancel_response("again")
            assert old.server_turn_complete()
        await asyncio.wait_for(old.release(), DEFAULT_SIGNAL_TIMEOUT_S)
        fresh = await conn.acquire_turn()
        fresh_wire = factory.conns[-1]
        assert fresh_wire is not wire
        baseline = list(fresh_wire.sent)
        resume.set()
        await wait_until(lambda: not conn._tool_tasks and registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert calls == [True]
        assert fresh_wire.sent == baseline
        assert not any(e.get("item", {}).get("type") == "function_call_output" for e in wire.sent)
        assert not any(e["type"] == "response.create" for e in wire.sent)
        assert not fresh.server_turn_complete()
    finally:
        resume.set()
        await conn.stop()
        await wait_until(lambda: not conn._tool_tasks and registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)


@pytest.mark.parametrize("blocked_event", ["input_audio_buffer.append", "input_audio_buffer.commit"])
async def test_release_fences_a_send_already_waiting_or_writing(blocked_event):
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        wire = factory.conns[0]
        old = await conn.acquire_turn()
        entered, resume = asyncio.Event(), asyncio.Event()
        original_send = wire.send

        async def send(event):
            if event["type"] == blocked_event:
                entered.set()
                await resume.wait()
            await original_send(event)

        wire.send = send
        sending = asyncio.create_task(old.send_audio(b"\x01\x00" * 1280) if blocked_event.endswith("append") else old.end_input())
        await asyncio.wait_for(entered.wait(), 1)
        release = asyncio.create_task(old.release())
        await asyncio.sleep(0)
        resume.set()
        await asyncio.gather(sending, release)
        assert not any(e["type"] == "response.create" for e in wire.sent)
        assert wire.sent[-1]["type"] == "input_audio_buffer.clear"
        fresh = await conn.acquire_turn()
        fresh_wire = factory.conns[-1]
        baseline = len(fresh_wire.sent)
        await conn._send_lock.acquire()
        late = asyncio.create_task(old.send_text_context("old"))
        await asyncio.sleep(0)
        conn._send_lock.release()
        await late
        await fresh.send_audio(b"\x02\x00" * 1280)
        assert [e["type"] for e in fresh_wire.sent[baseline:]] == ["input_audio_buffer.append"]
    finally:
        await conn.stop()


async def test_sdk_events_keep_audio_item_identity():
    from openai.types.realtime import ResponseAudioDeltaEvent, ResponseOutputItemAddedEvent

    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        await _begin_response(conn, factory.conns[0])
        for item_id in ("first", "second"):
            event = ResponseOutputItemAddedEvent.model_validate({
                "type": "response.output_item.added", "event_id": item_id,
                "response_id": "resp_1", "output_index": 0,
                "item": {"id": item_id, "type": "message", "role": "assistant", "content": []},
            })
            await conn._dispatch_event(event.type, event)
        audio = ResponseAudioDeltaEvent.model_validate({
            "type": "response.output_audio.delta", "event_id": "audio",
            "response_id": "resp_1", "item_id": "first", "output_index": 0,
            "content_index": 0, "delta": _b64(b"\0" * 4800),
        })
        await conn._dispatch_event(audio.type, audio)
        chunk = await anext(turn.audio_out_chunks())
        assert chunk.provider_item_id == "first"
        assert chunk.pcm == b"\0" * 4800
        await turn.truncate_assistant_audio(chunk.provider_item_id, 50)
        assert factory.conns[0].sent[-1] == {
            "type": "conversation.item.truncate", "item_id": "first", "content_index": 0, "audio_end_ms": 50,
        }
    finally:
        await conn.stop()


async def test_aborted_input_is_cleared_before_the_fresh_command():
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        wire = factory.conns[0]
        buffered = bytearray()
        committed = []
        original_send = wire.send

        async def send(event):
            if event["type"] == "input_audio_buffer.append":
                buffered.extend(base64.b64decode(event["audio"]))
            elif event["type"] == "input_audio_buffer.clear":
                buffered.clear()
            elif event["type"] == "input_audio_buffer.commit":
                committed.append(bytes(buffered))
                buffered.clear()
            await original_send(event)

        wire.send = send
        old = await conn.acquire_turn()
        await old.send_audio(b"\x01\x00" * 1280)
        assert buffered
        await old.release()
        fresh = await conn.acquire_turn()
        command = b"\x02\x00" * 1280
        await fresh.send_audio(command)
        await fresh.end_input()
        assert factory.conns == [wire]
        assert committed == [_upsample_16k_to_24k(command, None)[0]]
    finally:
        await conn.stop()


@pytest.mark.parametrize("pending_ack", [False, True])
async def test_reconnect_discards_ownership_and_late_release(pending_ack):
    conn, factory = _make_conn()
    meter_events = []

    class Meter:
        def mark_started(self):
            meter_events.append("start")

        def mark_ended(self):
            meter_events.append("end")

    conn.set_billable_activity_meter(Meter())
    await conn.start(ToolRegistry(), "")
    try:
        old_wire = factory.conns[0]
        old = await conn.acquire_turn()
        if pending_ack:
            async def send_without_ack(event):
                old_wire.sent.append(event)
            old_wire.send = send_without_ack
            await old.end_input()
            await old.release()
        else:
            await _begin_response(conn, old_wire)
            old_wire.feed_error(ConnectionError("lost"))
            await _wait_until(lambda: len(factory.conns) == 2 and conn._connected_event.is_set())
        fresh = await conn.acquire_turn()
        fresh_wire = factory.conns[-1]
        assert fresh_wire is not old_wire
        assert meter_events == ["start", "end", "start"]
        conn._deferred_reconnect.request()
        await old.release()
        assert conn._state is ConnectionState.IN_TURN
        assert conn._deferred_reconnect.pending
        assert meter_events == ["start", "end", "start"]
        assert conn._pending_commit is None
        assert conn._pending_response is None
        for event in (
            {"type": "response.created", "response": {"id": "old_pending"}},
            {"type": "response.output_audio.delta", "response_id": "old_pending", "item_id": "old", "delta": _b64(b"old")},
            {"type": "response.done", "response": {"id": "old_pending", "status": "completed"}},
        ):
            old_wire.feed(event)
        await asyncio.sleep(0)
        assert not fresh.server_turn_complete()
        assert fresh.chunks_received() == 0
        assert fresh.capture() is None
        assert len(factory.conns) == 2
    finally:
        await conn.stop()


async def test_release_cleanup_keeps_its_original_socket_during_reconnect():
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    resume = asyncio.Event()
    try:
        old_wire = factory.conns[0]
        old = await conn.acquire_turn()
        await _begin_response(conn, old_wire)
        entered = asyncio.Event()
        original_send = old_wire.send

        async def send(event):
            if event["type"] == "response.cancel":
                entered.set()
                await resume.wait()
            await original_send(event)

        old_wire.send = send
        release = asyncio.create_task(old.release())
        await asyncio.wait_for(entered.wait(), 1)
        await conn._teardown_session()
        opening = asyncio.create_task(conn._open_session())
        await _wait_until(lambda: len(factory.conns) == 2)
        resume.set()
        await asyncio.gather(release, opening)
        assert [e["type"] for e in factory.conns[-1].sent] == ["session.update"]
        assert not conn.is_paused()
        assert not conn._reconnect_event.is_set()
    finally:
        resume.set()
        await conn.stop()


@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection])
@pytest.mark.parametrize("ending", ["error", "closed"])
async def test_closing_receive_cannot_request_another_reconnect(conn_cls, ending):
    entered = asyncio.Event()
    wires = []

    class ClosingConnection(_FakeConn):
        async def __anext__(self):
            if self._inbox.empty():
                entered.set()
            try:
                return await super().__anext__()
            except asyncio.CancelledError:
                if ending == "error":
                    raise ConnectionError("socket closed during teardown") from None
                raise StopAsyncIteration from None

    def connect(**kwargs):
        wire = ClosingConnection() if not wires else _FakeConn()
        wires.append(wire)
        return _FakeAsyncCM(wire)

    conn = conn_cls(api_key="fake", connect_factory=connect, backoff_schedule=(0.0,))
    await conn.start(ToolRegistry(), "")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        request_planned_reopen(conn)
        await _wait_until(lambda: conn._connected_event.is_set())
        assert len(wires) == 2
        assert not conn._reconnect_event.is_set()
    finally:
        await conn.stop()


@pytest.mark.parametrize("event, advances", [
    # Progress: the model is working, whatever this dispatcher then does
    # with the event. A slow generation emitting these must not be reaped.
    ({"type": "response.created", "response": {"id": "resp_9"}}, True),
    ({"type": "response.output_item.added", "response_id": "resp_1",
      "item": {"id": "msg_9"}}, True),
    ({"type": "response.content_part.added", "response_id": "resp_1",
      "item_id": "msg_1"}, True),
    ({"type": "conversation.item.input_audio_transcription.completed",
      "item_id": "other", "transcript": "hi"}, True),
    ({"type": "input_audio_buffer.committed", "item_id": "item_9"}, True),
    # Liveness only: the socket is open, the turn is not moving. Counting
    # these would unbound the pre-response phase.
    ({"type": "session.updated", "session": {}}, False),
    ({"type": "rate_limits.updated", "rate_limits": []}, False),
    ({"type": "error", "error": {"message": "transient"}}, False),
    # Client-echoed acks of events we sent (e.g. the barge-in truncate),
    # not evidence the model itself is progressing.
    ({"type": "conversation.item.truncated", "item_id": "item_9"}, False),
])
@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection])
async def test_only_progress_events_advance_the_idle_anchor(conn_cls, event, advances):
    """#4532: the pre-response idle timer must mean "this turn is not
    moving", not "no audio yet" and not "the socket went quiet".

    Anything the server sends between ``response.create`` and the first
    audio delta that shows work happening moves ``last_activity_at()``;
    bookkeeping and error frames do not, so a session that chatters
    without ever answering still reaches the watchdog."""
    factory = _FakeConnectFactory()
    conn = conn_cls(api_key="fake", connect_factory=factory, backoff_schedule=(0.0,))
    await conn.start(ToolRegistry(), "")
    try:
        wire = factory.conns[0]
        turn = await conn.acquire_turn()
        await _begin_response(conn, wire)
        stale = asyncio.get_event_loop().time() - 100.0
        turn._last_activity_at = stale

        await conn._dispatch_event(event["type"], event)

        assert (turn.last_activity_at() > stale) is advances
        assert turn.chunks_received() == 0
        assert turn.server_turn_complete() is False
    finally:
        await conn.stop()
