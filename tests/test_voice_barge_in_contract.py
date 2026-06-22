"""Contract tests for the barge-in / provider-pack capability seam.

PR-3 of the robust-barge-in plan adds three capability-based methods to the
voice provider interface, with behaviour-neutral no-op defaults so this PR
changes no runtime behaviour:

  * ``LiveTurn.cancel_response(reason)`` — explicit local/manual cancel.
  * ``LiveTurn.truncate_assistant_audio(provider_item_id, audio_played_ms)``
    — align provider history to what the listener actually heard; MUST
    tolerate a missing ``provider_item_id``.
  * ``LiveConnection.supports_provider_vad()`` — native-VAD capability,
    distinct from barge-in support and from ``supports_server_vad()``.

These tests pin that all three shipped adapters (Gemini, OpenAI, and Grok via
its OpenAI subclass) expose the seam and that the cross-provider *tolerance*
contract holds (a missing item id / no active response is always a clean
no-op, never a raise).

Note: PR-4 made the OpenAI/Grok pack's seam *live* — `cancel_response` and
`truncate_assistant_audio` now issue real `response.cancel` /
`conversation.item.truncate` for an active response + real played-ms. That
provider-specific behaviour (and its guards) is pinned in
`tests/test_openai_session.py`; here we pin only the seam shape and the
no-op/tolerance paths that remain identical across providers (Gemini stays a
genuine no-op on every path — it self-truncates server-side).

Note on conformance checking: ``LiveTurn`` / ``LiveConnection`` are
``@runtime_checkable`` but also declare *optional* server-VAD members that the
Gemini adapter deliberately omits (it probes them with ``getattr``). So a bare
``isinstance(turn, LiveTurn)`` is already ``False`` for Gemini and is NOT a
clean full-conformance gate. We therefore assert the specific capability-seam
members directly — that is exactly the contract PR-3 is responsible for.
"""
from __future__ import annotations

import inspect

import pytest

from jasper.voice.session import LiveConnection, LiveTurn
from jasper.voice.gemini_session import GeminiLiveConnection, GeminiLiveTurn
from jasper.voice.grok_session import GrokRealtimeConnection
from jasper.voice.openai_session import (
    OpenAIRealtimeConnection,
    OpenAIRealtimeTurn,
)


# The two turn adapter classes. Grok reuses ``OpenAIRealtimeTurn`` verbatim
# (grok_session.py defines no turn class), so the per-turn seam is covered by
# the OpenAI row; ``test_grok_inherits_openai_seam`` pins that reuse.
TURN_CLASSES = (OpenAIRealtimeTurn, GeminiLiveTurn)

# All three connection adapter classes. Grok subclasses the OpenAI connection.
CONNECTION_CLASSES = (
    OpenAIRealtimeConnection,
    GrokRealtimeConnection,
    GeminiLiveConnection,
)

# Members the barge-in seam adds, plus the daemon-facing interrupt EVENT
# methods PR-3 must keep on the turn.
TURN_SEAM_METHODS = ("cancel_response", "truncate_assistant_audio")
# drop_pending_audio (A1) is part of the seam but OPTIONAL: the spine
# getattr-probes it, so only providers with an internal playout buffer
# implement it. Both of JTS's turn adapters are queue-backed, so both expose
# it; an adapter that streams without a buffer may omit it.
TURN_OPTIONAL_SEAM_METHODS = ("drop_pending_audio",)
TURN_EVENT_METHODS = ("interrupted", "wait_for_interrupt", "clear_interrupted")
CONNECTION_SEAM_METHODS = ("supports_provider_vad",)


def _make_turn(cls):
    """Construct a turn adapter for no-op behaviour checks.

    The seam methods are pure no-ops and never touch the connection, so a
    bare ``object()`` stand-in is sufficient. ``started_at`` is loop time
    (a float); 0.0 is fine for a turn we never drive."""
    return cls(conn=object(), started_at=0.0)


def _make_connection(cls):
    """Construct a connection adapter without opening the network.

    ``__init__`` builds only local state (state machine, asyncio
    primitives, lazy SDK client); it does not connect. Gemini requires an
    explicit ``model``."""
    if cls is GeminiLiveConnection:
        return cls(api_key="test-key", model="gemini-test")
    return cls(api_key="test-key")


# ---------------------------------------------------------------------------
# The Protocol itself declares the seam.
# ---------------------------------------------------------------------------


