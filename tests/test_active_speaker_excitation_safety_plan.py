# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy
import logging

import pytest

from jasper.active_speaker.driver_protection import apply_driver_low_limit
from jasper.active_speaker.driver_safety import compute_driver_safety_profile
from jasper.active_speaker.excitation_safety_plan import (
    DriverSweepGeneratorPlan,
    ExcitationSafetyPlanError,
    ExcitationSafetyPlanRefusal,
    PreparedDriverExcitationPlan,
    RequestedDriverExcitationPlan,
    effective_sweep_duration_limit_s,
    prepare_driver_excitation_plan,
    resolve_driver_excitation_ceilings,
    resolve_driver_measurement_band_hz,
    resolve_driver_protection_slope_db_per_octave,
)
from jasper.active_speaker.measurement import active_driver_targets
from tests._log_events import event_fields, event_records
from tests.active_speaker_fixtures import mono_output_topology


_JTS3_SENSITIVITIES = {"woofer": 83.3, "tweeter": 108.5}


def _profile_and_targets(
    *,
    woofer_required_filters: list | None = None,
    woofer_peak: float = -65,
    tweeter_peak: float = -65,
    mode: str = "active_2_way",
    mid_peak: float = -65,
    hard_band: list | None = None,
    measurement_band: list | None = None,
    tweeter_low_limit_hz: float = 1500.0,
    tweeter_published_slope_db_per_octave: float | None = None,
):
    topology = mono_output_topology(mode=mode)

    # #2603: a driver's low limit has one declared owner, and every other
    # low-limit field derives from it. This fixture's tweeter never was
    # internally coherent -- it declared a 500 Hz hard floor under a 5000 Hz
    # protective high-pass -- which the old rules allowed because the two
    # numbers answered to different checks. ``tweeter_low_limit_hz`` is now the
    # single declared number, and the tweeter's hard floor and protective
    # high-pass both follow it, so what each test varies stays visible.
    def _driver(role: str, peak: float, required_filters: list) -> dict:
        role_hard = hard_band or [500, 20_000]
        if role == "tweeter":
            role_hard = [tweeter_low_limit_hz, role_hard[1]]
        return {
            "hard_excitation_band_hz": role_hard,
            **(
                {
                    "recommended_highpass_hz": tweeter_low_limit_hz,
                    **(
                        {
                            "recommended_highpass_slope_db_per_octave":
                                tweeter_published_slope_db_per_octave,
                        }
                        if tweeter_published_slope_db_per_octave is not None
                        else {}
                    ),
                }
                if role == "tweeter"
                else {}
            ),
            "measurement_band_hz": measurement_band or [500, 10_000],
            "level_duration_limits": {
                # ``None`` omits the key -- since the 2026-08-23 ruling that is
                # the ordinary shape, and it is how a target says it has no
                # published level limit.
                **({} if peak is None else {"max_effective_peak_dbfs": peak}),
                "max_sweep_duration_s": 4,
            },
            "target_id": f"mono:{role}",
            "role": role,
            "model": f"Example {role}",
            "required_protection_filters": required_filters,
            "cabinet": {
                "enclosure_kind": "sealed",
                "radiator_count": 1,
                "effective_radiating_diameter_mm": 132 if role == "woofer" else 25,
                **({"baffle_width_mm": 210} if role == "woofer" else {}),
            },
        }

    if woofer_required_filters is None:
        woofer_required_filters = [
            {
                "kind": "lowpass",
                "cutoff_hz": 3000,
                "minimum_slope_db_per_octave": 24,
            }
        ]
    tweeter_filters = [
        {
            "kind": "highpass",
            "cutoff_hz": 5000,
            "minimum_slope_db_per_octave": 24,
        }
    ]
    drivers = [
        _driver("woofer", woofer_peak, woofer_required_filters),
    ]
    if mode == "active_3_way":
        drivers.append(
            _driver(
                "mid",
                mid_peak,
                [
                    {
                        "kind": "highpass",
                        "cutoff_hz": 500,
                        "minimum_slope_db_per_octave": 24,
                    },
                    {
                        "kind": "lowpass",
                        "cutoff_hz": 3000,
                        "minimum_slope_db_per_octave": 24,
                    },
                ],
            )
        )
    drivers.append(_driver("tweeter", tweeter_peak, tweeter_filters))
    settings = {"drivers": drivers, "crossover_candidates": []}
    profile = compute_driver_safety_profile(
        topology,
        manual_settings=settings,
        driver_research=None,
    )
    targets = {target["role"]: target for target in active_driver_targets(topology)}
    return topology, profile, targets


