# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room compatibility wrapper for the neutral acoustic playback core.

Room keeps its historical cache and ALSA defaults here:

    WAV -> correction_substream -> jasper-fanin -> CamillaDSP -> outputd

The shared leaf owns only process/tone mechanics.  Existing Room and temporary
cross-feature imports keep their signatures, paths, and exception behavior while
feature owners migrate to explicit neutral arguments.
"""

from __future__ import annotations

from pathlib import Path

from jasper.audio_measurement.playback import (
    PlaybackCleanupState as PlaybackCleanupState,
    PlaybackError as PlaybackError,
    PlaybackFailureCode as PlaybackFailureCode,
    PlaybackResult as PlaybackResult,
    SweepPlaybackError as SweepPlaybackError,
    TonePlayer as _SharedTonePlayer,
    ensure_sine_wav,
    play_wav,
)


DEFAULT_TONE_DIR = Path("/var/lib/jasper/correction/tones")
DEFAULT_ALSA_DEVICE = "correction_substream"


def _raise_legacy_error(error: PlaybackError, *, missing_label: str) -> None:
    if error.code is PlaybackFailureCode.MISSING_FILE:
        raise FileNotFoundError(f"{missing_label} not found: {error.wav_path}") from error
    if error.code is PlaybackFailureCode.START_FAILED and isinstance(
        error.__cause__, OSError
    ):
        raise error.__cause__
    raise error


async def play_sweep(
    wav_path: str | Path,
    *,
    alsa_device: str = DEFAULT_ALSA_DEVICE,
    timeout_s: float = 30.0,
) -> None:
    """Play one sweep through Room's stable measurement fan-in lane."""

    try:
        await play_wav(
            wav_path,
            alsa_device=alsa_device,
            timeout_s=timeout_s,
        )
    except PlaybackError as error:
        _raise_legacy_error(error, missing_label="sweep WAV")


def _ensure_tone_wav(
    *,
    freq_hz: float,
    duration_s: float,
    dbfs: float,
    sample_rate: int,
    cache_dir: Path = DEFAULT_TONE_DIR,
) -> Path:
    """Generate a Room tone while retaining its shipped cache path."""

    return ensure_sine_wav(
        freq_hz=freq_hz,
        duration_s=duration_s,
        dbfs=dbfs,
        sample_rate=sample_rate,
        cache_dir=cache_dir,
    )


class TonePlayer(_SharedTonePlayer):
    """Room-compatible continuous player with the historical ALSA default."""

    def __init__(
        self,
        wav_path: str | Path,
        *,
        alsa_device: str = DEFAULT_ALSA_DEVICE,
    ) -> None:
        super().__init__(wav_path, alsa_device=alsa_device)

    async def play(self) -> None:
        try:
            await super().play()
        except PlaybackError as error:
            _raise_legacy_error(error, missing_label="tone WAV")


async def play_test_tone(
    *,
    freq_hz: float = 1000.0,
    duration_s: float = 5.0,
    dbfs: float = -18.0,
    sample_rate: int = 48000,
    alsa_device: str = DEFAULT_ALSA_DEVICE,
    cache_dir: Path = DEFAULT_TONE_DIR,
) -> None:
    """Play Room's standalone level-check tone through the music chain."""

    wav_path = _ensure_tone_wav(
        freq_hz=freq_hz,
        duration_s=duration_s,
        dbfs=dbfs,
        sample_rate=sample_rate,
        cache_dir=cache_dir,
    )
    await play_sweep(
        wav_path,
        alsa_device=alsa_device,
        timeout_s=duration_s + 5.0,
    )
