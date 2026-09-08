# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Paid, read-only subway scenarios.

PASS_K = 3 turns per scenario, reported as separate trials. Announce the count
and estimated cost before running. Never loop or auto-retry.
Increase PASS_K only with explicit approval. See tests/voice_eval/README.md for evidence limits.
"""
from __future__ import annotations

import os

import pytest

from tests.voice_eval import oracles


PASS_K = 3


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_next_train_d_uptown(harness, trial: int) -> None:
    """Compare D-line tool arrivals with MTA and with the provider's spoken text.

    Use the household's configured station and direction for the oracle.
    """
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

    # Allow minute rounding to differ between separate tool and oracle fetches.
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
    # The tool caps arrivals across both directions; the oracle uses one.
    # Compare the common prefix because their result counts can differ.
    truth_sorted = sorted(truth)
    k = min(len(tool_mins), len(truth_sorted))
    assert harness.match_minutes(tool_mins[:k], truth_sorted[:k], tol=1), (
        f"[trial {trial}] tool D/{expected_dir} arrivals {tool_mins} "
        f"diverge from MTA {truth_sorted} on the soonest {k} — beyond "
        f"±1 min tolerance. See transcript: {result.transcript_path}"
    )

    # Spoken minutes must match the tool's already-rounded values exactly.
    result.require_spoken_text()
    spoken_mins = harness.extract_minutes_from_text(result.spoken_text)
    # This check assumes arrival minutes are the first numbers in the reply.
    spoken_relevant = spoken_mins[:len(tool_mins)]
    assert harness.match_minutes(spoken_relevant, tool_mins, tol=0), (
        f"[trial {trial}] tool returned {tool_mins} but model spoke "
        f"{spoken_relevant} (full extracted: {spoken_mins}). "
        f"Spoken text: {result.spoken_text!r}. "
        f"See transcript: {result.transcript_path}"
    )
