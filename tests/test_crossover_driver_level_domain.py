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


def test_quietest_locked_volume_is_exact_deterministic_and_fail_closed():
    from jasper.active_speaker.capture_geometry import quietest_locked_main_volume

    roles = frozenset({"woofer", "tweeter"})
    assert quietest_locked_main_volume(
        {"tweeter": -4.0, "woofer": -10.0}, roles
    ) == ("woofer", -10.0)
    assert quietest_locked_main_volume(
        {"woofer": -10.0, "tweeter": -10.0}, roles
    ) == ("tweeter", -10.0)
    assert quietest_locked_main_volume({"woofer": -10.0}, roles) is None
    assert (
        quietest_locked_main_volume(
            {"woofer": -10.0, "tweeter": -4.0, "mid": -6.0}, roles
        )
        is None
    )
    for invalid in (True, float("nan"), 0.1):
        assert (
            quietest_locked_main_volume(
                {"woofer": -10.0, "tweeter": invalid}, roles
            )
            is None
        )


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
    lease._outcomes["reference_axis_driver:mono:woofer"] = _locked_outcome(
        original=-30.0, locked=-10.0
    )
    lease._outcomes["reference_axis_driver:mono:tweeter"] = _locked_outcome(
        original=-30.0, locked=-4.0
    )
    assert lease.level_match_snapshot(current_context_id="profile-1")["ready"] is True

    applied: list[float] = []
    current = -27.0

    async def get_volume() -> float:
        return current

    async def set_volume(value: float) -> bool:
        nonlocal current
        applied.append(value)
        current = value
        return True

    assert await lease.acquire_driver_sweep_volume(
        "mono", "tweeter", get_volume, set_volume
    )
    assert applied[-1] == -4.0
    assert (await lease.finish_sweep_volume(set_volume, get_volume)).value == (
        "exact_restored"
    )
    assert applied[-1] == -27.0
    assert await lease.acquire_summed_sweep_volume("mono", get_volume, set_volume)
    assert applied[-1] == -10.0
    assert (await lease.finish_sweep_volume(set_volume, get_volume)).value == (
        "exact_restored"
    )
    assert applied[-1] == -27.0


@pytest.mark.asyncio
async def test_summed_sweep_lease_is_bound_to_requested_group():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    lease.configure_targets([
        {
            "target_id": f"{group}:{role}",
            "speaker_group_id": group,
            "role": role,
            "geometry": f"near_field_driver:{group}:{role}",
            "tone_frequency_hz": 250.0 if role == "woofer" else 6250.0,
            "commissioning_gain_db": 0.0,
        }
        for group in ("left", "right")
        for role in ("woofer", "tweeter")
    ])
    lease._outcomes["reference_axis_driver:left:woofer"] = _locked_outcome(
        original=-30.0, locked=-20.0
    )
    lease._outcomes["reference_axis_driver:right:woofer"] = _locked_outcome(
        original=-30.0, locked=-5.0
    )
    current = -27.0
    writes = []

    async def get_volume():
        return current

    async def set_volume(value):
        nonlocal current
        writes.append(value)
        current = value
        return True

    assert not await lease.acquire_summed_sweep_volume(
        "right", get_volume, set_volume
    )
    assert writes == []
    lease._outcomes["reference_axis_driver:right:tweeter"] = _locked_outcome(
        original=-30.0, locked=-6.0
    )
    assert await lease.acquire_summed_sweep_volume(
        "right", get_volume, set_volume
    )
    assert writes == [-6.0]
    lease.assert_sweep_volume_owned(
        source="summed_sweep",
        speaker_group_id="right",
        role="summed",
    )
    with pytest.raises(RuntimeError, match="does not own"):
        lease.assert_sweep_volume_owned(
            source="summed_sweep",
            speaker_group_id="left",
            role="summed",
        )
    assert (await lease.finish_sweep_volume(set_volume, get_volume)).value == (
        "exact_restored"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_lock", (float("nan"), 0.1, True))
async def test_summed_sweep_refuses_invalid_required_role_lock(invalid_lock):
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    lease.configure_targets([
        {
            "target_id": f"mono:{role}",
            "speaker_group_id": "mono",
            "role": role,
            "geometry": f"near_field_driver:mono:{role}",
            "tone_frequency_hz": 250.0 if role == "woofer" else 6250.0,
            "commissioning_gain_db": 0.0,
        }
        for role in ("woofer", "tweeter")
    ])
    lease._outcomes["reference_axis_driver:mono:woofer"] = _locked_outcome(
        original=-30.0, locked=-10.0
    )
    lease._outcomes["reference_axis_driver:mono:tweeter"] = _locked_outcome(
        original=-30.0, locked=invalid_lock
    )

    async def get_volume():
        return -27.0

    async def unexpected_set(_value):
        pytest.fail("invalid summed lock set must not mutate volume")

    assert not await lease.acquire_summed_sweep_volume(
        "mono", get_volume, unexpected_set
    )


@pytest.mark.asyncio
async def test_reference_axis_sweep_never_falls_back_to_near_field_lock():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0, locked=-18.0
    )
    writes: list[float] = []

    async def get_volume() -> float:
        return -27.0

    async def set_volume(value: float) -> bool:
        writes.append(value)
        return True

    assert not await lease.acquire_driver_sweep_volume(
        "mono",
        "woofer",
        get_volume,
        set_volume,
        capture_geometry="reference_axis",
    )
    assert writes == []

    lease._outcomes["reference_axis_driver:mono:woofer"] = _locked_outcome(
        original=-30.0, locked=-3.5
    )
    assert await lease.acquire_driver_sweep_volume(
        "mono",
        "woofer",
        get_volume,
        set_volume,
        capture_geometry="reference_axis",
    )
    assert writes == [-3.5]
    assert lease.driver_sweep_locked_main_volume_db(
        "mono", "woofer", capture_geometry="reference_axis"
    ) == -3.5


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


