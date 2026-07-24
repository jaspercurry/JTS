# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging

import pytest

from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.excitation_safety_plan import (
    DriverSweepGeneratorPlan,
    ExcitationSafetyPlanError,
    ExcitationSafetyPlanRefusal,
    PreparedDriverExcitationPlan,
    RequestedDriverExcitationPlan,
    prepare_driver_excitation_plan,
    resolve_driver_excitation_ceilings,
)
from jasper.active_speaker.measurement import active_driver_targets
from tests.active_speaker_fixtures import mono_output_topology


def _profile_and_targets(
    *,
    cooldown_s: float = 1,
    woofer_required_filters: list | None = None,
    woofer_peak: float = -65,
    tweeter_peak: float = -65,
    mode: str = "active_2_way",
    mid_peak: float = -65,
    hard_band: list | None = None,
    measurement_band: list | None = None,
    search_band: list | None = None,
):
    topology = mono_output_topology(mode=mode)

    def _driver(role: str, peak: float, required_filters: list) -> dict:
        return {
            "hard_excitation_band_hz": hard_band or [500, 20_000],
            "measurement_band_hz": measurement_band or [500, 10_000],
            "crossover_search_band_hz": search_band or [1500, 2500],
            "level_duration_limits": {
                "max_effective_peak_dbfs": peak,
                "max_sweep_duration_s": 4,
                "max_repeat_count": 3,
                "minimum_cooldown_s": cooldown_s,
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
    profile = build_driver_safety_profile(
        topology,
        manual_settings=settings,
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
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


def test_safety_plan_derives_closed_request_for_shared_admission():
    topology, profile, targets = _profile_and_targets()
    requested = _requested(targets["woofer"]["target_fingerprint"])
    prepared = prepare_driver_excitation_plan(topology, profile, requested)
    assert prepared.target_id == "mono:woofer"
    assert prepared.target_role == "woofer"
    assert prepared.execution_allowed is True
    assert prepared.request.band == requested.generator.band
    assert prepared.request.effective_peak_dbfs == pytest.approx(-70)
    assert prepared.minimum_cooldown_s == 1
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


def test_safety_plan_refuses_unconfirmed_profile():
    topology, profile, targets = _profile_and_targets()
    unconfirmed = dict(profile)
    unconfirmed["status"] = "needs_confirmation"
    unconfirmed["confirmation"] = None
    with pytest.raises(
        ExcitationSafetyPlanError,
        match=ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value,
    ):
        prepare_driver_excitation_plan(
            topology,
            unconfirmed,
            _requested(targets["woofer"]["target_fingerprint"]),
        )


# --- resolve_driver_excitation_ceilings: two-invariant protection model ------
#
# Operator ruling (2026-07-19): the -65 dBFS HF class default was sized for a
# naked driver tone with no proven protective HP. On the program-admission
# path (``program_admission=True``) it is superseded by a sensitivity-derived
# ceiling -- but ONLY when the declared cap still equals that class-default
# seed, and ONLY for the caller that asks for it. Every other caller keeps
# exactly today's behavior. The sensitivities come from the DECLARATION (the
# design draft's manual_settings, its one owner -- see
# design_draft.declared_driver_sensitivities), passed as a plain per-role
# mapping; they never ride the safety profile. JTS3 hardware numbers: woofer
# (Dayton Epique E150HE-44) 83.3 dB, tweeter (B&C DE250-8) 108.5 dB -- a
# 25.2 dB delta.

_JTS3_SENSITIVITIES = {"woofer": 83.3, "tweeter": 108.5}


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
    # derived = min(-8 - 25.2, -35) = -35 (the abs ceiling binds).
    _topology, profile, targets = _profile_and_targets(
        woofer_peak=-8, tweeter_peak=-65,
    )
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        targets["tweeter"]["target_fingerprint"],
        program_admission=True,
        declared_sensitivities=_JTS3_SENSITIVITIES,
    )
    assert ceiling == pytest.approx(-35.0)
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
    assert "event=active_speaker.excitation_ceiling_superseded" in caplog.text
    caplog.clear()
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.excitation_safety_plan"
    ):
        resolve_driver_excitation_ceilings(
            profile,
            targets["tweeter"]["target_fingerprint"],
            declared_sensitivities=_JTS3_SENSITIVITIES,
        )
    assert "event=active_speaker.excitation_ceiling_superseded" not in caplog.text


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
    assert "event=active_speaker.excitation_ceiling_derivation_skipped" in caplog.text
    assert "role=tweeter" in caplog.text
    assert "reason=declared_sensitivity_missing" in caplog.text
    caplog.clear()
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.excitation_safety_plan"
    ):
        resolve_driver_excitation_ceilings(
            profile, targets["tweeter"]["target_fingerprint"],
        )
    assert "excitation_ceiling_derivation_skipped" not in caplog.text


# --- resolve_driver_excitation_ceilings: band-edge asymmetry (PR-A, #1668) ---
#
# The lower permitted edge stays max(MIN, hard[0], measurement[0]) -- an
# absolute excursion-protection boundary, untouched. The upper permitted edge
# drops measurement_band[1] from the min() -- it is analysis-window metadata,
# not a protection boundary -- so it now binds at min(MAX_DRIVER_TEST_
# FREQUENCY_HZ, hard_band[1]) instead of also being capped by the (often
# narrower) declared measurement window.


def test_upper_edge_ignores_measurement_band_lower_edge_still_absolute():
    _topology, profile, targets = _profile_and_targets(
        hard_band=[1200, 20_000], measurement_band=[1800, 18_000],
        search_band=[1900, 2500],
    )
    band, _ceiling = resolve_driver_excitation_ceilings(
        profile, targets["woofer"]["target_fingerprint"],
    )
    # Upper edge: was 18_000 (measurement_band[1] used to bind); now 20_000
    # (hard_band[1] binds -- measurement_band[1] no longer participates).
    assert band.upper_hz == pytest.approx(20_000.0)
    # Lower edge: unchanged -- measurement_band[0] (1800) is still the
    # strictest of the three lower-edge candidates and still binds.
    assert band.lower_hz == pytest.approx(1800.0)


def test_upper_edge_still_bounded_by_global_ceiling_when_hard_band_is_wider():
    # A hard band WIDER than MAX_DRIVER_TEST_FREQUENCY_HZ still leaves the
    # global ceiling binding (min() semantics preserved) -- the asymmetry
    # only ever drops measurement_band[1] from the min(), it does not remove
    # the global ceiling as a backstop. (The driver safety profile validator
    # requires measurement_band ⊆ hard_band, so a hard band NARROWER than the
    # measurement band's upper edge cannot occur in a confirmed profile --
    # that direction is not a reachable scenario to pin here.)
    _topology, profile, targets = _profile_and_targets(
        hard_band=[1200, 30_000], measurement_band=[1800, 25_000],
        search_band=[1900, 2500],
    )
    band, _ceiling = resolve_driver_excitation_ceilings(
        profile, targets["woofer"]["target_fingerprint"],
    )
    assert band.upper_hz == pytest.approx(23_000.0)  # MAX_DRIVER_TEST_FREQUENCY_HZ
    assert band.lower_hz == pytest.approx(1800.0)
