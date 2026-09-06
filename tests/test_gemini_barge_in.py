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

Not duplicated here: Protocol conformance (``LiveTurn`` /
``Interruptible``) is pinned by ``tests/test_voice_barge_in_contract.py``;
the generic
"watchdog returns on ``server_turn_complete``" behaviour is pinned by
``tests/test_voice_daemon_defects.py``. The paid, on-device "speak over
Gemini TTS" proof is a SKIPPED voice-eval placeholder — see
``tests/voice_eval/regression/test_barge_in_gemini.py``.
"""
from __future__ import annotations

import asyncio

import pytest

from tests._gemini_fakes import Response as _Resp
from tests._gemini_fakes import ServerContent as _SC

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


def _turn(conn: "GeminiLiveConnection") -> "GeminiLiveTurn":
    return GeminiLiveTurn(
        conn, started_at=0.0, usage_baseline=conn._cumulative_usage,
    )


async def _interrupted(turn: "GeminiLiveTurn", *, timeout: float = 0.2) -> bool:
    """Probe the public seam only: True if ``wait_for_interrupt()``
    resolves within ``timeout`` (already armed — resolves near-instantly
    on an already-set event), False if it times out (not armed yet)."""
    try:
        await asyncio.wait_for(turn.wait_for_interrupt(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


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
# typed SDK fields (thinking_level / response_modalities /
# function_declarations) — the enum-member + model_validate rewrite must
# produce the identical wire payload as the raw string/dict forms it
# replaced.
# ---------------------------------------------------------------------------


def test_build_config_uses_typed_enums_and_validated_tool_declarations():
    """``_build_config`` passes ``ThinkingLevel``/``Modality`` enum members
    and ``FunctionDeclaration`` models (not raw strings/dicts). Pydantic
    coerces the old raw forms into the same models, so the assertions below
    prove the typed rewrite changes nothing on the wire."""
    from jasper.tools import ToolRegistry, tool

    @tool()
    def sample_tool() -> dict:
        """A sample tool for the structured config pin."""
        return {}

    registry = ToolRegistry()
    registry.register(sample_tool)
    decls = registry.function_declarations()

    conn = GeminiLiveConnection(api_key="fake", model="fake")
    conn._registry = registry
    cfg = conn._build_config()

    assert cfg.thinking_config.thinking_level == genai_types.ThinkingLevel.LOW
    assert cfg.response_modalities == [genai_types.Modality.AUDIO]
    assert cfg.tools[0].function_declarations[0].name == decls[0]["name"]

    # Wire-shape proof: the raw string/dict forms this replaced coerce,
    # via pydantic, into the identical serialized payload.
    legacy_thinking = genai_types.ThinkingConfig(thinking_level="low")
    assert (
        legacy_thinking.model_dump(exclude_none=True)
        == cfg.thinking_config.model_dump(exclude_none=True)
    )
    assert ["AUDIO"] == [m.value for m in cfg.response_modalities]
    legacy_tool = genai_types.Tool(function_declarations=decls)
    assert (
        legacy_tool.model_dump(exclude_none=True)
        == cfg.tools[0].model_dump(exclude_none=True)
    )


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

    assert await _interrupted(turn) is False
    turn.request_local_interrupt()
    # Event is set, so the playback path's interrupt race resolves at once.
    assert await _interrupted(turn) is True

    # Reconcile seam is a no-op even after a local interrupt (Gemini
    # self-truncates server-side; there is nothing to cancel/truncate).
    assert await turn.cancel_response("local-barge-in") is None
    assert await turn.truncate_assistant_audio(None, 1234) is None

    # The no-op reconcile must NOT have cleared the armed local interrupt.
    assert await _interrupted(turn) is True


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
    assert await _interrupted(turn) is True
    # Queued pre-interrupt audio dropped so it is NOT played post-barge.
    assert turn._audio_q.empty()
    # NOT complete yet: no generation_complete, no turn_complete.
    assert turn.server_turn_complete() is False

    # The trailing turn_complete is what actually completes the turn.
    await turn._on_response(_Resp(server_content=_SC(turn_complete=True)))
    assert turn.server_turn_complete() is True
