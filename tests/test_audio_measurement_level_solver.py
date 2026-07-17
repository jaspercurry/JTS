# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop measurement-level solver (W2.1 / punchlist #31).

Pins the 2026-07-16 jts3 regression numbers (see the module docstring on
``jasper.audio_measurement.level_solver``), each ceiling binding in
isolation, the refusal path, the ambient-stats event parser, and the SSOT
contract that the solver's own effective-peak ledger matches
``DriverSweepGeneratorPlan.effective_peak_dbfs`` exactly (no second,
drifting computation of the same ledger).
"""
from __future__ import annotations

import math

import pytest

from jasper.active_speaker.excitation_safety_plan import DriverSweepGeneratorPlan
from jasper.audio_measurement import level_solver
from jasper.audio_measurement.level_solver import (
    AmbientBand,
    LevelSolveRefusal,
    SolvedLevel,
    parse_ambient_stats_event,
    solve_level,
)
from jasper.audio_measurement.quality_model import DRIVER, QualityModel

# ---------------------------------------------------------------------------
# Regression: 2026-07-16 jts3 session numbers
# ---------------------------------------------------------------------------

_AMBIENT_BROADBAND_DBFS = -42.3
_MAX_EFFECTIVE_PEAK_DBFS = -8.0
_MAIN_VOLUME_CAP_DB = -3.0
_COMMISSIONING_BASELINE_DB = -5.0


def _solve(gain_map_db, admitted_band_hz, **overrides):
    kwargs = dict(
        gain_map_db=gain_map_db,
        admitted_band_hz=admitted_band_hz,
        commissioning_gain_baseline_db=_COMMISSIONING_BASELINE_DB,
        main_volume_cap_db=_MAIN_VOLUME_CAP_DB,
        max_effective_peak_dbfs=_MAX_EFFECTIVE_PEAK_DBFS,
        ambient_broadband_dbfs=_AMBIENT_BROADBAND_DBFS,
        model=DRIVER,
    )
    kwargs.update(overrides)
    return solve_level(**kwargs)


def test_regression_woofer_clears_target_with_main_volume_alone():
    solved = _solve(gain_map_db=1.9, admitted_band_hz=(40.0, 400.0))
    assert isinstance(solved, SolvedLevel)
    assert solved.predicted_worst_band_snr_db >= 26.0 - 1e-9
    assert solved.achieved_target is True
    # Selection rule: quietest that works. main_volume_db does the lifting;
    # commissioning_gain_db is untouched from its baseline.
    assert solved.commissioning_gain_db == pytest.approx(
        _COMMISSIONING_BASELINE_DB
    )
    assert solved.main_volume_db <= 0.0
    assert solved.main_volume_db < _MAIN_VOLUME_CAP_DB


def test_regression_tweeter_engages_commissioning_gain_master_pinned_at_cap():
    solved = _solve(gain_map_db=-16.4, admitted_band_hz=(2500.0, 20000.0))
    assert isinstance(solved, SolvedLevel)
    # Too insensitive to reach the target (floor + margin) even at every
    # ceiling's limit -- best effort: main_volume pinned at its cap,
    # commissioning_gain raised off baseline (never below it).
    assert solved.achieved_target is False
    assert solved.main_volume_db == pytest.approx(_MAIN_VOLUME_CAP_DB)
    assert solved.commissioning_gain_db > _COMMISSIONING_BASELINE_DB
    assert solved.commissioning_gain_db <= 0.0
    # Best effort still clears the bare floor (no margin) -- not a refusal.
    assert solved.predicted_worst_band_snr_db >= DRIVER.snr_warn_db
    assert solved.predicted_worst_band_snr_db < 26.0


# ---------------------------------------------------------------------------
# Pure band math: the LF-weighted fallback synthesis
# ---------------------------------------------------------------------------


def test_fallback_bands_apply_full_lf_margin_below_full_hz():
    solved = _solve(gain_map_db=0.0, admitted_band_hz=(40.0, 100.0))
    assert isinstance(solved, SolvedLevel)
    for band in solved.band_detail:
        assert band.ambient_dbfs == pytest.approx(
            _AMBIENT_BROADBAND_DBFS + level_solver.LF_AMBIENT_MARGIN_DB
        )


def test_fallback_bands_apply_no_lf_margin_above_zero_hz():
    solved = _solve(gain_map_db=0.0, admitted_band_hz=(4000.0, 16000.0))
    assert isinstance(solved, SolvedLevel)
    for band in solved.band_detail:
        assert band.ambient_dbfs == pytest.approx(_AMBIENT_BROADBAND_DBFS)


def test_fallback_bands_taper_between_corners():
    solved = _solve(gain_map_db=0.0, admitted_band_hz=(200.0, 2000.0))
    assert isinstance(solved, SolvedLevel)
    ambients = [band.ambient_dbfs for band in solved.band_detail]
    # Monotonically decreasing margin as frequency rises across the span.
    assert ambients == sorted(ambients, reverse=True)
    assert ambients[0] < _AMBIENT_BROADBAND_DBFS + level_solver.LF_AMBIENT_MARGIN_DB
    assert ambients[-1] > _AMBIENT_BROADBAND_DBFS


def test_worst_band_detail_matches_reported_worst_band_snr():
    solved = _solve(gain_map_db=1.9, admitted_band_hz=(40.0, 400.0))
    assert isinstance(solved, SolvedLevel)
    worst = max(solved.band_detail, key=lambda b: b.ambient_dbfs)
    assert worst.predicted_snr_db == pytest.approx(
        solved.predicted_worst_band_snr_db
    )


# ---------------------------------------------------------------------------
# Per-band ambient-stats input (bypasses LF-weighted synthesis)
# ---------------------------------------------------------------------------


def test_provided_ambient_bands_bypass_lf_weighting():
    bands = (
        AmbientBand(lo_hz=40.0, hi_hz=200.0, rms_dbfs=-50.0),
        AmbientBand(lo_hz=200.0, hi_hz=400.0, rms_dbfs=-55.0),
    )
    solved = solve_level(
        gain_map_db=1.9,
        admitted_band_hz=(40.0, 400.0),
        commissioning_gain_baseline_db=_COMMISSIONING_BASELINE_DB,
        main_volume_cap_db=_MAIN_VOLUME_CAP_DB,
        max_effective_peak_dbfs=_MAX_EFFECTIVE_PEAK_DBFS,
        ambient_broadband_dbfs=_AMBIENT_BROADBAND_DBFS,
        ambient_bands=bands,
        model=DRIVER,
    )
    assert isinstance(solved, SolvedLevel)
    reported = {round(b.ambient_dbfs, 2) for b in solved.band_detail}
    assert reported == {-50.0, -55.0}


def test_ambient_bands_outside_admitted_range_fall_back_to_broadband():
    bands = (AmbientBand(lo_hz=8000.0, hi_hz=16000.0, rms_dbfs=-20.0),)
    solved = solve_level(
        gain_map_db=1.9,
        admitted_band_hz=(40.0, 400.0),
        commissioning_gain_baseline_db=_COMMISSIONING_BASELINE_DB,
        main_volume_cap_db=_MAIN_VOLUME_CAP_DB,
        max_effective_peak_dbfs=_MAX_EFFECTIVE_PEAK_DBFS,
        ambient_broadband_dbfs=_AMBIENT_BROADBAND_DBFS,
        ambient_bands=bands,
        model=DRIVER,
    )
    assert isinstance(solved, SolvedLevel)
    # None of the out-of-range bands survive clipping -> synthesized
    # fallback bands (all within [40, 400) Hz) take over.
    assert all(b.lo_hz < 400.0 for b in solved.band_detail)


# ---------------------------------------------------------------------------
# Each ceiling binding in isolation
# ---------------------------------------------------------------------------


def test_main_volume_cap_binds_before_commissioning_gain_engages():
    """A required level main_volume_db alone (at its cap) cannot reach:
    volume pins at the cap, commissioning_gain partially absorbs the rest --
    landing strictly between its baseline and 0.0."""

    solved = solve_level(
        gain_map_db=0.0,
        admitted_band_hz=(1000.0, 4000.0),
        commissioning_gain_baseline_db=-10.0,
        main_volume_cap_db=-8.0,
        max_effective_peak_dbfs=0.0,
        ambient_broadband_dbfs=-36.0,
        model=DRIVER,
    )
    assert isinstance(solved, SolvedLevel)
    assert solved.main_volume_db == pytest.approx(-8.0)
    assert -10.0 < solved.commissioning_gain_db < 0.0


def test_max_effective_peak_ceiling_binds():
    """A driver-safety ceiling far tighter than the lever cap or mic-clip
    margin must cap the achieved level below what levers alone would allow."""

    loose_cap = solve_level(
        gain_map_db=0.0,
        admitted_band_hz=(1000.0, 4000.0),
        commissioning_gain_baseline_db=0.0,
        main_volume_cap_db=0.0,
        max_effective_peak_dbfs=-40.0,
        ambient_broadband_dbfs=-50.0,
        model=DRIVER,
    )
    assert isinstance(loose_cap, SolvedLevel)
    assert loose_cap.achieved_target is False
    # effective_peak_dbfs == amplitude(-12) + C + V must equal the ceiling
    # exactly when it is the binding constraint (amplitude fixed at -12).
    effective_peak = -12.0 + loose_cap.commissioning_gain_db + loose_cap.main_volume_db
    assert effective_peak == pytest.approx(-40.0)


def test_mic_clip_ceiling_binds():
    solved = solve_level(
        gain_map_db=20.0,
        admitted_band_hz=(2500.0, 20000.0),
        commissioning_gain_baseline_db=0.0,
        main_volume_cap_db=0.0,
        max_effective_peak_dbfs=0.0,
        ambient_broadband_dbfs=-30.0,
        model=DRIVER,
    )
    assert isinstance(solved, SolvedLevel)
    assert solved.achieved_target is False
    plan = DriverSweepGeneratorPlan(
        f1_hz=2500.0,
        f2_hz=20000.0,
        amplitude=10.0 ** (-12.0 / 20.0),
        duration_s=1.0,
        repeat_count=1,
        commissioning_gain_db=solved.commissioning_gain_db,
        main_volume_db=solved.main_volume_db,
    )
    predicted_mic_peak = plan.effective_peak_dbfs + 20.0 - (-12.0)
    assert predicted_mic_peak == pytest.approx(level_solver.MIC_CLIP_CEILING_DBFS)


# ---------------------------------------------------------------------------
# mic_clip_gain_map_db override (W2.2 clip-aware correction, hardware run 18)
# ---------------------------------------------------------------------------


def test_mic_clip_gain_map_db_none_matches_baseline_behavior():
    """Omitting the override reproduces the single-gain_map_db behavior
    exactly -- the default is a true no-op, not just "close"."""

    kwargs = dict(
        gain_map_db=20.0,
        admitted_band_hz=(2500.0, 20000.0),
        commissioning_gain_baseline_db=0.0,
        main_volume_cap_db=0.0,
        max_effective_peak_dbfs=0.0,
        ambient_broadband_dbfs=-30.0,
        model=DRIVER,
    )
    baseline = solve_level(**kwargs)
    explicit_none = solve_level(mic_clip_gain_map_db=None, **kwargs)
    assert isinstance(baseline, SolvedLevel) and isinstance(explicit_none, SolvedLevel)
    assert explicit_none == baseline


def test_mic_clip_gain_map_db_overrides_only_the_clip_ceiling():
    """A MEASURED gain more sensitive than the tone's gain_map_db tightens
    the mic-clip ceiling and lands a quieter chosen_sum -- but the SNR
    target math (predicted_worst_band_snr_db) still reads the ORIGINAL
    gain_map_db, not the override: the override is scoped to the clip
    safety ceiling only, per solve_level's own contract."""

    kwargs = dict(
        gain_map_db=20.0,
        admitted_band_hz=(2500.0, 20000.0),
        commissioning_gain_baseline_db=0.0,
        main_volume_cap_db=0.0,
        max_effective_peak_dbfs=0.0,
        ambient_broadband_dbfs=-30.0,
        model=DRIVER,
    )
    baseline = solve_level(**kwargs)
    overridden = solve_level(mic_clip_gain_map_db=22.0, **kwargs)
    assert isinstance(baseline, SolvedLevel) and isinstance(overridden, SolvedLevel)

    # The more-sensitive measured gain tightens the ceiling -> quieter.
    assert overridden.main_volume_db < baseline.main_volume_db

    # Reported SNR still comes from gain_map_db (20.0), not the override
    # (22.0) -- confirms the override never leaks into the SNR-target math.
    chosen_sum = overridden.main_volume_db + overridden.commissioning_gain_db
    worst_band_ambient = max(b.ambient_dbfs for b in overridden.band_detail)
    expected_snr = chosen_sum + 20.0 - worst_band_ambient
    assert overridden.predicted_worst_band_snr_db == pytest.approx(expected_snr)

    # And the mic-clip ceiling that actually bound this solve was computed
    # from the OVERRIDE (22.0), confirmed by reconstructing the predicted
    # peak with it landing exactly on MIC_CLIP_CEILING_DBFS.
    predicted_peak_via_override = chosen_sum + 22.0
    assert predicted_peak_via_override == pytest.approx(
        level_solver.MIC_CLIP_CEILING_DBFS
    )


