# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure analysis of an excitation-program capture (crossover conductor W1).

Synthetic-fixture round-trips for
docs/crossover-measurement-productization-design.md §5.6. A "capture" is
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
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.frame_fit import fit_frame
from jasper.audio_measurement.program import (
    KIND_SWEEP,
    PHASE_MEASURE,
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
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_OK,
    AMBIENT_NONSTATIONARITY_DB,
    CAPTURE_BOUND_MARGIN_S,
    GAIN_MAX_DIGITAL_PEAK_DBFS,
    GCC_SNAP_RADIUS_PERIODS,
    GCC_UPSAMPLE,
    IR_POST_MS,
    IR_PRE_MS,
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
    RIPPLE_TRIM_SANITY_MARGIN_DB,
    RIPPLE_TRIM_SEARCH_WINDOW_DB,
    SWEEP_PEAK_TO_RMS_DB,
    AlignmentEstimate,
    MeasurementGeometry,
    MeasurementPriors,
    PilotObservation,
    _aligned_branch_tf,
    _band_average_db,
    _band_exclusive_pieces,
    _build_candidate,
    _complex_tf,
    _deconvolve_window,
    _gate_floor_hz,
    _gcc_local_peak_snap,
    _gcc_phat,
    _global_offset,
    _locate_segments,
    _n_fft_for,
    _peak_dbfs,
    _ripple_db,
    _solve_gain_plan,
    _sweep_occurrence_index,
    analysis_diagnostic_summary,
    analyze_program_capture,
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    branch_level_bands_hz,
    overlap_band_hz,
    predicted_branch_sum,
    realized_branch_level_match,
    solve_branch_trims,
    solve_ripple_optimal_trim,
    summed_model_residual_delay_us,
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

    return _finalize(PHASE_MEASURE, 2, segments, cursor)


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
    step, after = program_analysis._locate_discontinuity(prog, sweep_locs)
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
        baselines_ppm={},
        max_residual_samples=0.0,
        glitch_detected=False,
        discontinuity_samples=program_analysis.DISCONTINUITY_UNRESOLVED,
        discontinuity_after_segment="",
    )
    analysis = program_analysis.ProgramAnalysis(
        phase=PHASE_MEASURE, program_id="test", locations=(), drift=drift,
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


def test_gcc_phat_sign_convention():
    # A ≈ B shifted right by +lag: build st = sw delayed by 30 samples.
    base = _band_impulse(400, 800.0, 3200.0, 1.0, n=2048)
    sw = base
    st = np.roll(base, 30)  # tweeter LATER by 30 samples
    lag, sign, conf, at_edge = _gcc_phat(
        st, sw, sample_rate=SR, band_hz=(800.0, 3200.0),
        upsample=16, max_lag_samples=200,
    )
    assert lag == pytest.approx(30.0, abs=0.5)  # positive ⇒ st (tweeter) later
    assert sign == 1
    assert not at_edge
    # Inverting the tweeter flips the correlation sign.
    lag2, sign2, _conf2, _edge2 = _gcc_phat(
        -st, sw, sample_rate=SR, band_hz=(800.0, 3200.0),
        upsample=16, max_lag_samples=200,
    )
    assert sign2 == -1
    assert lag2 == pytest.approx(30.0, abs=0.5)


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
    global_lag, _sign, _conf, _edge = _gcc_phat(
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
    (``snapped_delay_us is None``), ``_build_candidate`` selects the bare anchor
    and records ``snap_found=False`` / ``snap_delta_us=0`` — the physical
    plausibility rail then still gates that anchor downstream (Fix 3).
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
    assert candidate.snap_found is False
    assert candidate.snap_delta_us == pytest.approx(0.0, abs=1e-9)
    assert candidate.anchor_delay_us == pytest.approx(-physical_gap_us, abs=1e-9)
    assert candidate.delay_us == pytest.approx(-physical_gap_us, abs=1e-9)


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
    assert candidate.snap_delta_us == pytest.approx(0.0, abs=1e-9)
    assert candidate.alignment_seed_ripple_db is not None
    # Flatness is evidence only: anchor == selection ⇒ zero improvement.
    assert candidate.flatness_improvement_db == pytest.approx(0.0, abs=1e-9)
    assert candidate.predicted_ripple_db < 0.1


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
    instrument the G1 capture-quality ceiling was calibrated against.

    Both curves are rebuilt here from the same public primitives, so this pins
    the exact residual (``selected − anchor``, i.e. ``snap_delta_us``) rather
    than merely "something changed".
    """
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _impulse_branches()
    n_fft = 16_384
    anchor_us = _IMPULSE_ANCHOR_US
    # A snap the aligner found 60 us off the anchor — inside the +/-(period/6)
    # radius at Fc (83.3 us), so this is a reachable production selection.
    snapped_us = anchor_us + 60.0
    alignment = AlignmentEstimate(
        delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=anchor_us, snapped_delay_us=snapped_us,
    )
    candidate, (pred_freqs, pred_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        alignment, None, alignment_delay_bounds_us=(0.0, 1000.0),
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
    be materially closer than the independently-aligned one it replaced,
    because the aligned one describes a delay no selection realizes.
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
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=anchor_us, snapped_delay_us=snapped_us,
    )
    candidate, (freqs, applied_model_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        alignment, None, alignment_delay_bounds_us=(0.0, 1000.0),
    )
    _f, W, _gw = _aligned_branch_tf(woofer_ir, SR, n_fft, calibration=None)
    _f2, T, _gt = _aligned_branch_tf(tweeter_ir, SR, n_fft, calibration=None)
    aligned_model_db = 20.0 * np.log10(np.maximum(np.abs(predicted_branch_sum(
        W, T, candidate.trim_db["woofer"], candidate.trim_db["tweeter"], 1,
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
    aligned_err = _centred_rms(aligned_model_db)
    # The loop CLOSES: on an exact frame the applied model is not merely
    # closer, it is the physical sum (0.0 dB to float precision).
    assert applied_err == pytest.approx(0.0, abs=1e-6)
    # And the model it replaced was materially wrong about the same speaker —
    # 0.750 dB rms here, against a VERIFY tracking tolerance of 1.5 dB. So this
    # is a real slice of the tracking budget, not a rounding-scale change.
    assert aligned_err > 0.5


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
    measured_global_offset, _first, _stimuli = _global_offset(prog, cap, SR)
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
    # capture-quality instrument the G1 ripple ceiling was calibrated on, which
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
    global_offset, _first, stimuli = _global_offset(prog, capture, SR)
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
    global_offset, _first, stimuli = _global_offset(chk, full_cap, SR)
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


def test_channel_map_fails_when_no_driver_played():
    """No pilot signal at all (pure noise capture) ⇒ channel-map still fails.
    Neither driver's own band ever rises above its ambient, so TARGET fails
    for both roles regardless of how quiet/loud the room noise floor is."""
    roles = _check_roles()
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)
    n = pcm.shape[0] + 500 + 5000
    cap = np.random.default_rng(31).normal(0.0, 3e-4, n)
    res = analyze_program_capture(chk, cap, SR, priors=MeasurementPriors())
    assert res.channel_map_ok is False
    for pilot in res.pilots:
        assert pilot.channel_map_ok is False


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
    refuse honest sessions; this pins the margin that keeps it from doing so.
    """
    worst_measured_frame_disagreement_db = 1.30
    assert REALIZED_LEVEL_MATCH_TOLERANCE_DB > worst_measured_frame_disagreement_db
    assert REALIZED_LEVEL_MATCH_TOLERANCE_DB >= 2.0 * MIRRORED_HALVES_BIAS_DB
    # ...and its ceiling argument: a level error of this size already puts each
    # side of Fc at the tightest spec band's tolerance, so nothing the spec
    # calls a pass is refused here.
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
    ) <= RIPPLE_TRIM_SANITY_MARGIN_DB


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


def test_build_candidate_ripple_trim_sanity_guard_falls_back_with_warning(
    caplog, monkeypatch,
):
    """The guard's contract: a ripple-optimal result implausibly far (>6 dB)
    from the seed is distrusted, falls back to the band-average, and logs a
    WARNING — never a silent wild trim.

    Driven by stubbing the solver rather than by a fixture. Before PR-L3 a
    plain LR4 pair fired this guard on every run, because the SEED was the
    broken thing; with an unbiased seed no physically plausible branch pair
    moves the objective that far (verified across echo/comb fixtures during
    PR-L3), so the honest way to keep the guard covered is to exercise the
    guard, not to contrive physics that no longer happens."""
    caplog.set_level(logging.WARNING, logger="jasper.audio_measurement.program_analysis")
    fc_hz = 2000.0
    woofer_ir, tweeter_ir = _lr_pair_irs(fc_hz, order=4)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    monkeypatch.setattr(
        program_analysis,
        "solve_ripple_optimal_trim",
        lambda *a, **kw: (
            kw["seed_trim_db"] - RIPPLE_TRIM_SANITY_MARGIN_DB - 1.0, 0.0,
            kw["seed_trim_db"],
        ),
    )
    candidate, _pred = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter",
        _candidate_alignment(), None, **_straddling_sweeps(fc_hz),
    )
    # Rejected: the applied trim falls back to the band-average seed exactly.
    assert candidate.trim_db == candidate.trim_band_average_db
    assert "event=program_analysis.ripple_trim_rejected" in caplog.text
    assert f"margin_db={RIPPLE_TRIM_SANITY_MARGIN_DB}" in caplog.text
    assert RIPPLE_TRIM_SEARCH_WINDOW_DB > RIPPLE_TRIM_SANITY_MARGIN_DB  # the guard has real teeth


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
# — canonically in docs/HANDOFF-crossover-measurement-v2.md gotcha 22, which
# traced it while solving MEASURE's level per driver (#1825): the reading that
# looked like a drive delta was a sensitivity delta, precisely because the
# drive divides out. (docs/linearization-integrity-plan.md:95-96 states the
# same thing for the trim-vs-fit frame comparison.) Its consumers —
# ``linearization_fit.driver_core_level_db`` (reads ``DriverResponse``'s own
# magnitude curve) and ``solve_shared_level_frame`` (reconciles those core
# levels against the candidate trims) — all assume it silently. Build the
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


