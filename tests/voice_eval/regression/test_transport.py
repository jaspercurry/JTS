"""Transport-tool regression scenarios — next_track, previous_track,
pause, get_now_playing.

These lock the LLM-visible routing for the playback-control verbs: a
"skip" utterance must reach `next_track`, "go back" must reach
`previous_track`, "pause" / "stop" must reach `pause`, and "what's
playing?" must reach `get_now_playing`. The trajectory assertion is
the load-bearing one — several of these phrasings could plausibly be
mishandled conversationally or routed to `spotify_play`/`resume`, and
that's exactly the regression class this file guards.

============================================================
PLAYBACK + COST NOTICE — read carefully before running
============================================================
next_track / previous_track / pause dispatch to the live renderer
(`_detect_source` → shairport MPRIS / spotipy / bluez). When music
is actually playing on the speaker, calling them WILL skip / go
back / pause it. Skip via `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1` if
anyone is using the speaker.

`get_now_playing` is read-only (it queries metadata, doesn't change
playback) but shares the skip-guard for consistency and because it
still costs a paid LLM turn.

PASS_K = 3 turns per scenario function. DO NOT loop or increase
PASS_K without explicit human approval and confirmation that
playback control is OK. Per-turn cost: read harness.py top
docstring.
============================================================

Three-assertion shape, with a twist the read-only scenarios don't
have: the OUTCOME assertion is conditional on whether music is
actually playing. The harness has no guaranteed playback state, so:

  1. Trajectory: the model called the expected transport tool.
  2. Outcome: the tool returned a coherent shape — either ok=True
     (something was playing and the action dispatched) OR the
     documented "nothing is playing" error (source="none"). A bare
     unhandled exception or a missing both-of-those is the failure.
  3. Reality: the tool did NOT mis-route (e.g. next_track must not
     also call spotify_play); spoken text stays terse per the
     docstring's voice-answer style.
"""
from __future__ import annotations

import os

import pytest


PASS_K = 3


def _playback_skip() -> bool:
    """True if the user has opted out of playback-affecting tests for
    this run. Set `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1` to skip. Mirrors
    test_spotify.py's guard so one env var silences every
    playback-touching scenario."""
    return os.environ.get("JASPER_VOICE_EVAL_SKIP_PLAYBACK", "").strip() == "1"


def _assert_transport_outcome(call, *, trial: int, transcript_path) -> None:
    """Shared outcome check for the action verbs (next/previous/pause).

    The harness can't guarantee live playback, so we accept either of
    the two documented shapes and fail only on an unhandled exception
    or an unrecognised result. This keeps the scenario meaningful on a
    laptop (where nothing is playing → source='none') AND on the Pi
    mid-playback (where the action actually fires → ok=True)."""
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {transcript_path}",
        )
    res = call.result or {}
    ok = bool(res.get("ok"))
    nothing_playing = res.get("source") == "none" and bool(res.get("error"))
    assert ok or nothing_playing, (
        f"[trial {trial}] transport tool returned an unrecognised shape: "
        f"{res!r}. Expected either ok=True (dispatched) or "
        f"source='none' with an error (nothing playing). "
        f"See transcript: {transcript_path}"
    )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_skip_routes_to_next_track(harness, trial: int) -> None:
    """Asks 'skip this song' — the model must call `next_track`, NOT
    `spotify_play` (which would start a brand-new search) and NOT
    `resume`."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping playback-affecting scenario",
        )

    result = await harness.ask("skip this song")

    # 1. Trajectory — next_track, and nothing that would start a new
    # track instead.
    call = result.tool_call("next_track")
    assert call is not None, (
        f"[trial {trial}] model did not call next_track. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    assert result.tool_call("spotify_play") is None, (
        f"[trial {trial}] model called spotify_play on a 'skip' utterance "
        f"— it should advance the current queue via next_track, not start "
        f"a new search. See transcript: {result.transcript_path}"
    )

    # 2. Outcome — coherent shape (dispatched or nothing-playing).
    _assert_transport_outcome(call, trial=trial, transcript_path=result.transcript_path)


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_go_back_routes_to_previous_track(harness, trial: int) -> None:
    """Asks 'go back to the last song' — the model must call
    `previous_track`."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping playback-affecting scenario",
        )

    result = await harness.ask("go back to the last song")

    # 1. Trajectory
    call = result.tool_call("previous_track")
    assert call is not None, (
        f"[trial {trial}] model did not call previous_track. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )

    # 2. Outcome
    _assert_transport_outcome(call, trial=trial, transcript_path=result.transcript_path)


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_pause_routes_to_pause(harness, trial: int) -> None:
    """Asks 'pause the music' — the model must call `pause`. The pause
    docstring also claims 'stop' / 'make it stop' phrasings, but this
    scenario pins the canonical verb; a 'stop' variant is a good
    follow-up scenario."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping playback-affecting scenario",
        )

    result = await harness.ask("pause the music")

    # 1. Trajectory — pause, not resume (the opposite verb).
    call = result.tool_call("pause")
    assert call is not None, (
        f"[trial {trial}] model did not call pause. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    assert result.tool_call("resume") is None, (
        f"[trial {trial}] model called resume on a 'pause' utterance — "
        f"opposite verb. See transcript: {result.transcript_path}"
    )

    # 2. Outcome
    _assert_transport_outcome(call, trial=trial, transcript_path=result.transcript_path)


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_whats_playing_routes_to_get_now_playing(harness, trial: int) -> None:
    """Asks 'what's playing right now?' — the model must call
    `get_now_playing`.

    `get_now_playing` is read-only, but it shares the playback skip
    guard so a single env var silences all transport scenarios.

    Outcome here is shape-only: the tool always returns a dict with
    title / artist / album / source keys (empty strings when nothing
    is playing). We assert those keys exist rather than asserting a
    specific track, because the harness has no guaranteed now-playing
    state."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping playback-affecting scenario",
        )

    result = await harness.ask("what's playing right now?")

    # 1. Trajectory
    call = result.tool_call("get_now_playing")
    assert call is not None, (
        f"[trial {trial}] model did not call get_now_playing. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — the metadata shape is always present (the tool
    # returns empty strings, not a missing key, when idle).
    res = call.result or {}
    for key in ("title", "artist", "album", "source"):
        assert key in res, (
            f"[trial {trial}] get_now_playing result missing '{key}': "
            f"{res!r}. See transcript: {result.transcript_path}"
        )
