# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Paid Spotify scenarios can change live playback and the queue.

They use real OAuth accounts and the configured player. Prior playback is
not restored; resume the prior source manually after a run. Set
JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 to skip these scenarios.

PASS_K = 3 turns per scenario. Announce the count, estimated cost and playback
side effects before running. Never loop or auto-retry. Increase PASS_K only
with explicit approval and confirmation that playback is OK.

Checks inspect tool calls and returned names. They do not independently verify
the account library or prove audible playback. See tests/voice_eval/README.md.
"""
from __future__ import annotations

import pytest

from tests.voice_eval.regression._guards import playback_skip as _playback_skip


PASS_K = 3


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_queue_track_routes_to_spotify_queue(
    harness, trial: int,
) -> None:
    """Asks 'queue up No Surprises by Radiohead'. The model should call
    `spotify_queue`, not `spotify_play`, and the tool should add a track
    whose name matches the requested title."""
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping playback-affecting scenario",
        )

    result = await harness.ask("queue up No Surprises by Radiohead")

    # 1. Trajectory — queue request maps to spotify_queue only.
    queue_call = result.tool_call("spotify_queue")
    play_call = result.tool_call("spotify_play")
    assert queue_call is not None, (
        f"[trial {trial}] model did not call spotify_queue. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    assert play_call is None, (
        f"[trial {trial}] model called spotify_play for a queue request. "
        f"See transcript: {result.transcript_path}"
    )
    if queue_call.error:
        pytest.fail(
            f"[trial {trial}] spotify_queue raised: {queue_call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — tool reports successful queueing.
    res = queue_call.result or {}
    assert res.get("ok"), (
        f"[trial {trial}] spotify_queue did not return ok=True. "
        f"Result: {res!r}. See transcript: {result.transcript_path}"
    )

    # 3. Reality — queued title matches the requested track title.
    queued = (res.get("queued") or "").lower()
    assert "no surprises" in queued, (
        f"[trial {trial}] spotify_queue queued {res.get('queued')!r}, "
        f"not 'No Surprises'. See transcript: {result.transcript_path}"
    )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_play_owned_playlist_covers(harness, trial: int) -> None:
    """Resolve and play the household's Covers playlist.

    The library lookup reads current_user_playlists with limit=50 and no
    pagination. It omits later entries; configured playlists are a separate input.
    This scenario changes live playback and does not restore the prior source.
    """
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping playback-affecting scenario",
        )

    result = await harness.ask("play my Covers playlist")

    # 1. Trajectory — the model must call spotify_play.
    call = result.tool_call("spotify_play")
    assert call is not None, (
        f"[trial {trial}] model did not call spotify_play. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    res = call.result or {}
    assert res.get("ok"), (
        f"[trial {trial}] spotify_play did not return ok=True. "
        f"Result: {res!r}. This is the symptom of the >50-playlist "
        f"pagination bug — the resolver returned _NOT_UNDERSTOOD "
        f"because Covers wasn't in the first-50 page. "
        f"See transcript: {result.transcript_path}"
    )

    # 3. Reality — the resolved kind is playlist and the resolved
    # name contains "cover" (case-insensitive). Catches a fuzzy
    # mishit on a different playlist that happens to score above
    # threshold (e.g. "Covers and Remixes" is fine, but "Discover
    # Weekly" matching would mean the threshold is too loose).
    assert res.get("kind") == "playlist", (
        f"[trial {trial}] spotify_play resolved to kind={res.get('kind')!r}, "
        f"not 'playlist'. The model may have set kind='auto' rather "
        f"than 'playlist'. "
        f"See transcript: {result.transcript_path}"
    )
    playing = (res.get("playing") or "").lower()
    assert "cover" in playing, (
        f"[trial {trial}] spotify_play resolved to {res.get('playing')!r}, "
        f"which doesn't contain 'cover' — fuzzy-match landed on the "
        f"wrong playlist. "
        f"See transcript: {result.transcript_path}"
    )

    result.require_spoken_text()
    spoken = result.spoken_text.lower()
    assert "cover" in spoken, (
        f"[trial {trial}] model's spoken response doesn't mention "
        f"'cover' — likely played the wrong thing or didn't confirm. "
        f"Spoken text: {result.spoken_text!r}. "
        f"See transcript: {result.transcript_path}"
    )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_play_new_artist_song_routes_to_latest_by_artist(
    harness, trial: int,
) -> None:
    """Route the artist's latest-release request to spotify_play_latest_by_artist.

    This starts live playback and does not restore the prior source. Set
    JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 to skip it.
    """
    if _playback_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping playback-affecting scenario",
        )

    result = await harness.ask("play the new Rainbow Kitten Surprise song")

    latest_call = result.tool_call("spotify_play_latest_by_artist")
    play_call = result.tool_call("spotify_play")
    assert latest_call is not None, (
        f"[trial {trial}] model did not call "
        f"spotify_play_latest_by_artist. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"If spotify_play was called instead, the system prompt's "
        f"'new/newest/latest' rule didn't stick — that's the bug. "
        f"See transcript: {result.transcript_path}"
    )
    assert play_call is None, (
        f"[trial {trial}] model called spotify_play ALSO — should "
        f"only call spotify_play_latest_by_artist. Double-tool calls "
        f"on this phrasing mean the model is hedging. "
        f"See transcript: {result.transcript_path}"
    )
    if latest_call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {latest_call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — tool returned ok=True with kind ∈ {single, album}.
    res = latest_call.result or {}
    assert res.get("ok"), (
        f"[trial {trial}] spotify_play_latest_by_artist did not return "
        f"ok=True. Result: {res!r}. See transcript: {result.transcript_path}"
    )
    assert res.get("kind") in ("single", "album"), (
        f"[trial {trial}] unexpected kind={res.get('kind')!r}; expected "
        f"'single' or 'album'. See transcript: {result.transcript_path}"
    )

    # 3. Reality — the resolved artist must match the named artist.
    # Catches a fuzzy mishit (resolving to a different artist whose
    # name happens to contain "rainbow" or "kitten").
    resolved_artist = (res.get("artist") or "").lower()
    assert "rainbow kitten surprise" in resolved_artist, (
        f"[trial {trial}] resolved to artist={res.get('artist')!r}, "
        f"not 'Rainbow Kitten Surprise' — artist search landed on the "
        f"wrong band. See transcript: {result.transcript_path}"
    )
