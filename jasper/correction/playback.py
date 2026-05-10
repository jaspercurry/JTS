"""Sweep playback through the JTS music chain.

Plays a sweep WAV via `aplay -D plughw:Loopback,0,0` — the same ALSA
device the renderers (librespot, shairport-sync, bluealsa-aplay)
write to. This puts the sweep on the SAME signal path music takes:

    sweep WAV → plughw:Loopback,0,0 → snd-aloop → plughw:Loopback,1,0
              → jasper-camilla (main_volume + correction filters)
              → pcm.jasper_out (dmix on dongle)
              → dongle → amp → speakers

That's load-bearing: a measurement that bypassed CamillaDSP would
not reflect the chain music actually goes through, so the
correction filter we generated wouldn't act on a path that matched
what we measured. The Phase 0 page advice ("rotate phone, lay
flat, no case") plus this same-path injection ensures the IR we
recover is the real listening-position-to-room-to-mic transfer
function.

Why aplay subprocess, not sounddevice: we want the sweep to enter
at the same ALSA entry point music does (`hw:Loopback,0,0`
specifically — see docs/audio-paths.md). sounddevice's PortAudio
backend abstracts ALSA devices and would either go through the
default device or require an exact device-name match that varies
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


# Default ALSA device the sweep is played to. Matches the Loopback
# capture side of the JTS audio path (see docs/audio-paths.md).
DEFAULT_ALSA_DEVICE = "plughw:Loopback,0,0"


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
      alsa_device: ALSA device target. Default is plughw:Loopback,0,0
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
    """Play a single sine tone through the music chain so the user
    can adjust their amp's gain to a comfortable level before running
    a sweep.

    Default 1 kHz / 5 s / -18 dBFS — audible mid-range tone, long
    enough for the user to read the live mic meter and adjust the
    amp knob, loud enough to hear clearly but with headroom.
    """
    wav_path = _ensure_tone_wav(
        freq_hz=freq_hz, duration_s=duration_s, dbfs=dbfs,
        sample_rate=sample_rate, cache_dir=cache_dir,
    )
    await play_sweep(
        wav_path, alsa_device=alsa_device,
        timeout_s=duration_s + 5.0,
    )
