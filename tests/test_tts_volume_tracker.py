"""Tests for TtsVolumeTracker.

Critical paths:
- music playing: targets `windowed_rms + headroom - TTS_PEAK` with
  NO main_volume clamp. The whole point of measuring playback_rms
  is to match the actual signal — layering an unmeasured ceiling on
  top defeats that. Hearing safety is enforced by TtsPlayout's
  MAX_TTS_GAIN_DB.
- silence with stale anchor: anchor + headroom, CAPPED at
  main_volume. Defends against "loud-music-yesterday, quiet-bedroom-
  today" without depending on anchor freshness signals.
- silence with NO anchor recorded: falls back to main_volume
  (first-boot only — `DEFAULT_ANCHOR_DBFS` keeps real life out of
  this branch).
- mute → silence floor
- read failure → silence floor (better quiet than loud)
- pause skips polling (so duck-induced changes don't pull TTS down
  during a turn)
- positive offsets / loud playback both safety-clamped by TtsPlayout
  (defense in depth)
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from jasper.audio_io import TtsPlayout
from jasper.voice_daemon import TtsVolumeTracker


class _FakeCamilla:
    """In-memory stand-in for CamillaController."""

    def __init__(
        self,
        volume_db: float = 0.0,
        muted: bool = False,
        playback_rms: tuple[float, float] | None = None,
    ) -> None:
        self.volume_db = volume_db
        self.muted = muted
        self.playback_rms = playback_rms or (float("-inf"), float("-inf"))
        self.fail_volume = False
        self.fail_levels = False
        self.calls_volume = 0
        self.calls_levels = 0

    async def get_volume_and_mute(
        self, *, best_effort: bool = False,
    ) -> tuple[float, bool] | None:
        self.calls_volume += 1
        if self.fail_volume:
            if best_effort:
                return None
            raise RuntimeError("camilla volume read failed")
        return self.volume_db, self.muted

    async def get_playback_rms(
        self, *, best_effort: bool = False,
    ) -> tuple[float, float] | None:
        self.calls_levels += 1
        if self.fail_levels:
            if best_effort:
                return None
            raise RuntimeError("camilla levels read failed")
        return self.playback_rms


def _tts() -> TtsPlayout:
    return TtsPlayout(device="dummy", output_rate=48000, gain_db=-8.0)


def _tracker(
    cam: _FakeCamilla,
    tts: TtsPlayout,
    *,
    headroom_db: float = 12.0,
    silence_threshold_dbfs: float = -50.0,
    window_sec: float = 8.0,
    initial_anchor_dbfs: float = -120.0,
    source_rms_dbfs: float | None = -3.0,
) -> TtsVolumeTracker:
    """Default initial anchor is -120 dBFS so the silence-fallback
    branch reverts to legacy `master + offset` behavior in tests
    that don't explicitly exercise the anchor path. Tests that DO
    exercise the anchor path pass an explicit value.

    `source_rms_dbfs` primes the measured source loudness (default -3 dBFS
    so the gain-formula tests have a fixed, known source); pass None for a
    fresh tracker sitting on the seed (the source-measurement tests)."""
    tracker = TtsVolumeTracker(
        cam, tts,
        music_headroom_db=headroom_db,
        silence_threshold_dbfs=silence_threshold_dbfs,
        music_window_sec=window_sec,
        initial_anchor_dbfs=initial_anchor_dbfs,
    )
    if source_rms_dbfs is not None:
        tracker.note_source_chunk(_pcm_with_rms(source_rms_dbfs))
    return tracker


def _pcm_with_rms(rms_dbfs: float, n: int = 2400) -> bytes:
    """A mono int16 PCM chunk at a constant amplitude whose RMS (= peak,
    for a constant signal) sits at `rms_dbfs`."""
    amp = int(round(32768 * 10 ** (rms_dbfs / 20.0)))
    amp = max(1, min(32767, amp))
    return np.full(n, amp, dtype=np.int16).tobytes()


# --- silence-fallback path -------------------------------------------------

@pytest.mark.asyncio
async def test_silence_falls_back_to_main_volume():
    """Below silence threshold, no anchor → main_volume ceiling."""
    cam = _FakeCamilla(volume_db=-10.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    # no anchor → target = ceiling = main_volume = -10.
    assert tts.gain_db == -10.0


@pytest.mark.asyncio
async def test_silence_at_master_zero():
    cam = _FakeCamilla(volume_db=0.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    # no anchor → ceiling = main_volume = 0; clamped to MAX cap -6.
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


# --- music-playing path ---------------------------------------------------

@pytest.mark.asyncio
async def test_music_targets_rms_plus_headroom():
    """Music playing: gain = windowed_rms + headroom - source_rms.
    No ceiling on the music branch — only the hearing-safety MAX cap.
    With music_rms=-30, headroom=12, source_rms=-3:
      target = -30 + 12 - (-3) = -15
      -15 is below the MAX cap (-6), so result = -15
    """
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-30.0, -30.0))
    tts = _tts()
    tracker = _tracker(cam, tts, headroom_db=12.0)
    await tracker.apply_now()
    assert tts.gain_db == -15.0


@pytest.mark.asyncio
async def test_loud_music_clamped_only_by_hearing_safety_cap():
    """When music is loud enough that the formula would push TTS
    above MAX_TTS_GAIN_DB, only the hearing-safety cap binds —
    NOT the main_volume ceiling (that one's a silence-fallback
    backstop, not applicable while we're actively measuring music).
      master=-5, music_rms=-10 (loud), headroom=12, source_rms=-3
      target = -10 + 12 - (-3) = 5  → way above MAX
      MAX_TTS_GAIN_DB = -6 in TtsPlayout
      result = -6   (NOT a ceiling-binds value — no ceiling here)
    """
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-10.0, -10.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


@pytest.mark.asyncio
async def test_quiet_music_targets_match_actual_level():
    """The whole point of the playback_rms approach: AirPlay sender at
    50% leaves music at e.g. -36 dBFS even though master=-5. Then:
      target = -36 + 12 - (-3) = -21
      no ceiling on the music branch; result = -21.
    """
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-36.0, -36.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    assert tts.gain_db == -21.0


@pytest.mark.asyncio
async def test_loud_music_at_low_master_matches_music_not_master():
    """REGRESSION (the user-reported bug): loud music plays at
    low/medium master_volume — e.g. external-amp setup where the
    user has cranked main_volume to compensate for a quiet amp, OR
    AirPlay carrying loud music at listening_level 50%.

    Old behavior: main_volume ceiling clamped TTS to track master,
    leaving TTS several dB QUIETER than the music the tracker
    measured. Subjectively "voice significantly quieter than music."

    New behavior: tracker matches measured music; only the hearing-
    safety MAX cap binds.

      master=-25, music_rms=-15 (loud source), headroom=12, source_rms=-3
      target = -15 + 12 - (-3) = 0
      MAX_TTS_GAIN_DB = -6   (NOT the old master-volume ceiling)
      result = -6.
    """
    cam = _FakeCamilla(volume_db=-25.0, playback_rms=(-15.0, -15.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


@pytest.mark.asyncio
async def test_uses_max_of_left_right():
    """If channels differ (a panned-mono source, broken hardware),
    we use the louder channel — under-shooting risks loud TTS over
    the actually-loud channel."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-50.0, -20.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    # max(-50, -20) = -20; -20 + 12 - (-3) = -5; MAX cap at -6 binds.
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


# --- mute / failure paths -------------------------------------------------

@pytest.mark.asyncio
async def test_mute_goes_to_floor():
    cam = _FakeCamilla(volume_db=0.0, muted=True, playback_rms=(-15.0, -15.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MIN_TTS_GAIN_DB


@pytest.mark.asyncio
async def test_volume_read_failure_falls_safe():
    cam = _FakeCamilla(volume_db=0.0, playback_rms=(-30.0, -30.0))
    cam.fail_volume = True
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MIN_TTS_GAIN_DB


@pytest.mark.asyncio
async def test_levels_read_failure_uses_silence_fallback():
    """If we can read main_volume but can't read levels, treat as
    silence — fall back to ceiling formula. Don't go to floor; the
    user might be in a quiet room and want intelligible TTS."""
    cam = _FakeCamilla(volume_db=-10.0, playback_rms=(-30.0, -30.0))
    cam.fail_levels = True
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    assert tts.gain_db == -10.0  # no anchor → ceiling = main_volume = -10


# --- windowed peak --------------------------------------------------------

@pytest.mark.asyncio
async def test_windowed_peak_holds_through_quiet_passages():
    """A song's quiet bridge between loud chorus sections shouldn't
    pull TTS up. The windowed peak is the max RMS over the window."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-15.0, -15.0))
    tts = _tts()
    tracker = _tracker(cam, tts, window_sec=10.0)
    # Loud chorus first.  -15 + 12 - (-3) = 0 → MAX cap at -6.
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB
    # Quiet passage (well below silence threshold).
    cam.playback_rms = (-60.0, -60.0)
    await tracker.apply_now()
    # Windowed peak still has the -15 reading, so target unchanged.
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


# --- pause / resume ------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_skips_polls():
    cam = _FakeCamilla(volume_db=0.0, playback_rms=(-30.0, -30.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    tracker.POLL_INTERVAL_SEC = 0.01  # type: ignore[misc]
    tracker.pause()
    await tracker.start()
    await asyncio.sleep(0.05)
    await tracker.stop()
    # apply_now ran once at start (it doesn't honor pause).
    # Loop polls were skipped due to pause.
    assert cam.calls_volume == 1


@pytest.mark.asyncio
async def test_resume_re_enables_tracking():
    cam = _FakeCamilla(volume_db=-20.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    tracker.POLL_INTERVAL_SEC = 0.01  # type: ignore[misc]
    tracker.pause()
    await tracker.start()
    await asyncio.sleep(0.03)
    tracker.resume()
    await asyncio.sleep(0.05)
    await tracker.stop()
    # silence fallback: no anchor → ceiling = main_volume = -20
    assert tts.gain_db == -20.0


# --- gain quantization (smooths log spam) --------------------------------

@pytest.mark.asyncio
async def test_target_quantized_to_1db():
    """1 dB quantization keeps the log clean and avoids micro-
    adjustments below the human JND for loudness change (~3 dB)."""
    cam = _FakeCamilla(volume_db=-10.4, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts)
    await tracker.apply_now()
    # ceiling = main_volume = -10.4 → round to nearest int = -10
    assert tts.gain_db == -10.0


# --- loudness anchor branch ----------------------------------------------

@pytest.mark.asyncio
async def test_anchor_used_during_silence():
    """No music currently playing, but we have an anchor → use it.
    This is the iPhone-disconnect fix: anchor=-30, headroom=12,
    source_rms=-3 → target = -30 + 12 - (-3) = -15.
    master=-5, ceiling=main_volume=-5. min(-15, -5) = -15."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, initial_anchor_dbfs=-30.0)
    await tracker.apply_now()
    assert tts.gain_db == -15.0


@pytest.mark.asyncio
async def test_anchor_capped_by_master_ceiling():
    """Even with a loud anchor, main_volume ALWAYS binds as the
    absolute upper bound on the silence branch. anchor=-15 (loud)
    would compute target=0; ceiling=main_volume=-25 binds."""
    cam = _FakeCamilla(volume_db=-25.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, initial_anchor_dbfs=-15.0)
    await tracker.apply_now()
    assert tts.gain_db == -25.0


@pytest.mark.asyncio
async def test_anchor_iphone_at_low_scenario():
    """The motivating scenario: master at 60% (-20), iPhone at 20%.
    Music played at -42 dBFS RMS; anchor caught that. Music stops.
    User asks Jarvis: target = -42+12+3 = -27. Ceiling = main_volume
    = -20. min(-27, -20) = -27 → the anchor target binds (it sits
    below the main_volume ceiling)."""
    cam = _FakeCamilla(volume_db=-20.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, initial_anchor_dbfs=-42.0)
    await tracker.apply_now()
    assert tts.gain_db == -27.0


@pytest.mark.asyncio
async def test_anchor_quieter_than_ceiling():
    """Anchor at -50 dBFS (very quiet music at iPhone=10%).
    target = -50+12+3 = -35. ceiling = main_volume = -5.
    min(-35, -5) = -35. TTS plays at -35 dB."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, initial_anchor_dbfs=-50.0)
    await tracker.apply_now()
    assert tts.gain_db == -35.0


@pytest.mark.asyncio
async def test_anchor_updates_when_music_plays():
    """While music is playing, anchor follows windowed RMS so it
    stays current. After music stops, anchor stays at last value."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-30.0, -30.0))
    tts = _tts()
    tracker = _tracker(cam, tts, initial_anchor_dbfs=-60.0)
    # Music playing — anchor should update from -60 to -30.
    await tracker.apply_now()
    assert tracker._anchor_dbfs == -30.0  # type: ignore[attr-defined]
    # Music stops — anchor stays at -30.
    cam.playback_rms = (-80.0, -80.0)
    await tracker.apply_now()
    assert tracker._anchor_dbfs == -30.0  # type: ignore[attr-defined]
    # Silence-fallback uses anchor: target = -30+12+3 = -15.
    # ceiling = main_volume = -5. min(-15, -5) = -15.
    assert tts.gain_db == -15.0


@pytest.mark.asyncio
async def test_anchor_persists_during_silence_doesnt_decay():
    """The anchor must not decay or get reset just because music
    paused. It only updates UPWARD (in the sense of getting refreshed)
    when actual music is detected."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-25.0, -25.0))
    tts = _tts()
    tracker = _tracker(cam, tts, initial_anchor_dbfs=-60.0)
    await tracker.apply_now()  # music → anchor = -25
    cam.playback_rms = (-80.0, -80.0)
    for _ in range(5):
        await tracker.apply_now()
    # 5 silent polls — anchor unchanged.
    assert tracker._anchor_dbfs == -25.0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_no_anchor_recorded_falls_back_to_main_volume():
    """When initial_anchor_dbfs is the sentinel -120 (effectively
    'no anchor'), silence-fallback reverts to the main_volume ceiling.
    Defensive backstop in case the persisted anchor was never set."""
    cam = _FakeCamilla(volume_db=-10.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, initial_anchor_dbfs=-120.0)
    await tracker.apply_now()
    assert tts.gain_db == -10.0  # no anchor → ceiling = main_volume = -10


@pytest.mark.asyncio
async def test_anchor_does_not_update_during_silence():
    """Sanity: silent reading must not become the new anchor.
    Only readings above silence_threshold_dbfs count."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, initial_anchor_dbfs=-30.0)
    await tracker.apply_now()
    # Anchor unchanged from initial -30; not updated to -80.
    assert tracker._anchor_dbfs == -30.0  # type: ignore[attr-defined]


# --- regression: the user-reported "TTS quieter than music" bug -----------

@pytest.mark.asyncio
async def test_regression_airplay_listening_level_70_does_not_clobber_tts():
    """REGRESSION for the 2026-05-24 production complaint.

    Live state from the Pi when the user reported "TTS voice is
    significantly quieter than the music":
      - provider OpenAI gpt-realtime-2 (active source: AirPlay)
      - listening_level 70%  →  main_volume = -15 dB
      - anchor_dbfs ≈ -26 dBFS (modern pop through CamillaDSP)
      - headroom = 16 dB (scenario value), source RMS = -3 dB

    Under the old "ceiling = main_volume clamps everything" rule:
      target = -26 + 16 - (-3) = -7
      ceiling = -15
      result = min(-7, -15) = -15   ← clobbered ~8 dB below intent

    Under the music-branch-no-ceiling rule:
      target = -7   (passes through; -7 < MAX cap -6 so no clamp)
      result = -7.

    A regression here means somebody re-introduced an unmeasured
    clamp on the music branch — DON'T do that. The point of measuring
    playback_rms is to drive the level FROM the measurement.
    """
    cam = _FakeCamilla(volume_db=-15.0, playback_rms=(-26.0, -26.0))
    tts = _tts()
    tracker = _tracker(
        cam, tts,
        headroom_db=16.0,     # scenario value: target clears the ceiling
    )
    await tracker.apply_now()
    # Pre-fix: tts.gain_db would have been -15 (master ceiling clobber).
    # Post-fix: -7 dB, matching music + headroom math directly.
    assert tts.gain_db == -7.0
    # Specifically not -15 (the old buggy clamp). 8 dB difference in
    # TTS level is FAR above the ~3 dB JND for loudness — this is
    # what the user reported as "significantly quieter than music."
    assert tts.gain_db > -15.0


@pytest.mark.asyncio
async def test_external_amp_scenario_voice_matches_loud_music():
    """User has external amp with its own knob; turns up
    listening_level to compensate. main_volume ≈ -3 dB (94%),
    music plays loud (anchor ≈ -22 dBFS).

    target = -22 + 16 - (-3) = -3   → MAX cap binds at -6.

    The point of this test is to lock in that the external-amp case
    keeps working on the music branch — the measured loud music
    drives the level straight into the MAX cap.
    """
    cam = _FakeCamilla(volume_db=-3.0, playback_rms=(-22.0, -22.0))
    tts = _tts()
    tracker = _tracker(cam, tts, headroom_db=16.0)
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


@pytest.mark.asyncio
async def test_music_branch_ignores_master_volume_entirely():
    """Stronger property: as long as music is playing above silence,
    the SAME measured music level produces the SAME TTS gain
    regardless of main_volume. The measurement is downstream of
    main_volume, so it already reflects it — pulling main_volume
    into the formula a second time is the bug we're guarding."""
    music_rms = -25.0
    gains = []
    for vol_db in (-3.0, -10.0, -20.0, -30.0, -40.0):
        cam = _FakeCamilla(volume_db=vol_db, playback_rms=(music_rms, music_rms))
        tts = _tts()
        tracker = _tracker(
            cam, tts, headroom_db=16.0,
            initial_anchor_dbfs=-120.0,  # ensure music branch fires
        )
        await tracker.apply_now()
        gains.append(tts.gain_db)
    # All five readings must match — main_volume is invisible to the
    # music branch by design.
    assert len(set(gains)) == 1, f"main_volume leaked into music branch: {gains}"


# --- silence-fallback ceiling: the kept defense ---------------------------

@pytest.mark.asyncio
async def test_silence_with_loud_stale_anchor_at_low_master_stays_quiet():
    """The legitimate use case for the main_volume ceiling, kept
    intentionally: user played loud music at high volume yesterday
    (anchor = -10 dBFS, near peak). Today the room is quiet and
    main_volume is at -40 dB (bedroom level). Without the ceiling
    the anchor would project to a loud TTS. With the ceiling kept
    on the silence branch, TTS stays at master-volume-appropriate
    level.

      master=-40, anchor=-10, no music currently playing
      anchor target = -10 + 12 - (-3) = 5  → would be loud
      ceiling = main_volume = -40
      min(5, -40) = -40 → quiet bedroom respected.
    """
    cam = _FakeCamilla(volume_db=-40.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(
        cam, tts, headroom_db=12.0,
        initial_anchor_dbfs=-10.0,
    )
    await tracker.apply_now()
    # Ceiling binds at -40 (well within MIN/MAX clamp).
    assert tts.gain_db == -40.0


# --- structured event=tts_gain.compute telemetry --------------------------

@pytest.mark.asyncio
async def test_gain_compute_event_fires_on_user_perceptible_change(caplog):
    """When set_gain_db actually moves the gain, one structured event
    line fires carrying enough fields to reconstruct the choice. This
    is the observability fix that would have saved an hour on the
    2026-05-24 production bug investigation."""
    cam = _FakeCamilla(volume_db=-15.0, playback_rms=(-26.0, -26.0))
    tts = _tts()
    tracker = _tracker(
        cam, tts, headroom_db=16.0,
        initial_anchor_dbfs=-120.0,
    )
    caplog.set_level("INFO", logger="jasper.voice_daemon")
    await tracker.apply_now()
    events = [r for r in caplog.records if "event=tts_gain.compute" in r.message]
    assert len(events) == 1, f"expected 1 event, got {len(events)}"
    msg = events[0].message
    # Every field a debugger needs:
    for fragment in (
        "branch=music",          # which decision path
        "windowed_rms=-26.0",    # input — what playback_rms reported
        "source_rms_dbfs=-3.0",  # measured source loudness (seed until measured)
        "anchor_dbfs=-26.0",     # anchor (just updated from windowed)
        "main_volume_db=-15.0",  # CamillaDSP master at the moment
        "ceiling_db=-15.0",      # main_volume (silence-only)
        "target_db=-7.0",        # what formula computed
        "final_db=-7.0",         # what TtsPlayout actually applied
        "max_cap_db=-6.0",       # hearing-safety cap (constant)
    ):
        assert fragment in msg, f"missing {fragment!r} in: {msg}"


@pytest.mark.asyncio
async def test_gain_compute_event_does_not_fire_on_unchanged_gain(caplog):
    """Logging volume must stay proportional to perceptible change.
    Two consecutive applies with the same inputs produce one event
    (the first) and silence on the second."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-25.0, -25.0))
    tts = _tts()
    tracker = _tracker(
        cam, tts, headroom_db=16.0,
        initial_anchor_dbfs=-120.0,
    )
    caplog.set_level("INFO", logger="jasper.voice_daemon")
    await tracker.apply_now()
    await tracker.apply_now()  # same inputs, gain unchanged
    events = [r for r in caplog.records if "event=tts_gain.compute" in r.message]
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("vol_db,rms,anchor,expected_branch", [
    (-5.0, (-25.0, -25.0), -120.0, "music"),     # windowed > threshold
    (-5.0, (-80.0, -80.0), -30.0, "anchor"),     # silence + valid anchor
    (-5.0, (-80.0, -80.0), -120.0, "no_anchor"), # silence + sentinel anchor
])
async def test_gain_compute_event_branch_label_matches_formula(
    caplog, vol_db, rms, anchor, expected_branch,
):
    """The branch label in the event log must match the branch the
    formula actually took. If these drift apart, future debugging
    will be reading lies. Lock in lockstep behavior across all three
    branches."""
    cam = _FakeCamilla(volume_db=vol_db, playback_rms=rms)
    tts = _tts()
    tracker = _tracker(
        cam, tts, headroom_db=16.0,
        initial_anchor_dbfs=anchor,
    )
    caplog.set_level("INFO", logger="jasper.voice_daemon")
    await tracker.apply_now()
    events = [r for r in caplog.records if "event=tts_gain.compute" in r.message]
    assert len(events) == 1, f"case {expected_branch}: expected 1 event"
    assert f"branch={expected_branch}" in events[0].message, (
        f"expected branch={expected_branch} for vol={vol_db} "
        f"rms={rms} anchor={anchor}; got: {events[0].message}"
    )


# --- measured per-provider source LOUDNESS / RMS (cross-provider volume) --

@pytest.mark.asyncio
async def test_source_rms_seeds_until_measured():
    """Before any audio is observed, the source estimate is the RMS seed."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-48.0, -48.0))
    tts = _tts()
    tracker = _tracker(
        cam, tts, headroom_db=12.0, source_rms_dbfs=None,
    )
    await tracker.apply_now()
    assert tracker.source_rms_dbfs == TtsVolumeTracker.SOURCE_RMS_SEED_DBFS
    # Music branch: target = -48 + 12 - (-20) = -16.
    assert tts.gain_db == -16.0


@pytest.mark.asyncio
async def test_quiet_provider_is_boosted_to_match_music():
    """A provider with quieter RMS gets MORE gain so its output RMS lands
    at the same music+headroom target as a louder one."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-48.0, -48.0))
    tts = _tts()
    tracker = _tracker(
        cam, tts, headroom_db=12.0, source_rms_dbfs=None,
    )
    for _ in range(40):
        tracker.note_source_chunk(_pcm_with_rms(-24.0))
    assert tracker.source_rms_dbfs == pytest.approx(-24.0, abs=0.3)
    await tracker.apply_now()
    # target = -48 + 12 - (-24) = -12.
    assert tts.gain_db == -12.0


@pytest.mark.asyncio
async def test_providers_reach_same_output_rms_once_measured():
    """The definition of the fix: after each provider's RMS is measured,
    the OUTPUT RMS (source_rms + applied gain) is identical regardless of
    native loudness — equal perceived loudness. Music kept quiet enough
    that the -6 dB hearing-safety cap doesn't bind."""
    outputs = []
    for src_rms in (-16.0, -22.0, -28.0):
        cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-49.0, -49.0))
        tts = _tts()
        tracker = _tracker(
            cam, tts, headroom_db=6.0, source_rms_dbfs=None,
        )
        for _ in range(40):
            tracker.note_source_chunk(_pcm_with_rms(src_rms))
        await tracker.apply_now()
        outputs.append(round(src_rms + tts.gain_db))
    assert len(set(outputs)) == 1, f"providers not equalized: {outputs}"


def test_silent_chunks_do_not_move_source_estimate():
    """Inter-word/sentence gaps (below the voiced floor) must not drag the
    loudness estimate down — that would over-boost the next turn."""
    cam = _FakeCamilla()
    tts = _tts()
    tracker = _tracker(cam, tts, source_rms_dbfs=None)
    for _ in range(20):
        tracker.note_source_chunk(_pcm_with_rms(-60.0))  # below voiced floor
    assert tracker.source_rms_dbfs == TtsVolumeTracker.SOURCE_RMS_SEED_DBFS


def test_note_source_chunk_is_failsoft_on_garbage():
    """Measurement must never raise into the playback path."""
    cam = _FakeCamilla()
    tts = _tts()
    tracker = _tracker(cam, tts, source_rms_dbfs=None)
    tracker.note_source_chunk(b"")      # empty
    tracker.note_source_chunk(b"\x01")  # 1 byte — not a clean int16 frame
    assert tracker.source_rms_dbfs == TtsVolumeTracker.SOURCE_RMS_SEED_DBFS


def test_source_rms_is_loudness_not_peak():
    """RMS measures LOUDNESS, not peak: with loud and quiet voiced chunks
    the estimate is the POWER-mean (true RMS), dominated by the loud
    chunks — not the peak (max) and not a naive dB-average. This is what
    makes a compressed voice (Gemini) and a dynamic one (OpenAI) come out
    equally loud rather than equal-peak (the 2026-05-31 too-loud report)."""
    cam = _FakeCamilla()
    tts = _tts()
    tracker = _tracker(cam, tts, source_rms_dbfs=None)
    # Half loud (-12 dBFS RMS), half quiet (-30 dBFS RMS).
    for i in range(64):
        tracker.note_source_chunk(_pcm_with_rms(-12.0 if i % 2 == 0 else -30.0))
    # Power-mean: 10*log10((10^-1.2 + 10^-3.0)/2) ≈ -15 dBFS — NOT -12
    # (the peak/max) and NOT -21 (a naive dB-average).
    assert tracker.source_rms_dbfs == pytest.approx(-15.0, abs=0.5)