@pytest.mark.asyncio
async def test_reference_axis_level_ramp_uses_bounded_listening_position_cap(
    monkeypatch,
):
    from jasper.audio_measurement import ramp
    from jasper.correction import level_match
    from jasper.audio_measurement.ramp import (
        LISTENING_POSITION_CAP_BUMP_DB,
        LISTENING_POSITION_CAP_CEIL_DB,
    )
    from jasper.web import correction_crossover_backend as backend

    ramp_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ramp.MeasurementRamp,
        "from_env",
        classmethod(lambda cls, **kwargs: ramp_calls.append(kwargs) or object()),
    )

    outcome = SimpleNamespace(locked=False, ramp=SimpleNamespace(restored=False))

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def run_for_geometry(self, geometry, **_ports):
            assert geometry == "reference_axis_driver:mono:woofer"
            return outcome

    monkeypatch.setattr(level_match, "LevelMatchSession", FakeSession)
    lease = backend.CrossoverLevelLease()
    current = -30.0

    async def get_volume():
        return current

    async def set_volume(value):
        nonlocal current
        current = value
        return True

    assert (
        await lease.run_level_match(
            "reference_axis_driver:mono:woofer",
            get_main_volume_db=get_volume,
            set_main_volume_db=set_volume,
        )
        is outcome
    )
    assert ramp_calls == [
        {
            "allow_bounded_low_level": True,
            "cap_bump_db": LISTENING_POSITION_CAP_BUMP_DB,
            "cap_ceil_db": LISTENING_POSITION_CAP_CEIL_DB,
        }
    ]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "geometry",
    (
        "",
        "near_field_driver:mono:Woofer",
        "near_field_driver:mono:subwoofer",
        "reference_axis_driver",
        "reference_axis_driver:",
        "reference_axis_driver:mono",
        "reference_axis_driver::woofer",
    ),
)
async def test_run_level_match_rejects_noncanonical_geometry_before_ramp(geometry):
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    with pytest.raises(ValueError):
        await lease.run_level_match(geometry)


@pytest.mark.asyncio
async def test_sweep_lease_persists_restore_when_volume_write_response_is_lost(
    tmp_path,
):
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    state_path = tmp_path / "volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
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

    assert (await lease.finish_sweep_volume(restore, get_volume)).value == (
        "exact_restored"
    )
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
async def test_sweep_lease_rejects_positive_target_before_any_dsp_call():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0,
        locked=0.1,
    )

    async def get_volume() -> float:
        pytest.fail("positive target must be rejected before reading DSP volume")

    async def set_volume(_value: float) -> bool:
        pytest.fail("positive target must be rejected before writing DSP volume")

    assert not await lease.acquire_driver_sweep_volume(
        "mono", "woofer", get_volume, set_volume
    )


@pytest.mark.asyncio
async def test_sweep_lease_uses_emergency_attenuation_after_restore_rejection():
    from jasper.web.correction_crossover_backend import (
        EMERGENCY_SWEEP_VOLUME_DB,
        CrossoverLevelLease,
        UnresolvedVolumeRecoveryResult,
    )

    lease = CrossoverLevelLease()
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0,
        locked=-8.0,
    )
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

    assert await lease.acquire_driver_sweep_volume(
        "mono", "woofer", get_volume, set_volume
    )
    assert (
        await lease.finish_sweep_volume(set_volume, get_volume)
        is UnresolvedVolumeRecoveryResult.EMERGENCY_ATTENUATED
    )
    assert writes == [-8.0, -27.0, EMERGENCY_SWEEP_VOLUME_DB]
    assert lease.sweep_volume_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "geometry",
    (
        "near_field_driver:mono:woofer",
        "reference_axis_driver:mono:woofer",
    ),
)
async def test_level_intent_write_failure_prevents_volume_mutation(
    monkeypatch, geometry
):
    from jasper.web import correction_crossover_backend as backend

    def refuse_persist(_path, _payload):
        raise OSError("read-only state directory")

    monkeypatch.setattr(backend, "_write_volume_safety_state", refuse_persist)
    lease = backend.CrossoverLevelLease(volume_safety_state_path="state.json")
    writes = []

    async def get_volume():
        return -27.0

    async def set_volume(value):
        writes.append(value)
        return True

    with pytest.raises(OSError, match="read-only"):
        await lease.run_level_match(
            geometry,
            get_main_volume_db=get_volume,
            set_main_volume_db=set_volume,
        )

    assert writes == []