def test_regression_run18_clip_deescalation_lands_predicted_peak_at_target():
    """W2.2 pinned regression: hardware run 18 (jts3). The woofer's 250 Hz
    level-lock tone measured gain_map_db=-0.1, predicting a safe sweep --
    the solve chose main_volume=-7.35/commissioning=0.0 (achieving 26 dB
    worst-band SNR, matching the earlier same-day regression). The mic
    ACTUALLY hit 0 dBFS (clipped) instead.

    De-escalating from that measured evidence -- CrossoverLevelLease's
    ambient-shift correction (drop_db, item 1) plus the driver's OWN
    measured chain gain replacing the tone's gain_map_db for the mic-clip
    ceiling (item 2, padded by CLIP_UNDERESTIMATE_ALLOWANCE_DB since a
    clipped reading understates the true peak) -- must land the corrected
    solve's predicted mic peak (evaluated with the driver's real, unpadded
    measured gain) at MIC_TARGET_PEAK_DBFS minus the allowance: the
    allowance is baked into HOW FAR to drop, not left over as slack, so the
    solve target itself (evaluated with the PADDED gain the solver was
    actually configured with) lands exactly on MIC_TARGET_PEAK_DBFS.
    """

    gain_map_db = -0.1
    ambient_broadband_dbfs = -41.45
    kwargs = dict(
        gain_map_db=gain_map_db,
        admitted_band_hz=(40.0, 400.0),
        commissioning_gain_baseline_db=0.0,
        main_volume_cap_db=-3.0,
        max_effective_peak_dbfs=-8.0,
        model=DRIVER,
    )

    # First (clipped) sweep -- reproduces the run-18 numbers exactly, so
    # this fixture's ambient is grounded in the same solve the hardware ran.
    first = solve_level(ambient_broadband_dbfs=ambient_broadband_dbfs, **kwargs)
    assert isinstance(first, SolvedLevel)
    old_chosen_sum = first.main_volume_db + first.commissioning_gain_db
    assert old_chosen_sum == pytest.approx(-7.35, abs=0.01)
    assert first.predicted_worst_band_snr_db == pytest.approx(26.0)

    measured_mic_peak_dbfs = 0.0  # clipped -- clamped to the conservative value

    # Item 1: the signed ambient-shift de-escalation.
    drop_db = (
        (measured_mic_peak_dbfs - level_solver.MIC_TARGET_PEAK_DBFS)
        + level_solver.CLIP_UNDERESTIMATE_ALLOWANCE_DB
    )
    assert drop_db == pytest.approx(15.0)

    # Item 2: the driver's own measured chain gain (padded), replacing
    # gain_map_db for the mic-clip ceiling. Mirrors
    # CrossoverLevelLease.record_measured_gain's conversion from
    # effective_peak_dbfs (the excitation ledger's field) to the
    # chosen_sum-relative convention gain_map_db itself uses.
    measured_gain_db = (
        measured_mic_peak_dbfs
        - old_chosen_sum
        + level_solver.CLIP_UNDERESTIMATE_ALLOWANCE_DB
    )
    assert measured_gain_db == pytest.approx(10.35, abs=0.01)

    second = solve_level(
        ambient_broadband_dbfs=ambient_broadband_dbfs - drop_db,
        mic_clip_gain_map_db=measured_gain_db,
        **kwargs,
    )
    assert isinstance(second, SolvedLevel)
    new_chosen_sum = second.main_volume_db + second.commissioning_gain_db
    assert new_chosen_sum == pytest.approx(old_chosen_sum - drop_db, abs=0.01)

    # Evaluated with the PADDED (stored) gain the solver used, the
    # de-escalated level's predicted mic peak lands exactly on the target.
    predicted_padded_peak = new_chosen_sum + measured_gain_db
    assert predicted_padded_peak == pytest.approx(
        level_solver.MIC_TARGET_PEAK_DBFS, abs=0.05
    )

    # Evaluated with the driver's TRUE (unpadded) measured gain, the
    # allowance shows up as extra headroom below the target -- the
    # de-escalation errs quieter, never re-clips.
    unpadded_gain_db = measured_gain_db - level_solver.CLIP_UNDERESTIMATE_ALLOWANCE_DB
    predicted_true_peak = new_chosen_sum + unpadded_gain_db
    assert predicted_true_peak == pytest.approx(
        level_solver.MIC_TARGET_PEAK_DBFS - level_solver.CLIP_UNDERESTIMATE_ALLOWANCE_DB,
        abs=0.05,
    )
    assert predicted_true_peak < level_solver.MIC_TARGET_PEAK_DBFS

    # A second, IDENTICAL clip at this de-escalated level is not something
    # solve_level itself can refuse (it always answers what's asked) -- the
    # "second identical solve impossible" guarantee lives in the bounded
    # write-count on CrossoverLevelLease.record_solve_correction (see
    # tests/test_correction_crossover_backend_level_solve.py).


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_refusal_when_bare_floor_unreachable_even_at_max_levers():
    refused = solve_level(
        gain_map_db=-30.0,
        admitted_band_hz=(2500.0, 20000.0),
        commissioning_gain_baseline_db=_COMMISSIONING_BASELINE_DB,
        main_volume_cap_db=_MAIN_VOLUME_CAP_DB,
        max_effective_peak_dbfs=_MAX_EFFECTIVE_PEAK_DBFS,
        ambient_broadband_dbfs=_AMBIENT_BROADBAND_DBFS,
        model=DRIVER,
    )
    assert isinstance(refused, LevelSolveRefusal)
    assert refused.code == level_solver.REFUSAL_ROOM_TOO_NOISY
    assert refused.failing_band_hz[0] >= 2500.0
    assert refused.failing_band_hz[1] <= 20000.0
    assert refused.required_db == pytest.approx(DRIVER.snr_warn_db)
    assert refused.available_db < refused.required_db


