# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Impulse-response gating and the low-frequency validity floor.

These tests pin the P1a consult table (docs/active-crossover-information-design.md
"Measurement validity: gating and the low-frequency floor"): the hysteresis
reflection detector, the half-Hann-tail window, the ``f_valid = 1/window``
floor formula, and the near-field-exempt / measured / search-bound fragment
shapes.

Reflections are injected as delayed+scaled DELTAS, never as symmetric
linear-phase FIR output (``scipy.signal.firwin``/``firwin2``). A linear-phase
FIR pre-rings — its impulse response has meaningful energy *before* its
nominal peak (empirically ~1.6 ms early for a 3.6 ms floor bounce through a
1023-tap lp@400 "woofer" in early spikes of this module) — so an onset
detector fed FIR-shaped output reports the reflection earlier than it really
arrives. A pure delta has no such artifact, so `detect_first_reflection`'s
exact-timing behavior is pinned against deltas here (see
``jasper.audio_measurement.gating``'s own docstring and the module's "Known
risks" note carried in the P1a PR body).

Reflection-level / SNR envelope tested: empirically (see PR body), this
K=12 dB hysteresis design reliably recovers reflections from -6 to -10 dB
(comfortably inside the "-3..-10 dB domestic floor bounce" the K constant
targets) at 30-40 dB delta-vs-noise-floor SNR, with zero failures across a
50-seed sweep. -12 dB (exactly at K) is a genuine coin-flip boundary by
construction (hysteresis has to cross back above a threshold equal to the
reflection's own level), and weaker reflections or 20 dB SNR are, BY DESIGN,
not reliably caught by a delta-only synthetic test — see the module
docstring's "missing a reflection is the dangerous direction; raise K, not
lower it" note. This file pins the reliable envelope and separately pins the
graceful (never-silent, never-wrong-applied) fallback when nothing crosses
threshold.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement import gating

SR = 48000


def _delta_ir(n_samples: int, peak_idx: int, *, amplitude: float = 1.0) -> np.ndarray:
    ir = np.zeros(n_samples, dtype=np.float64)
    ir[peak_idx] = amplitude
    return ir


def _delta_ir_with_reflection(
    n_samples: int,
    peak_idx: int,
    offset_ms: float,
    reflection_db: float,
    *,
    sample_rate: int = SR,
) -> tuple[np.ndarray, int]:
    ir = _delta_ir(n_samples, peak_idx)
    refl_idx = peak_idx + int(round(offset_ms * 1e-3 * sample_rate))
    ir[refl_idx] = 10 ** (reflection_db / 20)
    return ir, refl_idx


# ---------- consult-table constants pinned -----------------------------------


def test_consult_table_constants_pinned():
    """The P1a consult table's numbers are a load-bearing contract, not just
    defaults — pin them so a drive-by retune doesn't silently change what
    "gated" means without a deliberate decision."""
    assert gating.REFLECTION_THRESHOLD_DB == 12.0
    assert gating.SEARCH_T_MIN_MS == 0.5
    assert gating.SEARCH_T_MAX_MS == 7.0
    assert gating.ENVELOPE_SMOOTH_MS == 0.20
    assert gating.TAPER_FRACTION == 0.25
    assert gating.NEAR_FLOOR_RATIO == 1.25
    assert gating.WINDOW_KIND == "half_hann_tail"
    assert gating.GATING_SCHEMA_VERSION == 1
    assert gating.FLOOR_MEASURED == "measured_reflection"
    assert gating.FLOOR_SEARCH_BOUND == "search_span_bound"
    assert gating.NEAR_FIELD_EXEMPT == "near_field"


# ---------- f_valid_floor_hz formula ------------------------------------------


def test_f_valid_floor_hz_4ms_window_is_250hz():
    assert gating.f_valid_floor_hz(0.004) == pytest.approx(250.0)


@pytest.mark.parametrize("window_ms", [3.6, 3.8, 4.0, 4.2, 4.5])
def test_f_valid_floor_hz_domestic_typical_windows_land_in_spec_range(window_ms):
    """1 m domestic-typical reflection-free windows (~3.51-4.65 ms, set by
    the floor bounce) should land the floor in the spec's stated 215-285 Hz.
    (The spec's own quoted range is itself ~3.5-4.7 ms; the parametrized
    values here are chosen strictly inside the 1/285..1/215 = 3.509-4.651 ms
    interval so the boundary rounding of the 215/285 Hz quotes themselves
    doesn't make this test flake.)"""
    floor_hz = gating.f_valid_floor_hz(window_ms / 1000.0)
    assert 215.0 <= floor_hz <= 285.0


def test_f_valid_floor_hz_guards_nonpositive_and_nonfinite():
    assert gating.f_valid_floor_hz(0.0) == float("inf")
    assert gating.f_valid_floor_hz(-0.001) == float("inf")
    assert gating.f_valid_floor_hz(float("nan")) == float("inf")
    assert gating.f_valid_floor_hz(float("inf")) == float("inf")


# ---------- detect_first_reflection: exact recovery on clean deltas ----------


@pytest.mark.parametrize("offset_ms", [3.6, 4.0, 5.8])
def test_detect_first_reflection_clean_delta_exact_recovery(offset_ms):
    """A strong, clean (near-zero-noise) delta reflection is recovered well
    inside the +/-0.5 ms promise — the base case the hysteresis logic must
    get right before any noise/level robustness claim means anything.

    The envelope-smoothing convolution (ENVELOPE_SMOOTH_MS, w samples wide)
    is centered, so it "sees" a fraction of an upcoming delta's energy
    starting ~w/2 samples early; a strong, clean reflection is therefore
    detected a handful of samples (empirically exactly -4 at these deltas'
    strengths, regardless of offset) before its true position. That is a
    deterministic property of the smoothing kernel, not test noise — hence
    the tight-but-not-zero tolerance below (well inside the ±0.5 ms/24-sample
    promise, comfortably outside the observed 4-sample bias).
    """
    n_samples = int(0.030 * SR)
    p_idx = 200
    ir, refl_idx = _delta_ir_with_reflection(n_samples, p_idx, offset_ms, -6.0)
    rng = np.random.default_rng(123)
    ir = ir + rng.normal(0, 10 ** (-60 / 20), n_samples)  # -60 dB noise floor

    det = gating.detect_first_reflection(ir, SR)
    assert det.direct_peak_idx == p_idx
    assert det.floor_source == gating.FLOOR_MEASURED
    got_ms = (det.reflection_idx - p_idx) / SR * 1000
    assert got_ms == pytest.approx(offset_ms, abs=0.5)
    assert abs(det.reflection_idx - refl_idx) <= 8


# ---------- detect_first_reflection: the reliable level/SNR envelope --------


@pytest.mark.parametrize("reflection_db", [-6.0, -8.0, -10.0])
@pytest.mark.parametrize("snr_db", [30.0, 40.0])
@pytest.mark.parametrize("offset_ms", [3.6, 5.8])
def test_detect_first_reflection_recovers_within_tolerance_across_levels_and_snr(
    reflection_db, snr_db, offset_ms
):
    """Recovery within +/-0.5 ms across the K=12 dB design's reliable catch
    zone (-6..-10 dB, comfortably above the K threshold) and 30-40 dB
    delta-vs-noise SNR. Fixed seed per combination for determinism; the
    combination was swept across 50 seeds with zero failures (see PR body)."""
    n_samples = int(0.030 * SR)
    p_idx = 200
    rng = np.random.default_rng(
        hash((round(reflection_db), round(snr_db), round(offset_ms * 10))) % (2**32)
    )
    noise_rms = 10 ** (-snr_db / 20)
    ir, refl_idx = _delta_ir_with_reflection(n_samples, p_idx, offset_ms, reflection_db)
    ir = ir + rng.normal(0, noise_rms, n_samples)

    det = gating.detect_first_reflection(ir, SR)
    assert det.floor_source == gating.FLOOR_MEASURED, (
        f"expected a measured reflection at {reflection_db} dB / {snr_db} dB "
        f"SNR (inside the documented reliable envelope), got {det.floor_source}"
    )
    got_ms = (det.reflection_idx - p_idx) / SR * 1000
    assert got_ms == pytest.approx(offset_ms, abs=0.5)


# ---------- detect_first_reflection: ungateable / graceful fallback ---------


def test_detect_first_reflection_silent_ir_is_ungateable():
    """An all-zero (silent) IR must not crash and must report floor_source
    None (ungateable) — never a fabricated reflection."""
    ir = np.zeros(2000, dtype=np.float64)
    det = gating.detect_first_reflection(ir, SR)
    assert det.floor_source is None
    assert det.reflection_idx is None


def test_detect_first_reflection_nan_ir_does_not_raise():
    """A NaN-poisoned IR must not raise (quality gating upstream should
    normally catch this, but gating must guard its own divides regardless)."""
    ir = np.zeros(2000, dtype=np.float64)
    ir[100] = 1.0
    ir[150] = float("nan")
    det = gating.detect_first_reflection(ir, SR)  # must not raise
    assert det.direct_peak_idx >= 0


def test_detect_first_reflection_empty_ir_is_ungateable():
    det = gating.detect_first_reflection(np.array([], dtype=np.float64), SR)
    assert det == gating.ReflectionDetection(0, None, None)


def test_detect_first_reflection_no_room_to_search_is_ungateable():
    """Direct peak within t_min of the array end: no room to search at all
    (distinct from a search that runs to completion and finds nothing)."""
    n_samples = 100  # far shorter than even t_min_ms's sample count at 48 kHz...
    ir = _delta_ir(n_samples, n_samples - 1)  # peak at the very last sample
    det = gating.detect_first_reflection(ir, SR)
    assert det.floor_source is None


def test_detect_first_reflection_degenerate_sample_rate_is_ungateable():
    ir = _delta_ir(1000, 200)
    for bad_sr in (0, -48000):
        det = gating.detect_first_reflection(ir, bad_sr)
        assert det.floor_source is None


def test_detect_first_reflection_no_reflection_in_span_returns_search_bound():
    """A real direct arrival with nothing crossing back above threshold
    within the search span reports the conservative search-span-bound floor
    — never None-with-reflection, never silently treated as unmeasured."""
    n_samples = 2000
    ir = _delta_ir(n_samples, 200)  # nothing after the peak at all
    det = gating.detect_first_reflection(ir, SR)
    assert det.floor_source == gating.FLOOR_SEARCH_BOUND
    assert det.reflection_idx is None
    assert det.direct_peak_idx == 200


# ---------- gate_impulse_response: window + fragment shape -------------------


def test_gate_impulse_response_measured_reflection_window_and_floor():
    """Spec anchor: a ~4 ms reflection-free window resolves to ~250 Hz.
    (The exact 4ms->250Hz formula is pinned precisely, independent of
    detection precision, by test_f_valid_floor_hz_4ms_window_is_250hz —
    this test checks the end-to-end wiring lands in the same ballpark, with
    tolerance for the detector's small, deterministic smoothing-driven bias;
    see test_detect_first_reflection_clean_delta_exact_recovery.)"""
    n_samples = int(0.030 * SR)
    p_idx = 500
    offset_ms = 4.0
    ir, refl_idx = _delta_ir_with_reflection(n_samples, p_idx, offset_ms, -6.0)

    gated, fragment = gating.gate_impulse_response(ir, SR)

    assert fragment["schema_version"] == 1
    assert fragment["window"] == "half_hann_tail"
    assert fragment["floor_source"] == gating.FLOOR_MEASURED
    assert fragment["direct_peak_ms"] == pytest.approx(p_idx / SR * 1000)
    assert fragment["first_reflection_ms"] == pytest.approx(
        refl_idx / SR * 1000, abs=0.15
    )
    # window_ms is exactly the peak-to-reflection span (taper NOT subtracted).
    assert fragment["window_ms"] == pytest.approx(
        fragment["first_reflection_ms"] - fragment["direct_peak_ms"]
    )
    assert fragment["window_ms"] == pytest.approx(offset_ms, abs=0.15)
    assert 230.0 <= fragment["f_valid_floor_hz"] <= 270.0
    # SC-2 fragment excludes applied/exempt_reason (caller's job to add).
    assert "applied" not in fragment
    assert "exempt_reason" not in fragment
    # Same length as input, regardless of geometry.
    assert gated.shape == ir.shape
    assert gated.dtype == np.float32


@pytest.mark.parametrize("window_ms", [3.6, 4.5])
def test_gate_impulse_response_floor_lands_in_domestic_range(window_ms):
    n_samples = int(0.030 * SR)
    p_idx = 300
    ir, _ = _delta_ir_with_reflection(n_samples, p_idx, window_ms, -6.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_MEASURED
    assert 215.0 <= fragment["f_valid_floor_hz"] <= 285.0


def test_gate_impulse_response_taper_is_half_hann_from_1_to_0():
    """Rectangular head through the peak, flat plateau, half-Hann taper
    (1 -> ~0.5 at its midpoint -> 0) into the detected reflection, 0 after.

    Rides a tiny (-60 dB) uniform background on top of the delta pair so
    ``gated / ir`` recovers the window's value at any index (division by the
    sparse delta positions alone would tell us nothing about the shape in
    between). The background sits far enough below the 12 dB threshold line
    that it does not perturb detection itself.
    """
    n_samples = 2000
    p_idx = 500
    nominal_span_samples = 240  # 5 ms nominal target
    background = 1e-3  # -60 dB
    ir = np.full(n_samples, background, dtype=np.float64)
    ir[:p_idx] = 0.0
    ir[p_idx] = 1.0
    ir[p_idx + nominal_span_samples] = 10 ** (-6.0 / 20)

    gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_MEASURED

    # Derive the actually-detected end from the fragment rather than the
    # nominal offset: envelope smoothing can shift the exact threshold
    # crossing by a few samples (pinned separately by the recovery-tolerance
    # tests above), and the window formula must match what was ACTUALLY
    # windowed, not what was intended.
    actual_span_samples = round(fragment["window_ms"] * 1e-3 * SR)
    end = p_idx + actual_span_samples
    taper_len = max(1, round(gating.TAPER_FRACTION * actual_span_samples))
    flat_end = end - taper_len
    mid = flat_end + taper_len // 2

    def win_at(i: int) -> float:
        return float(gated[i] / ir[i])

    assert win_at(p_idx) == pytest.approx(1.0, abs=1e-4)  # head
    assert win_at(p_idx + 10) == pytest.approx(1.0, abs=1e-4)  # plateau
    assert win_at(flat_end - 1) == pytest.approx(1.0, abs=1e-4)  # still flat
    assert win_at(mid) == pytest.approx(0.5, abs=0.05)  # taper midpoint
    assert win_at(end) == pytest.approx(0.0, abs=1e-4)  # zero at the boundary
    assert win_at(end + 5) == pytest.approx(0.0, abs=1e-6)  # zero after


def test_gate_impulse_response_no_reflection_uses_search_bound_window():
    """No reflection found -> window is the search-span bound (7.0 ms), never
    a silent None while still claiming a measured floor."""
    n_samples = 2000
    p_idx = 200
    ir = _delta_ir(n_samples, p_idx)

    gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_SEARCH_BOUND
    assert fragment["first_reflection_ms"] is None
    assert fragment["window_ms"] == pytest.approx(gating.SEARCH_T_MAX_MS, abs=0.05)
    assert fragment["f_valid_floor_hz"] == pytest.approx(
        1000.0 / gating.SEARCH_T_MAX_MS, abs=0.5
    )
    assert gated.shape == ir.shape


def test_gate_impulse_response_ungateable_returns_ir_unchanged():
    n_samples = 2000
    ir = np.zeros(n_samples, dtype=np.float64)  # silent -> ungateable

    gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] is None
    assert fragment["first_reflection_ms"] is None
    assert fragment["window_ms"] is None
    assert fragment["f_valid_floor_hz"] is None
    assert np.array_equal(gated, ir.astype(np.float32))
    # Caller rule: applied = floor_source is not None -> False here.
    applied = fragment["floor_source"] is not None
    assert applied is False


def test_gate_impulse_response_empty_ir_does_not_raise():
    gated, fragment = gating.gate_impulse_response(np.array([], dtype=np.float64), SR)
    assert gated.shape == (0,)
    assert fragment["floor_source"] is None
    assert fragment["direct_peak_ms"] == 0.0


# ---------- exempt_gating_block ------------------------------------------


def test_exempt_gating_block_shape():
    n_samples = 1000
    p_idx = 240
    ir = _delta_ir(n_samples, p_idx, amplitude=0.5)

    block = gating.exempt_gating_block(ir, SR)
    assert block == {
        "schema_version": 1,
        "applied": False,
        "exempt_reason": "near_field",
        "direct_peak_ms": pytest.approx(p_idx / SR * 1000),
        "first_reflection_ms": None,
        "window_ms": None,
        "window": "half_hann_tail",
        "f_valid_floor_hz": None,
        "floor_source": None,
    }


def test_exempt_gating_block_custom_reason():
    ir = _delta_ir(500, 50)
    block = gating.exempt_gating_block(ir, SR, reason="some_other_reason")
    assert block["exempt_reason"] == "some_other_reason"
    assert block["applied"] is False


def test_exempt_gating_block_empty_ir_does_not_raise():
    block = gating.exempt_gating_block(np.array([], dtype=np.float64), SR)
    assert block["direct_peak_ms"] == 0.0
    assert block["applied"] is False