@pytest.mark.asyncio
async def test_sweep_intent_write_failure_prevents_volume_mutation(monkeypatch):
    from jasper.web import correction_crossover_backend as backend

    lease = backend.CrossoverLevelLease(volume_safety_state_path="state.json")
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0,
        locked=-8.0,
    )

    def refuse_persist(_path, _payload):
        raise OSError("read-only state directory")

    monkeypatch.setattr(backend, "_write_volume_safety_state", refuse_persist)
    writes = []

    async def get_volume():
        return -27.0

    async def set_volume(value):
        writes.append(value)
        return True

    with pytest.raises(OSError, match="read-only"):
        await lease.acquire_driver_sweep_volume(
            "mono", "woofer", get_volume, set_volume
        )

    assert writes == []


@pytest.mark.asyncio
async def test_sweep_dual_recovery_failure_survives_restart(tmp_path):
    from jasper.web.correction_crossover_backend import (
        CrossoverLevelLease,
        UnresolvedVolumeRecoveryResult,
    )

    state_path = tmp_path / "volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
    lease._outcomes["near_field_driver:mono:woofer"] = _locked_outcome(
        original=-30.0,
        locked=-8.0,
    )
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

    assert await lease.acquire_driver_sweep_volume(
        "mono", "woofer", get_volume, set_volume
    )
    assert (
        await lease.finish_sweep_volume(set_volume, get_volume)
        is UnresolvedVolumeRecoveryResult.FAILED
    )
    assert writes == [-8.0, -27.0, -60.0]
    assert (
        CrossoverLevelLease(
            volume_safety_state_path=state_path
        ).unresolved_volume_safety
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "geometry",
    (
        "near_field_driver:mono:woofer",
        "reference_axis_driver:mono:woofer",
    ),
)
async def test_level_dual_recovery_failure_survives_restart(
    monkeypatch, tmp_path, geometry
):
    from jasper.audio_measurement.ramp import RampState
    from jasper.correction import level_match
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    outcome = SimpleNamespace(
        locked=True,
        ramp=SimpleNamespace(state=RampState.LOCKED, restored=False),
    )

    class FakeSession:
        def __init__(self, *, store, **_kwargs):
            self.store = store

        async def run_for_geometry(self, requested, **_ports):
            assert requested == geometry
            return outcome

    monkeypatch.setattr(level_match, "LevelMatchSession", FakeSession)
    state_path = tmp_path / "volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
    writes = []

    async def get_volume():
        return -27.0

    async def refuse_volume(value):
        writes.append(value)
        return False

    with pytest.raises(RuntimeError, match="recover the crossover volume"):
        await lease.run_level_match(
            geometry,
            get_main_volume_db=get_volume,
            set_main_volume_db=refuse_volume,
        )

    assert writes == [-27.0, -60.0]
    assert geometry not in lease._outcomes
    assert (
        CrossoverLevelLease(
            volume_safety_state_path=state_path
        ).unresolved_volume_safety
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "geometry",
    (
        "near_field_driver:mono:woofer",
        "reference_axis_driver:mono:woofer",
    ),
)
async def test_level_cancel_during_restore_drains_then_discards_identity(
    monkeypatch, tmp_path, geometry
):
    import asyncio

    from jasper.audio_measurement.ramp import RampState
    from jasper.correction import level_match
    from jasper.correction.level_match import MeasurementLevelLock
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    outcome = SimpleNamespace(
        locked=True,
        ramp=SimpleNamespace(state=RampState.LOCKED, restored=False),
    )

    class FakeSession:
        def __init__(self, *, store, **_kwargs):
            self.store = store

        async def run_for_geometry(self, requested, **_ports):
            self.store.put(
                MeasurementLevelLock(
                    geometry=requested,
                    main_volume_db=-8.0,
                    gain_map_db=None,
                    settled_mic_dbfs=None,
                    noise_floor_dbfs=None,
                )
            )
            return outcome

    monkeypatch.setattr(level_match, "LevelMatchSession", FakeSession)
    state_path = tmp_path / "volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    current = -27.0

    async def get_volume():
        return current

    async def blocked_restore(value):
        nonlocal current
        restore_started.set()
        await release_restore.wait()
        current = value
        return True

    task = asyncio.create_task(
        lease.run_level_match(
            geometry,
            get_main_volume_db=get_volume,
            set_main_volume_db=blocked_restore,
        )
    )
    await restore_started.wait()
    task.cancel()
    release_restore.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert current == -27.0
    assert lease.level_lock_store.get(geometry) is None
    assert geometry not in lease._outcomes
    assert (
        CrossoverLevelLease(
            volume_safety_state_path=state_path
        ).unresolved_volume_safety
        is None
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
