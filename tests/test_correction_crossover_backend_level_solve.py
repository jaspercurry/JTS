# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop level solver wiring into the driver-capture path (W2.1).

Pins: an isolated driver sweep reasserts the SOLVED level (not the raw ramp
lock), a refusal fires before any tone plays and does NOT invalidate the
driver's level lock, ``level_match_snapshot()`` surfaces the refusal for the
envelope, the bounded-correction escalation fires at most once, and a solve
that cannot resolve its ceilings falls back to the pre-W2.1 raw-lock
behavior cleanly.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.measurement import active_driver_targets
from jasper.audio_measurement.ramp import RampLockKind, RampState
from jasper.web.correction_crossover_backend import CrossoverLevelLease, LevelSolveRefused
from tests.active_speaker_fixtures import mono_output_topology


def _safety_profile_and_targets(
    *, woofer_max_effective_peak_dbfs: float = -8.0
):
    topology = mono_output_topology()
    common = {
        "hard_excitation_band_hz": [20, 20_000],
        "measurement_band_hz": [20, 20_000],
        "crossover_search_band_hz": [1500, 2500],
        "level_duration_limits": {
            "max_sweep_duration_s": 4,
            "max_repeat_count": 3,
            "minimum_cooldown_s": 1,
        },
    }
    settings = {
        "drivers": [
            {
                **common,
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "Example W6",
                "level_duration_limits": {
                    **common["level_duration_limits"],
                    "max_effective_peak_dbfs": woofer_max_effective_peak_dbfs,
                },
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
                **common,
                "target_id": "mono:tweeter",
                "role": "tweeter",
                "model": "Example T1",
                # Tweeters are hard-capped by driver_protection.py's own
                # code policy (max_auto_level_dbfs = -65.0 for HIGH_FREQUENCY_ROLES)
                # regardless of what a manual setting requests -- this value
                # must already comply or build_driver_safety_profile refuses
                # to confirm.
                "level_duration_limits": {
                    **common["level_duration_limits"],
                    "max_effective_peak_dbfs": -65.0,
                },
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
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )
    targets = {t["role"]: t for t in active_driver_targets(topology)}
    return topology, profile, targets


def _patch_solve_environment(monkeypatch, topology, profile):
    import jasper.output_topology as output_topology_mod
    from jasper.active_speaker import design_draft as design_draft_mod

    monkeypatch.setattr(output_topology_mod, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        design_draft_mod,
        "load_design_draft",
        lambda *args, **kwargs: {"driver_safety_profile": profile},
    )


def _ramp_outcome(
    *,
    locked: float,
    gain_map_db: float,
    cap_db: float,
    noise_floor_dbfs: float | None,
):
    return SimpleNamespace(
        ramp=SimpleNamespace(
            state=RampState.LOCKED,
            locked_main_volume_db=locked,
            gain_map_db=gain_map_db,
            cap_db=cap_db,
            noise_floor_dbfs=noise_floor_dbfs,
            lock_kind=RampLockKind.IN_WINDOW,
        ),
    )


def _configure_lease(lease, targets, *, commissioning_gain_db=-5.0):
    lease.configure_targets(
        [
            {
                "target_id": target["target_id"],
                "speaker_group_id": target["speaker_group_id"],
                "role": target["role"],
                "geometry": f"near_field_driver:{target['speaker_group_id']}:{target['role']}",
                "tone_frequency_hz": 1000.0,
                "commissioning_gain_db": commissioning_gain_db,
                "target_fingerprint": target["target_fingerprint"],
            }
            for target in targets.values()
        ]
    )


async def _volume_ports(start_db: float):
    current = {"value": start_db}

    async def get_main_volume_db() -> float:
        return current["value"]

    async def set_main_volume_db(value: float) -> bool:
        current["value"] = value
        return True

    return current, get_main_volume_db, set_main_volume_db


@pytest.mark.asyncio
async def test_driver_sweep_reasserts_solved_volume_not_raw_lock(monkeypatch):
    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-3.0, noise_floor_dbfs=-42.3
    )
    current, get_v, set_v = await _volume_ports(-27.0)

    acquired = await lease.acquire_driver_sweep_volume("mono", "woofer", get_v, set_v)
    assert acquired is True
    # The solved level clears >=26 dB worst-band SNR against this ambient
    # with plenty of headroom (regression-shaped inputs) -- the reasserted
    # volume must NOT be the raw -20.0 dB lock.
    assert current["value"] != pytest.approx(-20.0)
    assert current["value"] <= 0.0


@pytest.mark.asyncio
async def test_refused_solve_raises_and_preserves_the_lock(monkeypatch):
    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    # Extremely insensitive chain + loud room: unreachable even at max
    # levers -- REFUSAL, not a best-effort SolvedLevel.
    lease._outcomes["near_field_driver:mono:tweeter"] = _ramp_outcome(
        locked=-3.0, gain_map_db=-60.0, cap_db=-3.0, noise_floor_dbfs=-20.0
    )
    _, get_v, set_v = await _volume_ports(-27.0)

    with pytest.raises(LevelSolveRefused):
        await lease.acquire_driver_sweep_volume("mono", "tweeter", get_v, set_v)

    # The refusal must not have touched the driver's level lock.
    assert "near_field_driver:mono:tweeter" in lease._outcomes
    assert (
        lease._outcomes["near_field_driver:mono:tweeter"].ramp.locked_main_volume_db
        == -3.0
    )
    # And must not have started a volume transition (no tone plays).
    assert lease.sweep_volume_active is False


@pytest.mark.asyncio
async def test_refusal_surfaces_on_level_match_snapshot(monkeypatch):
    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:tweeter"] = _ramp_outcome(
        locked=-3.0, gain_map_db=-60.0, cap_db=-3.0, noise_floor_dbfs=-20.0
    )
    _, get_v, set_v = await _volume_ports(-27.0)

    with pytest.raises(LevelSolveRefused):
        await lease.acquire_driver_sweep_volume("mono", "tweeter", get_v, set_v)

    refusal = lease.level_match_snapshot()["solve_refusal"]
    assert refusal is not None
    assert refusal["code"] == "room_too_noisy_for_safe_measurement"
    assert refusal["role"] == "tweeter"