def test_refusal_never_reports_a_band_outside_the_admitted_range():
    refused = solve_level(
        gain_map_db=-60.0,
        admitted_band_hz=(1000.0, 2000.0),
        commissioning_gain_baseline_db=0.0,
        main_volume_cap_db=-3.0,
        max_effective_peak_dbfs=-8.0,
        ambient_broadband_dbfs=-40.0,
        model=DRIVER,
    )
    assert isinstance(refused, LevelSolveRefusal)
    assert 1000.0 <= refused.failing_band_hz[0] < refused.failing_band_hz[1] <= 2000.0


def test_solved_level_and_refusal_to_dict_are_json_shaped():
    solved = _solve(gain_map_db=1.9, admitted_band_hz=(40.0, 400.0))
    assert isinstance(solved, SolvedLevel)
    payload = solved.to_dict()
    assert set(payload) == {
        "main_volume_db",
        "commissioning_gain_db",
        "predicted_worst_band_snr_db",
        "band_detail",
        "achieved_target",
    }
    assert isinstance(payload["band_detail"], list)

    refused = solve_level(
        gain_map_db=-30.0,
        admitted_band_hz=(2500.0, 20000.0),
        commissioning_gain_baseline_db=_COMMISSIONING_BASELINE_DB,
        main_volume_cap_db=_MAIN_VOLUME_CAP_DB,
        max_effective_peak_dbfs=_MAX_EFFECTIVE_PEAK_DBFS,
        ambient_broadband_dbfs=_AMBIENT_BROADBAND_DBFS,
        model=DRIVER,
    )
    assert isinstance(refused, LevelSolveRefusal)
    assert set(refused.to_dict()) == {
        "code",
        "failing_band_hz",
        "required_db",
        "available_db",
    }


