"""Tests for TtsVolumeTracker.

Critical paths:
- silence (no music): falls back to legacy `main_volume + offset` formula
- music playing: targets `windowed_rms + headroom - GEMINI_PEAK`
- master_volume + offset is an absolute ceiling — playback_rms can
  only quiet TTS, never make it louder than master suggests
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

    async def get_volume_and_mute(self) -> tuple[float, bool]:
        self.calls_volume += 1
        if self.fail_volume:
            raise RuntimeError("camilla volume read failed")
        return self.volume_db, self.muted

    async def get_playback_rms(self) -> tuple[float, float]:
        self.calls_levels += 1
        if self.fail_levels:
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
) -> TtsVolumeTracker:
    return TtsVolumeTracker(
        cam, tts,
        offset_db=offset_db,
        music_headroom_db=headroom_db,
        silence_threshold_dbfs=silence_threshold_dbfs,
        music_window_sec=window_sec,
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
async def test_loud_music_clamped_to_master_ceiling():
    """When music is loud enough that the formula would push TTS
    above master+offset, the ceiling binds.
      master=-5, music_rms=-10 (loud), headroom=12, gemini_peak=-3
      target = -10 + 12 - (-3) = 5  → above MAX_TTS_GAIN_DB!
      ceiling = -5 + -8 = -13
      result = min(5, -13) = -13.
    """
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-10.0, -10.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    assert tts.gain_db == -13.0


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
async def test_master_ceiling_holds_when_master_is_quiet():
    """User has lowered master to -25 even though music plays loud.
      master=-25, music_rms=-15, headroom=12, gemini_peak=-3
      target = -15 + 12 - (-3) = 0  → would clip
      ceiling = -25 + -8 = -33
      result = -33.
    Master_volume is ALWAYS the upper bound."""
    cam = _FakeCamilla(volume_db=-25.0, playback_rms=(-15.0, -15.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    assert tts.gain_db == -33.0


@pytest.mark.asyncio
async def test_uses_max_of_left_right():
    """If channels differ (a panned-mono source, broken hardware),
    we use the louder channel — under-shooting risks loud TTS over
    the actually-loud channel."""
    cam = _FakeCamilla(volume_db=-5.0, playback_rms=(-50.0, -20.0))
    tts = _tts()
    tracker = _tracker(cam, tts, offset_db=-8.0)
    await tracker.apply_now()
    # max(-50, -20) = -20; -20 + 12 - (-3) = -5; ceiling -13 → -13.
    assert tts.gain_db == -13.0


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
    # Loud chorus first.
    await tracker.apply_now()
    assert tts.gain_db == -13.0  # ceiling-bound on loud music
    # Quiet passage.
    cam.playback_rms = (-60.0, -60.0)
    await tracker.apply_now()
    # Windowed peak still has the -15 reading, so target unchanged.
    assert tts.gain_db == -13.0


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
