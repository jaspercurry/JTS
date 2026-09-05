# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from tests.active_speaker_fixtures import mono_output_topology


def _topology():
    return mono_output_topology(topology_name="Bench mono")


def _locked_outcome(*, original: float, locked: float):
    from jasper.audio_measurement.ramp import RampState

    return SimpleNamespace(
        ramp=SimpleNamespace(
            state=RampState.LOCKED,
            original_main_volume_db=original,
            locked_main_volume_db=locked,
            restored=True,
        )
    )


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


def test_lease_snapshot_requires_every_driver_before_ready():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    lease.context_id = "profile-1"
    lease._targets = {
        "mono:woofer": {
            "target_id": "mono:woofer",
            "speaker_group_id": "mono",
            "role": "woofer",
            "geometry": "near_field_driver:mono:woofer",
            "tone_frequency_hz": 250.0,
            "commissioning_gain_db": -3.0,
        },
        "mono:tweeter": {
            "target_id": "mono:tweeter",
            "speaker_group_id": "mono",
            "role": "tweeter",
            "geometry": "near_field_driver:mono:tweeter",
            "tone_frequency_hz": 6250.0,
            "commissioning_gain_db": -18.0,
        },
    }
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0, locked=-10.0
    )
    assert lease.level_match_snapshot()["next_target"]["role"] == "tweeter"
    lease._outcomes["near_field_driver:mono:tweeter"] = _locked_outcome(
        original=-30.0, locked=-4.0
    )
    lease._outcomes["reference_axis_driver:mono:woofer"] = _locked_outcome(
        original=-30.0, locked=-10.0
    )
    lease._outcomes["reference_axis_driver:mono:tweeter"] = _locked_outcome(
        original=-30.0, locked=-4.0
    )
    assert lease.level_match_snapshot(current_context_id="profile-1")["ready"] is True
    assert lease.level_match_snapshot()["next_target"] is None


def test_discard_reference_axis_outcome_clears_runtime_and_lock_store():
    from jasper.correction.level_match import MeasurementLevelLock
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    geometry = "reference_axis_driver:mono:woofer"
    lease = CrossoverLevelLease()
    lease._outcomes[geometry] = _locked_outcome(original=-30.0, locked=-3.5)
    lease.level_lock_store.put(
        MeasurementLevelLock(
            geometry=geometry,
            main_volume_db=-3.5,
            gain_map_db=None,
            settled_mic_dbfs=None,
            noise_floor_dbfs=None,
        )
    )

    lease.discard_driver_level_outcome(
        "mono", "woofer", capture_geometry="reference_axis"
    )

    assert geometry not in lease._outcomes
    assert lease.level_lock_store.get(geometry) is None


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


class _LostResponse(RuntimeError):
    """The DSP applied the write; the transport never returned its ack."""


async def test_volume_intent_persists_when_the_write_response_is_lost(tmp_path):
    """The durable restore intent is written BEFORE the first volume
    mutation, so a lost setter response (or a crash mid-transition) hydrates
    as unresolved instead of forgetting the speaker is parked loud."""

    from jasper.web.correction_crossover_backend import (
        CrossoverLevelLease,
        UnresolvedVolumeRecoveryResult,
    )

    state_path = tmp_path / "volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
    current = -27.0

    async def get_volume() -> float:
        return current

    async def apply_then_timeout(value: float) -> bool:
        nonlocal current
        current = value
        raise _LostResponse

    lease._begin_volume_transition(
        source="driver_sweep",
        speaker_group_id="mono",
        role="woofer",
        original_main_volume_db=current,
    )
    with pytest.raises(_LostResponse):
        await apply_then_timeout(-8.0)
    assert current == -8.0

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

    async def restore(value: float) -> bool:
        nonlocal current
        current = value
        return True

    assert (
        await restarted.recover_unresolved_volume_safety(restore, get_volume)
        is UnresolvedVolumeRecoveryResult.EXACT_RESTORED
    )
    assert current == -27.0
    assert restarted.unresolved_volume_safety is None


@pytest.mark.parametrize("invalid_original", (float("nan"), float("inf"), 0.1))
def test_volume_transition_refuses_an_unrestorable_entry_volume(
    invalid_original, tmp_path
):
    """A durable intent is only worth writing if it can actually restore: a
    non-finite or above-0 dB entry volume is refused before any state is
    persisted and therefore before any volume moves."""

    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    state_path = tmp_path / "volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)

    with pytest.raises(ValueError):
        lease._begin_volume_transition(
            source="driver_sweep",
            speaker_group_id="mono",
            role="woofer",
            original_main_volume_db=invalid_original,
        )

    assert lease.unresolved_volume_safety is None
    assert not state_path.exists()


async def test_volume_recovery_uses_emergency_attenuation_after_restore_rejection():
    from jasper.web.correction_crossover_backend import (
        EMERGENCY_SWEEP_VOLUME_DB,
        CrossoverLevelLease,
        UnresolvedVolumeRecoveryResult,
    )

    lease = CrossoverLevelLease()
    writes = []
    current = -27.0

    async def get_volume() -> float:
        return current

    async def set_volume(value: float) -> bool:
        nonlocal current
        writes.append(value)
        if value == -27.0:
            return False
        current = value
        return True

    lease._begin_volume_transition(
        source="driver_sweep",
        speaker_group_id="mono",
        role="woofer",
        original_main_volume_db=current,
    )
    assert await set_volume(-8.0) is True

    assert (
        await lease._drain_volume_recovery(set_volume, get_volume)
        is UnresolvedVolumeRecoveryResult.EMERGENCY_ATTENUATED
    )
    assert writes == [-8.0, -27.0, EMERGENCY_SWEEP_VOLUME_DB]
    assert current == EMERGENCY_SWEEP_VOLUME_DB
    assert lease.unresolved_volume_safety is None


async def test_dual_recovery_failure_reports_failed_and_survives_restart(tmp_path):
    """Both recovery candidates refused: the drain answers FAILED rather than
    pretending resolution, and the unresolved intent is still on disk for the
    next process to find."""

    from jasper.web.correction_crossover_backend import (
        CrossoverLevelLease,
        UnresolvedVolumeRecoveryResult,
    )

    state_path = tmp_path / "volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
    current = -27.0
    writes = []

    async def get_volume():
        return current

    async def set_volume(value):
        nonlocal current
        writes.append(value)
        if value in {-27.0, -60.0}:
            return False
        current = value
        return True

    lease._begin_volume_transition(
        source="driver_sweep",
        speaker_group_id="mono",
        role="woofer",
        original_main_volume_db=current,
    )
    assert await set_volume(-8.0) is True

    assert (
        await lease._drain_volume_recovery(set_volume, get_volume)
        is UnresolvedVolumeRecoveryResult.FAILED
    )
    assert writes == [-8.0, -27.0, -60.0]
    assert lease.unresolved_volume_safety is not None
    assert (
        CrossoverLevelLease(
            volume_safety_state_path=state_path
        ).unresolved_volume_safety
        is not None
    )


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