# ---------------------------------------------------------------------------
# SSOT contract: the solver's ledger matches DriverSweepGeneratorPlan exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gain_map_db,admitted_band_hz",
    [
        (1.9, (40.0, 400.0)),
        (-16.4, (2500.0, 20000.0)),
        (0.0, (1000.0, 4000.0)),
    ],
)
def test_ssot_effective_peak_matches_driver_sweep_generator_plan(
    gain_map_db, admitted_band_hz
):
    """The solver never invents a second effective-peak computation: feeding
    its output straight into DriverSweepGeneratorPlan reproduces the SAME
    ledger admission will later validate."""

    solved = _solve(gain_map_db=gain_map_db, admitted_band_hz=admitted_band_hz)
    assert isinstance(solved, SolvedLevel)
    plan = DriverSweepGeneratorPlan(
        f1_hz=admitted_band_hz[0],
        f2_hz=admitted_band_hz[1],
        amplitude=10.0 ** (-12.0 / 20.0),
        duration_s=1.0,
        repeat_count=1,
        commissioning_gain_db=solved.commissioning_gain_db,
        main_volume_db=solved.main_volume_db,
    )
    assert plan.effective_peak_dbfs <= _MAX_EFFECTIVE_PEAK_DBFS + 1e-9
    predicted_mic_peak = plan.effective_peak_dbfs + gain_map_db - (-12.0)
    assert predicted_mic_peak <= level_solver.MIC_CLIP_CEILING_DBFS + 1e-9


