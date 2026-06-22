# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Volume regression scenarios — pins the LLM-visible contract for the
five volume tools (`get_volume`, `set_volume`, `adjust_volume`, `mute`,
`unmute`) backed by `jasper.tools.audio.make_audio_tools`.

Each scenario follows the same three-assertion shape as
`test_subway.py`:

  1. Trajectory: the model called the expected tool.
  2. Outcome: the tool returned the expected fields.
  3. Reality: the coordinator's persisted state matches what the tool
     reported (read back via the side-channel handle, no extra paid
     call).

============================================================
SIDE-EFFECT + COST NOTICE — read carefully before running
============================================================
These scenarios CHANGE THE SPEAKER VOLUME. The harness wires up the
real source-aware `VolumeCoordinator`, so when the model calls
`set_volume(20)` the speaker's actual output level moves. Each
scenario **captures the prior listening level and restores it in a
`finally`**, so a clean run leaves the speaker where it started — but
a crash mid-turn could leave it altered. Skip via
`JASPER_VOICE_EVAL_SKIP_PLAYBACK=1` when the household is using the
speaker (volume is a speaker side-effect, same gate as playback).

The coordinator drives CamillaDSP over a websocket, so these
scenarios only do anything useful where CamillaDSP is reachable (the
Pi). On a laptop the tools register but the coordinator can't reach
Camilla; collection still works.