@pytest.mark.asyncio
async def test_solve_falls_back_to_raw_lock_when_ceilings_unresolvable(monkeypatch):
    """No driver-safety profile confirmed -- the solve cannot resolve
    ceilings, so the pre-W2.1 raw-lock reassert behavior is preserved."""

    topology, _profile, targets = _safety_profile_and_targets()

    import jasper.output_topology as output_topology_mod
    from jasper.active_speaker import design_draft as design_draft_mod

    monkeypatch.setattr(output_topology_mod, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        design_draft_mod,
        "load_design_draft",
        lambda *args, **kwargs: {},  # no driver_safety_profile
    )

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-3.0, noise_floor_dbfs=-42.3
    )
    current, get_v, set_v = await _volume_ports(-27.0)

    acquired = await lease.acquire_driver_sweep_volume("mono", "woofer", get_v, set_v)
    assert acquired is True
    assert current["value"] == pytest.approx(-20.0)


@pytest.mark.asyncio
async def test_missing_gain_map_falls_back_to_raw_lock(monkeypatch):
    """A test double (or legacy in-memory outcome) without gain_map_db must
    degrade to the raw lock, not crash."""

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:woofer"] = SimpleNamespace(
        ramp=SimpleNamespace(
            state=RampState.LOCKED,
            locked_main_volume_db=-20.0,
        )
    )
    current, get_v, set_v = await _volume_ports(-27.0)

    acquired = await lease.acquire_driver_sweep_volume("mono", "woofer", get_v, set_v)
    assert acquired is True
    assert current["value"] == pytest.approx(-20.0)


def test_solve_escalation_fires_at_most_once(monkeypatch):
    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-3.0, noise_floor_dbfs=-42.3
    )

    baseline = lease._solve_driver_level(
        "mono", "woofer", capture_geometry="near_field"
    )
    lease.record_solve_escalation("mono", "woofer", shortfall_db=5.0)
    escalated = lease._solve_driver_level(
        "mono", "woofer", capture_geometry="near_field"
    )
    # A second escalation attempt must not stack on top of the first.
    lease.record_solve_escalation("mono", "woofer", shortfall_db=5.0)
    escalated_again = lease._solve_driver_level(
        "mono", "woofer", capture_geometry="near_field"
    )

    assert escalated.main_volume_db < baseline.main_volume_db
    assert escalated_again.main_volume_db == pytest.approx(escalated.main_volume_db)


def test_new_level_match_run_clears_solve_state(monkeypatch):
    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:tweeter"] = _ramp_outcome(
        locked=-3.0, gain_map_db=-60.0, cap_db=-3.0, noise_floor_dbfs=-20.0
    )
    lease._solve_driver_level("mono", "tweeter", capture_geometry="near_field")
    assert lease.level_match_snapshot()["solve_refusal"] is not None
    lease.record_solve_escalation("mono", "tweeter", shortfall_db=3.0)

    lease.invalidate_comparison_context()

    assert lease.level_match_snapshot()["solve_refusal"] is None
    assert lease._solve_escalation_db == {}


def test_record_driver_capture_escalates_on_measured_shortfall(monkeypatch):
    """Sweep 1's OWN measured verdict misses despite the solve predicting a
    safe level -- the wrapper escalates the lease's ambient assumption by
    exactly the measured shortfall, once."""

    import jasper.web.correction_crossover_backend as backend_mod

    lease = backend_mod.CrossoverLevelLease()
    monkeypatch.setattr(backend_mod, "_LEVEL_LEASE", lease)
    _, profile, targets = _safety_profile_and_targets()
    _configure_lease(lease, targets)

    monkeypatch.setattr(
        backend_mod.web_measurement,
        "record_driver_capture",
        lambda *args, **kwargs: {
            "recorded": False,
            "repeat_progress": {
                "attempts": 1,
                "accepted": 0,
                "latest_rejection": {
                    "accepted": False,
                    "reject_reason": "insufficient",
                    "snr_shortfall_db": 4.7,
                },
            },
        },
    )

    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )

    assert lease._solve_escalation_db == {"mono:woofer": pytest.approx(4.7)}

    # A second rejection for the SAME target must not stack a second bump.
    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )
    assert lease._solve_escalation_db == {"mono:woofer": pytest.approx(4.7)}


def test_record_driver_capture_does_not_escalate_on_acceptance(monkeypatch):
    import jasper.web.correction_crossover_backend as backend_mod

    lease = backend_mod.CrossoverLevelLease()
    monkeypatch.setattr(backend_mod, "_LEVEL_LEASE", lease)
    _, profile, targets = _safety_profile_and_targets()
    _configure_lease(lease, targets)

    monkeypatch.setattr(
        backend_mod.web_measurement,
        "record_driver_capture",
        lambda *args, **kwargs: {"recorded": True, "verdict": "ok"},
    )

    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )

    assert lease._solve_escalation_db == {}