def test_solver_never_proposes_a_positive_lever():
    """DriverSweepGeneratorPlan hard-refuses a positive commissioning_gain_db
    or main_volume_db (the CamillaDSP 0 dB safety ceiling) -- the solver must
    never propose one, across a spread of very sensitive chains."""

    for gain_map_db in (5.0, 20.0, 40.0, 60.0):
        solved = solve_level(
            gain_map_db=gain_map_db,
            admitted_band_hz=(1000.0, 4000.0),
            commissioning_gain_baseline_db=0.0,
            main_volume_cap_db=0.0,
            max_effective_peak_dbfs=0.0,
            ambient_broadband_dbfs=-90.0,
            model=DRIVER,
        )
        assert isinstance(solved, SolvedLevel)
        assert solved.main_volume_db <= 0.0
        assert solved.commissioning_gain_db <= 0.0
        # Constructing the plan must not raise.
        DriverSweepGeneratorPlan(
            f1_hz=1000.0,
            f2_hz=4000.0,
            amplitude=10.0 ** (-12.0 / 20.0),
            duration_s=1.0,
            repeat_count=1,
            commissioning_gain_db=solved.commissioning_gain_db,
            main_volume_db=solved.main_volume_db,
        )


def test_commissioning_gain_never_drops_below_baseline():
    """The solver only ever RAISES commissioning_gain_db off its baseline
    (less attenuation), never attenuates further -- across many rooms."""

    for ambient in (-20.0, -40.0, -60.0, -80.0):
        solved = _solve(
            gain_map_db=-16.4,
            admitted_band_hz=(2500.0, 20000.0),
            ambient_broadband_dbfs=ambient,
        )
        if isinstance(solved, SolvedLevel):
            assert solved.commissioning_gain_db >= _COMMISSIONING_BASELINE_DB - 1e-9


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_positive_commissioning_gain_baseline_rejected():
    with pytest.raises(ValueError):
        solve_level(
            gain_map_db=0.0,
            admitted_band_hz=(1000.0, 2000.0),
            commissioning_gain_baseline_db=0.1,
            main_volume_cap_db=-3.0,
            max_effective_peak_dbfs=-8.0,
            ambient_broadband_dbfs=-40.0,
            model=DRIVER,
        )


