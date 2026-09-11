# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synthetic tests for the ramp kernel's shared config and wire schema.

:class:`MeasurementRamp` (the validated tuning config) and :class:`LevelSample`
(the phone-reported wire sample) are consumed by the live ramp engine,
:mod:`jasper.active_speaker.seat_level_ramp`; this file pins their own
validation and parsing invariants only.
"""

from __future__ import annotations

import math

import pytest

from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.ramp import (
    HARD_CEILING_DBFS,
    LEVEL_EVENT_SCHEMA_VERSION,
    LISTENING_POSITION_CAP_BUMP_DB,
    LISTENING_POSITION_CAP_CEIL_DB,
    LevelSample,
    MeasurementRamp,
)

# --- config validation (the overshoot invariant can't be built violated) ------


def test_schema_version_pinned():
    assert LEVEL_EVENT_SCHEMA_VERSION == 1


def test_default_config_is_valid_and_pre_window_below_window():
    cfg = MeasurementRamp()
    assert cfg.pre_window <= cfg.window_low_dbfs
    # Overshoot invariant INCLUDING the step-quantization term (the review's
    # arithmetic fix): the first trusted crossing can sit a full step above the
    # pre-window before latency is added.
    overshoot = cfg.step_db + cfg.ramp_rate * cfg.max_loop_latency_s
    assert overshoot < 0.5 * (cfg.window_high_dbfs - cfg.window_low_dbfs)
    assert cfg.pre_window == pytest.approx(cfg.window_low_dbfs - overshoot)


def test_overshoot_invariant_rejects_a_too_fast_ramp():
    with pytest.raises(ValueError, match="overshoot guard"):
        MeasurementRamp(step_db=4.0, step_interval_s=0.5, max_loop_latency_s=2.0)


def test_overshoot_invariant_includes_step_quantization_term():
    # rate*latency alone = 0.8 < 4 (the OLD invariant would pass) but a huge
    # step means the crossing sample can already breach the window: rejected.
    with pytest.raises(ValueError, match="overshoot guard"):
        MeasurementRamp(step_db=8.0, step_interval_s=20.0, max_loop_latency_s=2.0)


def test_cap_ceil_cannot_exceed_hard_ceiling():
    with pytest.raises(ValueError, match="hard ceiling"):
        MeasurementRamp(cap_ceil_db=6.0)


@pytest.mark.parametrize("field", ["cap_bump_db", "cap_ceil_db"])
def test_dynamic_cap_inputs_must_be_finite(field):
    with pytest.raises(ValueError, match="must be finite"):
        MeasurementRamp(**{field: float("nan")})


def test_settle_hold_must_cover_loop_latency():
    with pytest.raises(ValueError, match="settle_hold_s"):
        MeasurementRamp(settle_hold_s=1.0, max_loop_latency_s=2.0)


def test_dynamic_cap_matches_level_defaults():
    cfg = MeasurementRamp()
    # The cap may be limited by the absolute ceiling, but never floored upward
    # beyond original+bump.
    assert cfg.dynamic_cap(-20.0) == -8.0
    assert cfg.dynamic_cap(-15.2) == pytest.approx(-3.2)
    assert cfg.dynamic_cap(-10.0) == -3.0
    assert cfg.dynamic_cap(-5.0) == -3.0
    assert cfg.dynamic_cap(-45.0) == -33.0


@pytest.mark.parametrize(
    ("original", "bump", "ceiling"),
    [
        (-80.0, 6.0, -6.0),
        (-45.0, 6.0, -6.0),
        (-20.0, 6.0, -6.0),
        (-10.0, 6.0, -6.0),
        (-5.0, 6.0, -6.0),
        (-45.0, 3.0, -12.0),
    ],
)
def test_dynamic_cap_never_exceeds_bump_or_absolute_ceiling(original, bump, ceiling):
    cfg = MeasurementRamp(cap_bump_db=bump, cap_ceil_db=ceiling)
    cap = cfg.dynamic_cap(original)
    assert cap <= original + bump
    assert cap <= ceiling
    assert cap <= HARD_CEILING_DBFS


def test_room_cap_keeps_attenuated_stimulus_inside_digital_envelope():
    shared = MeasurementRamp()
    room = MeasurementRamp(
        cap_bump_db=LISTENING_POSITION_CAP_BUMP_DB,
        cap_ceil_db=LISTENING_POSITION_CAP_CEIL_DB,
    )

    assert shared.cap_ceil_db == -3.0
    assert LISTENING_POSITION_CAP_BUMP_DB == 15.0
    assert room.cap_ceil_db == 0.0
    assert AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS == -12.0
    assert (
        room.cap_ceil_db + AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
    ) == -12.0


def test_safety_timeout_derived_from_worst_case_walk():
    cfg = MeasurementRamp()
    climb = (cfg.cap_ceil_db - cfg.start_db) / cfg.ramp_rate
    # The derived timeout must exceed the staircase's own worst-case climb —
    # the review's SF: a quiet amp must reach MAXED_OUT, not the timeout.
    assert cfg.safety_timeout > climb + cfg.settle_hold_s
    # Explicit values are honored verbatim.
    assert MeasurementRamp(safety_timeout_s=9.0).safety_timeout == 9.0


def test_bounded_low_stability_threshold_must_be_finite_and_nonnegative():
    with pytest.raises(ValueError, match="bounded_low_max_spread_db"):
        MeasurementRamp(bounded_low_max_spread_db=-0.1)
    with pytest.raises(ValueError, match="bounded_low_max_spread_db"):
        MeasurementRamp(bounded_low_max_spread_db=float("nan"))
    with pytest.raises(ValueError, match="bounded_low_max_shortfall_db"):
        MeasurementRamp(bounded_low_max_shortfall_db=0.0)
    with pytest.raises(ValueError, match="bounded_low_max_shortfall_db"):
        MeasurementRamp(bounded_low_max_shortfall_db=float("inf"))


# --- AGC empirical-slope-verification config ----------------------------------


def test_agc_slope_config_defaults_and_validation():
    cfg = MeasurementRamp()
    assert cfg.agc_slope_min_span_db == pytest.approx(6.0)
    assert cfg.agc_slope_min_steps == 3
    assert cfg.agc_slope_threshold == pytest.approx(0.7)
    with pytest.raises(ValueError, match="agc_slope_min_steps"):
        MeasurementRamp(agc_slope_min_steps=1)
    with pytest.raises(ValueError, match="agc_slope_min_span_db"):
        MeasurementRamp(agc_slope_min_span_db=0.0)
    with pytest.raises(ValueError, match="agc_slope_min_span_db"):
        MeasurementRamp(agc_slope_min_span_db=float("inf"))
    with pytest.raises(ValueError, match="agc_slope_threshold"):
        MeasurementRamp(agc_slope_threshold=0.0)
    with pytest.raises(ValueError, match="agc_slope_threshold"):
        MeasurementRamp(agc_slope_threshold=float("nan"))


# --- SAFETY: NaN / non-finite handling ----------------------------------------


def test_level_sample_from_dict_rejects_non_finite():
    with pytest.raises(ValueError, match="non-finite"):
        LevelSample.from_dict({"seq": 1, "rms_dbfs": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        LevelSample.from_dict({"seq": 1, "rms_dbfs": -20.0, "peak_dbfs": float("inf")})


# --- LevelSample parsing -----------------------------------------------------------


def test_level_sample_from_dict_strict_on_rms():
    s = LevelSample.from_dict(
        {"seq": 5, "t_client_ms": 500, "rms_dbfs": -22.0, "peak_dbfs": -18.0}
    )
    assert s.seq == 5 and s.rms_dbfs == -22.0 and s.agc_frozen is True
    with pytest.raises(KeyError):
        LevelSample.from_dict({"seq": 1})  # missing rms_dbfs


def test_level_sample_defaults_peak_to_rms():
    s = LevelSample.from_dict({"rms_dbfs": -30.0})
    assert s.peak_dbfs == -30.0 and math.isclose(s.rms_dbfs, -30.0)
