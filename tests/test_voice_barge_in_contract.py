"""Provider turn conformance and absent-response tolerance."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from google.genai import types
from openai.types.realtime import ResponseDoneEvent

from jasper.tools import ToolRegistry, tool
from jasper.voice.catalog import (
    PROVIDERS,
    InterruptReconcile,
    resolve_interrupt_reconcile,
)
from jasper.voice.gemini_session import GeminiLiveConnection, GeminiLiveTurn
from jasper.voice.grok_session import GrokRealtimeConnection
from jasper.voice.openai_session import (
    OpenAIRealtimeConnection,
    OpenAIRealtimeTurn,
)
from jasper.voice.session import Interruptible, LiveTurn
from jasper.voice.openai_live_session import OpenAILiveTurn
from tests._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_signalled, wait_until
from tests.test_gemini_connection import _FakeConnect
from tests.test_openai_session import _FakeConnectFactory


# The turn class each catalog provider drives. Grok defines no turn class of
# its own — `GrokRealtimeConnection` inherits OpenAI's `acquire_turn`, which
# `test_grok_inherits_openai_seam` pins.
PROVIDER_TURN_CLASSES = {
    "gemini": GeminiLiveTurn,
    "openai": OpenAIRealtimeTurn,
    "grok": OpenAIRealtimeTurn,
    "openai_live": OpenAILiveTurn,
}

TURN_CLASSES = (OpenAIRealtimeTurn, GeminiLiveTurn)


def _make_turn(cls):
    """Construct a turn adapter for shape and no-op behaviour checks.

    The seam methods are pure no-ops and never touch the connection, so a
    bare ``object()`` stand-in is sufficient. ``started_at`` is loop time
    (a float); 0.0 is fine for a turn we never drive."""
    return cls(conn=object(), started_at=0.0)


# ---------------------------------------------------------------------------
# Shape: every adapter conforms, and the catalog names no provider that
# doesn't.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", (*TURN_CLASSES, OpenAILiveTurn))
def test_turn_adapters_conform_to_the_protocols(cls):
    turn = _make_turn(cls)
    assert isinstance(turn, Interruptible)
    assert isinstance(turn, LiveTurn)


def test_fake_live_turn_conforms_to_the_protocol():
    """`FakeLiveTurn` (tests/_live_turn_fake.py) is hand-maintained rather
    than derived from `LiveTurn`, so a member added to the Protocol can
    leave the fake silently half-implemented — tests built on it would
    still pass, having exercised a shape the real seam no longer has. Pin
    conformance here so a Protocol change fails loudly instead."""
    from tests._live_turn_fake import FakeLiveTurn

    assert isinstance(FakeLiveTurn(), LiveTurn)


def test_every_provider_declaring_a_reconcile_kind_ships_an_interruptible_turn():
    """The catalog's `interrupt_reconcile` is a REQUIRED field, so declaring
    one is the same act as promising the seam. This pins that the two never
    drift: a fourth provider must appear in both places, and a turn class
    that drops part of `Interruptible` fails here rather than at the first
    barge-in."""
    assert set(PROVIDER_TURN_CLASSES) == {p.id for p in PROVIDERS}
    for provider_id, cls in PROVIDER_TURN_CLASSES.items():
        kind = resolve_interrupt_reconcile(provider_id)
        # Resolved, never the INHERITS placeholder.
        assert kind in (
            InterruptReconcile.NEEDS_CLIENT_TRUNCATE,
            InterruptReconcile.SERVER_SELF_TRUNCATES,
            InterruptReconcile.NATIVE_CONTINUOUS,
        )
        turn = _make_turn(cls)
        assert isinstance(turn, Interruptible), provider_id
        entry = next(p for p in PROVIDERS if p.id == provider_id)
        assert bool(getattr(turn, "continuous_input", False)) == entry.continuous_input
        # Only a provider that stops generating on the user's own voice is
        # exempt from the host's barge-in flush. Every other turn carries the
        # default, so a fifth adapter cannot inherit the exemption by accident.
        assert turn.owns_interruption is (provider_id == "openai_live")
        # No provider's follow-ups are proven on hardware yet, so every turn
        # closes when playout drains; Live runs its own window inside the turn.
        assert turn.host_followup_window is False, provider_id


# ---------------------------------------------------------------------------
# The cross-provider no-op paths are genuine no-ops.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", TURN_CLASSES)
async def test_cancel_response_is_noop(cls):
    turn = _make_turn(cls)
    assert await turn.cancel_response("local-barge-in") is None
    # Idempotent: a second call is still a clean no-op.
    assert await turn.cancel_response("again") is None


@pytest.mark.parametrize("cls", TURN_CLASSES)
async def test_truncate_tolerates_missing_item_id(cls):
    """Adapters MUST tolerate a missing provider_item_id (Gemini has none;
    OpenAI may not have observed one yet) — for any played-ms, with or
    without a ledger value, and never raise.

    A *populated* id is no longer a universal no-op: the OpenAI pack sends a
    real conversation.item.truncate for an owned item and valid played-ms. That
    provider-specific behaviour (and its ownership and cancel guards) is
    pinned in tests/test_openai_session.py; here we pin only the
    cross-provider tolerance of a *missing* id."""
    turn = _make_turn(cls)
    assert await turn.truncate_assistant_audio(None, 0) is None
    assert await turn.truncate_assistant_audio(None, 1500) is None


def test_grok_inherits_openai_seam():
    """Grok reuses the OpenAI adapter rather than reimplementing the seam.

    Same function objects ⇒ Grok's barge-in behaviour follows OpenAI's,
    which is exactly what its ``interrupt_reconcile = INHERITS`` declaration
    promises."""
    # Grok overrides neither acquire_turn (which constructs the turn) nor the
    # turn class itself, so it drives OpenAIRealtimeTurn verbatim and the
    # per-turn seam (cancel/truncate) is inherited unchanged. Same function
    # object ⇒ not overridden.
    assert (
        GrokRealtimeConnection.acquire_turn
        is OpenAIRealtimeConnection.acquire_turn
    )


@pytest.mark.parametrize("conn_cls", [OpenAIRealtimeConnection, GrokRealtimeConnection, GeminiLiveConnection])
@pytest.mark.parametrize("boundary,result_fails", [
    ("cancel", False), ("cancel", True), ("timeout", False), ("close", False), ("other_connection", False),
])
async def test_repeated_turns_bound_cancelled_tool_work_and_preserve_action_order(conn_cls, boundary, result_fails):
    gemini = conn_cls is GeminiLiveConnection
    factory = _FakeConnect() if gemini else _FakeConnectFactory()
    conn = conn_cls(api_key="fake", model="fake-model", connect_factory=factory, backoff_schedule=(0.0,))
    entered, completed = asyncio.Event(), asyncio.Event()
    finish_thread = threading.Event()
    loop = asyncio.get_running_loop()
    calls = []
    registry = ToolRegistry()
    connections = [conn]
    factories = [factory]

    def threaded_action():
        loop.call_soon_threadsafe(entered.set)
        assert finish_thread.wait(DEFAULT_SIGNAL_TIMEOUT_S)
        calls.append(0)
        loop.call_soon_threadsafe(completed.set)

    @tool(timeout=0.01 if boundary == "timeout" else DEFAULT_SIGNAL_TIMEOUT_S)
    async def first_action(index: int) -> dict:
        """Run the old action on a thread."""
        await asyncio.to_thread(threaded_action)
        return {"index": index}

    @tool()
    async def action(index: int) -> dict:
        """Record an ordered action."""
        calls.append(index)
        return {"index": index}

    async def submit(turn, indices):
        await turn.end_input()
        if gemini:
            factory.sessions[-1].feed(types.LiveServerMessage(tool_call=types.LiveServerToolCall(
                function_calls=[types.FunctionCall(id=f"call_{i}", name="first_action" if i == 0 else "action", args={"index": i}) for i in indices],
            )))
        else:
            await wait_until(lambda: turn._response_id is not None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
            factory.conns[-1]._inbox.put_nowait(ResponseDoneEvent.model_validate({
                "type": "response.done", "event_id": f"done_{indices[0]}", "response": {
                    "id": turn._response_id, "status": "completed", "output": [
                        {"type": "function_call", "call_id": f"call_{i}", "name": "first_action" if i == 0 else "action", "arguments": json.dumps({"index": i})}
                        for i in indices
                    ],
                },
            }))
        await wait_until(lambda: turn._tool_task is not None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)

    registry.register(action)
    registry.register(first_action)
    await conn.start(registry, "")
    try:
        old = await conn.acquire_turn()
        await submit(old, [0])
        await wait_signalled(entered, "first tool running")
        executor = registry._execution_task
        if boundary == "timeout":
            await wait_until(lambda: old._tool_task.done(), timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        await old.cancel_response("barge_in")
        await old.release()
        if boundary == "other_connection":
            factory = _FakeConnect() if gemini else _FakeConnectFactory()
            conn = conn_cls(api_key="fake", model="fake-model", connect_factory=factory, backoff_schedule=(0.0,))
            connections.append(conn)
            factories.append(factory)
            await conn.start(registry, "")
        for index in range(1, 5):
            turn = await conn.acquire_turn()
            await submit(turn, [index])
            for _ in range(3):
                await turn.cancel_response("barge_in")
            await turn.release()
            await wait_until(lambda: not conn._tool_tasks, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
            assert calls == []
            assert registry._execution_task is executor and not executor.done()

        latest = await conn.acquire_turn()
        if result_fails:
            if gemini:
                async def fail_result(**kwargs):
                    raise ConnectionError("result send failed")
                factory.sessions[-1].send_tool_response = fail_result
            else:
                send = factory.conns[-1].send

                async def fail_result(event):
                    if event.get("item", {}).get("type") == "function_call_output":
                        raise ConnectionError("result send failed")
                    await send(event)
                factory.conns[-1].send = fail_result
        await submit(latest, [5, 6])
        assert len(conn._tool_tasks) == 1
        assert not latest.server_turn_complete()
        if boundary == "close":
            await asyncio.wait_for(conn.stop(), DEFAULT_SIGNAL_TIMEOUT_S)
            assert calls == []
            assert registry._execution_task is not None
            finish_thread.set()
            await wait_signalled(completed, "thread completed after connection close")
            await wait_until(lambda: registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
            assert calls == [0]
            return
        finish_thread.set()
        await wait_signalled(completed, "old thread completed")
        await wait_until(lambda: not conn._tool_tasks, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert registry._execution_task is None
        assert calls == ([0, 5] if result_fails and not gemini else [0, 5, 6])
        assert not latest.server_turn_complete()
        assert latest.turn_lost() is result_fails
        if gemini:
            if boundary != "timeout":
                assert all(not session.sent_tool_responses for f in factories for session in f.sessions if session is not factory.sessions[-1])
            results = factory.sessions[-1].sent_tool_responses
            assert [[r.id for r in batch] for batch in results] == ([] if result_fails else [["call_5", "call_6"]])
        else:
            results = [
                e["item"]["call_id"] for f in factories for wire in f.conns for e in wire.sent
                if e.get("item", {}).get("type") == "function_call_output" and e["item"]["call_id"] != "call_0"
            ]
            assert results == ([] if result_fails else ["call_5", "call_6"])
            assert sum(e["type"] == "response.create" for e in factory.conns[-1].sent) == (1 if result_fails else 2)
    finally:
        finish_thread.set()
        await wait_signalled(completed, "release test thread")
        for connection in connections:
            await connection.stop()
        await wait_until(
            lambda: all(not connection._tool_tasks for connection in connections) and registry._execution_task is None,
            timeout=DEFAULT_SIGNAL_TIMEOUT_S,
        )