def test_positive_main_volume_cap_rejected():
    with pytest.raises(ValueError):
        solve_level(
            gain_map_db=0.0,
            admitted_band_hz=(1000.0, 2000.0),
            commissioning_gain_baseline_db=-3.0,
            main_volume_cap_db=0.1,
            max_effective_peak_dbfs=-8.0,
            ambient_broadband_dbfs=-40.0,
            model=DRIVER,
        )


def test_admitted_band_must_increase():
    with pytest.raises(ValueError):
        solve_level(
            gain_map_db=0.0,
            admitted_band_hz=(2000.0, 1000.0),
            commissioning_gain_baseline_db=-3.0,
            main_volume_cap_db=-3.0,
            max_effective_peak_dbfs=-8.0,
            ambient_broadband_dbfs=-40.0,
            model=DRIVER,
        )


def test_ambient_band_rejects_non_increasing_edges():
    with pytest.raises(ValueError):
        AmbientBand(lo_hz=200.0, hi_hz=100.0, rms_dbfs=-40.0)


def test_ambient_band_rejects_non_finite_rms():
    with pytest.raises(ValueError):
        AmbientBand(lo_hz=100.0, hi_hz=200.0, rms_dbfs=math.nan)


# ---------------------------------------------------------------------------
# Custom quality model / margin overrides
# ---------------------------------------------------------------------------


