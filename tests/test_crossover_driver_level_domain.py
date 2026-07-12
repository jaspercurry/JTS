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


@pytest.mark.asyncio
async def test_lease_requires_each_driver_and_summed_uses_quietest_lock():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    lease.context_id = "profile-1"
    lease.configure_targets([
        {
            "target_id": "mono:woofer",
            "speaker_group_id": "mono",
            "role": "woofer",
            "geometry": "near_field_driver:mono:woofer",
            "tone_frequency_hz": 250.0,
            "commissioning_gain_db": -3.0,
        },
        {
            "target_id": "mono:tweeter",
            "speaker_group_id": "mono",
            "role": "tweeter",
            "geometry": "near_field_driver:mono:tweeter",
            "tone_frequency_hz": 6250.0,
            "commissioning_gain_db": -18.0,
        },
    ])
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0, locked=-10.0
    )
    assert lease.level_match_snapshot()["next_target"]["role"] == "tweeter"
    lease._outcomes["near_field_driver:mono:tweeter"] = _locked_outcome(
        original=-30.0, locked=-4.0
    )
    assert lease.level_match_snapshot(current_context_id="profile-1")["ready"] is True

    applied: list[float] = []
    current = -27.0

    async def get_volume() -> float:
        return current

    async def set_volume(value: float) -> bool:
        applied.append(value)
        return True

    assert await lease.acquire_driver_sweep_volume(
        "mono", "tweeter", get_volume, set_volume
    )
    assert applied[-1] == -4.0
    assert await lease.restore_sweep_volume(set_volume)
    assert applied[-1] == -27.0
    assert await lease.acquire_summed_sweep_volume(get_volume, set_volume)
    assert applied[-1] == -10.0
    assert await lease.restore_sweep_volume(set_volume)
    assert applied[-1] == -27.0


@pytest.mark.asyncio
async def test_sweep_lease_restores_when_volume_write_response_is_lost():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0,
        locked=-8.0,
    )
    current = -27.0

    async def get_volume() -> float:
        return current

    async def apply_then_timeout(value: float) -> bool:
        nonlocal current
        current = value
        raise RuntimeError("websocket response lost")

    with pytest.raises(RuntimeError, match="response lost"):
        await lease.acquire_driver_sweep_volume(
            "mono", "woofer", get_volume, apply_then_timeout
        )
    assert current == -8.0

    restore_attempts = 0

    async def restore(value: float) -> bool:
        nonlocal current, restore_attempts
        restore_attempts += 1
        if restore_attempts == 1:
            raise RuntimeError("temporary websocket failure")
        current = value
        return True

    assert await lease.restore_sweep_volume(restore) is False
    assert await lease.restore_sweep_volume(restore) is True
    assert current == -27.0


@pytest.mark.asyncio
async def test_sweep_lease_rejects_nonfinite_entry_volume():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0,
        locked=-8.0,
    )

    async def get_volume() -> float:
        return float("nan")

    async def set_volume(_value: float) -> bool:
        pytest.fail("invalid entry volume must be rejected before a write")

    assert not await lease.acquire_driver_sweep_volume(
        "mono", "woofer", get_volume, set_volume
    )


@pytest.mark.asyncio
async def test_sweep_lease_uses_emergency_attenuation_after_restore_rejection():
    from jasper.web.correction_crossover_backend import (
        EMERGENCY_SWEEP_VOLUME_DB,
        CrossoverLevelLease,
    )

    lease = CrossoverLevelLease()
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0,
        locked=-8.0,
    )
    writes = []

    async def get_volume() -> float:
        return -27.0

    async def set_volume(value: float) -> bool:
        writes.append(value)
        return value != -27.0

    assert await lease.acquire_driver_sweep_volume(
        "mono", "woofer", get_volume, set_volume
    )
    assert await lease.restore_sweep_volume(set_volume) is False
    assert lease.sweep_volume_active is True
    assert await lease.emergency_lower_sweep_volume(set_volume) is True
    assert writes == [-8.0, -27.0, EMERGENCY_SWEEP_VOLUME_DB]
    assert lease.sweep_volume_active is False


def test_effective_excitation_includes_driver_main_lock():
    from jasper.active_speaker.baseline_profile import _effective_excitation_dbfs

    record = {
        "excitation": {
            "schema_version": 1,
            "scope": "sweep_plus_role_gain_and_driver_level_lock",
            "sweep_peak_dbfs": -12.0,
            "commissioning_gain_db": -6.0,
            "locked_main_volume_db": -4.0,
            "effective_peak_dbfs": -22.0,
        }
    }
    assert _effective_excitation_dbfs(record) == -22.0
    record["excitation"]["effective_peak_dbfs"] = -18.0
    assert _effective_excitation_dbfs(record) is None


def test_sequential_envelope_names_next_driver_frequency_and_optional_calibration():
    from jasper.active_speaker.crossover_envelope import build_crossover_envelope
    from tests.test_web_correction_crossover_flow import _envelope_status

    status = _envelope_status()
    status["level_match"] = {
        "running": False,
        "valid": True,
        "ready": False,
        "next_target": {
            "speaker_group_id": "mono",
            "role": "tweeter",
            "tone_frequency_hz": 6250.0,
        },
        "last": {"ramp": {"state": "locked"}},
    }
    envelope = build_crossover_envelope(status)

    assert envelope["screen"] == "microphone"
    assert envelope["next_action"]["label"] == "Set tweeter microphone level"
    assert "6250 Hz" in envelope["verdict_text"]
    assert "without one is supported" in envelope["verdict_text"]
