# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Analytic low-frequency alignment responses."""
from __future__ import annotations

import math

import numpy as np


def _positive_finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def lt_boost_db(f0_hz: float, fp_hz: float) -> float:
    """40*log10(f0/fp). Positive when extending (fp < f0)."""

    f0 = _positive_finite("f0_hz", f0_hz)
    fp = _positive_finite("fp_hz", fp_hz)
    return 40.0 * math.log10(f0 / fp)


def linkwitz_transform_params(f0_hz, q0, fp_hz, qp) -> dict:
    """Return an exact CamillaDSP LinkwitzTransform parameter dict."""

    f0 = _positive_finite("f0_hz", f0_hz)
    fp = _positive_finite("fp_hz", fp_hz)
    q0 = _positive_finite("q0", q0)
    qp = _positive_finite("qp", qp)
    if fp > f0:
        raise ValueError("fp_hz must not exceed f0_hz")
    if not 0.3 <= q0 <= 1.2 or not 0.3 <= qp <= 1.2:
        raise ValueError("q values must be between 0.3 and 1.2")
    return {
        "type": "LinkwitzTransform",
        "freq_act": f0,
        "q_act": q0,
        "freq_target": fp,
        "q_target": qp,
    }


def _frequency_grid(freqs_hz: np.ndarray) -> np.ndarray:
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    if freqs.ndim != 1 or not np.all(np.isfinite(freqs)) or np.any(freqs < 0.0):
        raise ValueError("freqs_hz must be a finite non-negative 1-D array")
    return freqs


def _second_order_denominator(freqs_hz: np.ndarray, f0_hz: float, q: float) -> np.ndarray:
    w = 2.0 * np.pi * freqs_hz
    w0 = 2.0 * np.pi * f0_hz
    return np.sqrt((w0 * w0 - w * w) ** 2 + (w0 * w / q) ** 2)


def second_order_highpass_db(freqs_hz: np.ndarray, f0_hz, q) -> np.ndarray:
    """Return an analog-prototype second-order high-pass magnitude in dB."""

    freqs = _frequency_grid(freqs_hz)
    f0 = _positive_finite("f0_hz", f0_hz)
    q = _positive_finite("q", q)
    w = 2.0 * np.pi * freqs
    magnitude = w * w / _second_order_denominator(freqs, f0, q)
    return 20.0 * np.log10(np.maximum(magnitude, 1e-300))


def lt_response_db(freqs_hz, f0_hz, q0, fp_hz, qp) -> np.ndarray:
    """Return the analytic Linkwitz-Transform magnitude in dB."""

    freqs = _frequency_grid(freqs_hz)
    params = linkwitz_transform_params(f0_hz, q0, fp_hz, qp)
    numerator = _second_order_denominator(
        freqs, params["freq_act"], params["q_act"]
    )
    denominator = _second_order_denominator(
        freqs, params["freq_target"], params["q_target"]
    )
    return 20.0 * np.log10(np.maximum(numerator / denominator, 1e-300))


def butterworth_highpass_db(freqs_hz, corner_hz, order: int) -> np.ndarray:
    """Return an analog Butterworth high-pass magnitude in dB."""

    freqs = _frequency_grid(freqs_hz)
    corner = _positive_finite("corner_hz", corner_hz)
    if type(order) is not int or order <= 0:
        raise ValueError("order must be a positive integer")
    ratio = np.divide(
        corner,
        freqs,
        out=np.full_like(freqs, np.inf),
        where=freqs > 0.0,
    )
    magnitude = 1.0 / np.sqrt(1.0 + ratio ** (2 * order))
    return 20.0 * np.log10(np.maximum(magnitude, 1e-300))


def boost_headroom_db(
    target_chain_db: np.ndarray,
    natural_chain_db: np.ndarray,
) -> float:
    """Return max(target - natural) over the grid, floored at zero."""

    target = np.asarray(target_chain_db, dtype=np.float64)
    natural = np.asarray(natural_chain_db, dtype=np.float64)
    if target.shape != natural.shape or target.size == 0:
        raise ValueError("target and natural chains must have the same non-empty shape")
    delta = target - natural
    if not np.all(np.isfinite(delta)):
        raise ValueError("chain responses must be finite")
    return max(0.0, float(np.max(delta)))


def peaking_response_db(freqs_hz, f0_hz, q, gain_db) -> np.ndarray:
    """Return an RBJ peaking-EQ analog-prototype magnitude in dB."""

    freqs = _frequency_grid(freqs_hz)
    f0 = _positive_finite("f0_hz", f0_hz)
    q = _positive_finite("q", q)
    gain = float(gain_db)
    if not math.isfinite(gain):
        raise ValueError("gain_db must be finite")
    normalized = freqs / f0
    amplitude = 10.0 ** (gain / 40.0)
    numerator = np.sqrt(
        (1.0 - normalized * normalized) ** 2
        + (amplitude * normalized / q) ** 2
    )
    denominator = np.sqrt(
        (1.0 - normalized * normalized) ** 2
        + (normalized / (amplitude * q)) ** 2
    )
    return 20.0 * np.log10(np.maximum(numerator / denominator, 1e-300))


def low_shelf_response_db(freqs_hz, f0_hz, q, gain_db) -> np.ndarray:
    """Return an RBJ low-shelf analog-prototype magnitude in dB."""

    freqs = _frequency_grid(freqs_hz)
    f0 = _positive_finite("f0_hz", f0_hz)
    q = _positive_finite("q", q)
    gain = float(gain_db)
    if not math.isfinite(gain):
        raise ValueError("gain_db must be finite")
    normalized = freqs / f0
    amplitude = 10.0 ** (gain / 40.0)
    damping = math.sqrt(amplitude) * normalized / q
    numerator = amplitude * np.sqrt(
        (amplitude - normalized * normalized) ** 2 + damping * damping
    )
    denominator = np.sqrt(
        (1.0 - amplitude * normalized * normalized) ** 2
        + damping * damping
    )
    return 20.0 * np.log10(np.maximum(numerator / denominator, 1e-300))