def test_custom_snr_floor_and_margin_shift_the_target():
    strict = QualityModel(snr_warn_db=10.0, snr_ok_db=15.0)
    solved = solve_level(
        gain_map_db=1.9,
        admitted_band_hz=(40.0, 400.0),
        commissioning_gain_baseline_db=_COMMISSIONING_BASELINE_DB,
        main_volume_cap_db=_MAIN_VOLUME_CAP_DB,
        max_effective_peak_dbfs=_MAX_EFFECTIVE_PEAK_DBFS,
        ambient_broadband_dbfs=_AMBIENT_BROADBAND_DBFS,
        model=strict,
        solver_margin_db=2.0,
    )
    assert isinstance(solved, SolvedLevel)
    assert solved.predicted_worst_band_snr_db >= 12.0 - 1e-9


# ---------------------------------------------------------------------------
# Ambient-stats event parser (Pi-side parse only; PR-b phone emitter is
# a follow-up -- see the module docstring)
# ---------------------------------------------------------------------------

_RUN_TOKEN = "abc123"


def _valid_event(**overrides):
    payload = {
        "ambient_stats": {
            "schema": level_solver.AMBIENT_STATS_SCHEMA_VERSION,
            "run_token": _RUN_TOKEN,
            "duration_s": 1.5,
            "clipped": False,
            "bands": [
                {"lo_hz": 40.0, "hi_hz": 200.0, "rms_dbfs": -50.0},
                {"lo_hz": 200.0, "hi_hz": 400.0, "rms_dbfs": -55.0},
            ],
        }
    }
    payload["ambient_stats"].update(overrides)
    return payload


