"""FFT-based regularized deconvolution for room-impulse extraction.

Given the captured signal y(t) ≈ (h * x)(t) where x is the played
sweep and h is the room IR we want to recover, we deconvolve:

    H(f) = Y(f) * conj(X(f)) / (|X(f)|² + ε)
    h(t) = ifft(H(f))

The Tikhonov regularizer ε keeps the inversion well-conditioned at
frequencies outside the sweep band where |X(f)| → 0. We use a
constant ε proportional to the peak of |X(f)|² rather than the
frequency-dependent ε(f) variants from the literature — for the
20 Hz–20 kHz sweep band that covers everything we care about, the
constant form is robust and keeps the code obvious.

The recovered IR is then trimmed to a window centered on the
direct-arrival peak (argmax of |h(t)|). Default window: 5 ms before
the peak (catches non-causal artifacts from the deconvolution) and
500 ms after (covers domestic-room decay; longer wastes memory and
adds noise from beyond the meaningful reverberation tail).
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_PRE_ARRIVAL_MS = 5.0
DEFAULT_POST_ARRIVAL_MS = 500.0
DEFAULT_EPSILON_RELATIVE = 1e-3


def deconvolve(
    captured: np.ndarray,
    sweep: np.ndarray,
    sample_rate: int,
    *,
    pre_arrival_ms: float = DEFAULT_PRE_ARRIVAL_MS,
    post_arrival_ms: float = DEFAULT_POST_ARRIVAL_MS,
    epsilon_relative: float = DEFAULT_EPSILON_RELATIVE,
) -> np.ndarray:
    """Recover h(t) from y(t) ≈ (h * x)(t) via regularized FFT.

    Args:
      captured: mono float32, the recorded sweep capture (with room
        response baked in).
      sweep: mono float32, the same sweep signal that was played.
        Must be the EXACT signal used at playback time — otherwise
        the deconvolution math is wrong by an unknown filter.
      sample_rate: shared by both signals (we resample at the Python
        layer if iOS Safari handed us something other than 48 kHz,
        before this function is called).
      pre_arrival_ms: how many ms before the peak to include in the
        IR window. Catches non-causal artifacts.
      post_arrival_ms: how many ms after the peak. 500 ms is plenty
        for typical living rooms (RT60 < 1 s).
      epsilon_relative: regularizer as a fraction of peak |X(f)|².
        Smaller = sharper deconvolution but more sensitive to
        capture noise outside the sweep band. 1e-3 is the standard
        Kirkeby value.

    Returns:
      ir (float32): the room impulse response, windowed.
    """
    if captured.ndim != 1 or sweep.ndim != 1:
        raise ValueError(
            f"captured and sweep must be 1-D; got shapes "
            f"{captured.shape} and {sweep.shape}"
        )
    if len(captured) < len(sweep):
        raise ValueError(
            f"captured ({len(captured)} samples) shorter than sweep "
            f"({len(sweep)} samples) — capture too short or "
            f"misaligned"
        )

    # Pad to next power of 2 ≥ len(captured) + len(sweep) for clean
    # linear convolution. (Strictly we only need len(captured), but
    # the extra room avoids circular wraparound at high frequencies.)
    n_pad = 1
    while n_pad < len(captured) + len(sweep):
        n_pad *= 2

    Y = np.fft.rfft(captured, n=n_pad)
    X = np.fft.rfft(sweep, n=n_pad)

    # Tikhonov-regularized inversion. Epsilon scales with peak power
    # so the same relative knob works for sweeps at different
    # amplitudes.
    eps = epsilon_relative * float(np.max(np.abs(X) ** 2))
    H = Y * np.conj(X) / (np.abs(X) ** 2 + eps)
    h_full = np.fft.irfft(H, n=n_pad)

    # Direct-arrival peak. argmax of |h| handles the case where the
    # peak is positive or negative (depends on sweep phase + sign of
    # the sound-pressure → mic-voltage transduction).
    peak_idx = int(np.argmax(np.abs(h_full)))

    pre_samples = max(0, int(round(pre_arrival_ms * sample_rate / 1000)))
    post_samples = max(1, int(round(post_arrival_ms * sample_rate / 1000)))

    start = max(0, peak_idx - pre_samples)
    end = min(len(h_full), peak_idx + post_samples)
    ir = h_full[start:end].astype(np.float32)

    logger.debug(
        "deconv: n_pad=%d peak_idx=%d ir_len=%d pre=%d post=%d eps=%.3g",
        n_pad, peak_idx, len(ir), pre_samples, post_samples, eps,
    )
    return ir


def magnitude_response(
    ir: np.ndarray,
    sample_rate: int,
    *,
    n_fft: int | None = None,
    normalize: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Magnitude response of an impulse response, in dB.

    Args:
      ir: mono float32 IR.
      sample_rate: in Hz.
      n_fft: FFT length. None → next power of 2 ≥ max(8192, len(ir)).
        8192 is the floor because we want enough frequency resolution
        in the bass region (5.86 Hz/bin at 48 kHz with N=8192) for
        meaningful 1/48-octave smoothing later.
      normalize: subtract peak so the response is "0 dB at the
        loudest frequency, negative everywhere else" (the convention
        for a relative magnitude response). Set False to preserve
        absolute deconvolution amplitude.

    Returns:
      (frequencies_hz, magnitude_db). Both 1-D float64.
    """
    if n_fft is None:
        # bit_length of (len-1) is the number of bits to represent
        # len-1; 1 << that is the next power of 2.
        n_fft = max(8192, 1 << (max(len(ir), 1) - 1).bit_length())
    H = np.fft.rfft(ir, n=n_fft)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    magnitude = np.abs(H)
    # Floor before log to avoid -inf at deep nulls.
    magnitude_db = 20 * np.log10(np.maximum(magnitude, 1e-12))
    if normalize:
        magnitude_db = magnitude_db - float(np.max(magnitude_db))
    return freqs.astype(np.float64), magnitude_db.astype(np.float64)
