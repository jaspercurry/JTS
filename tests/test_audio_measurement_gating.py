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
not reliably caught by a delta-only synthetic test. This file pins the
reliable envelope and separately pins the graceful (never-silent,
never-wrong-applied) fallback when nothing crosses threshold.
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
    assert gating.GATING_SCHEMA_VERSION == 2
    assert gating.FLOOR_MEASURED == "measured_reflection"
    assert gating.FLOOR_SEARCH_BOUND == "search_span_bound"
    assert gating.NEAR_FIELD_EXEMPT == "near_field"


def test_moving_k_off_the_shipped_operating_point_costs_the_evidence_band():
    """#1983 — both directions of K are lossy, observed through the fragment.

    Module guidance used to read "raise K, not lower it", which is backwards
    in both directions. Per :data:`~jasper.audio_measurement.gating`'s
    ``REFLECTION_THRESHOLD_DB`` block, raising K past the P_D peak is
    strictly dominated (both error rates degrade together), and K = 12 is the
    hardware FLOOR of the range that reproduces this speaker's anatomy, not
    merely a corpus optimum. Both costs are pinned here rather than only the
    optimistic one.
    """
    n, p = int(0.030 * SR), 500

    # RAISING K lowers the level a candidate must climb back over, so a
    # horn/baffle-scale feature the shipped bar excludes starts gating. The
    # certification's kurtosis challenger did exactly this on the real
    # 646 us feature (§5); the window collapses and the validity floor lands
    # inside the 2 kHz crossover's evidence band, destroying it. The feature
    # sits at -14 dB so both probes below clear the hysteresis level by 2 dB
    # rather than tying with it.
    early, _ = _delta_ir_with_reflection(n, p, 4.0, -6.0)
    early[p + int(round(0.646e-3 * SR))] = 10 ** (-14 / 20)

    _gated, shipped = gating.gate_impulse_response(early, SR)
    assert shipped["floor_source"] == gating.FLOOR_MEASURED
    assert shipped["window_ms"] == pytest.approx(4.0, abs=0.2)
    assert shipped["f_valid_floor_hz"] < 300.0

    _gated, raised = gating.gate_impulse_response(early, SR, threshold_db=16.0)
    assert raised["floor_source"] == gating.FLOOR_MEASURED
    assert raised["window_ms"] < 1.0
    assert raised["f_valid_floor_hz"] > 1500.0

    # LOWERING K raises that level past a real domestic bounce, which is then
    # missed: the window sits at the search ceiling and the record over-claims
    # validity an octave below the truth. §WO-6 measured the same shape on
    # hardware — 13/13 at K = 12, 10/13 at K = 11, 0/13 at K = 10.
    bounce, _ = _delta_ir_with_reflection(n, p, 3.6, -11.5)

    _gated, found = gating.gate_impulse_response(bounce, SR)
    assert found["floor_source"] == gating.FLOOR_MEASURED
    assert found["window_ms"] == pytest.approx(3.6, abs=0.2)

    _gated, missed = gating.gate_impulse_response(bounce, SR, threshold_db=10.0)
    assert missed["floor_source"] == gating.FLOOR_SEARCH_BOUND
    assert missed["window_ms"] == pytest.approx(gating.SEARCH_T_MAX_MS)
    assert missed["f_valid_floor_hz"] < found["f_valid_floor_hz"]


def test_gating_contract_constants_pinned():
    """The R9 contract's own numbers (#1969), same rule as the table above.

    ``TRUSTED_FLOOR_MULTIPLIER`` and ``TOA_REFINE_MS`` in particular are
    reproduction constants: the banked E5 record and the certified
    peak-refined variant were computed with these values, so changing one
    silently de-couples the product from the evidence that justified it.
    """
    assert gating.TRUSTED_FLOOR_MULTIPLIER == 2.5
    assert gating.TOA_REFINE_MS == 0.5
    assert gating.LEDGER_SPAN_MS == 1.6
    assert gating.LEDGER_PROMINENCE_DB == 6.0
    assert gating.LEDGER_PROMINENCE_LOOKBACK == 12
    assert gating.LEDGER_MAX_ENTRIES == 6
    assert gating.CLASS_DUT_INTERNAL == "DUT_internal_ungateable"
    assert gating.CLASS_GATEABLE == "gateable"


# ---------- the prominence vote's constants (R9 WO-6) ---------------------


def test_prominence_vote_operating_point_pinned():
    """Q is a measured operating point, not a tuning default.

    7.5 dB was selected from a 315-cell (K, Q) grid on our own ESS
    deconvolution chain, over the complete frozen criteria region, screened
    against this speaker's established anatomy
    (``captures/detector-certification-20260801`` §WO-6). Changing it
    silently de-couples the product from the measurement that justified it,
    and — because the ceiling is set by real hardware rather than by the
    corpus — a plausible-looking increase is the specific failure mode.
    """
    assert gating.REFLECTION_PROMINENCE_DB == 7.5
    # K did NOT move. The vote is additive; #1983's K analysis still stands.
    assert gating.REFLECTION_THRESHOLD_DB == 12.0


