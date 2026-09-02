# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The gate window's SHAPE, pinned independently of who applies it.

``build_gate_window`` was extracted so a forced-span caller (the gate ladder
in :mod:`~jasper.active_speaker.crossover_v2.gate_sweep`) and the shipped
gate cannot drift apart. These tests pin the shape itself and the property
that matters at the seam: the extraction changed nothing about what
``gate_impulse_response`` applies.
"""

from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement.gating import (
    TAPER_FRACTION,
    build_gate_window,
    gate_impulse_response,
)

RATE = 48_000


def _synthetic_ir(
    *, peak_idx: int = 480, reflection_idx: int = 720, length: int = 4800
) -> np.ndarray:
    """Direct arrival, a baffle-scale early copy, a reflection, and a tail.

    The early copy sits 0.25 ms after the peak — inside the window and below
    :data:`~jasper.audio_measurement.gating.SEARCH_T_MIN_MS`, so it is never
    gated out — which is what gives the gated spectrum structure for a
    numeric pin to be about.
    """
    ir = np.zeros(length, dtype=np.float32)
    ir[peak_idx] = 1.0
    ir[peak_idx + 12] = 0.35
    ir[reflection_idx] = 0.30
    tail = np.arange(length - peak_idx)
    ir[peak_idx:] += (0.05 * np.exp(-tail / 400.0)).astype(np.float32)
    return ir


@pytest.mark.parametrize("span", [1, 17, 240, 960])
@pytest.mark.parametrize("taper_fraction", [0.0, 0.15, TAPER_FRACTION, 1.0])
def test_window_is_unity_head_flat_plateau_and_half_hann_tail(
    span: int, taper_fraction: float
) -> None:
    """The declared shape, at every span and taper the callers use."""
    n, peak = 2048, 512
    window = build_gate_window(
        n, peak_idx=peak, span=span, taper_fraction=taper_fraction
    )
    end = peak + span
    taper_len = max(1, int(round(taper_fraction * span)))
    flat_end = max(peak, end - taper_len)

    assert window.dtype == np.float64
    assert window.shape == (n,)
    # Rectangular head, all the way to index 0 when no lead is asked for.
    assert np.all(window[: flat_end + 1] == 1.0)
    # Half-Hann tail: starts at 1, ends at 0, never rises.
    tail = window[flat_end : end + 1]
    assert tail[0] == pytest.approx(1.0)
    assert tail[-1] == pytest.approx(0.0, abs=1e-12)
    assert np.all(np.diff(tail) <= 1e-12)
    assert np.all(window[end + 1 :] == 0.0)


def test_lead_bounds_the_head_with_a_raised_cosine_fade() -> None:
    """A forced-span caller gets a bounded head, not a rectangular edge."""
    n, peak, span, lead = 2048, 512, 240, 48
    window = build_gate_window(n, peak_idx=peak, span=span, lead=lead)

    assert np.all(window[: peak - lead] == 0.0)
    fade = window[peak - lead : peak]
    assert fade[0] == pytest.approx(0.0, abs=1e-12)
    assert np.all(np.diff(fade) > 0.0)
    assert window[peak] == pytest.approx(1.0)
    # The lead is the only difference: past the peak the two shapes agree.
    unbounded = build_gate_window(n, peak_idx=peak, span=span)
    assert np.array_equal(window[peak:], unbounded[peak:])


@pytest.mark.parametrize(
    ("peak_idx", "span"),
    [(512, 0), (512, -1), (-1, 240), (2000, 240)],
)
def test_a_window_that_would_not_fit_is_refused(peak_idx: int, span: int) -> None:
    """Truncating it silently would publish a shorter window than claimed."""
    with pytest.raises(ValueError):
        build_gate_window(2048, peak_idx=peak_idx, span=span)


def test_gate_impulse_response_applies_exactly_the_factory_window() -> None:
    """The seam: the shipped gate is the factory's window and nothing else."""
    ir = _synthetic_ir()
    gated, fragment = gate_impulse_response(ir, RATE)

    peak = int(round(fragment["direct_peak_ms"] * RATE / 1000.0))
    span = int(round(fragment["window_ms"] * RATE / 1000.0))
    expected = ir.astype(np.float64) * build_gate_window(
        ir.size, peak_idx=peak, span=span
    )
    np.testing.assert_array_equal(gated, expected.astype(np.float32))


def test_gated_spectrum_of_a_known_ir_is_pinned() -> None:
    """A regression pin on the numbers, not only on the shape.

    Two tones through the same gate: the deconvolved-IR magnitudes a caller
    reads are stated here so a change to the window's construction has to
    move a published number rather than pass unnoticed.
    """
    ir = _synthetic_ir()
    gated, fragment = gate_impulse_response(ir, RATE)
    spectrum = np.fft.rfft(gated.astype(np.float64), n=1 << 16)
    freqs = np.fft.rfftfreq(1 << 16, d=1.0 / RATE)
    db = 20.0 * np.log10(np.abs(spectrum))

    assert fragment["window_ms"] == pytest.approx(4.9167, abs=1e-3)
    assert fragment["floor_source"] == "measured_reflection"
    for hz, expected_db in ((1000.0, 2.2552), (5000.0, 0.8998)):
        index = int(np.argmin(np.abs(freqs - hz)))
        assert db[index] == pytest.approx(expected_db, abs=5e-3)
