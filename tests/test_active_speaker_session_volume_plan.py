# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-scoped fixed measurement volume + fail-closed latch (Wave 2 C).

Pins the SSOT derivation and the durable latch semantics: intent written BEFORE
the first volume mutation, restore-exactly-once, readback-confirm failure ->
unresolved / emergency, wall-clock ceiling force-drain (live and on hydration),
and crash hydration staying fail-closed without relying on a process restart to
flip states.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.session_volume_plan import (
    SessionVolumeOpenResult,
    SessionVolumePlan,
    SessionVolumePlanError,
    SessionVolumeRestoreResult,
    session_measurement_volume_db,
)
from tests.active_speaker_fixtures import mono_output_topology


def _profile_and_targets(*, woofer_peak: float = -30.0, tweeter_peak: float = -70.0):
    topology = mono_output_topology()

    def _driver(target_id, role, peak, required):
        return {
            "target_id": target_id,
            "role": role,
            "model": f"model-{role}",
            "hard_excitation_band_hz": [500, 20_000],
            "measurement_band_hz": [500, 10_000],
            "crossover_search_band_hz": [1500, 2500],
            "level_duration_limits": {
                "max_effective_peak_dbfs": peak,
                "max_sweep_duration_s": 6,
                "max_repeat_count": 3,
                "minimum_cooldown_s": 0,
            },
            "required_protection_filters": required,
            "cabinet": {
                "enclosure_kind": "sealed",
                "radiator_count": 1,
                "effective_radiating_diameter_mm": 132 if role == "woofer" else 25,
                **({"baffle_width_mm": 210} if role == "woofer" else {}),
            },
        }

    settings = {
        "drivers": [
            _driver(
                "mono:woofer",
                "woofer",
                woofer_peak,
                [{"kind": "lowpass", "cutoff_hz": 3000, "minimum_slope_db_per_octave": 24}],
            ),
            _driver(
                "mono:tweeter",
                "tweeter",
                tweeter_peak,
                [{"kind": "highpass", "cutoff_hz": 5000, "minimum_slope_db_per_octave": 24}],
            ),
        ],
        "crossover_candidates": [],
    }
    profile = build_driver_safety_profile(
        topology,
        manual_settings=settings,
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )
    targets = {t["role"]: t["target_fingerprint"] for t in active_driver_targets(topology)}
    return profile, targets


class FakeVolume:
    """Records set/get ordering; confirms only the listed targets (else drifts)."""

    def __init__(self, *, initial=-6.0, confirm_targets=None, on_set=None):
        self.value = initial
        self.confirm_targets = confirm_targets  # None => confirm everything
        self.order: list = []
        self._on_set = on_set

    async def set(self, target):
        self.order.append(("set", round(float(target), 3)))
        if self._on_set is not None:
            self._on_set()
        if self.confirm_targets is None or round(float(target), 3) in {
            round(float(t), 3) for t in self.confirm_targets
        }:
            self.value = float(target)
        else:
            self.value = 999.0  # drifted: readback will not confirm the target
        return True

    async def get(self):
        self.order.append(("get", round(float(self.value), 3)))
        return self.value


# --- SSOT derivation ---------------------------------------------------------


def test_session_measurement_volume_is_the_minimum_cap():
    profile, targets = _profile_and_targets(woofer_peak=-30.0, tweeter_peak=-70.0)
    vol = session_measurement_volume_db(profile, targets.values())
    # woofer cap = min(-30, 0) = -30; tweeter cap = min(-70, -65) = -70; min = -70.
    assert vol == -70.0


def test_session_measurement_volume_requires_targets():
    profile, _ = _profile_and_targets()
    with pytest.raises(SessionVolumePlanError):
        session_measurement_volume_db(profile, [])


# --- latch: intent before mutation ------------------------------------------


def test_open_writes_active_intent_before_first_mutation(tmp_path):
    p = tmp_path / "sv.json"
    statuses_seen: list[str] = []

    def _record_status():
        statuses_seen.append(json.loads(p.read_text())["status"])

    vol = FakeVolume(initial=-6.0, on_set=_record_status)
    plan = SessionVolumePlan(state_path=p)

    result = asyncio.run(plan.open(-12.0, vol.set, vol.get))
    assert result is SessionVolumeOpenResult.OPENED
    # The durable state was already 'active' at the moment of the first set.
    assert statuses_seen and statuses_seen[0] == "active"
    on_disk = json.loads(p.read_text())
    assert on_disk["status"] == "active"
    assert "opened_at" in on_disk
    assert plan.measurement_volume_db == -12.0
    plan.assert_ready()


# --- latch: restore-once idempotence ----------------------------------------