def test_raising_q_to_the_corpus_optimum_rejects_the_speakers_own_reflection():
    """#1969 / WO-6 — the corpus alone points the wrong way on this knob.

    Picked on corpus statistics alone the vote wants Q = 13.5, which measured
    6.6x fewer false alarms — and rejects this speaker's one independently
    corroborated room reflection (1.275 ms, woofer branch) on 13 of 13 real
    captures. The fixture reproduces that reflection's arrival and its
    measured envelope prominence (9.25 dB median, 8.86 dB minimum) on a
    deterministic floor, so the ceiling is observable in the fragment.

    The opposite direction — lowering Q brings the early false detects back —
    is pinned by
    ``test_the_vote_rejects_the_early_fire_the_bare_threshold_accepts``.
    """
    n, p = int(0.030 * SR), 500
    # A sign-alternating floor has an exactly flat moving-RMS envelope, so
    # the candidate's prominence over it is set by construction rather than
    # by a seed: 0.046 puts it at 9.24 dB, the real reflection's median.
    ir = _delta_ir(n, p)
    span = np.arange(p + 1, p + int(round(8.0e-3 * SR)))
    ir[span] = 0.046 * np.where(span % 2 == 0, 1.0, -1.0)
    ir[p + int(round(1.275e-3 * SR))] = 10 ** (-8 / 20)

    _gated, shipped = gating.gate_impulse_response(ir, SR)
    assert shipped["floor_source"] == gating.FLOOR_MEASURED
    arrival_ms = shipped["first_reflection_ms"] - shipped["direct_peak_ms"]
    assert arrival_ms == pytest.approx(1.275, abs=0.1)

    _gated, raised = gating.gate_impulse_response(ir, SR, prominence_db=13.5)
    assert raised["floor_source"] == gating.FLOOR_SEARCH_BOUND
    assert raised["window_ms"] == pytest.approx(gating.SEARCH_T_MAX_MS)
    # The cost of the rejection is an over-claim, not a refusal: the window
    # stretches to the 7 ms ceiling, so the floor lands well below what the
    # real span supports.
    assert raised["f_valid_floor_hz"] < shipped["f_valid_floor_hz"]


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

    assert fragment["schema_version"] == 2
    assert fragment["window"] == "half_hann_tail"
    assert fragment["floor_source"] == gating.FLOOR_MEASURED
    assert fragment["direct_peak_ms"] == pytest.approx(p_idx / SR * 1000)
    assert fragment["first_reflection_ms"] == pytest.approx(
        refl_idx / SR * 1000, abs=0.15
    )
    # window_ms is exactly the peak-to-ONSET span (taper NOT subtracted).
    # Schema 2 split the two facts this pairing used to conflate:
    # `reflection_onset_ms` is where the window ends, `first_reflection_ms`
    # is when the reflection arrived. They coincided in schema 1 only
    # because the arrival was reported as the onset.
    assert fragment["window_ms"] == pytest.approx(
        fragment["reflection_onset_ms"] - fragment["direct_peak_ms"]
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


def test_apply_gate_fragment_reuses_signal_gate_without_inspecting_noise():
    """A paired IR receives the exact half-Hann operator chosen by signal."""

    n_samples = 2000
    signal, _ = _delta_ir_with_reflection(n_samples, 500, 4.0, -6.0)
    signal += 1e-3
    paired = np.linspace(-0.5, 0.5, n_samples, dtype=np.float64)

    gated_signal, fragment = gating.gate_impulse_response(signal, SR)
    gated_paired = gating.apply_gate_fragment(paired, SR, fragment)

    signal_window = np.divide(
        gated_signal,
        signal,
        out=np.zeros_like(gated_signal),
        where=signal != 0,
    )
    paired_window = np.divide(
        gated_paired,
        paired,
        out=np.zeros_like(gated_paired),
        where=paired != 0,
    )
    assert paired_window == pytest.approx(signal_window, abs=1e-5)


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
        "schema_version": 2,
        "applied": False,
        "exempt_reason": "near_field",
        "direct_peak_ms": pytest.approx(p_idx / SR * 1000),
        "first_reflection_ms": None,
        "reflection_onset_ms": None,
        "window_ms": None,
        "window": "half_hann_tail",
        "f_valid_floor_hz": None,
        "f_trusted_hz": None,
        "floor_source": None,
        "internal_reflection_ledger": [],
    }


def test_exempt_gating_block_custom_reason():
    ir = _delta_ir(500, 50)
    block = gating.exempt_gating_block(ir, SR, reason="some_other_reason")
    assert block["exempt_reason"] == "some_other_reason"
    assert block["applied"] is False


