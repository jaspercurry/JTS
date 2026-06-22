# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Gemini barge-in pack (robust-barge-in PR-5).

Pins the Gemini half of the provider-pack barge-in seam. PR-2 (spine) and
PR-3 (capability seam) already landed the moving parts — the daemon's local
Silero gate, ``request_local_interrupt``, the ``server_content.interrupted``
parse, and the ``cancel_response`` / ``truncate_assistant_audio`` no-op
stubs. This file pins the *Gemini pack's* contract on top of them:

  * point 3 — ``_build_config`` keeps manual VAD + NO_INTERRUPTION even when
    ``JASPER_BARGE_IN_GEMINI=1`` (option (a): single interruption authority
    is the daemon's local gate; the wire config is barge-in-agnostic);
  * points 1+2 — the local gate sets the interrupt event, and the reconcile
    seam stays a genuine no-op that never raises and never clears the local
    interrupt;
  * point 4 — an interrupted Gemini turn sends NO generation_complete; it
    goes ``interrupted`` -> ``turn_complete``, and the turn-end path keys on
    ``server_turn_complete()`` (with timer fallbacks), so it cannot hang
    waiting for a signal Gemini never emits;
  * point 5 — ``supports_provider_vad()`` is True (capability) while the
    daemon still owns the flush, so it never decides barge-in behaviour.

The paid, on-device "speak over Gemini TTS" proof is a separate, SKIPPED
voice-eval placeholder — see
``tests/voice_eval/regression/test_barge_in_gemini.py``.
"""
from __future__ import annotations

import asyncio
import sys
import types as _types
from dataclasses import dataclass
from typing import Any

import pytest

# turn_playback -> audio_io -> sounddevice; stub it so the import resolves
# in a headless test env (mirrors tests/test_turn_playback_barge_in.py).
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = _types.ModuleType("sounddevice")

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
# point 3 (+5) — the wire config is barge-in-agnostic (option a).
# ---------------------------------------------------------------------------


def test_barge_in_flag_does_not_change_gemini_wire_config(tmp_path):
    """Option (a): even with ``JASPER_BARGE_IN_GEMINI=1`` readable as ON, the
    Gemini connection keeps manual VAD (``disabled=True``) + NO_INTERRUPTION.

    The flag is owned by the daemon's local gate; the connection never reads
    it, so the flag-OFF and flag-ON wire configs are identical (single
    interruption authority, no server-side double-VAD). This is the guard
    against a future change that "enables server VAD for barge-in" (option b)
    and silently re-opens the self-interrupt-on-bleed loop."""
    from jasper.voice.provider_state import read_barge_in_enabled

    env = tmp_path / "voice_provider.env"
    env.write_text("JASPER_VOICE_PROVIDER=gemini\nJASPER_BARGE_IN_GEMINI=1\n")
    assert read_barge_in_enabled("gemini", str(env)) is True

    conn = GeminiLiveConnection(api_key="fake", model="fake")
    ric = conn._build_config().realtime_input_config
    assert ric.automatic_activity_detection.disabled is True
    assert ric.activity_handling == genai_types.ActivityHandling.NO_INTERRUPTION

    # point 5 — provider VAD is a capability (Gemini has native VAD), but it
    # is a DIFFERENT axis from barge-in: the daemon still flushes local TTS,
    # so this never decides barge-in behaviour. supports_server_vad stays
    # False (Gemini can't switch endpointing mid-session).
    assert conn.supports_provider_vad() is True
    assert conn.supports_server_vad() is False


# ---------------------------------------------------------------------------
# points 1+2 — local gate sets the event; the reconcile seam is a no-op.
# ---------------------------------------------------------------------------


async def test_local_gate_sets_interrupt_event_and_seam_stays_noop():
    """The local gate (``request_local_interrupt``) sets the interrupt event
    so JTS flushes its own TTS regardless of the provider, and the Gemini
    reconcile seam is a genuine no-op that never raises and — crucially —
    never clears the local interrupt (clearing is the daemon flush path's
    job via ``clear_interrupted``)."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = _turn(conn)

    assert turn.interrupted() is False
    turn.request_local_interrupt()
    assert turn.interrupted() is True
    # Event is set, so the playback path's race resolves immediately.
    await asyncio.wait_for(turn.wait_for_interrupt(), timeout=1.0)

    # Reconcile seam: no-op, idempotent, tolerant of a missing item id
    # (Gemini has none), and never raises — even after a local interrupt.
    assert await turn.cancel_response("local-barge-in") is None
    assert await turn.truncate_assistant_audio(None, 1234) is None
    assert await turn.truncate_assistant_audio("item_abc", 0) is None

    # The no-op reconcile must NOT have touched the local interrupt state.
    assert turn.interrupted() is True


# ---------------------------------------------------------------------------
# point 4 — interrupted turn sends NO generation_complete.
# ---------------------------------------------------------------------------


async def test_server_interrupt_drops_queued_audio_and_does_not_complete():
    """``server_content.interrupted`` flushes queued pre-interrupt audio and
    arms the interrupt event, but does NOT mark the turn complete — there is
    no generation_complete, and ``interrupted`` alone must not look like
    "model done" to the watchdog. The trailing ``turn_complete`` is the sole
    end signal (interrupted -> turn_complete)."""
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


async def test_idle_watchdog_ends_on_turn_complete_without_generation_complete():
    """The interrupted-without-generation_complete turn-end path.

    A Gemini turn signals completion only via ``server_turn_complete()`` (set
    by ``turn_complete``, which follows ``interrupted``). The daemon's
    ``_idle_watchdog`` ends the turn on that signal alone — it never waits on
    an OpenAI-style ``generation_complete`` / ``response.done``, so an
    interrupted Gemini turn cannot hang the watchdog."""
    from jasper.voice.turn_playback import _idle_watchdog

    class _CompletedTurn:
        def turn_lost(self) -> bool:
            return False

        def last_activity_at(self) -> float:
            return 0.0

        def last_chunk_at(self) -> float:
            return 0.0

        def server_turn_complete(self) -> bool:
            return True

        def audio_chunks_pending(self) -> int:
            return 0

    class _DrainedTts:
        def expected_drain_at(self) -> float:
            return 0.0

    # Returns promptly (it must not block on a signal Gemini never sends).
    await asyncio.wait_for(
        _idle_watchdog(
            _CompletedTurn(),
            _DrainedTts(),
            timeout=20.0,
            response_stall_timeout=8.0,
        ),
        timeout=2.0,
    )