def _requested(target_fingerprint: str, **overrides):
    values = {
        "f1_hz": 1000,
        "f2_hz": 4000,
        "amplitude": 0.1,
        "duration_s": 4,
        "repeat_count": 3,
        "commissioning_gain_db": -50,
        "main_volume_db": 0,
    }
    values.update(overrides)
    return RequestedDriverExcitationPlan(
        target_fingerprint=target_fingerprint,
        commissioning_context_fingerprint="a" * 64,
        generator=DriverSweepGeneratorPlan(**values),
    )


def _tweeter_target(profile, targets) -> dict:
    fingerprint = targets["tweeter"]["target_fingerprint"]
    return next(
        target
        for target in profile["targets"]
        if target["target_fingerprint"] == fingerprint
    )


def test_the_gate_bounds_a_request_by_the_shared_duration_limit(monkeypatch):
    """One owner for ``min(declared, code-side)`` (#2921).

    The gate compares a request against this number and the MEASURE composer is
    handed it to compose UNDER — so it has to be one function, not two that
    happen to agree today. The fixture declares 4 s for both drivers while the
    code-side table gives the woofer 12 s and the tweeter 4 s, so a restatement
    that dropped either half would disagree with the reader on one role.
    """
    _topology, profile, targets = _profile_and_targets()
    for role in ("woofer", "tweeter"):
        fingerprint = targets[role]["target_fingerprint"]
        shared = effective_sweep_duration_limit_s(profile, fingerprint)
        assert shared == 4.0
        prepared = prepare_driver_excitation_plan(
            topology=_topology,
            safety_profile=profile,
            requested_plan=_requested(fingerprint, f1_hz=2000, f2_hz=4000),
        )
        assert prepared.limits.maximum_duration_s == shared


def test_the_shared_duration_limit_refuses_an_unknown_target():
    _topology, profile, _targets = _profile_and_targets()
    with pytest.raises(ExcitationSafetyPlanError) as excinfo:
        effective_sweep_duration_limit_s(profile, "f" * 64)
    assert str(excinfo.value) == (
        ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
    )


def test_the_shared_duration_limit_refuses_an_undeclared_duration():
    _topology, profile, targets = _profile_and_targets()
    fingerprint = targets["woofer"]["target_fingerprint"]
    broken = deepcopy(profile)
    for target in broken["targets"]:
        if target["target_fingerprint"] == fingerprint:
            target["level_duration_limits"].pop("max_sweep_duration_s")
    with pytest.raises(ExcitationSafetyPlanError) as excinfo:
        effective_sweep_duration_limit_s(broken, fingerprint)
    assert str(excinfo.value) == (
        ExcitationSafetyPlanRefusal.MEASUREMENT_INPUTS_INVALID.value
    )


def test_the_protection_slope_reader_returns_the_published_number_not_the_derived_one():
    """The un-fusing (2026-08-23 owner ruling).

    The confirmed target carries both: the manufacturer's published condition
    under ``recommended_highpass_slope_db_per_octave``, and the commissioning
    figure this build derived from it — ``max(published, 24)`` — stamped on the
    protective high-pass. This reader owes the topology gate the FIRST, because
    a code figure may prefill and disclose but never refuse.
    """
    topology, profile, targets = _profile_and_targets(
        tweeter_published_slope_db_per_octave=12.0,
    )
    tweeter = _tweeter_target(profile, targets)
    stamped = next(
        item
        for item in tweeter["required_protection_filters"]
        if item["kind"] == "highpass"
    )
    # Both numbers really are on the record, and they really do differ…
    assert tweeter["recommended_highpass_slope_db_per_octave"] == 12.0
    assert stamped["minimum_slope_db_per_octave"] == 24.0
    # …and the reader hands the gate the published one.
    assert resolve_driver_protection_slope_db_per_octave(
        profile, targets["tweeter"]["target_fingerprint"],
    ) == 12.0


