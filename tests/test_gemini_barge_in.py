# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Gemini barge-in pack (robust-barge-in PR-5).

Pins the Gemini half of the provider-pack barge-in seam. PR-2 (spine) and
PR-3 (capability seam) already landed the moving parts — the daemon's local
Silero gate, ``request_local_interrupt``, the ``server_content.interrupted``
parse, and the ``cancel_response`` / ``truncate_assistant_audio`` no-op
stubs. This file pins the *Gemini pack's* decision and contract on top:

  * point 3 — ``_build_config`` keeps manual VAD + NO_INTERRUPTION (option
    (a): the daemon's local gate is the single interruption authority, so
    the connection wire config is barge-in-agnostic and never flips to
    server VAD);
  * points 1+2 — the local gate sets the interrupt event, and the reconcile
    seam stays a no-op that never raises and never clears an armed local
    interrupt;
  * point 4 — an interrupted Gemini turn sends NO generation_complete; it
    goes ``interrupted`` -> ``turn_complete``, so the turn-end signal the
    watchdog consumes (``server_turn_complete()``) is set by ``turn_complete``
    alone.

Not duplicated here: ``supports_provider_vad()``/``supports_server_vad()``
values are pinned by ``tests/test_voice_barge_in_contract.py``; the generic
"watchdog returns on ``server_turn_complete``" behaviour is pinned by
``tests/test_voice_daemon_defects.py``. The paid, on-device "speak over
Gemini TTS" proof is a SKIPPED voice-eval placeholder — see
``tests/voice_eval/regression/test_barge_in_gemini.py``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

try:
    from google.genai import types as genai_types

    from jasper.voice.gemini_session import (
        GeminiLiveConnection,
        GeminiLiveTurn,
    )

    _HAVE_GENAI = True
except ImportError:
    _HAVE_GENAI = False

pytestmark = pytest.mark.skipif(
    not _HAVE_GENAI, reason="google-genai not installed in this environment"
)


@dataclass
class _SC:
    """Stand-in for ``response.server_content`` (getattr-only access)."""

    turn_complete: bool = False
    interrupted: bool = False


@dataclass
class _Resp:
    """Stand-in for the SDK's response objects. ``_on_response`` only uses
    ``getattr()`` so any object with the right attributes works."""

    data: bytes | None = None
    tool_call: Any = None
    server_content: _SC | None = None
    usage_metadata: Any = None


def _turn(conn: "GeminiLiveConnection") -> "GeminiLiveTurn":
    return GeminiLiveTurn(
        conn, started_at=0.0, usage_baseline=conn._cumulative_usage,
    )


# ---------------------------------------------------------------------------
# point 3 — the wire config is barge-in-agnostic (option a).
# ---------------------------------------------------------------------------


def test_build_config_keeps_manual_vad_and_no_interruption():
    """Option (a) pin: Gemini's ``_build_config`` always emits manual VAD
    (``automatic_activity_detection.disabled=True``) + ``NO_INTERRUPTION``.

    The connection deliberately never reads the ``JASPER_BARGE_IN_GEMINI``
    flag — the daemon's local gate owns barge-in, so the config is the same
    whether barge-in is on or off. This guards against a future regression
    that flips Gemini to server VAD "for barge-in" (option b), which would
    re-open the self-interrupt-on-bleed loop NO_INTERRUPTION prevents."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    ric = conn._build_config().realtime_input_config
    assert ric.automatic_activity_detection.disabled is True
    assert ric.activity_handling == genai_types.ActivityHandling.NO_INTERRUPTION


# ---------------------------------------------------------------------------
# points 1+2 — local gate sets the event; the reconcile seam is a no-op.
# ---------------------------------------------------------------------------


async def test_local_gate_sets_interrupt_event_and_seam_stays_noop():
    """The local gate (``request_local_interrupt``) sets the interrupt event
    so JTS flushes its own TTS regardless of the provider, and the Gemini
    reconcile seam is a no-op that never raises and — crucially — never
    clears an armed local interrupt (clearing is the daemon flush path's job
    via ``clear_interrupted``)."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = _turn(conn)

    assert turn.interrupted() is False
    turn.request_local_interrupt()
    assert turn.interrupted() is True
    # Event is set, so the playback path's interrupt race resolves at once.
    await asyncio.wait_for(turn.wait_for_interrupt(), timeout=1.0)

    # Reconcile seam is a no-op even after a local interrupt (Gemini
    # self-truncates server-side; there is nothing to cancel/truncate).
    assert await turn.cancel_response("local-barge-in") is None
    assert await turn.truncate_assistant_audio(None, 1234) is None

    # The no-op reconcile must NOT have cleared the armed local interrupt.
    assert turn.interrupted() is True


# ---------------------------------------------------------------------------
# point 4 — interrupted turn sends NO generation_complete.
# ---------------------------------------------------------------------------


async def test_server_interrupt_drops_queued_audio_and_does_not_complete():
    """``server_content.interrupted`` flushes queued pre-interrupt audio and
    arms the interrupt event, but does NOT mark the turn complete — there is
    no generation_complete, and ``interrupted`` alone must not look like
    "model done" to the watchdog. The trailing ``turn_complete`` is the sole
    end signal (Gemini goes interrupted -> turn_complete).

    This pins the server-reported-interrupt path (defensive/forward-
    compatible; under the production manual-VAD + NO_INTERRUPTION config the
    server does not self-interrupt — the local gate, test above, is the
    production driver)."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = _turn(conn)
    conn._active_turn = turn

    # Audio queues up for playback ahead of the barge-in point.
    await turn._on_response(_Resp(data=b"pre-1"))
    await turn._on_response(_Resp(data=b"pre-2"))
    assert turn._audio_q.qsize() == 2

    # Server reports interruption (no turn_complete in this message).
    await turn._on_response(_Resp(server_content=_SC(interrupted=True)))
    assert turn.interrupted() is True
    # Queued pre-interrupt audio dropped so it is NOT played post-barge.
    assert turn._audio_q.empty()
    # NOT complete yet: no generation_complete, no turn_complete.
    assert turn.server_turn_complete() is False

    # The trailing turn_complete is what actually completes the turn.
    await turn._on_response(_Resp(server_content=_SC(turn_complete=True)))
    assert turn.server_turn_complete() is True
