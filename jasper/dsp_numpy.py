# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""numpy stand-ins for the `scipy.signal` calls the resident audio daemons make.

`import scipy.signal` costs ~75 MB RSS and ~800 modules over numpy alone.
The smallest supported box is a 415 MB Pi Zero 2 W, and the mic daemons run
under `jts-mic.slice`, which sets `MemorySwapMax=0` — those pages never leave
RAM (issue #3697).

Every function here reproduces its scipy counterpart on the inputs the daemons
actually feed it: `tests/test_dsp_numpy.py` pins the agreement against scipy
whenever scipy is installed. This is not a general DSP library; it covers the
calls the daemons make, at the rates they make them.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

#: `scipy.signal.resample_poly`'s default anti-imaging window, and the taps it
#: puts either side of the FIR centre per unit of `max(up, down)`.
_KAISER_BETA = 5.0
_HALF_LEN_PER_RATE = 10


@lru_cache(maxsize=8)
def _polyphase_plan(
    up: int, down: int,
) -> tuple[tuple[int, int, np.ndarray[Any, Any]], ...]:
    """Per-phase `(first output index, first tap index, reversed taps)`.

    `scipy.signal.resample_poly(x, up, down)` reduces to

        out[k] == convolve(zero_stuff(x, up), h)[half_len + down * k]

    with `h = firwin(2 * half_len + 1, 1 / max_rate, ("kaiser", 5.0)) * up`.
    Splitting `h` by residue mod `up` drops the multiply-by-zero half: within
    one residue the output indices step by `up` and the tap indices by `down`,
    which is what a caller needs to slice.
    """
    max_rate = max(up, down)
    half_len = _HALF_LEN_PER_RATE * max_rate
    width = 2 * half_len + 1
    offsets = np.arange(width, dtype=np.float64) - half_len
    taps = np.sinc(offsets / max_rate) * np.kaiser(width, _KAISER_BETA)
    # Unity DC gain, then times `up` so zero-stuffing does not drop the level.
    taps *= up / taps.sum()

    plan = []
    for phase in range(up):
        reversed_taps = np.ascontiguousarray(taps[phase::up][::-1])
        # Cached across calls: a stray in-place write would corrupt every
        # later resample on an audio path.
        reversed_taps.setflags(write=False)
        first_out = next(
            k for k in range(up) if (half_len + down * k) % up == phase
        )
        plan.append((first_out, (half_len + down * first_out) // up,
                     reversed_taps))
    return tuple(plan)


def resample_poly(x: Any, up: int, down: int) -> np.ndarray[Any, Any]:
    """Rational resample of a 1-D signal, float64 out.

    Matches `scipy.signal.resample_poly(x, up, down)` — same Kaiser(5.0)
    windowed-sinc taps, same group-delay trim, same zero-padded edges — to
    float64 rounding, so the group delay and passband a caller already ships
    are unchanged. scipy returns the input dtype; this always returns float64.
    """
    common = math.gcd(up, down)
    up //= common
    down //= common
    samples = np.asarray(x, dtype=np.float64)
    if up == down == 1:
        return samples.copy()

    n_in = int(samples.size)
    n_out = -(-(n_in * up) // down)
    out = np.zeros(n_out, dtype=np.float64)
    if n_out == 0:
        return out

    for first_out, first_tap, reversed_taps in _polyphase_plan(up, down):
        if first_out >= n_out:
            continue
        count = -(-(n_out - first_out) // up)
        span = int(reversed_taps.size)
        padded = np.zeros(
            max(first_tap + down * (count - 1) + span, span - 1 + n_in),
            dtype=np.float64,
        )
        padded[span - 1:span - 1 + n_in] = samples
        windows = sliding_window_view(padded, span)[first_tap::down][:count]
        out[first_out::up] = windows @ reversed_taps
    return out


def butter2_highpass_sos(
    cutoff_hz: float, rate_hz: float,
) -> np.ndarray[Any, Any]:
    """One second-order-section row for a 2nd-order Butterworth high-pass.

    Matches `scipy.signal.butter(2, cutoff_hz, btype="highpass", fs=rate_hz,
    output="sos")`: the same bilinear transform of the same analog prototype,
    written out in closed form for the Butterworth `Q = 1 / sqrt(2)`.
    """
    nyquist = 0.5 * rate_hz
    if not 0.0 < cutoff_hz < nyquist:
        raise ValueError(
            f"cutoff {cutoff_hz} Hz must be inside (0, {nyquist}) Hz"
        )
    # Prewarped, so the analog and digital cutoffs coincide at cutoff_hz.
    w0 = 2.0 * math.pi * cutoff_hz / rate_hz
    cos_w0 = math.cos(w0)
    alpha = math.sin(w0) / math.sqrt(2.0)
    a0 = 1.0 + alpha
    b0 = 0.5 * (1.0 + cos_w0) / a0
    return np.array([[
        b0, -2.0 * b0, b0,
        1.0, -2.0 * cos_w0 / a0, (1.0 - alpha) / a0,
    ]], dtype=np.float64)


def sosfilt(
    sos: Any, x: Any, zi: Any,
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """Cascaded biquads carrying state, returning `(filtered, final state)`.

    The textbook transposed-direct-form-II recursion per section, in float64,
    with the `(n_sections, 2)` state layout `scipy.signal.sosfilt` uses, so
    filtering a stream in chunks gives the same samples as filtering it whole
    and a `zi` from either side fits. Sections run outermost here and
    innermost in scipy, which is free to differ because each biquad is an
    independent stage. Agreement with scipy is to float rounding, not to the
    bit: scipy rounds this recursion its own way. Like scipy, each section's
    `a0` (column 3) is assumed to be 1.
    """
    sections = np.asarray(sos, dtype=np.float64)
    state = np.array(zi, dtype=np.float64, copy=True)
    signal = np.asarray(x, dtype=np.float64)
    for index, section in enumerate(sections):
        b0, b1, b2, _a0, a1, a2 = (float(v) for v in section)
        z0, z1 = float(state[index, 0]), float(state[index, 1])
        filtered = []
        for sample in signal.tolist():
            value = b0 * sample + z0
            z0 = b1 * sample - a1 * value + z1
            z1 = b2 * sample - a2 * value
            filtered.append(value)
        signal = np.array(filtered, dtype=np.float64)
        state[index, 0], state[index, 1] = z0, z1
    return signal, state
