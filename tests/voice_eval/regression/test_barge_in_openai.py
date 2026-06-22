# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Barge-in regression scenario — interrupt mid-TTS (OpenAI reference pack).

Pins the OpenAI barge-in pack (PR-4 of docs/HANDOFF-barge-in.md) end-to-end
against a live session: the user barges in while the assistant is speaking,
JTS cancels the in-progress response and truncates the conversation item to
the *heard* boundary, and the NEXT turn's context reflects only what was
actually said — not the unspoken tail the server still generated.

Two things are checked:

  1. Mechanism — ``event=barge.truncate`` is emitted with an ``audio_end_ms``
     equal to the played-ms handed to the seam (the heard boundary). This is
     the deterministic assertion.
  2. Context alignment — a follow-up turn asking what was said does NOT claim
     the full pre-barge answer. This is the *point* of truncate; it is fuzzier
     (model phrasing varies), so the bound is generous and the transcript is
     the source of truth on a failure.

Only ``needs_client_truncate`` providers (OpenAI; Grok by inheritance) do a
client truncate — the scenario skips on Gemini (it self-truncates server-side,
covered by its own scenario in PR-5).

============================================================
COST + SIDE-EFFECT NOTICE — read carefully before running
============================================================
PAID. Each trial runs TWO live turns (the barge-in turn + the follow-up),
so it is pricier per trial than a one-turn scenario. ``PASS_K = 1`` on
purpose: barge-in is timing-sensitive (we race the first audio chunk) and
expensive, so the default is a single trial. Do NOT bump ``PASS_K`` or loop
without explicit human approval and a stated dollar ceiling — re-running a
bad trial costs money and tells you nothing the transcript doesn't.

Read-only otherwise: no volume / playback / persisted state changes (the
harness bypasses ALSA, so no audio actually leaves a speaker). NOT gated on
``JASPER_VOICE_EVAL_SKIP_PLAYBACK``.

Approx OpenAI cost: ~$0.20/turn → ~$0.40 per trial (2 turns) at PASS_K=1.
============================================================
"""
from __future__ import annotations

import logging
import re

import pytest

from jasper.voice.catalog import InterruptReconcile, resolve_interrupt_reconcile


PASS_K = 1

_OPENAI_LOGGER = "jasper.voice.openai_session"
_AUDIO_END_MS_RE = re.compile(r"audio_end_ms=(\d+)")


def _skip_unless_client_truncate(harness) -> None:
    """Skip unless the active provider does a CLIENT truncate (OpenAI, or
    Grok by inheritance). Gemini self-truncates server-side and has no
    client truncate to assert — that path is PR-5's scenario."""
    provider = harness.cfg.voice_provider
    # The harness only opens against a valid configured provider (conftest
    # skips otherwise), so resolve_interrupt_reconcile won't raise here; a
    # KeyError would correctly surface a genuinely broken provider id.
    kind = resolve_interrupt_reconcile(provider)
    if kind is not InterruptReconcile.NEEDS_CLIENT_TRUNCATE:
        pytest.skip(
            f"voice-eval: barge-in client-truncate scenario only applies to "
            f"needs_client_truncate providers; active provider {provider!r} "
            f"is {kind.value}",
        )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_interrupt_mid_tts_truncates_to_heard_boundary(
    harness, caplog, trial: int,
) -> None:
    """Barge in after the first audio chunk of a long answer; assert the
    pack truncated to the played-ms boundary, then that the follow-up turn's
    context does not reference the unspoken tail."""
    _skip_unless_client_truncate(harness)

    # A long, easily-verified answer so a mid-TTS barge-in clearly lands
    # before the model finishes: counting leaves an unambiguous "how far
    # did you get" follow-up.
    prompt = (
        "Please count out loud slowly from one to thirty, "
        "saying each number on its own."
    )

    with caplog.at_level(logging.INFO, logger=_OPENAI_LOGGER):
        played_ms = await harness.ask_with_barge_in(prompt)

    # 1. Mechanism — a real chunk played, then truncate fired at exactly
    # that played-ms boundary. played_ms==0 would mean no audio arrived
    # before the barge-in (the pack would correctly no-op + WARN), which
    # means the scenario didn't exercise the truncate path — fail loudly so
    # it isn't mistaken for a pass.
    assert played_ms > 0, (
        f"[trial {trial}] no audio chunk arrived before barge-in "
        f"(played_ms={played_ms}); the truncate path was not exercised. "
        f"Re-check the provider streamed audio at all."
    )
    truncate_lines = [
        r.getMessage() for r in caplog.records
        if "barge.truncate" in r.getMessage()
        and "barge.truncate_skipped" not in r.getMessage()
        and "barge.truncate_failed" not in r.getMessage()
    ]
    assert truncate_lines, (
        f"[trial {trial}] no event=barge.truncate emitted — the pack did not "
        f"send conversation.item.truncate after the barge-in. "
        f"Records: {[r.getMessage() for r in caplog.records]}"
    )
    logged_ms = [
        int(m.group(1))
        for line in truncate_lines
        for m in [_AUDIO_END_MS_RE.search(line)]
        if m is not None
    ]
    assert played_ms in logged_ms, (
        f"[trial {trial}] barge.truncate audio_end_ms {logged_ms} did not "
        f"match the played-ms boundary {played_ms}. audio_end_ms MUST be the "
        f"ms actually rendered, never bytes-received."
    )

    # 2. Context alignment — the follow-up must not claim the full count.
    # After a barge-in one chunk in, only a few numbers were heard; truncate
    # deletes the rest from history. If truncate were broken, the server
    # would still hold the whole generated count and the model would answer
    # with a high number. The bound is generous (timing varies) — the
    # transcript is authoritative on a borderline failure.
    follow_up = await harness.ask("What is the highest number you counted out loud?")
    spoken = follow_up.spoken_text or ""
    numbers = harness.extract_minutes_from_text(spoken)
    if numbers:
        assert max(numbers) < 25, (
            f"[trial {trial}] follow-up says it counted to {max(numbers)} "
            f"after being interrupted near the start — history was NOT "
            f"truncated to the heard boundary (the model still 'remembers' "
            f"the unspoken tail). Spoken: {spoken!r}. "
            f"See transcript: {follow_up.transcript_path}"
        )