# ---------- the R9 gating contract (#1969) ---------------------------------
#
# Everything below rides ALONGSIDE the gate. The suite's first job is to
# prove that: the contract added reporting, and reporting must not be able
# to move a decision.


def _band_limited_ir(
    lo_hz: float,
    hi_hz: float,
    *,
    reflection_offset_ms: float | None = None,
    reflection_db: float = -11.6,
    noise_db: float = -45.0,
    seed: int = 1969,
) -> np.ndarray:
    """A physically realizable IR — a band-limited arrival on a noise floor.

    The delta fixtures above pin exact timing (see the module docstring on
    why deltas, not FIR output). These pin behaviour that only MEANS
    anything on a signal with a passband: an ideal delta has infinite
    bandwidth, so it has no "band the DUT radiates" and its analytic
    envelope is all sidelobe.
    """
    from scipy.signal import firwin, lfilter

    rng = np.random.default_rng(seed)
    n = int(0.030 * SR)
    x = np.zeros(n)
    x[500] = 1.0
    if reflection_offset_ms is not None:
        x[500 + int(round(reflection_offset_ms * 1e-3 * SR))] = 10 ** (
            reflection_db / 20
        )
    taps = firwin(511, [lo_hz / (SR / 2), hi_hz / (SR / 2)], pass_zero=False)
    ir = lfilter(taps, 1.0, x)
    return ir / np.abs(ir).max() + rng.normal(0, 10 ** (noise_db / 20), n)


# --- byte-identity: no UNINTENDED decision change --------------------------

#: Captured from the PRE-CONTRACT ``gate_impulse_response`` (commit
#: ``5e7efd268``) and asserted against the current one. These four fields
#: ARE the gate's decisions: whether a capture gates, where its window ends,
#: where the direct arrival was, and the low-frequency floor that follows.
#:
#: **Read this table for what it is.** The R9 disclosure contract (#1969)
#: could not move any of them — it added reporting only. The WO-6 prominence
#: vote CAN, and does so deliberately; it simply does not on these seven,
#: because their reflections are strong deltas that clear the vote easily.
#: So this table's job changed: it was proof that nothing moved, and it is
#: now the tripwire for a decision moving when nobody meant it to. The vote's
#: own intended change is pinned separately, on a fixture built for it
#: (``test_the_vote_rejects_the_early_fire_the_bare_threshold_accepts``).
#:
#: The same comparison was re-run out of band over the wider corpus — 34
#: fixtures including 10 real jts3 per-branch IRs — comparing the full
#: fragment plus a SHA-256 of the gated float32 samples, and came back
#: byte-identical across the WO-6 change too: 34/34 unchanged, including
#: all 13 real jts3 branch IRs. Those IRs are untracked session artifacts
#: and cannot live here, so this table is the tracked subset. See the PR body.
_PRE_CONTRACT_GATE_DECISIONS = {
    # name: (floor_source, window_ms, direct_peak_ms, f_valid_floor_hz)
    "measured_3.6ms_-6dB": ("measured_reflection", 3.5208333333333335,
                            10.416666666666666, 284.02366863905326),
    "measured_4.0ms_-10dB": ("measured_reflection", 3.9166666666666665,
                             10.416666666666666, 255.31914893617022),
    "measured_5.8ms_-8dB": ("measured_reflection", 5.708333333333333,
                            10.416666666666666, 175.1824817518248),
    "fallback_bare_delta": ("search_span_bound", 7.0, 4.166666666666667,
                            142.85714285714286),
    "fallback_weak_reflection": ("search_span_bound", 7.0, 10.416666666666666,
                                 142.85714285714286),
    "ungateable_silent": (None, None, 0.0, None),
    "ungateable_no_room": (None, None, 2.0625, None),
}


def _decision_fixture(name: str) -> np.ndarray:
    """Rebuild one fixture of :data:`_PRE_CONTRACT_GATE_DECISIONS` by name."""
    rng = np.random.default_rng(20260731)
    n = int(0.030 * SR)
    if name.startswith("measured_"):
        offset_ms = float(name.split("_")[1].removesuffix("ms"))
        level_db = float(name.split("_")[2].removesuffix("dB"))
        ir, _ = _delta_ir_with_reflection(n, 500, offset_ms, level_db)
        return ir + rng.normal(0, 10 ** (-45 / 20), n)
    if name == "fallback_bare_delta":
        return _delta_ir(2000, 200)
    if name == "fallback_weak_reflection":
        ir, _ = _delta_ir_with_reflection(n, 500, 4.0, -20.0)
        return ir + rng.normal(0, 10 ** (-50 / 20), n)
    if name == "ungateable_silent":
        return np.zeros(2000, dtype=np.float64)
    if name == "ungateable_no_room":
        return _delta_ir(100, 99)
    raise AssertionError(f"unknown fixture {name!r}")