def test_parse_valid_ambient_stats_event():
    bands = parse_ambient_stats_event(_valid_event(), expected_run_token=_RUN_TOKEN)
    assert bands == (
        AmbientBand(lo_hz=40.0, hi_hz=200.0, rms_dbfs=-50.0),
        AmbientBand(lo_hz=200.0, hi_hz=400.0, rms_dbfs=-55.0),
    )


def test_parse_absent_event_falls_back():
    assert parse_ambient_stats_event(None, expected_run_token=_RUN_TOKEN) is None
    assert parse_ambient_stats_event({}, expected_run_token=_RUN_TOKEN) is None
    assert (
        parse_ambient_stats_event({"other": 1}, expected_run_token=_RUN_TOKEN) is None
    )


def test_parse_run_token_mismatch_falls_back():
    event = _valid_event(run_token="stale-token")
    assert parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN) is None


def test_parse_unknown_schema_falls_back():
    event = _valid_event(schema=999)
    assert parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN) is None


def test_parse_clipped_capture_falls_back():
    event = _valid_event(clipped=True)
    assert parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN) is None


def test_parse_malformed_bands_fall_back():
    for bad_bands in ([], "not-a-list", [{"lo_hz": 1.0}], [{"lo_hz": "x", "hi_hz": 2, "rms_dbfs": 3}]):
        event = _valid_event(bands=bad_bands)
        assert parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN) is None


def test_parse_non_int_schema_falls_back():
    event = _valid_event(schema="1")
    assert parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN) is None
    event = _valid_event(schema=True)
    assert parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN) is None


def test_parse_oversized_band_list_falls_back():
    """A band list beyond AMBIENT_STATS_MAX_BANDS is malformed (or hostile)
    input -- same fail-soft path as any other malformed event."""

    oversized = [
        {"lo_hz": 20.0 + i, "hi_hz": 21.0 + i, "rms_dbfs": -50.0}
        for i in range(level_solver.AMBIENT_STATS_MAX_BANDS + 1)
    ]
    event = _valid_event(bands=oversized)
    assert parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN) is None

    at_cap = oversized[: level_solver.AMBIENT_STATS_MAX_BANDS]
    event = _valid_event(bands=at_cap)
    parsed = parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN)
    assert parsed is not None
    assert len(parsed) == level_solver.AMBIENT_STATS_MAX_BANDS


def test_parsed_ambient_bands_feed_the_solver_end_to_end():
    event = _valid_event()
    bands = parse_ambient_stats_event(event, expected_run_token=_RUN_TOKEN)
    solved = solve_level(
        gain_map_db=1.9,
        admitted_band_hz=(40.0, 400.0),
        commissioning_gain_baseline_db=_COMMISSIONING_BASELINE_DB,
        main_volume_cap_db=_MAIN_VOLUME_CAP_DB,
        max_effective_peak_dbfs=_MAX_EFFECTIVE_PEAK_DBFS,
        ambient_broadband_dbfs=_AMBIENT_BROADBAND_DBFS,
        ambient_bands=bands,
        model=DRIVER,
    )
    assert isinstance(solved, SolvedLevel)
    reported = {round(b.ambient_dbfs, 2) for b in solved.band_detail}
    assert reported == {-50.0, -55.0}
