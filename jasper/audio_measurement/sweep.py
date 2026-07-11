# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synchronized swept-sine (ESS) generation per Novak et al. 2015.

Why synchronized rather than vanilla Farina ESS: with a synchronized
sweep, harmonic-distortion impulses fall at integer-fraction offsets
of the linear IR location, making them trivial to discard during
deconvolution. Same number of lines as the vanilla form; uniformly
better. See `JAES 61(7) — Synchronized Swept-Sine: Theory,
Application, and Implementation` (Novak, Lotton, Simon).

The sweep is generated on the Pi at the playback sample rate
(48 kHz, matching CamillaDSP). Saved as 16-bit S16_LE WAV so
`aplay -D correction_substream` can consume it directly. Stored to
disk because the sweep is deterministic per (f1, f2, duration,
sample_rate) tuple — no point regenerating on every measurement.

The deconvolution path (jasper.audio_measurement.deconv) does NOT require a
separately-generated inverse filter. We do FFT-based regularized
inversion of the sweep at IR-extract time, which is more
numerically stable than a precomputed inverse and avoids the
amplitude-normalization gymnastics. So this module emits the sweep
only, plus its metadata.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .excitation import AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepMeta:
    """Everything the deconvolution path needs to recover the IR plus
    everything a future analyst would want to know about the sweep
    that produced their data. Persisted alongside the sweep WAV in
    the same directory."""
    f1: float
    f2: float
    L: float
    duration_s: float
    n_samples: int
    sample_rate: int
    amplitude_dbfs: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "f1": self.f1, "f2": self.f2, "L": self.L,
            "duration_s": self.duration_s,
            "n_samples": self.n_samples,
            "sample_rate": self.sample_rate,
            "amplitude_dbfs": self.amplitude_dbfs,
        }


def synchronized_swept_sine(
    f1: float = 20.0,
    f2: float = 20000.0,
    duration_approx_s: float = 10.0,
    sample_rate: int = 48000,
    amplitude_dbfs: float = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
) -> tuple[np.ndarray, SweepMeta]:
    """Generate a synchronized exponential swept-sine.

    Args:
      f1: start frequency (Hz). Default 20 — anything lower wastes
        sweep duration on inaudible content.
      f2: end frequency (Hz). Default 20000 (Nyquist at 48 kHz with
        0.83x margin). f2 must be < sample_rate / 2.
      duration_approx_s: target sweep duration. The actual duration
        is rounded slightly so the sweep has an integer number of
        cycles at f1 (Novak's "synchronization" condition).
      sample_rate: playback rate. Pin 48 kHz to match CamillaDSP.
      amplitude_dbfs: peak amplitude (dBFS). -12 keeps headroom for
        the renderer / DSP chain and avoids any clipping at the
        loudest peaks of the sweep window.

    Returns:
      (sweep, meta). `sweep` is float32 in [-amp, amp] where
      amp = 10**(amplitude_dbfs/20). `meta` carries the exact
      duration / L / etc. needed by deconvolution.
    """
    if f1 <= 0:
        raise ValueError(f"f1 must be positive, got {f1}")
    if f2 <= f1:
        raise ValueError(f"f2 ({f2}) must be > f1 ({f1})")
    if f2 >= sample_rate / 2:
        raise ValueError(
            f"f2 ({f2}) must be < Nyquist ({sample_rate / 2}); "
            f"increase sample_rate or lower f2"
        )

    # Novak's synchronization condition. Choose L (rate constant)
    # such that the sweep starts at a zero-crossing of f1 and the
    # number of cycles at f1 is an integer. This makes harmonic-
    # impulse offsets predictable. Derivation: integrate
    # phase(t) = 2π f1 L (exp(t/L) - 1) over t ∈ [0, T] where
    # T = L * ln(f2/f1) — choose L so that T is an integer
    # multiple of 1/f1. Equivalently, n_cycles_at_f1 = T*f1 must be
    # an integer.
    L_initial = duration_approx_s / math.log(f2 / f1)
    n_cycles_at_f1 = round(L_initial * f1)
    if n_cycles_at_f1 < 1:
        raise ValueError(
            f"duration_approx_s={duration_approx_s} too short for "
            f"f1={f1} (need at least one cycle at start)"
        )
    L = n_cycles_at_f1 / f1
    duration_s = L * math.log(f2 / f1)
    n_samples = int(round(duration_s * sample_rate))

    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    amp = 10 ** (amplitude_dbfs / 20.0)
    phase = 2 * np.pi * f1 * L * (np.exp(t / L) - 1)
    sweep = amp * np.sin(phase)

    # Light fade-in/out — eliminates the click from a sweep that
    # doesn't quite end at a zero-crossing in float32 precision, and
    # masks any DC offset on the playback chain. 5 ms fade at 48 kHz =
    # 240 samples; trivially short relative to 10-second sweep.
    fade_samples = max(8, int(0.005 * sample_rate))
    if fade_samples * 2 < n_samples:
        fade_in = np.linspace(0.0, 1.0, fade_samples) ** 2
        fade_out = np.linspace(1.0, 0.0, fade_samples) ** 2
        sweep[:fade_samples] *= fade_in
        sweep[-fade_samples:] *= fade_out

    meta = SweepMeta(
        f1=float(f1), f2=float(f2), L=float(L),
        duration_s=float(duration_s),
        n_samples=int(n_samples),
        sample_rate=int(sample_rate),
        amplitude_dbfs=float(amplitude_dbfs),
    )
    return sweep.astype(np.float32), meta


def write_sweep_wav(
    path: str | Path,
    sweep: np.ndarray,
    sample_rate: int,
) -> None:
    """Write a mono float32 sweep as 16-bit PCM WAV (S16_LE).

    Why 16-bit not 32-bit float: the playback chain accepts S16_LE
    fine and the file is half the size, which matters at install
    time when we cache the sweep on disk. The 96 dB dynamic range
    of 16-bit is far more than the sweep itself spans (the sweep
    sits at -12 dBFS, the room dynamic range we measure is ~70 dB).
    """
    from scipy.io import wavfile

    if sweep.ndim != 1:
        raise ValueError(
            f"sweep must be mono (1-D), got shape {sweep.shape}"
        )
    clipped = np.clip(sweep, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    wavfile.write(str(path), sample_rate, int16)


def read_wav_mono(
    path: str | Path,
) -> tuple[np.ndarray, int]:
    """Read a WAV file as mono float32 in [-1, 1].

    Used for both ingesting the captured sweep from the iPhone
    upload AND for unit tests on synthesized fixtures. Auto-handles
    16-bit and 32-bit-float WAVs (the only formats we expect — iOS
    Safari capture is float32, our sweep cache is int16). Stereo
    inputs are downmixed to mono by averaging.
    """
    from scipy.io import wavfile

    sr, data = wavfile.read(str(path))
    # Capture the source dtype BEFORE downmixing: np.mean promotes an
    # integer array to float, so keying the normalization off data.dtype
    # after a stereo mean would skip the integer scaling and leave the
    # signal at ±32767 instead of ±1.0.
    source_dtype = data.dtype
    if data.ndim == 2:
        # Downmix stereo → mono by simple average. We expect mono
        # capture from iOS (channelCount: 1 in getUserMedia), but
        # accept stereo defensively.
        data = data.mean(axis=1)
    # Convert to float32 in [-1, 1] based on the source dtype.
    if np.issubdtype(source_dtype, np.integer):
        max_val = float(np.iinfo(source_dtype).max)
        signal = data.astype(np.float32) / max_val
    else:
        signal = data.astype(np.float32)
    return signal, int(sr)
