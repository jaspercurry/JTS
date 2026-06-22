# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Barge-in regression scenario — "interrupt mid-TTS (Gemini)".

ROBUST-BARGE-IN PR-5 (Gemini pack). This scenario is intentionally SKIPPED
and must NOT run as part of any automated suite. It is the named placeholder
from docs/HANDOFF-barge-in.md PR-5 ("interrupt mid-TTS" + on-device: speak
over Gemini TTS -> quiet < ~400 ms), recorded here so the paid / on-device
gap is VISIBLE and owned rather than silently missing.

Why it is skipped, not implemented
-----------------------------------
  * The voice-eval harness deliberately BYPASSES the wake loop and ALSA/dmix
    — it drives ONE prompt-response turn via ``harness.ask()`` and tests the
    LLM session, not the audio plumbing (see tests/voice_eval/README.md).
    Barge-in is exactly an audio-plumbing behaviour: the user talks OVER the
    assistant's TTS while it plays, the daemon's local Silero-on-AEC gate
    fires (``WakeLoop._handle_playback_frame`` -> ``request_local_interrupt``),
    and ``TtsPlayout.flush()`` cuts the audible output. None of that overlap
    is expressible through the single-turn text-ask harness, which has no
    "speak while the model is speaking" surface.

  * The real proof is ON-DEVICE and is owned by HANDOFF-barge-in.md blocker
    #4 / PR-7: on a Pi with an AEC-clean reference leg, enable
    ``JASPER_BARGE_IN_GEMINI=1``, speak over Gemini TTS, and confirm the
    speaker goes quiet in < ~400 ms AND does not self-interrupt on its own
    TTS bleed. That needs a microphone, the AEC bridge, and a human talker —
    none available to a hardware-free pytest run.

============================================================
COST NOTICE — read before ever un-skipping
============================================================
If a barge-in-capable harness extension (audio overlap + AEC-clean mic)
eventually lands, running this opens a PAID Gemini Live session:
~$0.025 / turn, ~$0.075 per pass^3 scenario. Announce the cost first and
NEVER loop / auto-rerun (tests/voice_eval/README.md cost rules).
============================================================

The hardware-free contract for the Gemini pack — the local gate sets the
interrupt event, the reconcile seam stays a no-op, and an interrupted turn
ends via ``turn_complete`` (no generation_complete) — IS pinned today, in
``tests/test_gemini_barge_in.py``.
"""
from __future__ import annotations

import pytest

# Belt-and-suspenders against accidental billing: this never collects into a
# runnable test. Un-skipping requires a barge-in-capable harness + explicit,
# cost-scoped human approval (see the COST NOTICE above).
pytestmark = pytest.mark.skip(
    reason=(
        "barge-in is audio-overlap behaviour the single-turn voice-eval "
        "harness cannot drive; the real proof is on-device + PAID "
        "(HANDOFF-barge-in.md PR-5/PR-7). The hardware-free Gemini-pack "
        "contract is pinned in tests/test_gemini_barge_in.py."
    ),
)

# pass^3 if/when a barge-in-capable harness exists; kept for shape parity
# with the other regression scenarios. Unused while skipped.
PASS_K = 3


async def test_interrupt_mid_tts_gemini(harness) -> None:
    """PLACEHOLDER (skipped). Intended on-device check: while Gemini TTS is
    playing, a user utterance over it flushes local TTS in < ~400 ms with no
    second wake word and without self-interrupting on bleed.

    Requires a barge-in-capable harness extension (audio overlap + AEC-clean
    mic) that does not exist hardware-free. See the module docstring for the
    cost + on-device ownership."""
    pytest.skip("on-device / paid; not expressible in the single-turn harness")
