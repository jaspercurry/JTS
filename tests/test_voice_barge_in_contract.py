"""Conformance + tolerance contract for the voice turn adapters.

Two halves:

  * **Shape** — every shipped turn adapter satisfies `LiveTurn` and its
    `Interruptible` half, and the set of Interruptible adapters covers
    exactly the providers `jasper/voice/catalog.py` declares an
    `interrupt_reconcile` kind for. A provider that declares a
    reconciliation kind but ships a turn missing part of the seam is the
    failure this catches.
  * **Tolerance** — the cross-provider no-op paths (a missing
    `provider_item_id`, no active response) are clean no-ops on every
    adapter, never a raise. Provider-specific *live* behaviour
    (`response.cancel` + `conversation.item.truncate` for a real id and real
    played-ms) is pinned in `tests/test_openai_session.py`; Gemini stays a
    genuine no-op on every path because it self-truncates server-side.

See ADR-0115.
"""
from __future__ import annotations

import pytest

from jasper.voice.catalog import (
    PROVIDERS,
    InterruptReconcile,
    resolve_interrupt_reconcile,
)
from jasper.voice.gemini_session import GeminiLiveTurn
from jasper.voice.grok_session import GrokRealtimeConnection
from jasper.voice.openai_session import (
    OpenAIRealtimeConnection,
    OpenAIRealtimeTurn,
)
from jasper.voice.session import Interruptible, LiveTurn


# The turn class each catalog provider drives. Grok defines no turn class of
# its own — `GrokRealtimeConnection` inherits OpenAI's `acquire_turn`, which
# `test_grok_inherits_openai_seam` pins.
PROVIDER_TURN_CLASSES = {
    "gemini": GeminiLiveTurn,
    "openai": OpenAIRealtimeTurn,
    "grok": OpenAIRealtimeTurn,
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


@pytest.mark.parametrize("cls", TURN_CLASSES)
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
        )
        assert isinstance(_make_turn(cls), Interruptible), provider_id


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
    real conversation.item.truncate for a real id + positive played-ms. That
    provider-specific behaviour (and its no-op-if-0 and cancel guards) is
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
