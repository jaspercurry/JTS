# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Citi Bike regression scenarios.

Same three-assertion shape as the other transit scenarios:
  1. Trajectory: did the model call get_citibike_status?
  2. Outcome: did the tool return a non-empty stations list?
  3. Reality: do the tool's station_ids and counts match a direct
     GBFS oracle within tolerance? Counts swing minute-to-minute,
     so the test asserts the same station_ids appear and bike
     counts are within +/-3 of the oracle (a much looser tolerance
     than bus's route-order match — bikes can be taken / returned
     between the model's fetch and the oracle's fetch in a way
     buses can't be re-dispatched).

Read-only — no playback side-effects.

============================================================
COST NOTICE — read tests/voice_eval/harness.py top docstring
============================================================
Paid LLM API calls per turn. Read-only scenarios but the LLM
cost still applies. PASS_K = 3 turns per scenario function.
DO NOT loop or increase PASS_K without explicit human approval.
============================================================
"""
from __future__ import annotations

import os

import pytest

from jasper.citibike import parse_saved_stations
from tests.voice_eval import oracles


PASS_K = 3


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_citibike_general_situation(harness, trial: int) -> None:
    """Asks 'what's the Citi Bike situation?' — the model should call
    `get_citibike_status` with an empty station_label, get back
    every saved station, and speak the counts.

    Three assertions:
      1. Trajectory — model called get_citibike_status with an
         empty (or no) `station_label` (cross-tool calls disqualify).
      2. Outcome — tool returned at least one station whose status
         is "ok" or "offline" (anything other than all "missing").
      3. Reality — every station_id the tool returned matches a saved
         station, and per-station bike/ebike counts are within +/-3
         of a direct GBFS oracle (counts move minute-to-minute)."""
    if not parse_saved_stations(os.environ.get("JASPER_CITIBIKE_STATIONS", "")):
        pytest.skip(
            "voice-eval: Citi Bike not configured "
            "(JASPER_CITIBIKE_STATIONS required) — set it to run this scenario",
        )

    result = await harness.ask("what's the Citi Bike situation?")

    # 1. Trajectory
    call = result.tool_call("get_citibike_status")
    assert call is not None, (
        f"[trial {trial}] model did not call get_citibike_status. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )
    # General query should pass empty or no filter — the model is
    # explicitly told to do this for "what's the situation" prompts.
    passed_filter = (call.args or {}).get("station_label", "")
    assert not passed_filter.strip(), (
        f"[trial {trial}] expected empty station_label for general "
        f"query, got {passed_filter!r}. See {result.transcript_path}"
    )

    # 2. Outcome
    tool_stations = (call.result or {}).get("stations") or []
    assert tool_stations, (
        f"[trial {trial}] tool returned no stations while "
        f"{len(parse_saved_stations(os.environ.get('JASPER_CITIBIKE_STATIONS', '')))} are configured. "
        f"See transcript: {result.transcript_path}"
    )
    statuses = {s.get("status") for s in tool_stations}
    assert statuses & {"ok", "offline"}, (
        f"[trial {trial}] every saved station came back missing — "
        f"saved IDs may all be stale. See {result.transcript_path}"
    )

    # 3. Reality — compare against a direct GBFS oracle.
    truth = await oracles.citibike_status(list(parse_saved_stations(os.environ.get("JASPER_CITIBIKE_STATIONS", ""))))
    if truth is None:
        pytest.skip(
            f"[trial {trial}] GBFS oracle unreachable; can't validate. "
            f"Re-run when gbfs.citibikenyc.com is responding."
        )

    # All station_ids in the tool result must be saved stations.
    saved_ids = {sid for sid, _ in parse_saved_stations(os.environ.get("JASPER_CITIBIKE_STATIONS", ""))}
    tool_ids = {s.get("station_id") for s in tool_stations}
    assert tool_ids <= saved_ids, (
        f"[trial {trial}] tool returned unknown station_ids "
        f"{tool_ids - saved_ids}. See {result.transcript_path}"
    )

    # Per-station bike/ebike counts within +/-3 of the oracle. Wider
    # tolerance than bus's route match because individual bikes get
    # taken/returned between fetches; we just want to catch wholesale
    # divergence (e.g. tool reports 50 when oracle shows 5).
    for s in tool_stations:
        sid = s.get("station_id")
        live = truth.get(sid)
        if not live or live.get("missing"):
            continue
        if s.get("status") != "ok":
            continue
        tool_ebikes = int(s.get("ebikes", 0))
        tool_classic = int(s.get("classic_bikes", 0))
        oracle_ebikes = live["ebikes"]
        oracle_classic = live["classic_bikes"]
        assert abs(tool_ebikes - oracle_ebikes) <= 3, (
            f"[trial {trial}] {sid}: tool ebikes={tool_ebikes}, "
            f"oracle={oracle_ebikes}. See {result.transcript_path}"
        )
        assert abs(tool_classic - oracle_classic) <= 3, (
            f"[trial {trial}] {sid}: tool classic={tool_classic}, "
            f"oracle={oracle_classic}. See {result.transcript_path}"
        )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_citibike_station_specific(harness, trial: int) -> None:
    """Asks 'what's the deal with <station> Citi Bike?' — the model
    should call `get_citibike_status` with a station_label substring
    matching the saved label, and the response should narrow to that
    one station.

    Three assertions:
      1. Trajectory — model called get_citibike_status with a non-
         empty station_label that's a substring of a saved label.
      2. Outcome — tool returned exactly one station (the filter
         narrowed) OR more if multiple labels contain the substring.
         no_match must be false.
      3. Reality — the returned station_ids all map to saved stations
         whose labels contain the passed filter."""
    if not parse_saved_stations(os.environ.get("JASPER_CITIBIKE_STATIONS", "")):
        pytest.skip(
            "voice-eval: Citi Bike not configured "
            "(JASPER_CITIBIKE_STATIONS required) — set it to run this scenario",
        )
    # Pick the first saved label and use its first word as the
    # spoken substring (a label like "9 Av & 41 St" → "9 Av").
    saved = list(parse_saved_stations(os.environ.get("JASPER_CITIBIKE_STATIONS", "")))
    first_label = saved[0][1]
    spoken_phrase = " ".join(first_label.split()[:2]) or first_label

    result = await harness.ask(
        f"what's the deal with the {spoken_phrase} Citi Bike?",
    )

    # 1. Trajectory
    call = result.tool_call("get_citibike_status")
    assert call is not None, (
        f"[trial {trial}] model did not call get_citibike_status. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See {result.transcript_path}",
        )
    passed_filter = (call.args or {}).get("station_label", "").strip()
    assert passed_filter, (
        f"[trial {trial}] expected non-empty station_label for "
        f"station-specific query, got {passed_filter!r}. "
        f"See {result.transcript_path}"
    )

    # 2. Outcome
    payload = call.result or {}
    tool_stations = payload.get("stations") or []
    no_match = payload.get("no_match", False)
    if no_match:
        pytest.fail(
            f"[trial {trial}] tool returned no_match=true for "
            f"filter={passed_filter!r}. The model's chosen substring "
            f"didn't match any saved label. "
            f"Saved labels: {[lab for _, lab in saved]}. "
            f"See {result.transcript_path}"
        )
    assert tool_stations, (
        f"[trial {trial}] tool returned no_match=false but empty "
        f"stations. See {result.transcript_path}"
    )

    # 3. Reality — every returned station's saved label contains the
    # filter substring (case-insensitive).
    saved_by_id = {sid: lab for sid, lab in saved}
    needle = passed_filter.casefold()
    for s in tool_stations:
        sid = s.get("station_id")
        label = saved_by_id.get(sid)
        assert label is not None, (
            f"[trial {trial}] returned unsaved station_id {sid!r}. "
            f"See {result.transcript_path}"
        )
        assert needle in label.casefold(), (
            f"[trial {trial}] returned {sid!r}/{label!r} which does "
            f"not contain {passed_filter!r}. "
            f"See {result.transcript_path}"
        )
