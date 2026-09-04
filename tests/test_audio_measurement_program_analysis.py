# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure analysis of an excitation-program capture (crossover conductor W1).

Synthetic-fixture round-trips for
docs/historical/crossover-measurement-productization-design.md §5.6. A "capture" is
composed by convolving each program channel with a distinct synthetic driver
IR (a band-passed impulse at a known delay/amplitude/polarity), summing to
mono, applying a known global offset + clock drift ε + additive noise, and —
for the glitch case — deleting a run of samples. The analysis must recover:

  - clock drift ε within ±2 ppm (two identical woofer sweeps at a known
    scheduled separation, design §3.1);
  - tweeter-vs-woofer relative delay within ±5 µs, with the documented sign
    convention (positive delay_us ⇒ tweeter earlier ⇒ delay the tweeter);
  - polarity (correlation sign, cross-checked against the flatter sum);
  - branch trims within ±0.3 dB;
  - a dropped-buffer glitch ⇒ ``glitch_detected`` (capture must be rejected);
  - per-segment clip runs;
  - the CHECK behavioral linearity verdict (an AGC-compressed pilot delta fails).

Runtime is kept low with short (≥0.5 s) sweeps and 48 kHz mono buffers.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import functools
import math
from fractions import Fraction

import numpy as np
import pytest
from scipy.signal import fftconvolve, resample_poly

from jasper.audio_measurement import analysis as analysis_mod
from jasper.audio_measurement import (
    deconv,
    gate_disclosure,
    gating,
    program_analysis,
    snr_policy,
)
from jasper.audio_measurement.alignment import (
    GCC_UPSAMPLE,
    _gcc_correlation,
    _gcc_local_peak_snap,
    gcc_phat,
)
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.frame_fit import fit_frame
from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
    KIND_SWEEP,
    PROGRAM_PHASE_MEASURE,
    RoleBand,
    _finalize,
    _occurrence_suffix,
    _seconds_to_samples,
    _silence,
    _stimulus,
    _sweep_meta,
    build_check_program,
    build_measure_program,
    build_verify_program,
    mesm_gap_samples,
    render_program_pcm,
)
from jasper.audio_measurement.comparison_bands import (
    branch_snr_band_hz,
    crossover_region_band_hz,
    overlap_band_hz,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY,
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_DECLARED_POLARITY_OBJECTIVES,
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_FLATNESS_MAX_STEPS,
    ALIGNMENT_FLATNESS_STEP_US,
    ALIGNMENT_OK,
    ALIGNMENT_SNR_REFUSAL_VERDICT,
    AMBIENT_MIN_USABLE_FRACTION,
    AppliedAlignment,
    AMBIENT_NONSTATIONARITY_DB,
    CAPTURE_BOUND_MARGIN_S,
    GAIN_MAX_DIGITAL_PEAK_DBFS,
    GCC_SNAP_RADIUS_PERIODS,
    IR_POST_MS,
    IR_PRE_MS,
    ConfiguredPathConditioningError,
    ILL_CONDITIONED_PROTECTION_DEEMBEDDING,
    GAIN_BOUND_CAPTURE_FLOOR,
    GAIN_BOUND_DEGENERATE_AMBIENT,
    GAIN_BOUNDS,
    GAIN_BOUND_FLAT_TARGET,
    GAIN_BOUND_NO_AMBIENT_EVIDENCE,
    GAIN_BOUND_PILOT_SNR,
    GAIN_BOUND_ROOM_SNR,
    LINEARITY_SNR_BIAS_BUDGET_FRACTION,
    LINEARITY_TOLERANCE_DB,
    MEASURE_SNR_SOLVE_MARGIN_DB,
    PILOT_MIN_SNR_DB,
    RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB,
    RIPPLE_TRIM_MAX_DB,
    RIPPLE_TRIM_MIN_DB,

    RIPPLE_TRIM_SEARCH_WINDOW_DB,
    SWEEP_PEAK_TO_RMS_DB,
    AlignmentEstimate,
    MeasurementGeometry,
    MeasurementPriors,
    PilotObservation,
    _aligned_branch_tf,
    _ambient_from_capture,
    _band_average_db,
    _band_exclusive_pieces,
    _build_candidate,
    _complex_tf,
    _compose_configured_path_ir,
    _deconvolve_window,
    _gate_floor_hz,
    _global_offset,
    _locate_segments,
    _n_fft_for,
    _peak_dbfs,
    _ripple_db,
    _select_alignment_pair,
    _snr_floor_ok,
    _solve_gain_plan,
    _sweep_occurrence_index,
    ABSOLUTE_NO_FC,
    ABSOLUTE_NO_TARGET,
    ABSOLUTE_NO_TRUSTED_BAND,
    analysis_diagnostic_summary,
    analyze_program_capture,
    DRIVER_SNR_ALIGNMENT_KEY,
    driver_alignment_snr_verdict,
    driver_snr_verdict,
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    branch_level_bands_hz,
    predicted_branch_sum,
    realized_branch_level_match,
    solve_branch_trims,
    solve_ripple_optimal_trim,
    summed_model_residual_delay_us,
)
from jasper.active_speaker.branch_chain import (
    CrossoverSection,
    crossover_response_complex,
    radiating_band_hz,
)
from jasper.active_speaker.driver_protection import driver_protection_profile
from jasper.active_speaker.excitation_safety_plan import (
    resolve_driver_excitation_ceilings,
)
from tests._flat_lin_corpus import (
    CDHORN_CALIBRATION,
    CDHORN_ROOT,
    CORPUS_CALIBRATION_SIGN_CONVENTION,
    requires_cdhorn,
    sweep_anchored_global_offset,
)

SR = 48_000
FC_HZ = 1600.0
GLOBAL_OFFSET = 1234


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _band_impulse(delay: int, f_lo: float, f_hi: float, amp: float, n: int = 4096) -> np.ndarray:
    """A band-passed impulse at ``delay`` samples — a synthetic driver IR."""
    imp = np.zeros(n)
    imp[delay] = amp
    spectrum = np.fft.rfft(imp)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    return np.fft.irfft(spectrum, n)


def _synthesize(
    program,
    *,
    woofer_ir: np.ndarray,
    tweeter_ir: np.ndarray,
    global_offset: int = GLOBAL_OFFSET,
    epsilon: float = 0.0,
    noise: float = 1e-4,
    seed: int = 0,
) -> np.ndarray:
    """Compose a mono capture from a program + per-channel synthetic driver IRs."""
    pcm = render_program_pcm(program)
    mono = np.zeros(pcm.shape[0], dtype=np.float64)
    if pcm.shape[1] >= 1:
        mono += fftconvolve(pcm[:, 0], woofer_ir)[: pcm.shape[0]]
    if pcm.shape[1] >= 2:
        mono += fftconvolve(pcm[:, 1], tweeter_ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(global_offset), mono, np.zeros(20_000)])
    if epsilon != 0.0:
        frac = Fraction(1.0 + epsilon).limit_denominator(500_000)
        cap = resample_poly(cap, frac.numerator, frac.denominator)
    if noise:
        cap = cap + np.random.default_rng(seed).normal(0.0, noise, cap.size)
    return cap


def _transfers(sections_by_role):
    """What the HOST hands the kernel: per-role ``freqs -> complex response``.

    The kernel may not import ``jasper.active_speaker``
    (``tests/test_correction_boundary_ssot.py``), so priors carry the evaluated
    transfer rather than the sections behind it. This mirrors
    ``crossover_v2_flow._role_transfers``.
    """
    return {
        role: functools.partial(crossover_response_complex, sections=sections)
        for role, sections in sections_by_role.items()
    }


def _configured_path_priors(protection, configured, *, tweeter_sign=-1):
    return MeasurementPriors(
        crossover_fc_hz=FC_HZ,
        measurement_protection_response_by_role=protection,
        configured_crossover_response_by_role=configured,
        configured_polarity_sign_by_role={"woofer": 1, "tweeter": tweeter_sign},
    )


# --------------------------------------------------------------------------- #
# MEASURE — the combined drift/delay/polarity/trim round-trip
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("polarity_amp,expected_polarity", [(0.7, "normal"), (-0.7, "inverted")])
def test_measure_round_trip_recovers_drift_delay_polarity_trims(polarity_amp, expected_polarity):
    # The 2 dB gain-plan asymmetry is incidental regression cover: it makes the
    # trim assertion below fail if the deconvolution ever stops dividing the
    # drive out. Equalizing it would not break this test but WOULD delete that
    # cover — the explicit net is
    # test_measure_analysis_is_invariant_to_the_programmed_drive_gain.
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    d_w = 200
    tau_true = 25  # tweeter arrives 25 samples LATER than the woofer
    eps = 80e-6
    woofer_ir = _band_impulse(d_w, 150.0, 6000.0, 1.0)
    tweeter_ir = _band_impulse(d_w + tau_true, 300.0, 20000.0, polarity_amp)
    cap = _synthesize(prog, woofer_ir=woofer_ir, tweeter_ir=tweeter_ir, epsilon=eps)

    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        geometry=MeasurementGeometry(),  # d=0 ⇒ no parallax
    )

    # Drift within ±2 ppm.
    assert res.drift is not None
    assert res.drift.epsilon_ppm == pytest.approx(eps * 1e6, abs=2.0)
    assert not res.glitch_detected

    # Delay within ±5 µs. tweeter LATER ⇒ delay_us = D_w − D_t < 0.
    expected_delay_us = -tau_true / SR * 1e6
    assert res.alignment is not None
    assert res.alignment.delay_us == pytest.approx(expected_delay_us, abs=5.0)

    # Polarity correct, and it agrees with the flatter-sum cross-check.
    assert res.alignment.polarity == expected_polarity
    assert res.alignment.polarity_agrees_with_sum

    # Trims within ±0.3 dB: the woofer (amp 1.0) is 3.1 dB louder than the
    # tweeter (|amp| 0.7), so the woofer branch is attenuated by ~3.1 dB.
    trims = res.candidate.trim_db
    assert trims["woofer"] == pytest.approx(20.0 * np.log10(0.7), abs=0.3)
    assert trims["tweeter"] == pytest.approx(0.0, abs=0.3)


def test_configured_path_conditioning_accepts_both_inclusive_boundaries():
    """Both ±12 dB VALUE boundaries admit: P exactly ON the floor, C/P exactly
    ON the limit.

    Says nothing about the required-bin mask's FREQUENCY edges — the name reads
    as though it covers those too, and it does not. Those are
    ``test_configured_path_required_mask_frequency_edges_are_inclusive``.
    """
    floor = 10.0 ** (-12.0 / 20.0)
    limit = 10.0 ** (12.0 / 20.0)

    impulse = np.zeros(64)
    impulse[3] = 1.0
    priors = _configured_path_priors(
        {"woofer": lambda f: np.full(f.shape, floor, dtype=np.complex128)},
        {"woofer": lambda f: np.full(f.shape, floor * limit, dtype=np.complex128)},
        tweeter_sign=1,
    )
    composed = _compose_configured_path_ir(
        "woofer", impulse, SR, (0.0, SR / 2), priors
    )
    np.testing.assert_allclose(composed, impulse * limit, rtol=1e-12, atol=1e-12)


@pytest.mark.parametrize("edge", ["lo", "hi"])
def test_configured_path_required_mask_frequency_edges_are_inclusive(edge):
    """A violation sitting exactly ON a band edge is inside the policy.

    Pins ``required = (freqs >= lo_hz) & (freqs <= hi_hz)``. Both comparisons,
    one parameter each, because either could be flipped on its own. Flip one to
    exclusive and that edge bin silently stops being required, so the ±12 dB
    conditioning policy stops binding there and the composition admits a shape
    §4.2 says to refuse.

    Distinct from ``..._accepts_both_inclusive_boundaries``, which is about the
    ±12 dB VALUE boundaries, and from
    ``test_conditioning_binds_on_candidate_required_bins_not_the_driven_band``,
    which is about the mask's SCOPE. This one is only about its edges.
    """
    # The whole guard rests on the edge landing exactly ON an FFT bin: off-grid,
    # >= and > agree there and this test would stay green under the very flip it
    # exists to catch. So take the edges FROM the grid the kernel builds from
    # the same n and sample rate, and assert the hit rather than assume it.
    n = 64
    freqs = np.fft.rfftfreq(n, 1.0 / SR)  # 750.0 Hz bins at SR = 48 kHz
    lo_hz, hi_hz = float(freqs[4]), float(freqs[8])  # 3000.0, 6000.0
    violating_hz = lo_hz if edge == "lo" else hi_hz
    assert np.count_nonzero(freqs == violating_hz) == 1, "edge is not on a bin"
    # One bin further out, for the just-outside half of the boundary check.
    outside_hz = float(freqs[3] if edge == "lo" else freqs[9])

    floor = 10.0 ** (-12.0 / 20.0)

    def _priors_with_p_underfloor_at(bad_hz):
        def protection(f):
            out = np.ones(f.shape, dtype=np.complex128)
            out[f == bad_hz] = floor * 0.5  # one bin, clearly under the floor
            return out
        return _configured_path_priors(
            {"woofer": protection},
            {"woofer": lambda f: np.ones(f.shape, dtype=np.complex128)},
            tweeter_sign=1,
        )

    impulse = np.zeros(n)
    impulse[3] = 1.0

    # ON the edge: refused.
    with pytest.raises(ConfiguredPathConditioningError, match="P below -12 dB"):
        _compose_configured_path_ir(
            "woofer", impulse, SR, (lo_hz, hi_hz),
            _priors_with_p_underfloor_at(violating_hz),
        )
    # One bin OUTSIDE it: admitted. Without this half, widening the mask by a
    # bin would read as "inclusive" too.
    composed = _compose_configured_path_ir(
        "woofer", impulse, SR, (lo_hz, hi_hz),
        _priors_with_p_underfloor_at(outside_hz),
    )
    assert np.all(np.isfinite(composed))


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("p_nonfinite", "non-finite P"),
        ("c_nonfinite", "non-finite C"),
        ("p_underfloor", "P below -12 dB"),
        ("ratio_over", r"C/P above \+12 dB"),
        ("s_nonfinite", "non-finite S"),
    ],
)
def test_configured_path_conditioning_refuses_without_clipping(case, message):
    floor = 10.0 ** (-12.0 / 20.0)
    limit = 10.0 ** (12.0 / 20.0)
    p_value, c_value = 1.0, 1.0
    if case == "p_nonfinite": p_value = np.nan
    if case == "c_nonfinite": c_value = np.inf
    if case == "p_underfloor": p_value = floor * (1.0 - 1e-6)
    if case == "ratio_over": c_value = limit * (1.0 + 1e-6)

    impulse = np.zeros(64)
    impulse[3] = np.nan if case == "s_nonfinite" else 1.0
    priors = _configured_path_priors(
        {"woofer": lambda f: np.full(f.shape, p_value, dtype=np.complex128)},
        {"woofer": lambda f: np.full(f.shape, c_value, dtype=np.complex128)},
        tweeter_sign=1,
    )
    with pytest.raises(
        ConfiguredPathConditioningError,
        match=f"{ILL_CONDITIONED_PROTECTION_DEEMBEDDING}.*{message}",
    ) as exc_info:
        _compose_configured_path_ir("woofer", impulse, SR, (0.0, SR / 2), priors)
    assert exc_info.value.slug == ILL_CONDITIONED_PROTECTION_DEEMBEDDING
    # Pins the WIRING, not just the flag: only the |P| < floor branch may set
    # it, and it is what routes the two branches to different household copy.
    assert exc_info.value.protection_floor is (case == "p_underfloor")


def test_conditioning_binds_on_candidate_required_bins_not_the_driven_band():
    """§4.2 scopes the policy to candidate-REQUIRED bins, and only those.

    The driven band is a superset; over-refusing a frozen contract shows up as
    hard stops (an analog LR4 |P| crosses -12 dB at 0.761*fc; #1654 pushes
    sweep floors toward it — panel SF2). Both directions: no consumed bin may
    be weakened.
    """
    def priors(candidate_band):
        # P = HP4@1200, C = HP4@2400: |P| crosses the -12 dB floor at ~918 Hz.
        return dataclasses.replace(
            _configured_path_priors(
                _transfers({"woofer": (CrossoverSection(1200.0, 4, True),)}),
                _transfers({"woofer": (CrossoverSection(2400.0, 4, True),)}),
                tweeter_sign=1,
            ),
            candidate_required_band_hz_by_role={"woofer": candidate_band},
        )

    impulse = np.zeros(4096)
    impulse[7] = 1.0
    driven = (600.0, 20000.0)

    # ADMITS: the sweep drove to 600 Hz, far under the floor — but no candidate
    # fits or compares there, so those bins are not evidence.
    composed = _compose_configured_path_ir(
        "woofer", impulse, SR, driven, priors((1200.0, 20000.0)),
    )
    assert np.all(np.isfinite(composed))

    # STILL REFUSES when a candidate DOES require them — nothing consumed is
    # exempted.
    with pytest.raises(ConfiguredPathConditioningError, match="P below -12 dB"):
        _compose_configured_path_ir(
            "woofer", impulse, SR, driven, priors((600.0, 20000.0)),
        )


# The live fixed-Fc checkpoint. Its DECLARATIONS were read from jts3 on
# 2026-08-05 and are hardcoded on purpose.
#
# What this block catches, stated narrowly — an earlier version claimed three
# things and covered one:
#
#   LIVE. The sweep-floor derivation. The driven bands are recomputed from the
#   declaration on every run and checked against what jts3 was read to sweep,
#   so moving that derivation fails HERE, in seconds, rather than at the
#   hardware bench with a household waiting. The declared protection corner is
#   live too, against its code-owned class minimum.
#
#   TRANSCRIBED. The +-12 dB conditioning constants and the required-bin mask.
#   ``floor_db`` below copies the kernel's function-local literals
#   (``_compose_configured_path_ir``'s ``10.0 ** (-12.0 / 20.0)`` and
#   ``10.0 ** (12.0 / 20.0)``), which have no exported name to import — a
#   second copy of a number, which is the thing this block exists to avoid.
#   Measured, not assumed: loosening the kernel
#   floor to -13 dB leaves this block green, and so does over-scoping the mask
#   to the whole driven band. Each is caught elsewhere in this module — by
#   test_configured_path_conditioning_refuses_without_clipping[p_underfloor...]
#   and test_conditioning_binds_on_candidate_required_bins_not_the_driven_band
#   respectively — so the coverage exists; it just is not here. Making them
#   live here wants a named kernel constant to import, which is its own PR.
#
# Why the live half is derived rather than transcribed: #1654 changed the
# sweep-floor derivation — moving the tweeter's floor from the measurement
# band's 2000 Hz down to its declared hard floor of 1600 Hz, so a sub-2-kHz Fc
# candidate can be judged where it actually hands over — and this guard stayed
# green through the exact change it promised to catch, because the literal WAS
# the input. It is now an expectation checked against the live derivation.
_CHECKPOINT_FC_HZ = 2000.0
# jts3's declaration, in the shape the resolver reads.
# ``max_effective_peak_dbfs`` is NOT a recorded jts3 value on either role, and
# since 2026-08-23 the resolver does not require the field at all (absence is
# the ordinary shape); the band derivation never reads it either, so -65.0 is
# filler kept only because it matches what jts3 has on disk. It is the class-default seed for the TWEETER only
# (``driver_protection``'s high-frequency branch); the woofer's low-frequency
# class default is ``MAX_TEST_LEVEL_DBFS`` = 0.0, and -65.0 there is just the
# conservative value matching its sibling.
_CHECKPOINT_DECLARED_TARGETS = {
    "woofer": {
        "target_id": "mono:woofer",
        "target_fingerprint": "checkpoint-woofer",
        "role": "woofer",
        "hard_excitation_band_hz": [45, 4000],
        "measurement_band_hz": [60, 4000],
        "required_protection_filters": [],
        "level_duration_limits": {"max_effective_peak_dbfs": -65.0},
    },
    "tweeter": {
        "target_id": "mono:tweeter",
        "target_fingerprint": "checkpoint-tweeter",
        "role": "tweeter",
        # A compression driver, which is what makes a 2000 Hz declared
        # high-pass legal: it sits exactly ON that style's code-owned class
        # minimum. Asserted below, so a policy move fails here too.
        "driver_style": "compression_driver",
        "hard_excitation_band_hz": [1600, 20_000],
        "measurement_band_hz": [2000, 18_000],
        "required_protection_filters": [
            {"kind": "highpass", "cutoff_hz": 2000,
             "minimum_slope_db_per_octave": 24},
        ],
        "level_duration_limits": {"max_effective_peak_dbfs": -65.0},
    },
}
# The REALIZED protection graph. Order 4 (LR4) stays recorded rather than
# inferred: the declaration above states a MINIMUM slope, which an LR8 would
# also satisfy. The corner is tied back to the declaration by assertion.
_CHECKPOINT_PROTECTION = {
    "woofer": (), "tweeter": (CrossoverSection(2000.0, 4, True),),
}


def _checkpoint_driven_hz():
    """Each role's swept band, derived the way a live session derives it.

    Two stages, both real. ``resolve_driver_excitation_ceilings`` turns the
    declaration into a permitted band — ``program_admission=True`` is the
    conductor's own flag (``jasper.web.correction_crossover_v2``), since these
    bands exist to serve the admission-gated CHECK/MEASURE programs. Then
    ``build_measure_program`` intersects that with the MEASURE sweep window,
    which is what puts the woofer at 150 Hz rather than its declared 60 Hz
    analysis floor. The analysis reads a sweep SEGMENT's ``(f1, f2)``, so the
    segments are what this returns — the peaks and durations are the module's
    standard MEASURE fixture values and do not touch the bands.

    Both of the tweeter's edges come from its declared HARD band rather than
    its narrower measurement band: the upper since #1668, the lower since
    #1654. Only the lower one is load-bearing below — every margin in these
    tests is |P| at that edge.
    """
    profile = {"targets": list(_CHECKPOINT_DECLARED_TARGETS.values())}
    roles = [
        RoleBand(
            role, channel,
            resolve_driver_excitation_ceilings(
                profile, target["target_fingerprint"], program_admission=True,
            )[0],
        )
        for channel, (role, target) in enumerate(
            _CHECKPOINT_DECLARED_TARGETS.items()
        )
    ]
    program = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, roles,
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    return {
        seg.role: (seg.f1_hz, seg.f2_hz)
        for seg in (program.segment("sweep_w"), program.segment("sweep_t"))
    }


_CHECKPOINT_DRIVEN_HZ = _checkpoint_driven_hz()


def _checkpoint_priors(fc_hz, protection):
    configured = {"woofer": (CrossoverSection(fc_hz, 4, False),),
                  "tweeter": (CrossoverSection(fc_hz, 4, True),)}
    lo, hi = overlap_band_hz(fc_hz)
    return dataclasses.replace(
        _configured_path_priors(
            _transfers(protection), _transfers(configured), tweeter_sign=1,
        ),
        crossover_fc_hz=fc_hz,
        # An INDEPENDENT reproduction of the union whose one owner is
        # ``crossover_v2.priors.candidate_required_band_hz`` (it lived on
        # ``CrossoverV2Session._measure_priors`` when this helper was
        # written, and this citation followed it there and then to
        # ``priors.measure_priors``). Deliberately not a call to that owner:
        # this is the KERNEL's own statement of the bins it expects to be
        # given, so a drift in the owner should make this disagree rather than
        # follow along. Anchoring it on the thing under test is the blindness
        # the sweep golden's header records.
        candidate_required_band_hz_by_role={
            role: (min(radiating_band_hz(sec)[0], lo),
                   max(radiating_band_hz(sec)[1], hi))
            for role, sec in configured.items()
        },
    )


def test_the_live_checkpoint_declarations_compose():
    """(a) + (b): both roles ADMIT, with the margins the ruling records.

    Opens with the two facts everything below rests on: that the derivation
    still lands on the bands jts3 was read to sweep, and that the protection
    corner is still the class minimum it was declared against.
    """
    # THE tripwire. The right-hand side is what jts3 was read to sweep; the
    # left-hand side is derived from its declaration on every run. Move the
    # sweep-floor derivation and this line fails, naming both numbers.
    assert _CHECKPOINT_DRIVEN_HZ == {
        "woofer": (150.0, 4000.0), "tweeter": (1600.0, 20000.0),
    }
    # The corner that makes the tweeter's margin thin is not a household
    # choice: the declared high-pass sits exactly ON the code-owned class
    # minimum for this driver style, so a policy move breaks the declaration
    # rather than quietly following it down. Both halves pinned — the realized
    # graph against the declaration, the declaration against the policy.
    declared_hp = _CHECKPOINT_DECLARED_TARGETS["tweeter"]
    corner_hz = declared_hp["required_protection_filters"][0]["cutoff_hz"]
    assert _CHECKPOINT_PROTECTION["tweeter"][0].fc_hz == corner_hz
    assert driver_protection_profile(
        "tweeter", driver_style=declared_hp["driver_style"],
    ).min_highpass_hz == corner_hz

    impulse = np.zeros(16_384)
    impulse[9] = 1.0
    priors = _checkpoint_priors(_CHECKPOINT_FC_HZ, _CHECKPOINT_PROTECTION)
    for role, driven in _CHECKPOINT_DRIVEN_HZ.items():
        composed = _compose_configured_path_ir(role, impulse, SR, driven, priors)
        assert np.all(np.isfinite(composed)), role

    # |P| at each role's required-mask lower edge. The tweeter's edge is its
    # driven floor, now 1600 Hz (#1654) — 0.8x its 2000 Hz protection corner,
    # where an LR4 high-pass has fallen to -10.79 dB, leaving only 1.21 dB over
    # the -12 dB refusal. THAT margin is the governing constraint on how low an
    # Fc candidate may be proposed, and it was 5.98 dB before #1654 widened the
    # sweep. It does not erode further as candidates move down: P is the
    # PROTECTION transfer, fixed at 2000 Hz whatever Fc is being evaluated, and
    # the mask floor is pinned by the sweep floor rather than by the candidate.
    floor_db = -12.0
    at_floor = np.abs(crossover_response_complex(
        np.array([_CHECKPOINT_DRIVEN_HZ["tweeter"][0]]),
        _CHECKPOINT_PROTECTION["tweeter"]))[0]
    assert 20 * np.log10(at_floor) == pytest.approx(-10.786, abs=0.01)
    assert 20 * np.log10(at_floor) - floor_db == pytest.approx(1.214, abs=0.01)
    # On the analysis grid the edge lands on the first bin at or above 1600 Hz
    # (1602.54 Hz here), so the realized margin is 1.25 dB: the figure is
    # grid-dependent, not a pure LR4 constant. Both are quoted because the
    # ruling's number is the continuous one and the refusal uses the grid one.
    freqs = np.fft.rfftfreq(impulse.size, 1.0 / SR)
    edge = freqs[freqs >= _CHECKPOINT_DRIVEN_HZ["tweeter"][0]][0]
    on_grid = np.abs(crossover_response_complex(
        np.array([edge]), _CHECKPOINT_PROTECTION["tweeter"]))[0]
    assert on_grid == pytest.approx(0.290190, abs=1e-6)
    assert 20 * np.log10(on_grid) - floor_db == pytest.approx(1.254, abs=0.01)
    assert 20 * np.log10(on_grid) - floor_db > 0.0, "the checkpoint must admit"
    # The woofer declares no protection at all, so |P| is unity across its whole
    # driven band — the full 12 dB of margin.
    woofer_p = np.abs(crossover_response_complex(
        freqs, _CHECKPOINT_PROTECTION["woofer"]))
    assert woofer_p.min() == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize(
    ("corner_hz", "admits"),
    # (c): the reconciliation's closed form is corner <= K * max(driven_lo,
    # Fc/2), and K is NOT a constant. The ANALOG LR4 prototype is
    # scale-invariant — |P| = w^4/(1+w^4) crosses -12 dB at w = 0.761039, i.e.
    # K = 1.313993 at every frequency — but the shipped response is the
    # bilinear/tan-prewarped DIGITAL realization at 48 kHz, so K sags below
    # that analog value, and sags further as the floor rises: ON-GRID, 1.31052
    # at this 1600 Hz floor and 1.30859 at the 2000 Hz floor #1654 replaced.
    # Say which floor AND which basis: the same K solved CONTINUOUSLY at
    # exactly 1600 Hz is 1.31053 (the figure the crossover-measurement-v2
    # campaign record quotes), because the grid edge bin is 1602.54 Hz, not 1600.
    # Both solved against the shipped response on the analysis grid: |P| at the
    # 1602.54 Hz edge bin hits the -12 dB floor at corner = 2100.15 Hz (it was
    # 2618.47 Hz before the sweep widened, which is why every row here moved).
    [(2000.0, True), (2100.0, True), (2200.0, False), (2617.0, False)],
)
def test_a_higher_declared_protection_corner_is_the_refusing_direction(
    corner_hz, admits,
):
    """(c): the direction a declared corner has to move to break composition."""
    impulse = np.zeros(16_384)
    impulse[9] = 1.0
    protection = {"woofer": (), "tweeter": (CrossoverSection(corner_hz, 4, True),)}
    priors = _checkpoint_priors(_CHECKPOINT_FC_HZ, protection)
    call = functools.partial(
        _compose_configured_path_ir, "tweeter", impulse, SR,
        _CHECKPOINT_DRIVEN_HZ["tweeter"], priors,
    )
    if admits:
        assert np.all(np.isfinite(call()))
    else:
        with pytest.raises(ConfiguredPathConditioningError, match="P below -12 dB"):
            call()


def test_configured_path_refuses_a_segment_with_no_declared_band():
    """No declared sweep band is MISSING EVIDENCE, not ill-conditioning.

    No sweep bounds means no candidate-required bins to hold the §4.2 policy
    over, so the seam refuses through the kernel's own undeclared-band
    vocabulary — the two route to different household-facing outcomes.
    """
    priors = _configured_path_priors(
        _transfers({"woofer": (CrossoverSection(300.0, 4, True),)}),
        _transfers({"woofer": (CrossoverSection(FC_HZ, 4, True),)}), tweeter_sign=1,
    )
    with pytest.raises(ValueError, match="woofer sweep segment has no declared band") as exc:
        _compose_configured_path_ir("woofer", np.zeros(64), SR, None, priors)
    assert not isinstance(exc.value, ConfiguredPathConditioningError)


def test_measure_negative_drift_round_trip():
    """A slow capture clock (ε < 0) round-trips the same way a fast one does."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 25
    eps = -80e-6
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.7),
        epsilon=eps,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.drift.epsilon_ppm == pytest.approx(eps * 1e6, abs=2.0)
    assert not res.glitch_detected
    assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)
    assert res.alignment.status == ALIGNMENT_OK


def test_delay_beyond_search_window_is_refused():
    """A true delay outside ±search_window must fail loud, not return a
    moderate-confidence clamped value (gate finding S2a: tau=110 samples vs a
    96-sample window once returned −2673 µs at confidence 0.575)."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 110  # beyond the default 2 ms ⇒ 96-sample window
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.alignment.status == ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW
    assert res.alignment.confidence == 0.0
    assert res.candidate.confidence == 0.0


def test_delay_near_search_window_edge_is_accurate():
    """A true delay just INSIDE the window is measured, not refused."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 90  # inside the 96-sample window, 6 samples from the edge
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.alignment.status == ALIGNMENT_OK
    assert res.alignment.confidence > 0.0
    assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)


def test_oversized_capture_is_bounded_and_still_analyzes():
    """A stuck long recording is truncated to program + margin before any
    full-rate FFT (defense at the FFT, 1 GB Pi); the program at the head of
    the capped window still analyzes correctly."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    tau_true = 30
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.8),
        epsilon=0.0,
    )
    bound_samples = prog.total_samples + int(CAPTURE_BOUND_MARGIN_S * SR)
    # A "stuck" tail far beyond the bound.
    stuck = np.concatenate([
        cap, np.random.default_rng(9).normal(0.0, 1e-4, 30 * SR)
    ])
    assert stuck.size > bound_samples
    res = analyze_program_capture(prog, stuck, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert not res.glitch_detected
    assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)


def test_channel_map_fails_on_swapped_channels():
    """Swapped driver wiring ⇒ each pilot window is noise-dominated (a driver
    fed fully out-of-band content produces essentially no linear output), so
    the pilot's energy is NOT concentrated in its declared band and channel-map
    sanity fails.

    The plants are double-convolved band-passes (~−88 dB stopband): a
    single 4096-tap brick-wall mask leaks at ~−44 dB, which would leave a
    coherent in-band ghost of the pilot ABOVE the noise floor — an artifact of
    the fixture, not of a physical driver, which rolls off far deeper when fed
    a fully disjoint band."""
    roles = [
        RoleBand("woofer", 0, FrequencyBand(150.0, 1200.0)),
        RoleBand("tweeter", 1, FrequencyBand(2500.0, 20000.0)),
    ]
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)

    def _deep_plant(delay, f_lo, f_hi, amp):
        single = _band_impulse(delay, f_lo, f_hi, 1.0)
        return amp * fftconvolve(single, single)  # stopband dB doubles

    woofer_plant = _deep_plant(200, 150.0, 1200.0, 1.0)
    tweeter_plant = _deep_plant(225, 2500.0, 20000.0, 0.8)

    def _capture(ch0_plant, ch1_plant, seed):
        mono = (
            fftconvolve(pcm[:, 0], ch0_plant)[: pcm.shape[0]]
            + fftconvolve(pcm[:, 1], ch1_plant)[: pcm.shape[0]]
        )
        cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])
        return cap + np.random.default_rng(seed).normal(0.0, 3e-5, cap.size)

    correct = analyze_program_capture(
        chk, _capture(woofer_plant, tweeter_plant, 11), SR, priors=MeasurementPriors(),
    )
    assert correct.channel_map_ok is True

    swapped = analyze_program_capture(
        chk, _capture(tweeter_plant, woofer_plant, 12), SR, priors=MeasurementPriors(),
    )
    assert swapped.channel_map_ok is False


def test_measure_uses_check_ambient_for_snr_verdicts():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    # A quiet ambient report (from CHECK) ⇒ per-driver SNR verdicts populate.
    ambient = {
        "schema_version": 1,
        "bands": [
            {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -90.0},
        ],
    }
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, ambient_report=ambient),
    )
    woofer = next(r for r in res.driver_responses if r.role == "woofer")
    assert woofer.snr is not None
    assert woofer.snr["decision_class"] == "magnitude"
    # Without an ambient report the SNR block is absent.
    res_none = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert all(r.snr is None for r in res_none.driver_responses)


# --------------------------------------------------------------------------- #
# #1830 — the per-driver SNR verdict must be a MEASUREMENT of the level played
#
# `_measure_priors` carrying CHECK's ambient report (#1972) made the verdict
# populate, but it was still being subtracted from the deconvolved transfer
# function, whose whole point is that the drive cancels
# (`test_measure_analysis_is_invariant_to_the_programmed_drive_gain`). So the
# revived instrument could not see the one thing #1829's SNR-solved levels made
# it exist to see. These pin the units, not the physics.
# --------------------------------------------------------------------------- #

# The two relevant bands at FC_HZ: `relevant_hz` is (FC_HZ/2, FC_HZ*2), and
# `transition` (350-1000 Hz) and `mid` (1000-4000 Hz) are the CROSSOVER_SNR_BANDS_HZ
# entries overlapping it. Asserted below rather than assumed.
SNR_RELEVANT_BAND_IDS = ("transition", "mid")
SNR_DRIVE_BACKOFF_DB = -20.0


def _check_measured_ambient(sigma: float, seed: int = 7) -> dict:
    """CHECK's OWN ambient report, produced by the production analyzer.

    Built by running a real CHECK capture through `analyze_program_capture`
    rather than hand-shaping a dict. That distinction is the whole reason this
    defect survived: `test_measure_uses_check_ambient_for_snr_verdicts` above
    constructs the report by hand, so it never exercised the raw-dBFS shape
    `_analyze_check` actually emits (`snr_policy.framed_ambient_band_report`,
    which carries no `domain` key and therefore unwraps as "raw").
    """
    chk = build_check_program(_roles(), ambient_s=4.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)
    mono = (
        fftconvolve(pcm[:, 0], _band_impulse(200, 150.0, 6000.0, 1.0))[: pcm.shape[0]]
        + fftconvolve(pcm[:, 1], _band_impulse(225, 300.0, 20000.0, 0.7))[: pcm.shape[0]]
    )
    cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(seed).normal(0.0, sigma, cap.size)
    res = analyze_program_capture(chk, cap, SR, priors=MeasurementPriors())
    assert res.ambient_report is not None and res.ambient_report["bands"]
    # Premise: this is a RAW-domain report. If CHECK ever starts emitting a
    # deconvolved one, the verdict's signal side must change with it.
    assert snr_policy.unwrap_noise_report(res.ambient_report)[0] == "raw"
    return res.ambient_report


def _measure_snr_bands(ambient_report, *, drive_offset_db: float, sigma: float):
    """{band_id: entry} of the woofer's per-band SNR at a given drive level."""
    prog = build_measure_program(
        {"woofer": -11.0 + drive_offset_db, "tweeter": -13.0 + drive_offset_db},
        _roles(), sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
        noise=sigma,
    )
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, ambient_report=ambient_report),
        geometry=MeasurementGeometry(),
    )
    woofer = next(r for r in res.driver_responses if r.role == "woofer")
    assert woofer.snr is not None
    return woofer.snr, {b["band_id"]: b for b in woofer.snr["bands"]}


def test_measure_snr_verdict_moves_with_the_measurement_level():
    """A 20 dB quieter MEASURE into an unchanged room must read 20 dB worse.

    THE regression for #1830. The verdict was revived by threading CHECK's
    ambient report into MEASURE's priors, but the signal side stayed
    `magnitude_band_levels` on the deconvolved transfer function — a quantity
    the deconvolution makes invariant to the programmed drive by construction.
    Subtracting a fixed dBFS room floor from an invariant leaves an invariant:
    before this fix both drive levels below reported the SAME worst-band SNR
    to 0.0 dB, so the instrument #1829 needed — "is the quieter per-driver
    response still clear of the floor?" — was structurally unable to answer.
    """
    sigma = 3e-4
    ambient = _check_measured_ambient(sigma)

    loud_block, loud = _measure_snr_bands(ambient, drive_offset_db=0.0, sigma=sigma)
    _quiet_block, quiet = _measure_snr_bands(
        ambient, drive_offset_db=SNR_DRIVE_BACKOFF_DB, sigma=sigma,
    )

    # Premise: these are the bands the decision is actually scoped to.
    assert loud_block["relevant_hz"] == [FC_HZ / 2.0, FC_HZ * 2.0]
    for band_id in SNR_RELEVANT_BAND_IDS:
        lo, hi = loud[band_id]["band_hz"]
        assert hi > FC_HZ / 2.0 and lo < FC_HZ * 2.0

    # Compared per band_id, never via `worst_relevant`: which band wins a tie
    # is snr_policy's business and must not be what this test rests on.
    for band_id in SNR_RELEVANT_BAND_IDS:
        moved = quiet[band_id]["estimated_snr_db"] - loud[band_id]["estimated_snr_db"]
        assert moved == pytest.approx(SNR_DRIVE_BACKOFF_DB, abs=0.5), (
            f"{band_id} SNR moved {moved:.2f} dB for a "
            f"{SNR_DRIVE_BACKOFF_DB:.0f} dB quieter measurement — the verdict is "
            "being read out of the drive-invariant deconvolved domain again"
        )
        assert loud[band_id]["method"] == "fft_band_power_difference"


def test_measure_backed_off_into_a_noisy_room_reports_insufficient():
    """The behaviour change #1830 ships, stated as the household sees it.

    One room, one pair of drivers, two MEASURE levels. The louder one clears
    `DRIVER.snr_ok_db` in both bands the crossover decision reads; backing the
    drive off by 20 dB — which is what #1829's SNR solve does when the room
    allows it — puts both bands under `DRIVER.snr_warn_db`, and the shipped
    magnitude verdict for that is `insufficient` (`snr_policy._band_verdict`).

    So a session that passes today can begin disclosing `insufficient` after
    this fix. That is the instrument working: before it, the quiet arm below
    reported the loud arm's verdict, because the number never moved.

    Asserts the overall verdict, not which band carries it — every relevant
    band lands on the same verdict, so `worst_relevant`'s tie-break cannot
    change the answer.
    """
    # Chosen so the loud arm clears `snr_ok_db` in both relevant bands and the
    # 20 dB back-off puts both under `snr_warn_db` — the verdict has to cross
    # BOTH thresholds for this to pin the grading rather than one edge of it.
    sigma = 1.2e-2
    room = _check_measured_ambient(sigma)

    loud_block, loud = _measure_snr_bands(room, drive_offset_db=0.0, sigma=sigma)
    quiet_block, quiet = _measure_snr_bands(
        room, drive_offset_db=SNR_DRIVE_BACKOFF_DB, sigma=sigma,
    )

    assert loud_block["verdict"] == "ok", (
        "control arm must pass, or the failing arm proves nothing"
    )
    assert quiet_block["verdict"] == "insufficient"
    for band_id in SNR_RELEVANT_BAND_IDS:
        assert loud[band_id]["estimated_snr_db"] >= DRIVER.snr_ok_db
        assert loud[band_id]["verdict"] == "ok"
        entry = quiet[band_id]
        assert entry["estimated_snr_db"] < DRIVER.snr_warn_db
        assert entry["verdict"] == "insufficient"
        assert entry["shortfall_db"] > 0.0


def test_driver_snr_verdict_is_absent_rather_than_computed_across_domains():
    """Fail closed: a raw noise report with no raw capture to pair it against
    yields NO verdict, never a cross-domain one.

    `_driver_response`'s ambient argument is optional and its raw-capture
    argument is too, so nothing but this rule stops the two sides of the
    subtraction from drifting back apart. The deconvolved arm below is the
    other half: a report that says it IS deconvolved still reads the transfer
    function, which is the only domain pairing that was ever correct here.
    """
    ir = _band_impulse(200, 150.0, 6000.0, 1.0)
    raw_report = {"schema_version": 1, "bands": [
        {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -90.0},
    ]}

    unpaired = program_analysis._driver_response(
        "woofer", ir, SR, calibration=None, ambient_report=raw_report,
        fc_hz=FC_HZ, n_fft=8192, capture_segment=None,
    )
    assert unpaired.snr is None

    deconvolved = program_analysis._driver_response(
        "woofer", ir, SR, calibration=None,
        ambient_report={**raw_report, "domain": "deconvolved"},
        fc_hz=FC_HZ, n_fft=8192, capture_segment=None,
    )
    assert deconvolved.snr is not None
    # Only "mid" has a matching noise row; the rest report method "none".
    mid = next(b for b in deconvolved.snr["bands"] if b["band_id"] == "mid")
    assert mid["method"] == "deconvolved_band_difference"

    # A segment that is present but degenerate — a capture truncated before
    # this sweep — reaches "not measured" by the other spelling: a block that
    # says `unknown` over zero bands, never a number.
    degenerate = program_analysis._driver_response(
        "woofer", ir, SR, calibration=None, ambient_report=raw_report,
        fc_hz=FC_HZ, n_fft=8192, capture_segment=np.zeros(0),
    )
    assert degenerate.snr is not None
    assert degenerate.snr["bands"] == []
    assert degenerate.snr["verdict"] == "unknown"
    assert degenerate.snr["worst_relevant"] is None


def test_diagnostic_summary_names_the_band_behind_each_driver_snr_pair():
    """#2613: `{role}_snr_db` and `{role}_snr_verdict` say how bad and how
    trusted, never WHICH band. Fourteen consecutive rounds recorded
    `tweeter_snr_db=-1.2 insufficient` and the limiting band had to be
    re-derived from the crossover frequency and the declared driver bands
    because no artifact carried it. `{role}_snr_band` is that band, read off
    the same `worst_relevant` entry as the other two — checked here against
    the real producer, not a hand-built double."""
    ir = _band_impulse(200, 150.0, 6000.0, 1.0)
    ambient = {"schema_version": 1, "domain": "deconvolved", "bands": [
        {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -90.0},
    ]}
    resp = program_analysis._driver_response(
        "woofer", ir, SR, calibration=None, ambient_report=ambient,
        fc_hz=FC_HZ, n_fft=8192, capture_segment=None,
    )
    worst = resp.snr["worst_relevant"]
    assert worst["band_id"] == "mid"

    summary = analysis_diagnostic_summary(program_analysis.ProgramAnalysis(
        phase=PROGRAM_PHASE_MEASURE, program_id="test", locations=(),
        driver_responses=(resp,),
    ))
    assert summary["woofer_snr_band"] == worst["band_id"]
    # Beside the pair it explains, all three off the one entry.
    assert summary["woofer_snr_db"] == worst["estimated_snr_db"]
    assert summary["woofer_snr_verdict"] == worst["verdict"]


def test_diagnostic_summary_snr_band_is_none_not_a_stand_in_label():
    """`worst_band_verdict` filters candidates on band overlap and verdict
    rank, never on identity, so it can select a band carrying no `band_id`.
    The field then has to read `None` — a fabricated label or the string
    "None" would be indistinguishable from a real band."""
    resp = program_analysis.DriverResponse(
        role="tweeter",
        freqs_hz=np.linspace(100.0, 20000.0, 64),
        magnitude_db=np.zeros(64),
        complex_tf=np.ones(64, dtype=complex),
        gating={"applied": True, "window_ms": 8.0},
        snr={"worst_relevant": {
            "band_id": None, "estimated_snr_db": -1.2, "verdict": "insufficient",
        }},
        validity_floor_hz=None,
    )
    summary = analysis_diagnostic_summary(program_analysis.ProgramAnalysis(
        phase=PROGRAM_PHASE_MEASURE, program_id="test", locations=(),
        driver_responses=(resp,),
    ))
    assert summary["tweeter_snr_band"] is None
    # Present, not merely absent: the key still reports that the pair beside
    # it was read from a band whose identity the block did not carry.
    assert "tweeter_snr_band" in summary
    assert summary["tweeter_snr_verdict"] == "insufficient"


def test_diagnostic_summary_alignment_snr_trio_is_none_when_the_block_predates_it():
    """A ``snr`` dict written before #2640 — magnitude-only, no ``"alignment"``
    key at all — must not raise, and the three alignment keys must still be
    present, with ``None`` values: never a raise, and never a guessed verdict
    for evidence the block never carried."""
    resp = program_analysis.DriverResponse(
        role="tweeter",
        freqs_hz=np.linspace(100.0, 20000.0, 64),
        magnitude_db=np.zeros(64),
        complex_tf=np.ones(64, dtype=complex),
        gating={"applied": True, "window_ms": 8.0},
        snr={"worst_relevant": {
            "band_id": "mid", "estimated_snr_db": 30.0, "verdict": "ok",
        }},
        validity_floor_hz=None,
    )
    summary = analysis_diagnostic_summary(program_analysis.ProgramAnalysis(
        phase=PROGRAM_PHASE_MEASURE, program_id="test", locations=(),
        driver_responses=(resp,),
    ))
    assert summary["tweeter_alignment_snr_db"] is None
    assert summary["tweeter_alignment_snr_verdict"] is None
    assert summary["tweeter_alignment_snr_band"] is None
    # Present, not merely absent — the same None-vs-absent rule the magnitude
    # trio's own band identity is held to above.
    assert "tweeter_alignment_snr_db" in summary
    assert "tweeter_alignment_snr_verdict" in summary
    assert "tweeter_alignment_snr_band" in summary


@pytest.mark.parametrize(
    "label,capture_len,anchor,expected",
    [
        ("anchor past the capture end", 1000, 5_000_000, 0),
        # `anchor + n_samples` lands far enough below zero that a naive
        # `min(size, anchor + n)` stop is more negative than -size, which numpy
        # clamps to an empty slice — so this case passes either way and is NOT
        # the one with teeth.
        ("anchor far before the capture", 1000, -10**6, 0),
        # THE case with teeth. `anchor + n_samples` lands in (-capture_len, 0),
        # so a bare `min(size, ...)` stop is a NEGATIVE index and numpy reads it
        # as an offset from the END — returning a non-empty slice of the wrong
        # region as if it were this sweep. Sits mid-range between the two
        # above, which is exactly why neither of them catches it.
        ("anchor before the capture, stop lands negative", 1000, -29_000, 0),
        ("partial overlap at the capture tail", 1000, 998, 2),
    ],
)
def test_raw_sweep_segment_clamps_instead_of_raising(
    label, capture_len, anchor, expected,
):
    """`_raw_sweep_segment` never raises, and never returns the WRONG samples,
    on a capture that cannot contain the segment.

    The locator is what fails a truncated capture; the SNR verdict must not
    turn that into an exception inside the analysis — nor quietly hand
    `band_levels_dbfs` a slice of some other part of the recording, which
    would produce a confident number about audio this sweep never played.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    seg = prog.segment("sweep_w")
    got = program_analysis._raw_sweep_segment(np.zeros(capture_len), seg, anchor)
    assert got.size == expected, label


def test_raw_sweep_segment_returns_the_whole_scheduled_segment():
    """The ordinary case: an in-bounds anchor yields exactly `segment.n_samples`.

    Load-bearing beyond "no clamping happened" — it is what makes the
    rectangular window's duty-cycle offset zero (see `_driver_snr_block`).
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    seg = prog.segment("sweep_w")
    full = np.zeros(seg.n_samples + 200)
    assert program_analysis._raw_sweep_segment(full, seg, 100).size == seg.n_samples


def test_measure_no_drift_delay_is_tight():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 40
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.9),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)
    assert res.drift.epsilon_ppm == pytest.approx(0.0, abs=2.0)


# --------------------------------------------------------------------------- #
# sweep-composition PR-A (#1668): N-repeat exposure + era tolerance
# --------------------------------------------------------------------------- #


def _build_old_shaped_measure_program():
    """Hand-built replica of the PRE-#1668 ``build_measure_program`` output:
    guard, sweep_w, gap_w_t, sweep_t, gap_t_w, sweep_w_rep, tail — the woofer
    repeated once, the tweeter never repeated. The current composer can no
    longer produce this asymmetric shape (it is symmetric by construction —
    see ``program.build_measure_program``'s docstring), so era-tolerance
    coverage for the NEW analysis reading an OLD-shaped program has to
    construct it directly from the same primitives the old composer used.
    """
    woofer = RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0))
    tweeter = RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0))
    gain_plan = {"woofer": -11.0, "tweeter": -13.0}
    w_dur, t_dur = 0.8, 0.6
    w_f1, w_f2 = 150.0, 6000.0
    t_f1, t_f2 = 300.0, 20000.0
    w_meta = _sweep_meta(w_f1, w_f2, w_dur, gain_plan["woofer"])
    t_meta = _sweep_meta(t_f1, t_f2, t_dur, gain_plan["tweeter"])

    segments = []
    cursor = 0
    guard_n = _seconds_to_samples(2.0, SR)
    segments.append(_silence("guard", cursor, guard_n))
    cursor += guard_n

    def _sw(seg_id, rb, f1, f2, dur):
        return _stimulus(
            segment_id=seg_id, kind=KIND_SWEEP, role=rb.role, channel=rb.channel,
            start=cursor, f1_hz=f1, f2_hz=f2, duration_s=dur,
            gain_db=gain_plan[rb.role], downstream_gain_db=0.0,
        )

    sweep_w = _sw("sweep_w", woofer, w_f1, w_f2, w_dur)
    segments.append(sweep_w)
    cursor += sweep_w.n_samples
    gap_w = mesm_gap_samples(w_meta, ir_tail_s=0.5)
    segments.append(_silence("gap_w_t", cursor, gap_w))
    cursor += gap_w

    sweep_t = _sw("sweep_t", tweeter, t_f1, t_f2, t_dur)
    segments.append(sweep_t)
    cursor += sweep_t.n_samples
    gap_t = mesm_gap_samples(t_meta, ir_tail_s=0.5)
    segments.append(_silence("gap_t_w", cursor, gap_t))
    cursor += gap_t

    sweep_w_rep = _sw("sweep_w_rep", woofer, w_f1, w_f2, w_dur)
    segments.append(sweep_w_rep)
    cursor += sweep_w_rep.n_samples

    tail_n = _seconds_to_samples(0.5, SR)
    segments.append(_silence("tail", cursor, tail_n))
    cursor += tail_n

    return _finalize(PROGRAM_PHASE_MEASURE, 2, segments, cursor)


def test_measure_repeat_responses_recover_the_primary_magnitude():
    """Design item 7/8: every occurrence past a driver's first is deconvolved
    + gated + TF'd and attached as ``repeat_responses`` on the primary — this
    pins BOTH the count (N-1 repeats per driver at the N=3 default) and that
    each repeat's recovered magnitude agrees with the primary within noise
    tolerance (they are bit-identical stimuli through the SAME synthetic IR)."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert not res.glitch_detected
    assert res.drift is not None
    # Diagnostic-only per-role epsilon now covers the tweeter too (it repeats
    # under N=3, unlike the pre-#1668 asymmetric shape).
    assert set(res.drift.per_role_epsilon_ppm) == {"woofer", "tweeter"}

    by_role = {resp.role: resp for resp in res.driver_responses}
    assert set(by_role) == {"woofer", "tweeter"}
    for role, resp in by_role.items():
        assert resp.repeat_index is None  # primaries are never themselves a repeat
        assert len(resp.repeat_responses) == 2  # N=3 ⇒ 2 repeats past the primary
        assert [r.repeat_index for r in resp.repeat_responses] == [1, 2]
        mask = (resp.freqs_hz > 500.0) & (resp.freqs_hz < 2000.0)
        assert mask.any()
        for repeat in resp.repeat_responses:
            assert repeat.role == role
            assert repeat.repeat_responses == ()  # a repeat never carries its own repeats
            diff_db = np.abs(resp.magnitude_db[mask] - repeat.magnitude_db[mask])
            assert float(diff_db.max()) < 0.5  # bit-identical stimuli, noise-level agreement


def test_era_tolerance_old_shaped_program_analyzes_without_crash_or_version_flag():
    """An old-shaped (pre-#1668) MEASURE program — woofer repeated once,
    tweeter never — must still analyze cleanly through the NEW analysis: no
    crash, no version flag, woofer gets exactly one repeat response, the
    tweeter gets none."""
    prog = _build_old_shaped_measure_program()
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert not res.glitch_detected
    assert res.drift is not None
    # Woofer-pair-only: the tweeter has just one located occurrence, so it
    # contributes no per-role epsilon diagnostic (never a crash either way).
    assert set(res.drift.per_role_epsilon_ppm) == {"woofer"}

    by_role = {resp.role: resp for resp in res.driver_responses}
    assert len(by_role["woofer"].repeat_responses) == 1
    assert by_role["woofer"].repeat_responses[0].repeat_index == 1
    assert by_role["tweeter"].repeat_responses == ()


@pytest.mark.parametrize("n", [0, 1, 2, 5])
def test_sweep_occurrence_index_round_trips_through_occurrence_suffix(n):
    """Analysis-side ``_sweep_occurrence_index`` must invert composition-side
    ``_occurrence_suffix`` exactly — the contract ``_sweep_occurrences_by_role``
    relies on to group located sweeps by occurrence order rather than physical
    schedule position under the N=3 interleaved MEASURE layout (design §5.4,
    sweep-composition PR-A #1668)."""
    assert _sweep_occurrence_index(f"sweep_w{_occurrence_suffix(n)}") == n


# --------------------------------------------------------------------------- #
# glitch injection
# --------------------------------------------------------------------------- #


def test_dropped_buffer_glitch_is_detected():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    # Delete 128 samples mid-capture (between the two woofer sweeps): the
    # woofer-repeat separation shrinks ⇒ out-of-band ε ⇒ glitch.
    mid = cap.size // 2
    glitched = np.concatenate([cap[:mid], cap[mid + 128:]])
    res = analyze_program_capture(prog, glitched, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.glitch_detected
    assert res.drift.glitch_detected


def test_clean_capture_is_not_flagged_as_glitch():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(260, 300.0, 20000.0, 0.7),
        epsilon=40e-6,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert not res.glitch_detected
    # No bound tripped, and genuine drift must NOT be mistaken for a step
    # (#1765): the estimator runs on every capture, so a clean one is the
    # false-positive guard for `_locate_discontinuity`.
    assert res.drift.glitch_inputs == ()
    assert res.drift.discontinuity_samples == 0.0
    assert res.drift.discontinuity_after_segment == ""


def test_midcapture_splice_is_attributed_to_a_discontinuity_not_drift():
    """The 2026-07-27 JTS3 hardware glitch (issue #1765), in fixture form.

    A single +64-sample insertion between the first woofer sweep and the first
    tweeter sweep. The capture must be rejected — but as a discrete timeline
    STEP, not as clock drift: the reported epsilon is an artefact of the step
    (the woofer pair straddles it, the tweeter pair does not) and stays well
    inside ``MAX_DRIFT_PPM``, exactly as the real capture's 94.12 ppm did.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    # Splice in the quiet gap between sweep_w and sweep_t — the window the
    # hardware forensics localised the real step to.
    sweep_w = prog.segment("sweep_w")
    cut = GLOBAL_OFFSET + sweep_w.start_sample + sweep_w.n_samples + 4_000
    spliced = np.concatenate([cap[:cut], np.zeros(64), cap[cut:]])

    res = analyze_program_capture(
        prog, spliced, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.glitch_detected
    # The residual guard is what actually catches this class — the ppm bound
    # never fired on either real hardware glitch.
    assert "residual_desync" in res.drift.glitch_inputs
    assert "epsilon_out_of_bound" not in res.drift.glitch_inputs
    assert abs(res.drift.epsilon_ppm) < program_analysis.MAX_DRIFT_PPM
    # …and the step itself is named, with its size and where it landed.
    assert res.drift.discontinuity_samples == pytest.approx(64.0, abs=2.0)
    assert res.drift.discontinuity_after_segment == "sweep_w"


# --- D7: the desync guard vs. its own estimator's resolution -------------------
#
# Series-2 diagnosis (2026-08-17). The guard's 1.5-sample threshold used to be
# compared against a residual built from `_locate_in_window`'s INTEGER
# `int(np.argmax(...))` of the dry stimulus against the room-convolved capture,
# whose argmax hops between adjacent early lobes by a few samples — so 1.5 sat
# below the instrument's own noise. The two tests below pin both directions of
# the fix: a clean capture whose locate hopped must be ACCEPTED, and a capture
# carrying a real timeline step must still be REJECTED, with the gate's
# sensitivity numerically unchanged.


def _pre_d7_max_residual(prog, locations, epsilon):
    """The residual statistic exactly as it was computed before D7.

    Straight from `located_start` — the integer locate — rather than from
    `_subsample_separation`. The global offset cancels in the per-role
    demeaning, so it is not a parameter here.
    """
    groups: dict[str, list[float]] = {}
    for loc in locations:
        if loc.kind != program_analysis.KIND_SWEEP:
            continue
        start = prog.segment(loc.segment_id).start_sample
        groups.setdefault(loc.role, []).append(
            loc.located_start - start * (1.0 + epsilon)
        )
    out = 0.0
    for resids in groups.values():
        mean = sum(resids) / len(resids)
        out = max(out, max(abs(r - mean) for r in resids))
    return out


def test_integer_locate_lobe_hop_on_a_clean_capture_is_not_a_desync():
    """D7 — the guard must not fire below the resolution of its own estimator.

    Eight physically-clean series-2 captures were rejected `residual_desync` at
    a reported 2.00-3.13 samples — the band this test asserts into; the rest of
    that forensic sits on `GLITCH_RESIDUAL_SAMPLES`, which owns it. This
    reproduces the signature at the seam it came from: one clean capture, its
    own locations, and a few samples of
    integer-locate error injected on ONE occurrence. The AUDIO is untouched, so
    the honest verdict is "no desync" — and the fixed guard, which measures the
    occurrences against each other rather than against their located starts,
    returns essentially the answer it gave before the injection: the reported
    max moves by 1.166e-6 samples, against a threshold of 1.5. The tolerance is
    1e-5 because the movement is fixture- and hop-size-dependent — sweeping the
    injection over ±1..9 samples on either tweeter occurrence peaks at 1.65e-6,
    so 1e-5 keeps ~6x headroom over that, with room left for FFT-backend
    variation across CI's three Python versions and a different OS.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(260, 300.0, 20000.0, 0.7),
        epsilon=40e-6,
    )
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert not res.glitch_detected

    # Inject on a TWEETER occurrence deliberately: on this fixture the tweeter
    # owns the reported max (0.0148 against the woofer's 0.0013), so perturbing
    # a woofer occurrence would leave `max_residual_samples` untouched to the
    # bit and the comparison below could not fail whatever the guard did.
    hopped_locations = [
        dataclasses.replace(loc, located_start=loc.located_start + 4)
        if loc.segment_id == "sweep_t_rep"
        else loc
        for loc in res.locations
    ]
    clean = program_analysis._estimate_drift(prog, cap, SR, res.locations)
    hopped = program_analysis._estimate_drift(prog, cap, SR, hopped_locations)

    # The defect is reproduced: read off the integer locate, those same
    # locations land squarely in the banked 2.00-3.13 band and trip the gate.
    pre_d7 = _pre_d7_max_residual(prog, hopped_locations, hopped.epsilon_ppm / 1e6)
    assert pre_d7 == pytest.approx(3.0, abs=0.05)
    assert pre_d7 > program_analysis.GLITCH_RESIDUAL_SAMPLES

    # The shipped guard is unmoved — a locate that hopped is not a capture that
    # desynced. Four samples of injected locate error move the answer by six
    # orders of magnitude less than the threshold it is compared against.
    assert hopped.max_residual_samples == pytest.approx(
        clean.max_residual_samples, abs=1e-5
    )
    assert hopped.max_residual_samples < 0.5
    assert "residual_desync" not in hopped.glitch_inputs
    assert not hopped.glitch_detected


@pytest.mark.parametrize(
    "after_segment, insert_samples, expect_rejected, expect_spread",
    [
        # #2533's shape, at its own program position: a deterministic
        # ~128-sample playback insertion in the gap after the SECOND tweeter
        # sweep, which broke every lateral capture of the 2026-08-15 campaign.
        ("sweep_t_rep", 128, True, 42.667),
        # The gate's small end, pinned so a future edit cannot quietly widen
        # the hole D7 was accused of widening.
        ("sweep_w", 4, True, 2.0),
        ("sweep_w", 2, False, 1.0),
    ],
)
def test_desync_guard_keeps_its_teeth_after_d7(
    after_segment, insert_samples, expect_rejected, expect_spread
):
    """D7's other direction: a sharper instrument must not be a blunter gate.

    A real timeline step is a real separation, so both estimators measure it —
    pinned here by asserting the pre-D7 statistic and the shipped one agree on
    every one of these captures. What D7 changed is the noise floor underneath
    them, never the sensitivity; the measured before/after for that floor is
    recorded on `GLITCH_RESIDUAL_SAMPLES` rather than restated here.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    seg = prog.segment(after_segment)
    cut = GLOBAL_OFFSET + seg.start_sample + seg.n_samples + 4_000
    spliced = np.concatenate([cap[:cut], np.zeros(insert_samples), cap[cut:]])

    res = analyze_program_capture(
        prog, spliced, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.drift.max_residual_samples == pytest.approx(expect_spread, abs=0.05)
    assert ("residual_desync" in res.drift.glitch_inputs) is expect_rejected
    # …and the pre-D7 statistic agrees, on a real step, to within a hundredth of
    # a sample. The two estimators only part company on the noise.
    assert _pre_d7_max_residual(
        prog, res.locations, res.drift.epsilon_ppm / 1e6
    ) == pytest.approx(res.drift.max_residual_samples, abs=0.01)


@pytest.mark.parametrize(
    "insert_samples, expect_slip, expect_residual_desync",
    [
        # The BOUNDARY this guard moved, pinned end to end on the real
        # analysis path. Before the sub-sample slip gate, only the spread
        # guard rejected, so +2 and +3 sailed through: at 48 kHz that is 41
        # and 62 us of woofer-vs-tweeter error, against a 20 us relative-phase
        # budget. The Stage-0 bank recorded a real silent USB slip of +1.986
        # samples, so this was not a theoretical hole.
        (0, False, False),
        # Honest limit, pinned in the same table as the wins: one sample is
        # NOT caught, and SLIP_GATE_SAMPLES says why reaching it would cost
        # more than it buys.
        (1, False, False),
        (2, True, False),   # newly caught — was silently accepted
        (3, True, False),   # newly caught — was silently accepted
        # The spread guard's own pins, unchanged: it still fires at +4 and +7,
        # and this PR retuned nothing about it.
        (4, True, True),
        (7, True, True),
    ],
)
def test_timeline_slip_gate_moves_the_floor_from_four_samples_to_two(
    insert_samples, expect_slip, expect_residual_desync
):
    """The whole point of the sub-sample slip gate, as a before/after table.

    Read the two columns together: ``timeline_slip`` is the new instrument and
    ``residual_desync`` is the shipped one. The shipped one's behaviour is
    IDENTICAL to what it was — that is the claim "nothing about the spread
    guard was retuned", asserted rather than promised.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    seg = prog.segment("sweep_w")
    cut = GLOBAL_OFFSET + seg.start_sample + seg.n_samples + 4_000
    spliced = (
        np.concatenate([cap[:cut], np.zeros(insert_samples), cap[cut:]])
        if insert_samples
        else cap
    )

    res = analyze_program_capture(
        prog, spliced, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    inputs = res.drift.glitch_inputs
    assert ("timeline_slip" in inputs) is expect_slip, (
        f"+{insert_samples} samples: glitch_inputs={inputs}, "
        f"step={res.drift.discontinuity_samples}"
    )
    assert ("residual_desync" in inputs) is expect_residual_desync
    if expect_slip:
        # The slip's own record, so a forensic reader sees the size that was
        # rejected rather than only that something was.
        assert res.drift.discontinuity_samples == pytest.approx(
            insert_samples, abs=0.5
        )
        assert res.drift.discontinuity_after_segment == "sweep_w"


def test_diagnostic_summary_says_WHY_the_gate_window_is_what_it_is():
    """#1966 — the sidecar must distinguish "reflection found" from "capped".

    ``gate_impulse_response`` uses ``SEARCH_T_MAX_MS`` as BOTH the search
    ceiling and the fallback window when nothing is found, so a capped window
    and a genuinely reflection-terminated one can print the identical
    ``gate_window_ms``. Across the entire 2026-07-30 corpus every capture was
    the capped case — 7.0 ms window, 142.857 Hz validity floor, on all 8 cloud
    positions and all 17 VERIFY sidecars — while the label "reflection-gated"
    read downstream as "reflections removed". The gate computed
    ``floor_source`` all along; the sidecar dropped it.

    This fixture is a clean, reflection-free capture, i.e. the corpus's own
    state: the detector finds nothing and the window is capped at the ceiling.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    summary = analysis_diagnostic_summary(res)

    assert summary["woofer_gate_floor_source"] == gating.FLOOR_SEARCH_BOUND
    assert summary["tweeter_gate_floor_source"] == gating.FLOOR_SEARCH_BOUND
    # The corpus signature: window sitting exactly at the search ceiling, with
    # a validity floor of 1/window — which is what makes the bare number
    # unreadable without the field above.
    assert summary["woofer_gate_window_ms"] == pytest.approx(
        gating.SEARCH_T_MAX_MS, abs=0.05
    )
    assert summary["woofer_validity_floor_hz"] == pytest.approx(
        1000.0 / gating.SEARCH_T_MAX_MS, rel=0.02
    )


def test_diagnostic_summary_SAYS_the_ceiling_cap_in_words_not_only_an_enum():
    """#1966's other half: persisting ``floor_source`` fixed the record for a
    machine; a person reading the dump still had to know the vocabulary.

    The sidecar therefore carries the sentence too, and on the corpus's own
    state — a clean, reflection-free capture — that sentence must say
    "nothing was gated out" rather than anything a reader could take as
    "reflections removed".
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    summary = analysis_diagnostic_summary(res)

    for role in ("woofer", "tweeter"):
        text = summary[f"{role}_gate_disclosure"]
        assert "no reflection found" in text
        assert "nothing was gated out" in text
        assert "reflection measured" not in text
    # Rendered by the ONE writer, never re-phrased here: the sentence must be
    # byte-identical to what gate_disclosure produces for the same block.
    assert summary["woofer_gate_disclosure"] == gate_disclosure.describe_gate(
        res.driver_responses[0].gating
    )


def test_driver_response_prices_the_gate_over_the_band_it_actually_drove():
    """E5's eval-band correction, at the seam that supplies the band.

    ``_driver_response`` threads the segment's own sweep bounds into the
    delta. Evaluating a high-passed branch from its trusted floor instead —
    where it has no output — is what over-reported 4.045 dB against an
    honest 1.368 dB on the banked corpus.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    for resp, segment_id in zip(res.driver_responses, ("sweep_w", "sweep_t")):
        delta = resp.gating["pre_post_gate_delta"]
        assert delta is not None, "the delta must be reported, not skipped"
        segment = prog.segment(segment_id)
        # The evaluation band is the intersection, never the full range.
        assert delta["radiated_band_hz"] == [segment.f1_hz, segment.f2_hz]
        assert delta["eval_band_hz"][0] == pytest.approx(
            max(resp.gating["f_trusted_hz"], segment.f1_hz)
        )
        assert delta["eval_band_hz"][1] == pytest.approx(segment.f2_hz)
        # Both readings are recorded so the correction stays auditable.
        assert delta["literal_band_hz"][1] == 20000.0


def test_diagnostic_summary_reports_a_measured_reflection_as_such():
    """#1966, the discriminating half: a capture WITH a reflection must not
    report the same provenance as one without. Same field, different value —
    otherwise the disclosure discloses nothing."""
    fc_hz = 2000.0
    woofer_ir, _tweeter_ir, _n_fft = _reflection_fixture(fc_hz)
    _gated, fragment = gating.gate_impulse_response(woofer_ir, SR)
    assert fragment["floor_source"] == gating.FLOOR_MEASURED
    assert fragment["first_reflection_ms"] is not None
    # And the window is SHORTER than the ceiling, because a real onset ended
    # it — the pair (window, floor_source) is what makes a record readable.
    assert fragment["window_ms"] < gating.SEARCH_T_MAX_MS


def test_discontinuity_reaches_the_durable_diagnostic_summary():
    """`analysis_diagnostic_summary` is the per-capture record the conductor
    persists, so the new attribution has to survive the trip (#1765)."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    sweep_w = prog.segment("sweep_w")
    cut = GLOBAL_OFFSET + sweep_w.start_sample + sweep_w.n_samples + 4_000
    spliced = np.concatenate([cap[:cut], np.zeros(64), cap[cut:]])

    res = analyze_program_capture(
        prog, spliced, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    summary = analysis_diagnostic_summary(res)
    assert summary["glitch_detected"] is True
    assert "residual_desync" in summary["glitch_inputs"]
    assert summary["discontinuity_samples"] == pytest.approx(64.0, abs=2.0)
    assert summary["discontinuity_after_segment"] == "sweep_w"


@pytest.mark.parametrize(
    "confidence,expect_unresolved",
    [
        pytest.param(0.0298, True, id="real-incident-0.0298"),
        # D-3: the boundary itself, not just "somewhere below 0.3" — pins
        # the gate's exact comparison operator.
        pytest.param(0.3, False, id="at-floor-0.3-is-trusted"),
        pytest.param(0.2999, True, id="just-below-floor-0.2999-is-not"),
    ],
)
def test_unlocatable_sweeps_report_unresolved_not_a_fabricated_discontinuity(
    confidence, expect_unresolved,
):
    """#1839: `_locate_discontinuity` must not turn an untrustworthy sweep
    location into a confident-looking number. Real incident (session
    cap_-Us10xORVNlFa_dgi-sP7g): sweeps located at confidence 0.0298 against
    production's 0.3 floor (``SWEEP_LOCATE_CONFIDENCE_FLOOR``), and
    the pre-fix code still reported a fabricated
    ``discontinuity_samples = -2090.5``.

    Reuses the EXACT located sweeps the pinned splice fixture above
    (``test_midcapture_splice_is_attributed_to_a_discontinuity_not_drift``)
    resolves to a confident +64.00-sample step — only ``confidence`` is
    overridden, isolating the new precondition from the fit math it guards.
    Trusted, this input resolves +64.00 (that pinned test); untrusted, it
    must report the sentinel instead of ANY number, right or wrong.

    The 0.3 / 0.2999 cases pin that the gate is a strict ``confidence <
    FLOOR`` — the floor value ITSELF is trusted, not merely "somewhere
    below 0.3" — matching `crossover_v2_flow._sweep_locate_confidence_ok`'s
    exact negation (``confidence >= FLOOR``) at the same threshold. See
    `test_locate_confidence_floors_agree` in
    tests/test_measurement_integrity_floor_contracts.py for the
    cross-module value pin these two gates share.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    sweep_w = prog.segment("sweep_w")
    cut = GLOBAL_OFFSET + sweep_w.start_sample + sweep_w.n_samples + 4_000
    spliced = np.concatenate([cap[:cut], np.zeros(64), cap[cut:]])

    res = analyze_program_capture(
        prog, spliced, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    # Sanity: this fixture really does resolve a confident step when the
    # sweeps are trusted — the pinned +64.00 case proven above.
    assert res.drift.discontinuity_samples == pytest.approx(64.0, abs=2.0)

    sweep_locs = [
        dataclasses.replace(loc, confidence=confidence)
        for loc in res.locations
        if loc.kind == KIND_SWEEP
    ]
    # The fit now measures positions out of the capture itself (sub-sample,
    # so it can gate), which is why the capture is an argument and the fit
    # rides back as a third return value.
    step, after, _fit = program_analysis._locate_discontinuity(
        prog, spliced, sweep_locs
    )
    if expect_unresolved:
        assert step == program_analysis.DISCONTINUITY_UNRESOLVED
        assert after == ""
    else:
        assert step == pytest.approx(64.0, abs=2.0)
        assert after == "sweep_w"


def test_discontinuity_unresolved_survives_the_durable_diagnostic_summary():
    """#1839 consumer audit: `analysis_diagnostic_summary` does ``float(...)``
    on ``discontinuity_samples`` (see the pinned-value test above, which
    exercises the numeric path) — it must degrade, not raise, when that field
    holds ``DISCONTINUITY_UNRESOLVED`` instead of a number. This is the
    "must never raise" operator-retention-sidecar path
    (``analysis_diagnostic_summary``'s own docstring)."""
    drift = program_analysis.DriftEstimate(
        epsilon_ppm=0.0,
        max_residual_samples=0.0,
        glitch_detected=False,
        discontinuity_samples=program_analysis.DISCONTINUITY_UNRESOLVED,
        discontinuity_after_segment="",
    )
    analysis = program_analysis.ProgramAnalysis(
        phase=PROGRAM_PHASE_MEASURE, program_id="test", locations=(), drift=drift,
    )
    summary = analysis_diagnostic_summary(analysis)
    assert summary["discontinuity_samples"] == program_analysis.DISCONTINUITY_UNRESOLVED


def test_glitch_log_event_survives_an_unresolved_discontinuity(caplog):
    """#1839 consumer audit, second seam: the ``program_analysis.glitch``
    log_event rounds ``discontinuity_samples`` for display — it must not
    raise when that value is the ``DISCONTINUITY_UNRESOLVED`` sentinel. A
    genuinely noise-buried capture (not a hand-built fixture) drives
    sweep-locate confidence low enough to trip BOTH the glitch verdict and
    the new gate at once, matching the real incident's shape (mis-located
    sweeps produced a residual that tripped ``glitch_detected``, #1838's D3).
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
        noise=5.0,  # deliberately buries the sweeps far under the noise floor
    )
    with caplog.at_level(logging.WARNING, logger="jasper.audio_measurement.program_analysis"):
        res = analyze_program_capture(
            prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        )
    assert res.glitch_detected
    assert res.drift.discontinuity_samples == program_analysis.DISCONTINUITY_UNRESOLVED
    glitch_lines = [
        r.getMessage() for r in caplog.records
        if "event=program_analysis.glitch" in r.getMessage()
    ]
    assert len(glitch_lines) == 1
    assert "discontinuity_samples=unresolved" in glitch_lines[0]


# --------------------------------------------------------------------------- #
# integrity: clip runs + locator robustness
# --------------------------------------------------------------------------- #


def test_clip_run_detection():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    # Hard-clip a run inside the woofer sweep window.
    sweep_w = prog.segment("sweep_w")
    start = GLOBAL_OFFSET + sweep_w.start_sample + 500
    cap[start:start + 8] = 1.0
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    clipped = {loc.segment_id: loc.clipped for loc in res.locations}
    assert clipped["sweep_w"] is True
    assert clipped["sweep_t"] is False


def test_locator_robust_to_large_global_offset():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    tau_true = 30
    for offset in (0, int(0.5 * SR)):
        cap = _synthesize(
            prog,
            woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
            tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.8),
            global_offset=offset,
            epsilon=0.0,
        )
        res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
        assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)


# --------------------------------------------------------------------------- #
# sign convention (worked example)
# --------------------------------------------------------------------------- #


def testgcc_phat_sign_convention():
    # A ≈ B shifted right by +lag: build st = sw delayed by 30 samples.
    base = _band_impulse(400, 800.0, 3200.0, 1.0, n=2048)
    sw = base
    st = np.roll(base, 30)  # tweeter LATER by 30 samples
    lag, sign, conf, at_edge = gcc_phat(
        st, sw, sample_rate=SR, band_hz=(800.0, 3200.0),
        upsample=16, max_lag_samples=200,
    )
    assert lag == pytest.approx(30.0, abs=0.5)  # positive ⇒ st (tweeter) later
    assert sign == 1
    assert not at_edge
    # Inverting the tweeter flips the correlation sign.
    lag2, sign2, _conf2, _edge2 = gcc_phat(
        -st, sw, sample_rate=SR, band_hz=(800.0, 3200.0),
        upsample=16, max_lag_samples=200,
    )
    assert sign2 == -1
    assert lag2 == pytest.approx(30.0, abs=0.5)


def test_gcc_confidence_is_never_nan_even_for_a_silent_capture():
    """Two NaN blocks, pinned — a downstream gate's equivalence rests on them.

    ``gcc_phat``'s confidence flows into ``ProgramAnalysis.alignment.confidence``,
    which MEASURE screens with ``alignment_confidence_ok`` (``crossover_v2_flow``:
    ``confidence >= ALIGNMENT_CONFIDENCE_TRUST_FLOOR``). That phrasing and its
    negation are only interchangeable while the value is a real number: ``NaN``
    compares False both ways, so a NaN confidence would make the rung's sense
    depend on which way it was written. Nothing downstream re-checks for NaN, so
    the property has to hold HERE, and it does for two reasons this pins:

    * :func:`_gcc_correlation` clamps the phase-transform magnitude
      (``mag[mag < 1e-12] = 1e-12``), so a silent band cannot divide by zero and
      seed the correlation with NaN;
    * the confidence itself is ``max(0.0, (primary - secondary) / primary) if
      primary > 0 else 0.0`` — the guard blocks 0/0 and the ``max`` blocks a
      negative.

    Silence is the input that reaches both: every bin is zero, so the clamp is
    what the division sees and ``primary`` is what the guard rejects.
    """
    silence = np.zeros(2048, dtype=np.float64)

    cc, m = _gcc_correlation(
        silence, silence, sample_rate=SR, band_hz=(800.0, 3200.0), upsample=16,
    )
    assert cc.size == m
    assert np.all(np.isfinite(cc)), "the magnitude clamp is what keeps this finite"

    _lag, _sign, conf, _at_edge = gcc_phat(
        silence, silence, sample_rate=SR, band_hz=(800.0, 3200.0),
        upsample=16, max_lag_samples=200,
    )
    assert not np.isnan(conf)
    assert conf == 0.0, "no evidence at all reads as no confidence, not as NaN"

    # And on a real capture the same expression stays inside [0, 1], so the
    # comparison against the trust floor is total in both directions.
    base = _band_impulse(400, 800.0, 3200.0, 1.0, n=2048)
    _l, _s, real_conf, _e = gcc_phat(
        np.roll(base, 30), base, sample_rate=SR, band_hz=(800.0, 3200.0),
        upsample=16, max_lag_samples=200,
    )
    assert not np.isnan(real_conf)
    assert 0.0 <= real_conf <= 1.0


def test_delay_sign_convention_tweeter_earlier_is_positive():
    """positive delay_us ⇒ tweeter arrives EARLIER ⇒ delay the tweeter branch."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    # Tweeter EARLIER than the woofer (d_t < d_w).
    d_w, d_t = 260, 200
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(d_w, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(d_t, 300.0, 20000.0, 0.9),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    # D_w − D_t = 60 samples ⇒ positive delay_us.
    assert res.alignment.delay_us > 0
    assert res.alignment.delay_us == pytest.approx((d_w - d_t) / SR * 1e6, abs=5.0)


def test_snap_recovers_physical_delay_through_drift_and_noise():
    """Delay-selection physics gate (methodology §10, 2026-07-22): with the
    plausibility bound supplied, the anchor-primary + gated-local-peak-snap
    selector recovers the physical inter-driver delay from a capture carrying a
    known clock drift. The successor to the retired ``_flatness_delay_us``
    170/170 drift-removal gate: the inter-sweep clock term is removed from the
    raw argmax gap, the physical remainder is kept, and the SELECTED applied
    delay tracks truth.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 25  # tweeter arrives 25 samples LATER than the woofer
    eps = 80e-6
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.7),
        epsilon=eps,
    )
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=FC_HZ, alignment_delay_bounds_us=(0.0, 1000.0),
        ),
        geometry=MeasurementGeometry(),  # d=0 ⇒ no parallax
    )
    expected_delay_us = -tau_true / SR * 1e6
    # The SELECTED (snapped) applied delay recovers truth within the sub-sample
    # snap precision — the drift term ε·(tweeter_start − woofer_start) was
    # removed from the raw gap, and the physical remainder retained.
    assert res.alignment.status == ALIGNMENT_OK
    assert res.candidate.snap_found is True
    assert res.candidate.delay_us == res.alignment.delay_us
    assert res.alignment.delay_us == pytest.approx(expected_delay_us, abs=5.0)
    # The integer anchor is close; the GCC seed is retained separately. On a
    # single clean correlation peak, anchor, seed, and snap all coincide.
    assert res.candidate.anchor_delay_us == pytest.approx(expected_delay_us, abs=11.0)
    assert res.alignment.seed_delay_us == pytest.approx(expected_delay_us, abs=5.0)


def test_gcc_local_peak_snap_stays_near_anchor_despite_taller_far_peak():
    """Wrong-peak regression (methodology §10, 2026-07-22): a TALLER correlation
    peak planted ~200 µs from the anchor must NOT steer the selector — the
    radius-gated snap stays on the near-anchor local peak. Pins the bake-off's
    'window-max is a stable wrong answer' finding: the global GCC peak lands on
    the taller far feature, the ±(period/6) snap does not.
    """
    fc_hz = 1600.0
    lo, hi = 800.0, 3200.0
    near = 8  # samples: the true near-anchor peak
    far = near + 12  # ~250 µs away at 48 kHz — well outside ±(period/6) ≈ 104 µs
    base = 400
    woofer_ir = _band_impulse(base, lo, hi, 1.0, n=4096)
    # Tweeter: a SMALLER near component + a LARGER far component (the echo).
    tweeter_ir = (
        _band_impulse(base + near, lo, hi, 0.55, n=4096)
        + _band_impulse(base + far, lo, hi, 1.0, n=4096)
    )
    radius = SR / fc_hz * GCC_SNAP_RADIUS_PERIODS

    # The GLOBAL correlation peak sits on the taller far feature...
    global_lag, _sign, _conf, _edge = gcc_phat(
        tweeter_ir, woofer_ir, sample_rate=SR, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, max_lag_samples=200,
    )
    assert global_lag == pytest.approx(far, abs=1.5)

    # ...but the anchor-gated snap stays on the near peak. (The band-limited
    # main lobe blends the near component toward the taller far one, so the
    # near local max sits a little past `near` — the point is that it stays
    # inside the radius and never rails the full ~250 µs to the far lobe.)
    snapped = _gcc_local_peak_snap(
        tweeter_ir, woofer_ir, sample_rate=SR, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, anchor_lag_samples=float(near), radius_samples=radius,
    )
    assert snapped is not None
    assert abs(snapped - near) <= radius  # never rails to the far lobe
    assert abs(snapped - far) > radius  # decisively NOT the taller far peak
    assert snapped < (near + far) / 2


def test_gcc_local_peak_snap_heals_anchor_jitter():
    """Anchor-jitter healing (methodology §10, 2026-07-22): the integer argmax
    anchor is ±1–2 samples unstable on back-to-back captures; snapping both to
    the SAME nearby correlation peak collapses that jitter. Pins the bake-off's
    44.7→6.9 µs healing at the selector level.
    """
    fc_hz = 1600.0
    lo, hi = 800.0, 3200.0
    true_lag = 9
    base = 400
    woofer_ir = _band_impulse(base, lo, hi, 1.0, n=4096)
    tweeter_ir = _band_impulse(base + true_lag, lo, hi, 0.8, n=4096)
    radius = SR / fc_hz * GCC_SNAP_RADIUS_PERIODS

    # Two anchors differing by 2 samples (integer-argmax jitter) → same peak.
    snap_lo = _gcc_local_peak_snap(
        tweeter_ir, woofer_ir, sample_rate=SR, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, anchor_lag_samples=float(true_lag - 1),
        radius_samples=radius,
    )
    snap_hi = _gcc_local_peak_snap(
        tweeter_ir, woofer_ir, sample_rate=SR, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, anchor_lag_samples=float(true_lag + 1),
        radius_samples=radius,
    )
    assert snap_lo is not None and snap_hi is not None
    # Both anchors heal to the same sub-sample peak (≪ the 1-sample jitter).
    assert abs(snap_lo - snap_hi) * 1e6 / SR < 3.0  # µs
    assert snap_lo == pytest.approx(true_lag, abs=0.5)


def test_gcc_local_peak_snap_falls_back_when_no_local_max_in_radius():
    """Fallback: a monotonic correlation neighbourhood (no interior local
    maximum within the radius) returns ``None`` so the caller keeps the bare
    anchor — never railing, never searching wider.
    """
    lo, hi = 800.0, 3200.0
    base = 400
    woofer_ir = _band_impulse(base, lo, hi, 1.0, n=4096)
    tweeter_ir = _band_impulse(base + 6, lo, hi, 0.8, n=4096)  # peak at lag 6
    # Anchor on the main lobe's rising flank (apex at 6 just outside a ±2-sample
    # window): the neighbourhood is monotonic, so no interior local max exists.
    snapped = _gcc_local_peak_snap(
        tweeter_ir, woofer_ir, sample_rate=SR, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, anchor_lag_samples=3.0, radius_samples=2.0,
    )
    assert snapped is None


def test_build_candidate_falls_back_to_anchor_when_snap_absent():
    """When the aligner found no local peak in the snap radius
    (``snapped_delay_us is None``), the SEED is the bare anchor and
    ``snap_found`` records that — the physical plausibility rail then still
    gates the committed delay downstream (Fix 3).

    ``snap_found`` and ``snap_delta_us`` are asserted as the INDEPENDENT facts
    they are (#2607 review nit): the first is the seed's provenance, the second
    is ``committed − anchor``, and since #2598 an absent snap no longer implies
    a zero delta — the objective is free to commit away from the anchor. They
    coincide here because this fixture's flattest pair IS the anchor, which the
    test says out loud rather than treating one as evidence of the other.
    """
    woofer_ir = np.zeros(8192)
    tweeter_ir = np.zeros(8192)
    woofer_ir[1000] = 1.0
    tweeter_ir[1011] = 1.0
    physical_gap_us = 3 / SR * 1e6
    # The aligner owns the anchor; supply it as the aligner would (argmax gap 11
    # − drift 8 = 3 samples, no parallax) with no local snap peak found.
    alignment = AlignmentEstimate(
        delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=-physical_gap_us, snapped_delay_us=None,
    )
    candidate, _predicted = _build_candidate(
        woofer_ir, tweeter_ir, SR, 16_384, 2000.0, "woofer", "tweeter",
        alignment, None, alignment_delay_bounds_us=(0.0, 1000.0),
    )
    # The seed's provenance: the aligner offered no local snap peak.
    assert candidate.snap_found is False
    assert candidate.anchor_delay_us == pytest.approx(-physical_gap_us, abs=1e-9)
    # The commitment, and its residual — a separate fact, equal to zero here
    # only because the objective agreed with the anchor.
    assert candidate.delay_us == pytest.approx(-physical_gap_us, abs=1e-9)
    assert candidate.snap_delta_us == pytest.approx(
        candidate.delay_us - candidate.anchor_delay_us, abs=1e-9,
    )
    assert candidate.snap_delta_us == pytest.approx(0.0, abs=1e-9)


def test_build_candidate_anchor_overrides_wrong_periodic_gcc_lobe():
    """A confident GCC peak on a neighboring comb lobe must not steer selection.

    The aligner-owned anchor is 3 samples of physical branch delay (the measured
    argmax gap minus its inter-sweep drift). The anchor is primary (methodology
    §10, 2026-07-22); even when the GCC seed sits on a wrong comb lobe (-650 us)
    and no snap was found, the bare anchor is selected — never the wrong GCC lobe.
    """
    woofer_ir = np.zeros(8192)
    tweeter_ir = np.zeros(8192)
    # Keep both peaks beyond IR_PRE_MS so the direct-arrival windows share the
    # same local origin instead of clipping against index zero.
    woofer_ir[1000] = 1.0
    tweeter_ir[1011] = 1.0
    physical_gap_us = 3 / SR * 1e6
    gcc_seed_us = -650.0
    alignment = AlignmentEstimate(
        delay_us=gcc_seed_us,
        raw_delay_us=gcc_seed_us,
        parallax_us=0.0,
        polarity="normal",
        polarity_sign=1,
        polarity_agrees_with_sum=True,
        confidence=0.9,
        status=ALIGNMENT_OK,
        anchor_delay_us=-physical_gap_us,
        snapped_delay_us=None,
    )

    candidate, _predicted = _build_candidate(
        woofer_ir,
        tweeter_ir,
        SR,
        16_384,
        2000.0,
        "woofer",
        "tweeter",
        alignment,
        None,
        alignment_delay_bounds_us=(0.0, 1000.0),
    )

    assert candidate.delay_us == pytest.approx(-physical_gap_us, abs=1e-9)
    assert candidate.anchor_delay_us == pytest.approx(-physical_gap_us, abs=1e-9)
    assert candidate.snap_found is False  # aligner reported no local snap peak
    # Independent of the line above (#2607 nit): the delta is
    # ``committed − anchor``, which an absent snap does not by itself pin.
    assert candidate.snap_delta_us == pytest.approx(
        candidate.delay_us - candidate.anchor_delay_us, abs=1e-9,
    )
    assert candidate.snap_delta_us == pytest.approx(0.0, abs=1e-9)
    assert candidate.alignment_seed_ripple_db is not None
    # The bare anchor IS the seed here, and it is already the flattest pair, so
    # the selector commits it unchanged: zero improvement.
    assert candidate.flatness_improvement_db == pytest.approx(0.0, abs=1e-9)
    assert candidate.predicted_ripple_db < 0.1


# --------------------------------------------------------------------------- #
# the joint (polarity, delay) selector — issue #2598
# --------------------------------------------------------------------------- #


def _lr4_branches(fc_hz=2000.0, n_bins=4097, f_max_hz=24_000.0):
    """A complementary 4th-order Linkwitz-Riley pair — two cascaded 2nd-order
    Butterworths per branch — which sums FLAT in matched polarity and nulls at
    the corner in reverse. The shape every 2-way crossover is trying to be, and
    the shape the selector has to be able to tell apart.
    """
    freqs = np.linspace(0.0, f_max_hz, n_bins)
    s = 1j * freqs / fc_hz
    butter2 = s * s + np.sqrt(2.0) * s + 1.0
    W = (1.0 / butter2) ** 2
    T = ((s * s) / butter2) ** 2
    return freqs, W, T


def _flat_branches(n_bins=4097, f_max_hz=24_000.0):
    """Two flat, full-range, identical branches. Not a crossover — the
    degenerate pair whose reverse-polarity sum is UNIFORM silence.
    """
    freqs = np.linspace(0.0, f_max_hz, n_bins)
    return freqs, np.ones(n_bins, dtype=np.complex128), np.ones(n_bins, dtype=np.complex128)


def test_select_alignment_pair_keeps_the_seed_inside_the_flat_basin():
    """The no-op control. When flatness cannot separate the candidates, the
    committed pair is the SEED — correlation's own answer, byte-identical —
    which is what keeps this change from perturbing captures it has nothing to
    say about.

    A matched-polarity LR4 pair at its own corner is already flat, so every
    nearby pair of the same sign sums within the flat-minimum epsilon, the
    regularization decides, and it decides for the seed.
    """
    freqs, W, T = _lr4_branches()
    seed_delay_us = 40.0
    selection = _select_alignment_pair(
        freqs, W, T, fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=40.0, seed_delay_us=seed_delay_us, seed_polarity_sign=1,
    )
    assert selection is not None
    assert selection.polarity_sign == 1
    assert selection.delay_us == pytest.approx(seed_delay_us, abs=1e-12)
    assert selection.flatness_improvement_db == pytest.approx(0.0, abs=1e-12)
    assert selection.polarity_agrees_with_sum is True
    assert selection.objective == ALIGNMENT_COMMITTED_FLAT_SUM


def test_select_alignment_pair_overrides_a_correlation_sign_that_nulls():
    """The whole point, in the smallest honest fixture: a complementary LR4
    pair whose seed sign is inverted commands a null at its own corner, and the
    objective flips it and says correlation disagreed.
    """
    freqs, W, T = _lr4_branches()
    selection = _select_alignment_pair(
        freqs, W, T, fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=0.0, seed_delay_us=0.0, seed_polarity_sign=-1,
    )
    assert selection is not None
    assert selection.polarity_sign == 1
    assert selection.polarity_agrees_with_sum is False
    assert selection.flatness_improvement_db > 10.0
    assert selection.objective == ALIGNMENT_COMMITTED_FLAT_SUM


def test_select_alignment_pair_is_indifferent_to_a_uniform_cancellation():
    """A known and deliberate bound on the objective, pinned so nobody has to
    rediscover it: RIPPLE IS A SHAPE METRIC. Two identical full-range branches
    in reverse polarity cancel UNIFORMLY, and a uniformly-silent sum has zero
    ripple — the flattest curve there is.

    That degenerate pair is not a crossover (a real one hands complementary
    filters to the two branches, so its reverse-polarity sum is a notch and not
    a floor), and the fallback is the safe one: with both signs scoring equally
    the flat-minimum regularization keeps the SEED, so on this input the
    selector commits exactly what correlation alone would have. It cannot make
    such a capture worse than the path it replaced.
    """
    freqs, W, T = _flat_branches()
    for seed_sign in (1, -1):
        selection = _select_alignment_pair(
            freqs, W, T, fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
            trim_w_db=0.0, trim_t_db=0.0,
            anchor_delay_us=0.0, seed_delay_us=0.0, seed_polarity_sign=seed_sign,
        )
        assert selection is not None
        assert selection.polarity_sign == seed_sign
        assert selection.polarity_agrees_with_sum is True


def test_select_alignment_pair_never_proposes_a_delay_past_the_declared_bound():
    """The declared |delay| range bounds the SEARCH, not just the verdict.

    A grid point outside it would be refused by the downstream plausibility
    screen, taking the whole capture with it — so the search never offers one.
    The seed is deliberately exempt: it is what the pre-#2598 path would have
    applied anyway, and excluding it would hide the comparison.

    The fixture puts the UNBOUNDED optimum outside the bound on purpose (#2607
    review C3): a matched LR4 pair sums flattest time-aligned, so the anchor is
    the global optimum, and the anchor here is four times the declared ceiling.
    Delete the filter and the search commits 400 us — which is what makes this
    a guard and not an assertion the fixture satisfies by accident.
    """
    freqs, W, T = _lr4_branches()
    bound_us = 100.0
    unbounded_optimum_us = 400.0
    selection = _select_alignment_pair(
        freqs, W, T, fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=unbounded_optimum_us,
        # An admissible seed, so the exemption cannot be the escape hatch that
        # keeps the assertion true.
        seed_delay_us=50.0, seed_polarity_sign=1,
        delay_bounds_us=(0.0, bound_us),
    )
    assert selection is not None
    assert abs(selection.delay_us) <= bound_us
    assert abs(selection.delay_us - unbounded_optimum_us) > bound_us


def test_select_alignment_pair_bounds_its_grid_at_a_low_corner():
    """Bounded CPU. The span is one period at Fc, so a low corner would make
    the point count scale as 1/Fc; the cap widens the step instead.
    """
    freqs, W, T = _lr4_branches()
    high = _select_alignment_pair(
        freqs, W, T, fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=0.0, seed_delay_us=0.0, seed_polarity_sign=1,
    )
    low = _select_alignment_pair(
        freqs, W, T, fc_hz=80.0, lo_hz=40.0, hi_hz=160.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=0.0, seed_delay_us=0.0, seed_polarity_sign=1,
    )
    assert high is not None and low is not None
    # At 2 kHz the cap is inactive and the step is the declared one.
    assert high.grid_step_us == pytest.approx(ALIGNMENT_FLATNESS_STEP_US, rel=0.02)
    # At 80 Hz (a 12.5 ms period) it is active: the grid stays bounded and the
    # step widens to cover the same +/- one period.
    assert low.grid_points <= 2 * ALIGNMENT_FLATNESS_MAX_STEPS + 2
    assert low.grid_step_us > ALIGNMENT_FLATNESS_STEP_US
    assert low.grid_step_us * ALIGNMENT_FLATNESS_MAX_STEPS == pytest.approx(
        1e6 / 80.0, rel=1e-6,
    )


def test_selector_scores_the_shipped_frame_without_declared_bounds():
    """The objective must grade the curve the speaker EMITS, on every path.

    The frame (`anchor_delay_us`) and the delay ESTIMATOR (anchor-primary vs
    the bare GCC seed) are different questions, and the shipped model has
    always gated its residual on the aligner's STATUS alone. While the selector
    additionally required declared bounds, a preset without a
    ``delay_range_ms`` made it score at residual 0 while the emitted model
    carried ``committed − anchor`` — so the pair, polarity included, was chosen
    on a curve nothing plays (#2607 review C1; the reviewer measured a 20.37 dB
    penalty on a constructed case).

    The invariant, stated so it cannot be satisfied by coincidence: the ripple
    the selector reports for its commitment IS the ripple of the persisted
    model curve. The seed's own ripple is asserted to differ, so the equality
    is not two identical curves agreeing trivially.
    """
    fc_hz = 2000.0
    n_fft = 16_384
    freqs_full = np.fft.rfftfreq(n_fft, 1.0 / SR)
    s = 1j * freqs_full / fc_hz
    butter2 = s * s + np.sqrt(2.0) * s + 1.0
    phase = np.exp(-1j * 2.0 * np.pi * freqs_full * 0.02)
    woofer_ir = np.fft.irfft((1.0 / butter2) ** 2 * phase, n=n_fft)
    tweeter_ir = np.fft.irfft(((s * s) / butter2) ** 2 * phase, n=n_fft)

    anchor_us = 40.0
    alignment = AlignmentEstimate(
        # A GCC seed far from the anchor — the incident's own shape (-292 us
        # seed against a +62 us anchor).
        delay_us=-300.0, raw_delay_us=-300.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=anchor_us, snapped_delay_us=None,
    )
    candidate, (freqs, pred_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        alignment, None,
        alignment_delay_bounds_us=None,  # the preset declares no delay range
    )
    # The frame exists without declared bounds, and it is the aligner's anchor.
    assert candidate.anchor_delay_us == pytest.approx(anchor_us)

    lo, hi = overlap_band_hz(fc_hz)
    shipped_ripple_db = _ripple_db(
        freqs, 10.0 ** (pred_db / 20.0), lo, hi,
    )
    committed_ripple_db = (
        candidate.alignment_seed_ripple_db - candidate.flatness_improvement_db
    )
    assert committed_ripple_db == pytest.approx(shipped_ripple_db, abs=1e-9)
    # Live: the pair the old path would have shipped scores differently, so the
    # equality above is a frame check and not two copies of one curve.
    assert candidate.alignment_seed_ripple_db - shipped_ripple_db > 1.0
    # This capture's commitment DID leave the anchor's comb lobe (half a period
    # at 2 kHz is 250 us; it committed 260 us away), so the compensating
    # control the #2607 panel required in exchange for the +/-1-period span is
    # exercised here on a candidate `_build_candidate` actually produced —
    # never a hand-set field.
    assert abs(candidate.delay_us - anchor_us) > 0.5e6 / fc_hz
    assert candidate.left_anchor_lobe is True


def _alignment_selection_records(caplog):
    return [
        record for record in caplog.records
        if "event=program_analysis.alignment_selection" in record.getMessage()
    ]


def test_a_lobe_hop_raises_the_selection_log_to_warning(caplog):
    """The compensating control has to be LOUD, not merely present.

    The #2607 panel let the ±1-period span stand on two conditions: the
    lobe-leaving commitment rides the candidate, and it raises this line to
    WARNING. The second half is a log LEVEL, which no assertion covered — and a
    disclosure nobody's filter surfaces is the journald equivalent of a field
    nobody reads (#2607 delta review D1b).
    """
    fc_hz = 2000.0
    n_fft = 16_384
    freqs_full = np.fft.rfftfreq(n_fft, 1.0 / SR)
    s = 1j * freqs_full / fc_hz
    butter2 = s * s + np.sqrt(2.0) * s + 1.0
    phase = np.exp(-1j * 2.0 * np.pi * freqs_full * 0.02)
    woofer_ir = np.fft.irfft((1.0 / butter2) ** 2 * phase, n=n_fft)
    tweeter_ir = np.fft.irfft(((s * s) / butter2) ** 2 * phase, n=n_fft)
    alignment = AlignmentEstimate(
        delay_us=-300.0, raw_delay_us=-300.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=40.0, snapped_delay_us=None,
    )
    with caplog.at_level(
        logging.INFO, logger="jasper.audio_measurement.program_analysis",
    ):
        candidate, _predicted = _build_candidate(
            woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
            alignment, None, alignment_delay_bounds_us=None,
        )
    assert candidate.left_anchor_lobe is True
    records = _alignment_selection_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "left_anchor_lobe=true" in records[0].getMessage()


def test_an_ordinary_selection_stays_at_info(caplog):
    """The negative control: if every selection logged at WARNING the level
    would carry no information. A flat-sum commitment that agreed with
    correlation and stayed inside the anchor's lobe is routine, and routine is
    INFO.
    """
    woofer_ir, tweeter_ir = _impulse_branches()
    alignment = AlignmentEstimate(
        delay_us=_IMPULSE_ANCHOR_US, raw_delay_us=_IMPULSE_ANCHOR_US,
        parallax_us=0.0, polarity="normal", polarity_sign=1,
        confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=_IMPULSE_ANCHOR_US, snapped_delay_us=None,
    )
    with caplog.at_level(
        logging.INFO, logger="jasper.audio_measurement.program_analysis",
    ):
        candidate, _predicted = _build_candidate(
            woofer_ir, tweeter_ir, SR, 16_384, 2000.0, "woofer", "tweeter",
            alignment, None, alignment_delay_bounds_us=(0.0, 1000.0),
        )
    assert candidate.left_anchor_lobe is False
    assert candidate.alignment_objective == ALIGNMENT_COMMITTED_FLAT_SUM
    records = _alignment_selection_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


def test_the_selection_log_never_emits_a_bare_nan(caplog):
    """Every measured quantity on this line survives a non-finite input as
    ``null``, never as ``nan``.

    The route is real and needs no stub: the low-SNR refusal returns BEFORE the
    finite-score filter — it commits the declared design without searching — so
    a branch carrying NaN reaches the COMMITTED ripple too, not just the
    declined seed's. Doubly unreachable in production (it needs both a broken
    transfer function and an unmeasurable branch), but the earlier comment here
    asserted the committed score could not be non-finite, which was checkable
    and false (#2607 delta review D3). A bare NaN reads as a number to whatever
    parses the journal; ``null`` reads as "no number".
    """
    n_fft = 16_384
    freqs_full = np.fft.rfftfreq(n_fft, 1.0 / SR)
    s = 1j * freqs_full / 2000.0
    butter2 = s * s + np.sqrt(2.0) * s + 1.0
    phase = np.exp(-1j * 2.0 * np.pi * freqs_full * 0.02)
    woofer_ir = np.fft.irfft((1.0 / butter2) ** 2 * phase, n=n_fft)
    tweeter_ir = np.fft.irfft(((s * s) / butter2) ** 2 * phase, n=n_fft)
    tweeter_ir = tweeter_ir.copy()
    tweeter_ir[5] = np.nan

    alignment = AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=40.0, snapped_delay_us=None,
    )
    with caplog.at_level(
        logging.INFO, logger="jasper.audio_measurement.program_analysis",
    ):
        _build_candidate(
            woofer_ir, tweeter_ir, SR, n_fft, 2000.0, "woofer", "tweeter",
            alignment, None, alignment_delay_bounds_us=(0.0, 1000.0),
            branch_snr_insufficient=True,
        )
    records = _alignment_selection_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "nan" not in message.lower()
    # The committed score specifically — the one the deleted comment claimed
    # could not get here.
    assert "ripple_db=null" in message


def test_a_commitment_inside_the_anchor_lobe_is_not_flagged():
    """The negative control for the disclosure above: a candidate that stays in
    the anchor's own lobe must NOT raise the flag, or the flag says nothing.
    """
    woofer_ir, tweeter_ir = _impulse_branches()
    alignment = AlignmentEstimate(
        delay_us=_IMPULSE_ANCHOR_US, raw_delay_us=_IMPULSE_ANCHOR_US,
        parallax_us=0.0, polarity="normal", polarity_sign=1,
        confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=_IMPULSE_ANCHOR_US, snapped_delay_us=None,
    )
    candidate, _predicted = _build_candidate(
        woofer_ir, tweeter_ir, SR, 16_384, 2000.0, "woofer", "tweeter",
        alignment, None, alignment_delay_bounds_us=(0.0, 1000.0),
    )
    assert abs(candidate.delay_us - candidate.anchor_delay_us) <= 0.5e6 / 2000.0
    assert candidate.left_anchor_lobe is False


def test_select_alignment_pair_refuses_a_non_finite_score():
    """A branch transfer function carrying NaN in-band produces no verdict.

    ``_ripple_db`` is finite for any finite band, so this only happens on input
    that is already broken — but the failure mode matters: NaN propagates
    through the minimum, the flat-minimum comparison then rejects every pair,
    and the selection would die on ``min()`` of an empty sequence deep inside
    an analysis seam. The pre-#2598 code guarded exactly this before storing
    its evidence pair; the guard has to live on the SELECTION path now that
    flatness chooses (#2607 review nit). ``None`` here means the caller keeps
    the correlation seed, with a WARNING naming the cause.
    """
    freqs, W, T = _lr4_branches()
    W = W.copy()
    W[np.argmin(np.abs(freqs - 2000.0))] = np.nan
    assert _select_alignment_pair(
        freqs, W, T, fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=0.0, seed_delay_us=0.0, seed_polarity_sign=1,
    ) is None


def test_retention_sidecar_carries_the_alignment_decision_record():
    """All three sinks or none: the retained capture's own sidecar has to say
    which objective committed the pair, what correlation answered, whether the
    two agreed, and whether the commitment left the anchor's comb lobe.

    The sidecar is what a forensic reader opens months later beside the WAV; a
    decision legible only in a live journal is not a record (#2598, #2607 S2).
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, -0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        geometry=MeasurementGeometry(),
    )
    summary = analysis_diagnostic_summary(res)
    assert summary["alignment_objective"] == ALIGNMENT_COMMITTED_FLAT_SUM
    # Concrete values, not just "the summary echoes the object": these branches
    # really are inverted, correlation read them that way, and the objective
    # agreed — so the record has to say inverted / agreed / in-lobe.
    assert summary["seed_polarity"] == "inverted"
    assert summary["polarity_agrees_with_sum"] is True
    assert summary["left_anchor_lobe"] is False
    assert summary["polarity_agrees_with_sum"] is res.alignment.polarity_agrees_with_sum
    assert summary["left_anchor_lobe"] is res.candidate.left_anchor_lobe
    # The record describes THIS capture's commitment, not a default: these
    # branches really are inverted, and the objective says so.
    assert summary["polarity"] == "inverted"


def test_select_alignment_pair_returns_none_without_a_scoring_band():
    """No bin inside the band ⇒ no objective ⇒ no answer invented."""
    freqs, W, T = _lr4_branches()
    assert _select_alignment_pair(
        freqs, W, T, fc_hz=2000.0, lo_hz=30_000.0, hi_hz=40_000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=0.0, seed_delay_us=0.0, seed_polarity_sign=1,
    ) is None


def test_select_alignment_pair_refuses_to_flip_on_an_unmeasurable_branch():
    """The SNR precondition, at the function that owns it.

    The same inputs that make the objective flip the sign above are handed to
    it again with the capture calling one branch unmeasurable. It commits the
    DECLARED design — polarity AND, with nothing applied to hold, no delay —
    instead of a pair read off noise, scores the declined seed anyway, and
    names which commitment it made.
    """
    freqs, W, T = _lr4_branches()
    kwargs = dict(
        fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=25.0, seed_delay_us=90.0, seed_polarity_sign=-1,
    )
    trusted = _select_alignment_pair(freqs, W, T, **kwargs)
    refused = _select_alignment_pair(
        freqs, W, T, **kwargs, branch_snr_insufficient=True,
    )
    assert trusted is not None and refused is not None
    assert trusted.objective == ALIGNMENT_COMMITTED_FLAT_SUM
    assert refused.objective == ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR
    # Declared polarity, no search — and no delay, because neither the anchor
    # (25.0) nor the seed (90.0) may be committed here: both are this refused
    # capture's own answer (#2617).
    assert refused.polarity_sign == 1
    assert refused.delay_us == 0.0
    assert refused.grid_points == 1
    # Disclosed, not silent: the refused pair is still scored and reported.
    assert refused.seed_polarity_sign == -1
    assert refused.seed_ripple_db == pytest.approx(trusted.seed_ripple_db)
    # …and the cross-check reports NOT-ASKED, not disagreement: no flat sum ran
    # on the polarity axis here, so `False` would describe a comparison that
    # never happened.
    assert refused.polarity_agrees_with_sum is None
    assert trusted.polarity_agrees_with_sum is False


def test_select_alignment_pair_holds_the_applied_delay_on_an_unmeasurable_branch():
    """Issue #2617: the refusal's delay half.

    A speaker that already runs a commissioned alignment gets that alignment
    HELD — the ethos's best-available rule — rather than a fresh number read
    off the capture the SNR verdict just refused. The anchor and the seed are
    both offered and both declined, and the objective says which commitment
    this is, so a persisted candidate distinguishes "we held what this speaker
    plays" from "the design asks for none".
    """
    freqs, W, T = _lr4_branches()
    kwargs = dict(
        fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=25.0, seed_delay_us=90.0, seed_polarity_sign=-1,
        branch_snr_insufficient=True,
    )
    held = _select_alignment_pair(
        freqs, W, T, **kwargs, applied_alignment=AppliedAlignment(-140.0),
    )
    assert held is not None
    assert held.objective == ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR
    assert held.delay_us == pytest.approx(-140.0)
    assert held.polarity_sign == 1
    assert held.grid_points == 1
    assert held.polarity_agrees_with_sum is None
    # An applied delay of exactly zero is still a READ applied alignment, not
    # an absent one: the objective, not the value, is what says a prior existed.
    zero = _select_alignment_pair(
        freqs, W, T, **kwargs, applied_alignment=AppliedAlignment(0.0),
    )
    assert zero is not None
    assert zero.objective == ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR
    assert zero.delay_us == 0.0
    # THIRD arm: a graph IS applied and its record does not say what it plays.
    # Same 0.0 as the no-profile arm, but "the design asks for none" would be a
    # claim about this speaker that nothing checked, so the record says which.
    unreadable = _select_alignment_pair(
        freqs, W, T, **kwargs, applied_alignment=AppliedAlignment(None),
    )
    assert unreadable is not None
    assert unreadable.objective == ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY
    assert unreadable.delay_us == 0.0
    assert unreadable.polarity_sign == 1
    # All three commit the DECLARED polarity, so all three must be in the set
    # the estimate-publish gate and the household copy branch on.
    assert {held.objective, zero.objective, unreadable.objective} <= (
        ALIGNMENT_DECLARED_POLARITY_OBJECTIVES
    )


def test_the_applied_delay_never_reaches_a_capture_good_enough_to_score():
    """The prior is scoped to the refusal and nothing else.

    Handing the same applied delay to a TRUSTED capture must change nothing:
    a capture the SNR policy accepted is graded on its own evidence, and a
    selector that could be nudged toward the answer the speaker already has
    would make every re-measurement partly a copy of the last one.
    """
    freqs, W, T = _lr4_branches()
    kwargs = dict(
        fc_hz=2000.0, lo_hz=1000.0, hi_hz=4000.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=25.0, seed_delay_us=90.0, seed_polarity_sign=-1,
    )
    blind = _select_alignment_pair(freqs, W, T, **kwargs)
    told = _select_alignment_pair(
        freqs, W, T, **kwargs, applied_alignment=AppliedAlignment(-140.0),
    )
    assert blind is not None and told is not None
    assert told == blind


@pytest.mark.parametrize(
    (
        "tweeter_drive_db", "expected_magnitude_verdict",
        "expected_alignment_verdict", "expected_objective",
        "expected_polarity", "expected_cross_check",
    ),
    [
        (-13.0, "ok", "ok", ALIGNMENT_COMMITTED_FLAT_SUM, "inverted", True),
        # THE WINDOW the alignment decision class closes: the magnitude law
        # (25/20 dB) is satisfied here and the alignment law (35 dB) is not.
        # Reading the displayed magnitude verdict, as the first cut of this
        # refusal did, would ship a polarity read off this capture.
        (
            -48.0, "ok", "insufficient",
            ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR, "normal", None,
        ),
        (
            -70.0, "insufficient", "insufficient",
            ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR, "normal", None,
        ),
    ],
)
def test_measure_analysis_refuses_the_flip_when_a_branch_snr_is_insufficient(
    tweeter_drive_db, expected_magnitude_verdict, expected_alignment_verdict,
    expected_objective, expected_polarity, expected_cross_check,
):
    """The wiring: ``_measure_analysis`` reads the per-branch ALIGNMENT-class
    capture verdict and hands the refusal to the selector.

    Same genuinely-inverted branches every time; only the tweeter's DRIVE
    changes, which is what moves its capture SNR across the policy's lines
    against a fixed ambient report. Heard properly, the selector commits the
    inversion it can see. Unmeasurable BY THE LAW A POLARITY DECISION IS HELD
    TO, it commits the preset's declared design instead — the fail-safe
    direction, since the preset is the thing VERIFY grades against, and a sign
    read off a capture the instrument calls unusable is the defect this issue
    is about.

    Nothing is stubbed: both verdicts come from the production SNR policy
    reading the production ambient report.
    """
    ambient = {
        "schema_version": 1,
        "bands": [
            {"band_id": "low", "band_hz": [150.0, 1000.0], "level_dbfs": -90.0},
            {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -90.0},
            {"band_id": "high", "band_hz": [4000.0, 16000.0], "level_dbfs": -90.0},
        ],
    }
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": tweeter_drive_db}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, -0.7),
        epsilon=80e-6,
    )
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, ambient_report=ambient),
        geometry=MeasurementGeometry(),
    )
    tweeter = next(r for r in res.driver_responses if r.role == "tweeter")
    assert driver_snr_verdict(tweeter) == expected_magnitude_verdict
    assert driver_alignment_snr_verdict(tweeter) == expected_alignment_verdict
    assert res.candidate.alignment_objective == expected_objective
    assert res.candidate.polarity == expected_polarity
    assert res.alignment.polarity == expected_polarity
    # Correlation's own answer is preserved beside the commitment either way.
    # The cross-check is True where the objective agreed with it and NOT-ASKED
    # (None) on the refusal, which never put a flat sum on the polarity axis.
    assert res.candidate.seed_polarity_sign == -1
    assert res.alignment.polarity_agrees_with_sum is expected_cross_check


def _refused_two_way_analysis(applied_alignment=None, *, ambient=None):
    """A MEASURE analysis whose tweeter branch the ALIGNMENT law refuses.

    The -48 dB row of the parametrization above — inside the 15 dB window
    where the magnitude law passes and the alignment law does not — so the
    refusal is produced by the production SNR policy rather than asserted.
    """
    ambient = ambient or {
        "schema_version": 1,
        "bands": [
            {"band_id": "low", "band_hz": [150.0, 1000.0], "level_dbfs": -90.0},
            {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -90.0},
            {"band_id": "high", "band_hz": [4000.0, 16000.0], "level_dbfs": -90.0},
        ],
    }
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -48.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, -0.7),
        epsilon=80e-6,
    )
    return analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=FC_HZ, ambient_report=ambient,
            applied_alignment=applied_alignment,
        ),
        geometry=MeasurementGeometry(),
    )


def test_measure_analysis_holds_the_applied_delay_when_a_branch_is_unmeasurable(
    caplog,
):
    """Issue #2617, end to end through ``analyze_program_capture``.

    Same refused capture twice: told what the speaker already plays, and not.
    Told, it commits exactly that — on the CANDIDATE and on the published
    ``AlignmentEstimate`` that ``alignment_to_candidate_fields`` turns into the
    apply — and never the anchor this capture produced. Not told, it commits no
    delay. The polarity is the declared design either way (#2607, unchanged),
    which is what makes this the delay HALF of one refusal rather than a
    second, competing one.
    """
    caplog.set_level(logging.INFO)
    held = _refused_two_way_analysis(AppliedAlignment(59.6))
    assert held.candidate.alignment_objective == (
        ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR
    )
    assert held.candidate.delay_us == pytest.approx(59.6)
    assert held.alignment.delay_us == pytest.approx(59.6)
    # The anchor is REPORTED (it is evidence) and DECLINED (it is this
    # capture's own answer). Those must be two different numbers, or the fix
    # is not doing anything.
    assert held.candidate.anchor_delay_us is not None
    assert held.candidate.anchor_delay_us != pytest.approx(59.6)
    # #2607's half, unchanged: the polarity is the preset's declaration, and
    # nothing claims a flat sum answered it.
    assert held.candidate.polarity == "normal"
    assert held.alignment.polarity == "normal"
    assert held.alignment.polarity_agrees_with_sum is None
    # The reason is readable in one line, with both numbers on it.
    line = next(
        rec.getMessage() for rec in caplog.records
        if "program_analysis.alignment_selection" in rec.getMessage()
    )
    assert "objective=applied_alignment_held_after_low_snr" in line
    assert "applied_delay_us=59.6" in line
    assert "branch_snr_insufficient=true" in line
    assert "anchor_delay_us=" in line

    none_applied = _refused_two_way_analysis()
    assert none_applied.candidate.alignment_objective == (
        ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR
    )
    assert none_applied.candidate.delay_us == 0.0
    assert none_applied.alignment.delay_us == 0.0
    assert none_applied.candidate.polarity == "normal"


def test_the_2611_anchor_outlier_never_becomes_the_commitment():
    """The measured hazard, as a scenario (2026-08-16 jts3, issue #2611).

    Nine positions read the anchor: +59.6 µs on-axis — the shipped, correct
    value — against six clustered near -211 µs. The GOOD anchor was the
    outlier, so a refused capture that commits its own anchor re-rolls that
    die every round. Here the capture's anchor is forced to the -211 µs
    cluster while the speaker already plays +59.6: the commitment must be
    +59.6, and the roughly -270 µs of commanded correction that would have
    computed a deep null must never enter the candidate.
    """
    fc_hz, lo_hz, hi_hz = 1885.0, 942.5, 3770.0
    #: The gap this speaker's drivers really have — the on-axis anchor, and the
    #: value the shipped tune carried. A refused capture cannot see it.
    true_gap_us = 59.6
    #: What SIX of the nine positions read instead.
    outlier_anchor_us = -211.0
    freqs, W, T = _lr4_branches(fc_hz=fc_hz)
    kwargs = dict(
        fc_hz=fc_hz, lo_hz=lo_hz, hi_hz=hi_hz,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=outlier_anchor_us, seed_delay_us=outlier_anchor_us,
        seed_polarity_sign=-1,
        branch_snr_insufficient=True,
    )
    held = _select_alignment_pair(
        freqs, W, T, **kwargs, applied_alignment=AppliedAlignment(true_gap_us),
    )
    assert held is not None
    assert held.delay_us == pytest.approx(true_gap_us)
    assert held.objective == ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR
    # The compensating disclosure: 270.6 µs is more than half a period at
    # 1885 Hz (265.3 µs), so the record says the held delay and this capture's
    # anchor disagree by more than the lobe the anchor owns — and that raises
    # the selection log to WARNING.
    assert held.left_anchor_lobe is True

    # What each candidate commitment does to the SPEAKER — scored against the
    # TRUE gap, which is the frame the emitted graph actually runs in and the
    # one no refused capture can supply. This is the -36 dB-hole class of
    # commanded null in first-principles form.
    band = (freqs >= lo_hz) & (freqs <= hi_hz)

    def emitted_ripple_db(committed_us: float) -> float:
        summed = predicted_branch_sum(
            W[band], T[band], 0.0, 0.0, 1, freqs_hz=freqs[band],
            residual_delay_us=summed_model_residual_delay_us(
                true_gap_us, committed_us,
            ),
        )
        return _ripple_db(freqs[band], summed, lo_hz, hi_hz)

    assert emitted_ripple_db(outlier_anchor_us) > 30.0, (
        "the pre-#2617 commitment — this capture's own anchor — commands a null"
    )
    assert emitted_ripple_db(held.delay_us) < 0.01
    # And the degraded commitment, for a speaker with nothing to hold: a mild
    # penalty, not a null. That is the whole defence of committing 0.0 rather
    # than parking the alignment (never-nanny: best available, disclosed).
    none_applied = _select_alignment_pair(freqs, W, T, **kwargs)
    assert none_applied is not None
    assert none_applied.delay_us == 0.0
    assert emitted_ripple_db(none_applied.delay_us) < 1.0


#: The 2026-08-16 jts3 numbers, as the two models one held round can ship: the
#: applied graph's own +59.6 us commitment against a refused capture's -211 us
#: anchor is a 270.6 us disagreement, and half a period at 1885 Hz is 265.3 us.
HELD_ROUND_FC_HZ = 1885.0
HELD_ROUND_RESIDUAL_US = 270.6


def _held_round_models():
    """The shipped and the pre-#2617-scoping summed models of ONE held round.

    The same branches under the same commitment, differing only in whether the
    refused capture's anchor is allowed to phase the curve — which is exactly
    the difference ``summed_model_residual_delay_us`` now withholds.
    """
    freqs, W, T = _lr4_branches(fc_hz=HELD_ROUND_FC_HZ)

    def _curve(residual_us):
        summed = predicted_branch_sum(
            W, T, 0.0, 0.0, 1, freqs_hz=freqs, residual_delay_us=residual_us,
        )
        return freqs, 20.0 * np.log10(np.maximum(np.abs(summed), 1e-12))

    return _curve(0.0), _curve(HELD_ROUND_RESIDUAL_US)


def test_a_held_rounds_model_does_not_carry_the_refused_anchors_residual():
    """#2617 safety lens S-SF2, at the source, end to end.

    ``predicted_sum`` is not a drawing: the accountability gate can refuse a
    candidate on it and VERIFY tracking can fail a round on it. On the refused
    path the committed delay came from the applied graph and the anchor came
    from a capture the SNR policy called unusable FOR ALIGNMENT, so phasing the
    model by their difference fabricates a comb the emitted graph need not
    have.

    Pinned through the two numbers the analysis publishes rather than by
    rebuilding the branches: ``predicted_ripple_db`` is BY CONSTRUCTION the
    zero-residual ripple, so the shipped ``predicted_sum`` matching it IS the
    statement that no residual phased it. The disagreement is still recorded on
    ``snap_delta_us`` — withheld from the model, not from the receipt.
    """
    res = _refused_two_way_analysis(AppliedAlignment(59.6))
    assert res.candidate.alignment_objective == (
        ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR
    )
    withheld_us = summed_model_residual_delay_us(
        res.candidate.anchor_delay_us, res.candidate.delay_us,
    )
    assert abs(withheld_us) > 100.0, "the fixture must produce a large residual"

    freqs, predicted_db = res.predicted_sum
    lo, hi = overlap_band_hz(FC_HZ)
    band = (freqs >= lo) & (freqs <= hi)
    shipped_ripple = float(
        np.max(predicted_db[band]) - np.min(predicted_db[band])
    )
    assert shipped_ripple == pytest.approx(res.candidate.predicted_ripple_db, abs=1e-9)
    assert res.candidate.snap_delta_us == pytest.approx(withheld_us)


def test_diagnostic_summary_alignment_snr_trio_is_distinct_from_the_magnitude_one():
    """#2640: the diagnostic summary publishes the ALIGNMENT-class SNR trio
    beside the pre-existing MAGNITUDE one, off the same ``driver_responses``
    entry, and the two trios are not just a copy of each other.

    Reuses the ``-48 dB`` fixture two tests up: the magnitude law (25 dB) is
    satisfied and the alignment law (35 dB, no ``reduced`` rung) is not, so
    the two trios genuinely disagree here rather than coincidentally match.
    """
    res = _refused_two_way_analysis()
    tweeter = next(r for r in res.driver_responses if r.role == "tweeter")
    alignment_worst = tweeter.snr[DRIVER_SNR_ALIGNMENT_KEY]["worst_relevant"]

    summary = analysis_diagnostic_summary(res)
    # The alignment trio carries the ALIGNMENT block's own worst_relevant
    # values — read off the same entry the summary is built from, not a
    # separately-asserted number.
    assert summary["tweeter_alignment_snr_db"] == alignment_worst["estimated_snr_db"]
    assert summary["tweeter_alignment_snr_verdict"] == alignment_worst["verdict"]
    assert summary["tweeter_alignment_snr_band"] == alignment_worst["band_id"]

    # ...and distinct from the magnitude trio: the -48 dB row exists
    # precisely because the two laws disagree here.
    assert summary["tweeter_snr_verdict"] == "ok"
    assert summary["tweeter_alignment_snr_verdict"] == "insufficient"
    assert summary["tweeter_snr_verdict"] != summary["tweeter_alignment_snr_verdict"]


def test_a_held_round_clears_the_accountability_prediction_gate():
    """Drive both models through the gate that grades a candidate.

    Design constraint (#2622 panel): a predicted-vs-spec comparison leaning on
    the UNTRUSTED anchor must not condemn a held round. The evaluator and the
    gate are the production ones; only the curve differs. The gate stopped
    REFUSING with the nanny burn-down, so what separates the two curves is now
    its ledger verdict rather than a raised refusal — the same comparison,
    read one field over.
    """
    from jasper.active_speaker.crossover_v2 import accountability
    from jasper.active_speaker.crossover_v2.candidates import LinearizationState
    from jasper.active_speaker.crossover_v2_flow import (
        PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
        spec_report_for_predicted_sum,
    )

    shipped, combed = _held_round_models()

    def _ledger(predicted):
        # The FITTED arm. A pre-fit curve barely worse than the post-fit one is
        # what puts the improvement comparison under its material threshold, so
        # the verdict turns entirely on whether the post-fit curve met the spec
        # on its own.
        freqs, db = predicted
        raw = (freqs, db - 0.05)
        decision = accountability.assess_accountability(
            predicted_sum=predicted,
            raw_predicted_sum=raw,
            state=LinearizationState(outcome="fitted", linearized_predicted_sum=predicted),
            grade_prediction=spec_report_for_predicted_sum,
            material_improvement_db=PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
        )
        # No refusal to assert away: the gate has no ``refusal_reason`` to set
        # since the realized-level demotion (doctrine deviation (i)), which
        # ``test_crossover_v2_accountability`` pins structurally.
        return decision.spec_report["comparison"]["reason"]

    # The held round clears its own gate…
    assert _ledger(shipped) == accountability.LEDGER_PREDICTED_IN_SPEC
    # …and would NOT have, on the curve the refused anchor phased. This is the
    # kill S-SF2 named: a correctly-aligned speaker graded down by arithmetic
    # over an anchor its own capture disowned. (Until the nanny burn-down this
    # arm REFUSED under ``correction_not_an_improvement``; the arithmetic that
    # separates the two curves is unchanged, and it is what this test is about.)
    assert _ledger(combed) == accountability.LEDGER_NOT_AN_IMPROVEMENT
    # The evaluator's own read of the two, so the mechanism is visible and not
    # merely the verdict.
    shipped_report = spec_report_for_predicted_sum(shipped)
    combed_report = spec_report_for_predicted_sum(combed)
    assert shipped_report is not None and combed_report is not None
    assert shipped_report.overall_passed
    assert not combed_report.overall_passed


def test_verify_tracking_passes_a_held_round_and_still_fails_a_real_comb():
    """Drive both models through the gate that can FAIL a round.

    Three cases, one grader (``verification.evaluate_realization`` at the
    shipped ``VERIFY_TOLERANCE_DB``): a correctly-aligned speaker measured
    against the SHIPPED model MATCHES; measured against the model the refused
    anchor would have phased it FAILS — a round rolled back by arithmetic; and
    a speaker that REALLY combs still FAILS against the shipped model, which is
    the half of the constraint that must not be bought with leniency.
    """
    from jasper.active_speaker.crossover_v2 import verification
    from jasper.active_speaker.crossover_v2.contracts import RealizationStatus
    from jasper.active_speaker.crossover_v2.contracts import VERIFY_TOLERANCE_DB

    shipped, combed = _held_round_models()

    def _tracking(measured_ir, predicted):
        prog = build_verify_program(FC_HZ, sweep_s=1.5)
        pcm = render_program_pcm(prog)
        mono = fftconvolve(pcm[:, 0], measured_ir)[: pcm.shape[0]]
        cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
        cap = cap + np.random.default_rng(11).normal(0.0, 1e-4, cap.size)
        return analyze_program_capture(
            prog, cap, SR,
            priors=MeasurementPriors(
                crossover_fc_hz=FC_HZ, predicted_sum=predicted,
            ),
        ).verify_tracking

    def _verdict(tracking):
        return verification.evaluate_realization(
            tracking=tracking, tolerance_db=VERIFY_TOLERANCE_DB,
        ).status

    # A speaker whose emitted graph is correct: one clean arrival.
    aligned_ir = _band_impulse(200, 150.0, 20000.0, 1.0, n=8192)
    # A speaker that really combs: the same arrival plus a delayed copy, which
    # is what a WRONG alignment sounds like.
    combed_ir = aligned_ir.copy()
    combed_ir[200 + int(round(270e-6 * SR)):] += aligned_ir[
        200:len(aligned_ir) - int(round(270e-6 * SR))
    ]

    assert _verdict(_tracking(aligned_ir, shipped)) is RealizationStatus.MATCHED
    assert _verdict(_tracking(aligned_ir, combed)) is RealizationStatus.FAILED
    assert _verdict(_tracking(combed_ir, shipped)) is RealizationStatus.FAILED


# --------------------------------------------------------------------------- #
# the SNR verdict is scoped to the band the stimulus occupies (#2613)
# --------------------------------------------------------------------------- #


# The band table `_driver_snr_block` reads its signal side over — six canonical
# acoustic rows, and the shape of the #2613 defect: a row is admitted to the
# veto if it merely OVERLAPS the decision window, however little of it the
# sweep entered. Spelled here (rather than imported) so the ambient report a
# test builds and the rows a test names cannot drift apart silently.
_SNR_ROWS_HZ = (
    ("sub_bass", 20.0, 80.0),
    ("bass", 80.0, 160.0),
    ("upper_bass", 160.0, 350.0),
    ("transition", 350.0, 1000.0),
    ("mid", 1000.0, 4000.0),
    ("treble", 4000.0, 12000.0),
)

# jts3's commissioned geometry for the 2026-08-15/16 rounds #2613 was filed
# from — the same Fc and operator-declared driver bands the incident-replay
# fixture carries (tests/fixtures/crossover_v2_alignment_incident_20260816/
# session_context.json; the bands come from the driver safety profile's
# `hard_excitation_band_hz`, which is what RoleBand.band is built from). The
# real geometry, not a stand-in: the defect is a relationship between Fc, the
# sweep edges, and the SNR row table, so a rounded Fc could silently stop
# exercising it.
_JTS3_FC_HZ = 1648.7
_JTS3_WOOFER_BAND_HZ = (45.0, 4000.0)     # Epique E150HE-44; swept 150-4000
_JTS3_TWEETER_BAND_HZ = (1600.0, 20000.0)  # DE250-8; swept 1600-20000

# A quiet, FLAT room: every row at the same level, so a row's SNR is decided
# purely by whether the stimulus put energy in it. A sloped ambient would
# confound "the sweep never went here" with "the room is loud here".
_FLAT_AMBIENT = {
    "schema_version": 1,
    "bands": [
        {"band_id": band_id, "band_hz": [lo, hi], "level_dbfs": -90.0}
        for band_id, lo, hi in _SNR_ROWS_HZ
    ],
}


def _band_limited_two_way(
    *, fc_hz, woofer_band, tweeter_band, tweeter_drive_db=-13.0,
):
    """Analyze a two-way whose drivers are swept over THEIR OWN declared bands.

    The repo's other MEASURE fixtures declare a 300 Hz tweeter, which is swept
    across the whole nominal ``[Fc/2, Fc*2]`` window and so cannot express
    #2613 at all. A shipped two-way does not look like that:
    ``build_measure_program`` sweeps each driver over
    ``_intersect_band(declared_band, 150 Hz, 23 kHz)``, so a compression
    tweeter's sweep starts ABOVE ``Fc/2`` — jts3's at 1600 Hz against a
    824.35 Hz window floor — leaving canonical SNR rows inside the nominal
    window that its own stimulus never entered.

    Whether a driver leaves such a row is per-branch geometry, not a rule:
    jts3's woofer sweeps 150-4000 Hz and covers its whole window, which is why
    it is this fixture's control rather than a second defect. The mirror case
    (a woofer that DOES stop inside the window) needs its own bands, and the
    test below supplies them.

    Each branch's synthetic IR is band-limited to the same declared band, so
    "no signal in that row" is a property of the STIMULUS plan, not an
    artefact of an unrealistically narrow driver.
    """
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": tweeter_drive_db},
        [
            RoleBand("woofer", 0, FrequencyBand(*woofer_band)),
            RoleBand("tweeter", 1, FrequencyBand(*tweeter_band)),
        ],
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, woofer_band[0], woofer_band[1], 1.0),
        tweeter_ir=_band_impulse(225, tweeter_band[0], tweeter_band[1], -0.7),
    )
    return prog, analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=fc_hz, ambient_report=_FLAT_AMBIENT,
        ),
        geometry=MeasurementGeometry(),
    )


def _alignment_rows(response):
    """``{band_id: (estimated_snr_db, verdict)}`` for a branch's ALIGNMENT block."""
    block = (response.snr or {}).get("alignment") or {}
    return (
        block.get("relevant_hz"),
        block.get("worst_relevant") or {},
        {b["band_id"]: (b["estimated_snr_db"], b["verdict"]) for b in block["bands"]},
    )


def test_branch_snr_band_hz_clamps_the_window_to_the_stimulus_own_edges():
    """``[Fc/ρ, Fc·ρ]`` ∩ the radiated band, whichever edge binds.

    One branch, so the function cannot know which role it is holding: a
    tweeter's low edge and a woofer's high edge both have to clamp through the
    same call. The nominal band is preserved exactly when the stimulus spans
    it, and when no bounds are declared at all.
    """
    # Tweeter shape: swept from above Fc/2, so `lo` clamps up.
    assert branch_snr_band_hz(1600.0, (1500.0, 20000.0)) == (1500.0, 3200.0)
    # Woofer shape: swept to below Fc*2, so `hi` clamps down.
    assert branch_snr_band_hz(1600.0, (150.0, 2000.0)) == (800.0, 2000.0)
    # A stimulus that spans the nominal window changes nothing…
    assert branch_snr_band_hz(1600.0, (150.0, 20000.0)) == (800.0, 3200.0)
    # …and neither does an undeclared band: byte-identical to the pre-#2613
    # window, the same way overlap_band_hz treats an absent bound.
    assert branch_snr_band_hz(1600.0, None) == (800.0, 3200.0)


def _alignment_block_for(fc_hz, radiated_band_hz, capture_dbfs):
    """The ALIGNMENT-class block a branch gets for one window and capture level."""
    lo, hi = branch_snr_band_hz(fc_hz, radiated_band_hz)
    return (lo, hi), snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
        capture_bands=[
            {"band_id": band_id, "band_hz": [row_lo, row_hi],
             "level_dbfs": capture_dbfs}
            for band_id, row_lo, row_hi in _SNR_ROWS_HZ
        ],
        noise_bands=_FLAT_AMBIENT["bands"],
        noise_floor_dbfs_scalar=None,
        relevant_hz=(lo, hi),
        model=DRIVER,
        band_method="fft_band_power_difference",
    )


@pytest.mark.parametrize(
    "fc_hz,radiated_band_hz,capture_dbfs,expected_verdict,expected_band",
    [
        # Nothing spans the inverted interval -> genuinely no evidence.
        (1600.0, (8000.0, 20000.0), -20.0, "unknown", None),
        # `mid` [1000, 4000] spans the inverted (3000, 2000) interval, so it
        # IS enfranchised and returns a real verdict either way…
        (1000.0, (3000.0, 20000.0), -20.0, "ok", "mid"),
        (1000.0, (3000.0, 20000.0), -88.0, ALIGNMENT_SNR_REFUSAL_VERDICT, "mid"),
        # …and the mirror geometry (a woofer far below Fc) does the same.
        (6000.0, (150.0, 2000.0), -20.0, "ok", "mid"),
    ],
)
def test_branch_snr_band_hz_empty_window_can_still_return_a_real_verdict(
    fc_hz, radiated_band_hz, capture_dbfs, expected_verdict, expected_band,
):
    """An empty window is NOT "no verdict" — the earlier claim here was false.

    The first version of this test asserted ``unknown`` on ONE degenerate
    geometry and the docstring generalized that into "an empty window can
    never refuse". Both were wrong. ``worst_band_verdict``'s ``_band_overlaps``
    tests ``row_hi > lo`` and ``row_lo < hi`` INDEPENDENTLY, so when the window
    is inverted a row spanning the whole inverted interval satisfies both and
    is enfranchised: ``Fc = 1000`` radiating ``[3000, 20000]`` gives the window
    ``(3000, 2000)`` and returns a real ``mid`` verdict — ``ok`` clean,
    ``insufficient`` buried. Only a window nothing spans yields ``unknown``.

    Parametrized across the degenerate geometries rather than pinned to the
    lucky one, so the FIRST row no longer stands in for a rule it does not
    make. The property that IS universal is the next test's.
    """
    (lo, hi), block = _alignment_block_for(fc_hz, radiated_band_hz, capture_dbfs)
    assert lo >= hi, "these geometries must produce an EMPTY window"
    assert block["verdict"] == expected_verdict
    worst = block["worst_relevant"]
    assert (worst["band_id"] if worst else None) == expected_band


def test_branch_snr_band_hz_never_admits_a_row_outside_the_radiated_band():
    """The universal guarantee, empty windows included.

    Admission needs ``row_hi > lo >= radiated_lo`` AND
    ``row_lo < hi <= radiated_hi``, so every enfranchised row overlaps the
    radiated band whether or not the window is empty. That — not "an empty
    window returns unknown" — is what makes #2607's fail-safe unable to turn
    on a row the stimulus never entered, and it is the whole point of the
    clamp.

    Swept rather than argued, over a grid that deliberately includes inverted
    windows (a bare assertion on non-empty windows would miss the case the
    false claim above was hiding in).
    """
    fc_values = [80.0, 250.0, 700.0, 1000.0, 1648.7, 2500.0, 6000.0, 9000.0]
    lo_edges = [20.0, 150.0, 1600.0, 3000.0, 8000.0]
    hi_edges = [500.0, 2000.0, 4000.0, 12000.0, 20000.0]
    empty_windows = admitted_through_empty = 0

    for fc_hz in fc_values:
        for rad_lo in lo_edges:
            for rad_hi in hi_edges:
                if rad_hi <= rad_lo:
                    continue
                lo, hi = branch_snr_band_hz(fc_hz, (rad_lo, rad_hi))
                if lo >= hi:
                    empty_windows += 1
                for band_id, row_lo, row_hi in _SNR_ROWS_HZ:
                    if not snr_policy._band_overlaps([row_lo, row_hi], lo, hi):
                        continue
                    if lo >= hi:
                        admitted_through_empty += 1
                    assert row_hi > rad_lo and row_lo < rad_hi, (
                        f"row {band_id} [{row_lo}, {row_hi}] was admitted for "
                        f"fc={fc_hz} radiated=({rad_lo}, {rad_hi}) window="
                        f"({lo}, {hi}) but the stimulus never entered it"
                    )

    # The grid has to actually exercise the hazardous shapes, or the assertion
    # above passes by never running on one (issue #2198's "instrument silence
    # is not evidence").
    assert empty_windows > 0
    assert admitted_through_empty > 0


def test_measure_snr_verdict_ignores_a_row_the_tweeter_sweep_never_entered():
    """#2613: a deliberately empty row must not veto a clean capture.

    THE incident, end to end, on **jts3's own commissioned geometry** — the
    same Fc and declared driver bands as the 2026-08-15/16 rounds
    (``tests/fixtures/crossover_v2_alignment_incident_20260816/``): LR4 at
    :data:`_JTS3_FC_HZ`, an Epique E150HE-44 woofer declared ``[45, 4000]``
    and a DE250-8 tweeter declared ``[1600, 20000]``.

    That geometry is what makes the failure structural. The nominal window is
    ``[824.35, 3297.4]``, and the ``transition`` row (350-1000 Hz) OVERLAPS it
    — 1000 > 824.35 — so the row was enfranchised. The tweeter sweep starts at
    1600 Hz, so that row holds nothing but room noise, and its SNR is the ratio
    of the room to itself: **no room, no night, and no drive level can move
    it**. The box recorded −1.2 dB there (−0.4 dB on five of six captures in
    the 2026-08-10 dump), the ALIGNMENT verdict read ``insufficient``, and
    #2607's declared-design fail-safe fired on 14 of 14 rounds.

    The **woofer is the control, and it is a geometric one**: its sweep
    (150-4000 Hz after the MEASURE clamp) spans the whole nominal window, so no
    row inside that window is empty for it, its window is unchanged by the fix,
    and it verdicted ``ok`` at 44.0 dB on those same 14 rounds. Tweeter fails
    and woofer passes on the same captures because of where the sweeps end —
    which is exactly the asymmetry a broadband-SNR explanation could not
    produce.

    Each assertion below is a separate claim, and each FAILS on the pre-fix
    code: the window is clamped, the veto comes from an occupied row, the
    verdict passes, and the flat-sum selector therefore gets to run at all
    (pre-fix it committed the declared design without scoring the pair, so the
    cross-check reports "not asked").

    The empty row's evidence is still REPORTED, just not admitted: the
    per-band table is diagnostics, ``relevant_hz`` is the franchise.
    """
    _prog, res = _band_limited_two_way(
        fc_hz=_JTS3_FC_HZ,
        woofer_band=_JTS3_WOOFER_BAND_HZ,
        tweeter_band=_JTS3_TWEETER_BAND_HZ,
    )
    tweeter = next(r for r in res.driver_responses if r.role == "tweeter")
    window, worst, rows = _alignment_rows(tweeter)

    # The empty row IS inside the nominal window — that is the whole defect,
    # and asserting it here stops a future Fc change from quietly turning this
    # test into a tautology that would pass with or without the fix.
    assert 1000.0 > _JTS3_FC_HZ / 2.0
    # The window no longer reaches below the tweeter's own sweep floor.
    assert window == [1600.0, _JTS3_FC_HZ * 2.0]
    # The row that used to veto is still measured, still damning, still shown…
    assert rows["transition"][1] == "insufficient"
    assert rows["transition"][0] < 10.0
    # …and no longer decides anything: the veto is scoped to an OCCUPIED row.
    assert worst["band_id"] == "mid"
    assert worst["verdict"] == "ok"
    assert rows["mid"][0] > DRIVER.alignment_snr_ok_db
    assert driver_alignment_snr_verdict(tweeter) == "ok"
    assert driver_snr_verdict(tweeter) == "ok"

    # …so the flat-sum selector actually scores the pair instead of the
    # declared-design fail-safe committing on a one-point grid.
    assert res.candidate.alignment_objective == ALIGNMENT_COMMITTED_FLAT_SUM
    assert res.alignment.polarity_agrees_with_sum is not None

    # The geometric control. This woofer's sweep spans the whole nominal
    # window, so the clamp is a no-op for it and its verdict is identical
    # either side of the fix — which is why the box saw the tweeter fail and
    # the woofer pass on the SAME 14 captures.
    woofer = next(r for r in res.driver_responses if r.role == "woofer")
    w_window, _w_worst, _w_rows = _alignment_rows(woofer)
    assert w_window == [_JTS3_FC_HZ / 2.0, _JTS3_FC_HZ * 2.0]
    assert driver_alignment_snr_verdict(woofer) == "ok"


def test_measure_snr_verdict_ignores_a_row_above_the_woofer_sweep():
    """The woofer mirror: the same clamp, from the other edge.

    A woofer declared to 4 kHz at Fc = 2.5 kHz leaves the ``treble`` row
    (4-12 kHz) inside the nominal ``[1250, 5000]`` window with nothing in it:
    the sweep stops exactly at that row's 4 kHz floor, so its coverage is
    0 Hz — the same "row the stimulus never entered" shape as the tweeter's
    ``transition``, arriving from above instead of below. Pinned because the
    fix is stated for ONE branch that does not know its role, so both edges
    must be exercised: a clamp written only for a tweeter would leave this
    half broken.
    """
    _prog, res = _band_limited_two_way(
        fc_hz=2500.0, woofer_band=(150.0, 4000.0), tweeter_band=(1500.0, 20000.0),
    )
    woofer = next(r for r in res.driver_responses if r.role == "woofer")
    window, worst, rows = _alignment_rows(woofer)

    assert window == [1250.0, 4000.0]
    assert rows["treble"][1] == "insufficient"
    assert worst["band_id"] == "mid"
    assert driver_alignment_snr_verdict(woofer) == "ok"
    assert res.candidate.alignment_objective == ALIGNMENT_COMMITTED_FLAT_SUM


@pytest.mark.parametrize(
    "tweeter_drive_db,expected_magnitude_verdict",
    [
        # The 15 dB window #2607 opened the alignment class for: the magnitude
        # law (25/20 dB) is satisfied and the 35 dB alignment law is not.
        (-48.0, "ok"),
        (-70.0, "insufficient"),
    ],
)
def test_measure_snr_verdict_still_refuses_a_noisy_occupied_row(
    tweeter_drive_db, expected_magnitude_verdict,
):
    """Scoping the window is not weakening the law.

    jts3's own geometry again — the ``transition`` row is still empty and
    still excluded — but the tweeter is driven so quietly that the OCCUPIED
    ``mid`` row is itself buried. The 35 dB alignment threshold is unchanged,
    the veto now comes from a row that carries real signal, and #2607's
    declared-design fail-safe fires exactly as it should. A genuinely noisy
    capture must still refuse; this is what stops the fix from reading as
    "make the tweeter pass".
    """
    _prog, res = _band_limited_two_way(
        fc_hz=_JTS3_FC_HZ,
        woofer_band=_JTS3_WOOFER_BAND_HZ,
        tweeter_band=_JTS3_TWEETER_BAND_HZ,
        tweeter_drive_db=tweeter_drive_db,
    )
    tweeter = next(r for r in res.driver_responses if r.role == "tweeter")
    _window, worst, rows = _alignment_rows(tweeter)

    assert worst["band_id"] == "mid"
    assert rows["mid"][0] < DRIVER.alignment_snr_ok_db
    assert driver_alignment_snr_verdict(tweeter) == ALIGNMENT_SNR_REFUSAL_VERDICT
    assert driver_snr_verdict(tweeter) == expected_magnitude_verdict
    assert res.candidate.alignment_objective == (
        ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR
    )


# --------------------------------------------------------------------------- #
# the summed model carries the COMMITTED delay (rung P3 / R10b)
# --------------------------------------------------------------------------- #


_IMPULSE_GAP_SAMPLES = 11
# The anchor an aligner with NO inter-sweep drift and NO parallax reports for
# `_impulse_branches`: the raw argmax gap, negated into the signed candidate
# convention. Building it from the fixture rather than quoting a number is what
# lets the physics-loop test below be exact — with either correction non-zero,
# `−anchor` is deliberately NOT the gap the argmax frame removed.
_IMPULSE_ANCHOR_US = -_IMPULSE_GAP_SAMPLES / SR * 1e6


def _impulse_branches(*, woofer_idx=1000, gap_samples=_IMPULSE_GAP_SAMPLES, n=8192):
    """Two ideal, coincident-bandwidth branches whose argmax gap is known."""
    woofer_ir = np.zeros(n)
    tweeter_ir = np.zeros(n)
    woofer_ir[woofer_idx] = 1.0
    tweeter_ir[woofer_idx + gap_samples] = 1.0
    return woofer_ir, tweeter_ir


def test_summed_model_residual_delay_us_is_applied_minus_anchor():
    """The ONE derivation of the model's delay term, and its two boundaries.

    ``anchor_delay_us`` is ``(D_w − D_t)`` in the signed candidate convention
    (positive ⇒ tweeter EARLIER), so ``−anchor`` is the ``(D_t − D_w)`` the
    argmax-referenced frame already removed and the residual is
    ``applied − anchor``. AT the anchor it is exactly zero, which is what makes
    the anchor the frame the branch pair is already in; a refused estimate
    (no anchor) carries no delay term at all, matching
    ``crossover_v2_flow.alignment_to_candidate_fields``, which applies no delay
    for that same status.
    """
    assert summed_model_residual_delay_us(120.0, 120.0) == 0.0
    assert summed_model_residual_delay_us(120.0, 180.0) == pytest.approx(60.0)
    assert summed_model_residual_delay_us(120.0, 60.0) == pytest.approx(-60.0)
    # Sign is carried through the negative (woofer-delayed) lobe unchanged.
    assert summed_model_residual_delay_us(-120.0, -180.0) == pytest.approx(-60.0)
    # No trustworthy anchor ⇒ no term, whatever delay is quoted.
    assert summed_model_residual_delay_us(None, 999.0) == 0.0


def test_predicted_sum_carries_the_committed_delay_and_ripple_does_not():
    """**The R10b change.** The persisted ``predicted_sum`` models the sum the
    speaker will actually produce under the COMMITTED delay; the candidate's
    ``predicted_ripple_db`` deliberately stays on the independently-aligned
    instrument the G1 capture-quality threshold was calibrated against.

    Both curves are rebuilt here from the same public primitives, so this pins
    the exact residual (``committed − anchor``, i.e. ``snap_delta_us``) rather
    than merely "something changed".
    """
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _impulse_branches()
    n_fft = 16_384
    anchor_us = _IMPULSE_ANCHOR_US
    # A snap the aligner found 60 us off the anchor — inside the +/-(period/6)
    # radius at Fc (83.3 us), so this is a reachable correlation seed.
    snapped_us = anchor_us + 60.0
    alignment = AlignmentEstimate(
        delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1,
        confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=anchor_us, snapped_delay_us=snapped_us,
    )
    candidate, (pred_freqs, pred_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        alignment, None,
        # A declared |delay| ceiling BELOW the anchor's own magnitude (229 us),
        # which is what keeps this fixture's residual large enough for (2) to
        # be a live assertion since #2598: two ideal coincident-bandwidth
        # impulses sum flattest time-aligned, so an unconstrained flat-sum
        # search would commit at the anchor and the two ripple instruments
        # would agree to 0.06 dB. Under the declared ceiling the flattest
        # ADMISSIBLE pair is the correlation seed, and the selector commits it.
        alignment_delay_bounds_us=(0.0, 175.0),
    )
    assert candidate.delay_us == pytest.approx(snapped_us, abs=1e-9)
    assert candidate.snap_delta_us == pytest.approx(60.0, abs=1e-9)

    freqs, W, gate_w = _aligned_branch_tf(woofer_ir, SR, n_fft, calibration=None)
    _f2, T, gate_t = _aligned_branch_tf(tweeter_ir, SR, n_fft, calibration=None)
    trim_w = candidate.trim_db["woofer"]
    trim_t = candidate.trim_db["tweeter"]
    aligned = predicted_branch_sum(W, T, trim_w, trim_t, 1)
    applied = predicted_branch_sum(
        W, T, trim_w, trim_t, 1,
        freqs_hz=freqs, residual_delay_us=candidate.snap_delta_us,
    )

    # (1) The persisted curve is the APPLIED model, exactly.
    np.testing.assert_array_equal(pred_freqs, freqs)
    np.testing.assert_allclose(
        pred_db, 20.0 * np.log10(np.maximum(np.abs(applied), 1e-12)), atol=1e-12,
    )
    # ...and it is genuinely a different curve from the pre-R10b one, so the
    # term is not a silent no-op on this path.
    aligned_db = 20.0 * np.log10(np.maximum(np.abs(aligned), 1e-12))
    assert not np.allclose(pred_db, aligned_db, atol=1e-6)

    # (2) `predicted_ripple_db` — G1's instrument — did NOT move onto it.
    lo, hi = overlap_band_hz(fc_hz)
    branch_floor_hz = max(
        (f for f in (_gate_floor_hz(gate_w), _gate_floor_hz(gate_t)) if f is not None),
        default=None,
    )
    lo_clamped = max(lo, branch_floor_hz) if branch_floor_hz is not None else lo
    assert candidate.predicted_ripple_db == pytest.approx(
        _ripple_db(freqs, aligned, lo_clamped, hi), abs=1e-12,
    )
    # And the two instruments really do disagree here, so (2) is a live
    # assertion rather than one satisfied by both curves being the same.
    assert candidate.predicted_ripple_db != pytest.approx(
        _ripple_db(freqs, applied, lo_clamped, hi), abs=0.5,
    )


def test_zero_residual_predicted_sum_is_bit_identical_to_the_pre_r10b_model():
    """The reduction that keeps every anchor-selected session unchanged.

    When the snap is not taken the selection IS the anchor, the residual is
    exactly ``0.0``, and the persisted curve must be bit-for-bit the
    zero-residual sum this module built before R10b — not merely close. The
    same reduction is what leaves an aligner-refused (no-anchor) analysis
    untouched.
    """
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _impulse_branches()
    n_fft = 16_384
    anchor_us = _IMPULSE_ANCHOR_US
    for snapped_us, anchor in ((None, anchor_us), (None, None)):
        alignment = AlignmentEstimate(
            delay_us=anchor_us if anchor is not None else -650.0,
            raw_delay_us=-650.0, parallax_us=0.0,
            polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
            confidence=0.9,
            status=ALIGNMENT_OK if anchor is not None
            else ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
            anchor_delay_us=anchor, snapped_delay_us=snapped_us,
        )
        candidate, (_pred_freqs, pred_db) = _build_candidate(
            woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
            alignment, None, alignment_delay_bounds_us=(0.0, 1000.0),
        )
        freqs, W, _gw = _aligned_branch_tf(woofer_ir, SR, n_fft, calibration=None)
        _f2, T, _gt = _aligned_branch_tf(tweeter_ir, SR, n_fft, calibration=None)
        legacy = predicted_branch_sum(
            W, T, candidate.trim_db["woofer"], candidate.trim_db["tweeter"], 1,
        )
        np.testing.assert_array_equal(
            pred_db, 20.0 * np.log10(np.maximum(np.abs(legacy), 1e-12)),
        )


def test_committed_delay_model_tracks_the_real_applied_sum_better():
    """The reason this is an improvement and not just a change.

    The speaker will emit the two branches under the committed delay, in the
    ORIGINAL physical time origin. Rebuild that sum from the raw IRs and score
    both models against it on one mean-centred ruler: the applied model has to
    be materially closer than a model of a delay no selection realizes.

    The comparator is the SEED delay the flat-sum selector rejected (#2598) —
    the pair the aligner's correlation snap would have committed before it.
    Two ideal coincident impulses sum flattest time-aligned, so the selector
    lands near the anchor and the zero-residual curve is no longer the wrong
    model to contrast with; the rejected seed is, and it is also the more
    interesting one, since it is what this speaker would have shipped.
    """
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _impulse_branches()
    n_fft = 16_384
    # Zero drift, zero parallax ⇒ the anchor IS the negated argmax gap, so the
    # model's residual and the physical sum's residual are the SAME number and
    # the loop can be closed exactly rather than approximately.
    anchor_us = _IMPULSE_ANCHOR_US
    snapped_us = anchor_us + 60.0
    alignment = AlignmentEstimate(
        delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1,
        confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=anchor_us, snapped_delay_us=snapped_us,
    )
    candidate, (freqs, applied_model_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        alignment, None, alignment_delay_bounds_us=(0.0, 1000.0),
    )
    _f, W, _gw = _aligned_branch_tf(woofer_ir, SR, n_fft, calibration=None)
    _f2, T, _gt = _aligned_branch_tf(tweeter_ir, SR, n_fft, calibration=None)
    seed_model_db = 20.0 * np.log10(np.maximum(np.abs(predicted_branch_sum(
        W, T, candidate.trim_db["woofer"], candidate.trim_db["tweeter"], 1,
        freqs_hz=freqs,
        residual_delay_us=summed_model_residual_delay_us(anchor_us, snapped_us),
    )), 1e-12))

    # Truth: the raw branches, each delayed by the side of the committed delay
    # it actually carries (the same construction
    # `test_snap_production_path_preserves_parallax_contract` closes its
    # physics loop with).
    g_w = 10.0 ** (candidate.trim_db["woofer"] / 20.0)
    g_t = 10.0 ** (candidate.trim_db["tweeter"] / 20.0)
    applied_us = candidate.delay_us
    physical = (
        np.fft.rfft(woofer_ir, n=n_fft) * g_w
        * np.exp(-1j * 2.0 * np.pi * freqs * max(0.0, -applied_us) * 1e-6)
        + np.fft.rfft(tweeter_ir, n=n_fft) * g_t
        * np.exp(-1j * 2.0 * np.pi * freqs * max(0.0, applied_us) * 1e-6)
    )
    physical_db = 20.0 * np.log10(np.maximum(np.abs(physical), 1e-12))

    lo, hi = overlap_band_hz(fc_hz)
    band = (freqs >= lo) & (freqs <= hi)

    def _centred_rms(model_db):
        err = model_db[band] - physical_db[band]
        return float(np.sqrt(np.mean((err - np.mean(err)) ** 2)))

    applied_err = _centred_rms(applied_model_db)
    seed_err = _centred_rms(seed_model_db)
    # The loop CLOSES: on an exact frame the applied model is not merely
    # closer, it is the physical sum (0.0 dB to float precision).
    assert applied_err == pytest.approx(0.0, abs=1e-6)
    # And the rejected seed was materially wrong about the same speaker,
    # against a VERIFY tracking tolerance of 1.5 dB. So this is a real slice of
    # the tracking budget, not a rounding-scale change.
    assert seed_err > 0.5


@pytest.mark.parametrize(
    ("d_w", "d_t"),
    [
        (230, 200),
        (200, 230),
    ],
)
def test_snap_production_path_preserves_parallax_contract(
    d_w, d_t,
):
    """The full MEASURE snap path keeps raw/corrected frames honest on both lobes."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0},
        _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    woofer_ir = _band_impulse(d_w, 150.0, 6000.0, 1.0)
    tweeter_ir = _band_impulse(d_t, 300.0, 20000.0, 0.9)
    cap = _synthesize(
        prog,
        woofer_ir=woofer_ir,
        tweeter_ir=tweeter_ir,
        epsilon=30e-6,
        noise=0.0,
    )
    geometry = MeasurementGeometry(driver_spacing_m=0.15, mic_distance_m=1.0)
    result = analyze_program_capture(
        prog,
        cap,
        SR,
        geometry=geometry,
        priors=MeasurementPriors(
            crossover_fc_hz=FC_HZ,
            alignment_delay_bounds_us=(0.0, 1000.0),
        ),
    )

    expected_raw_us = (d_w - d_t) / SR * 1e6
    expected_delay_us = expected_raw_us - geometry.parallax_us()
    assert result.drift.epsilon_ppm == pytest.approx(30.0, abs=2.0)
    assert result.alignment.seed_delay_us == pytest.approx(expected_delay_us, abs=5.0)
    measured_global_offset, _first, _stimuli, _amb = _global_offset(prog, cap, SR)
    seg_w = prog.segment("sweep_w")
    seg_t = prog.segment("sweep_t")
    epsilon = result.drift.epsilon_ppm / 1e6
    woofer_full_ir, _pre_w = _deconvolve_window(
        cap,
        seg_w,
        measured_global_offset + seg_w.start_sample,
        SR,
        epsilon=epsilon,
    )
    tweeter_full_ir, _pre_t = _deconvolve_window(
        cap,
        seg_t,
        measured_global_offset + seg_t.start_sample,
        SR,
        epsilon=epsilon,
    )
    measured_peak_gap_us = (
        int(np.argmax(np.abs(tweeter_full_ir)))
        - int(np.argmax(np.abs(woofer_full_ir)))
    ) / SR * 1e6
    inter_sweep_drift_us = (
        epsilon * (seg_t.start_sample - seg_w.start_sample) / SR * 1e6
    )
    physical_peak_gap_us = measured_peak_gap_us - inter_sweep_drift_us
    physical_seed_us = -(physical_peak_gap_us + geometry.parallax_us())
    # Anchor-primary + gated snap (methodology §10, 2026-07-22): the selected
    # delay is the physical peak-gap anchor snapped within ±(period/6) at Fc —
    # same sign side as the anchor, never a periodic-comb-lobe jump.
    snap_radius_us = 1e6 / FC_HZ * GCC_SNAP_RADIUS_PERIODS
    # S1 single-source pin: the candidate's anchor is EXACTLY the aligner-owned
    # anchor (derived, never a parallel argmax), and both equal the manual
    # physical peak-gap computation.
    assert result.candidate.anchor_delay_us == result.alignment.anchor_delay_us
    assert result.candidate.anchor_delay_us == pytest.approx(physical_seed_us, abs=1e-9)
    assert abs(result.alignment.delay_us - physical_seed_us) <= snap_radius_us + 1e-6
    assert math.copysign(1.0, result.alignment.delay_us) == math.copysign(
        1.0,
        physical_seed_us,
    )
    # The band-limited synthetic IR's spectral truncation shifts its argmax by
    # a fraction of a sample relative to the impulse placement. The production
    # selector operates in that measured argmax frame, so allow that expected
    # analysis granularity in addition to the sub-sample snap.
    assert result.alignment.delay_us == pytest.approx(
        expected_delay_us, abs=8.0,
    )
    assert result.alignment.raw_delay_us == pytest.approx(
        result.alignment.delay_us + result.alignment.parallax_us, abs=1e-9,
    )
    assert result.alignment.confidence_source == "gcc_phat_seed"
    assert result.candidate.delay_us == result.alignment.delay_us
    assert result.candidate.alignment_seed_ripple_db is not None
    # Flatness is evidence only now — not a selector — so the anchor→snap ripple
    # improvement is recorded but not constrained to be non-negative.
    assert result.candidate.flatness_improvement_db is not None
    responses = {response.role: response for response in result.driver_responses}
    lo_hz, hi_hz = overlap_band_hz(
        FC_HZ,
        tweeter_sweep_lo_hz=prog.segment("sweep_t").f1_hz,
        woofer_sweep_hi_hz=prog.segment("sweep_w").f2_hz,
    )
    floors = [
        response.validity_floor_hz
        for response in responses.values()
        if response.validity_floor_hz is not None
    ]
    lo_hz = max([lo_hz, *floors])
    # `predicted_ripple_db` reads the INDEPENDENTLY ALIGNED sum — the
    # capture-quality instrument the G1 ripple threshold was calibrated on, which
    # deliberately did not follow the persisted curve onto the committed-delay
    # model at rung P3 / R10b. So this reconstruction stays five-argument.
    predicted_aligned = predicted_branch_sum(
        responses["woofer"].complex_tf,
        responses["tweeter"].complex_tf,
        result.candidate.trim_db["woofer"],
        result.candidate.trim_db["tweeter"],
        result.alignment.polarity_sign,
    )
    assert result.candidate.predicted_ripple_db == pytest.approx(
        _ripple_db(
            responses["woofer"].freqs_hz,
            predicted_aligned,
            lo_hz,
            hi_hz,
        ),
        abs=1e-9,
    )
    # ...while the PERSISTED prediction — VERIFY's tracking reference — is the
    # same branches under the delay this candidate commits (rung P3 / R10b).
    # The residual is `selected − anchor`, which on this path is exactly the
    # candidate's own `snap_delta_us`; the sub-sample snap makes it small but
    # non-zero, so the two curves below are genuinely different objects.
    #
    # It is pinned by CONSTRUCTION here rather than against a hand-built
    # physical sum, because this fixture carries a deliberate inter-sweep drift
    # AND a parallax correction: `−anchor_delay_us` is then the drift-corrected,
    # parallax-corrected gap rather than the raw argmax gap the branch frame
    # removed, so the model's residual and a raw-time-origin physical residual
    # are not the same number by construction. The exact closure lives in
    # `test_committed_delay_model_tracks_the_real_applied_sum_better`, on a
    # zero-drift zero-parallax frame where they are.
    assert result.candidate.snap_delta_us != pytest.approx(0.0, abs=1e-6)
    predicted_applied = predicted_branch_sum(
        responses["woofer"].complex_tf,
        responses["tweeter"].complex_tf,
        result.candidate.trim_db["woofer"],
        result.candidate.trim_db["tweeter"],
        result.alignment.polarity_sign,
        freqs_hz=responses["woofer"].freqs_hz,
        residual_delay_us=summed_model_residual_delay_us(
            result.alignment.anchor_delay_us, result.candidate.delay_us,
        ),
    )
    persisted_freqs, persisted_db = result.predicted_sum
    np.testing.assert_array_equal(persisted_freqs, responses["woofer"].freqs_hz)
    np.testing.assert_allclose(
        persisted_db,
        20.0 * np.log10(np.maximum(np.abs(predicted_applied), 1e-12)),
        atol=1e-12,
    )
    assert not np.allclose(
        persisted_db,
        20.0 * np.log10(np.maximum(np.abs(predicted_aligned), 1e-12)),
        atol=1e-6,
    )

    # Close the physics loop in the fixture's original common time origin.
    # This assertion fails if the production path discards the physical peak
    # gap even though its peak-referenced objective may still look flat.
    n_fft = (responses["woofer"].freqs_hz.size - 1) * 2
    freqs_hz = responses["woofer"].freqs_hz
    g_w = 10.0 ** (result.candidate.trim_db["woofer"] / 20.0)
    g_t = 10.0 ** (result.candidate.trim_db["tweeter"] / 20.0)

    def physical_sum(applied_delay_us: float) -> np.ndarray:
        W_physical = np.fft.rfft(woofer_ir, n=n_fft) * g_w
        T_physical = np.fft.rfft(tweeter_ir, n=n_fft) * g_t
        W_physical *= np.exp(
            -1j * 2.0 * np.pi * freqs_hz
            * max(0.0, -applied_delay_us) * 1e-6
        )
        T_physical *= np.exp(
            -1j * 2.0 * np.pi * freqs_hz
            * max(0.0, applied_delay_us) * 1e-6
        )
        return W_physical + result.alignment.polarity_sign * T_physical

    applied_ripple_db = _ripple_db(
        freqs_hz,
        physical_sum(result.candidate.delay_us),
        lo_hz,
        hi_hz,
    )
    unapplied_ripple_db = _ripple_db(
        freqs_hz,
        physical_sum(0.0),
        lo_hz,
        hi_hz,
    )
    assert applied_ripple_db < 1.0
    assert unapplied_ripple_db > 5.0
    diagnostic = analysis_diagnostic_summary(result)
    assert diagnostic["alignment_confidence_source"] == "gcc_phat_seed"
    assert diagnostic["alignment_seed_delay_us"] == pytest.approx(
        result.alignment.seed_delay_us,
        abs=0.001,
    )
    assert diagnostic["alignment_refinement_delta_us"] == pytest.approx(
        result.alignment.delay_us - result.alignment.seed_delay_us,
        abs=0.001,
    )
    assert diagnostic["flatness_improvement_db"] == pytest.approx(
        result.candidate.flatness_improvement_db,
        abs=0.0001,
    )
    assert diagnostic["anchor_delay_us"] == pytest.approx(
        result.candidate.anchor_delay_us, abs=0.001,
    )
    assert diagnostic["snap_delta_us"] == pytest.approx(
        result.candidate.snap_delta_us, abs=0.001,
    )
    assert diagnostic["snap_found"] is result.candidate.snap_found


def test_parallax_is_subtracted():
    geo = MeasurementGeometry(driver_spacing_m=0.15, mic_distance_m=1.0)
    parallax = geo.parallax_us()
    assert parallax > 0
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(230, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200, 300.0, 20000.0, 0.9),
        epsilon=0.0,
    )
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ), geometry=geo,
    )
    # delay_us = raw_delay_us − parallax_us.
    assert res.alignment.delay_us == pytest.approx(
        res.alignment.raw_delay_us - parallax, abs=1e-6
    )


# --------------------------------------------------------------------------- #
# CHECK — ambient, linearity, channel map, gain plan
# --------------------------------------------------------------------------- #


def _check_roles() -> list[RoleBand]:
    """Disjoint bands for the CHECK fixtures below (Fix 1, W6.4).

    Unlike ``_roles()``'s heavily-overlapping MEASURE-style bands (needed
    elsewhere for Fc-overlap trim solving), the band-relative channel-map test
    (`_channel_map_ok`) measures absolute dB rise rather than a total-energy
    fraction, so a CROSS band immediately adjacent to a shared boundary now
    also picks up the stimulus generator's own ~5 ms fade-in/out spectral
    splatter (`synchronized_swept_sine`) — real, but concentrated within
    about an octave of a chirp's own start/end frequency, same as a real
    driver's transition band. Keeping these two bands clearly apart avoids
    exercising that (real, but here irrelevant) edge effect.
    """
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 1200.0)),
        RoleBand("tweeter", 1, FrequencyBand(2500.0, 20000.0)),
    ]


def _check_capture(program, *, compress_hi: bool = False, seed: int = 3):
    pcm = render_program_pcm(program)
    woofer_ir = _band_impulse(200, 150.0, 1200.0, 1.0)
    tweeter_ir = _band_impulse(225, 2500.0, 20000.0, 0.7)
    mono = (
        fftconvolve(pcm[:, 0], woofer_ir)[: pcm.shape[0]]
        + fftconvolve(pcm[:, 1], tweeter_ir)[: pcm.shape[0]]
    )
    cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])
    if compress_hi:
        # Simulate AGC: attenuate the two HI pilots so the captured 10 dB delta
        # is compressed to ~4 dB.
        for role in ("woofer", "tweeter"):
            seg = program.segment(f"pilot_{role}_hi")
            lo = 500 + seg.start_sample
            cap[lo:lo + seg.n_samples] *= 10 ** (-6.0 / 20.0)
    cap = cap + np.random.default_rng(seed).normal(0.0, 3e-5, cap.size)
    return cap


def test_check_linearity_and_channel_map_pass_for_clean_capture():
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.linearity_ok is True
    assert res.channel_map_ok is True
    for pilot in res.pilots:
        assert pilot.captured_delta_db == pytest.approx(10.0, abs=0.5)


def test_check_pilot_programmed_hi_gain_db_matches_the_segment():
    """``PilotObservation.programmed_hi_gain_db`` (measurement-honesty gate
    G3's raw material, crossover_v2_flow.py) is the HI segment's own
    declared gain — published for every pilot, not just a v2 MEASURE/VERIFY
    leading pair, since ``_pilot_observations`` is the ONE construction site
    for all of them."""
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.pilots
    for pilot in res.pilots:
        hi_seg = prog.segment(f"pilot_{pilot.role}_hi")
        assert pilot.programmed_hi_gain_db == pytest.approx(hi_seg.gain_db)


def test_check_linearity_fails_under_simulated_agc():
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog, compress_hi=True)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    # The programmed 10 dB delta is captured as ~4 dB ⇒ linearity fails loud.
    assert res.linearity_ok is False


def test_check_gain_plan_targets_measure_window_with_guard():
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(target_capture_dbfs=-10.5),
    )
    plan = res.gain_plan
    assert plan is not None
    # ≥6 dB digital guard on every solved gain.
    for gain in plan.gain_db.values():
        assert gain <= -6.0 + 1e-9
    assert plan.predicted_peak_dbfs <= -6.0 + 1e-9
    assert plan.snr_floor_ok is True


def test_check_gain_plan_uses_peak_referenced_level_not_ambient_subtracted():
    """Review finding: `_solve_gain_plan` uses a pilot level ABSOLUTELY
    (``k = level - gain_db``), not as a delta, so feeding it the
    ambient-subtracted `level_*_dbfs` (built for the linearity verdict)
    silently shifts the solved gain by however much ambient power was
    subtracted — moving `MeasurementPriors.target_capture_dbfs`'s documented
    capture-PEAK target hotter than intended. `_solve_gain_plan` must read
    the separate, non-ambient-subtracted `peak_*_dbfs` instead.

    This fixture's flat targets land comfortably away from the ≥6 dB
    digital-guard cap (confirmed below) — unlike
    `test_check_gain_plan_targets_measure_window_with_guard`, whose gains
    land exactly AT the cap either way, so a same-vs-swapped-consumer bug
    would have been invisible there (the guard clamps away the divergence).

    Read against `RoleGainSolve.flat_target_gain_db` since #1825: that field
    IS ``min(target_capture_dbfs - k, GAIN_MAX_DIGITAL_PEAK_DBFS)``, the
    quantity this test was written to pin, while `gain_db` is now that value
    after the SNR solve backs it off. The `k`-consumer promise is unchanged.
    """
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(target_capture_dbfs=-10.5),
    )
    plan = res.gain_plan
    assert plan is not None
    for role in ("woofer", "tweeter"):
        pilot = next(p for p in res.pilots if p.role == role)
        lo_seg = prog.segment(f"pilot_{role}_lo")
        hi_seg = prog.segment(f"pilot_{role}_hi")
        expected_k = (
            (pilot.peak_lo_dbfs - lo_seg.gain_db)
            + (pilot.peak_hi_dbfs - hi_seg.gain_db)
        ) / 2.0
        expected_gain = min(-10.5 - expected_k, GAIN_MAX_DIGITAL_PEAK_DBFS)
        # Confirm this fixture actually exercises the "away from the cap"
        # case: the unclamped value must already be quieter than the cap by
        # a real margin, or this assertion would pass for ANY k (correct or
        # buggy) once both clamp to the same -6 dB ceiling.
        assert expected_gain < GAIN_MAX_DIGITAL_PEAK_DBFS - 0.5
        solve = plan.role_solves[role]
        assert solve.flat_target_gain_db == pytest.approx(expected_gain, abs=1e-6)

        # And: the solved gain must NOT match what the ambient-subtracted
        # level would have produced (the pre-fix bug) — a >2 dB divergence
        # confirms the two consumers are genuinely decoupled, not
        # coincidentally equal on this fixture.
        buggy_k = (
            (pilot.level_lo_dbfs - lo_seg.gain_db)
            + (pilot.level_hi_dbfs - hi_seg.gain_db)
        ) / 2.0
        buggy_gain = min(-10.5 - buggy_k, GAIN_MAX_DIGITAL_PEAK_DBFS)
        assert abs(solve.flat_target_gain_db - buggy_gain) > 2.0


# --------------------------------------------------------------------------- #
# CHECK's ambient window CLIPS on a late capture, never SLIDES (issue #1818)
# --------------------------------------------------------------------------- #
#
# The shipped CHECK program is 12 s of ambient silence at [0, 12 s) followed
# IMMEDIATELY by the courtesy prelude — 0.6 s of -18 dBFS beeps at
# [12.0, 12.6) s (`courtesy_prelude_for_phase(journey.PHASE_CHECK)` is True.
# The name is qualified because that rule is asked of the SESSION phase — the
# `jasper.active_speaker.crossover_v2.journey` family — and not of
# `program.PROGRAM_PHASE_CHECK`, the stimulus phase this file imports. CHECK
# opens stage 1, so it is one of the phases that keeps the prelude). A
# window whose end is computed from the CLAMPED start walks forward onto those
# beeps on a capture that began late. Measured on that shipped geometry over a
# -70 dBFS floor: a 0.6 s late start read the window RMS 39.5 dB hot (-70.00
# -> -30.52 dBFS) and the worst framed band 21.8 dB hot (-74.65 -> -52.85),
# and by ~5.9 s late the window had slid onto the PILOTS (-23.18 dBFS), which
# flipped `snr_floor_ok` False and refused a perfectly quiet room.
#
# The fixtures below use a SHORT (2 s) ambient window for runtime; the
# mechanism they exercise is the beep adjacency, which is identical at either
# length because the composer butts the prelude against the window's end.

AMBIENT_FIXTURE_FLOOR_DBFS = -70.0


def _check_ambient_program(*, ambient_s: float = 2.0):
    """A CHECK program with the shipped beep-adjacency: courtesy prelude
    spliced in immediately after the ambient window."""
    return build_check_program(
        _check_roles(), ambient_s=ambient_s, pilot_duration_s=0.6,
        courtesy_prelude=True,
    )


def _ambient_fixture_capture(program, *, seed: int = 11) -> np.ndarray:
    """The program's own PCM (so the beeps are real, at their programmed
    level) over a known -70 dBFS room floor."""
    mono = render_program_pcm(program).sum(axis=1).astype(np.float64)
    floor = np.random.default_rng(seed).normal(
        0.0, 10 ** (AMBIENT_FIXTURE_FLOOR_DBFS / 20.0), mono.size
    )
    return mono + floor


def _rms_dbfs(samples: np.ndarray) -> float:
    return 20.0 * math.log10(max(float(np.sqrt(np.mean(samples ** 2))), 1e-12))


def test_check_ambient_window_clips_on_a_late_capture_never_slides_onto_the_beeps():
    """#1818: a late-started capture yields a SHORTER window, not one that has
    walked forward onto the courtesy beeps.

    Both halves matter. The window must be clipped (its length shrinks by
    exactly the lost head), and what it measures must still be the room —
    the -70 dBFS floor, not the -18 dBFS beep sitting immediately after the
    window's scheduled end. Computing ``end`` from the clamped start instead
    reads this same fixture ~40 dB hot, which poisons `_snr_floor_ok` AND the
    gain solve that reads the same report.
    """
    prog = _check_ambient_program()
    amb = prog.segment(AMBIENT_SEGMENT_ID)
    cap = _ambient_fixture_capture(prog)

    late = int(round(0.6 * SR))
    samples, report = _ambient_from_capture(cap[late:], SR, amb, -late)

    assert samples is not None
    # Clipped at the window's own scheduled end: shorter by exactly the head
    # the capture missed. A slid window would still be `amb.n_samples` long.
    assert samples.size == amb.n_samples - late
    # And it is still the ROOM. The beep would drag this up by tens of dB.
    assert _rms_dbfs(samples) == pytest.approx(AMBIENT_FIXTURE_FLOOR_DBFS, abs=1.5)
    worst = max(b["level_dbfs"] for b in report["bands"])
    assert worst < AMBIENT_FIXTURE_FLOOR_DBFS + 15.0


def test_check_ambient_window_is_unchanged_for_an_on_time_capture():
    """The #1818 fix is a no-op whenever the window's schedule position is
    non-negative — i.e. every ordinary capture, which leads the program.

    Pinned by reconstructing the window from its own schedule position and
    demanding bit equality of BOTH returned values, so a future edit to the
    slicing cannot quietly move the ordinary path while fixing the late one.
    """
    prog = _check_ambient_program()
    amb = prog.segment(AMBIENT_SEGMENT_ID)
    cap = np.concatenate([
        np.zeros(GLOBAL_OFFSET), _ambient_fixture_capture(prog),
    ])

    samples, report = _ambient_from_capture(cap, SR, amb, GLOBAL_OFFSET)

    begin = GLOBAL_OFFSET + amb.start_sample
    expected = cap[begin:begin + amb.n_samples]
    assert samples is not None
    assert np.array_equal(samples, expected)
    assert report == snr_policy.framed_ambient_band_report(expected, SR, percentile=95)


@pytest.mark.parametrize(
    "kept_samples_delta,expect_evidence", [(0, True), (-1, False)],
)
def test_check_ambient_min_usable_fraction_is_pinned_at_its_boundary(
    kept_samples_delta, expect_evidence,
):
    """`AMBIENT_MIN_USABLE_FRACTION` pinned AT its boundary, inclusive —
    CHECK's window now shares the pilot window's constant AND its policy.

    Exactly the shape of the sibling assertion on `_pilot_ambient_samples`
    (`test_crossover_v2_program_pilots.py`): the offset is supplied rather
    than recovered by correlation, so "exactly at the fraction is KEPT, one
    sample under is dropped" is an exact statement about the constant.
    """
    prog = _check_ambient_program()
    amb = prog.segment(AMBIENT_SEGMENT_ID)
    cap = _ambient_fixture_capture(prog)

    kept = int(AMBIENT_MIN_USABLE_FRACTION * amb.n_samples) + kept_samples_delta
    global_offset = -(amb.start_sample + amb.n_samples - kept)
    samples, report = _ambient_from_capture(cap, SR, amb, global_offset)

    if expect_evidence:
        assert samples is not None and samples.size == kept
        assert report["bands"]
    else:
        assert samples is None
        assert report["bands"] == []


def test_check_ambient_below_the_usable_fraction_degrades_to_disclosed_no_evidence():
    """Too little window left ⇒ the analysis says "no evidence" rather than
    estimating a floor from a couple of hundred samples.

    The degradation is fail-closed and DISCLOSED on both consumers of the
    report: `_snr_floor_ok` refuses (a missing floor is not a passing floor),
    and the gain solve reaches its `GAIN_BOUND_NO_AMBIENT_EVIDENCE` bound
    rather than solving against a fabricated number. This is the honest
    direction — the pre-#1818 code answered instead with a window that had
    slid onto the beeps and the pilots.
    """
    prog = _check_ambient_program()
    amb = prog.segment(AMBIENT_SEGMENT_ID)
    cap = _ambient_fixture_capture(prog)

    # Late by more than half the window ⇒ under the usable fraction.
    late = int(round(0.75 * amb.n_samples))
    samples, report = _ambient_from_capture(cap[late:], SR, amb, -late)

    assert samples is None
    assert report["bands"] == []
    assert _snr_floor_ok(report, -10.5) is False

    # End-to-end, through the real offset recovery: the whole CHECK analysis
    # reaches the disclosed no-evidence bound rather than a solved gain.
    res = analyze_program_capture(prog, cap[late:], SR, priors=MeasurementPriors())
    assert res.ambient_report["bands"] == []
    assert res.gain_plan is not None
    assert res.gain_plan.snr_floor_ok is False
    assert res.gain_plan.role_solves
    for solve in res.gain_plan.role_solves.values():
        assert solve.bound_by == GAIN_BOUND_NO_AMBIENT_EVIDENCE


# --------------------------------------------------------------------------- #
# MEASURE level solve — as quiet as the fit's SNR need allows (issue #1825)
# --------------------------------------------------------------------------- #
#
# Before #1825 the CHECK gain solve drove every driver's MEASURE sweep until
# its capture peak hit `DEFAULT_TARGET_CAPTURE_DBFS` (-10.5 dBFS) — the ADC's
# headroom, not the room's noise floor. Since MEASURE is the only phase whose
# level is solved (CHECK's pilots and every summed-sweep phase ride
# `BASE_STIMULUS_PEAK_DBFS` clamped by the driver cap), that made capture 2 of
# a v2 session structurally the loudest thing the household hears — the
# 2026-07-28 owner report. These fixtures drive `_solve_gain_plan` directly so
# `k`, the ambient report, and Fc are all controlled: the synthetic-capture
# fixtures above cannot express "a quiet room" and "a noisy room" as separate
# cases.
#
# Constraint 5 of the work order — per-ROLE gains may differ, per-REPEAT must
# not (bit-identical stimulus is the drift estimator's input) — is a composer
# property and is already pinned by
# `test_audio_measurement_program.py::test_measure_program_layout_is_n3_interleaved_repeats_bit_identical`,
# whose fixture plan already carries DIFFERENT woofer/tweeter gains.

# `DRIVER.peak_too_low_dbfs`, spelled out so a silent retune of the shared
# quality model shows up here as a failure rather than as a moved goalpost.
DRIVER_PEAK_TOO_LOW_DBFS = -45.0

_SOLVE_BANDS = (
    ("sub_bass", 20.0, 80.0),
    ("bass", 80.0, 160.0),
    ("upper_bass", 160.0, 350.0),
    ("transition", 350.0, 1000.0),
    ("mid", 1000.0, 4000.0),
    ("treble", 4000.0, 12000.0),
)


def _ambient(**levels: float) -> dict[str, object]:
    """An ambient report shaped like `snr_policy.framed_ambient_band_report`.

    Any band not named defaults to -100 dBFS (effectively silent), so a
    fixture states only the bands it cares about.
    """
    return {
        "schema_version": 1,
        "bands": [
            {
                "band_id": band_id,
                "band_hz": [lo, hi],
                "level_dbfs": levels.get(band_id, -100.0),
            }
            for band_id, lo, hi in _SOLVE_BANDS
        ],
    }


def _solve_pilots(program, *, k_db: dict[str, float]) -> list[PilotObservation]:
    """Pilots whose peak levels encode an exact per-role chain gain ``k``.

    `_solve_gain_plan` recovers ``k = peak - segment.gain_db`` from each
    pilot, so writing ``peak = gain_db + k`` inverts it exactly and lets a
    fixture name the chain gain it wants to test (a real sensitive tweeter
    behind a pad has a very different ``k`` from a woofer).
    """
    pilots: list[PilotObservation] = []
    for role, k in k_db.items():
        lo_seg = program.segment(f"pilot_{role}_lo")
        hi_seg = program.segment(f"pilot_{role}_hi")
        pilots.append(
            PilotObservation(
                role=role,
                level_lo_dbfs=lo_seg.gain_db + k,
                level_hi_dbfs=hi_seg.gain_db + k,
                programmed_delta_db=hi_seg.gain_db - lo_seg.gain_db,
                captured_delta_db=hi_seg.gain_db - lo_seg.gain_db,
                linearity_ok=True,
                channel_map_ok=True,
                peak_lo_dbfs=lo_seg.gain_db + k,
                peak_hi_dbfs=hi_seg.gain_db + k,
            )
        )
    return pilots


def _solve(
    ambient_report,
    *,
    k_db: dict[str, float] | None = None,
    fc_hz: float | None = 2000.0,
    pilot_levels_db: tuple[float, float] = (-10.0, 0.0),
    roles=None,
):
    program = build_check_program(
        roles or _check_roles(), ambient_s=2.0, pilot_duration_s=0.6,
        pilot_levels_db=pilot_levels_db,
    )
    pilots = _solve_pilots(
        program, k_db=k_db or {"woofer": 0.0, "tweeter": 0.0}
    )
    return _solve_gain_plan(
        program, pilots, ambient_report,
        MeasurementPriors(target_capture_dbfs=-10.5, crossover_fc_hz=fc_hz),
    )


def test_measure_level_solve_backs_off_in_a_quiet_room():
    """The headline behavior: a room quiet enough that the fit's SNR need is
    met well below the flat capture target gets a QUIETER MEASURE."""
    # Woofer band 150-1200 Hz, tweeter 2500-20000 Hz (`_check_roles`). Fc
    # 2000 Hz ⇒ overlap window [1000, 4000], so the woofer's `transition`
    # band and the tweeter's `mid` band both carry the alignment requirement.
    plan = _solve(_ambient(upper_bass=-62.0, transition=-64.0, mid=-70.0, treble=-78.0))
    for role in ("woofer", "tweeter"):
        solve = plan.role_solves[role]
        assert solve.bound_by == GAIN_BOUND_ROOM_SNR
        assert solve.gain_db < solve.flat_target_gain_db
        assert solve.reduction_db > 0.0
        # The solve landed exactly on "worst overlapping band + that band's
        # requirement" — a solve, not a fixed offset. (``k`` is 0 in this
        # fixture, so the digital gain IS the targeted capture peak.)
        assert solve.gain_db == pytest.approx(solve.required_capture_dbfs)
    # Both drivers still get a real signal: nothing near the capture floor.
    assert plan.predicted_peak_dbfs > DRIVER_PEAK_TOO_LOW_DBFS


def test_measure_level_solve_never_goes_louder_than_todays_flat_target():
    """The one-way guarantee: a room noisy enough that the SNR requirement
    wants MORE level than the flat target keeps today's behavior exactly,
    and says so (`bound_by == flat_target`)."""
    plan = _solve(_ambient(upper_bass=-20.0, transition=-22.0, mid=-24.0, treble=-26.0))
    for role in ("woofer", "tweeter"):
        solve = plan.role_solves[role]
        assert solve.bound_by == GAIN_BOUND_FLAT_TARGET
        assert solve.gain_db == pytest.approx(solve.flat_target_gain_db)
        assert solve.reduction_db == pytest.approx(0.0)


@pytest.mark.parametrize(
    "ambient_report",
    [
        pytest.param({}, id="empty-report"),
        pytest.param({"schema_version": 1, "bands": []}, id="no-bands"),
        pytest.param(
            {"schema_version": 1, "bands": [{"band_id": "mid", "level_dbfs": -70.0}]},
            id="rows-without-edges",
        ),
    ],
)
def test_measure_level_solve_falls_back_with_a_disclosed_reason(ambient_report):
    """No usable ambient evidence ⇒ today's flat target, WITH a reason. Never
    a silent guess, and never a refusal: this is a comfort optimization, so
    losing the evidence must cost the session nothing but the reduction."""
    plan = _solve(ambient_report)
    for role in ("woofer", "tweeter"):
        solve = plan.role_solves[role]
        assert solve.bound_by == GAIN_BOUND_NO_AMBIENT_EVIDENCE
        assert solve.gain_db == pytest.approx(solve.flat_target_gain_db)
        # A missing number is None, never a zero that reads as evidence.
        assert solve.ambient_dbfs is None
        assert solve.required_snr_db is None
        assert solve.required_capture_dbfs is None


def test_measure_level_solve_falls_back_for_a_band_the_report_cannot_reach():
    """The reachable no-evidence case: a driver measured entirely above the
    ambient table's top band (`CROSSOVER_SNR_BANDS_HZ` stops at 12 kHz) has
    no ambient row to solve against, so it keeps the flat target and says so
    — while its sibling, which does have evidence, still backs off."""
    roles = [
        RoleBand("woofer", 0, FrequencyBand(150.0, 1200.0)),
        RoleBand("tweeter", 1, FrequencyBand(13_000.0, 20_000.0)),
    ]
    plan = _solve(_ambient(upper_bass=-60.0, transition=-62.0), roles=roles)
    assert plan.role_solves["tweeter"].bound_by == GAIN_BOUND_NO_AMBIENT_EVIDENCE
    assert plan.role_solves["tweeter"].reduction_db == pytest.approx(0.0)
    assert plan.role_solves["woofer"].bound_by == GAIN_BOUND_ROOM_SNR
    assert plan.role_solves["woofer"].reduction_db > 0.0


def test_ambient_rows_in_band_skips_rows_it_cannot_read():
    """`_ambient_rows_in_band` is fed by more than one report producer (see
    `snr_policy.unwrap_noise_report`'s legacy bare-band shape), so a row it
    cannot read must cost it that row's evidence — never raise inside CHECK's
    accept path."""
    rows = program_analysis._ambient_rows_in_band(
        (150.0, 1200.0),
        [
            {"band_id": "upper_bass", "band_hz": [160.0, 350.0], "level_dbfs": -60.0},
            "not-a-mapping",
            {"band_id": "no-edges", "level_dbfs": -10.0},
            {"band_id": "bad-level", "band_hz": [160.0, 350.0], "level_dbfs": "loud"},
            {"band_id": "nan", "band_hz": [160.0, 350.0], "level_dbfs": float("nan")},
            {"band_id": "treble", "band_hz": [4000.0, 12000.0], "level_dbfs": -70.0},
        ],
    )
    assert rows == [(160.0, 350.0, -60.0)]


def test_snr_floor_ok_skips_rows_it_cannot_read():
    """#1831: `_snr_floor_ok` sits one function below `_ambient_rows_in_band`
    (previous test) and reads the exact same ``level_dbfs`` shape, but until
    this fix its ``float(b["level_dbfs"])`` was unguarded — a malformed row
    raised ``ValueError`` instead of costing this gate that row's evidence.
    Unreachable from today's in-process producer alone (which always writes
    numeric levels), but the asymmetry with its sibling bites a replayed or
    legacy-artifact ambient report. Same fixture shape as the sibling test,
    with a non-mapping row, a missing key, a non-numeric value, and a NaN
    value all present alongside two genuinely-readable rows.

    Numbers matter here, not just "did it raise": a broken fix that silently
    coerced the unreadable rows to ``0.0`` dBFS (louder than every real row
    here) would flip this from PASS (``True``) to FAIL (``False``) — so this
    also pins that unreadable rows are skipped, not defaulted.
    """
    report = {
        "bands": [
            {"level_dbfs": -60.0},
            "not-a-mapping",
            {"no_level_key": True},
            {"level_dbfs": "loud"},
            {"level_dbfs": float("nan")},
            {"level_dbfs": -90.0},
        ],
    }
    # Worst READABLE level is -60.0 (DRIVER.snr_ok_db == 25.0 dB):
    # -10.5 - (-60.0) == 49.5 >= 25.0.
    assert program_analysis._snr_floor_ok(report, target_capture_dbfs=-10.5) is True


def test_snr_floor_ok_false_when_no_row_is_readable():
    """#1831 fail-closed extension: `_snr_floor_ok` already treats an EMPTY
    ``bands`` list as "no evidence" (``False``); a ``bands`` list present but
    every row unreadable must resolve the same way — never claim the SNR
    floor is satisfied from zero real evidence."""
    report = {"bands": [{"level_dbfs": "loud"}, {"no_level_key": True}, "garbage"]}
    assert program_analysis._snr_floor_ok(report, target_capture_dbfs=-10.5) is False


def test_measure_level_solve_refuses_a_degenerate_ambient_report(caplog):
    """D2 (#1838): the capture floor winning is a REFUSAL, not a level.

    A near-silent ambient report lets the SNR math propose an inaudible
    sweep. `DRIVER.peak_too_low_dbfs` catches that — and until #1838 the
    solve then SHIPPED the floor, which is exactly what killed session
    cap_-Us10xORVNlFa_dgi-sP7g: both roles solved to a -45 dBFS capture
    target, 34 dB below flat, and the guards that should have caught it
    (pilot SNR, sweep locate) were computed from the same collapsed ambient
    report and could not bound it by construction. Every arm resolving below
    a level too faint to measure means the ambient evidence is not solvable,
    so the honest answer is today's proven flat target plus a named reason.
    """
    with caplog.at_level(logging.WARNING, logger="jasper.audio_measurement.program_analysis"):
        plan = _solve(_ambient())  # every band at -100 dBFS
    for role in ("woofer", "tweeter"):
        solve = plan.role_solves[role]
        assert solve.bound_by == GAIN_BOUND_DEGENERATE_AMBIENT
        # The refusal keeps today's level — never quieter, never the floor.
        assert solve.gain_db == pytest.approx(solve.flat_target_gain_db)
        assert solve.reduction_db == pytest.approx(0.0)
        assert solve.gain_db > DRIVER_PEAK_TOO_LOW_DBFS
        # `capture_floor` survives in the vocabulary — solves persisted
        # before #1838 carry it — but the solver never emits it again.
        assert solve.bound_by != GAIN_BOUND_CAPTURE_FLOOR
    assert GAIN_BOUND_CAPTURE_FLOOR in GAIN_BOUNDS
    # And it is not silent: one structured WARNING per refused role, carrying
    # the arms it refused so the refusal is diagnosable from the journal.
    refusals = [
        r for r in caplog.records
        if "event=program_analysis.measure_level_solve_refused" in r.getMessage()
    ]
    assert len(refusals) == 2
    assert all("reason=degenerate_ambient" in r.getMessage() for r in refusals)
    assert all("required_capture_dbfs=" in r.getMessage() for r in refusals)


def test_measure_level_solve_reports_the_room_demand_even_when_it_is_refused():
    """`required_capture_dbfs` is the ROOM-SNR demand specifically, not the
    capture peak finally aimed at. Pinned because the two diverge exactly when
    another arm wins, and a reader of the disclosure event must not mistake
    one for the other — `bound_by` is what says which number bound the level.

    Since #1838 the demand is a quadruple, not a triple: ambient + required
    SNR + the stimulus crest factor that converts a band-RMS demand into the
    capture-PEAK units `k_db` and the flat target are expressed in.
    """
    # Near-silent room ⇒ the solve is refused, and the room demand it
    # rejected stays visible on the disclosure.
    plan = _solve(_ambient())
    for solve in plan.role_solves.values():
        assert solve.bound_by == GAIN_BOUND_DEGENERATE_AMBIENT
        assert solve.crest_factor_db is not None
        assert solve.required_capture_dbfs == pytest.approx(
            solve.ambient_dbfs + solve.required_snr_db + solve.crest_factor_db
        )
        # The refused demand really was below the capture floor — that is
        # what made it degenerate.
        assert solve.required_capture_dbfs < DRIVER_PEAK_TOO_LOW_DBFS


def test_measure_level_solve_respects_the_pilot_snr_floor():
    """Constraint 3: MEASURE opens on a two-level pilot pair, and the quiet
    side sits the pair's delta below the sweep gain. Backing a driver off far
    enough to sink its own pilots under `PILOT_MIN_SNR_DB` (issue #1816's
    guard) would trade a loud capture for a failing one.

    At the shipped 10 dB pilot delta the room-SNR requirement is the stricter
    of the two, so this widens the pair to 30 dB to make the pilot floor bind
    — pinning that the guard is live rather than accidentally satisfied.
    """
    ambient = _ambient(upper_bass=-62.0, transition=-64.0, mid=-70.0, treble=-78.0)
    shipped = _solve(ambient, pilot_levels_db=(-10.0, 0.0))
    widened = _solve(ambient, pilot_levels_db=(-30.0, 0.0))
    for role in ("woofer", "tweeter"):
        assert shipped.role_solves[role].bound_by == GAIN_BOUND_ROOM_SNR
        solve = widened.role_solves[role]
        assert solve.bound_by == GAIN_BOUND_PILOT_SNR
        # The floor is exactly "the pair's worst ambient, plus the delta the
        # quiet pilot loses, plus the guard's own floor and this solve's
        # margin" — computed against the role's own band.
        worst_ambient = max(
            band["level_dbfs"]
            for band in ambient["bands"]
            if band["band_hz"][1] > solve.band_hz[0]
            and band["band_hz"][0] < solve.band_hz[1]
        )
        assert solve.gain_db == pytest.approx(
            worst_ambient + 30.0 + PILOT_MIN_SNR_DB + MEASURE_SNR_SOLVE_MARGIN_DB
            # D6 (#1838): the pilot floor is peak-expressed too. A pilot is a
            # swept sine over the role's WHOLE band, so its occupancy term is
            # 1 and only the peak-to-RMS constant carries.
            + SWEEP_PEAK_TO_RMS_DB
        )
        # …and still quieter than today, never louder.
        assert solve.gain_db < solve.flat_target_gain_db


def test_measure_level_solve_demands_alignment_snr_inside_the_overlap_band():
    """The requirement is the shipped split-SNR policy, not one number: a
    band inside the crossover overlap feeds MEASURE's delay/polarity estimate
    (an alignment-class decision, `DRIVER.alignment_snr_ok_db`), everything
    else feeds magnitude/trim decisions (`DRIVER.snr_ok_db`)."""
    # One loud band well OUTSIDE the Fc=2000 overlap window [1000, 4000] and
    # inside the woofer's own 150-1200 Hz band: `upper_bass` (160-350 Hz).
    outside = _solve(_ambient(upper_bass=-60.0), fc_hz=2000.0).role_solves["woofer"]
    assert outside.required_snr_db == pytest.approx(25.0 + MEASURE_SNR_SOLVE_MARGIN_DB)

    # Drop Fc to 320 Hz ⇒ overlap [160, 640], which now covers that same
    # band, so the SAME room demands 10 dB more and solves 10 dB louder.
    inside = _solve(_ambient(upper_bass=-60.0), fc_hz=320.0).role_solves["woofer"]
    assert inside.required_snr_db == pytest.approx(35.0 + MEASURE_SNR_SOLVE_MARGIN_DB)
    assert inside.gain_db == pytest.approx(outside.gain_db + 10.0)


def test_measure_level_solve_without_fc_takes_the_conservative_requirement():
    """No Fc prior ⇒ the alignment requirement everywhere. Withholding Fc must
    not buy a quieter measurement on a technicality."""
    known = _solve(_ambient(upper_bass=-60.0), fc_hz=2000.0).role_solves["woofer"]
    unknown = _solve(_ambient(upper_bass=-60.0), fc_hz=None).role_solves["woofer"]
    assert unknown.required_snr_db == pytest.approx(35.0 + MEASURE_SNR_SOLVE_MARGIN_DB)
    assert unknown.gain_db > known.gain_db


def test_measure_level_solve_is_per_driver_on_the_jts3_shape():
    """Regression anchor for the field report (JTS3, 2026-07-28).

    The rig: a direct-driven B&C DE250 compression tweeter (108.5 dB/W behind
    a 14.4 dB physical pad) beside a far less sensitive woofer, in a room
    whose noise is LF-dominated as every room's is. Two independent things
    then differ per driver, and the solve must honour both:

      * ``k`` — the tweeter's chain gain is far higher, which the PRE-#1825
        solve already handled (it aimed both at one capture peak);
      * the ROOM in each driver's own band — which it did not. The tweeter is
        measured from 2.5 kHz up, where this room is ~16 dB quieter than the
        woofer's 150-1200 Hz span, so the tweeter genuinely needs less drive.

    The asserted outcome is the MECHANISM (the tweeter gets the larger
    reduction, because its band is quieter), not a specific dB figure. The
    issue's "~11.5 dB hotter" reading came from `branch_level_match`'s
    `level_w`/`level_t`, which are DECONVOLVED transfer-function levels —
    `program.segment_stimulus` regenerates the reference at the segment's own
    `gain_db` and `deconv.regularized_deconvolution_full`'s epsilon is
    relative to ``|X|**2``, so the drive cancels mathematically exactly. That
    11.5 dB is a sensitivity delta, not a drive delta, and cannot be this
    change's acceptance target.

    The ambient rows are the MEASURED jts3 room (#1838's forensics: Parseval
    ground truth recomputed from the retained CHECK WAV of session
    cap_-Us10xORVNlFa_dgi-sP7g, 2026-07-28 evening) rather than the
    illustrative levels this fixture carried before. Same LF-dominated shape,
    real numbers — and the room that broke the solve is the right room to
    anchor the corrected one against.
    """
    ambient = _ambient(
        sub_bass=-57.86, bass=-69.35, upper_bass=-68.13,
        transition=-71.24, mid=-81.51, treble=-90.99,
    )
    plan = _solve(ambient, k_db={"woofer": -6.0, "tweeter": 12.0}, fc_hz=2000.0)
    woofer, tweeter = plan.role_solves["woofer"], plan.role_solves["tweeter"]

    # Both got quieter than today, and neither is at the flat target.
    for solve in (woofer, tweeter):
        assert solve.bound_by == GAIN_BOUND_ROOM_SNR
        assert solve.reduction_db > 0.0

    # The tweeter's band is the quieter one, so it backs off further — the
    # per-driver behavior the flat target could not express.
    assert tweeter.ambient_dbfs < woofer.ambient_dbfs
    assert tweeter.reduction_db > woofer.reduction_db

    # And the two roles' gains genuinely differ (a shared reduction would
    # have preserved the pre-existing skew instead of solving it away).
    assert plan.gain_db["woofer"] != pytest.approx(plan.gain_db["tweeter"])


def test_sweep_band_crest_factor_matches_the_rendered_sweep():
    """D6 (#1838): the crest law is measured against the real stimulus, not
    asserted.

    `sweep_band_crest_factor_db` claims a swept sine's peak sits
    ``3.01 + 10*log10(ln(f2/f1)/ln(hi/lo))`` dB above its RMS inside
    ``[lo, hi]``. This renders both production MEASURE sweeps through
    `program.segment_stimulus`'s own generator and measures that distance
    directly, Parseval-style.

    The measurement deliberately uses a RECTANGULAR window rather than
    `snr_policy.band_levels_dbfs`. That estimator's DEFAULT Hann window is
    correct for the stationary ambient it reads, but a sweep is non-stationary:
    the window attenuates whichever frequencies happen to occur near the ends
    of the capture, which on a 4 s woofer sweep misreports the band split by
    tens of dB. Reading the crest through it would measure the window, not
    the sweep.
    """
    from jasper.audio_measurement.sweep import synchronized_swept_sine

    def measured_crest_db(x, sample_rate, lo, hi):
        n = x.size
        power = np.abs(np.fft.rfft(x)) ** 2 * 2.0
        power[0] /= 2.0
        if n % 2 == 0:
            power[-1] /= 2.0
        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        mask = (freqs >= lo) & (freqs < hi)
        band_dbfs = 10.0 * math.log10(float(np.sum(power[mask])) / (n * n))
        peak_dbfs = 20.0 * math.log10(float(np.max(np.abs(x))))
        return peak_dbfs - band_dbfs

    sample_rate = 48000
    for (f1, f2), duration_s in (((150.0, 2000.0), 4.0), ((1500.0, 23000.0), 3.0)):
        stimulus, _ = synchronized_swept_sine(
            f1=f1, f2=f2, duration_approx_s=duration_s,
            sample_rate=sample_rate, amplitude_dbfs=0.0,
        )
        stimulus = np.asarray(stimulus, dtype=np.float64)
        # The whole sweep band: pure peak-to-RMS, no occupancy term.
        assert measured_crest_db(stimulus, sample_rate, f1, f2) == pytest.approx(
            SWEEP_PEAK_TO_RMS_DB, abs=0.1
        )
        for _band_id, lo, hi in _SOLVE_BANDS:
            lo_clipped, hi_clipped = max(lo, f1), min(hi, f2)
            if hi_clipped <= lo_clipped:
                continue
            predicted = program_analysis.sweep_band_crest_factor_db(
                (f1, f2), (lo, hi)
            )
            measured = measured_crest_db(
                stimulus, sample_rate, lo_clipped, hi_clipped
            )
            # 0.7 dB covers the one 10 Hz-wide clipped sliver in the set,
            # where FFT bin granularity (not the law) is the limit; every
            # other row agrees to within 0.05 dB.
            assert measured == pytest.approx(predicted, abs=0.7), (f1, f2, lo, hi)


def test_measure_level_solve_lands_in_a_sane_regime_on_the_field_session():
    """The #1838 acceptance arithmetic, pinned: the corrected solve on the
    REAL inputs of session cap_-Us10xORVNlFa_dgi-sP7g (JTS3, 2026-07-28
    evening, build 790c10864) lands between this morning's flat levels and
    the levels the broken solve actually shipped.

    What the session shipped: `band_levels_dbfs` reported that room 25-46 dB
    quieter than it was, both roles hit the capture floor, and MEASURE played
    33-34 dB below flat — inaudible to its own pilots and its own sweep
    locator. This asserts the same room, read correctly, produces a REAL but
    modest backoff instead, and that both roles stay well clear of the floor.
    """
    ambient = _ambient(  # Parseval ground truth from the retained CHECK WAV
        sub_bass=-57.86, bass=-69.35, upper_bass=-68.13,
        transition=-71.24, mid=-81.51, treble=-90.99,
    )
    plan = _solve(ambient, k_db={"woofer": -6.0, "tweeter": 12.0}, fc_hz=2000.0)
    shipped_reduction_db = {"woofer": 33.0, "tweeter": 34.5}
    for role, solve in plan.role_solves.items():
        # A solved level, from real evidence — not a refusal, not the floor.
        assert solve.bound_by == GAIN_BOUND_ROOM_SNR
        assert 0.0 < solve.reduction_db < shipped_reduction_db[role]
        # The capture this asks for is comfortably above "too low to trust",
        # which is the property the shipped solve lost.
        assert solve.required_capture_dbfs > DRIVER_PEAK_TOO_LOW_DBFS + 10.0
    # The woofer's band is the noisy one, so it barely moves; the tweeter's
    # is ~12 dB quieter and carries the 41 dB alignment demand, so it moves
    # further. Both directions are the mechanism, not a tuned constant.
    assert plan.role_solves["woofer"].reduction_db < 12.0
    assert plan.role_solves["tweeter"].reduction_db > 15.0


def test_measure_level_solve_leaves_the_snr_floor_gate_untouched():
    """`snr_floor_ok` is the room-QUALITY gate (asked of the reference target
    against the whole ambient report, sub-bass included). The solve answers a
    different question, and must not move which sessions CHECK accepts."""
    quiet = _ambient(upper_bass=-62.0, transition=-64.0, mid=-70.0, treble=-78.0)
    noisy = _ambient(sub_bass=-20.0)
    assert _solve(quiet).snr_floor_ok is True
    assert _solve(noisy).snr_floor_ok is False
    # …even though sub-bass (20-80 Hz) overlaps NEITHER driver's measurement
    # band, so it never entered a per-role solve.
    for solve in _solve(noisy).role_solves.values():
        assert solve.ambient_dbfs is not None
        assert solve.ambient_dbfs < -20.0


def test_measure_level_solve_publishes_a_json_safe_disclosure():
    """`RoleGainSolve.to_dict` is what lands in the session's check.json — it
    must round-trip through JSON (the evidence store re-opens artifacts)."""
    plan = _solve(_ambient(upper_bass=-62.0, mid=-70.0))
    for role, solve in plan.role_solves.items():
        payload = json.loads(json.dumps(solve.to_dict()))
        assert payload["role"] == role
        assert payload["bound_by"] in GAIN_BOUNDS
        assert payload["reduction_db"] >= 0.0
        assert payload["band_hz"] is not None


def test_check_ambient_report_present():
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.ambient_report is not None
    assert res.ambient_report["bands"]


# --------------------------------------------------------------------------- #
# CHECK linearity — band-relative, ambient-compensated (2026-07-20 fix)
# --------------------------------------------------------------------------- #
#
# Real hardware (jts3, 2026-07-20): a Dayton iMM-6C and a UMIK-2 capture, same
# room/placement, both failed `agc_behavioral_fail` at CHECK — the OLD
# full-band-PEAK linearity estimate let continuous LF room rumble ~30 dB
# above the tweeter-band ambient inflate the quiet woofer pilot's peak and
# compress the captured 10 dB delta below the 0.5 dB tolerance, even though
# both mics agreed the driver was linear once measured in its own band with
# RMS (9.8-10.0 dB on both). Same bug class the channel-map discriminator was
# fixed for in #1594 (gotcha #6); this is its un-fixed sibling.


def _check_rumble_capture(rumble_hz: tuple[float, ...], rumble_amp: float, *, seed: int):
    """`_check_capture`'s clean signal plus continuous rumble tones present
    across the WHOLE capture (ambient window and every pilot window alike —
    a real room's noise floor doesn't know when the pilot is playing)."""
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    pcm = render_program_pcm(prog)
    woofer_ir = _band_impulse(200, 150.0, 1200.0, 1.0)
    tweeter_ir = _band_impulse(225, 2500.0, 20000.0, 0.7)
    mono = (
        fftconvolve(pcm[:, 0], woofer_ir)[: pcm.shape[0]]
        + fftconvolve(pcm[:, 1], tweeter_ir)[: pcm.shape[0]]
    )
    cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])
    t = np.arange(cap.size) / SR
    rumble = rumble_amp * sum(np.sin(2 * np.pi * f * t) for f in rumble_hz)
    return prog, cap + rumble + np.random.default_rng(seed).normal(0.0, 3e-5, cap.size)


def _old_peak_delta(prog, capture, role: str) -> float:
    """The OLD (pre-fix) full-band-peak linearity delta, on the SAME located
    windows the new estimator uses — a direct old-vs-new comparison."""
    global_offset, _first, stimuli, _amb = _global_offset(prog, capture, SR)
    locations = _locate_segments(prog, capture, SR, global_offset, stimuli)
    by_id = {loc.segment_id: loc for loc in locations}
    lo_seg = prog.segment(f"pilot_{role}_lo")
    hi_seg = prog.segment(f"pilot_{role}_hi")
    lo_loc = by_id[f"pilot_{role}_lo"]
    hi_loc = by_id[f"pilot_{role}_hi"]
    lo_samples = capture[lo_loc.located_start:lo_loc.located_start + lo_seg.n_samples]
    hi_samples = capture[hi_loc.located_start:hi_loc.located_start + hi_seg.n_samples]
    return _peak_dbfs(hi_samples) - _peak_dbfs(lo_samples)


def test_check_linearity_survives_strong_in_band_room_rumble():
    """Rumble INSIDE the woofer's own declared [150, 1200] Hz band, strong
    enough to fail the OLD full-band-peak estimate (asserted inline) — the
    NEW band-relative, ambient-subtracted estimate is untouched, and
    genuinely trustworthy (``snr_valid`` True), not merely excused."""
    prog, cap = _check_rumble_capture((300.0, 500.0, 800.0), 0.008, seed=21)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.linearity_ok is True
    woofer_pilot = next(p for p in res.pilots if p.role == "woofer")
    assert woofer_pilot.snr_valid is True
    assert woofer_pilot.captured_delta_db == pytest.approx(10.0, abs=0.5)
    assert abs(_old_peak_delta(prog, cap, "woofer") - 10.0) > 0.5


def test_check_linearity_survives_strong_out_of_band_room_rumble():
    """Same shape, rumble entirely OUTSIDE any declared pilot band (below the
    woofer's own [150, 1200] Hz — infra-bass HVAC/traffic territory). A
    full-band PEAK estimate has no notion of "band" at all, so out-of-band
    energy corrupts it just as badly as in-band rumble; the new band-relative
    estimate is untouched."""
    prog, cap = _check_rumble_capture((40.0, 70.0, 110.0), 0.05, seed=22)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.linearity_ok is True
    for pilot in res.pilots:
        assert pilot.snr_valid is True
    assert abs(_old_peak_delta(prog, cap, "woofer") - 10.0) > 0.5


def test_check_linearity_fails_under_slow_agc_gain_ride():
    """The gate must keep its teeth: a SLOW multi-dB gain envelope ridden
    across the WHOLE lo->hi pilot span (a realistic browser-AGC shape, not
    just a step confined to the hi pilot) must still fail linearity, with a
    genuinely trustworthy SNR — this is a real behavioral failure, not an
    SNR excuse."""
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    lo_seg = prog.segment("pilot_woofer_lo")
    hi_seg = prog.segment("pilot_woofer_hi")
    a = 500 + lo_seg.start_sample
    b = 500 + hi_seg.start_sample + hi_seg.n_samples
    depth_db = 6.0
    ramp_db = np.linspace(0.0, -depth_db, b - a)
    cap[a:b] *= 10.0 ** (ramp_db / 20.0)
    cap[b:] *= 10.0 ** (-depth_db / 20.0)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    woofer_pilot = next(p for p in res.pilots if p.role == "woofer")
    assert woofer_pilot.snr_valid is True
    assert res.linearity_ok is False


def test_check_linearity_fails_under_classic_agc_step_between_pilots():
    """The classic AGC-settle shape, alongside the slow-ramp case above: a
    STEP confined to the gap before the hi pilot (AGC settles fully before
    hi starts, rather than still transitioning during either segment —
    `_check_capture(compress_hi=True)`, the same fixture
    `test_check_linearity_fails_under_simulated_agc` uses) must still fail
    linearity, with a genuinely trustworthy SNR — not a low-SNR excuse."""
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog, compress_hi=True)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    woofer_pilot = next(p for p in res.pilots if p.role == "woofer")
    assert woofer_pilot.snr_valid is True
    assert res.linearity_ok is False


def test_pilot_min_snr_db_matches_its_own_derivation():
    """Pins `PILOT_MIN_SNR_DB` to its documented derivation (the comment
    above the constant in program_analysis.py) so a future edit to
    `AMBIENT_NONSTATIONARITY_DB` / `LINEARITY_SNR_BIAS_BUDGET_FRACTION` /
    `LINEARITY_TOLERANCE_DB` can't silently drift the derived floor without
    this test failing — the formula is recomputed independently here, not
    imported from the module's private intermediate variables."""
    k = 10.0 ** (AMBIENT_NONSTATIONARITY_DB / 10.0)
    snr_linear_min = (10.0 / math.log(10.0)) * (k - 1.0) / (
        LINEARITY_TOLERANCE_DB * LINEARITY_SNR_BIAS_BUDGET_FRACTION
    )
    expected = 10.0 * math.log10(snr_linear_min)
    assert PILOT_MIN_SNR_DB == pytest.approx(expected, abs=1e-9)


def test_check_low_snr_quiet_pilot_routes_to_snr_floor_not_linearity_fail():
    """When the quiet (lo) pilot's own in-band SNR is too low to trust the
    ambient-subtracted estimate, the verdict must NOT be a linearity
    FAILURE — while ``snr_valid``/``pilot_snr_ok`` flags the low-confidence
    evidence so the conductor can route to the honest room/positioning
    reason (``REASON_SNR_FLOOR``), never blaming the phone's AGC
    (``crossover_v2_flow._consume_check``).

    D7 (#1838): not a FAILURE, and not a PASS either — ``linearity_ok`` is
    ``None``. It was forced ``True`` until then, which is how session
    cap_-Us10xORVNlFa_dgi-sP7g published ``linearity_ok=true`` beside a
    captured delta of -60.9 dB against a programmed 10.0 dB. The aggregate
    follows the same rule: one unknown role makes the capture's verdict
    unknown, because "the role we could read was fine" is a different claim
    from "the pilots were linear".
    """
    # Strong in-woofer-band rumble, loud enough to bury the QUIET (-10 dB)
    # woofer pilot's own in-band power near the ambient floor. The tweeter's
    # disjoint band is unaffected — only the woofer's evidence is untrusted.
    prog, cap = _check_rumble_capture((300.0, 500.0, 800.0), 0.02, seed=23)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    woofer_pilot = next(p for p in res.pilots if p.role == "woofer")
    tweeter_pilot = next(p for p in res.pilots if p.role == "tweeter")
    assert woofer_pilot.snr_valid is False
    assert woofer_pilot.linearity_ok is None  # unknown — never a false FAILURE
    assert tweeter_pilot.snr_valid is True
    assert tweeter_pilot.linearity_ok is True
    assert res.pilot_snr_ok is False
    assert res.linearity_ok is None


def test_pilot_linearity_aggregate_is_tri_state():
    """D7 (#1838): FAILURE wins, then UNKNOWN, then PASS.

    Pinned directly because the reduction used to be ``all(...)``, and
    Python folds ``None`` to False there — so the moment an unreadable pilot
    started reporting ``None`` instead of ``True``, a plain ``all()`` would
    have turned every unknown into a linearity FAILURE and produced exactly
    the mic accusation the low-SNR routing exists to prevent.
    """
    def _pilot(role, linearity_ok):
        return PilotObservation(
            role=role, level_lo_dbfs=-30.0, level_hi_dbfs=-20.0,
            programmed_delta_db=10.0, captured_delta_db=10.0,
            linearity_ok=linearity_ok, channel_map_ok=True,
        )

    aggregate = program_analysis._aggregate_linearity_ok
    assert aggregate([]) is None
    assert aggregate([_pilot("w", True), _pilot("t", True)]) is True
    assert aggregate([_pilot("w", True), _pilot("t", None)]) is None
    assert aggregate([_pilot("w", None), _pilot("t", None)]) is None
    # A real failure still outranks an unknown — an unreadable sibling role
    # must not launder a driver that genuinely failed.
    assert aggregate([_pilot("w", False), _pilot("t", None)]) is False
    assert aggregate([_pilot("w", False), _pilot("t", True)]) is False


# --------------------------------------------------------------------------- #
# CHECK channel map — band-relative discriminator (Fix 1, W6.4)
# --------------------------------------------------------------------------- #


def test_channel_map_exclusive_pieces_subtract_overlap():
    """Direct unit coverage for `_band_exclusive_pieces` (production helper):
    two drivers' declared bands legitimately overlap around the crossover
    point, so the CROSS test only looks at the part of the OTHER role's band
    that this role's own band does NOT already cover."""
    # Overlapping bands (MEASURE-style): only the non-overlapping remainder
    # of `other` survives.
    assert _band_exclusive_pieces((300.0, 20000.0), (150.0, 6000.0)) == [(6000.0, 20000.0)]
    assert _band_exclusive_pieces((150.0, 6000.0), (300.0, 20000.0)) == [(150.0, 300.0)]
    # Disjoint bands: nothing to subtract, `other` passes through whole.
    assert _band_exclusive_pieces((2500.0, 20000.0), (150.0, 1200.0)) == [(2500.0, 20000.0)]
    # `other` fully inside `own`: nothing left of `other` at all.
    assert _band_exclusive_pieces((1000.0, 2000.0), (150.0, 6000.0)) == []


def test_channel_map_survives_concurrent_room_rumble():
    """Reproduces the W6 run-5 hardware shape (2026-07-18/19, jts3): a
    cap-clamped, genuinely-quiet tweeter pilot (~25 dB of real TARGET rise
    over its own ambient — not the ~50-70 dB the other CHECK fixtures use)
    plays alongside a strong, CONTINUOUS low-frequency room rumble present
    for the WHOLE capture — the leading ambient window AND every pilot
    window alike, not a burst timed to coincide with the tweeter pilot (a
    real room's noise floor doesn't know when the tweeter is playing).

    Under the OLD total-in-band-energy-FRACTION test (reimplemented inline
    below, exactly as `_channel_map_ok` computed it pre-Fix-1), the rumble's
    energy in the (unrelated) woofer band dominates the tweeter pilot
    window's TOTAL spectral energy, driving the in-band fraction to ~0.12 —
    comfortably under the old >0.5 threshold, so the old code would veto the
    tweeter's channel-map verdict even though the tweeter played correctly.
    This was the run-5 bug. The NEW band-relative test isn't fooled: the
    rumble is equally present in the long ambient window, so it contributes
    ~0 dB of CROSS rise regardless of which band it lives in."""
    roles = _check_roles()
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)

    # Cap-clamped tweeter: the pilot plays, just quietly (unlike the ~0.7-1.0
    # unit-gain drivers used elsewhere in this file).
    tweeter_gain = 0.05
    mono = pcm[:, 0] + tweeter_gain * pcm[:, 1]
    cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])

    # Strong, continuous LF room rumble (three low tones, present across the
    # entire capture) plus a modest broadband noise floor.
    t = np.arange(cap.size) / SR
    rumble = 0.02 * (
        np.sin(2 * np.pi * 40.0 * t) + np.sin(2 * np.pi * 70.0 * t) + np.sin(2 * np.pi * 110.0 * t)
    )
    full_cap = cap + rumble + np.random.default_rng(30).normal(0.0, 3e-4, cap.size)

    res = analyze_program_capture(chk, full_cap, SR, priors=MeasurementPriors())
    assert res.channel_map_ok is True
    for pilot in res.pilots:
        assert pilot.channel_map_ok is True

    # OLD math, reimplemented inline: >50% of the tweeter pilot's TOTAL
    # spectral energy must land in its own declared band. It does not.
    global_offset, _first, stimuli, _amb = _global_offset(chk, full_cap, SR)
    locations = _locate_segments(chk, full_cap, SR, global_offset, stimuli)
    by_id = {loc.segment_id: loc for loc in locations}
    hi_seg = chk.segment("pilot_tweeter_hi")
    hi_loc = by_id["pilot_tweeter_hi"]
    hi_samples = full_cap[hi_loc.located_start:hi_loc.located_start + hi_seg.n_samples]
    window = np.hanning(hi_samples.size)
    spectrum = np.abs(np.fft.rfft(hi_samples * window)) ** 2
    freqs = np.fft.rfftfreq(hi_samples.size, 1.0 / SR)
    in_band = (freqs >= hi_seg.f1_hz) & (freqs <= hi_seg.f2_hz)
    old_fraction = float(np.sum(spectrum[in_band])) / float(np.sum(spectrum))
    assert old_fraction < 0.5


def test_channel_map_fails_when_one_driver_never_played():
    """The realistic miswire — one driver plays, the other is silent.

    This is the case that protects a household, and it is the one the
    band-relative rise test was built for: the WORKING driver anchors
    `_global_offset`, so every segment locates correctly and the ambient
    window is real. The silent role's own band then shows ~0 dB of rise over
    that ambient and fails TARGET, while the working role clears it by a wide
    margin.

    Pinned per-ROLE (not just on the session verdict) because a per-role
    regression here is exactly what would let a miswired speaker commission.
    """
    roles = _check_roles()
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)
    # The woofer plays and anchors offset recovery; the tweeter never does.
    mono = pcm[:, 0]
    cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(5).normal(0.0, 3e-5, cap.size)

    res = analyze_program_capture(chk, cap, SR, priors=MeasurementPriors())

    # A real ambient window was measured, so the rise test genuinely ran.
    assert res.ambient_report["bands"]
    by_role = {p.role: p for p in res.pilots}
    assert by_role["tweeter"].channel_map_ok is False
    assert by_role["tweeter"].channel_map_target_rise_db == pytest.approx(0.0, abs=1.0)
    assert by_role["woofer"].channel_map_ok is True
    assert by_role["woofer"].channel_map_target_rise_db > 20.0
    assert res.channel_map_ok is False


def test_check_refuses_a_capture_with_no_program_in_it_at_all():
    """No pilot signal anywhere (pure noise capture) ⇒ CHECK refuses.

    With nothing to correlate against, `_global_offset` anchors on the first
    stimulus (``pilot_woofer_lo``) and returns a MEANINGLESS offset (order
    -10^4 samples; the exact value is an argmax over noise and is not stable
    across environments), i.e. the window's scheduled span lies almost
    entirely before the capture began. Since #1818 the ambient helper answers
    that honestly ("no evidence") instead of fabricating a window from
    ``capture[0:n]``, which is what the pre-#1818 clamped-start slice did — a
    window that was only accidentally right, because a capture normally leads
    the program and so happens to open on real room noise.

    What must hold is that the SESSION is refused, and it is, three
    independent ways. Per-role channel-map detail is deliberately NOT asserted
    here: with no ambient evidence `_channel_map_ok` documents a fallback to
    the original total-in-band-energy-fraction test (no rise concept without
    an ambient window), and white noise against the tweeter's 2.5-20 kHz
    declared band does put most of its energy in band — which since #2052
    resolves that role to UNKNOWN rather than to a PASS it did not earn. The
    woofer's own band still fails the fraction outright, and a FAILURE
    outranks an unknown in the fold, so the session verdict is ``False``.
    Per-role protection is pinned on the realistic miswire fixture above,
    where the offset is sound.
    """
    roles = _check_roles()
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)
    n = pcm.shape[0] + 500 + 5000
    cap = np.random.default_rng(31).normal(0.0, 3e-4, n)

    res = analyze_program_capture(chk, cap, SR, priors=MeasurementPriors())

    assert res.ambient_report["bands"] == []       # honest: no evidence
    assert res.channel_map_ok is False
    assert res.linearity_ok is False
    assert res.gain_plan is not None
    assert res.gain_plan.snr_floor_ok is False


# --------------------------------------------------------------------------- #
# CHECK channel map — the CROSS test is an ISOLATION RATIO, not an additive
# cross-rise bound (2026-08-21, jts3).
#
# The retired bound refused a cross rise at or above a flat 6.0 dB. That
# constant was tuned against the OLD measurement frame's room floor, which
# masked the cross-band content an honest capture always carries — so raising
# the session level lifted the mask and made a healthy speaker fail a
# `channel_map_mismatch` hard stop, `condition_not_retriable`, blocking every
# round in the louder frame. The rows below are that measurement, and they are
# the reason the metric is now `target_rise - cross_rise`.
# --------------------------------------------------------------------------- #


#: Centre of `_band_impulse`'s default 4096-tap buffer — see `_isolation_pilot`.
_MASK_DELAY = 2048


def _isolation_pilot(
    seg,
    other_band: tuple[float, float],
    ambient: np.ndarray,
    *,
    target_rise_db: float,
    cross_rise_db: float,
    seed: int,
) -> np.ndarray:
    """A pilot window built to a COMMANDED pair of rises over ``ambient``.

    Deliberately a two-band synthesis rather than a room simulation: this
    block pins where the CROSS decision BOUNDARY sits, so the two rises have
    to be dialled in exactly — including a NEGATIVE cross rise, which tonight's
    quietest row carries (the room's cross band during that pilot sat below the
    level the separate ambient window measured) and which no additive room
    model can produce. End-to-end realism against a real room's continuous
    noise already has its own fixtures — `test_channel_map_survives_concurrent_
    room_rumble` and the miswire pair — and this does not replace them.

    ``_deep_plant``'s doubled ~-88 dB stopband rather than a single mask, so
    the own-band content's leak into the other band cannot move the commanded
    cross rise; every caller re-reads both achieved rises out of the production
    code and asserts them against the command, so a fixture that stopped being
    honest fails loudly instead of quietly grading the wrong number.

    ``_MASK_DELAY`` centres each mask in its own 4096-tap buffer. At delay 0
    the brick-wall mask's symmetric sinc wraps to the END of the buffer and the
    caller's ``[:n]`` truncation throws it away, which collapses the stopband
    to about -8 dB — measured: a 73.1 dB target rise then produced a 64.6 dB
    "cross" rise made entirely of its own leak. Every other fixture in this
    file passes a non-zero delay for the same reason.
    """
    own_band = (float(seg.f1_hz), float(seg.f2_hz))
    rng = np.random.default_rng(seed)
    out = np.zeros(seg.n_samples)
    commands = (
        (own_band, target_rise_db),
        (other_band, cross_rise_db),
    )
    for band, rise_db in commands:
        want = program_analysis._band_rms_dbfs(ambient, SR, *band) + rise_db
        content = fftconvolve(
            rng.normal(0.0, 1.0, seg.n_samples),
            _deep_plant(_MASK_DELAY, band[0], band[1], 1.0),
        )[: seg.n_samples]
        have = program_analysis._band_rms_dbfs(content, SR, *band)
        out = out + content * 10.0 ** ((want - have) / 20.0)
    return out


def _isolation_case(role: str, target_rise_db: float, cross_rise_db: float, seed: int):
    """Run `_channel_map_ok` on one commanded rise pair. Returns its 3-tuple."""
    roles = _check_roles()
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    seg = chk.segment(f"pilot_{role}_hi")
    bands = {r.role: (r.band.lower_hz, r.band.upper_hz) for r in roles}
    other_band = bands["tweeter" if role == "woofer" else "woofer"]
    ambient = np.random.default_rng(seed).normal(0.0, 3e-4, SR)
    pilot = _isolation_pilot(
        seg, other_band, ambient,
        target_rise_db=target_rise_db, cross_rise_db=cross_rise_db, seed=seed + 1,
    )
    return program_analysis._channel_map_ok(
        pilot, SR, seg, ambient_samples=ambient, other_bands=(other_band,),
    )


#: The 2026-08-21 jts3 table, read off `event=correction.crossover_v2_check_diag`
#: — ONE healthy speaker, one basin-2 config, byte-identical graph, three
#: session levels. `(label, role, target_rise_db, cross_rise_db)`.
#:
#: The two louder sessions are the ones the retired additive bound refused. The
#: cross energy is NOT electrical crosstalk: a two-level discriminator through
#: the BASELINE graph held cross-band rise at <=3 dB across both the -16.8 and
#: -6.8 faders while own-band rises tracked the fader exactly, so the chain is
#: linear. Through the per-driver ROUTING graph (which strips no crossover
#: filters) the CHECK sees program-segment skirt content plus modest driver
#: nonlinearity — content at a roughly FIXED RELATIVE level, which is exactly
#: what an additive bound cannot describe and a ratio can.
_MEASURED_HEALTHY_ROWS = (
    ("ref -27.5, seat 68.1 dB SPL", "woofer", 53.4, -0.79),
    ("ref -9.77, seat 73.3 dB SPL", "woofer", 48.5, 4.13),
    ("ref -9.77, seat 73.3 dB SPL", "tweeter", 71.7, 10.81),
    ("ref -6.80, seat 78.6 dB SPL", "woofer", 51.4, 7.27),
    ("ref -6.80, seat 78.6 dB SPL", "tweeter", 73.1, 15.23),
)


@pytest.mark.parametrize(
    "label,role,target_rise_db,cross_rise_db",
    _MEASURED_HEALTHY_ROWS,
    ids=[f"{row[0]}-{row[1]}" for row in _MEASURED_HEALTHY_ROWS],
)
def test_channel_map_accepts_every_measured_session_level(
    label, role, target_rise_db, cross_rise_db,
):
    """Every row of the hardware table passes — including the two the retired
    additive bound refused.

    This is the regression, stated as the speaker experienced it: the SAME
    healthy speaker measured at three session levels, whose cross rise walks
    -0.79 -> 4.13 -> 7.27 dB (woofer) and 10.81 -> 15.23 dB (tweeter) purely
    because the room floor stopped masking it. The isolation ratio barely
    moves across the same span (44.1-60.9 dB), which is the whole claim: the
    ratio is level-independent where the additive rise is not.
    """
    ok, target_rise, cross_rise = _isolation_case(
        role, target_rise_db, cross_rise_db, seed=821,
    )
    # The fixture is honest — the production code read back what was commanded.
    assert target_rise == pytest.approx(target_rise_db, abs=0.5), label
    assert cross_rise == pytest.approx(cross_rise_db, abs=0.5), label

    isolation = program_analysis.channel_map_isolation_db(target_rise, cross_rise)
    assert isolation == pytest.approx(target_rise_db - cross_rise_db, abs=1.0), label
    assert isolation > program_analysis.CHANNEL_MAP_MIN_ISOLATION_DB, label
    assert ok is True, (
        f"{label}: {role} isolation {isolation:.1f} dB refused — a healthy "
        "speaker measured at an honest level is being told to rewire itself"
    )


def test_channel_map_refuses_abnormal_cross_band_energy():
    """The two shapes the CROSS test exists to catch, at any level.

    **This is not the mis-wire test, and an earlier version of this docstring
    said it was.** Seven wiring shapes were run through this validator and the
    cross rise stayed within ±0.4 dB on every one, because a wiring fault
    changes which DRIVER radiates, not which BAND carries the energy — so
    `_channel_map_ok`'s TARGET floor is what fires on a mis-wire, and the
    realistically-rolled-off swap that clears the whole channel map is a
    PRE-EXISTING gap (issue #2800), on `main` and on this branch alike. What
    the CROSS half guards is abnormal cross-band ENERGY, and both fixtures
    below are that:

    * the degenerate case — one signal reaching BOTH bands at once, isolation
      ~0. Kept because it is the fail-closed floor of this rung: whatever
      produces it, energy in two bands at equal strength is not a driver
      playing its own band, and the rung must refuse it.
    * a heavy bleed at 10 dB of isolation, an order of magnitude below every
      honest row on record.

    Both are commanded well above `CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB`, so
    the ratio is actually judged, and both clear the TARGET floor first — or
    the refusal would prove nothing about the CROSS half.
    """
    judged_above = program_analysis.CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB

    both_bands_ok, both_target, both_cross = _isolation_case("woofer", 50.0, 50.0, seed=901)
    assert program_analysis.channel_map_isolation_db(
        both_target, both_cross,
    ) == pytest.approx(0.0, abs=1.0)
    assert both_target > judged_above, (
        "the fixture must clear the judged threshold, or the CROSS half never "
        "looked and this proves nothing"
    )
    assert both_bands_ok is False

    bleed_ok, bleed_target, bleed_cross = _isolation_case("woofer", 50.0, 40.0, seed=902)
    assert program_analysis.channel_map_isolation_db(
        bleed_target, bleed_cross,
    ) == pytest.approx(10.0, abs=1.0)
    assert bleed_target > judged_above
    assert bleed_ok is False


def test_channel_map_cross_test_never_eats_the_target_floor():
    """The CROSS half must not convert a quiet-but-correct capture into a
    rewire hard stop. Pinned because it DID, and shipping it would have been
    the very bug this metric was written to remove.

    The mechanism, stated exactly: the CROSS test refuses when
    ``target_rise - cross_rise < BOUND``, i.e. when
    ``target_rise < BOUND + cross_rise``. So it does not merely sit beside the
    TARGET floor — it RAISES the effective floor to
    ``max(FLOOR, BOUND + cross_rise)``, eating the floor by ``cross_rise`` dB.
    An earlier draft argued a bound at or below `CHANNEL_MAP_TARGET_RISE_DB`
    was enough to prevent this; that argument only holds at ``cross_rise <= 0``
    and is false in general.

    The measured case, end to end: target 13.50 / cross 1.72 — both values
    taken from this suite's own noisy-room fixture — gives isolation 11.78,
    which under an ungated ratio refused as the NON-retriable
    ``channel_map_mismatch`` where `main` refused it as the retriable
    ``snr_floor``. A hard stop telling a household to open its speaker, on a
    capture whose only real problem was that it was quiet (#2052/#2644).

    The guard is `CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB`: below it the TARGET
    floor governs alone. What that buys, and what this test really pins, is
    that a CROSS refusal is self-justifying — above the threshold, refusing
    requires ``cross_rise >= CHANNEL_MAP_TARGET_RISE_DB``, so the WRONG band
    cleared the very bar we demand of a driver that played. Nothing merely
    quiet can manufacture that.
    """
    floor = program_analysis.CHANNEL_MAP_TARGET_RISE_DB
    bound = program_analysis.CHANNEL_MAP_MIN_ISOLATION_DB
    judged_above = program_analysis.CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB
    assert judged_above == pytest.approx(floor + bound), (
        "the threshold IS floor+bound; any other value breaks the "
        "cross_rise >= floor implication this rung's honesty rests on"
    )

    # The regression itself, on the real validator.
    ok, target_rise, cross_rise = _isolation_case("woofer", 13.50, 1.72, seed=904)
    assert target_rise == pytest.approx(13.50, abs=0.5)
    assert cross_rise == pytest.approx(1.72, abs=0.5)
    isolation = program_analysis.channel_map_isolation_db(target_rise, cross_rise)
    assert isolation < bound, (
        "premise: this capture's ratio IS under the bound — if it were not, "
        "the guard below would be untested"
    )
    assert target_rise >= floor, "premise: it cleared the TARGET floor"
    assert ok is True, (
        "a quiet-but-correct capture must not be refused by the CROSS half; "
        "the honest, retriable finding is snr_floor"
    )
    # The household-visible half of the claim — that such a capture lands on
    # `snr_floor` rather than `channel_map_mismatch` — is the composition of
    # this `ok` with a rung already pinned where it belongs, in
    # `tests/test_crossover_v2_capture_dispatch.py`
    # (`test_check_rungs_report_their_own_finding`'s `pilot_snr_ok=False` row,
    # plus `test_check_never_refuses_on_an_unestablished_fact`). Re-asserting
    # the ladder here would import the flow package into an audio-measurement
    # test to restate a fact that file already owns. Verified end-to-end by
    # hand against the real `check_screens` in the fix round for this rung.


def test_channel_map_isolation_boundary_is_inclusive_at_the_bound(monkeypatch):
    """Isolation exactly AT the bound passes; one thousandth under it fails.

    A DIRECT call with scripted band levels rather than a synthesized capture:
    the boundary direction is a one-`<`-versus-`<=` decision, and a fixture
    whose achieved rises carry ±0.5 dB of tolerance cannot pin it at all.

    Inclusive-pass is deliberate and is the SAFE direction here: this rung
    ends in a non-retriable hard stop that tells a household to open its
    speaker, so a capture sitting exactly on the bar is given the bar.
    """
    seg = build_check_program(
        _check_roles(), ambient_s=1.0, pilot_duration_s=0.5,
    ).segment("pilot_woofer_hi")
    own = (float(seg.f1_hz), float(seg.f2_hz))
    other = (2500.0, 20000.0)
    # Sentinel-valued arrays so the stub can tell pilot from ambient by value
    # rather than by identity (`np.asarray` may or may not copy).
    ambient = np.full(64, 1.0)
    pilot = np.full(64, 2.0)
    bound = program_analysis.CHANNEL_MAP_MIN_ISOLATION_DB
    # Comfortably above the judged threshold, so the ratio is actually judged.
    target_rise = program_analysis.CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB + 30.0

    def _script(cross_rise: float):
        def _rms(samples, sample_rate, f1, f2):
            if float(np.asarray(samples).flat[0]) == 1.0:
                return -80.0
            return -80.0 + (target_rise if (f1, f2) == own else cross_rise)
        return _rms

    def _run(cross_rise: float):
        monkeypatch.setattr(program_analysis, "_band_rms_dbfs", _script(cross_rise))
        return program_analysis._channel_map_ok(
            pilot, SR, seg, ambient_samples=ambient, other_bands=(other,),
        )

    at_bound = _run(target_rise - bound)          # isolation == bound exactly
    assert at_bound[0] is True
    assert program_analysis.channel_map_isolation_db(
        at_bound[1], at_bound[2],
    ) == pytest.approx(bound)

    under = _run(target_rise - bound + 0.001)     # isolation == bound - 0.001
    assert under[0] is False


def test_channel_map_isolation_is_one_definition_not_two():
    """`channel_map_isolation_db` is the SSOT for the metric, and it is
    ``None``-safe in the fail-closed direction.

    Both the verdict and every reporting surface (the CHECK diag event, the
    forensic analysis dump) read this one function, so the ratio an operator
    sees beside a refusal is the ratio that caused it. A missing rise is "no
    evidence" — `None`, never a number a caller could mistake for a pass.
    """
    assert program_analysis.channel_map_isolation_db(48.5, 4.13) == pytest.approx(44.37)
    assert program_analysis.channel_map_isolation_db(53.4, -0.79) == pytest.approx(54.19)
    assert program_analysis.channel_map_isolation_db(None, 1.0) is None
    assert program_analysis.channel_map_isolation_db(20.0, None) is None
    assert program_analysis.channel_map_isolation_db(None, None) is None


# --------------------------------------------------------------------------- #
# CHECK channel map — honest-unknown on the no-ambient fallback (issue #2052)
#
# The fallback runs whenever the ambient window is absent or unusable. Three
# facts are pinned below and they are one design: the fallback's PASS is not
# evidence (so it is UNKNOWN), its FAIL still is (so it stays a finding), and
# the fold over the roles is tri-state rather than `all()`.
# --------------------------------------------------------------------------- #


def _deep_plant(delay: int, f_lo: float, f_hi: float, amp: float) -> np.ndarray:
    """A synthetic driver IR with a ~-88 dB stopband.

    Doubling `_band_impulse`'s ~-44 dB single-mask stopband, for the same
    reason `test_channel_map_fails_on_swapped_channels` does it: at -44 dB a
    driver fed fully out-of-band content still leaks a coherent in-band ghost
    above the noise floor, which is a fixture artifact rather than anything a
    physical driver does.
    """
    single = _band_impulse(delay, f_lo, f_hi, 1.0)
    return amp * fftconvolve(single, single)


def _degraded_check_capture(
    woofer_plant: np.ndarray | None,
    tweeter_plant: np.ndarray | None,
    seed: int,
    *,
    late_samples: int = 0,
):
    """A CHECK capture whose ambient window is gone, so the fallback runs.

    Two independent ways to lose the window, both reproduced by the callers:
    starting the recording ``late_samples`` after the program (the #1818 shape
    — below `AMBIENT_MIN_USABLE_FRACTION` of the scheduled window survives, so
    `_ambient_from_capture` answers "no evidence"), or silencing the driver
    that anchors offset recovery, which leaves the window's scheduled span
    almost entirely before the capture began.
    """
    roles = [
        RoleBand("woofer", 0, FrequencyBand(150.0, 1200.0)),
        RoleBand("tweeter", 1, FrequencyBand(2500.0, 20000.0)),
    ]
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)
    mono = np.zeros(pcm.shape[0])
    for channel, plant in ((0, woofer_plant), (1, tweeter_plant)):
        if plant is not None:
            mono = mono + fftconvolve(pcm[:, channel], plant)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(seed).normal(0.0, 3e-5, cap.size)
    return chk, (cap[late_samples:] if late_samples else cap)


#: 0.9 s of a 1.0 s scheduled window, plus the fixture's own 500-sample lead —
#: comfortably under `AMBIENT_MIN_USABLE_FRACTION` (0.5).
_LATE_SAMPLES = int(0.9 * SR) + 500


def test_channel_map_fallback_pass_is_unknown_and_fail_is_a_finding():
    """The no-ambient fallback is ONE-SIDED evidence, reported one-sided.

    Direct unit coverage of `_channel_map_ok`'s fallback branch, because the
    asymmetry IS the design and a symmetric rewrite in either direction is a
    real regression:

    * folding the FAIL to ``None`` too drops the only channel-map evidence a
      capture with no ambient window still carries (pinned end-to-end by
      `test_degraded_miswire_still_names_the_wiring_not_the_room`);
    * leaving the PASS as ``True`` republishes the #2042 false pass (pinned by
      `test_channel_map_fallback_never_passes_a_driver_that_never_played`).

    Both rise numbers stay ``None`` on this path — there is no rise concept
    without an ambient reference — so the tell that the fallback ran, rather
    than the rise test, is unchanged.
    """
    # The real scheduled segment, not a hand-built twin: its declared band IS
    # the fact under test, and a local copy is a second statement of it.
    chk = build_check_program(
        [
            RoleBand("woofer", 0, FrequencyBand(150.0, 1200.0)),
            RoleBand("tweeter", 1, FrequencyBand(2500.0, 20000.0)),
        ],
        ambient_s=1.0, pilot_duration_s=0.5,
    )
    seg = chk.segment("pilot_woofer_hi")
    noise = np.random.default_rng(2052).normal(0.0, 1.0, seg.n_samples)
    in_band = fftconvolve(noise, _band_impulse(0, 150.0, 1200.0, 1.0))[: seg.n_samples]
    out_of_band = fftconvolve(noise, _band_impulse(0, 4000.0, 20000.0, 1.0))[: seg.n_samples]

    ok, target_rise, cross_rise = program_analysis._channel_map_ok(
        in_band, SR, seg, ambient_samples=None,
    )
    assert ok is None                       # cleared the fraction — not evidence
    assert target_rise is None and cross_rise is None

    ok, target_rise, cross_rise = program_analysis._channel_map_ok(
        out_of_band, SR, seg, ambient_samples=None,
    )
    assert ok is False                      # missed its own band — a finding
    assert target_rise is None and cross_rise is None


def test_channel_map_fallback_never_passes_a_driver_that_never_played():
    """#2042's flagged false PASS, removed (issue #2052).

    The realistic miswire — one driver silent — recorded so late that the
    ambient window is unusable. The rise test cannot run, and the fallback's
    fraction is cleared by the silent role's window of pure ROOM NOISE, because
    a 2.5-20 kHz declared band holds most of broadband noise's energy. Before
    #2052 that published ``channel_map_ok=True`` for a driver that produced
    nothing at all.

    What must hold: neither role claims a PASS, the session verdict is UNKNOWN
    rather than a PASS, and — the `all()` trap — an unknown beside a pass is
    NOT laundered into the ``channel_map_mismatch`` hard stop, which would tell
    this household to open a speaker on evidence that was never taken.
    """
    chk, cap = _degraded_check_capture(
        _deep_plant(200, 150.0, 1200.0, 1.0), None, 5, late_samples=_LATE_SAMPLES,
    )
    res = analyze_program_capture(chk, cap, SR, priors=MeasurementPriors())

    assert res.ambient_report["bands"] == []           # the fallback really ran
    by_role = {p.role: p for p in res.pilots}
    assert by_role["tweeter"].channel_map_ok is None   # was True before #2052
    assert by_role["woofer"].channel_map_ok is None
    assert all(p.channel_map_target_rise_db is None for p in res.pilots)
    assert res.channel_map_ok is None
    # Still refused, on the rungs whose evidence this capture DOES carry.
    assert res.linearity_ok is False


def test_degraded_miswire_still_names_the_wiring_not_the_room():
    """The fallback's FAIL is the only wiring evidence a lost window leaves.

    Both miswire shapes that destroy offset recovery outright — a swapped
    pair, and a silent ANCHOR driver (`_global_offset` anchors on
    ``pilot_woofer_lo``) — take the fallback with no ambient window at all.
    Their surviving role misses its own declared band, so the fallback fails
    and the session verdict stays an explicit ``False``, which
    `capture_dispatch.check_screens` maps to ``channel_map_mismatch`` — a
    wiring remedy for a wiring fault.

    This is the half of #2052 that a blanket "unknown whenever the window is
    gone" would have cost: measured on this branch, both shapes would have
    dropped to ``None`` and fallen from `check_screens`' rung 3 to its rung 5,
    with copy blaming the room instead. Refusal held either way; the
    household's remedy did not.
    """
    woofer = _deep_plant(200, 150.0, 1200.0, 1.0)
    tweeter = _deep_plant(225, 2500.0, 20000.0, 0.8)
    for tag, (ch0, ch1, seed) in {
        "swapped pair": (tweeter, woofer, 12),
        "silent anchor driver": (None, tweeter, 7),
    }.items():
        chk, cap = _degraded_check_capture(ch0, ch1, seed)
        res = analyze_program_capture(chk, cap, SR, priors=MeasurementPriors())
        assert res.ambient_report["bands"] == [], tag   # the fallback really ran
        assert res.channel_map_ok is False, tag


def test_channel_map_aggregate_is_tri_state():
    """The channel-map fold, pinned on the same table as ``linearity_ok``.

    Both go through `_aggregate_tri_state_ok` — one fold, because "what does
    unknown mean here" is one decision. FAILURE outranks UNKNOWN outranks
    PASS: a role that genuinely landed in the wrong band must not be laundered
    by an unreadable sibling, and a readable sibling must not upgrade an
    unknown into "the map is right".

    Pinned directly because the reduction was ``all(...)`` until #2052, and
    Python folds ``None`` to False there — so the moment the fallback started
    answering ``None``, a plain ``all()`` would have turned every unknown into
    ``channel_map_mismatch``: a hard stop telling a household to rewire its
    speaker, decided on evidence nobody took.
    """
    aggregate = program_analysis._aggregate_tri_state_ok
    assert aggregate([]) is None
    assert aggregate([True, True]) is True
    assert aggregate([True, None]) is None
    assert aggregate([None, None]) is None
    assert aggregate([False, None]) is False
    assert aggregate([False, True]) is False


# --------------------------------------------------------------------------- #
# VERIFY
# --------------------------------------------------------------------------- #


def test_verify_summed_response_and_ripple():
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    ir = _band_impulse(200, 150.0, 20000.0, 1.0, n=8192)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(5).normal(0.0, 1e-4, cap.size)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.summed_response is not None
    # A flat synthetic IR sums flat ⇒ small ripple through the crossover.
    assert res.summed_ripple_db is not None
    assert res.summed_ripple_db < 3.0


# --------------------------------------------------------------------------- #
# flatness-verify (#1668 PR-D) was RETIRED by the flat-linearization plan's
# PR-5 (the spec-curve SSOT). ``_flatness_tracking`` graded ONE capture on its
# own grid against its own band mean; the spec claim is now graded once per
# spatial cloud group by ``flat_spec.evaluate_flat_spec`` +
# ``spec_flatness_gauge`` (see tests/test_flat_spec_ssot.py). These two tests
# pin that the retirement is complete and that the SIBLING claim which stayed
# — integration-verify's tracking comparator — is untouched by it.
# --------------------------------------------------------------------------- #


def test_verify_carries_no_capture_grid_flatness_claim():
    """PR-5: a VERIFY analysis no longer answers "is the speaker flat".

    The same fixture that used to prove flatness_tracking's independence from
    ``predicted_sum`` now proves the field is gone entirely — a single
    capture cannot support the claim, so ``ProgramAnalysis`` must not carry
    a place to put one. ``verify_tracking`` (the claim a single capture CAN
    support) is unaffected and still ``None`` here for its own reason: no
    predicted_sum was supplied."""
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    ir = _band_impulse(200, 150.0, 20000.0, 1.0, n=8192)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(5).normal(0.0, 1e-4, cap.size)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.verify_tracking is None
    assert not hasattr(res, "flatness_tracking")
    assert not hasattr(program_analysis, "_flatness_tracking")
    assert not hasattr(program_analysis, "FLATNESS_VERIFY_HI_HZ")
    assert not hasattr(program_analysis, "FLATNESS_VERIFY_TOLERANCE_DB")


def test_verify_with_no_fc_hz_still_yields_a_summed_response():
    """The half of the retired test that was never about flatness: a VERIFY
    analysis with NO crossover_fc_hz prior skips integration-verify's whole
    ripple/tracking block and still returns the gated summed response the
    cloud pipeline combines."""
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    ir = _band_impulse(200, 150.0, 20000.0, 1.0, n=8192)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(5).normal(0.0, 1e-4, cap.size)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.summed_ripple_db is None
    assert res.verify_tracking is None
    assert res.summed_response is not None
    assert res.summed_response.validity_floor_hz is not None


def test_verify_tracking_against_predicted_sum():
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    ir = _band_impulse(200, 150.0, 20000.0, 1.0, n=8192)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(6).normal(0.0, 1e-4, cap.size)
    # A flat predicted sum over the crossover region ⇒ small tracking error.
    pred_freqs = np.geomspace(100.0, 20000.0, 400)
    pred_db = np.zeros_like(pred_freqs)
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, predicted_sum=(pred_freqs, pred_db)),
    )
    assert res.verify_tracking is not None
    assert res.verify_tracking["rms_db"] < 3.0
    # W6.7 ruling 1: a flat prediction has no bin more than
    # ``VERIFY_NOTCH_EXCLUSION_DB`` below its own median, so nothing is
    # excluded — the notch-excluded fields are byte-identical to the raw
    # full-band ones.
    assert res.verify_tracking["max_db_notch_excluded"] == pytest.approx(
        res.verify_tracking["max_db"]
    )
    assert res.verify_tracking["rms_db_notch_excluded"] == pytest.approx(
        res.verify_tracking["rms_db"]
    )


def test_verify_tracking_smooths_measured_and_predicted_curves_equally():
    """An exact raw model must not fail because only the capture is smoothed."""
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    ir = np.zeros(8192)
    # A pre-arrival 100 samples before the direct peak creates fine, bounded
    # ripple that 1/6-octave smoothing changes materially without introducing
    # a modeled deep-notch exclusion that would hide the mismatch.
    ir[100] = 0.7
    ir[200] = 1.0
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])

    baseline = analyze_program_capture(
        prog,
        cap,
        SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    response = baseline.summed_response
    assert response is not None
    predicted_sum = (response.freqs_hz, response.magnitude_db)

    result = analyze_program_capture(
        prog,
        cap,
        SR,
        priors=MeasurementPriors(
            crossover_fc_hz=FC_HZ,
            predicted_sum=predicted_sum,
        ),
    )
    tracking = result.verify_tracking
    assert tracking is not None
    assert tracking["max_db_notch_excluded"] == pytest.approx(0.0, abs=1e-5)
    assert tracking["rms_db_notch_excluded"] == pytest.approx(0.0, abs=1e-5)

    # Prove the fixture catches the hardware failure mode: the old one-sided
    # comparator falsely rejects this exact model against its own capture.
    measured_smoothed = analysis_mod.smooth_fractional_octave(
        response.freqs_hz,
        response.magnitude_db,
        program_analysis.VERIFY_TRACKING_SMOOTHING_FRACTION,
    )
    _old_rms, old_max = analysis_mod.notch_excluded_tracking_error_db(
        response.freqs_hz,
        measured_smoothed,
        response.magnitude_db,
        (FC_HZ / 2.0, FC_HZ * 2.0),
        notch_exclusion_db=program_analysis.VERIFY_NOTCH_EXCLUSION_DB,
    )
    assert old_max > 1.5


# --------------------------------------------------------------------------- #
# frame discipline (rung P1)
# --------------------------------------------------------------------------- #


def _verify_against(predicted_sum, *, seed: int = 7):
    """Analyze one synthetic VERIFY capture against a supplied predicted sum."""
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    ir = np.zeros(8192)
    ir[100] = 0.7
    ir[200] = 1.0
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    del seed
    return analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, predicted_sum=predicted_sum),
    )


def _self_prediction():
    """The capture's OWN summed response — a prediction in the SAME frame, so
    any frame the fit then reports is exactly what the test injected."""
    baseline = _verify_against(None)
    response = baseline.summed_response
    assert response is not None
    return response.freqs_hz, response.magnitude_db


def _with_frame(pred_freqs, pred_db, *, offset_db: float, tilt_db_per_octave: float):
    """A predicted curve moved by a known frame, so the analysis sees exactly
    ``offset_db``/``tilt_db_per_octave`` of MEASURED-minus-PREDICTED.

    The frame is subtracted from the prediction rather than added to the
    capture — the capture is the thing under test and must stay untouched. The
    DC bin has no octave and is left alone (``log2(0)``).
    """
    freqs = np.asarray(pred_freqs, dtype=float)
    positive = freqs > 0.0
    frame = np.zeros_like(freqs)
    frame[positive] = offset_db + tilt_db_per_octave * np.log2(freqs[positive] / 1000.0)
    return freqs, np.asarray(pred_db, dtype=float) - frame


def test_verify_discloses_the_frame_it_compared_across():
    """Rung P1: every tracking record says what frame the two curves spanned.

    Not a nice-to-have. VERIFY differences an ON-AXIS two-branch model against
    an IN-ROOM gated measurement; on the 2026-07-29 corpus a single
    −0.79 dB/octave tilt between those frames was 84 % of the flow's apparent
    prediction error (first-principles panel W1). A record without the frame is
    half a measurement.
    """
    result = _verify_against(_self_prediction())
    tracking = result.verify_tracking
    assert tracking is not None
    frame = tracking["frame"]
    assert set(frame) == {
        "offset_db", "tilt_db_per_octave", "pivot_hz", "n_bins", "band_hz",
        "raw", "tilt_removed",
    }
    # The record's raw pair IS the reported pair — one computation, two views,
    # so a surface reading the disclosure can never quote a different number
    # than the one the tolerance gated on.
    assert frame["raw"]["rms_db"] == tracking["rms_db"]
    assert frame["raw"]["max_db"] == tracking["max_db_notch_excluded"]
    # Same-frame prediction ⇒ a frame near zero, measured rather than assumed.
    assert frame["offset_db"] == pytest.approx(0.0, abs=1e-6)
    assert frame["tilt_db_per_octave"] == pytest.approx(0.0, abs=1e-6)
    # The frame was measured inside the graded band (its outermost GRID BINS,
    # which is why it is not the band's own edges) and nowhere else.
    lo, hi = tracking["tracking_band_hz"]
    assert lo <= frame["band_hz"][0] <= frame["band_hz"][1] <= hi
    assert frame["n_bins"] > 100


def _notched(pred_freqs, pred_db, *, center_hz: float, depth_db: float = 25.0):
    """A deep modelled interference notch in the PREDICTED curve.

    Narrow (a sixth-octave half-width) and deep enough to clear
    ``VERIFY_NOTCH_EXCLUSION_DB``, so the flow's own gating comparator refuses
    to grade those bins — which is exactly why the frame fit must refuse to
    estimate from them.
    """
    freqs = np.asarray(pred_freqs, dtype=float)
    notch = np.zeros_like(freqs)
    positive = freqs > 0.0
    octaves = np.zeros_like(freqs)
    octaves[positive] = np.log2(freqs[positive] / center_hz)
    notch = -depth_db * np.exp(-((octaves / (1.0 / 6.0)) ** 2))
    return freqs, np.asarray(pred_db, dtype=float) + notch


@pytest.mark.parametrize("notch_at,center_hz", [("edge", FC_HZ * 2.0), ("centre", FC_HZ)])
def test_excluding_notch_bins_from_the_fit_beats_fitting_the_whole_band(
    notch_at, center_hz,
):
    """The bins the comparator refuses to GRADE must not estimate the FRAME.

    Gate finding on PR #1987. The fit used to run over the whole
    validity-floor-clamped band, INCLUDING the deep-predicted-notch bins
    ``notch_excluded_tracking_error_db`` deliberately drops. Inside a modelled
    notch the depth is hypersensitive to sub-dB branch differences (the W6.7
    finding the exclusion exists for), so a straight line through one lets the
    notch lever the slope — on the gate's own bench a −0.800 dB/octave frame
    with a 25 dB edge notch came back **+0.226**, the wrong sign, and would
    then have been "removed" from the residual as if it were instrument tilt.

    **What is pinned is the DIRECTION, measured in-test against the very
    construction it replaces — not a magnitude and not a sign.** Both are
    tempting and both would be false pins: how far a surviving notch skirt can
    still lever the slope depends on the skirt's WIDTH, which the exclusion
    bounds only in DEPTH (12 dB). On this fixture the shipped fit still lands
    +0.31 dB/oct at the band edge — better than the pre-fix +5.72 by 18×, and
    still the wrong sign. Asserting sign recovery here would be asserting a
    property the mask intersection does not have; asserting a tolerance would
    be freezing an accident of this notch's shape. The honest, useful contract
    is that trusting fewer bins is never worse than trusting all of them.
    """
    tilt = -0.800
    framed = _with_frame(
        *_self_prediction(), offset_db=0.0, tilt_db_per_octave=tilt,
    )
    supplied_freqs, supplied_db = _notched(*framed, center_hz=center_hz)
    result = _verify_against((supplied_freqs, supplied_db))
    tracking = result.verify_tracking
    assert tracking is not None
    frame = tracking["frame"]

    freqs, measured_db, predicted_db = result.verify_tracking_curve
    freqs = np.asarray(freqs, dtype=float)
    band = tuple(tracking["tracking_band_hz"])
    in_band = (freqs >= band[0]) & (freqs <= band[1])

    # The notch really did exclude bins — otherwise this fixture proves nothing.
    assert frame["n_bins"] < int(in_band.sum())
    assert band[0] <= frame["band_hz"][0] <= frame["band_hz"][1] <= band[1]

    # The construction this replaced: fit over every graded bin, notch included.
    unexcluded = fit_frame(
        freqs[in_band], measured_db[in_band], predicted_db[in_band],
    )
    shipped_error = abs(frame["tilt_db_per_octave"] - tilt)
    unexcluded_error = abs(unexcluded.tilt_db_per_octave - tilt)
    assert shipped_error < unexcluded_error
    # And the pre-fix construction really was badly levered here, so the
    # comparison above is not two near-identical numbers agreeing by luck.
    assert unexcluded_error > 1.0


def test_the_beside_grade_is_not_promised_to_be_smaller_than_the_raw_one():
    """"tilt-removed ≤ raw" is NOT a theorem and must never be asserted.

    The two numbers are taken over different bin sets by different graders, and
    removing a frame estimated on one set can legitimately raise a residual
    measured on another — before the notch-exclusion fix, a 25 dB notch at band
    centre did exactly that (tilt-removed max 4.85 dB vs raw 4.74 dB). What the
    product promises is DISCLOSURE of both, not an ordering between them; a
    test that pinned the ordering would be pinning a coincidence, and the next
    corpus that broke it would look like a regression instead of a measurement.

    So this pins the honest contract: both numbers are present and finite, and
    nothing anywhere requires one to be smaller.
    """
    pred_freqs, pred_db = _self_prediction()
    framed = _with_frame(pred_freqs, pred_db, offset_db=0.0, tilt_db_per_octave=-0.8)
    result = _verify_against(_notched(*framed, center_hz=FC_HZ))
    tracking = result.verify_tracking
    assert tracking is not None
    raw, tilt_removed = tracking["frame"]["raw"], tracking["frame"]["tilt_removed"]
    for value in (raw["rms_db"], raw["max_db"],
                  tilt_removed["rms_db"], tilt_removed["max_db"]):
        assert isinstance(value, float) and math.isfinite(value)


def test_an_injected_tilt_is_recovered_and_graded_beside_the_raw_residual():
    """The synthetic-tilt fixture: inject a KNOWN frame between two otherwise
    identical curves and watch the product path split it out.

    The raw grade must SHOW the tilt (that is what an undisclosed frame does to
    a comparison) and the tilt-removed grade must collapse to ~nothing (that is
    what the disclosure buys). Both numbers ride the same record; neither
    replaces the other.
    """
    offset_db, tilt_db_per_octave = 1.4, -0.8
    result = _verify_against(_with_frame(
        *_self_prediction(),
        offset_db=offset_db,
        tilt_db_per_octave=tilt_db_per_octave,
    ))
    tracking = result.verify_tracking
    assert tracking is not None
    frame = tracking["frame"]

    # The tilt comes back. The offset is stated at the fit's own pivot, so
    # re-express the injected frame there before comparing.
    assert frame["tilt_db_per_octave"] == pytest.approx(tilt_db_per_octave, abs=0.01)
    assert frame["offset_db"] == pytest.approx(
        offset_db + tilt_db_per_octave * np.log2(frame["pivot_hz"] / 1000.0), abs=0.01
    )

    # The raw grade carries the tilt; the beside-grade does not. What remains
    # in the beside-grade is the analysis path's own 1/6-octave smoothing
    # acting on a log-linear ramp over a LINEAR FFT grid — tens of millidB,
    # three orders below the tolerance and two below the tilt it removed.
    raw, tilt_removed = frame["raw"], frame["tilt_removed"]
    assert raw["rms_db"] > 0.15
    assert tilt_removed["rms_db"] < 0.1 * raw["rms_db"]
    assert tilt_removed["rms_db"] == pytest.approx(0.0, abs=0.05)
    assert tilt_removed["max_db"] == pytest.approx(0.0, abs=0.1)
    assert raw["max_db"] > tilt_removed["max_db"]


def test_the_frame_disclosure_never_moves_the_raw_grade():
    """The MUST-NOT of rung P1, pinned: the gate reads the same number it read
    before a frame was ever fitted.

    Recomputed independently from the very curves the analysis reduced
    (``verify_tracking_curve``) with the shipped graders — so this fails if the
    reported raw scalars are ever quietly taken over a frame-removed curve,
    which is the exact regression the beside-grade could invite.
    """
    pred_freqs, tilted = _with_frame(
        *_self_prediction(), offset_db=2.0, tilt_db_per_octave=-0.9,
    )
    result = _verify_against((pred_freqs, tilted))
    tracking = result.verify_tracking
    assert tracking is not None
    assert tracking["frame"]["tilt_db_per_octave"] == pytest.approx(-0.9, abs=0.01)

    freqs, measured_db, predicted_db = result.verify_tracking_curve
    band = tuple(tracking["tracking_band_hz"])
    rms, max_abs = analysis_mod.tracking_error_db(
        freqs, measured_db, predicted_db, band,
    )
    assert tracking["rms_db"] == rms
    assert tracking["max_db"] == max_abs
    _rms_excl, max_excl = analysis_mod.notch_excluded_tracking_error_db(
        freqs, measured_db, predicted_db, band,
        notch_exclusion_db=program_analysis.VERIFY_NOTCH_EXCLUSION_DB,
        notch_reference_db=np.interp(freqs, pred_freqs, tilted),
    )
    assert tracking["max_db_notch_excluded"] == max_excl


def test_a_verify_with_no_prediction_discloses_no_frame_at_all():
    """No comparison, no frame. Absence must mean "nothing was compared",
    never "the frames agreed"."""
    result = _verify_against(None)
    assert result.verify_tracking is None


def test_the_frame_and_its_beside_grades_ride_the_retention_sidecar():
    """The retained clip is the forensic record of ONE comparison, and the
    frame is half of what that comparison was — so it travels with it, flat,
    beside the raw scalars it does not replace."""
    result = _verify_against(_with_frame(
        *_self_prediction(), offset_db=0.0, tilt_db_per_octave=0.7,
    ))
    summary = program_analysis.analysis_diagnostic_summary(result)
    tracking = result.verify_tracking
    assert tracking is not None

    frame = tracking["frame"]
    assert summary["frame_offset_db"] == frame["offset_db"]
    assert summary["frame_tilt_db_per_octave"] == pytest.approx(0.7, abs=0.01)
    assert summary["frame_pivot_hz"] == frame["pivot_hz"]
    assert summary["frame_n_bins"] == frame["n_bins"]
    assert summary["rms_db_tilt_removed"] == frame["tilt_removed"]["rms_db"]
    assert summary["max_db_notch_excluded_tilt_removed"] == frame["tilt_removed"]["max_db"]
    # And the raw ones are still there, unchanged, beside them.
    assert summary["rms_db"] == tracking["rms_db"]
    assert summary["max_db_notch_excluded"] == tracking["max_db_notch_excluded"]


def test_notch_mask_uses_raw_prediction_when_comparison_is_smoothed():
    """Smoothing must not erase the identity of a modeled deep notch."""
    freqs = np.geomspace(500.0, 8000.0, 2001)
    raw_predicted_db = np.zeros_like(freqs)
    notch_idx = int(np.argmin(np.abs(freqs - FC_HZ)))
    raw_predicted_db[notch_idx] = -30.0
    smoothed_predicted_db = analysis_mod.smooth_fractional_octave(
        freqs,
        raw_predicted_db,
        program_analysis.VERIFY_TRACKING_SMOOTHING_FRACTION,
    )
    # The narrow raw notch is lifted above the 12 dB mask threshold by the
    # comparison smoothing. Put the only mismatch at that modeled-notch bin.
    assert smoothed_predicted_db[notch_idx] > -12.0
    measured_db = smoothed_predicted_db.copy()
    measured_db[notch_idx] += 8.0

    _smoothed_mask_rms, smoothed_mask_max = (
        analysis_mod.notch_excluded_tracking_error_db(
            freqs,
            measured_db,
            smoothed_predicted_db,
            (500.0, 8000.0),
            notch_exclusion_db=program_analysis.VERIFY_NOTCH_EXCLUSION_DB,
        )
    )
    raw_mask_rms, raw_mask_max = analysis_mod.notch_excluded_tracking_error_db(
        freqs,
        measured_db,
        smoothed_predicted_db,
        (500.0, 8000.0),
        notch_exclusion_db=program_analysis.VERIFY_NOTCH_EXCLUSION_DB,
        notch_reference_db=raw_predicted_db,
    )

    assert smoothed_mask_max > 1.5
    assert raw_mask_rms == pytest.approx(0.0)
    assert raw_mask_max == pytest.approx(0.0)


def test_verify_tracking_notch_exclusion_reduces_max_through_the_pipeline():
    """W6.7 ruling 1, wired end-to-end through ``analyze_program_capture``: a
    real captured null (a two-tap comb filter — an actual acoustic-style
    interference notch, not a hand-drawn curve) compared against a predicted
    null of the same general shape but a slightly different exact
    frequency/depth (the "sub-dB/sub-degree branch difference" the run-7
    architect's analysis names). The exact numeric OLD-fails/NEW-passes
    contract is pinned directly against ``notch_excluded_tracking_error_db``
    in test_audio_measurement_harmonics.py; this test only pins that the
    pipeline actually WIRES the exclusion in (1/6-oct smoothing, the notch
    boundary applied) and that it measurably shrinks the max here too."""
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    null_delay = int(round(SR / FC_HZ))
    ir = np.zeros(8192)
    ir[200] = 1.0
    ir[200 + null_delay] = -1.0
    spectrum = np.fft.rfft(ir)
    freqs_ir = np.fft.rfftfreq(ir.size, 1.0 / SR)
    spectrum[(freqs_ir < 150.0) | (freqs_ir > 20000.0)] = 0.0
    ir = np.fft.irfft(spectrum, ir.size)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(11).normal(0.0, 1e-4, cap.size)

    pred_freqs = np.geomspace(100.0, 20000.0, 400)
    # The predicted null assumes a slightly different delay -- the analytic
    # comb-filter magnitude for the SAME shape, a hair off in frequency.
    predicted_mag = 2.0 * np.abs(np.sin(np.pi * pred_freqs * (null_delay + 0.4) / SR))
    pred_db = 20.0 * np.log10(np.maximum(predicted_mag, 1e-6))

    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, predicted_sum=(pred_freqs, pred_db)),
    )
    assert res.verify_tracking is not None
    tracking = res.verify_tracking
    # The raw full-band max is dominated by the notch-position mismatch
    # (the run-7 bug shape); excluding the predicted notch's own interior
    # measurably shrinks it.
    assert tracking["max_db_notch_excluded"] < tracking["max_db"]
    assert tracking["rms_db_notch_excluded"] < tracking["rms_db"]


# --------------------------------------------------------------------------- #
# W6.9 — gating-consistent prediction + validity-floor tracking clamp
# --------------------------------------------------------------------------- #
#
# W6.9 forensics (2026-07-19) numerically reproduced a W6 run-7/8 hardware
# VERIFY failure and traced it to two compounding bugs: (1) the MEASURE-side
# prediction composed each branch's TF from a FIXED 65 ms window
# (`IR_PRE_MS` + `IR_POST_MS`), so a 15 cm desk-bounce reflection at the mic
# position was baked into the predicted sum (a spurious ~1125 Hz null) even
# though (2) VERIFY's own measured sum is adaptively reflection-gated
# (`_driver_response`) and never had that reflection to begin with — and the
# VERIFY tracking comparator's band never clamped to that adaptive gate's own
# validity floor (`gating.f_valid_floor_hz`), so sub-validity bins decided the
# verdict. Fix 1 (validity-floor clamp) and Fix 2 (gating-consistent
# `_aligned_branch_tf`) are exercised together below; the mechanism was also
# validated against the actual forensics hardware capture (run-8 a5:
# `analyze_program_capture` on the real WAV yields tracking
# rms=1.496/max=5.115 dB against the ≈1.50/5.12 forensics target — see the
# W6.9 commit message for the reproduction, not re-run here since the WAV/npz
# fixtures are hardware artifacts, not part of this hardware-free suite).


def _reflection_branch(peak: int, f_lo: float, f_hi: float, amp: float,
                        *, reflection_delay_s: float = 0.0, reflection_amp: float = 0.0,
                        n: int = 8192) -> np.ndarray:
    """A band-passed direct-arrival impulse, optionally with a genuine near
    reflection: a scaled, delayed COPY of the direct arrival's own shape
    (causal — zeroed before the reflection onset), the same construction
    ``gate_test.py`` (W6.9 forensics) used to reproduce the hardware desk
    bounce. ``reflection_delay_s`` must be ≥ ``gating.SEARCH_T_MIN_MS`` (0.5
    ms) to be detectable at all — a reflection that close to the direct
    arrival's own tail is structurally invisible to
    ``gating.detect_first_reflection``'s search window by design."""
    direct = _band_impulse(peak, f_lo, f_hi, amp, n=n)
    if reflection_amp == 0.0:
        return direct
    delay_samples = int(round(reflection_delay_s * SR))
    reflection = reflection_amp * np.roll(direct, delay_samples)
    reflection[:delay_samples] = 0.0
    return direct + reflection


def _old_fixed_window_branch_tf(full_ir: np.ndarray, n_fft: int):
    """Byte-for-byte the pre-W6.9 ``_aligned_branch_tf``: fixed
    ``IR_PRE_MS``/``IR_POST_MS`` window, no reflection gating."""
    peak_idx = int(np.argmax(np.abs(full_ir)))
    window = deconv.direct_arrival_window(
        full_ir, SR, direct_peak_idx=peak_idx,
        pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS,
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    return _complex_tf(ir, SR, n_fft=n_fft, calibration=None)


def _reflection_fixture(fc_hz: float, *, peak: int = 300, n: int = 8192):
    """Woofer branch with a genuine near reflection at +0.70 ms (a real
    ``gating.detect_first_reflection`` hit — comfortably inside its [0.5, 7]
    ms search window, standing in for the forensics' ~0.6-0.7 ms hardware
    desk bounce); tweeter branch clean. Returns
    ``(woofer_ir, tweeter_ir, n_fft)``."""
    woofer_ir = _reflection_branch(
        peak, 150.0, 6000.0, 1.0,
        reflection_delay_s=0.70e-3, reflection_amp=1.0, n=n,
    )
    tweeter_ir = _reflection_branch(peak, 300.0, 20000.0, 0.7, n=n)
    return woofer_ir, tweeter_ir, _n_fft_for(woofer_ir, tweeter_ir)


def test_aligned_branch_tf_applies_the_same_adaptive_gate_as_driver_response():
    """Fix 2, isolated: ``_aligned_branch_tf`` must reflection-gate exactly
    like ``_driver_response`` does, not use the fixed 65 ms window alone."""
    fc_hz = 2000.0
    woofer_ir, _tweeter_ir, n_fft = _reflection_fixture(fc_hz)

    freqs, W_new, fragment = _aligned_branch_tf(woofer_ir, SR, n_fft, calibration=None)
    assert fragment["floor_source"] == "measured_reflection"
    floor_hz = _gate_floor_hz(fragment)
    assert floor_hz is not None and floor_hz > fc_hz / 2.0  # gate is tighter than the nominal band

    _f2, W_old = _old_fixed_window_branch_tf(woofer_ir, n_fft)
    # Around the injected reflection's comb notch, the OLD fixed-window TF is
    # badly depressed while the NEW gated TF stays flat — the collapse the
    # forensics cited (a fixed-window null vs. a gated peak at the same bin).
    lo, hi = fc_hz / 2.0, fc_hz * 2.0
    band = (freqs >= lo) & (freqs <= hi)
    old_db = 20.0 * np.log10(np.maximum(np.abs(W_old[band]), 1e-12))
    new_db = 20.0 * np.log10(np.maximum(np.abs(W_new[band]), 1e-12))
    old_ripple = float(old_db.max() - old_db.min())
    new_ripple = float(new_db.max() - new_db.min())
    assert old_ripple > 5.0
    assert new_ripple < 1.0
    assert new_ripple < old_ripple - 4.0


def test_gating_consistent_candidate_removes_reflection_notch_end_to_end():
    """Fix 2, end to end: a fixed-window prediction bakes a real near
    reflection into a notch that fails VERIFY tolerance; the gating-
    consistent prediction is clean and the SAME real capture passes."""
    fc_hz = 2000.0
    lo, hi = fc_hz / 2.0, fc_hz * 2.0
    woofer_ir, tweeter_ir, n_fft = _reflection_fixture(fc_hz)

    alignment = AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
    )
    candidate, (pred_freqs, pred_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter", alignment, None,
    )
    # NEW: gating-consistent prediction is clean.
    assert candidate.predicted_ripple_db < 1.5

    # OLD: byte-for-byte the pre-fix fixed-window path bakes in a notch that
    # would fail the same ±1.5 dB VERIFY tolerance the run-7/8 bug tripped.
    freqs_old, W_old = _old_fixed_window_branch_tf(woofer_ir, n_fft)
    _f2, T_old = _old_fixed_window_branch_tf(tweeter_ir, n_fft)
    trim_w_old, trim_t_old, _lw, _lt = solve_branch_trims(freqs_old, W_old, T_old, fc_hz)
    old_ripple = _ripple_db(
        freqs_old, predicted_branch_sum(W_old, T_old, trim_w_old, trim_t_old, +1), lo, hi,
    )
    assert old_ripple > 5.0
    assert old_ripple > candidate.predicted_ripple_db + 4.0
    old_pred_db = 20.0 * np.log10(np.maximum(
        np.abs(predicted_branch_sum(W_old, T_old, trim_w_old, trim_t_old, +1)), 1e-12,
    ))

    # The REAL physical system (the reflection is reality — it doesn't get
    # gated away by wishing) is the raw time-domain sum of both branches at
    # the candidate's own solved trims; VERIFY captures this same system.
    g_w = 10.0 ** (candidate.trim_db["woofer"] / 20.0)
    g_t = 10.0 ** (candidate.trim_db["tweeter"] / 20.0)
    combined_ir = g_w * woofer_ir + alignment.polarity_sign * g_t * tweeter_ir

    prog = build_verify_program(fc_hz, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    mono = fftconvolve(pcm[:, 0], combined_ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(42).normal(0.0, 1e-5, cap.size)

    res_new = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=fc_hz, predicted_sum=(pred_freqs, pred_db)),
    )
    assert res_new.verify_tracking is not None
    assert res_new.verify_tracking["max_db_notch_excluded"] < 1.5  # NEW: tracking passes

    res_old = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=fc_hz, predicted_sum=(freqs_old, old_pred_db)),
    )
    assert res_old.verify_tracking is not None
    assert res_old.verify_tracking["max_db_notch_excluded"] > 1.5  # OLD: same capture, false fail


def test_validity_floor_clamp_excludes_only_sub_floor_divergence():
    """Fix 1: the VERIFY tracking comparator drops bins below THIS capture's
    own gate-derived validity floor. A predicted-sum divergence placed
    entirely below that floor must not move the gated numbers (though it
    still shows up in the raw ``*_full_band`` diagnostics); the identical
    divergence placed above the floor must still fail the gate."""
    fc_hz = 2000.0
    woofer_ir, tweeter_ir, n_fft = _reflection_fixture(fc_hz)
    alignment = AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
    )
    candidate, (pred_freqs, pred_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter", alignment, None,
    )
    g_w = 10.0 ** (candidate.trim_db["woofer"] / 20.0)
    g_t = 10.0 ** (candidate.trim_db["tweeter"] / 20.0)
    combined_ir = g_w * woofer_ir + alignment.polarity_sign * g_t * tweeter_ir

    prog = build_verify_program(fc_hz, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    mono = fftconvolve(pcm[:, 0], combined_ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(42).normal(0.0, 1e-5, cap.size)

    # This capture's OWN reflection gate clamps the tracking band above the
    # nominal Fc/2 — confirm the fixture actually exercises the clamp before
    # trusting the below/above assertions that depend on it.
    baseline = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=fc_hz, predicted_sum=(pred_freqs, pred_db)),
    )
    band_lo = baseline.verify_tracking["tracking_band_hz"][0]
    assert band_lo > fc_hz / 2.0

    def _predicted_with_bump(center_hz: float, *, width_octave: float = 0.06) -> tuple[np.ndarray, np.ndarray]:
        # A narrow (0.06-octave) log-Gaussian bump so it decays to
        # negligible well before the clamped band edge — a wide bump would
        # leak enough tail across the floor to confound the below/above
        # assertions below.
        with np.errstate(divide="ignore"):
            ratio = np.where(pred_freqs > 0, pred_freqs / center_hz, 1.0)
        bump = 10.0 * np.exp(-0.5 * (np.log2(ratio) / width_octave) ** 2)
        return pred_freqs, pred_db + bump

    below_center = band_lo * 0.75  # inside the nominal [Fc/2, 2Fc] band, below the clamped floor
    assert below_center > fc_hz / 2.0
    res_below = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=fc_hz, predicted_sum=_predicted_with_bump(below_center),
        ),
    )
    tracking_below = res_below.verify_tracking
    assert tracking_below["max_db_notch_excluded"] < 1.5  # excluded: not gated
    assert tracking_below["max_db_full_band"] > 5.0  # still visible as a diagnostic

    above_center = band_lo * 1.3  # comfortably above the clamped floor
    res_above = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=fc_hz, predicted_sum=_predicted_with_bump(above_center),
        ),
    )
    tracking_above = res_above.verify_tracking
    assert tracking_above["max_db_notch_excluded"] > 1.5  # not excluded: still gates


def test_build_candidate_raises_when_validity_floor_consumes_whole_band():
    """If a branch's reflection gate is tight enough that the clamped low
    edge reaches/exceeds the band's high edge, `solve_branch_trims`/`_ripple_db`
    raise on the now-empty mask rather than silently computing over nothing.
    This is deliberate: `jasper.web.correction_crossover_v2`'s existing
    catch-all seam already classifies an analyze-time ValueError as
    `internal_error` (its own comment names this exact case), so a
    degenerate floor surfaces through that EXISTING signal instead of a new,
    invented reason code."""
    fc_hz = 100.0  # hi = 200 Hz — easy for a close reflection's floor to exceed
    woofer_ir = _reflection_branch(
        300, 50.0, 500.0, 1.0,
        reflection_delay_s=1.5e-3, reflection_amp=1.0,  # floor ~ 1/0.0015 = 667 Hz > 200 Hz
    )
    tweeter_ir = _reflection_branch(300, 100.0, 500.0, 0.7)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    alignment = AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
    )
    with pytest.raises(ValueError):
        _build_candidate(
            woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter", alignment, None,
        )


def test_overlap_band_hz_clamps_to_true_driver_sweep():
    """Fix 1 (root cause): the SSOT overlap-band helper clamps the nominal
    [Fc/2, 2*Fc] band up/down to the REAL driver-sweep bounds, since a
    driver's MEASURE sweep only covers its own declared band — e.g. a
    tweeter sweep starting AT Fc means [Fc/2, Fc) is pure deconvolution
    noise for that branch, never a real measurement."""
    fc_hz = 2000.0
    # Tweeter excited only from Fc (its own MEASURE sweep starts at 2000 Hz,
    # the real-world root cause) — the nominal Fc/2=1000 Hz floor is noise
    # for that branch, so the helper clamps `lo` up to it.
    lo, hi = overlap_band_hz(fc_hz, tweeter_sweep_lo_hz=2000.0, woofer_sweep_hi_hz=6000.0)
    assert lo == pytest.approx(2000.0)
    assert hi == pytest.approx(4000.0)  # woofer's 6000 Hz ceiling doesn't bind here

    # Woofer's own sweep ceiling narrower than the nominal 2*Fc ⇒ hi clamps down.
    lo, hi = overlap_band_hz(fc_hz, tweeter_sweep_lo_hz=300.0, woofer_sweep_hi_hz=3000.0)
    assert lo == pytest.approx(1000.0)  # tweeter's 300 Hz doesn't bind
    assert hi == pytest.approx(3000.0)

    # No sweep-segment evidence (legacy callers) ⇒ byte-identical nominal band.
    assert overlap_band_hz(fc_hz) == (1000.0, 4000.0)


def test_build_candidate_threads_overlap_band_into_trim_and_ripple(monkeypatch):
    """Fix 1, consumer wiring: `_build_candidate` must compute lo/hi via the
    SAME SSOT `overlap_band_hz` helper (clamped to the true driver-sweep
    bounds) and pass that band into the ripple calculation — not silently
    keep computing its own unclamped [Fc/2, 2*Fc] locally. Spies on
    `_ripple_db` (rather than asserting on DSP output numbers, which are
    sensitive to windowing/gating details unrelated to this fix) to pin the
    actual band value `_build_candidate` used.

    #1667: the ripple-optimal trim solve calls `_ripple_db` once per
    scanned candidate trim (plus once more for the final predicted-ripple
    evidence), so this now asserts every recorded call used the expected
    band rather than pinning an exact call count — the count is an
    implementation detail of the scan width, the band is the SSOT
    invariant this test actually protects."""
    fc_hz = 2000.0
    woofer_ir = _band_impulse(300, 500.0, 6000.0, 1.0)
    tweeter_ir = _band_impulse(300, 300.0, 20000.0, 0.7)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    alignment = AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
    )
    seen_lo_hi = []
    real_ripple_db = program_analysis._ripple_db

    def _spy_ripple_db(freqs, magnitude, lo, hi):
        seen_lo_hi.append((lo, hi))
        return real_ripple_db(freqs, magnitude, lo, hi)

    monkeypatch.setattr(program_analysis, "_ripple_db", _spy_ripple_db)

    _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter", alignment, None,
        tweeter_sweep_lo_hz=fc_hz, woofer_sweep_hi_hz=3000.0,
    )
    # A clean (non-reflective) fixture never trips the branch-floor clamp, so
    # every ripple call's band is exactly the SSOT helper's output — proving
    # `_build_candidate` threads the sweep bounds through, not just accepts
    # and ignores them.
    expected_lo, expected_hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=fc_hz, woofer_sweep_hi_hz=3000.0,
    )
    assert seen_lo_hi  # the ripple-optimal scan + final evidence both called it
    assert all(entry == (expected_lo, expected_hi) for entry in seen_lo_hi)
    assert expected_lo == pytest.approx(fc_hz)  # lo clamped UP from the nominal Fc/2=1000
    assert expected_hi == pytest.approx(3000.0)  # hi clamped DOWN from the nominal 2*Fc=4000


# --------------------------------------------------------------------------- #
# PR-L3 — the level-match frame, and #1667's ripple-optimal trim solve
# --------------------------------------------------------------------------- #
#
# solve_branch_trims band-energy-averages |W| and |T|, which is a LEVEL match
# only when each branch is weighted symmetrically about Fc. It used to read
# both branches over the SHARED both-branches-excited overlap band, whose
# lower edge overlap_band_hz clamps UP to the tweeter's sweep floor — and a
# tweeter swept from Fc upward (the real JTS3 geometry) leaves [Fc, 2*Fc],
# entirely inside the woofer's own rolloff skirt. That measured skirt depth,
# not sensitivity, and shipped a ~10 dB-dark tweeter.
#
# #1667 tried to absorb it by changing the OBJECTIVE (solve_ripple_optimal_trim,
# a summed-ripple re-solve seeded by the band-average). PR-L3's offline replay
# of the archived JTS3 captures showed that recovered only 2.1-4.1 dB of a
# 10.9-13.1 dB error, because on the same one-sided band the summed ripple is
# the tweeter's own ripple and barely responds to the tweeter's gain. The frame
# is now fixed at the source — each branch is read on its OWN side of Fc — and
# the ripple polish runs only where its band straddles Fc.


def _lr_pair_irs(fc_hz: float, order: float, *, delay: int = 300, n: int = 8192):
    """Woofer/tweeter IR pair whose FFT magnitude follows a textbook
    Linkwitz-Riley-shaped complementary lowpass/highpass split at ``fc_hz``
    (``order=4`` is a true LR4; smaller orders are a deliberately gentler
    slope, used to keep a fixture's band-average bias inside the sanity
    guard's margin for the "wiring works" test below). Both branches share
    the SAME linear-phase delay (a co-located pair, zero relative delay,
    normal polarity) — only the magnitude differs, mirroring
    `_band_impulse`'s own "shape a flat impulse spectrum" technique."""
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    with np.errstate(divide="ignore"):
        ratio = np.where(freqs > 0, (freqs / fc_hz) ** order, 0.0)
    w_mag = 1.0 / (1.0 + ratio)
    t_mag = ratio / (1.0 + ratio)
    imp = np.zeros(n)
    imp[delay] = 1.0
    spectrum = np.fft.rfft(imp)
    woofer_ir = np.fft.irfft(spectrum * w_mag, n)
    tweeter_ir = np.fft.irfft(spectrum * t_mag, n)
    return woofer_ir, tweeter_ir


def _lr4_branch_magnitudes(fc_hz: float, freqs: np.ndarray, tweeter_gain_db: float = 0.0):
    """A textbook LR4 complementary pair on ``freqs``, tweeter optionally hotter.

    An LR crossover's defining property: the two branches sum to EXACTLY flat
    magnitude at unity gain on both. So the analytically known level-match
    answer is ``trim_t = -tweeter_gain_db`` — the ground truth every test in
    this section measures its estimator against.
    """
    ratio4 = (freqs / fc_hz) ** 4
    return (
        1.0 / (1.0 + ratio4),
        (ratio4 / (1.0 + ratio4)) * 10.0 ** (tweeter_gain_db / 20.0),
    )


# The closed-form bias of band-averaging BOTH branches over the one-sided
# [Fc, 2*Fc] band on an ideal LR4 pair whose drivers have EQUAL sensitivity —
# the answer must be 0.0 dB and is not. Derived (not fitted) by
# test_one_sided_overlap_band_biases_the_level_match below; quoted in
# solve_branch_trims' docstring and in the PR-L3 write-up, so it lives here as
# a named constant rather than a magic number in one assertion.
ONE_SIDED_BAND_BIAS_DB = 10.59
# The residual bias of the SHIPPED estimator (each branch on its own side, at
# the full Fc±1-octave ratio) on that same ideal pair. Non-zero because a
# linear-frequency bin grid weights the wider upper half more; it shrinks as
# the ratio narrows and is ~20x smaller than what it replaced.
MIRRORED_HALVES_BIAS_DB = 0.54


def test_one_sided_overlap_band_biases_the_level_match():
    """PR-L3 root cause, in closed form. Two EQUAL-sensitivity drivers behind
    an ideal LR4 pair have a true level-match answer of exactly 0.0 dB. Read
    over the one-sided ``[Fc, 2*Fc]`` band that ``overlap_band_hz`` produces
    when the tweeter's sweep starts AT Fc, a band-power average reports the
    tweeter ~10.6 dB hot — it is measuring the woofer's crossover skirt, not
    its sensitivity. That number is the frame defect that put the archived
    JTS3 tweeter trims 10.9-13.1 dB below the same analysis's own fit frame.

    Widening back to the nominal symmetric ``[Fc/2, 2*Fc]`` band is NOT the
    fix: it is still biased (a linear-frequency bin grid over a log-asymmetric
    interval), and on real hardware it would average the tweeter over bins it
    was never excited in. Reading each branch on its own side is."""
    fc_hz = 2000.0
    freqs = np.linspace(1.0, 24000.0, 262_144)
    W, T = _lr4_branch_magnitudes(fc_hz, freqs)

    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=fc_hz, woofer_sweep_hi_hz=4.0 * fc_hz,
    )
    assert (lo, hi) == pytest.approx((2000.0, 4000.0))  # asymmetric: clamps to [Fc, 2*Fc]

    def gap_db(w_band, t_band) -> float:
        w_db = 20.0 * np.log10(np.maximum(np.abs(W), 1e-12))
        t_db = 20.0 * np.log10(np.maximum(np.abs(T), 1e-12))
        return _band_average_db(freqs, t_db, *t_band) - _band_average_db(
            freqs, w_db, *w_band
        )

    # The defect: one shared, one-sided band.
    assert gap_db((lo, hi), (lo, hi)) == pytest.approx(ONE_SIDED_BAND_BIAS_DB, abs=0.02)
    # Not fixed by widening to the nominal band either.
    nominal = (fc_hz / 2.0, fc_hz * 2.0)
    assert gap_db(nominal, nominal) == pytest.approx(3.03, abs=0.02)
    # The shipped estimator: each branch on its own side of Fc.
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs, W, T, fc_hz,
        woofer_span_hz=(fc_hz / 4.0, fc_hz * 2.0),
        tweeter_span_hz=(fc_hz / 2.0, fc_hz * 4.0),
    )
    assert trim_w == pytest.approx(0.0)  # woofer is the quieter (unattenuated) branch
    assert trim_t == pytest.approx(-MIRRORED_HALVES_BIAS_DB, abs=0.02)
    assert abs(trim_t) < ONE_SIDED_BAND_BIAS_DB / 10.0


@pytest.mark.parametrize("tweeter_gain_db", [0.0, 6.0, 10.8, 25.2])
def test_solve_branch_trims_recovers_a_known_sensitivity_gap(tweeter_gain_db):
    """The estimator's contract: on an ideal LR4 pair, the solved tweeter trim
    is the negated sensitivity gap, to within the fixed
    ``MIRRORED_HALVES_BIAS_DB`` residual — and that residual is INDEPENDENT of
    the gap, which is what makes it a frame property rather than a fudge.
    10.8 dB is JTS3's real padded gap (25.2 dB datasheet, -14.4 dB L-pad);
    25.2 dB is the bare gap the broken frame used to land on."""
    fc_hz = 2000.0
    freqs = np.linspace(1.0, 24000.0, 262_144)
    W, T = _lr4_branch_magnitudes(fc_hz, freqs, tweeter_gain_db=tweeter_gain_db)
    _trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs, W, T, fc_hz,
        woofer_span_hz=(fc_hz / 4.0, fc_hz * 2.0),
        tweeter_span_hz=(fc_hz / 2.0, fc_hz * 4.0),
    )
    assert trim_t == pytest.approx(
        -(tweeter_gain_db + MIRRORED_HALVES_BIAS_DB), abs=0.02
    )


def test_branch_level_bands_hz_are_mirrored_about_fc_and_clamped():
    """The band pair is log-symmetric about Fc by construction, capped at the
    nominal octave, and narrowed by whichever branch's own span binds first."""
    fc = 2000.0
    wide_w, wide_t = (150.0, 4000.0), (2000.0, 20000.0)
    (w_lo, w_hi), (t_lo, t_hi) = branch_level_bands_hz(
        fc, woofer_span_hz=wide_w, tweeter_span_hz=wide_t,
    )
    assert (w_lo, w_hi, t_lo, t_hi) == pytest.approx((1000.0, 2000.0, 2000.0, 4000.0))
    # Whichever OUTER edge binds first sets the shared ratio, keeping the two
    # halves mirror-symmetric even when only one branch is the constraint.
    (w_lo, w_hi), (t_lo, t_hi) = branch_level_bands_hz(
        fc, woofer_span_hz=(1500.0, 4000.0), tweeter_span_hz=wide_t,
    )
    assert (w_lo, w_hi, t_lo, t_hi) == pytest.approx((1500.0, 2000.0, 2000.0, 2666.6667))
    (w_lo, w_hi), (t_lo, t_hi) = branch_level_bands_hz(
        fc, woofer_span_hz=wide_w, tweeter_span_hz=(2000.0, 2500.0),
    )
    assert (w_lo, w_hi, t_lo, t_hi) == pytest.approx((1600.0, 2000.0, 2000.0, 2500.0))
    assert w_hi == t_lo == fc  # the two halves always meet exactly at Fc
    # Defaults are the nominal Fc +/- one octave.
    assert branch_level_bands_hz(fc) == ((1000.0, 2000.0), (2000.0, 4000.0))


def test_branch_level_bands_hz_refuse_a_branch_that_does_not_reach_fc():
    """The INNER edges are load-bearing too (PR-L3 review S2). The halves meet
    AT Fc, so the woofer's span must reach up to Fc and the tweeter's down to
    it. Nothing ties a declared driver band to the chosen Fc, so a tweeter
    swept from 2.5 kHz under a 2 kHz Fc is representable — and would put
    500 Hz of never-excited deconvolution noise inside the tweeter's own half,
    the exact failure this estimator exists to avoid. There is no level match
    in that capture, so it raises into the ``internal_error`` seam rather than
    shipping a guessed trim."""
    fc = 2000.0
    with pytest.raises(ValueError, match="tweeter span .* does not reach Fc"):
        branch_level_bands_hz(
            fc, woofer_span_hz=(150.0, 4000.0), tweeter_span_hz=(2500.0, 20000.0),
        )
    with pytest.raises(ValueError, match="woofer span .* does not reach Fc"):
        branch_level_bands_hz(
            fc, woofer_span_hz=(150.0, 1800.0), tweeter_span_hz=(2000.0, 20000.0),
        )
    # A degenerate span that starts (woofer) or ends (tweeter) AT Fc leaves
    # that branch no half at all, and is refused by the same two checks —
    # which is why the ratio below them needs no third guard: once both spans
    # straddle Fc it exceeds 1 by construction.
    with pytest.raises(ValueError, match="woofer span .* does not reach Fc"):
        branch_level_bands_hz(
            fc, woofer_span_hz=(fc, 4000.0), tweeter_span_hz=(2000.0, 20000.0),
        )
    with pytest.raises(ValueError, match="tweeter span .* does not reach Fc"):
        branch_level_bands_hz(
            fc, woofer_span_hz=(150.0, fc), tweeter_span_hz=(fc, fc),
        )
    # Touching Fc exactly from the correct side IS enough — the halves only
    # need to meet there.
    assert branch_level_bands_hz(
        fc, woofer_span_hz=(1000.0, fc), tweeter_span_hz=(fc, 4000.0),
    ) == ((1000.0, fc), (fc, 4000.0))


def test_build_candidate_refuses_a_tweeter_swept_above_fc():
    """The refusal reaches the candidate builder, not just the band helper:
    a declared tweeter sweep that starts ABOVE Fc raises out of
    ``_build_candidate`` (PR-L3 review S2), which the web layer's existing
    catch-all classifies as ``internal_error``."""
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _lr_pair_irs(fc_hz, order=4)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    with pytest.raises(ValueError, match="does not reach Fc"):
        _build_candidate(
            woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
            _candidate_alignment(), None,
            tweeter_sweep_lo_hz=2500.0, woofer_sweep_hi_hz=4.0 * fc_hz,
            woofer_sweep_lo_hz=150.0, tweeter_sweep_hi_hz=20000.0,
        )


# --------------------------------------------------------------------------- #
# PR-L4 item 1 — the inter-driver realized-level assertion
# --------------------------------------------------------------------------- #
#
# The archived 2026-07-27 JTS3 session `d5b171fa81a5` (the profile the owner
# heard as ~10 dB dark), as its own evidence store recorded it. The candidate
# JSON is laptop-durable at captures/iloud-comparison-20260727/trim-replay/
# data/candidate.json and quoted in FORENSICS-SYNTHESIS.md; these three numbers
# are all the assertion needs, so they live here rather than behind a corpus
# env gate.
#
#   linearization.tweeter.target_level_db   -7.2433   (the fit's own plateau)
#   linearization.woofer.target_level_db   -21.1250
#   analysis.trim_db.tweeter               -22.6392   (the trim that shipped)
#
# The fit drives each branch to its own target, so the realized inter-driver
# level is (target_t + trim_t) - (target_w + trim_w) = -8.7575 dB: the tweeter
# nearly 9 dB below the woofer, across its whole passband, which is what the
# owner heard. NOTHING in the pipeline computed that subtraction.
JTS3_20260727_TARGET_LEVEL_DB = {"woofer": -21.1250, "tweeter": -7.2433}
JTS3_20260727_SHIPPED_TRIM_DB = {"woofer": 0.0, "tweeter": -22.6392}
JTS3_20260727_REALIZED_GAP_DB = -8.7575


def _flat_branch_pair(freqs: np.ndarray, fc_hz: float, levels_db):
    """A branch pair already fitted flat at ``levels_db``, with LR4 skirts.

    The state ``realized_branch_level_match`` grades: each branch linearized to
    a constant over its own passband, still carrying its own side of the
    crossover. Reproduces a fitted candidate without needing its capture.
    """
    ratio4 = (freqs / fc_hz) ** 4
    lp, hp = 1.0 / (1.0 + ratio4), ratio4 / (1.0 + ratio4)
    return (
        (lp * 10.0 ** (levels_db["woofer"] / 20.0)).astype(complex),
        (hp * 10.0 ** (levels_db["tweeter"] / 20.0)).astype(complex),
    )


def test_realized_level_match_refuses_the_archived_jts3_dark_profile():
    """PR-L4 item 1 against the profile that shipped dark.

    Rebuilt from the archived candidate's own recorded numbers (above): the fit
    put the tweeter at -7.24 dB and the woofer at -21.13 dB in one frame, then
    the trim took another 22.64 dB off the tweeter. The pair the speaker ran
    was therefore ~8.8 dB apart, and the assertion this test pins is the one
    stage that would have said so — every other comparator in the chain either
    compared the speaker to itself or was disconnected.
    """
    fc_hz = 2000.0
    freqs = np.linspace(1.0, 24000.0, 262_144)
    W, T = _flat_branch_pair(freqs, fc_hz, JTS3_20260727_TARGET_LEVEL_DB)

    match = realized_branch_level_match(
        freqs, W, T, fc_hz,
        trim_w_db=JTS3_20260727_SHIPPED_TRIM_DB["woofer"],
        trim_t_db=JTS3_20260727_SHIPPED_TRIM_DB["tweeter"],
    )
    assert match.matched is False
    # The archived arithmetic, recovered by the estimator to within its own
    # known linear-bin systematic (MIRRORED_HALVES_BIAS_DB).
    assert match.difference_db == pytest.approx(
        JTS3_20260727_REALIZED_GAP_DB, abs=MIRRORED_HALVES_BIAS_DB + 0.1
    )
    assert abs(match.difference_db) > REALIZED_LEVEL_MATCH_TOLERANCE_DB
    # Signed, because "the tweeter is 9 dB down" and "9 dB up" are opposite
    # defects and only one of them is what the household heard.
    assert match.difference_db < 0.0


def test_realized_level_match_accepts_the_pair_the_fixed_trim_produces():
    """The same drivers with a trim that actually levels them: the assertion
    passes, and it passes with real margin rather than by a hair.

    The frame this trim comes from is PR-L3's: the fit frame put the two
    branches 13.88 dB apart on that session, so trimming the tweeter by that
    much is the level match. The shipped trim was 22.64 dB — 8.8 dB of frame
    error, the gap the test above catches."""
    fc_hz = 2000.0
    freqs = np.linspace(1.0, 24000.0, 262_144)
    W, T = _flat_branch_pair(freqs, fc_hz, JTS3_20260727_TARGET_LEVEL_DB)
    fit_frame_gap_db = (
        JTS3_20260727_TARGET_LEVEL_DB["tweeter"]
        - JTS3_20260727_TARGET_LEVEL_DB["woofer"]
    )

    match = realized_branch_level_match(
        freqs, W, T, fc_hz, trim_w_db=0.0, trim_t_db=-fit_frame_gap_db,
    )
    assert match.matched is True
    assert abs(match.difference_db) <= MIRRORED_HALVES_BIAS_DB + 0.1


def test_realized_level_match_reads_each_branch_on_its_own_side_of_fc():
    """The bands are :func:`branch_level_bands_hz`', not a second derivation —
    the SSOT property that keeps this check from becoming a rival estimator
    with a rival answer (PR-L3's whole lesson, applied to its own successor)."""
    fc_hz = 2000.0
    freqs = np.linspace(1.0, 24000.0, 65_536)
    W, T = _lr4_branch_magnitudes(fc_hz, freqs)
    spans = {"woofer_span_hz": (300.0, 3000.0), "tweeter_span_hz": (1500.0, 12000.0)}

    match = realized_branch_level_match(
        freqs, W.astype(complex), T.astype(complex), fc_hz,
        trim_w_db=0.0, trim_t_db=0.0, **spans,
    )
    assert (match.woofer_band_hz, match.tweeter_band_hz) == branch_level_bands_hz(
        fc_hz, **spans
    )
    # Mirrored about Fc, and neither half crosses it.
    assert match.woofer_band_hz[1] == fc_hz == match.tweeter_band_hz[0]


def test_realized_level_match_refuses_a_span_that_does_not_reach_fc():
    """Same raise surface as the trim solve it wraps — never a guessed verdict
    on a geometry that has no level match in it."""
    fc_hz = 2000.0
    freqs = np.linspace(1.0, 24000.0, 8192)
    W, T = _lr4_branch_magnitudes(fc_hz, freqs)
    with pytest.raises(ValueError, match="does not reach Fc"):
        realized_branch_level_match(
            freqs, W.astype(complex), T.astype(complex), fc_hz,
            trim_w_db=0.0, trim_t_db=0.0,
            woofer_span_hz=(300.0, 3000.0), tweeter_span_hz=(2500.0, 12000.0),
        )


def test_realized_level_match_tolerance_clears_the_measured_frame_noise():
    """The tolerance's own floor argument, pinned rather than asserted in prose.

    PR-L3 measured the level frame against the fit frame on all five archived
    cdhorn MEASURE captures and they agreed to 1.30 dB worst case, on top of a
    known +0.54 dB linear-bin systematic. A tolerance at or below that would
    put a finding on honest sessions — a refusal on them, when this bound was
    derived and the check still refused — and either way a bar the honest
    spread crosses says nothing. This pins the margin that keeps it from doing
    so.
    """
    worst_measured_frame_disagreement_db = 1.30
    assert REALIZED_LEVEL_MATCH_TOLERANCE_DB > worst_measured_frame_disagreement_db
    assert REALIZED_LEVEL_MATCH_TOLERANCE_DB >= 2.0 * MIRRORED_HALVES_BIAS_DB
    # ...and its ceiling argument: a level error of this size already puts each
    # side of Fc at the tightest spec band's tolerance, so nothing the spec
    # calls a pass is reported as mislevelled here.
    from jasper.active_speaker.flat_spec import SPEC_BANDS

    assert REALIZED_LEVEL_MATCH_TOLERANCE_DB == pytest.approx(
        2.0 * SPEC_BANDS[0][2]
    )


def test_solve_ripple_optimal_trim_recovers_lr4_pair_from_a_biased_seed():
    """#1667 core regression, re-seated on the geometry where the ripple
    polish still runs (PR-L3): a band that straddles Fc. A textbook LR4 pair
    sums to EXACTLY flat magnitude at unity gain on both branches, so the
    analytically known optimum is ``trim_t = 0.0`` dB, and the objective must
    find it from a deliberately over-attenuating seed.

    The seed is supplied explicitly rather than taken from
    ``solve_branch_trims``, which no longer produces a badly biased one — the
    -8 dB below is the shape of the bias the old shared-band call used to
    hand it, kept so the objective itself stays verified against it.

    The LR4 bowl near 0.0 dB is WIDE relative to
    ``RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB``, so flat-minimum regularization
    (architect follow-up) legitimately pulls the DEFAULT-regularized result
    back toward the seed — by design, trading a negligible amount of extra
    ripple for session-to-session stability. This test therefore asserts on
    RIPPLE (within epsilon of the sharp optimum), not on the regularized TRIM
    value, and separately pins the SHARP (epsilon effectively disabled)
    optimum at the exact analytic 0.0 dB truth."""
    fc_hz = 2000.0
    freqs = np.geomspace(200.0, 8000.0, 2000)
    W, T = _lr4_branch_magnitudes(fc_hz, freqs)

    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=fc_hz / 2.0, woofer_sweep_hi_hz=4.0 * fc_hz,
    )
    assert (lo, hi) == pytest.approx((1000.0, 4000.0))  # straddles Fc
    biased_seed = -8.0

    # Sharp optimum: epsilon effectively disabled, recovers the
    # analytically known 0.0 dB truth to within one scan step
    # (RIPPLE_TRIM_SEARCH_STEP_DB = 0.1 dB) — exactly the pre-
    # regularization behavior.
    trim_t_sharp, ripple_sharp, seed = solve_ripple_optimal_trim(
        freqs, W, T, fc_hz, lo_hz=lo, hi_hz=hi,
        seed_trim_db=biased_seed, trim_w_db=0.0, sign=1,
        flat_minimum_epsilon_db=1e-9,
    )
    assert seed == biased_seed
    assert trim_t_sharp == pytest.approx(0.0, abs=0.15)
    assert ripple_sharp < 0.1  # near-perfect LR4 cancellation at the sharp optimum

    # Regularized (default epsilon): pulled toward the (very negative)
    # seed, away from 0.0 -- asserted on ripple, not the trim value.
    trim_t_reg, ripple_reg, _seed = solve_ripple_optimal_trim(
        freqs, W, T, fc_hz, lo_hz=lo, hi_hz=hi,
        seed_trim_db=biased_seed, trim_w_db=0.0, sign=1,
    )
    assert ripple_reg <= ripple_sharp + RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB
    assert trim_t_reg < trim_t_sharp  # pulled toward the very-negative seed
    assert trim_t_reg > biased_seed  # but nowhere near all the way back


def test_solve_ripple_optimal_trim_ties_break_toward_seed():
    """The exact-tie case, a special case of the broader flat-minimum
    epsilon-region rule (architect follow-up): a frequency-independent
    (flat) branch pair sums to a CONSTANT for every trim — no frequency-
    dependent magnitude on either side to create ripple — so every
    candidate in the scan window ties for minimum ripple (~0 exactly, an
    epsilon-region spanning the ENTIRE window). The selection rule must
    pick the seed itself, not drift to an arbitrary tied candidate."""
    freqs = np.linspace(500.0, 4000.0, 256)
    W = np.full_like(freqs, 1.0)
    T = np.full_like(freqs, 0.5)
    seed = -6.02  # an arbitrary, non-grid-aligned seed
    trim_t, ripple_db, returned_seed = solve_ripple_optimal_trim(
        freqs, W, T, 1600.0, lo_hz=800.0, hi_hz=3200.0,
        seed_trim_db=seed, trim_w_db=0.0, sign=1,
    )
    assert returned_seed == seed
    assert trim_t == pytest.approx(seed, abs=1e-9)
    assert ripple_db < 1e-6


def test_solve_ripple_optimal_trim_prefers_near_seed_shallow_bowl_over_far_global_min():
    """Flat-minimum regularization (architect follow-up, #1667): a
    synthetic shallow bowl with TWO distinct, well-separated, comparably-low
    ripple minima -- built from a flat background region plus a second
    region whose T is ~180 degrees out of phase with W (a genuine magnitude
    interference null). The null's OWN curve dips toward that
    cancellation and recovers on the far side, crossing the background
    region's level once on EACH side of the null -- exactly the
    multi-minimum shape real hardware gets from imperfect time alignment/
    comb-filtering, not a contrived edge case.

    The FAR minimum (~+12.6 dB) has objectively LOWER ripple than the NEAR
    one (~-2.3 dB, verified below to be the sharp/unregularized global
    minimum on this exact scan), but both are comfortably within
    ``RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB`` of each other. The regularized
    selection must prefer the NEAR one — closer to the band-average seed —
    trading the far candidate's marginally better flatness for
    session-to-session repeatability."""
    n_bg, n_null = 200, 200
    freqs = np.linspace(1000.0, 3000.0, n_bg + n_null)
    W = np.empty(freqs.size, dtype=complex)
    T = np.empty(freqs.size, dtype=complex)
    W[:n_bg] = 0.5
    T[:n_bg] = 0.15
    W[n_bg:] = 1.0
    T[n_bg:] = -0.5  # ~180 degrees out of phase from W -> an interference null
    lo, hi = float(freqs[0]), float(freqs[-1])

    seed = -3.28  # close to the near minimum, far from the far one

    # Sharp (epsilon effectively disabled): confirms the fixture's TRUE
    # global minimum is the NEAR basin, not the far one -- the premise the
    # rest of this test depends on.
    trim_sharp, ripple_sharp, _seed = solve_ripple_optimal_trim(
        freqs, W, T, 2000.0, lo_hz=lo, hi_hz=hi,
        seed_trim_db=seed, trim_w_db=0.0, sign=1, window_db=20.0,
        flat_minimum_epsilon_db=1e-9,
    )
    assert trim_sharp == pytest.approx(-2.28, abs=0.15)

    trim_reg, ripple_reg, seed_out = solve_ripple_optimal_trim(
        freqs, W, T, 2000.0, lo_hz=lo, hi_hz=hi,
        seed_trim_db=seed, trim_w_db=0.0, sign=1, window_db=20.0,
    )
    assert seed_out == seed
    # Stays in the NEAR basin (close to seed), nowhere near the far one.
    assert trim_reg < 5.0
    assert abs(trim_reg - seed) < abs(trim_reg - 12.6)
    # Within the promised epsilon budget of the true sharp optimum -- the
    # regularization traded distance for flatness within budget, not an
    # unbounded amount.
    assert ripple_reg <= ripple_sharp + RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB

    # The far basin's own best discrete candidate is a GENUINE, comparable
    # alternative (also within epsilon of the global min) -- confirms this
    # is a real two-option test, not one where the far option was
    # accidentally excluded by the search window or grid.
    far_summed = predicted_branch_sum(W, T, 0.0, 12.6, 1)
    far_ripple = _ripple_db(freqs, far_summed, lo, hi)
    assert far_ripple <= ripple_sharp + RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB
    assert far_ripple < ripple_reg  # objectively flatter, yet regularization declines it


def test_solve_ripple_optimal_trim_never_exceeds_physical_attenuation_bounds():
    """A degenerate 'flat reference branch' scenario has NO interior ripple
    minimum: attenuating the only-varying branch toward silence is always
    'more flat' against a perfectly flat reference (nothing on the
    reference side to trade off against), so an unconstrained scan would
    walk all the way to a physically invalid net-gain trim. The search must
    clamp to [RIPPLE_TRIM_MIN_DB, RIPPLE_TRIM_MAX_DB] and never return a
    value outside it, regardless of which direction the (degenerate,
    monotonic) objective wants to walk — this is the #1667 implementation
    bug the search-window clamp fixes (a real 'MeasuredCrossoverCandidate
    attenuation_out_of_range' failure was reproduced via this exact shape
    before the clamp landed)."""
    freqs = np.linspace(500.0, 4000.0, 256)
    W = np.full_like(freqs, 1.0)  # perfectly flat reference
    T = 1.0 + 0.3 * np.sin(2 * np.pi * (freqs - 500.0) / 700.0)  # a varying branch

    # A seed near the physical ceiling: the (degenerate, monotonic)
    # objective wants to walk further toward 0 dB / positive gain, but must
    # never cross it. window_db is deliberately huge to exercise the
    # physical clamp rather than the ordinary search window.
    trim_t, _ripple, _seed = solve_ripple_optimal_trim(
        freqs, W, T, 1600.0, lo_hz=800.0, hi_hz=3200.0,
        seed_trim_db=-0.05, trim_w_db=0.0, sign=1, window_db=100.0,
    )
    assert RIPPLE_TRIM_MIN_DB <= trim_t <= RIPPLE_TRIM_MAX_DB

    # A seed near the attenuation floor: same clamp, the other side.
    trim_t2, _ripple2, _seed2 = solve_ripple_optimal_trim(
        freqs, W, T, 1600.0, lo_hz=800.0, hi_hz=3200.0,
        seed_trim_db=-59.95, trim_w_db=0.0, sign=1, window_db=100.0,
    )
    assert RIPPLE_TRIM_MIN_DB <= trim_t2 <= RIPPLE_TRIM_MAX_DB


def _candidate_alignment() -> AlignmentEstimate:
    return AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
    )


# Sweep bounds whose overlap band STRADDLES Fc (tweeter swept from Fc/2), the
# geometry where the ripple polish still runs, vs. the real JTS3 ONE-SIDED
# geometry (tweeter swept from Fc). Both give the level match the same
# per-branch spans — that is the point: the level match no longer depends on
# where the tweeter sweep starts, because it never reads the tweeter below Fc.
def _straddling_sweeps(fc_hz: float) -> dict[str, float]:
    return dict(
        tweeter_sweep_lo_hz=fc_hz / 2.0, woofer_sweep_hi_hz=4.0 * fc_hz,
        woofer_sweep_lo_hz=150.0, tweeter_sweep_hi_hz=20000.0,
    )


def _one_sided_sweeps(fc_hz: float) -> dict[str, float]:
    return dict(
        tweeter_sweep_lo_hz=fc_hz, woofer_sweep_hi_hz=4.0 * fc_hz,
        woofer_sweep_lo_hz=150.0, tweeter_sweep_hi_hz=20000.0,
    )


def test_build_candidate_applies_ripple_optimal_trim_with_band_average_evidence():
    """#1667 wiring (raw/program_analysis population): `_build_candidate`'s
    applied ``trim_db`` is the ripple-optimal solve, and
    ``trim_band_average_db`` preserves the band-average seed as evidence —
    both populated, and they differ whenever the ripple-optimal search
    genuinely moves the trim. The woofer/reference branch is never touched
    by the search, so its trim is identical in both mappings.

    Straddling sweeps (PR-L3): the polish only runs where its band can see
    both branches — the one-sided case is
    ``test_build_candidate_skips_the_ripple_polish_on_a_one_sided_band``."""
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _lr_pair_irs(fc_hz, order=4)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    candidate, _pred = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        _candidate_alignment(), None, **_straddling_sweeps(fc_hz),
    )
    assert candidate.trim_band_average_db is not None
    assert candidate.trim_db["woofer"] == candidate.trim_band_average_db["woofer"]
    assert candidate.trim_db["tweeter"] != candidate.trim_band_average_db["tweeter"]
    # The seed is now within ~0.6 dB of this fixture's analytic 0.0 dB truth,
    # so the polish only ever nudges — and stays inside the sanity guard
    # (otherwise it would have fallen back and the two would be equal).
    assert candidate.trim_band_average_db["tweeter"] == pytest.approx(
        -MIRRORED_HALVES_BIAS_DB, abs=0.1
    )
    assert candidate.trim_db["tweeter"] > candidate.trim_band_average_db["tweeter"]
    assert abs(
        candidate.trim_db["tweeter"] - candidate.trim_band_average_db["tweeter"]
    ) <= REALIZED_LEVEL_MATCH_TOLERANCE_DB
    assert candidate.ripple_polish_rejected_delta_db is None


def test_build_candidate_skips_the_ripple_polish_on_a_one_sided_band(caplog):
    """PR-L3: on the real JTS3 geometry (tweeter swept from Fc) the shared
    ripple band clamps to ``[Fc, 2*Fc]``, where the woofer is 20+ dB down its
    skirt — the summed ripple is the tweeter's own and cannot express the
    handoff LEVEL. Replayed on the archived 2026-07-25 run-5 MEASURE capture
    the objective moved an unbiased -12.368 dB seed 7.9 dB back down; a
    selector that cannot see the woofer must not set the woofer's handoff
    level, so it is skipped (disclosed), not guarded.

    The level match itself is unaffected by the one-sidedness — it reads each
    branch on its own side — so this candidate's trim equals the straddling
    fixture's seed."""
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.program_analysis")
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _lr_pair_irs(fc_hz, order=4)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    candidate, _pred = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        _candidate_alignment(), None, **_one_sided_sweeps(fc_hz),
    )
    assert candidate.trim_db == candidate.trim_band_average_db
    assert candidate.trim_db["tweeter"] == pytest.approx(
        -MIRRORED_HALVES_BIAS_DB, abs=0.1
    )
    assert "event=program_analysis.ripple_trim_skipped" in caplog.text
    assert "reason=ripple_band_one_sided" in caplog.text
    assert "event=program_analysis.ripple_trim_rejected" not in caplog.text


def test_build_candidate_logs_the_level_match_frame_ledger(caplog):
    """PR-L3 disclosure: every MEASURE analysis emits the level match's own
    inputs — both branch levels and the two bands they were read over — so a
    future frame defect is visible in the journal instead of only in a
    re-derivation months later. Read beside the per-role ``target_level_db``
    that ``correction.crossover_v2_linearization_giveback`` carries for the
    same capture."""
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.program_analysis")
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _lr_pair_irs(fc_hz, order=4)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    _candidate, _pred = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        _candidate_alignment(), None, **_one_sided_sweeps(fc_hz),
    )
    assert "event=program_analysis.branch_level_match" in caplog.text
    assert 'woofer_band_hz="(1000.0, 2000.0)"' in caplog.text
    assert 'tweeter_band_hz="(2000.0, 4000.0)"' in caplog.text
    assert "level_w_db=" in caplog.text
    assert "level_t_db=" in caplog.text


@pytest.mark.parametrize("excursion_db", [-4.0, 4.0])
def test_build_candidate_rejects_a_polish_the_level_gate_could_not_grade(
    caplog, monkeypatch, excursion_db,
):
    """The coupling, and it is the whole point of the guard now.

    A polish that moves the trim further than
    ``REALIZED_LEVEL_MATCH_TOLERANCE_DB`` is rejected back to the band-average
    seed, disclosed on the journal AND on the candidate. Both signs, because
    the bound is on the magnitude and a one-sided guard would let the other
    direction through.

    **Why this bound and not a separate one.** The excursion passes straight
    through to the committed pair as realized inter-driver level error, so a
    polish admitted past the gate's tolerance produces a round that cannot be
    graded as level-matched. The old bound was ``RIPPLE_TRIM_SANITY_MARGIN_DB``
    (6.0 dB) — DOUBLE the tolerance — which left a 3.0-6.0 dB dead band of
    admitted polishes that the realized-level gate was then guaranteed to
    report against. On jts3 the polish landed 3.9 dB into that band.

    **Mutation guard, and it is exact.** ``excursion_db = 4.0`` sits above 3.0
    and below 6.0. Restoring a 6.0 dB bound admits it, so ``trim_db`` no longer
    equals the seed and every assertion below fails. A bound anywhere in
    (3.0, 4.0] would also fail the sibling case that admits at 2.5.

    Driven by stubbing the solver rather than by a fixture. Before PR-L3 a
    plain LR4 pair fired this guard on every run, because the SEED was the
    broken thing; with an unbiased seed no physically plausible branch pair
    moves the objective that far (verified across echo/comb fixtures during
    PR-L3), so the honest way to keep the guard covered is to exercise the
    guard, not to contrive physics that no longer happens."""
    caplog.set_level(logging.WARNING, logger="jasper.audio_measurement.program_analysis")
    assert abs(excursion_db) > REALIZED_LEVEL_MATCH_TOLERANCE_DB
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _lr_pair_irs(fc_hz, order=4)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    monkeypatch.setattr(
        program_analysis,
        "solve_ripple_optimal_trim",
        lambda *a, **kw: (
            kw["seed_trim_db"] + excursion_db, 0.0, kw["seed_trim_db"],
        ),
    )
    candidate, _pred = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        _candidate_alignment(), None, **_straddling_sweeps(fc_hz),
    )
    # Rejected: the applied trim falls back to the band-average seed exactly.
    assert candidate.trim_db == candidate.trim_band_average_db
    assert "event=program_analysis.ripple_trim_rejected" in caplog.text
    assert f"tolerance_db={REALIZED_LEVEL_MATCH_TOLERANCE_DB}" in caplog.text
    assert f"rejected_delta_db={excursion_db}" in caplog.text
    # The candidate carries it too — without this the three ways `trim_db` can
    # equal the seed are indistinguishable downstream.
    assert candidate.ripple_polish_rejected_delta_db == pytest.approx(excursion_db)
    # The guard still has real teeth: the scan can reach further than the bound.
    assert RIPPLE_TRIM_SEARCH_WINDOW_DB > REALIZED_LEVEL_MATCH_TOLERANCE_DB


def test_build_candidate_admits_a_polish_the_level_gate_can_grade(
    caplog, monkeypatch,
):
    """The other side of the coupled bound, and the half a one-sided test
    would miss: a polish INSIDE the tolerance is applied, not rejected.

    2.5 dB is above nothing in particular except the point — it is a real
    excursion that the old 6.0 dB bound also admitted, so this asserts the
    change narrowed the guard rather than closing it. The candidate's
    rejection field stays ``None``, which is what lets a reader tell an
    admitted polish from a discarded one.

    **Mutation guard.** Lowering the bound below 2.5 dB rejects this and fails
    the first two assertions."""
    caplog.set_level(logging.WARNING, logger="jasper.audio_measurement.program_analysis")
    excursion_db = 2.5
    assert abs(excursion_db) < REALIZED_LEVEL_MATCH_TOLERANCE_DB
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _lr_pair_irs(fc_hz, order=4)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    monkeypatch.setattr(
        program_analysis,
        "solve_ripple_optimal_trim",
        lambda *a, **kw: (
            kw["seed_trim_db"] + excursion_db, 0.0, kw["seed_trim_db"],
        ),
    )
    candidate, _pred = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        _candidate_alignment(), None, **_straddling_sweeps(fc_hz),
    )
    assert candidate.trim_db["tweeter"] == pytest.approx(
        candidate.trim_band_average_db["tweeter"] + excursion_db
    )
    assert candidate.ripple_polish_rejected_delta_db is None
    assert "event=program_analysis.ripple_trim_rejected" not in caplog.text


# --------------------------------------------------------------------------- #
# drive-gain invariance — the frame every MEASURE consumer works in
# --------------------------------------------------------------------------- #
#
# ``_deconvolve_window`` builds its deconvolution reference with
# ``segment_stimulus(segment)``, which regenerates the sweep at
# ``amplitude_dbfs=segment.gain_db`` — so the reference carries that segment's
# own PROGRAMMED digital gain. The inversion in
# ``deconv.regularized_deconvolution_full`` is
#
#     H = Y·conj(X) / (|X|² + ε·max|X|²)
#
# which is EXACTLY invariant under X → aX, Y → aY: numerator and denominator
# both scale by a². Every MEASURE output is therefore a per-unit-drive transfer
# function, which is what lets CHECK's gain plan drive the woofer and tweeter
# at different digital levels without biasing the measurement.
#
# That invariance is load-bearing and, until this test, asserted only in prose
# — canonically in docs/historical/crossover-measurement-v2-campaign-record.md gotcha #22, which
# traced it while solving MEASURE's level per driver (#1825): the reading that
# looked like a drive delta was a sensitivity delta, precisely because the
# drive divides out. (docs/historical/linearization-campaign-2026-07.md:95-96 states the
# same thing for the trim-vs-fit frame comparison.) Its consumer —
# ``linearization_fit.driver_core_level_db`` (reads ``DriverResponse``'s own
# magnitude curve) — assumes it silently. Build the
# reference at unit amplitude instead, or normalize the stimulus before
# inversion, and every one of them inherits an inter-driver level error of
# exactly the woofer/tweeter gain difference: the same failure SHAPE as the
# ~10 dB-dark tweeter in ``solve_branch_trims``' own history, and just as
# invisible. Verified sensitive — stubbing a unit-amplitude reference moves
# both quantities below by the full 10 dB skew.

# The skew: enough to be unmistakable, and the size of a real gain-plan spread.
DRIVE_GAIN_SKEW_DB = -10.0
# Float rounding through the deconvolution FFTs — largest OUT of band, where
# |H| is near the 1e-12 magnitude floor and the dB scale is ill-conditioned,
# not in the passbands the level frame actually reads. Measured worst case on
# this fixture is 1.1e-3 dB (in-band: 2.7e-5), so this leaves ~9x headroom
# above the noise while sitting three orders of magnitude below the skew a
# regression would introduce. Compared full-band on purpose: it is the
# stronger claim and it still clears with margin, so there is no arbitrary
# band choice to defend.
DRIVE_GAIN_INVARIANCE_TOL_DB = 0.01


def _measure_analysis_at_gains(gain_plan, *, woofer_ir, tweeter_ir):
    """Analyze one noise-free MEASURE capture rendered at ``gain_plan``.

    Noise-free on purpose: drive-gain invariance is exact algebra, not a
    statistical claim. Additive noise at a FIXED level does not scale with the
    drive, so a quieter driver would genuinely measure worse — a real SNR
    property of the capture, but one that would only put a noise floor under
    the thing this pins.
    """
    prog = build_measure_program(
        gain_plan, _roles(), sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(prog, woofer_ir=woofer_ir, tweeter_ir=tweeter_ir, noise=0.0)
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        geometry=MeasurementGeometry(),  # d=0 ⇒ no parallax
    )
    return prog, cap, res


def _segment_layout(program):
    return [(s.segment_id, s.start_sample, s.n_samples, s.channel) for s in program.segments]


def _sweep_t_peak_dbfs(result):
    (loc,) = [x for x in result.locations if x.segment_id == "sweep_t"]
    return loc.peak_dbfs


def test_measure_analysis_is_invariant_to_the_programmed_drive_gain():
    """The same drivers measured twice, the tweeter driven 10 dB quieter the
    second time, must produce the same transfer functions and the same trims.

    Both captures come from the SAME synthetic plants, so every difference
    between the two analyses is attributable to the gain plan alone. What the
    assertions cover is exactly what the shared level frame reads: the
    candidate's ``trim_band_average_db`` seed and the applied ``trim_db``, plus
    each ``DriverResponse.magnitude_db`` curve that ``driver_core_level_db``
    takes its core-passband median from.
    """
    woofer_ir = _band_impulse(200, 150.0, 6000.0, 1.0)
    tweeter_ir = _band_impulse(225, 300.0, 20000.0, 0.7)
    even_gains = {"woofer": -11.0, "tweeter": -11.0}
    skewed_gains = {"woofer": -11.0, "tweeter": -11.0 + DRIVE_GAIN_SKEW_DB}

    prog_even, cap_even, even = _measure_analysis_at_gains(
        even_gains, woofer_ir=woofer_ir, tweeter_ir=tweeter_ir,
    )
    prog_skew, cap_skew, skew = _measure_analysis_at_gains(
        skewed_gains, woofer_ir=woofer_ir, tweeter_ir=tweeter_ir,
    )

    # Premise, made explicit: gain_db scales a stimulus segment's PCM and
    # changes nothing else — the sweep metadata (and so the whole schedule,
    # and so every deconvolution anchor) is identical between the two runs.
    assert _segment_layout(prog_skew) == _segment_layout(prog_even)
    assert prog_skew.total_samples == prog_even.total_samples
    # ...and the drive really did change, by the full skew, so nothing below
    # is passing vacuously.
    assert not np.allclose(cap_even, cap_skew)
    assert _sweep_t_peak_dbfs(skew) == pytest.approx(
        _sweep_t_peak_dbfs(even) + DRIVE_GAIN_SKEW_DB, abs=0.01
    )

    # The two level-frame inputs the candidate carries.
    for label, even_map, skew_map in (
        ("trim_band_average_db",
         even.candidate.trim_band_average_db, skew.candidate.trim_band_average_db),
        ("trim_db", even.candidate.trim_db, skew.candidate.trim_db),
    ):
        assert even_map is not None and skew_map is not None
        assert set(even_map) == set(skew_map) == {"woofer", "tweeter"}
        for role in sorted(even_map):
            assert skew_map[role] == pytest.approx(
                even_map[role], abs=DRIVE_GAIN_INVARIANCE_TOL_DB
            ), (
                f"{label}[{role}] moved with the drive gain: "
                f"{even_map[role]:.4f} → {skew_map[role]:.4f} dB"
            )

    # ...and the per-driver curves the fit reads its core level off.
    assert [r.role for r in skew.driver_responses] == [r.role for r in even.driver_responses]
    for r_even, r_skew in zip(even.driver_responses, skew.driver_responses, strict=True):
        assert np.array_equal(r_even.freqs_hz, r_skew.freqs_hz)
        worst_db = float(np.max(np.abs(
            np.asarray(r_skew.magnitude_db) - np.asarray(r_even.magnitude_db)
        )))
        assert worst_db <= DRIVE_GAIN_INVARIANCE_TOL_DB, (
            f"{r_even.role} magnitude_db moved {worst_db:.4f} dB with the "
            "drive gain — the deconvolution reference is no longer carrying "
            "the segment's programmed gain"
        )

    # The tolerance has teeth: a normalized reference shifts both quantities
    # by the FULL skew, three orders of magnitude past it.
    assert DRIVE_GAIN_INVARIANCE_TOL_DB < abs(DRIVE_GAIN_SKEW_DB) / 100.0


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #


def test_a_two_branch_measure_still_requires_the_fc_prior():
    # ``crossover_fc_hz=None`` is legal for a ONE-branch program (a speaker
    # with no crossover). A two-branch one still raises: every pair
    # quantity is derived from the corner, so a caller that forgot it must not
    # get a candidate-free analysis that reads like an honest absence.
    prog = build_measure_program({"woofer": -11.0, "tweeter": -13.0}, _roles())
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
    )
    with pytest.raises(ValueError):
        analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())


def test_rate_mismatch_is_rejected():
    prog = build_verify_program(FC_HZ, sweep_s=0.6)
    cap = np.zeros(10_000)
    with pytest.raises(ValueError):
        analyze_program_capture(prog, cap, 44_100)


# --------------------------------------------------------------------------- #
# PR-L3 — real-hardware corpus: the frame the fix was diagnosed on
# --------------------------------------------------------------------------- #
#
# The 2026-07-24/25 JTS3 cdhorn MEASURE captures are gitignored and
# laptop-durable, so this skips cleanly in CI. Root, skip gate and the
# era-exact sweep anchor come from tests/_flat_lin_corpus.py, shared with
# tests/test_spatial_combine.py (this module became the corpus's second reader
# on 2026-07-27). The era-exact program parameters below are that module's —
# it is the authority on what these captures were played under.

# What the SHIPPED (pre-PR-L3) pipeline persisted for run 5, read off the
# session's own archived candidate JSON (``run5_candidate.json``): a tweeter
# band-average trim of -26.5088 dB against a fit frame (``target_level_db``
# tweeter -6.9678, woofer -20.4118) only 13.4440 dB apart. The 13.06 dB
# disagreement between those two frames is the defect; the offline replay
# reproduced both to 4 decimal places before anything was changed.
L3_RUN5_BROKEN_TRIM_DB = -26.5088
L3_RUN5_FIT_FRAME_GAP_DB = 13.4440

# The run-5 session's own declared topology, which is also the shape #1870's
# 2026-07-30 field session failed on: a woofer swept to 4000 Hz and a tweeter
# swept from 2000, against a 2000 Hz LR4. The woofer's declared span therefore
# reaches an octave into its own stopband — the #1929 premise, on real bytes.
CDHORN_RUN5_FC_HZ = 2000.0
CDHORN_RUN5_BANDS_HZ = {"woofer": (150.0, 4000.0), "tweeter": (2000.0, 20000.0)}
CDHORN_RUN5_DRIVER_CLASS = {"woofer": "unknown", "tweeter": "compression_horn"}


def _cdhorn_run5_analysis(monkeypatch):
    """The archived run-5 MEASURE capture, analyzed era-exactly.

    Extracted so the two corpus regressions below read ONE deconvolution
    chain — the property tests/_flat_lin_corpus.py's docstring calls
    load-bearing, applied to this module's own second reader.
    """
    import glob
    import wave

    from jasper.audio_measurement.calibration import parse_calibration_text

    def load(path: str) -> np.ndarray:
        with wave.open(path) as handle:
            raw = handle.readframes(handle.getnframes())
            channels = handle.getnchannels()
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
        return samples[::2] if channels == 2 else samples

    # Era-exact, like every other corpus reader: the pins here came from the
    # analysis as PERFORMED, which read this UMIK-2 file on the parser's
    # "correction" default. The file is really a RESPONSE curve (the product
    # negates it since 2026-07-27); flipping the corpus to match is #1774, one
    # edit at the constant. Stated rather than inherited so it reads as a
    # decision — see tests/_flat_lin_corpus.py for the full cost.
    calibration = parse_calibration_text(
        CDHORN_CALIBRATION.read_text(),
        sign_convention=CORPUS_CALIBRATION_SIGN_CONVENTION,
    )
    program = build_measure_program(
        {"woofer": -6.0005, "tweeter": -15.0105},
        [
            RoleBand("woofer", 0, FrequencyBand(*CDHORN_RUN5_BANDS_HZ["woofer"])),
            RoleBand("tweeter", 1, FrequencyBand(*CDHORN_RUN5_BANDS_HZ["tweeter"])),
        ],
        leading_pilot_gains_db=(-16.0006, -6.0005),
        leading_pilot_role="woofer",
        courtesy_prelude=True,
    )
    capture = load(sorted(glob.glob(f"{CDHORN_ROOT}/*run5_measure.wav"))[-1])
    offset = sweep_anchored_global_offset(capture, program.segment("sweep_w"))
    real_global_offset = program_analysis._global_offset
    monkeypatch.setattr(
        program_analysis,
        "_global_offset",
        lambda prog, cap, rate: (offset, *real_global_offset(prog, cap, rate)[1:]),
    )
    analysis = analyze_program_capture(
        program, capture, SR,
        calibration=calibration,
        geometry=MeasurementGeometry(driver_spacing_m=0.254, mic_distance_m=1.0),
        priors=MeasurementPriors(crossover_fc_hz=CDHORN_RUN5_FC_HZ),
    )
    return analysis, program, capture, offset, calibration


def _cdhorn_run5_envelope(role, response):
    """The shipped fit path's envelope for one archived run-5 driver."""
    from jasper.active_speaker.linearization_envelope import compose_envelope

    band = CDHORN_RUN5_BANDS_HZ[role]
    seed = compose_envelope(
        role, response, excited_band_hz=band, mic_tier="reference",
        driver_class=CDHORN_RUN5_DRIVER_CLASS[role], sigma_db=None,
    )
    return compose_envelope(
        role, response, excited_band_hz=band, mic_tier="reference",
        driver_class=CDHORN_RUN5_DRIVER_CLASS[role],
        sigma_db=np.full_like(seed.freqs_hz, 0.01),
    )


@requires_cdhorn
def test_level_match_frame_agrees_with_the_fit_frame_on_the_jts3_corpus(monkeypatch):
    """PR-L3 hardware regression, from the offline replay's own numbers.

    On the archived 2026-07-25 JTS3 run-5 MEASURE capture the level match now
    lands within ~1.1 dB of the SAME analysis's per-driver ``target_level_db``
    frame — the frame the 2026-07-27 forensics independently validated to
    0.17 dB against the spatial cloud. Before the fix the two disagreed by
    13.06 dB, and the trim that shipped (-26.5 dB) left the measured summed
    response 25.5 dB non-flat across 200 Hz - 16 kHz. Both frames are computed
    from the same complex branch TFs, so this comparison is invariant to the
    session's drive-gain plan (a wrong assumed gain scales a branch by a
    constant and cancels in the difference).

    Also pins the premise of the diagnosis: ``_aligned_branch_tf`` (the trim
    path) and ``_driver_response`` (the fit path) return byte-identical
    transfer functions — the "reference mismatch between the two paths"
    hypothesis was REFUTED, and no future change should quietly introduce
    one."""
    from jasper.active_speaker.linearization_fit import fit_driver_linearization

    analysis, program, capture, offset, calibration = _cdhorn_run5_analysis(monkeypatch)
    candidate = analysis.candidate
    assert candidate is not None
    responses = {r.role: r for r in analysis.driver_responses}

    # The two paths' transfer functions are the same numbers.
    epsilon = analysis.drift.epsilon_ppm / 1e6
    irs = {}
    for role, seg_id in (("woofer", "sweep_w"), ("tweeter", "sweep_t")):
        segment = program.segment(seg_id)
        irs[role] = _deconvolve_window(
            capture, segment, offset + segment.start_sample, SR, epsilon=epsilon,
        )[0]
    n_fft = _n_fft_for(irs["woofer"], irs["tweeter"])
    for role, ir in irs.items():
        _f, tf, _gate = _aligned_branch_tf(ir, SR, n_fft, calibration=calibration)
        # BYTE-identical, not merely close, and complex — the refuted claim was
        # a "reference mismatch between the two paths", which a phase-only
        # divergence would also be. `array_equal` on the complex arrays is the
        # assertion that actually says what was checked.
        assert np.array_equal(tf, responses[role].complex_tf)

    # The fit frame, from the shipped fit path on those same responses.
    fit_levels = {
        role: fit_driver_linearization(
            responses[role], _cdhorn_run5_envelope(role, responses[role]),
        ).target_level_db
        for role in CDHORN_RUN5_BANDS_HZ
    }
    fit_gap_db = fit_levels["tweeter"] - fit_levels["woofer"]
    assert fit_gap_db == pytest.approx(L3_RUN5_FIT_FRAME_GAP_DB, abs=0.01)

    trim_gap_db = -candidate.trim_band_average_db["tweeter"]
    assert trim_gap_db - fit_gap_db == pytest.approx(-1.08, abs=0.25)
    # ...and nowhere near the frame that shipped.
    assert candidate.trim_band_average_db["tweeter"] > L3_RUN5_BROKEN_TRIM_DB + 10.0

    # The applied trim leaves the MEASURED summed response near as flat as
    # this raw (uncorrected) pair can be: within 1 dB of the best achievable
    # over 200 Hz - 16 kHz, where the shipped trim was >10 dB worse than best.
    freqs = np.asarray(responses["woofer"].freqs_hz, dtype=float)
    W = np.asarray(responses["woofer"].complex_tf)
    T = np.asarray(responses["tweeter"].complex_tf)
    centers = [200.0 * 2 ** (n / 3) for n in range(19)]  # 200 Hz .. 16 kHz

    def summed_spread_db(trim_t_db: float) -> float:
        summed = predicted_branch_sum(
            W, T, 0.0, trim_t_db, analysis.alignment.polarity_sign
        )
        summed_db = 20.0 * np.log10(np.maximum(np.abs(summed), 1e-12))
        levels = [
            _band_average_db(freqs, summed_db, c / 2 ** (1 / 6), c * 2 ** (1 / 6))
            for c in centers
        ]
        return max(levels) - min(levels)

    best = min(summed_spread_db(t) for t in np.arange(-40.0, 0.01, 0.5))
    assert summed_spread_db(candidate.trim_db["tweeter"]) <= best + 1.0
    assert summed_spread_db(L3_RUN5_BROKEN_TRIM_DB) > best + 10.0


# What the two level estimators read on run 5 once the fit's median is taken
# off the declared CAPTURE span and put on the driver's radiating band
# (#1929). Both measured on the archived bytes by this test; the woofer's
# +2.09 dB is the real-hardware size of the effect #1809's comment measured
# at 1.66 dB on a synthetic and #1870's field session was refused over.
L1929_RUN5_CORE_SHIFT_DB = {"woofer": 2.0931, "tweeter": 0.5066}
L1929_RUN5_DISAGREEMENT_DB = {"declared_span": 1.0761, "radiating_band": 0.5104}


@requires_cdhorn
def test_the_radiating_band_core_level_converges_on_the_trim_frame(monkeypatch):
    """**#1929 hardware regression.** The frame gate exists to ask whether two
    INDEPENDENT measured estimates of one physical quantity — where these two
    drivers sit relative to each other — agree. On real bytes, taking the
    fit's median off the declared capture span and putting it on the driver's
    radiating band makes them agree **better**: 1.076 dB to 0.510 dB.

    That is the whole argument for the change, and it cannot be made on a
    synthetic. The declared spans here are the ones #1870's field session ran
    (woofer to 4000 Hz, tweeter from 2000, Fc 2000 LR4), so the woofer's own
    LR4 stopband occupies ~28% of its core-mask bins and drags its median
    2.09 dB below where the driver sits. The tweeter, declared FROM Fc, is
    contaminated only by the knee and moves 0.51 dB — the asymmetry is why
    the error does not cancel between the roles and lands on the gate.

    Direction is asserted before magnitude: whatever the corpus era, the
    radiating-band frame must not agree WORSE than the declared-span one.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, radiating_band_hz,
    )
    from jasper.active_speaker.linearization_fit import driver_core_level_db

    analysis, _program, _capture, _offset, _cal = _cdhorn_run5_analysis(monkeypatch)
    responses = {r.role: r for r in analysis.driver_responses}
    envelopes = {
        role: _cdhorn_run5_envelope(role, responses[role])
        for role in CDHORN_RUN5_BANDS_HZ
    }
    bands = {
        role: radiating_band_hz((
            CrossoverSection(
                fc_hz=CDHORN_RUN5_FC_HZ, order=4, highpass=(role == "tweeter"),
            ),
        ))
        for role in CDHORN_RUN5_BANDS_HZ
    }
    # The premise: the woofer really was swept an octave past where it
    # radiates, which is what a `measurement_band_hz` declaration is for.
    assert bands["woofer"][1] < CDHORN_RUN5_BANDS_HZ["woofer"][1]

    declared = {r: driver_core_level_db(responses[r], envelopes[r]) for r in envelopes}
    radiating = {
        r: driver_core_level_db(responses[r], envelopes[r], radiating_band_hz=bands[r])
        for r in envelopes
    }
    for role in envelopes:
        # Each driver reads HIGHER once its own stopband stops voting: the
        # sign is structural, the size is this speaker's.
        assert radiating[role] > declared[role], role
        assert radiating[role] - declared[role] == pytest.approx(
            L1929_RUN5_CORE_SHIFT_DB[role], abs=0.05
        ), role

    # ...and the two estimators converge. `trim_band_average_db` is the trim
    # solve's mirrored ±1-octave power average — the other instrument, on the
    # same bytes, which #1929 does not touch.
    trims = analysis.candidate.trim_band_average_db
    trim_gap_db = trims["tweeter"] - trims["woofer"]
    disagreement = {
        "declared_span": abs(
            (declared["tweeter"] - declared["woofer"]) + trim_gap_db
        ),
        "radiating_band": abs(
            (radiating["tweeter"] - radiating["woofer"]) + trim_gap_db
        ),
    }
    assert disagreement["radiating_band"] < disagreement["declared_span"]
    for frame, expected in L1929_RUN5_DISAGREEMENT_DB.items():
        assert disagreement[frame] == pytest.approx(expected, abs=0.05), frame
    # Both clear the gate on THIS session — it is a healthy capture, and the
    # point is the margin, not a flipped verdict (the flip is pinned end to
    # end on a synthetic in tests/test_crossover_v2_conductor.py).
    assert disagreement["declared_span"] < REALIZED_LEVEL_MATCH_TOLERANCE_DB


# --------------------------------------------------------------------------- #
# R18 — honest post-apply verification (issues #1868 / #1654)
# --------------------------------------------------------------------------- #


R18_FC_HZ = 2000.0
#: The JTS3 shape the 2026-08-05 checkpoint ran: the tweeter is swept FROM Fc,
#: so ``overlap_band_hz`` clamps VERIFY's tracking band up to 2000 Hz and the
#: sub-Fc half of the crossover region is structurally ungradeable by it.
#: ``tracking_band_lo_hz=2000.0`` in that session's ``verify_diag`` is the
#: journal-verified fact, and the only one quoted in these tests.
R18_OLD_TRACKING_BAND_HZ = (2000.0, 4000.0)
R18_LP4 = (CrossoverSection(fc_hz=R18_FC_HZ, order=4, highpass=False),)
R18_HP4 = (CrossoverSection(fc_hz=R18_FC_HZ, order=4, highpass=True),)


def _r18_transfers():
    """The candidate's committed crossover, as the host hands it to the kernel."""
    return {
        "woofer": functools.partial(crossover_response_complex, sections=R18_LP4),
        "tweeter": functools.partial(crossover_response_complex, sections=R18_HP4),
    }


def _r18_system(*, dip_center_hz=None, dip_depth_db=0.0):
    """A SYNTHETIC applied system: an ideal LR4 pair, optionally with a
    realization suck-out injected at ``dip_center_hz``.

    Synthetic and labelled as such — no number here is presented as the
    2026-08-05 hardware measurement. What IS reproduced from that session is
    the SHAPE of the blind spot: a defect centred below Fc, inside the
    crossover region, below the old band's floor.

    Returns ``(freqs, ir, system_db)``. The ideal pair sums FLAT by
    construction (LR4 lowpass + highpass is allpass), so ``system_db`` carries
    exactly the injected defect and the design target is exactly 0 dB.
    """
    n_fft = 8192
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SR)
    total = crossover_response_complex(
        freqs, R18_LP4
    ) + crossover_response_complex(freqs, R18_HP4)
    if dip_center_hz is not None:
        octaves = np.zeros_like(freqs)
        positive = freqs > 0.0
        octaves[positive] = np.log2(freqs[positive] / dip_center_hz)
        total = total * 10.0 ** (
            -dip_depth_db * np.exp(-((octaves / (1.0 / 6.0)) ** 2)) / 20.0
        )
    # Bulk delay so the impulse is causal inside the deconvolution window; the
    # analysis re-references the direct peak itself.
    bulk = np.exp(-1j * 2.0 * np.pi * freqs * 300.0 / SR)
    ir = np.fft.irfft(total * bulk, n_fft)
    return freqs, ir, 20.0 * np.log10(np.maximum(np.abs(total), 1e-12))


def _r18_capture(ir):
    prog = build_verify_program(R18_FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    return prog, np.concatenate([np.zeros(800), mono, np.zeros(5000)])


def _r18_verify(*, dip_center_hz=None, dip_depth_db=0.0, with_target=True):
    """Analyze one synthetic VERIFY capture of :func:`_r18_system`.

    ``predicted_sum`` is the system's OWN response — the worst case for the
    tracking comparator and exactly the shape #1868 is about: a model that
    reproduces the defect, so tracking structurally cannot fail on it.
    """
    freqs, ir, system_db = _r18_system(
        dip_center_hz=dip_center_hz, dip_depth_db=dip_depth_db
    )
    prog, cap = _r18_capture(ir)
    return analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=R18_FC_HZ,
            predicted_sum=(freqs, system_db),
            measure_excited_band_hz=(R18_FC_HZ, 6000.0),
            configured_crossover_response_by_role=(
                _r18_transfers() if with_target else None
            ),
            configured_polarity_sign_by_role={"woofer": 1, "tweeter": 1},
        ),
    )


def test_verify_reuses_one_measured_smoothing_for_both_graders(monkeypatch):
    """Tracking and absolute grade the same once-smoothed measured curve."""
    smoothed_inputs = []
    original = analysis_mod.smooth_fractional_octave

    def counted_smoothing(freqs, magnitude_db, fraction=48):
        smoothed_inputs.append(magnitude_db)
        return original(freqs, magnitude_db, fraction)

    monkeypatch.setattr(
        analysis_mod, "smooth_fractional_octave", counted_smoothing
    )
    result = _r18_verify()

    assert "not_evaluated" not in result.verify_absolute
    assert result.verify_tracking is not None
    # One measured call plus the independently smoothed prediction. The
    # absolute grader must consume the measured result already used above.
    assert len(smoothed_inputs) == 2
    assert sum(
        values is result.summed_response.magnitude_db
        for values in smoothed_inputs
    ) == 1


def test_crossover_region_band_reaches_below_the_tweeter_sweep_floor():
    """The band widening, isolated (#1654's revival for #1868).

    ``overlap_band_hz`` clamps UP to the tweeter's sweep floor because its
    consumers read that branch ALONE. The summed capture does not, so its
    honest region reaches the woofer's side of Fc — where a handoff null lands
    when the tweeter is swept from Fc.
    """
    old = overlap_band_hz(R18_FC_HZ, tweeter_sweep_lo_hz=R18_FC_HZ)
    assert old == R18_OLD_TRACKING_BAND_HZ

    new = crossover_region_band_hz(
        R18_FC_HZ, validity_floor_hz=357.0, radiated_band_hz=(150.0, 20_000.0),
    )
    assert new == (1000.0, 4000.0)
    assert new[0] < old[0]


def test_verify_absolute_grades_from_the_validity_floor_not_the_trusted_one():
    """The verdict band's lower edge is this capture's ``1/T`` floor.

    ``2.5/T`` is disclosed beside a verdict and never bounds one
    (``gating.TRUSTED_FLOOR_MULTIPLIER``). A 0.9 ms reflection shortens the
    gate until the floor — not ``Fc/2``, not the radiated band — is what binds
    the lower edge, so the two floors would give visibly different bands.
    """
    _freqs, ir, _db = _r18_system()
    echo_samples = int(round(0.9e-3 * SR))
    reflected = ir.copy()
    reflected[echo_samples:] += 0.5 * reflected[: reflected.size - echo_samples]
    prog, cap = _r18_capture(reflected)
    result = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=R18_FC_HZ,
            configured_crossover_response_by_role=_r18_transfers(),
            configured_polarity_sign_by_role={"woofer": 1, "tweeter": 1},
        ),
    )
    summed = result.summed_response
    lo, hi = result.verify_absolute["band_hz"]
    assert lo == pytest.approx(summed.validity_floor_hz)
    assert lo == pytest.approx(summed.gating["f_valid_floor_hz"])
    # The floor binds, and the trusted floor would have moved this edge.
    assert R18_FC_HZ / 2.0 < lo < summed.gating["f_trusted_hz"] < hi


def test_crossover_region_band_is_bounded_by_the_captures_own_evidence():
    """Never a frequency literal: an unradiated band or an empty intersection
    yields ``None``, never a span."""
    # Gate floor above the whole region ⇒ nothing this capture supports.
    assert crossover_region_band_hz(
        R18_FC_HZ, validity_floor_hz=9000.0, radiated_band_hz=(150.0, 20_000.0),
    ) is None
    # An absent floor or band is "the record does not say", never a default.
    assert crossover_region_band_hz(
        R18_FC_HZ, validity_floor_hz=None, radiated_band_hz=(150.0, 20_000.0),
    ) is None
    assert crossover_region_band_hz(
        R18_FC_HZ, validity_floor_hz=357.0, radiated_band_hz=None,
    ) is None
    assert crossover_region_band_hz(
        0.0, validity_floor_hz=357.0, radiated_band_hz=(150.0, 20_000.0),
    ) is None


def test_absolute_claim_sees_a_model_reproduced_dip_that_tracking_cannot():
    """THE headline (#1868). One capture, two graders, opposite answers.

    A synthetic 5 dB suck-out at 1700 Hz — inside the crossover region, below
    the old tracking band's 2000 Hz floor — that the prediction reproduces
    exactly. Tracking passes with two orders of magnitude to spare: the
    defect is in BOTH curves and outside the band it grades. The absolute
    claim grades against the candidate's own crossover instead, and reports it.
    """
    result = _r18_verify(dip_center_hz=1700.0, dip_depth_db=5.0)

    tracking = result.verify_tracking
    assert tracking is not None
    assert tuple(tracking["tracking_band_hz"]) == R18_OLD_TRACKING_BAND_HZ
    assert tracking["max_db_notch_excluded"] < 0.2  # shipped gate is 1.5 dB

    absolute = result.verify_absolute
    assert "not_evaluated" not in absolute
    assert absolute["band_hz"] == [1000.0, 4000.0]
    assert absolute["max_db"] > 3.0  # derived tolerance is 2.0 dB
    # A DIP: signed, and reported where it actually is.
    assert absolute["worst_db"] < -3.0
    assert 1650.0 < absolute["worst_hz"] < 1750.0


def test_absolute_claim_needs_the_widened_band_to_see_that_dip():
    """The widening is load-bearing, not decorative.

    Re-grade the SAME capture against the SAME target over the OLD
    [2000, 4000] band: a 1.7 kHz defect barely registers there. Revert the
    widening and the headline test above stops failing.
    """
    result = _r18_verify(dip_center_hz=1700.0, dip_depth_db=5.0)
    summed = result.summed_response
    target_db = np.zeros_like(summed.freqs_hz)  # ideal LR4 pair sums flat
    measured_db = analysis_mod.smooth_fractional_octave(
        summed.freqs_hz, summed.magnitude_db, 6,
    )
    _rms, old_band_max = analysis_mod.tracking_error_db(
        summed.freqs_hz, measured_db, target_db, R18_OLD_TRACKING_BAND_HZ,
    )
    assert old_band_max < 1.0  # under the 2.0 dB tolerance: would have passed
    assert result.verify_absolute["max_db"] > old_band_max + 2.5


def test_absolute_claim_is_quiet_on_a_speaker_that_meets_its_crossover():
    """No defect, no finding — an instrument that fired on a correct handoff
    would be worse than none."""
    result = _r18_verify()
    absolute = result.verify_absolute
    assert "not_evaluated" not in absolute
    assert absolute["max_db"] < 0.5
    assert result.verify_tracking["max_db_notch_excluded"] < 0.2


def test_absolute_claim_records_why_it_could_not_be_evaluated():
    """Not-evaluated carries its own reason — never an absent key a consumer
    could read as a pass."""
    assert _r18_verify(with_target=False).verify_absolute == {
        "not_evaluated": ABSOLUTE_NO_TARGET
    }
    _freqs, ir, _db = _r18_system()
    prog, cap = _r18_capture(ir)
    no_fc = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert no_fc.verify_absolute == {"not_evaluated": ABSOLUTE_NO_FC}


def test_absolute_claim_refuses_an_untrusted_crossover_region():
    """A region outside the capture's trusted band grades nothing and says so,
    never falling back to a nominal band."""
    _freqs, ir, _db = _r18_system()
    prog, cap = _r18_capture(ir)
    # Fc low enough that [Fc/2, 2Fc] lands entirely below the summed sweep's
    # own radiated floor (150 Hz), so the trusted intersection is empty.
    result = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=60.0,
            configured_crossover_response_by_role=_r18_transfers(),
            configured_polarity_sign_by_role={"woofer": 1, "tweeter": 1},
        ),
    )
    assert result.verify_absolute == {"not_evaluated": ABSOLUTE_NO_TRUSTED_BAND}


def test_absolute_target_carries_the_candidates_configured_polarity():
    """The target is the candidate's own crossover INCLUDING region polarity.

    Flip the tweeter's declared sign and the design target stops being flat, so
    a correctly-summing measurement reads as a large deviation. A target that
    dropped polarity would report the same number either way.
    """
    _freqs, ir, _db = _r18_system()
    prog, cap = _r18_capture(ir)

    def analyze(signs):
        return analyze_program_capture(
            prog, cap, SR,
            priors=MeasurementPriors(
                crossover_fc_hz=R18_FC_HZ,
                configured_crossover_response_by_role=_r18_transfers(),
                configured_polarity_sign_by_role=signs,
            ),
        ).verify_absolute

    assert analyze({"woofer": 1, "tweeter": 1})["max_db"] < 0.5
    assert analyze({"woofer": 1, "tweeter": -1})["max_db"] > 5.0