def test_a_maker_that_publishes_no_slope_gives_the_gate_nothing_to_refuse_on():
    """An ordinary datasheet — a recommended crossover frequency with no slope
    qualifier — must not be read as declaring this build's own 24. The stamp is
    still 24 because that is the filter this build EMITS; the reader says
    ``None`` because nobody published a condition."""
    topology, profile, targets = _profile_and_targets()
    tweeter = _tweeter_target(profile, targets)
    stamped = next(
        item
        for item in tweeter["required_protection_filters"]
        if item["kind"] == "highpass"
    )
    assert "recommended_highpass_slope_db_per_octave" not in tweeter
    assert stamped["minimum_slope_db_per_octave"] == 24.0
    assert resolve_driver_protection_slope_db_per_octave(
        profile, targets["tweeter"]["target_fingerprint"],
    ) is None


def test_a_profile_stored_before_the_owner_pair_landed_reads_as_unpublished():
    """A pre-#2897 target carries only the projections. It is a sound
    declaration and stays confirmed, but it has no published slope on it — so
    the gate applies no slope bound until the next /sound/ save, which is the
    de-nanny direction rather than a fabricated 24."""
    topology, profile, targets = _profile_and_targets(
        tweeter_published_slope_db_per_octave=12.0,
    )
    legacy = deepcopy(profile)
    for target in legacy["targets"]:
        target.pop("recommended_highpass_hz", None)
        target.pop("recommended_highpass_slope_db_per_octave", None)
    assert resolve_driver_protection_slope_db_per_octave(
        legacy, targets["tweeter"]["target_fingerprint"],
    ) is None


def test_safety_plan_derives_closed_request_for_shared_admission():
    topology, profile, targets = _profile_and_targets()
    requested = _requested(targets["woofer"]["target_fingerprint"])
    prepared = prepare_driver_excitation_plan(topology, profile, requested)
    assert prepared.target_id == "mono:woofer"
    assert prepared.target_role == "woofer"
    assert prepared.execution_allowed is True
    assert prepared.request.band == requested.generator.band
    assert prepared.request.effective_peak_dbfs == pytest.approx(-70)
    assert prepared.refusals == ()
    assert prepared.to_dict()["accepts_protection_evidence"] is True
    with pytest.raises(TypeError, match="prepare_driver_excitation_plan"):
        PreparedDriverExcitationPlan()


def test_outside_limits_remains_blocked():
    topology, profile, targets = _profile_and_targets()
    requested = _requested(
        targets["tweeter"]["target_fingerprint"],
        duration_s=5,
    )
    prepared = prepare_driver_excitation_plan(topology, profile, requested)
    assert prepared.execution_allowed is False
    assert prepared.refusals == (
        ExcitationSafetyPlanRefusal.REQUEST_OUTSIDE_LIMITS,
    )


def test_closed_generator_rejects_positive_gain():
    _topology, _profile, targets = _profile_and_targets()
    with pytest.raises(ExcitationSafetyPlanError, match="non-positive"):
        _requested(
            targets["woofer"]["target_fingerprint"],
            commissioning_gain_db=1,
        )



