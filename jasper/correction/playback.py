"""Sweep playback through the JTS music chain.

Plays a sweep WAV via `aplay -D correction_substream`, a dedicated
fan-in lane for correction/test audio. This puts the sweep on the
SAME signal path music takes without borrowing any renderer's private
lane:

    sweep WAV → correction_substream → snd-aloop lane 4
              → jasper-fanin → pcm.jasper_capture
              → jasper-camilla (main_volume + correction filters)
              → outputd_content_playback → jasper-outputd
              → dongle → amp → speakers

That's load-bearing: a measurement that bypassed CamillaDSP would
not reflect the chain music actually goes through, so the
correction filter we generated wouldn't act on a path that matched
what we measured. The Phase 0 page advice ("rotate phone, lay
flat, no case") plus this same-path injection ensures the IR we
recover is the real listening-position-to-room-to-mic transfer
function.

Why aplay subprocess, not sounddevice: we want the sweep to enter
at a stable named ALSA entry point that jasper-fanin consumes
(`correction_substream` — see docs/audio-paths.md). sounddevice's
PortAudio backend abstracts ALSA devices and would either go through
the default device or require an exact device-name match that varies
by Trixie ALSA generation; aplay is the canonical tool for "play
this WAV on this device" and is already installed everywhere.
"""
from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


# Test-tone WAV cache. Generated once per (freq, duration, dbfs) tuple
# and re-used across measurements.
DEFAULT_TONE_DIR = Path("/var/lib/jasper/correction/tones")


# Default ALSA device the sweep is played to. Dedicated fan-in input
# lane for correction/test audio (see docs/audio-paths.md).
DEFAULT_ALSA_DEVICE = "correction_substream"


class SweepPlaybackError(RuntimeError):
    """aplay returned non-zero or the subprocess timed out."""


