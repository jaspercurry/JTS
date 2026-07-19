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


def _profile_and_targets(
    *,
    woofer_peak: float = -30.0,
    tweeter_peak: float = -70.0,
    sensitivities: dict[str, float] | None = None,
):
    topology = mono_output_topology()
    sensitivities = sensitivities or {}

    def _driver(target_id, role, peak, required):
        entry = {
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
        if role in sensitivities:
            entry["sensitivity_db_2v83_1m"] = sensitivities[role]
        return entry

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


def test_session_measurement_volume_targets_the_least_sensitive_driver():
    # The B1 rule: V = min(reference -20, max(caps)). The HIGHEST cap (the
    # least-sensitive driver) governs; more-sensitive drivers attenuate DOWN
    # digitally (always satisfiable), never the other way around.
    profile, targets = _profile_and_targets(woofer_peak=0.0, tweeter_peak=-65.0)
    # caps: woofer min(0, 0) = 0; tweeter min(-65, -65) = -65; max = 0 -> V = -20.
    assert session_measurement_volume_db(profile, targets.values()) == -20.0

    # When the highest cap binds BELOW the reference, it wins.
    profile2, targets2 = _profile_and_targets(woofer_peak=-30.0, tweeter_peak=-70.0)
    # caps: woofer -30, tweeter -70; max = -30 -> V = min(-20, -30) = -30.
    assert session_measurement_volume_db(profile2, targets2.values()) == -30.0


def test_session_measurement_volume_unaffected_by_hf_ceiling_derivation():
    """W6.5 pin: this module exclusively serves the program-admission v2
    conductor, so it always resolves ceilings on the proven-HP path. With
    JTS3's sensitivities declared and the tweeter at its -65 seed, the
    tweeter's OWN resolved cap moves from -65 to -35 (derived) -- but
    ``max(caps)`` is still the woofer's -8, so the derived session volume is
    unchanged. No behavior change expected; this pins that.
    """
    profile, targets = _profile_and_targets(
        woofer_peak=-8.0,
        tweeter_peak=-65.0,
        sensitivities={"woofer": 83.3, "tweeter": 108.5},
    )
    assert session_measurement_volume_db(profile, targets.values()) == -20.0


def test_session_measurement_volume_refuses_unmeasurable_profile():
    # Every cap at or below the -60 dB emergency floor: no driver can be
    # measured at a safe volume -> typed refusal, never a zero-SNR session.
    # (This invariant would have caught the inverted min(caps) derivation.)
    profile, targets = _profile_and_targets(woofer_peak=-65.0, tweeter_peak=-70.0)
    with pytest.raises(
        SessionVolumePlanError, match="profile_unmeasurable_at_safe_volume"
    ):
        session_measurement_volume_db(profile, targets.values())


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


def test_needs_recovery_true_for_unresolved_and_foreign_active(tmp_path):
    # Branch 1: latched unresolved -> needs_recovery (and surfaced payload).
    p1 = tmp_path / "sv1.json"
    vol = FakeVolume(initial=-6.0, confirm_targets=set())
    plan1 = SessionVolumePlan(state_path=p1)
    asyncio.run(plan1.open(-12.0, vol.set, vol.get))  # nothing confirms
    assert plan1.unresolved_volume_safety is not None
    assert plan1.needs_recovery is True

    # Branch 2: crash-hydrated active within the ceiling -> needs_recovery is
    # the ONLY surfaced signal (unresolved_volume_safety stays None).
    p2 = tmp_path / "sv2.json"
    vol2 = FakeVolume(initial=-6.0)
    opener = SessionVolumePlan(
        state_path=p2, wall_clock_ceiling_s=1800.0, clock=lambda: 1000.0
    )
    asyncio.run(opener.open(-12.0, vol2.set, vol2.get))
    assert opener.needs_recovery is False  # owned by this process
    reborn = SessionVolumePlan(
        state_path=p2, wall_clock_ceiling_s=1800.0, clock=lambda: 1005.0
    )
    assert reborn.unresolved_volume_safety is None
    assert reborn.needs_recovery is True

    # Draining resolves both signals.
    asyncio.run(reborn.recover_unresolved(vol2.set, vol2.get))
    assert reborn.needs_recovery is False

    # No state at all -> nothing to recover.
    assert SessionVolumePlan().needs_recovery is False


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
