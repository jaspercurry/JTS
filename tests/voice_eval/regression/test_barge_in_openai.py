# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Paid interruption followed by a context check: two turns per trial.

Output acknowledgements are simulated. No sound leaves a speaker. Exact
cancel/truncate transport behavior is pinned by the offline replay lane.
Announce cost before running; never auto-retry.
"""
from __future__ import annotations

import pytest

from jasper.audio_io import confirmed_tts_flush
from jasper.voice.catalog import InterruptReconcile, resolve_interrupt_reconcile

PASS_K = 1


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_interrupt_mid_tts_reconciles_local_boundary(harness, trial: int) -> None:
    if resolve_interrupt_reconcile(harness.cfg.voice_provider) is not InterruptReconcile.NEEDS_CLIENT_TRUNCATE:
        pytest.skip("Provider has no client item-truncate API")
    ack = await harness.ask_with_barge_in(
        "Please count out loud slowly from one to thirty, saying each number on its own."
    )
    assert confirmed_tts_flush(ack)
    assert any(e["provider_item_id"] and e["drained_frames"] > 0 for e in ack["events"])
    follow_up = await harness.ask("What is the highest number you counted out loud?")
    numbers = harness.extract_minutes_from_text(follow_up.require_spoken_text())
    assert numbers, follow_up.transcript_path
    assert max(numbers) < 25, follow_up.transcript_path