def test_measure_requires_fc_prior():
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


@requires_cdhorn
def test_the_frame_gate_ruling_does_not_reach_a_session_whose_frames_agree(
    monkeypatch,
):
    """**#1866 hardware regression: the ruling is inert on an ordinary
    session.**

    The owner's 2026-07-30 frame-gate ruling adds a branch that fires only on
    a disagreement OVER ``LEVEL_FRAME_AGREEMENT_TOLERANCE_DB``. Run 5 is the
    corpus's healthy capture, so the branch must be unreachable on it — no
    finding is banked, nothing is published, and every number this module
    already pins on these bytes is untouched.

    Read this test as the negative control for the ruling: the change is
    supposed to be invisible to every session whose two estimators agree, and
    the archived bytes are the only place that claim can be made against real
    hardware rather than a fixture. It re-derives nothing — it asserts the
    ruling's own precondition against the values the test above measured, on
    the same analysis, so the two cannot drift into disagreeing about which
    session this is.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, radiating_band_hz,
    )
    from jasper.active_speaker.crossover_v2_flow import (
        LEVEL_FRAME_AGREEMENT_TOLERANCE_DB,
    )
    from jasper.active_speaker.linearization_fit import (
        driver_core_level_db, solve_shared_level_frame,
    )

    analysis, _program, _capture, _offset, _cal = _cdhorn_run5_analysis(monkeypatch)
    responses = {r.role: r for r in analysis.driver_responses}
    core_levels_db = {}
    for role in CDHORN_RUN5_BANDS_HZ:
        envelope = _cdhorn_run5_envelope(role, responses[role])
        core_levels_db[role] = driver_core_level_db(
            responses[role],
            envelope,
            radiating_band_hz=radiating_band_hz((
                CrossoverSection(
                    fc_hz=CDHORN_RUN5_FC_HZ, order=4, highpass=(role == "tweeter"),
                ),
            )),
        )
    frame = solve_shared_level_frame(
        core_levels_db, dict(analysis.candidate.trim_band_average_db)
    )
    disagreement_db = max(abs(v) for v in frame.offset_db.values())

    # The number the gate reads, on the archived bytes, is the one the test
    # above measured — so this session's verdict is the same one it always
    # was, and the ruling's branch is not entered.
    assert disagreement_db == pytest.approx(
        L1929_RUN5_DISAGREEMENT_DB["radiating_band"], abs=0.05
    )
    assert disagreement_db < LEVEL_FRAME_AGREEMENT_TOLERANCE_DB
    # With 2.49 dB of margin, this is not a session sitting on the threshold:
    # a change that moved the estimator enough to start banking findings on
    # healthy hardware would have to move it by more than four times the
    # residual #1929 left behind.
    assert LEVEL_FRAME_AGREEMENT_TOLERANCE_DB - disagreement_db > 2.0