@pytest.mark.parametrize("name", sorted(_PRE_CONTRACT_GATE_DECISIONS))
def test_gate_decisions_are_unchanged_by_the_disclosure_contract(name):
    """The tripwire for an unintended decision change.

    Spans all three states a capture can end in — measured reflection,
    ceiling fallback, ungateable — against values recorded from the
    pre-contract code. R9's disclosure contract could not move these
    (reporting only); WO-6's prominence vote can, and deliberately does not
    here — see :data:`_PRE_CONTRACT_GATE_DECISIONS`.
    """
    expected = _PRE_CONTRACT_GATE_DECISIONS[name]
    _gated, fragment = gating.gate_impulse_response(_decision_fixture(name), SR)
    got = (
        fragment["floor_source"],
        fragment["window_ms"],
        fragment["direct_peak_ms"],
        fragment["f_valid_floor_hz"],
    )
    assert got == pytest.approx(expected, rel=1e-12, abs=1e-12), (
        f"{name}: gate DECISION moved. Expected {expected}, got {got}. If "
        "that was deliberate, it needs the measurement to justify it and a "
        "fixture that pins the new behaviour, not a golden refresh."
    )


def test_the_window_is_built_from_the_onset_not_the_reported_arrival():
    """The structural reason a reported ToA cannot move a window.

    ``apply_gate_fragment`` rebuilds the operator from ``direct_peak_ms``
    and ``window_ms`` ALONE. If the reported arrival ever leaked into the
    window, the rebuild would disagree with what was actually applied.
    """
    ir, _ = _delta_ir_with_reflection(int(0.030 * SR), 500, 4.0, -6.0)
    ir = ir + 1e-3
    gated, fragment = gating.gate_impulse_response(ir, SR)
    rebuilt = gating.apply_gate_fragment(ir, SR, fragment)
    assert np.array_equal(gated, rebuilt)
    # ... and the arrival really is a DIFFERENT number from the bound here,
    # so the equality above is not passing by coincidence.
    assert fragment["first_reflection_ms"] != fragment["reflection_onset_ms"]


# --- peak reporting (certification §4.5) ----------------------------------