Plus the usual paid LLM API cost per turn. PASS_K = 3 turns per
scenario function. DO NOT loop or increase PASS_K without explicit
human approval and confirmation that changing the volume is OK.
============================================================
"""
from __future__ import annotations

import os

import pytest


PASS_K = 3


def _playback_skip() -> bool:
    """True if the user has opted out of speaker-affecting tests for
    this run. Volume is a speaker side-effect, so it shares the
    playback gate. Set `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1` to skip."""
    return os.environ.get("JASPER_VOICE_EVAL_SKIP_PLAYBACK", "").strip() == "1"


def _coordinator(harness):
    """The harness's VolumeCoordinator, or skip if registry wiring
    regressed. Mirrors test_timer's `_reset_scheduler` side-channel
    pattern — the coordinator is exposed via `test_state` so a
    scenario can read+restore the level without a second paid call."""
    coord = harness.test_state.get("volume_coordinator")
    if coord is None:
        pytest.skip(
            "voice-eval: harness.test_state missing 'volume_coordinator' "
            "— registry wiring regressed. See _build_test_registry.",
        )
    return coord


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_set_volume_absolute(harness, trial: int) -> None:
    """Asks 'set the volume to 20 percent' — the model should call
    `set_volume` with `percent=20` and the coordinator should apply
    (close to) 20%.

    The headline failure modes this catches:
      - model answers conversationally without calling a tool
        ('Okay, volume set to twenty') while nothing actually moves;
      - model calls `adjust_volume` instead (relative) for an
        absolute request;
      - tool applies a wildly different level than requested.

    Restores the prior level in a `finally`."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping volume-changing scenario",
        )
    coord = _coordinator(harness)
    prior = coord.get_listening_level()
    try:
        result = await harness.ask("set the volume to 20 percent")

        # 1. Trajectory — the model must call set_volume (absolute),
        # not adjust_volume (relative).
        call = result.tool_call("set_volume")
        assert call is not None, (
            f"[trial {trial}] model did not call set_volume. "
            f"Tools observed: "
            f"{[r.name for r in result.tool_call_records] or 'none'}. "
            f"If adjust_volume was called instead, the model treated an "
            f"absolute request as relative. "
            f"See transcript: {result.transcript_path}"
        )
        if call.error:
            pytest.fail(
                f"[trial {trial}] set_volume raised: {call.error}. "
                f"(CamillaDSP unreachable off-Pi is expected — run on the "
                f"Pi.) See transcript: {result.transcript_path}",
            )

        # 2. Outcome — tool returned ok=True and an applied percent
        # near the requested 20. Tolerance absorbs the dB<->percent
        # rounding in _percent_to_db / _db_to_percent.
        res = call.result or {}
        assert res.get("ok") is True, (
            f"[trial {trial}] set_volume did not return ok=True: {res!r}. "
            f"See transcript: {result.transcript_path}"
        )
        applied = res.get("percent")
        assert isinstance(applied, int), (
            f"[trial {trial}] set_volume returned non-int percent: "
            f"{applied!r}. See transcript: {result.transcript_path}"
        )
        assert abs(applied - 20) <= 2, (
            f"[trial {trial}] requested 20% but tool applied {applied}% "
            f"(beyond ±2 rounding tolerance). Args: {call.args!r}. "
            f"See transcript: {result.transcript_path}"
        )

        # 3. Reality — the coordinator's in-memory canonical level
        # matches what the tool reported. Catches "tool returned a
        # number it never actually applied".
        assert coord.get_listening_level() == applied, (
            f"[trial {trial}] tool reported {applied}% but coordinator "
            f"level is {coord.get_listening_level()}%. "
            f"See transcript: {result.transcript_path}"
        )
    finally:
        await coord.set_listening_level(prior)


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_adjust_volume_relative(harness, trial: int) -> None:
    """Asks 'turn it down a little' — the model should call
    `adjust_volume` with a small NEGATIVE delta (the tool docstring
    maps 'a little' → ±5), not `set_volume`.

    Catches: relative request misrouted to the absolute tool, or a
    positive delta for a 'down' request (direction inversion)."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping volume-changing scenario",
        )
    coord = _coordinator(harness)
    prior = coord.get_listening_level()
    # Seed a known mid level so a downward adjust has room to move and
    # isn't clamped at the floor.
    await coord.set_listening_level(60)
    try:
        result = await harness.ask("turn it down a little")

        # 1. Trajectory — the model must call adjust_volume (relative).
        call = result.tool_call("adjust_volume")
        set_call = result.tool_call("set_volume")
        assert call is not None, (
            f"[trial {trial}] model did not call adjust_volume. "
            f"Tools observed: "
            f"{[r.name for r in result.tool_call_records] or 'none'}. "
            f"A relative 'turn it down' must route to adjust_volume. "
            f"See transcript: {result.transcript_path}"
        )
        assert set_call is None, (
            f"[trial {trial}] model ALSO called set_volume — a relative "
            f"'turn it down a little' should only adjust_volume. "
            f"See transcript: {result.transcript_path}"
        )
        if call.error:
            pytest.fail(
                f"[trial {trial}] adjust_volume raised: {call.error}. "
                f"See transcript: {result.transcript_path}",
            )

        # 2. Outcome — the delta the model passed is negative (down).
        delta = call.args.get("delta_percent")
        assert isinstance(delta, int) and delta < 0, (
            f"[trial {trial}] adjust_volume delta_percent={delta!r}; "
            f"expected a negative int for 'turn it down'. "
            f"See transcript: {result.transcript_path}"
        )
        res = call.result or {}
        assert res.get("ok") is True, (
            f"[trial {trial}] adjust_volume did not return ok=True: "
            f"{res!r}. See transcript: {result.transcript_path}"
        )

        # 3. Reality — the new level is below the seeded 60 (it went
        # down) and matches the coordinator's canonical level.
        applied = res.get("percent")
        assert isinstance(applied, int) and applied < 60, (
            f"[trial {trial}] level after 'turn it down' is {applied!r}; "
            f"expected below the seeded 60%. "
            f"See transcript: {result.transcript_path}"
        )
        assert coord.get_listening_level() == applied, (
            f"[trial {trial}] tool reported {applied}% but coordinator "
            f"level is {coord.get_listening_level()}%. "
            f"See transcript: {result.transcript_path}"
        )
    finally:
        await coord.set_listening_level(prior)


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_get_volume_reports_current_level(harness, trial: int) -> None:
    """Asks 'what's the volume?' — the model should call `get_volume`
    (and NOT change anything), and the reported percent should match
    the level we seeded.

    Catches: model answering a query by guessing instead of calling
    the tool, and the loudest failure mode — the model speaking a
    number that doesn't match what the tool returned."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping volume-changing scenario",
        )
    coord = _coordinator(harness)
    prior = coord.get_listening_level()
    # Seed a known, non-round level so a coincidental guess is unlikely.
    seeded = await coord.set_listening_level(35)
    try:
        result = await harness.ask("what's the volume?")

        # 1. Trajectory — the model must call get_volume.
        call = result.tool_call("get_volume")
        assert call is not None, (
            f"[trial {trial}] model did not call get_volume. "
            f"Tools observed: "
            f"{[r.name for r in result.tool_call_records] or 'none'}. "
            f"See transcript: {result.transcript_path}"
        )
        if call.error:
            pytest.fail(
                f"[trial {trial}] get_volume raised: {call.error}. "
                f"See transcript: {result.transcript_path}",
            )

        # A query must not mutate — set_volume / adjust_volume must be
        # absent (the get_volume docstring: "don't change the volume on
        # a query").
        assert result.tool_call("set_volume") is None, (
            f"[trial {trial}] model called set_volume on a volume QUERY. "
            f"See transcript: {result.transcript_path}"
        )
        assert result.tool_call("adjust_volume") is None, (
            f"[trial {trial}] model called adjust_volume on a volume "
            f"QUERY. See transcript: {result.transcript_path}"
        )

        # 2. Outcome — the tool reports the seeded level.
        res = call.result or {}
        assert res.get("percent") == seeded, (
            f"[trial {trial}] get_volume reported {res.get('percent')!r}; "
            f"expected the seeded {seeded}%. "
            f"See transcript: {result.transcript_path}"
        )

        # 3. Spoken reality — the model's spoken number matches the
        # tool's reported level. Skips when no transcript captured.
        # tol=2 absorbs the model rounding to a tens boundary.
        if result.spoken_text:
            spoken_nums = harness.extract_minutes_from_text(result.spoken_text)
            assert spoken_nums, (
                f"[trial {trial}] no number in spoken volume answer: "
                f"{result.spoken_text!r}. "
                f"See transcript: {result.transcript_path}"
            )
            assert any(abs(n - seeded) <= 2 for n in spoken_nums), (
                f"[trial {trial}] tool reported {seeded}% but model spoke "
                f"{spoken_nums} — pure hallucination of the level. "
                f"Spoken text: {result.spoken_text!r}. "
                f"See transcript: {result.transcript_path}"
            )
    finally:
        await coord.set_listening_level(prior)


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_mute_then_unmute_round_trips_level(harness, trial: int) -> None:
    """Two turns: 'mute the speaker' then 'unmute'. The model must
    call `mute` then `unmute`, and unmute must restore the pre-mute
    level (the coordinator's documented behaviour).

    Catches: mute/unmute misrouted to set_volume(0)/set_volume(N),
    and the unmute path losing the pre-mute level."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping volume-changing scenario",
        )
    coord = _coordinator(harness)
    prior = coord.get_listening_level()
    # Seed a known level so the round-trip target is unambiguous.
    seeded = await coord.set_listening_level(45)
    try:
        # Turn 1: mute.
        muted = await harness.ask("mute the speaker")
        mute_call = muted.tool_call("mute")
        assert mute_call is not None, (
            f"[trial {trial}] model did not call mute. "
            f"Tools observed: "
            f"{[r.name for r in muted.tool_call_records] or 'none'}. "
            f"See transcript: {muted.transcript_path}"
        )
        if mute_call.error:
            pytest.fail(
                f"[trial {trial}] mute raised: {mute_call.error}. "
                f"See transcript: {muted.transcript_path}",
            )
        assert (mute_call.result or {}).get("muted") is True, (
            f"[trial {trial}] mute did not return muted=True: "
            f"{mute_call.result!r}. See transcript: {muted.transcript_path}"
        )
        assert coord.is_muted(), (
            f"[trial {trial}] coordinator not muted after mute call. "
            f"See transcript: {muted.transcript_path}"
        )

        # Turn 2: unmute.
        result = await harness.ask("unmute")
        unmute_call = result.tool_call("unmute")
        assert unmute_call is not None, (
            f"[trial {trial}] model did not call unmute. "
            f"Tools observed: "
            f"{[r.name for r in result.tool_call_records] or 'none'}. "
            f"See transcript: {result.transcript_path}"
        )
        if unmute_call.error:
            pytest.fail(
                f"[trial {trial}] unmute raised: {unmute_call.error}. "
                f"See transcript: {result.transcript_path}",
            )

        # Outcome + reality — unmute restored the seeded pre-mute level.
        restored = (unmute_call.result or {}).get("percent")
        assert restored == seeded, (
            f"[trial {trial}] unmute restored {restored!r}; expected the "
            f"pre-mute {seeded}%. The pre-mute level was lost. "
            f"See transcript: {result.transcript_path}"
        )
        assert not coord.is_muted(), (
            f"[trial {trial}] coordinator still muted after unmute. "
            f"See transcript: {result.transcript_path}"
        )
    finally:
        # Best-effort: clear any lingering mute, then restore the level.
        if coord.is_muted():
            await coord.unmute(fallback_level=prior)
        await coord.set_listening_level(prior)