def test_the_level_ceiling_reports_where_its_number_came_from() -> None:
    """Delegation is a provenance fact, not a magic value (2026-08-23).

    The ceiling used to be selected by comparing the declared peak against the
    class default: equal meant "delegated", anything else meant "literal", and
    ``min(declared, class_default)`` then clamped the literal arm back down. A
    value steering a derivation is how a household number typed one dB louder
    than a code figure silently lost both its intent and its level.

    Three cases, one predicate. Absence is the ordinary delegation; a stored
    class seed said the same thing under the retired contract and is read the
    same way; anything else is a declaration, honoured verbatim in BOTH
    directions from the class figure.
    """
    from jasper.active_speaker.driver_protection import driver_protection_profile
    from jasper.active_speaker.excitation_safety_plan import (
        LEVEL_CEILING_DECLARED,
        LEVEL_CEILING_LEGACY_CLASS_SEED,
        LEVEL_CEILING_UNDECLARED,
        declared_level_ceiling_dbfs,
    )

    protection = driver_protection_profile("tweeter", driver_style="dome_tweeter")
    assert protection.max_auto_level_dbfs == -65.0

    def _target(limits: dict) -> dict:
        return {
            "role": "tweeter",
            "driver_style": "dome_tweeter",
            "level_duration_limits": limits,
        }

    assert declared_level_ceiling_dbfs(_target({})) == (
        -65.0,
        LEVEL_CEILING_UNDECLARED,
    )
    assert declared_level_ceiling_dbfs(
        _target({"max_effective_peak_dbfs": -65.0})
    ) == (-65.0, LEVEL_CEILING_LEGACY_CLASS_SEED)
    for declared in (-66.0, -64.0):
        assert declared_level_ceiling_dbfs(
            _target({"max_effective_peak_dbfs": declared})
        ) == (declared, LEVEL_CEILING_DECLARED)

    # A present-but-unusable value is a malformed record, not a silent fallback
    # to the class figure. So is a target with no limits object at all.
    for junk in (True, "quiet", float("nan")):
        with pytest.raises(ExcitationSafetyPlanError):
            declared_level_ceiling_dbfs(_target({"max_effective_peak_dbfs": junk}))
    with pytest.raises(ExcitationSafetyPlanError):
        declared_level_ceiling_dbfs({"role": "tweeter"})


def test_an_undeclared_peak_delegates_the_hf_ceiling_on_the_proven_hp_path() -> None:
    """Absence reaches the derivation, end to end through the real resolver.

    The path a profile saved after 2026-08-23 takes: no ``max_effective_peak_dbfs``
    on the tweeter at all, and the level comes from the sensitivity delta
    against the woofer's own cap.

    Mutation guard: make absence resolve literally instead of delegating and
    this lands on the -65 class default, 41.8 dB adrift.
    """
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=None,
    )
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        program_admission=True,
        declared_sensitivities=_JTS3_SENSITIVITIES,
    )
    # woofer cap -8.0, sensitivity delta 25.2 dB -> -33.2.
    assert ceiling == pytest.approx(-33.2)

    # And the naked-tone path still keeps the class default, exactly as it does
    # for a declared seed -- delegation is what the proven high-pass buys.
    _band, naked = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        declared_sensitivities=_JTS3_SENSITIVITIES,
    )
    assert naked == pytest.approx(-65.0)


def test_a_declared_peak_louder_than_the_class_default_is_not_clamped() -> None:
    """The clamp that went with the save-time refusal (2026-08-23).

    ``min(declared, class_default)`` used to pull a declared -50 back to -65
    while ``_target_issues`` refused the save — a code figure overruling a
    declaration in two places at once, and the reason the old comment at this
    site called the corner "silently unmet". The declaration wins now, and the
    only bound left on it is digital full scale.
    """
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=-50,
    )
    for program_admission in (False, True):
        _band, ceiling = resolve_driver_excitation_ceilings(
            profile,
            targets["tweeter"]["target_fingerprint"],
            program_admission=program_admission,
            declared_sensitivities=_JTS3_SENSITIVITIES,
        )
        assert ceiling == pytest.approx(-50.0)


def test_naked_path_keeps_legacy_ceiling_even_with_sensitivities_declared():
    # Pin BOTH sides of the conditional: the SAME profile + declared
    # sensitivities resolve to the untouched -65 class default when the
    # caller does not mark the proven-HP path.
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=-65,
    )
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        declared_sensitivities=_JTS3_SENSITIVITIES,
    )
    assert ceiling == pytest.approx(-65.0)


def test_program_admission_path_derives_jts3_ceiling():
    # The JTS3 worked example: woofer cap -8, sensitivities 83.3/108.5 ->
    # derived = -8 - 25.2 = -33.2. The provisional -35 hedge that used to clamp
    # this by a further 1.8 dB was retired 2026-08-20; the sensitivity
    # arithmetic is the operative ceiling.
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=-65,
    )
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        program_admission=True,
        declared_sensitivities=_JTS3_SENSITIVITIES,
    )
    assert ceiling == pytest.approx(-33.2)
    # The woofer itself is a low-frequency role: its own ceiling is untouched
    # by the two-invariant HF derivation regardless of the flag.
    _woofer_band, woofer_ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["woofer"]["target_fingerprint"],
        program_admission=True,
        declared_sensitivities=_JTS3_SENSITIVITIES,
    )
    assert woofer_ceiling == pytest.approx(-8.0)