def test_restore_is_exact_and_once():
    vol = FakeVolume(initial=-6.0)
    plan = SessionVolumePlan()
    assert asyncio.run(plan.open(-12.0, vol.set, vol.get)) is SessionVolumeOpenResult.OPENED
    assert vol.value == -12.0
    first = asyncio.run(plan.close(vol.set, vol.get))
    assert first is SessionVolumeRestoreResult.EXACT_RESTORED
    assert vol.value == -6.0  # original restored
    set_calls = sum(1 for e in vol.order if e[0] == "set")
    again = asyncio.run(plan.close(vol.set, vol.get))
    assert again is SessionVolumeRestoreResult.ALREADY_RESOLVED
    # Idempotent: a second close performs no further volume mutation.
    assert sum(1 for e in vol.order if e[0] == "set") == set_calls
    assert plan.unresolved_volume_safety is None


# --- latch: readback-confirm failure ----------------------------------------


def test_open_confirm_failure_falls_back_to_emergency():
    # Neither the measurement volume nor the original confirms; emergency does.
    vol = FakeVolume(initial=-6.0, confirm_targets={-60.0})
    plan = SessionVolumePlan()
    result = asyncio.run(plan.open(-12.0, vol.set, vol.get))
    assert result is SessionVolumeOpenResult.EMERGENCY_ATTENUATED
    assert vol.value == -60.0  # emergency floor
    # Emergency confirmed => resolved (no lingering unresolved risk).
    assert plan.unresolved_volume_safety is None


def test_open_confirm_failure_no_fallback_latches_unresolved(tmp_path):
    # Nothing confirms -> measurement, exact, AND emergency all fail.
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0, confirm_targets=set())
    plan = SessionVolumePlan(state_path=p)
    result = asyncio.run(plan.open(-12.0, vol.set, vol.get))
    assert result is SessionVolumeOpenResult.FAILED
    unresolved = plan.unresolved_volume_safety
    assert unresolved is not None
    assert unresolved["emergency_volume_db"] == -60.0
    assert json.loads(p.read_text())["status"] == "unresolved"


# --- ceiling force-drain (live + hydration) ---------------------------------


def test_wall_clock_ceiling_force_drains_stale_active(tmp_path):
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=10.0, clock=lambda: 1000.0)
    asyncio.run(opener.open(-12.0, vol.set, vol.get))
    assert vol.value == -12.0

    # A fresh process hydrates the durable state well past the ceiling.
    later = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=10.0, clock=lambda: 5000.0)
    assert later.stale_active() is True
    with pytest.raises(SessionVolumePlanError):
        later.assert_ready()
    drained = asyncio.run(later.enforce_ceiling(vol.set, vol.get))
    assert drained is SessionVolumeRestoreResult.EXACT_RESTORED
    assert vol.value == -6.0
    assert json.loads(p.read_text())["status"] == "resolved"


def test_enforce_ceiling_noop_when_fresh(tmp_path):
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1000.0)
    asyncio.run(opener.open(-12.0, vol.set, vol.get))
    fresh = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1001.0)
    assert fresh.stale_active() is False
    assert asyncio.run(fresh.enforce_ceiling(vol.set, vol.get)) is None
    assert vol.value == -12.0  # untouched


# --- crash hydration is fail-closed -----------------------------------------


def test_crash_hydrated_active_is_not_ready_until_recovered(tmp_path):
    p = tmp_path / "sv.json"
    vol = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1000.0)
    asyncio.run(opener.open(-12.0, vol.set, vol.get))

    # Simulate a restart: new instance hydrates the SAME durable active state,
    # still within the ceiling. The status is NOT flipped to unresolved...
    reborn = SessionVolumePlan(state_path=p, wall_clock_ceiling_s=1800.0, clock=lambda: 1005.0)
    assert reborn.unresolved_volume_safety is None
    assert reborn.stale_active() is False
    # ...but the volume is not owned by this process, so it is not usable.
    with pytest.raises(SessionVolumePlanError):
        reborn.assert_ready()
    # recover_unresolved drains it (unlike the lease, this does not refuse active).
    recovered = asyncio.run(reborn.recover_unresolved(vol.set, vol.get))
    assert recovered is SessionVolumeRestoreResult.EXACT_RESTORED
    assert vol.value == -6.0


def test_hydrated_malformed_state_is_unresolved(tmp_path):
    p = tmp_path / "sv.json"
    p.write_text("{ not valid json")
    plan = SessionVolumePlan(state_path=p)
    assert plan.unresolved_volume_safety is not None
    with pytest.raises(SessionVolumePlanError):
        plan.assert_ready()


def test_open_refuses_over_unresolved_state(tmp_path):
    p = tmp_path / "sv.json"
    p.write_text("garbage")
    vol = FakeVolume()
    plan = SessionVolumePlan(state_path=p)
    with pytest.raises(SessionVolumePlanError, match="recover it"):
        asyncio.run(plan.open(-12.0, vol.set, vol.get))
