# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy

import pytest

from tests.active_speaker_fixtures import mono_output_topology


def _topology():
    return mono_output_topology(topology_name="Bench mono")


def _locks(topology):
    from jasper.active_speaker.measurement import active_driver_targets

    return {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": -3.0 if target["role"] == "woofer" else -18.0,
            "locked_main_volume_db": -10.0 if target["role"] == "woofer" else -4.0,
        }
        for target in active_driver_targets(topology)
    }


def test_comparison_set_requires_all_drivers_and_recomputes_fingerprint(tmp_path):
    from jasper.active_speaker.capture_geometry import comparison_set_valid
    from jasper.active_speaker.measurement import start_active_comparison_set
    topology = _topology()
    locks = _locks(topology)
    with pytest.raises(ValueError, match="incomplete"):
        start_active_comparison_set(
            topology,
            profile_context_id="profile-1",
            setup_sha256="a" * 64,
            device_sha256="b" * 64,
            calibration_id="",
            driver_level_locks={"mono:woofer": locks["mono:woofer"]},
            state_path=tmp_path / "incomplete.json",
        )

    evidence = start_active_comparison_set(
        topology,
        profile_context_id="profile-1",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks=locks,
        state_path=tmp_path / "complete.json",
        now="2026-07-11T12:00:00Z",
    )
    assert comparison_set_valid(evidence)
    tampered = copy.deepcopy(evidence)
    tampered["driver_level_locks"]["mono:tweeter"]["locked_main_volume_db"] = -2.0
    assert comparison_set_valid(tampered) is False
    malformed = copy.deepcopy(evidence)
    malformed["driver_level_locks"]["mono:tweeter"]["role"] = "woofer"
    from jasper.active_speaker.capture_geometry import comparison_set_fingerprint

    malformed["fingerprint"] = comparison_set_fingerprint(malformed)
    assert comparison_set_valid(malformed) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (
            "near_field_driver:mono:woofer",
            ("near_field", "mono", "woofer"),
        ),
        (
            "reference_axis_driver:rack:left:mid",
            ("reference_axis", "rack:left", "mid"),
        ),
        (
            "reference_axis_driver:stereo:right:tweeter",
            ("reference_axis", "stereo:right", "tweeter"),
        ),
    ),
)
def test_driver_level_geometry_parser_round_trips_canonical_keys(value, expected):
    from jasper.active_speaker.capture_geometry import (
        driver_level_geometry,
        parse_driver_level_geometry,
    )

    assert parse_driver_level_geometry(value) == expected
    geometry, group_id, role = expected
    assert driver_level_geometry(group_id, role, geometry) == value


@pytest.mark.parametrize(
    "value",
    (
        "",
        " near_field_driver:mono:woofer",
        "near_field_driver:mono:Woofer",
        "Near_Field_driver:mono:woofer",
        "browser_driver:mono:woofer",
        "near_field_driver::woofer",
        "near_field_driver:mono:",
        "near_field_driver:mono",
        "near_field_driver:mono:subwoofer",
        "near_field_driver:mono:woofer:extra",
        "near_field_driver:mono:woofer ",
    ),
)
def test_driver_level_geometry_parser_rejects_noncanonical_keys(value):
    from jasper.active_speaker.capture_geometry import parse_driver_level_geometry

    with pytest.raises(ValueError):
        parse_driver_level_geometry(value)


def test_driver_level_geometry_writer_rejects_non_active_role():
    from jasper.active_speaker.capture_geometry import driver_level_geometry

    with pytest.raises(ValueError, match="unsupported driver role"):
        driver_level_geometry("mono", "subwoofer", "reference_axis")


def test_unresolved_volume_safety_hydrates_from_a_crash_mid_transition(tmp_path):
    """A process that crashes mid-transition leaves ``status: "active"`` on
    disk (a lost setter response never gets the chance to mark it resolved
    OR unresolved); the next process must hydrate that as unresolved rather
    than forgetting the speaker was parked loud with no confirmed restore."""

    import json

    from jasper.web.correction_crossover_backend import (
        _VOLUME_SAFETY_SCHEMA_VERSION,
        _VOLUME_SAFETY_STATE_KIND,
        CrossoverLevelLease,
    )

    state_path = tmp_path / "volume-safety.json"
    state_path.write_text(
        json.dumps({
            "schema_version": _VOLUME_SAFETY_SCHEMA_VERSION,
            "kind": _VOLUME_SAFETY_STATE_KIND,
            "status": "active",
            "reason": None,
            "source": "driver_sweep",
            "speaker_group_id": "mono",
            "role": "woofer",
            "original_main_volume_db": -27.0,
            "emergency_volume_db": -60.0,
        }),
        encoding="utf-8",
    )

    restarted = CrossoverLevelLease(volume_safety_state_path=state_path)
    assert restarted.unresolved_volume_safety == {
        "status": "unresolved",
        "reason": "service_restarted_during_volume_transition",
        "source": "driver_sweep",
        "speaker_group_id": "mono",
        "role": "woofer",
        "original_main_volume_db": -27.0,
        "emergency_volume_db": -60.0,
    }


def test_effective_excitation_includes_driver_main_lock():
    from jasper.active_speaker.baseline_profile import _effective_excitation_dbfs

    locked = {
        "schema_version": 1,
        "scope": "sweep_plus_role_gain_and_driver_level_lock",
        "sweep_peak_dbfs": -12.0,
        "commissioning_gain_db": -6.0,
        "locked_main_volume_db": -4.0,
        "effective_peak_dbfs": -22.0,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "baseline_id": "baseline-1",
        "topology_id": "bench_mono",
        "role": "woofer",
    }
    assert _effective_excitation_dbfs({"excitation": locked}) == -22.0

    varying = {
        **locked,
        "scope": "sweep_plus_role_varying_commission_gain",
        "effective_peak_dbfs": -18.0,
    }
    varying.pop("locked_main_volume_db")
    assert _effective_excitation_dbfs({"excitation": varying}) == -18.0

    assert _effective_excitation_dbfs({
        "excitation": {**locked, "sweep_peak_dbfs": "-12"}
    }) is None