def test_explicit_household_value_is_never_overridden():
    # The household typed a REAL, different value (-70, not the -65 seed) --
    # even on the proven-HP path, with sensitivities declared, it is always
    # respected as-is.
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=-70,
    )
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        program_admission=True,
        declared_sensitivities=_JTS3_SENSITIVITIES,
    )
    assert ceiling == pytest.approx(-70.0)


def test_missing_sensitivity_falls_back_to_legacy_ceiling():
    # Seed matches (-65) and program_admission=True, but the declaration
    # carries no sensitivities -- nothing to derive from, so the legacy
    # class-default ceiling holds (every caller that doesn't thread the
    # declaration keeps exactly today's behavior).
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=-65,
    )
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        program_admission=True,
    )
    assert ceiling == pytest.approx(-65.0)
    # A HALF declaration (tweeter only, no LF sibling) also derives nothing.
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        program_admission=True,
        declared_sensitivities={"tweeter": 108.5},
    )
    assert ceiling == pytest.approx(-65.0)


def test_three_way_shaped_variant_takes_the_conservative_candidate():
    # A 3-way (woofer/mid/tweeter): the tweeter's derived ceiling must be
    # conservative across every declared low-frequency sibling, not just one.
    # woofer cap -8, sens 83.3 -> derived -35 (abs ceiling, as in the 2-way
    # case). mid cap -30, sens 100.0 -> derived = min(-30 - (108.5-100.0),
    # -35) = min(-38.5, -35) = -38.5, the MORE conservative candidate, so it
    # must win over the woofer's -35.
    _topology, profile, targets = _profile_and_targets(
        mode="active_3_way",
        woofer_peak=-8,
        mid_peak=-30,
        tweeter_peak=-65,
    )
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        program_admission=True,
        declared_sensitivities={"woofer": 83.3, "mid": 100.0, "tweeter": 108.5},
    )
    assert ceiling == pytest.approx(-38.5)


def test_ceiling_supersession_logs_event(caplog):
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=-65,
    )
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.excitation_safety_plan"
    ):
        resolve_driver_excitation_ceilings(
            profile,
            targets["tweeter"]["target_fingerprint"],
            program_admission=True,
            declared_sensitivities=_JTS3_SENSITIVITIES,
        )
    assert event_records(caplog, "active_speaker.excitation_ceiling_superseded")
    caplog.clear()
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.excitation_safety_plan"
    ):
        resolve_driver_excitation_ceilings(
            profile,
            targets["tweeter"]["target_fingerprint"],
            declared_sensitivities=_JTS3_SENSITIVITIES,
        )
    assert not event_records(caplog, "active_speaker.excitation_ceiling_superseded")


def test_skipped_derivation_logs_named_role(caplog):
    # Nit 1 (2026-07-19 gate): the proven-HP path WOULD derive (HF role,
    # seed-equal cap) but no declared sensitivity exists -- the silent
    # fallback to the near-inaudible class default cost a puzzled hardware
    # triage, so the skip is a named INFO event. The naked path stays silent.
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=-65,
    )
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.excitation_safety_plan"
    ):
        resolve_driver_excitation_ceilings(
            profile,
            targets["tweeter"]["target_fingerprint"],
            program_admission=True,
        )
    fields = event_fields(
        caplog, "active_speaker.excitation_ceiling_derivation_skipped"
    )
    assert fields["role"] == "tweeter"
    assert fields["reason"] == "declared_sensitivity_missing"
    caplog.clear()
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.excitation_safety_plan"
    ):
        resolve_driver_excitation_ceilings(
            profile, targets["tweeter"]["target_fingerprint"],
        )
    assert not event_records(
        caplog, "active_speaker.excitation_ceiling_derivation_skipped"
    )


