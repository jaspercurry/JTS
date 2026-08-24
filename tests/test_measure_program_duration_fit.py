# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The MEASURE composer fits each sweep to the duration limit admission judges
it against (#2921).

A synchronized sweep realizes at the NEAREST phase-closing length, so a request
that equals its ceiling can land just above it: 150-4000 Hz asked for the
composer's nominal 4.0 s realizes 4.00577 s, 5.8 ms over a declared 4.0 s
limit, and ``prepare_driver_excitation_plan`` refused the whole program
(``program_segment_outside_limits``). It was deterministic -- it killed the arm
walk 2-for-2 on jts3 -- and no declaration edit could raise the tweeter's
ceiling, whose effective limit is the CODE-side ``driver_sweep_duration_s``
4.0 s. So the fix is composer-side, and these pin it against the REAL admission
evaluator rather than a re-derivation of it.
"""
from __future__ import annotations

import logging

import pytest

from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.excitation_safety_plan import (
    effective_sweep_duration_limit_s,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.program_admission import (
    ProgramAdmissionRefusal,
    admit_excitation_program,
)
from jasper.active_speaker.session_volume_plan import session_measurement_volume_db
from jasper.active_speaker.test_signal_plan import driver_sweep_duration_s
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    PROGRAM_SAMPLE_RATE_HZ,
    RoleBand,
    build_measure_program,
)
from jasper.audio_measurement.sweep import (
    phase_closing_duration_s,
    synchronized_sweep_metadata,
)
from tests.active_speaker_fixtures import mono_output_topology

#: Tonight's jts3 shape: the woofer runs 150-4000 Hz (the band whose 4 s
#: request realizes over its ceiling) and the tweeter 1600-20000 Hz.
WOOFER_BAND_HZ = (150.0, 4000.0)
TWEETER_BAND_HZ = (1600.0, 20_000.0)


def _profile_and_targets(
    *, woofer_max_sweep_s: float = 4, tweeter_max_sweep_s: float = 6,
):
    """A confirmed profile over the two bands above.

    ``woofer_max_sweep_s`` defaults to the jts3 declaration that produced the
    refusal. The tweeter's declaration is deliberately AMPLE so its effective
    limit comes from the code-side ``driver_sweep_duration_s('tweeter')`` --
    the half no declaration edit can raise.
    """
    topology = mono_output_topology()

    def _limits(peak: float, max_sweep_s: float) -> dict[str, object]:
        return {
            "max_effective_peak_dbfs": peak,
            "max_sweep_duration_s": max_sweep_s,
            "max_repeat_count": 3,
            "minimum_cooldown_s": 0,
        }

    settings = {
        "drivers": [
            {
                "hard_excitation_band_hz": [100, 20_000],
                "measurement_band_hz": [100, 10_000],
                "level_duration_limits": _limits(0.0, woofer_max_sweep_s),
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "W",
                "required_protection_filters": [
                    {
                        "kind": "lowpass",
                        "cutoff_hz": 3000,
                        "minimum_slope_db_per_octave": 24,
                    }
                ],
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 132,
                    "baffle_width_mm": 210,
                },
            },
            {
                "hard_excitation_band_hz": [1500, 22_000],
                "measurement_band_hz": [1500, 20_000],
                "level_duration_limits": _limits(-65.0, tweeter_max_sweep_s),
                "target_id": "mono:tweeter",
                "role": "tweeter",
                "model": "T",
                "recommended_highpass_hz": 1500,
                "required_protection_filters": [
                    {
                        "kind": "highpass",
                        "cutoff_hz": 5000,
                        "minimum_slope_db_per_octave": 24,
                    }
                ],
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 25,
                },
            },
        ],
        "crossover_candidates": [],
    }
    profile = build_driver_safety_profile(
        topology,
        manual_settings=settings,
        driver_research=None,
        saved_at="2026-08-23T00:00:00Z",
    )
    targets = {
        t["role"]: t["target_fingerprint"] for t in active_driver_targets(topology)
    }
    return topology, profile, targets


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(*WOOFER_BAND_HZ)),
        RoleBand("tweeter", 1, FrequencyBand(*TWEETER_BAND_HZ)),
    ]


def _limits_for(profile, targets) -> dict[str, float]:
    """The per-role effective limits the production context resolves -- read
    from the SAME owner the admission gate reads."""
    return {
        role: effective_sweep_duration_limit_s(profile, targets[role])
        for role in ("woofer", "tweeter")
    }


def _admit(topology, profile, targets, program, session_volume_db):
    return admit_excitation_program(
        program,
        topology=topology,
        safety_profile=profile,
        role_targets=targets,
        session_volume_db=session_volume_db,
    )


def _compose(gains, session_volume_db, limits=None):
    return build_measure_program(
        gains,
        _roles(),
        sweep_duration_limits_s=limits,
        downstream_gain_db=session_volume_db,
    )


def _sweep_duration_s(program, segment_id: str) -> float:
    return program.segment(segment_id).n_samples / PROGRAM_SAMPLE_RATE_HZ


# --------------------------------------------------------------------------- #
# 1. tonight's exact refusal
# --------------------------------------------------------------------------- #


def test_the_woofer_sweep_that_overshot_a_four_second_limit_is_now_admitted():
    """The regression: woofer limit 4.0 s over 150-4000 Hz."""
    topology, profile, targets = _profile_and_targets()
    limits = _limits_for(profile, targets)
    assert limits["woofer"] == 4.0
    sv = session_measurement_volume_db(profile, targets.values())
    gains = {"woofer": -6.0, "tweeter": -46.0}

    unfitted = _compose(gains, sv)
    fitted = _compose(gains, sv, limits)

    # The premise: the nominal request realizes ABOVE the limit, and the real
    # admission evaluator refuses it for exactly that.
    assert _sweep_duration_s(unfitted, "sweep_w") > limits["woofer"]
    refused = _admit(topology, profile, targets, unfitted, sv)
    assert not refused.allowed
    assert ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS in refused.refusals

    # The fix: at or below the limit, phase-closing, and admitted.
    assert _sweep_duration_s(fitted, "sweep_w") <= limits["woofer"]
    admitted = _admit(topology, profile, targets, fitted, sv)
    assert admitted.allowed, admitted.refusals
    assert all(seg.execution_allowed for seg in admitted.segments)


def test_every_repeat_of_a_fitted_sweep_is_the_same_length():
    """The repeats are the in-capture drift estimator: a fit that moved only
    the first occurrence would silently break the comparison they exist for."""
    _topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    fitted = _compose(
        {"woofer": -6.0, "tweeter": -46.0}, sv, _limits_for(profile, targets),
    )
    lengths = {
        _sweep_duration_s(fitted, seg_id)
        for seg_id in ("sweep_w", "sweep_w_rep", "sweep_w_rep2")
    }
    assert len(lengths) == 1


def test_the_fitted_length_closes_its_phase():
    """A fitted sweep is a whole number of cycles at f1 -- what the harmonic
    offsets and the deconvolution both ride on. Feeding it back as a request
    must recover the same length, or the segment the composer describes is not
    the one it renders."""
    fitted_s = phase_closing_duration_s(
        *WOOFER_BAND_HZ, at_or_below_s=4.0, sample_rate=PROGRAM_SAMPLE_RATE_HZ,
    )
    meta = synchronized_sweep_metadata(
        f1=WOOFER_BAND_HZ[0],
        f2=WOOFER_BAND_HZ[1],
        duration_approx_s=fitted_s,
        sample_rate=PROGRAM_SAMPLE_RATE_HZ,
    )
    assert meta.duration_s == pytest.approx(fitted_s, abs=1e-12)
    cycles_at_f1 = meta.L * WOOFER_BAND_HZ[0]
    assert cycles_at_f1 == pytest.approx(round(cycles_at_f1), abs=1e-9)


def test_the_fit_gives_up_less_than_a_hundredth_of_a_decibel_of_energy():
    """The whole SNR cost of the fit, stated as a number rather than waved at:
    one cycle at f1 out of a four-second sweep, against a 12 dB SNR floor."""
    import math

    nominal = synchronized_sweep_metadata(
        f1=WOOFER_BAND_HZ[0], f2=WOOFER_BAND_HZ[1], duration_approx_s=4.0,
        sample_rate=PROGRAM_SAMPLE_RATE_HZ,
    ).duration_s
    fitted = phase_closing_duration_s(
        *WOOFER_BAND_HZ, at_or_below_s=4.0, sample_rate=PROGRAM_SAMPLE_RATE_HZ,
    )
    cost_db = -10.0 * math.log10(fitted / nominal)
    assert 0.0 < cost_db < 0.03


# --------------------------------------------------------------------------- #
# 2. the tweeter, whose ceiling is code-side and unraisable
# --------------------------------------------------------------------------- #


def test_the_tweeter_fits_its_code_side_four_second_ceiling():
    """No declaration can raise this one: ``driver_sweep_duration_s('tweeter')``
    is 4.0 s and the ``min`` takes it whatever the operator declares."""
    topology, profile, targets = _profile_and_targets(tweeter_max_sweep_s=20)
    limits = _limits_for(profile, targets)
    assert driver_sweep_duration_s("tweeter") == 4.0
    assert limits["tweeter"] == 4.0

    sv = session_measurement_volume_db(profile, targets.values())
    fitted = _compose({"woofer": -6.0, "tweeter": -46.0}, sv, limits)

    assert _sweep_duration_s(fitted, "sweep_t") <= limits["tweeter"]
    admitted = _admit(topology, profile, targets, fitted, sv)
    assert admitted.allowed, admitted.refusals


def test_a_tweeter_declaration_below_its_nominal_sweep_is_fitted_too():
    """The tweeter's nominal 3 s already clears a 4 s ceiling, so the ceiling
    that actually bites it is a DECLARED one below that -- pinned here so the
    role is not covered only by a case where the fit is inert."""
    topology, profile, targets = _profile_and_targets(tweeter_max_sweep_s=2)
    limits = _limits_for(profile, targets)
    assert limits["tweeter"] == 2.0

    sv = session_measurement_volume_db(profile, targets.values())
    gains = {"woofer": -6.0, "tweeter": -46.0}

    unfitted = _compose(gains, sv)
    assert _sweep_duration_s(unfitted, "sweep_t") > limits["tweeter"]
    assert not _admit(topology, profile, targets, unfitted, sv).allowed

    fitted = _compose(gains, sv, limits)
    assert _sweep_duration_s(fitted, "sweep_t") <= limits["tweeter"]
    assert _admit(topology, profile, targets, fitted, sv).allowed


# --------------------------------------------------------------------------- #
# 3. inert wherever there is room
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "limits",
    [
        pytest.param(None, id="no-limits"),
        pytest.param({}, id="empty-mapping"),
        pytest.param({"woofer": 12.0, "tweeter": 12.0}, id="ample-limits"),
    ],
)
def test_the_fit_is_byte_inert_when_the_limits_leave_room(limits):
    """Byte equality, not "close enough": every round banked before #2921 and
    every box whose declaration leaves room must compose the SAME program, or
    a program_id comparison that anchors a before/after verdict moves under it.
    """
    gains = {"woofer": -6.0, "tweeter": -46.0}
    assert _compose(gains, -20.0, limits).program_id == _compose(
        gains, -20.0, None,
    ).program_id


def test_a_role_absent_from_the_limits_keeps_its_nominal_sweep():
    gains = {"woofer": -6.0, "tweeter": -46.0}
    nominal = _compose(gains, -20.0)
    partial = _compose(gains, -20.0, {"woofer": 4.0})
    assert _sweep_duration_s(partial, "sweep_w") < _sweep_duration_s(
        nominal, "sweep_w",
    )
    assert _sweep_duration_s(partial, "sweep_t") == _sweep_duration_s(
        nominal, "sweep_t",
    )


# --------------------------------------------------------------------------- #
# 4. degenerate: a limit no sweep of that band can fit
# --------------------------------------------------------------------------- #


def test_a_limit_shorter_than_one_cycle_refuses_by_name():
    """An unmeasurable band is a refusal, not a shorter sweep. The composer
    names the role, the band, and the limit -- ValueError is the vocabulary its
    other structural refusals already speak (see the Nyquist guard), so this
    mints no new slug."""
    with pytest.raises(ValueError, match=r"woofer MEASURE sweep over \[150,4000\] Hz"):
        _compose({"woofer": -6.0, "tweeter": -46.0}, -20.0, {"woofer": 0.001})


def test_the_kernel_refuses_a_ceiling_under_one_cycle_at_f1():
    with pytest.raises(ValueError):
        phase_closing_duration_s(150.0, 4000.0, at_or_below_s=0.001)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_a_non_positive_or_non_finite_ceiling_refuses(bad):
    with pytest.raises(ValueError, match="at_or_below_s"):
        phase_closing_duration_s(150.0, 4000.0, at_or_below_s=bad)


# --------------------------------------------------------------------------- #
# 5. disclosure
# --------------------------------------------------------------------------- #


def test_the_compose_path_discloses_only_when_the_fit_shortened_the_request(caplog):
    gains = {"woofer": -6.0, "tweeter": -46.0}
    with caplog.at_level(logging.INFO, logger="jasper.audio_measurement.program"):
        _compose(gains, -20.0, {"woofer": 12.0, "tweeter": 12.0})
    assert "measure_program.sweep_fitted" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="jasper.audio_measurement.program"):
        _compose(gains, -20.0, {"woofer": 4.0, "tweeter": 4.0})
    fitted_lines = [
        rec.getMessage()
        for rec in caplog.records
        if "measure_program.sweep_fitted" in rec.getMessage()
    ]
    # Once, for the one role the fit bit -- not once per occurrence, and not
    # for the tweeter, whose 3 s nominal already clears 4 s.
    assert len(fitted_lines) == 1
    line = fitted_lines[0]
    assert "role=woofer" in line
    assert "limit_s=4.0" in line
    assert "sweep_nominal_s=4.005766" in line
    assert "sweep_fitted_s=3.983876" in line
