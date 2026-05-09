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
from pathlib import Path

logger = logging.getLogger(__name__)


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