@pytest.mark.parametrize("role", ["woofer", "mid", "subwoofer", "tweeter", "full_range"])
@pytest.mark.parametrize("program_admission", [False, True])
@pytest.mark.parametrize("hard_upper, expected_upper", [(12000, 12000), (20000, 20000), (30000, 23000)])
@pytest.mark.parametrize("global_max", [18000, 23000])
def test_excitation_band_uses_audio_edges_and_keeps_interior_limits(monkeypatch, role, program_admission, hard_upper, expected_upper, global_max):
    monkeypatch.setattr("jasper.active_speaker.excitation_safety_plan.MAX_DRIVER_TEST_FREQUENCY_HZ", global_max)
    target = {
        "role": role, "target_id": f"mono:{role}", "target_fingerprint": "f" * 64,
        "hard_excitation_band_hz": [45, hard_upper],
        "measurement_band_hz": [60, 10000],
        "required_protection_filters": [], "level_duration_limits": {},
    }
    band, _cap = resolve_driver_excitation_ceilings(
        {"targets": [target]}, "f" * 64, program_admission=program_admission,
    )
    expected_lower = (45 if program_admission else 60) if role == "tweeter" else 20
    assert (band.lower_hz, band.upper_hz) == (
        expected_lower, min(global_max, 20000 if role in {"tweeter", "full_range"} else expected_upper))


_JTS3_TWEETER = {"hard_band": [1600, 20_000], "measurement_band": [2000, 18_000],
                 "tweeter_low_limit_hz": 1600.0}


def test_hf_proven_hp_sweep_floor_follows_the_declared_hard_band():
    # THE change: on the proven-HP path the tweeter is swept from its declared
    # HARD floor, not its narrower declared analysis window.
    _topology, profile, targets = _profile_and_targets(**_JTS3_TWEETER)
    band, _cap = resolve_driver_excitation_ceilings(
        profile, targets["tweeter"]["target_fingerprint"], program_admission=True,
    )
    # Follows hard_band[0], NOT measurement_band[0] (2000) and not a literal.
    assert band.lower_hz == pytest.approx(1600.0)


def test_hf_naked_tone_sweep_floor_still_binds_at_the_declared_analysis_window():
    # Negative 1: without the proven high-pass in the graph there is no filter
    # between the driver and the sub-window region, so the analysis floor
    # keeps binding. Same declaration, same role, only the path differs.
    _topology, profile, targets = _profile_and_targets(**_JTS3_TWEETER)
    band, _cap = resolve_driver_excitation_ceilings(
        profile, targets["tweeter"]["target_fingerprint"],
    )
    assert band.lower_hz == pytest.approx(2000.0)


def test_the_widened_floor_never_reaches_below_the_declared_hard_band():
    # Negative 3 -- containment. The widening swaps WHICH declared floor binds;
    # it never removes the hard band as the absolute floor. A declaration whose
    # hard floor sits above its analysis floor is still held at the hard floor.
    _topology, profile, targets = _profile_and_targets(
        hard_band=[2500, 20_000], measurement_band=[2500, 18_000],
        tweeter_low_limit_hz=2500.0,
    )
    band, _cap = resolve_driver_excitation_ceilings(
        profile, targets["tweeter"]["target_fingerprint"], program_admission=True,
    )
    assert band.lower_hz == pytest.approx(2500.0)


def test_the_widened_floor_is_announced_when_it_moves(caplog):
    # Observability: an operator triaging a hardware session can see that this
    # driver was deliberately excited below its declared analysis window.
    _topology, profile, targets = _profile_and_targets(**_JTS3_TWEETER)
    with caplog.at_level(logging.INFO):
        resolve_driver_excitation_ceilings(
            profile, targets["tweeter"]["target_fingerprint"], program_admission=True,
        )
    fields = event_fields(
        caplog, "active_speaker.excitation_floor_widened_to_hard_band"
    )
    assert fields["declared_measurement_floor_hz"] == "2000.0"
    assert fields["excitation_floor_hz"] == "1600.0"