async def play_sweep(
    wav_path: str | Path,
    *,
    alsa_device: str = DEFAULT_ALSA_DEVICE,
    timeout_s: float = 30.0,
) -> None:
    """Play a sweep WAV via `aplay -D <device>`.

    Async — uses `asyncio.create_subprocess_exec` so the caller's
    event loop can do other work (capture-state polling, SSE
    progress events to the browser) while aplay drains the WAV.
    Returns when aplay exits cleanly. Raises SweepPlaybackError on
    non-zero exit or timeout.

    Args:
      wav_path: path to the sweep WAV. Must exist and be readable
        by the calling process.
      alsa_device: ALSA device target. Default is correction_substream,
        which puts the sweep into the music chain. Override for
        tests / hardware experiments.
      timeout_s: hard kill if aplay doesn't finish in this window.
        Generous — a 10 s sweep takes 10 s to play; 30 s leaves
        plenty for setup + flush.
    """
    wav_path = Path(wav_path)
    if not wav_path.exists():
        raise FileNotFoundError(f"sweep WAV not found: {wav_path}")

    # `-q` quiets aplay's "Playing WAV..." banner so the journal isn't
    # spammed every measurement. Errors still go to stderr.
    proc = await asyncio.create_subprocess_exec(
        "aplay", "-D", alsa_device, "-q", str(wav_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        # Don't leave a runaway aplay attached to the loopback —
        # would block the next measurement until the kernel reaped
        # it.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise SweepPlaybackError(
            f"aplay timed out after {timeout_s} s playing {wav_path}"
        )
    if proc.returncode != 0:
        msg = stderr.decode(errors="replace").strip()
        raise SweepPlaybackError(
            f"aplay failed (rc={proc.returncode}, device={alsa_device}): {msg}"
        )
    logger.info(
        "sweep played: %s → %s (%d bytes stderr)",
        wav_path, alsa_device, len(stderr or b""),
    )


def _ensure_tone_wav(
    *,
    freq_hz: float,
    duration_s: float,
    dbfs: float,
    sample_rate: int,
    cache_dir: Path = DEFAULT_TONE_DIR,
) -> Path:
    """Generate (and cache on disk) a sine-tone WAV. Used by the
    "test speaker volume" pre-measurement step so the user can
    adjust their amp before running a sweep.

    Generation is deterministic per (freq, duration, dbfs,
    sample_rate) tuple — re-use the cached file on subsequent calls.
    Same fade-in/out shape as the sweep so the tone doesn't click
    the speaker at the boundaries.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    wav_path = cache_dir / (
        f"tone_{int(freq_hz)}Hz_{int(duration_s * 1000)}ms_"
        f"{int(abs(dbfs) * 10)}dbm_{sample_rate}Hz.wav"
    )
    if wav_path.exists():
        return wav_path

    n = int(round(duration_s * sample_rate))
    amp = 10 ** (dbfs / 20.0)
    t = np.arange(n, dtype=np.float64) / sample_rate
    sig = amp * np.sin(2 * math.pi * freq_hz * t)

    fade = max(8, int(0.005 * sample_rate))
    if fade * 2 < n:
        sig[:fade] *= np.linspace(0.0, 1.0, fade) ** 2
        sig[-fade:] *= np.linspace(1.0, 0.0, fade) ** 2

    from scipy.io import wavfile
    int16 = (np.clip(sig, -1.0, 1.0) * 32767.0).astype(np.int16)
    wavfile.write(str(wav_path), sample_rate, int16)
    logger.info(
        "test tone cached: %s (%.0f Hz, %.1f s, %.1f dBFS)",
        wav_path, freq_hz, duration_s, dbfs,
    )
    return wav_path


async def play_test_tone(
    *,
    freq_hz: float = 1000.0,
    duration_s: float = 5.0,
    dbfs: float = -18.0,
    sample_rate: int = 48000,
    alsa_device: str = DEFAULT_ALSA_DEVICE,
    cache_dir: Path = DEFAULT_TONE_DIR,
) -> None:
    """Play a single sine tone through the music chain. Used for
    debugging / standalone level checks; the autolevel flow uses
    `TonePlayer` instead (continuous + cancellable).
    """
    wav_path = _ensure_tone_wav(
        freq_hz=freq_hz, duration_s=duration_s, dbfs=dbfs,
        sample_rate=sample_rate, cache_dir=cache_dir,
    )
    await play_sweep(
        wav_path, alsa_device=alsa_device,
        timeout_s=duration_s + 5.0,
    )


class TonePlayer:
    """Cancellable continuous tone player.

    Plays a tone WAV via `aplay` and supports early termination via
    `cancel()`. Used by the autolevel flow: the WAV is long (15 s
    of safety) and we kill aplay the moment the autolevel loop
    decides we're done.

    Why aplay-and-kill rather than streaming via sounddevice: same
    ALSA path the sweep + music use (via jasper-fanin). Keeps everything
    going through CamillaDSP so main_volume changes during the ramp
    apply to the tone in real time.
    """

    def __init__(
        self,
        wav_path: str | Path,
        *,
        alsa_device: str = DEFAULT_ALSA_DEVICE,
    ) -> None:
        self._wav_path = Path(wav_path)
        self._alsa_device = alsa_device
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False

    async def play(self) -> None:
        """Block until aplay exits naturally OR `cancel()` is called.
        Doesn't raise on cancel — caller checks `cancelled` to
        distinguish a normal end-of-file from a deliberate stop."""
        if not self._wav_path.exists():
            raise FileNotFoundError(
                f"tone WAV not found: {self._wav_path}"
            )
        self._proc = await asyncio.create_subprocess_exec(
            "aplay", "-D", self._alsa_device, "-q", str(self._wav_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await self._proc.communicate()
        except asyncio.CancelledError:
            self.cancel()
            # Drain so the proc reaps cleanly.
            try:
                await self._proc.wait()
            except Exception:  # noqa: BLE001
                pass
            raise
        logger.info(
            "tone player exit: %s (cancelled=%s rc=%s)",
            self._wav_path, self._cancelled, self._proc.returncode,
        )

    def cancel(self) -> None:
        """Stop playback. Safe to call from any thread (no event-
        loop interaction)."""
        self._cancelled = True
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancelled
