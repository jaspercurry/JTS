"""Spotify regression scenarios.

============================================================
PLAYBACK + COST NOTICE — read carefully before running
============================================================
These scenarios trigger REAL playback on the speaker. The
harness wires up the real OAuth router and real librespot
device, so when the model calls `spotify_play("Covers")`,
music actually starts coming out of the speaker. **Skip via
`JASPER_VOICE_EVAL_SKIP_PLAYBACK=1` if anyone is using the
speaker.**

Plus the usual paid LLM API cost per turn. PASS_K = 3 turns
per scenario function. DO NOT loop or increase PASS_K without
explicit human approval and confirmation that playback is OK.
============================================================

Each scenario follows the same three-assertion shape as
`test_subway.py`:

  1. Trajectory: the model called the expected tool.
  2. Outcome: the tool returned the expected fields.
  3. Reality: the tool's data is internally consistent (e.g.
     the resolved name matches the requested name).

We don't have a clean independent oracle for "is Covers in this
user's Spotify library" — verifying that would require us to also
authenticate against the same account and enumerate playlists,
which is essentially what the tool does. So the reality check here
is shape-based: confirm the tool reported a successful playback
start with a name that includes the query.
"""
from __future__ import annotations

import os

import pytest


PASS_K = 3


def _playback_skip() -> bool:
    """True if the user has opted out of playback-affecting tests
    for this run. Set `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1` to skip."""
    return os.environ.get("JASPER_VOICE_EVAL_SKIP_PLAYBACK", "").strip() == "1"


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_play_owned_playlist_covers(harness, trial: int) -> None:
    """Asks 'play my Covers playlist' — the model should call
    `spotify_play` with `kind="playlist"` and `query="Covers"`,
    the tool's resolver should find a playlist whose name contains
    "cover", and playback should start.

    **KNOWN FAILING (2026-05-21)**: `current_user_playlists` is
    fetched with `limit=50` and no pagination. If a household
    member's library has more than 50 playlists and "Covers" is
    not in the first 50, the resolver returns
    `_NOT_UNDERSTOOD` and the test fails on assertion 2. The
    failure documents the bug; when pagination + per-account cache
    land, this turns green.

    Side-effect: starts playing Covers on the speaker. The fixture
    doesn't restore the previous source — that's deliberate, the
    test should leave evidence that something was played. If you
    were listening to AirPlay, you'll need to resume manually.
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

    # 2. Outcome — the tool returned ok=True (resolver found a
    # match and start_playback fired). Without pagination, this
    # is the assertion that fails when Covers is the 51st+ entry.
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

    # 4. Spoken reality — the model's spoken confirmation should
    # mention "cover" (it speaks the tool's `confirm` field, which
    # contains the resolved playlist name). Skips when no transcript
    # captured.
    if result.spoken_text:
        spoken = result.spoken_text.lower()
        assert "cover" in spoken, (
            f"[trial {trial}] model's spoken response doesn't mention "
            f"'cover' — likely played the wrong thing or didn't confirm. "
            f"Spoken text: {result.spoken_text!r}. "
            f"See transcript: {result.transcript_path}"
        )