def test_protocol_declares_capability_seam():
    for name in TURN_SEAM_METHODS + TURN_EVENT_METHODS:
        assert hasattr(LiveTurn, name), f"LiveTurn missing {name}"
    for name in CONNECTION_SEAM_METHODS + ("supports_server_vad",):
        assert hasattr(LiveConnection, name), f"LiveConnection missing {name}"


def test_seam_method_signatures_match_protocol():
    # cancel_response(reason)
    assert list(inspect.signature(LiveTurn.cancel_response).parameters) == [
        "self",
        "reason",
    ]
    # truncate_assistant_audio(provider_item_id, audio_played_ms)
    assert list(
        inspect.signature(LiveTurn.truncate_assistant_audio).parameters
    ) == ["self", "provider_item_id", "audio_played_ms"]


# ---------------------------------------------------------------------------
# Every adapter exposes the seam.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", TURN_CLASSES)
def test_turn_adapters_expose_seam(cls):
    for name in TURN_SEAM_METHODS + TURN_EVENT_METHODS:
        assert callable(getattr(cls, name, None)), f"{cls.__name__} missing {name}"
    # The seam actions are coroutines; the daemon awaits them.
    assert inspect.iscoroutinefunction(cls.cancel_response)
    assert inspect.iscoroutinefunction(cls.truncate_assistant_audio)


@pytest.mark.parametrize("cls", TURN_CLASSES)
def test_buffered_turn_adapters_drain_pending_audio(cls):
    """drop_pending_audio (A1) stops post-flush replay on burst-delivery
    providers. Both queue-backed adapters expose it and return the dropped
    count (0 on an empty queue), and it is declared on the Protocol. It is
    synchronous (the spine calls it inline after the flush, not awaited)."""
    for name in TURN_OPTIONAL_SEAM_METHODS:
        assert hasattr(LiveTurn, name), f"LiveTurn missing {name}"
    turn = _make_turn(cls)
    assert not inspect.iscoroutinefunction(cls.drop_pending_audio)
    result = turn.drop_pending_audio()
    assert isinstance(result, int) and result == 0  # empty queue → nothing


@pytest.mark.parametrize("cls", CONNECTION_CLASSES)
def test_connection_adapters_expose_seam(cls):
    for name in CONNECTION_SEAM_METHODS:
        assert callable(getattr(cls, name, None)), f"{cls.__name__} missing {name}"


# ---------------------------------------------------------------------------
# The defaults are genuine no-ops (this PR is behaviour-neutral).
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

    A *populated* id is no longer a universal no-op: post-PR-4 the OpenAI
    pack sends a real conversation.item.truncate for a real id + positive
    played-ms. That provider-specific behaviour (and its no-op-if-0 and
    cancel guards) is pinned in tests/test_openai_session.py; here we pin
    only the cross-provider tolerance of a *missing* id."""
    turn = _make_turn(cls)
    assert await turn.truncate_assistant_audio(None, 0) is None
    assert await turn.truncate_assistant_audio(None, 1500) is None


@pytest.mark.parametrize("cls", CONNECTION_CLASSES)
def test_supports_provider_vad_returns_bool(cls):
    conn = _make_connection(cls)
    result = conn.supports_provider_vad()
    assert isinstance(result, bool)


def test_supports_provider_vad_is_separate_from_server_vad():
    """The new capability is a distinct axis from supports_server_vad():
    Gemini has native VAD (True here) but cannot switch endpointing
    mid-session (False there)."""
    gemini = _make_connection(GeminiLiveConnection)
    assert gemini.supports_provider_vad() is True
    assert gemini.supports_server_vad() is False

    openai = _make_connection(OpenAIRealtimeConnection)
    assert openai.supports_provider_vad() is True


def test_grok_inherits_openai_seam():
    """Grok reuses the OpenAI adapter rather than reimplementing the seam.

    Same function objects ⇒ Grok's barge-in behaviour follows OpenAI's,
    which is exactly what its ``interrupt_reconcile = INHERITS`` declaration
    promises."""
    # Connection-level capability is the inherited OpenAI method.
    assert (
        GrokRealtimeConnection.supports_provider_vad
        is OpenAIRealtimeConnection.supports_provider_vad
    )
    # Grok overrides neither acquire_turn (which constructs the turn) nor the
    # turn class itself, so it drives OpenAIRealtimeTurn verbatim and the
    # per-turn seam (cancel/truncate) is inherited unchanged. Same function
    # object ⇒ not overridden.
    assert (
        GrokRealtimeConnection.acquire_turn
        is OpenAIRealtimeConnection.acquire_turn
    )