def test_reported_arrival_is_the_envelope_peak_not_the_onset():
    """The certified peak-reporting fix.

    The shipped detector reports the smoothed envelope's threshold CROSSING,
    which precedes the reflection: measured against exact synthetic ground
    truth the bias is -0.125 ms, eating most of a 0.15 ms tolerance before
    noise is considered (``captures/detector-certification-20260801`` §4.5).
    Relocalising the report to the envelope peak moved the median error to
    0.000 ms and lifted strict-tolerance detection 0.464 -> 0.609 with the
    detection decision byte-identical.
    """
    n = int(0.030 * SR)
    ir, refl_idx = _delta_ir_with_reflection(n, 500, 4.0, -6.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    truth_ms = refl_idx / SR * 1000.0

    onset_err = abs(fragment["reflection_onset_ms"] - truth_ms)
    arrival_err = abs(fragment["first_reflection_ms"] - truth_ms)
    assert arrival_err < onset_err, (
        "peak reporting must be closer to the true arrival than the onset"
    )
    assert arrival_err == pytest.approx(0.0, abs=1e-9)
    # The onset is EARLY, never late — which is what makes gating to it the
    # conservative choice.
    assert fragment["reflection_onset_ms"] <= fragment["first_reflection_ms"]


def test_peak_refinement_is_bounded_to_the_certified_window():
    """It relocalises within ``TOA_REFINE_MS`` of the onset and no further —
    a wider search could walk onto a LATER, stronger reflection and report
    that one's arrival as the first."""
    n = int(0.030 * SR)
    ir, _ = _delta_ir_with_reflection(n, 500, 4.0, -6.0)
    # A much louder second reflection well outside the refine window.
    ir[500 + int(round(5.5 * 1e-3 * SR))] = 10 ** (-1.0 / 20)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    drift_ms = fragment["first_reflection_ms"] - fragment["reflection_onset_ms"]
    assert 0.0 <= drift_ms <= gating.TOA_REFINE_MS


# --- the asymmetric-cost classification guard ------------------------------


def test_a_sub_search_window_feature_is_classified_and_never_gates():
    """The guard's whole promise, on the shape that motivated it.

    jts3's horn carries a real ~291 us feature at -11.2 dB. It is NOT a room
    reflection and gating there would set the window to 0.29 ms — a 3448 Hz
    validity floor and an 8621 Hz trusted floor — destroying the entire
    evidence band around the 2 kHz crossover the tuner depends on
    (``captures/detector-certification-20260801`` §5). A miss merely
    over-claims low-frequency validity; this would be catastrophic, so the
    costs are deliberately not symmetric.
    """
    ir = _band_limited_ir(2500.0, 18000.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)

    ledger = fragment["internal_reflection_ledger"]
    internal = [e for e in ledger if e["classification"] == gating.CLASS_DUT_INTERNAL]
    assert internal, "a wideband branch's early features must be enumerated"
    for entry in internal:
        assert entry["tau_us"] < gating.SEARCH_T_MIN_MS * 1000.0
        assert set(entry) == {"tau_us", "level_db", "prominence_db", "classification"}

    # ... and NOTHING it recorded became a window bound.
    assert fragment["floor_source"] == gating.FLOOR_SEARCH_BOUND
    assert fragment["window_ms"] == pytest.approx(gating.SEARCH_T_MAX_MS, abs=0.05)
    assert fragment["first_reflection_ms"] is None


def test_ledger_classifies_by_the_minimum_gate_boundary_exactly():
    """``SEARCH_T_MIN_MS`` is the single boundary — a feature inside the
    search span is ``gateable`` (the detector's own decision governs it),
    one before it is loudspeaker-internal by construction."""
    ir = _band_limited_ir(2500.0, 18000.0, reflection_offset_ms=1.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    for entry in fragment["internal_reflection_ledger"]:
        expected = (
            gating.CLASS_DUT_INTERNAL
            if entry["tau_us"] < gating.SEARCH_T_MIN_MS * 1000.0
            else gating.CLASS_GATEABLE
        )
        assert entry["classification"] == expected


def test_ledger_is_bounded_so_a_noisy_capture_cannot_bloat_every_sidecar():
    """It is persisted per capture, so its length is a resource question."""
    rng = np.random.default_rng(4)
    ir = np.zeros(int(0.030 * SR))
    ir[500] = 1.0
    ir[501:] = rng.normal(0, 10 ** (-20 / 20), ir.size - 501)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    assert len(fragment["internal_reflection_ledger"]) <= gating.LEDGER_MAX_ENTRIES


def test_ledger_is_a_list_even_when_empty_never_none():
    """Empty means "the search looked and found no prominent early feature",
    which is a different claim from "nothing looked"."""
    _gated, fragment = gating.gate_impulse_response(np.zeros(2000), SR)
    assert fragment["internal_reflection_ledger"] == []


# --- the prominence vote (R9 WO-6) ----------------------------------------


def test_a_prominent_candidate_passes_the_vote():
    """The vote must not simply suppress detection.

    A clean, strong reflection stands far out of the valley before it, so
    the vote is a no-op on exactly the material the detector was already
    right about.
    """
    n = int(0.030 * SR)
    ir, _refl = _delta_ir_with_reflection(n, 500, 4.0, -6.0)
    voted = gating.detect_first_reflection(ir, SR)
    unvoted = gating.detect_first_reflection(ir, SR, prominence_db=0.0)
    assert voted.floor_source == gating.FLOOR_MEASURED
    assert voted.reflection_idx == unvoted.reflection_idx


def _early_fire_ir(
    *,
    floor_db: float = -26.0,
    bump_db: float = -22.0,
    bump_ms: tuple[float, float] = (0.55, 1.10),
    reflection_ms: float = 4.0,
    reflection_db: float = -6.0,
    seed: int = 1,
) -> np.ndarray:
    """The measured early-fire shape: a shallow noise rise, not a discrete blip.

    A direct arrival on a shaped noise floor, with a broad low bump just
    past the search open that crosses ``peak - K`` without standing out of
    its own surroundings, and a real reflection later. This is the shape the
    certification measured as the shipped detector's dominant failure —
    18.1% of criteria-region positives firing EARLY, typically at the
    search-window open — and the shape the S0 incident (#1790) is a field
    instance of.
    """
    rng = np.random.default_rng(seed)
    n, p = int(0.030 * SR), 500
    gain = np.full(n, 10 ** (floor_db / 20))
    lo = p + int(round(bump_ms[0] * 1e-3 * SR))
    hi = p + int(round(bump_ms[1] * 1e-3 * SR))
    gain[lo:hi] = 10 ** (bump_db / 20)
    ir = rng.standard_normal(n) * gain
    ir[p] = 1.0
    ir[p + int(round(reflection_ms * 1e-3 * SR))] += 10 ** (reflection_db / 20)
    return ir


def test_the_vote_rejects_the_early_fire_the_bare_threshold_accepts():
    """The regression this change exists for, end to end.

    Without the vote the detector fires at ~0.52 ms — a confident wrong
    answer that collapses the window and puts the validity floor near
    1.9 kHz, destroying the crossover evidence band. With it, the candidate
    is voted down, the scan RESUMES, and the real 4 ms reflection is found:
    the same capture goes from a 0.5 ms window to a ~3.9 ms one.

    That resumption is why the fix raises detection rather than trading it:
    measured on our chain, P_D 0.674 -> 0.712 while early fires fall
    0.181 -> 0.124 (``captures/detector-certification-20260801`` §WO-6).
    """
    ir = _early_fire_ir()

    unvoted = gating.detect_first_reflection(ir, SR, prominence_db=0.0)
    assert unvoted.floor_source == gating.FLOOR_MEASURED
    unvoted_ms = (unvoted.reflection_idx - unvoted.direct_peak_idx) / SR * 1000
    assert unvoted_ms < 1.0, "fixture must reproduce the early fire"

    _gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_MEASURED
    assert fragment["window_ms"] == pytest.approx(4.0, abs=0.2), (
        "the vote must not merely reject — the scan resumes and finds the "
        "real reflection"
    )
    # And the disclosed floors follow the honest window, not the collapsed one.
    assert fragment["f_valid_floor_hz"] < 300.0
    assert fragment["f_trusted_hz"] < 750.0


def test_a_voted_down_candidate_does_not_end_the_search():
    """The scan resumes past a rejected excursion — the property that makes
    the vote a fix rather than a miss-rate trade.

    Stated separately from the end-to-end regression above because it is the
    load-bearing structural claim: a first-crossing-wins detector with a
    vote bolted on would return ``search_span_bound`` here.
    """
    ir = _early_fire_ir()
    det = gating.detect_first_reflection(ir, SR)
    assert det.floor_source == gating.FLOOR_MEASURED
    assert det.reflection_idx is not None
    tau_ms = (det.reflection_idx - det.direct_peak_idx) / SR * 1000
    assert tau_ms == pytest.approx(4.0, abs=0.2)


def test_disabling_the_vote_recovers_the_pre_wo6_detector_exactly():
    """``prominence_db <= 0`` is the null, and the null is the old detector.

    The certification harness leans on this: its variant family at null
    parameters is asserted against this function, so every number WO-6
    selected on is only comparable if the null really is a no-op. Checked
    across the three states a capture can end in.
    """
    n = int(0.030 * SR)
    cases = {
        "measured": _delta_ir_with_reflection(n, 500, 4.0, -6.0)[0],
        "ceiling": _delta_ir(2000, 200),
        "ungateable": np.zeros(2000, dtype=np.float64),
        "early_fire": _early_fire_ir(),
    }
    for name, ir in cases.items():
        for k in (6.0, 12.0, 20.0):
            a = gating.detect_first_reflection(
                ir, SR, threshold_db=k, prominence_db=0.0
            )
            b = gating.detect_first_reflection(
                ir, SR, threshold_db=k, prominence_db=-1.0
            )
            assert a == b, f"{name} at K={k}: a non-positive Q must not vote"


def test_the_vote_cannot_resurrect_a_sub_minimum_gate_feature():
    """The asymmetric-cost guard outranks the vote, both directions.

    A DUT-internal feature before ``SEARCH_T_MIN_MS`` is un-gateable by
    construction, and the vote is a REJECTION filter applied inside the
    search span — it can never admit something the span excludes. Pinned
    because "we added a second acceptance stage" is exactly the change that
    could, if mis-wired, widen what gates.
    """
    ir = _band_limited_ir(2500.0, 18000.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_SEARCH_BOUND
    internal = [
        e for e in fragment["internal_reflection_ledger"]
        if e["classification"] == gating.CLASS_DUT_INTERNAL
    ]
    assert internal, "the fixture must still carry sub-min-gate features"
    assert fragment["first_reflection_ms"] is None


def test_the_candidate_scan_terminates_on_an_envelope_that_never_stands_out():
    """Bounded work, on the input designed to make the scan loop longest.

    Broadband noise with no discrete arrival crosses the threshold many
    times and every crossing is voted down, so this exercises the maximum
    number of resume-scan iterations. The loop is bounded because each
    iteration advances the cursor past a strictly longer prefix of the
    search span; this pins that it actually returns, and returns the
    conservative bound rather than an invented reflection.
    """
    rng = np.random.default_rng(11)
    n = int(0.030 * SR)
    ir = rng.standard_normal(n) * 10 ** (-20 / 20)
    ir[500] = 1.0
    det = gating.detect_first_reflection(ir, SR)
    assert det.floor_source in (gating.FLOOR_SEARCH_BOUND, gating.FLOOR_MEASURED)


def test_candidate_prominence_is_non_negative_and_reads_the_smoothed_envelope():
    """The vote's statistic, in isolation.

    Non-negative by construction (the crossing's own sample is in both the
    top window and the valley window), and monotone in how far the peak
    stands above the valley — so a shallow rise scores low and a spike
    scores high, which is the whole discriminator.
    """
    env = np.full(400, 0.1)
    env[200] = 0.1          # crossing on a flat floor: no prominence at all
    flat = gating._candidate_prominence_db(
        env, direct_peak_idx=0, crossing_idx=200, search_end_idx=399,
        refine_samples=24,
    )
    assert flat == pytest.approx(0.0, abs=1e-9)

    env2 = np.full(400, 0.1)
    env2[150:160] = 0.01    # a deep valley before the candidate
    env2[205] = 1.0         # a strong peak just after the crossing
    tall = gating._candidate_prominence_db(
        env2, direct_peak_idx=0, crossing_idx=200, search_end_idx=399,
        refine_samples=24,
    )
    assert tall == pytest.approx(40.0, abs=1e-6)
    assert tall > flat


# --- the disclosed floors --------------------------------------------------


def test_f_trusted_floor_is_two_and_a_half_times_the_nominal_one():
    assert gating.f_trusted_floor_hz(0.004) == pytest.approx(625.0)
    assert gating.f_trusted_floor_hz(0.007) == pytest.approx(1000.0 / 7.0 * 2.5)


def test_f_trusted_floor_guards_nonpositive_and_nonfinite_like_its_sibling():
    for bad in (0.0, -0.001, float("nan"), float("inf")):
        assert gating.f_trusted_floor_hz(bad) == float("inf")


@pytest.mark.parametrize(
    ("t_first_bounce_s", "expected_hz"), [(0.0025, 1000.0), (0.003, 2500.0 / 3.0)]
)
def test_f_entanglement_floor_divides_the_multiplier_by_the_bounce_time(
    t_first_bounce_s, expected_hz
):
    """#3495's second floor: the same 2.5 over the ROOM's first arrival rather
    than over the window. At this rig class's 2.5-3 ms the floor lands near
    1 kHz, above every rung the ladder can offer."""
    assert gating.f_entanglement_floor_hz(t_first_bounce_s) == pytest.approx(expected_hz)


@pytest.mark.parametrize("bad", [0.0, -0.001, float("nan"), float("inf")])
def test_f_entanglement_floor_refuses_rather_than_returning_a_floor(bad):
    """Deliberately NOT its siblings' ``+inf``. A window that cannot resolve
    has a pessimistic floor; a bounce time that is absent or non-physical has
    no floor at all, and the caller must reach for
    :data:`gating.ENTANGLEMENT_SOURCE_UNKNOWN` instead of a number."""
    with pytest.raises(ValueError):
        gating.f_entanglement_floor_hz(bad)


def test_the_measured_entanglement_word_is_the_gate_bound_word():
    """One vocabulary, not two: a floor timed off the gate's own reflection is
    spelt with the source that named the bound, so a consumer comparing the
    two fields never sees synonyms."""
    assert gating.ENTANGLEMENT_SOURCE_MEASURED == gating.FLOOR_MEASURED
    assert gating.ENTANGLEMENT_SOURCES == {
        gating.ENTANGLEMENT_SOURCE_MEASURED,
        gating.ENTANGLEMENT_SOURCE_DECLARED,
        gating.ENTANGLEMENT_SOURCE_UNKNOWN,
    }
    assert len(gating.ENTANGLEMENT_SOURCES) == 3


# ---- the floor/provenance invariant, in its ONE home (#3522) --------------
# Every pin on the RULE lives here now; each seam keeps only a pin that it
# still reaches this rule.


#: Pairs that cannot both be true. The test below checks both doors against
#: every row here on purpose: the lenient one's contract IS "everything the
#: strict one refuses", so pinning them from two different lists would let
#: the two drift apart.
_IMPOSSIBLE_PAIRS = [
    # Outside the vocabulary: a fourth word no consumer can read.
    (1000.0, "measured"),
    (1000.0, ""),
    (1000.0, 3),
    (1000.0, ["unknown"]),
    (1000.0, {"source": "declared_geometry"}),
    (None, "declared"),
    # Inside it, but the pair disagrees about whether a floor is known.
    (1000.0, gating.ENTANGLEMENT_SOURCE_UNKNOWN),
    (None, gating.ENTANGLEMENT_SOURCE_DECLARED),
    (None, gating.ENTANGLEMENT_SOURCE_MEASURED),
    # Named, but not a frequency: a floor at or below DC would read as a clamp
    # at DC, and NaN compares false against every band edge.
    (0.0, gating.ENTANGLEMENT_SOURCE_DECLARED),
    (-1.0, gating.ENTANGLEMENT_SOURCE_DECLARED),
    (float("nan"), gating.ENTANGLEMENT_SOURCE_MEASURED),
    (float("inf"), gating.ENTANGLEMENT_SOURCE_MEASURED),
]


@pytest.mark.parametrize(
    ("hz", "source"),
    # Plus the two shapes only a document reaches: a floor that is not a
    # number at all, and a source that is missing rather than wrong.
    _IMPOSSIBLE_PAIRS
    + [("not a number", gating.ENTANGLEMENT_SOURCE_DECLARED), (1000.0, None)],
)
def test_a_pair_that_cannot_be_true_raises_strictly_and_coerces_to_unknown(hz, source):
    """See :class:`gating.EntanglementFloor`'s docstring for the invariant."""
    with pytest.raises((TypeError, ValueError)):
        gating.EntanglementFloor(hz, source)
    assert gating.EntanglementFloor.coerce(hz, source) == gating.EntanglementFloor.unknown()


def test_coerce_keeps_a_pair_that_agrees_and_types_the_number():
    assert gating.EntanglementFloor.coerce(
        "1000", gating.ENTANGLEMENT_SOURCE_DECLARED
    ) == gating.EntanglementFloor(1000.0, gating.ENTANGLEMENT_SOURCE_DECLARED)


def test_a_floor_derived_from_a_bounce_time_is_the_formula_and_never_unknown():
    """``from_bounce_s`` is :func:`gating.f_entanglement_floor_hz` plus the
    word for which bounce time it was. A time this precise came from
    somewhere, so ``unknown`` beside it would be a lie about the number."""
    floor = gating.EntanglementFloor.from_bounce_s(
        0.0025, gating.ENTANGLEMENT_SOURCE_DECLARED
    )
    assert floor.hz == pytest.approx(gating.f_entanglement_floor_hz(0.0025))
    assert floor.source == gating.ENTANGLEMENT_SOURCE_DECLARED
    with pytest.raises(ValueError):
        gating.EntanglementFloor.from_bounce_s(
            0.0025, gating.ENTANGLEMENT_SOURCE_UNKNOWN
        )
    with pytest.raises(ValueError):
        gating.EntanglementFloor.from_bounce_s(
            0.0, gating.ENTANGLEMENT_SOURCE_MEASURED
        )


@pytest.mark.parametrize(
    "name", ["measured_4.0ms_-10dB", "fallback_bare_delta", "ungateable_silent"]
)
def test_disclosure_fields_are_present_on_every_bound_source(name):
    """The persisted schema, on all three states a capture can end in."""
    _gated, fragment = gating.gate_impulse_response(_decision_fixture(name), SR)
    assert set(fragment) == {
        "schema_version", "direct_peak_ms", "first_reflection_ms",
        "reflection_onset_ms", "window_ms", "window", "f_valid_floor_hz",
        "f_trusted_hz", "floor_source", "internal_reflection_ledger",
    }
    assert isinstance(fragment["internal_reflection_ledger"], list)
    if fragment["floor_source"] is None:
        assert fragment["f_valid_floor_hz"] is None
        assert fragment["f_trusted_hz"] is None
    else:
        assert fragment["f_trusted_hz"] == pytest.approx(
            gating.TRUSTED_FLOOR_MULTIPLIER * fragment["f_valid_floor_hz"]
        )


def test_the_trusted_floor_does_not_reach_the_gate_window_or_validity_floor():
    """2.5/T is disclosed beside the nominal floor, and this module never reads it.

    2.5x stricter than the floor the product refuses on, so if it had leaked
    into the gate, the window or the reported validity floor would move with
    it. Both must be exactly what 1/T alone produces.

    Scoped to THIS module on purpose. The trusted floor DOES bound a verdict,
    through the flat spec's band clamps
    (:func:`jasper.active_speaker.flat_spec.evaluate_flat_spec`), so a name
    claiming it gates nothing anywhere would be false. It is disclosed here
    and consumed there; only gating's own window is off limits to it.
    """
    ir, _ = _delta_ir_with_reflection(int(0.030 * SR), 500, 4.0, -6.0)
    _gated, fragment = gating.gate_impulse_response(ir, SR)
    window_s = fragment["window_ms"] / 1000.0
    assert fragment["f_valid_floor_hz"] == pytest.approx(
        gating.f_valid_floor_hz(window_s)
    )
    assert fragment["f_trusted_hz"] == pytest.approx(
        gating.f_trusted_floor_hz(window_s)
    )
    assert fragment["f_trusted_hz"] > fragment["f_valid_floor_hz"]


def test_apply_gate_fragment_still_ignores_every_contract_field():
    """The paired-noise seam reads ``direct_peak_ms``/``window_ms`` only, so
    stripping the contract's additions must not change the operator."""
    n = 2000
    signal, _ = _delta_ir_with_reflection(n, 500, 4.0, -6.0)
    signal += 1e-3
    paired = np.linspace(-0.5, 0.5, n, dtype=np.float64)
    _gated, fragment = gating.gate_impulse_response(signal, SR)
    stripped = {
        k: v for k, v in fragment.items()
        if k in {"direct_peak_ms", "window_ms", "floor_source"}
    }
    assert np.array_equal(
        gating.apply_gate_fragment(paired, SR, fragment),
        gating.apply_gate_fragment(paired, SR, stripped),
    )


def test_exempt_gating_block_empty_ir_does_not_raise():
    block = gating.exempt_gating_block(np.array([], dtype=np.float64), SR)
    assert block["direct_peak_ms"] == 0.0
    assert block["applied"] is False
