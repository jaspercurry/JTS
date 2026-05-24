"""Tests for TtsVolumeTracker.

Critical paths:
- music playing: targets `windowed_rms + headroom - TTS_PEAK` with
  NO master+offset clamp. The whole point of measuring playback_rms
  is to match the actual signal — layering an unmeasured ceiling on
  top defeats that. Hearing safety is enforced by TtsPlayout's
  MAX_TTS_GAIN_DB.
- silence with stale anchor: anchor + headroom, CAPPED at
  master+offset. Defends against "loud-music-yesterday, quiet-bedroom-
  today" without depending on anchor freshness signals.
- silence with NO anchor recorded: falls back to master+offset
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
    offset_db: float = -8.0,
    headroom_db: float = 12.0,
    silence_threshold_dbfs: float = -50.0,
    window_sec: float = 8.0,
    initial_anchor_dbfs: float = -120.0,
) -> TtsVolumeTracker:
    """Default initial anchor is -120 dBFS so the silence-fallback
    branch reverts to legacy `master + offset` behavior in tests
    that don't explicitly exercise the anchor path. Tests that DO
    exercise the anchor path pass an explicit value."""
    return TtsVolumeTracker(
        cam, tts,
        offset_db=offset_db,
        music_headroom_db=headroom_db,
        silence_threshold_dbfs=silence_threshold_dbfs,
        music_window_sec=window_sec,
        initial_anchor_dbfs=initial_anchor_dbfs,
    )


# --- silence-fallback path -------------------------------------------------

@pytest.mark.asyncio
async def test_silence_falls_back_to_legacy_formula():
    """Below silence threshold → main_volume + offset."""
    cam = _FakeCamilla(volume_db=-10.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    # -10 + -8 = -18; quantized to -18.
    assert tts.gain_db == -18.0


@pytest.mark.asyncio
async def test_silence_at_master_zero():
    cam = _FakeCamilla(volume_db=0.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    assert tts.gain_db == -8.0


# --- music-playing path ---------------------------------------------------

@pytest.mark.asyncio
async def test_music_targets_rms_plus_headroom():
    """Music playing: gain = windowed_rms + headroom - gemini_peak,
    capped at ceiling. With master=-5, music_rms=-30, headroom=12,
    gemini_peak=-3:
      target = -30 + 12 - (-3) = -15
      ceiling = -5 + -8 = -13
      result = min(-15, -13) = -15
    """
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-30.0, -30.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, headroom_db=12.0)
    await tracker.apply_now()
    assert tts.gain_db == -15.0


@pytest.mark.asyncio
async def test_loud_music_clamped_only_by_hearing_safety_cap():
    """When music is loud enough that the formula would push TTS
    above MAX_TTS_GAIN_DB, only the hearing-safety cap binds —
    NOT the master+offset ceiling (that one's a silence-fallback
    backstop, not applicable while we're actively measuring music).
      master=-5, music_rms=-10 (loud), headroom=12, tts_peak=-3
      target = -10 + 12 - (-3) = 5  → way above MAX
      MAX_TTS_GAIN_DB = -6 in TtsPlayout
      result = -6   (NOT -13, the old ceiling-binds value)
    """
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-10.0, -10.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


@pytest.mark.asyncio
async def test_quiet_music_targets_match_actual_level():
    """The whole point of the playback_rms approach: AirPlay sender at
    50% leaves music at e.g. -36 dBFS even though master=-5. Then:
      target = -36 + 12 - (-3) = -21
      ceiling = -5 + -8 = -13
      result = min(-21, -13) = -21.   (TTS quieter than ceiling)
    """
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-36.0, -36.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    assert tts.gain_db == -21.0


@pytest.mark.asyncio
async def test_loud_music_at_low_master_matches_music_not_master():
    """REGRESSION (the user-reported bug): loud music plays at
    low/medium master_volume — e.g. external-amp setup where the
    user has cranked main_volume to compensate for a quiet amp, OR
    AirPlay carrying loud music at listening_level 50%.

    Old behavior: master+offset ceiling clamped TTS to track master,
    leaving TTS several dB QUIETER than the music the tracker
    measured. Subjectively "voice significantly quieter than music."

    New behavior: tracker matches measured music; only the hearing-
    safety MAX cap binds.

      master=-25, music_rms=-15 (loud source), headroom=12, peak=-3
      target = -15 + 12 - (-3) = 0
      MAX_TTS_GAIN_DB = -6   (NOT the old ceiling at -33)
      result = -6.
    """
    cam = _FakeCamilla(volume_db=-25.0, playback_rms=(-15.0, -15.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


@pytest.mark.asyncio
async def test_uses_max_of_left_right():
    """If channels differ (a panned-mono source, broken hardware),
    we use the louder channel — under-shooting risks loud TTS over
    the actually-loud channel."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-50.0, -20.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    # max(-50, -20) = -20; -20 + 12 - (-3) = -5; MAX cap at -6 binds.
    assert tts.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


# --- mute / failure paths -------------------------------------------------

@pytest.mark.asyncio
async def test_mute_goes_to_floor():
    cam = _FakeCamilla(volume_db=0.0, muted=True, playback_rms=(-15.0, -15.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    assert tts.gain_db == TtsPlayout.MIN_TTS_GAIN_DB


@pytest.mark.asyncio
async def test_volume_read_failure_falls_safe():
    cam = _FakeCamilla(volume_db=0.0, playback_rms=(-30.0, -30.0))
    cam.fail_volume = True
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
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
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    assert tts.gain_db == -18.0  # -10 + -8


# --- windowed peak --------------------------------------------------------

@pytest.mark.asyncio
async def test_windowed_peak_holds_through_quiet_passages():
    """A song's quiet bridge between loud chorus sections shouldn't
    pull TTS up. The windowed peak is the max RMS over the window."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-15.0, -15.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, window_sec=10.0)
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
    tracker = _tracker(cam, tts, offset_db=-8.0)
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
    tracker = _tracker(cam, tts, offset_db=-8.0)
    tracker.POLL_INTERVAL_SEC = 0.01  # type: ignore[misc]
    tracker.pause()
    await tracker.start()
    await asyncio.sleep(0.03)
    tracker.resume()
    await asyncio.sleep(0.05)
    await tracker.stop()
    # silence fallback: -20 + -8 = -28
    assert tts.gain_db == -28.0


# --- gain quantization (smooths log spam) --------------------------------

@pytest.mark.asyncio
async def test_target_quantized_to_1db():
    """1 dB quantization keeps the log clean and avoids micro-
    adjustments below the human JND for loudness change (~3 dB)."""
    cam = _FakeCamilla(volume_db=-10.4, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    # -10.4 + -8 = -18.4 → round to nearest int = -18
    assert tts.gain_db == -18.0


# --- loudness anchor branch ----------------------------------------------

@pytest.mark.asyncio
async def test_anchor_used_during_silence():
    """No music currently playing, but we have an anchor → use it.
    This is the iPhone-disconnect fix: anchor=-30, headroom=12,
    gemini=-3 → target = -30 + 12 - (-3) = -15.
    master=-5, ceiling=-13. min(-15, -13) = -15."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, initial_anchor_dbfs=-30.0)
    await tracker.apply_now()
    assert tts.gain_db == -15.0


@pytest.mark.asyncio
async def test_anchor_capped_by_master_ceiling():
    """Even with a loud anchor, master_volume + offset ALWAYS binds
    as the absolute upper bound. anchor=-15 (loud) would compute
    target=0; ceiling=master+offset=-25-8=-33 binds."""
    cam = _FakeCamilla(volume_db=-25.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, initial_anchor_dbfs=-15.0)
    await tracker.apply_now()
    assert tts.gain_db == -33.0


@pytest.mark.asyncio
async def test_anchor_iphone_at_low_scenario():
    """The motivating scenario: master at 60% (-20), iPhone at 20%.
    Music played at -42 dBFS RMS; anchor caught that. Music stops.
    User asks Jarvis: target = -42+12+3 = -27. Ceiling = -20-8 = -28.
    min(-27, -28) = -28 → ceiling binds (anchor would push slightly
    above ceiling). With even quieter anchor, anchor would bind."""
    cam = _FakeCamilla(volume_db=-20.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, initial_anchor_dbfs=-42.0)
    await tracker.apply_now()
    assert tts.gain_db == -28.0


@pytest.mark.asyncio
async def test_anchor_quieter_than_ceiling():
    """Anchor at -50 dBFS (very quiet music at iPhone=10%).
    target = -50+12+3 = -35. ceiling = -5-8 = -13.
    min(-35, -13) = -35. TTS plays at -35 dB."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, initial_anchor_dbfs=-50.0)
    await tracker.apply_now()
    assert tts.gain_db == -35.0


@pytest.mark.asyncio
async def test_anchor_updates_when_music_plays():
    """While music is playing, anchor follows windowed RMS so it
    stays current. After music stops, anchor stays at last value."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-30.0, -30.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, initial_anchor_dbfs=-60.0)
    # Music playing — anchor should update from -60 to -30.
    await tracker.apply_now()
    assert tracker._anchor_dbfs == -30.0  # type: ignore[attr-defined]
    # Music stops — anchor stays at -30.
    cam.playback_rms = (-80.0, -80.0)
    await tracker.apply_now()
    assert tracker._anchor_dbfs == -30.0  # type: ignore[attr-defined]
    # Silence-fallback uses anchor: target = -30+12+3 = -15.
    # ceiling = -5-8 = -13. min(-15, -13) = -15.
    assert tts.gain_db == -15.0


@pytest.mark.asyncio
async def test_anchor_persists_during_silence_doesnt_decay():
    """The anchor must not decay or get reset just because music
    paused. It only updates UPWARD (in the sense of getting refreshed)
    when actual music is detected."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-25.0, -25.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, initial_anchor_dbfs=-60.0)
    await tracker.apply_now()  # music → anchor = -25
    cam.playback_rms = (-80.0, -80.0)
    for _ in range(5):
        await tracker.apply_now()
    # 5 silent polls — anchor unchanged.
    assert tracker._anchor_dbfs == -25.0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_no_anchor_recorded_falls_back_to_master_offset():
    """When initial_anchor_dbfs is the sentinel -120 (effectively
    'no anchor'), silence-fallback reverts to legacy master+offset.
    Defensive backstop in case the persisted anchor was never set."""
    cam = _FakeCamilla(volume_db=-10.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, initial_anchor_dbfs=-120.0)
    await tracker.apply_now()
    assert tts.gain_db == -18.0  # -10 + -8


@pytest.mark.asyncio
async def test_anchor_does_not_update_during_silence():
    """Sanity: silent reading must not become the new anchor.
    Only readings above silence_threshold_dbfs count."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0, initial_anchor_dbfs=-30.0)
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
      - headroom = 16 dB (env default), tts_peak = -3 dB, offset = 0

    Under the old "ceiling = master+offset clamps everything" rule:
      target = -26 + 16 - (-3) = -7
      ceiling = -15 + 0 = -15
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
        offset_db=0.0,        # production default
        headroom_db=16.0,     # production default
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

    Before fix this passed since -3 < ceiling (-3). The point of
    this test is to lock in that the external-amp case keeps
    working with offset_db = 0 (the production default).
    """
    cam = _FakeCamilla(volume_db=-3.0, playback_rms=(-22.0, -22.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=0.0, headroom_db=16.0)
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
            cam, tts, offset_db=0.0, headroom_db=16.0,
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
    """The legitimate use case for the master+offset ceiling, kept
    intentionally: user played loud music at high volume yesterday
    (anchor = -10 dBFS, near peak). Today the room is quiet and
    main_volume is at -40 dB (bedroom level). Without the ceiling
    the anchor would project to a loud TTS. With the ceiling kept
    on the silence branch, TTS stays at master-volume-appropriate
    level.

      master=-40, anchor=-10, no music currently playing
      anchor target = -10 + 12 - (-3) = 5  → would be loud
      ceiling = -40 + 0 = -40
      min(5, -40) = -40 → quiet bedroom respected.
    """
    cam = _FakeCamilla(volume_db=-40.0, playback_rms=(-80.0, -80.0))
    tts = _tts()
    tracker = _tracker(
        cam, tts, offset_db=0.0, headroom_db=12.0,
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
        cam, tts, offset_db=0.0, headroom_db=16.0,
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
        "anchor_dbfs=-26.0",     # anchor (just updated from windowed)
        "main_volume_db=-15.0",  # CamillaDSP master at the moment
        "offset_db=0.0",         # the deprecated knob's value
        "ceiling_db=-15.0",      # main_volume + offset (silence-only)
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
        cam, tts, offset_db=0.0, headroom_db=16.0,
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
        cam, tts, offset_db=0.0, headroom_db=16.0,
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
