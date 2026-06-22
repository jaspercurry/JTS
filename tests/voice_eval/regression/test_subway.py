# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Subway regression scenarios — re-diagnoses the "Jarvis hallucinates
train times" bug and locks the fix once it lands.

Each scenario is parametrized over `trial` to express pass^k
semantics (per Anthropic's eval methodology): for regression tests
where consistency is required, the test passes only if ALL trials
pass. Pytest reports each trial separately so flakiness is visible
as a per-trial fail rate rather than a hidden flake.

============================================================
COST NOTICE — read tests/voice_eval/harness.py top docstring
============================================================
This file invokes paid LLM APIs. Each `harness.ask()` is one
real turn against the active voice provider. PASS_K = 3 means
3 turns per scenario function. **DO NOT increase PASS_K or
wrap in a loop without explicit human approval.** The subway
scenarios are read-only (no playback side-effects), but the
LLM cost still applies.
============================================================
"""
from __future__ import annotations

import os

import pytest

from tests.voice_eval import oracles


# Run each scenario 3× (pass^3). For a regression test where
# consistency matters, all three must pass — pytest fails the overall
# scenario if any trial fails. Bump or reduce via the parametrize.
PASS_K = 3


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_next_train_d_uptown(harness, trial: int) -> None:
    """Asks 'when's the next train?' — at the speaker's home station
    (9 Av on the D) with home direction uptown, the model should
    call `get_subway_arrivals` with empty args (defaults fill in
    station + direction) and speak the times the tool returned.

    Three assertions, in increasing strictness:

      1. Trajectory: did the model call the tool?
         (catches "model hallucinated times from training-data
         knowledge without consulting a tool")
      2. Outcome: did the tool's data match independent MTA reality?
         (catches "tool's API client is broken / direction routing
         is wrong / station ID is wrong")
      3. Spoken minutes match tool minutes
         (catches "tool returned [6, 22, 36] but model said
         '4, 12, 19'" — pure model-side hallucination, the loudest
         failure mode and the whole reason the harness exists)

    The model's spoken text comes from the provider's native
    transcript stream — no STT pass needed."""
    if not os.environ.get("JASPER_SUBWAY_STATION_ID", "").strip():
        pytest.skip(
            "voice-eval: subway not configured "
            "(JASPER_SUBWAY_STATION_ID empty) — set it to run this scenario",
        )

    result = await harness.ask("when's the next train?")

    # 1. Trajectory — the model must call the subway tool.
    call = result.tool_call("get_subway_arrivals")
    assert call is not None, (
        f"[trial {trial}] model did not call get_subway_arrivals. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )

    # 2. Outcome — what the tool returned must match independent MTA
    # ground truth within tolerance. Tolerance is ±1 min to absorb
    # the ~50ms gap between the tool's MTA fetch and the oracle's.
    expected_dir = (
        "N" if os.environ.get("JASPER_SUBWAY_DEFAULT_DIRECTION", "").lower()
        in {"uptown", "north", "northbound", "n", "manhattan"} else "S"
    )
    truth = await oracles.subway_arrivals(
        station=os.environ.get("JASPER_SUBWAY_STATION_ID", ""),
        line="D",
        direction=expected_dir,
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )
    # get_subway_arrivals returns a structured `arrivals` list
    # ([{line, direction, direction_label, minutes_from_now}, …]),
    # NOT a flat `next_arrivals_minutes`. Pull the D-line minutes for
    # the direction the oracle queried.
    tool_mins = sorted(
        a["minutes_from_now"]
        for a in (call.result or {}).get("arrivals", [])
        if a.get("line") == "D" and a.get("direction") == expected_dir
    )
    assert tool_mins, (
        f"[trial {trial}] tool returned no D/{expected_dir} arrivals. "
        f"Result keys: {list((call.result or {}).keys())!r}. "
        f"See transcript: {result.transcript_path}"
    )
    if not truth:
        pytest.skip(
            f"[trial {trial}] subway oracle returned no arrivals "
            f"(transient) — nothing to compare against.",
        )
    # The tool caps total arrivals across BOTH directions while the
    # oracle returns the next few for ONE direction, so the list
    # lengths legitimately differ. Compare the soonest `k` trains both
    # report (k = the shorter length) — that's the "next train(s)" the
    # question asks about and is stable across the ~50 ms gap between
    # the tool's fetch and the oracle's. `minutes_match` requires equal
    # length, so slice both to k first.
    truth_sorted = sorted(truth)
    k = min(len(tool_mins), len(truth_sorted))
    assert harness.match_minutes(tool_mins[:k], truth_sorted[:k], tol=1), (
        f"[trial {trial}] tool D/{expected_dir} arrivals {tool_mins} "
        f"diverge from MTA {truth_sorted} on the soonest {k} — beyond "
        f"±1 min tolerance. See transcript: {result.transcript_path}"
    )

    # 3. Reality (spoken) — the model's spoken minutes match the tool's
    # return. If the model said "next train in 8" but the tool returned
    # 6, that's a pure hallucination — the model ignored the data the
    # tool provided. tol=0 because the model should speak exactly what
    # the tool returned (rounded to the same minute we returned).
    if not result.spoken_text:
        # Provider didn't ship transcript deltas this turn. Surface
        # but don't fail — listening to the WAV is still possible.
        pytest.skip(
            f"[trial {trial}] no spoken-text transcript captured "
            f"(provider's text channel may be off). Listen to "
            f"{result.response_audio_path} to verify by ear."
        )
    spoken_mins = harness.extract_minutes_from_text(result.spoken_text)
    # The spoken text often includes extra numbers ("D train", times
    # like "5 minutes" — but we want the FIRST `len(tool_mins)`
    # numbers that appear in the response. This catches the common
    # case where the model says "Next D trains in X, Y, and Z minutes."
    spoken_relevant = spoken_mins[:len(tool_mins)]
    assert harness.match_minutes(spoken_relevant, tool_mins, tol=0), (
        f"[trial {trial}] tool returned {tool_mins} but model spoke "
        f"{spoken_relevant} (full extracted: {spoken_mins}). "
        f"Spoken text: {result.spoken_text!r}. "
        f"See transcript: {result.transcript_path}"
    )