@pytest.mark.parametrize("role", ["woofer", "tweeter"])
def test_hard_band_widening_event_excludes_low_frequency_roles_and_unchanged_floors(caplog, role):
    _topology, profile, targets = _profile_and_targets(
        hard_band=[1600, 20_000], measurement_band=[1600, 18_000],
        tweeter_low_limit_hz=1600.0,
    )
    with caplog.at_level(logging.INFO):
        resolve_driver_excitation_ceilings(
            profile, targets[role]["target_fingerprint"], program_admission=True,
        )
    assert not event_records(
        caplog, "active_speaker.excitation_floor_widened_to_hard_band"
    )


# --- resolve_driver_measurement_band_hz (flat-linearization plan PR-4) ------
#
# The declared measurement_band_hz itself, separate from
# resolve_driver_excitation_ceilings' derived excitation ceiling — PR-4's
# contract-derived echo/null analysis band needs the declared analysis
# WINDOW, which that function reads and validates but does not return.


def test_resolve_driver_measurement_band_hz_returns_the_declared_window():
    _topology, profile, targets = _profile_and_targets(
        measurement_band=[5000, 20_000],
    )
    band = resolve_driver_measurement_band_hz(
        profile, targets["tweeter"]["target_fingerprint"],
    )
    assert band == (5000.0, 20_000.0)


def test_resolve_driver_measurement_band_hz_differs_from_the_excitation_ceiling():
    # The exact case resolve_driver_excitation_ceilings' own docstring names:
    # a tweeter whose measurement band tops out below its hard band. The
    # excitation ceiling's upper edge is the WIDER hard-band/global-ceiling
    # figure; the measurement band is the declared window itself.
    _topology, profile, targets = _profile_and_targets(
        hard_band=[1600, 20_000], measurement_band=[2000, 18_000],
    )
    ceiling_band, _peak = resolve_driver_excitation_ceilings(
        profile, targets["tweeter"]["target_fingerprint"],
    )
    measurement_band = resolve_driver_measurement_band_hz(
        profile, targets["tweeter"]["target_fingerprint"],
    )
    assert measurement_band == (2000.0, 18_000.0)
    assert ceiling_band.upper_hz == pytest.approx(20_000.0)
    assert measurement_band[1] != ceiling_band.upper_hz


def test_resolve_driver_measurement_band_hz_raises_when_the_field_is_missing():
    # _target_for_request's own shape check is on the ``targets`` list, not
    # ``profile["status"]`` (that gate belongs to
    # prepare_driver_excitation_plan, a different caller) — so the way to
    # reach PROFILE_NOT_CONFIRMED here is a target record missing the field
    # itself, mirroring resolve_driver_excitation_ceilings' own identical
    # check on the same record.
    _topology, profile, targets = _profile_and_targets()
    mutated = dict(profile)
    mutated["targets"] = [
        {**t, "measurement_band_hz": None}
        if t.get("role") == "woofer"
        else t
        for t in profile["targets"]
    ]
    with pytest.raises(
        ExcitationSafetyPlanError,
        match=ExcitationSafetyPlanRefusal.MEASUREMENT_INPUTS_INVALID.value,
    ):
        resolve_driver_measurement_band_hz(
            mutated, targets["woofer"]["target_fingerprint"],
        )


def test_resolve_driver_measurement_band_hz_raises_on_unknown_target():
    _topology, profile, _targets = _profile_and_targets()
    with pytest.raises(
        ExcitationSafetyPlanError,
        match=ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value,
    ):
        resolve_driver_measurement_band_hz(profile, "not-a-real-fingerprint")


def test_a_full_range_sweep_uses_audio_edges_after_low_limit_projection():
    declared_floor_hz = 80.0
    target = apply_driver_low_limit(
        {
            "role": "full_range",
            "target_id": "mono:full_range",
            "target_fingerprint": "f" * 64,
            "recommended_highpass_hz": declared_floor_hz,
            "hard_excitation_band_hz": [40.0, 20_000.0],
            "measurement_band_hz": [40.0, 15_000.0],
            "required_protection_filters": [],
            "level_duration_limits": {
                "max_sweep_duration_s": 4,
            },
        },
        role="full_range",
    )

    band, _ceiling = resolve_driver_excitation_ceilings({"targets": [target]}, "f" * 64)

    assert target["hard_excitation_band_hz"][0] == declared_floor_hz
    assert (band.lower_hz, band.upper_hz) == (20.0, 20000.0)
