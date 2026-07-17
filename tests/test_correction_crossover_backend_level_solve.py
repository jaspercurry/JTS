# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop level solver wiring into the driver-capture path (W2.1/W2.2).

Pins: an isolated driver sweep reasserts the SOLVED level (not the raw ramp
lock), a refusal fires before any tone plays and does NOT invalidate the
driver's level lock, ``level_match_snapshot()`` surfaces the refusal for the
envelope, the bounded correction (W2.2: ONE signed adjustment slot, up to
``_MAX_SOLVE_CORRECTION_WRITES`` writes, clip de-escalation using the
driver's OWN measured mic peak, and a typed refusal past the bound), and a
solve that cannot resolve its ceilings falls back to the pre-W2.1 raw-lock
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
async def test_level_solve_refused_str_is_the_mapped_household_copy(monkeypatch):
    """W2.4 (hardware run 20): ``str(LevelSolveRefused(...))`` used to be the
    raw diagnostic string ``"level_solve_refused code=... band=...Hz"`` --
    the exact text that leaked to the household on TWO unmigrated surfaces:
    the phone's ``sweep_failed`` host event
    (``jasper.web.correction_crossover_flow``'s ``error=str(exc)``) and the
    wizard's relay status line
    (``jasper.web.correction_setup._relay_failure_message``'s generic
    ``str(exc)`` fallback). ``str(exc)`` must instead be EXACTLY the same
    mapped sentence the envelope renders
    (``jasper.active_speaker.crossover_envelope.describe_level_solve_refusal``)
    -- one code -> copy mapping, never a raw code/band string on any
    household surface."""

    from jasper.active_speaker.crossover_envelope import (
        describe_level_solve_refusal,
    )

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:tweeter"] = _ramp_outcome(
        locked=-3.0, gain_map_db=-60.0, cap_db=-3.0, noise_floor_dbfs=-20.0
    )
    _, get_v, set_v = await _volume_ports(-27.0)

    with pytest.raises(LevelSolveRefused) as excinfo:
        await lease.acquire_driver_sweep_volume("mono", "tweeter", get_v, set_v)

    message = str(excinfo.value)
    assert "level_solve_refused" not in message
    assert "code=" not in message
    assert "band=" not in message
    assert message == describe_level_solve_refusal(excinfo.value.refusal.to_dict())
    # Sanity: this IS the household-facing "too high to measure reliably"
    # room_too_noisy copy, not a generic/empty fallback.
    assert "too high to measure reliably at safe levels" in message


def test_refusal_pending_predicate_parity_with_envelope_rendering():
    """W2.4 parity pin: the between-set restart's clear/preserve decision
    (``CrossoverLevelLease._target_refusal_pending``, reading
    ``_solve_refusal`` + the exhausted write count directly) and the
    envelope's refusal rendering
    (``crossover_envelope._active_level_solve_refusal``, re-deriving the
    same OR from ``level_match_snapshot()``'s ``solve_refusal`` /
    ``solve_correction.exhausted`` projections) are SEPARATE readers of the
    same two stored facts. They are equivalent today only because both
    sides implement the same OR -- nothing structural forces it -- so this
    test pins ``envelope-renders-a-refusal <=> restart-clears`` across the
    three representative states. If either side changes, this is the
    contract to keep green: a divergence means the household could be shown
    a refusal whose restart preserves the doomed correction (run 20's dead
    loop) or have a correction cleared with no refusal ever rendered."""

    from jasper.active_speaker.crossover_envelope import (
        _active_level_solve_refusal,
    )

    _topology, _profile, targets = _safety_profile_and_targets()
    target_id = "mono:woofer"

    def parity(lease) -> None:
        status = {"level_match": lease.level_match_snapshot()}
        rendered = _active_level_solve_refusal(status, target_id) is not None
        pending = lease._target_refusal_pending(target_id)
        assert rendered == pending, (
            f"envelope renders refusal={rendered} but restart "
            f"refusal_pending={pending} -- the two readers of the same "
            "stored facts have diverged"
        )

    # State 1 (run 20's shape): genuine pre-flight refusal at writes=1 --
    # one write BELOW the exhausted bound. Both readers must say refusal.
    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease.record_solve_correction(
        "mono", "woofer", trigger="completed_insufficient", shortfall_db=12.3
    )
    lease._solve_refusal = {
        "target_id": target_id,
        "role": "woofer",
        "code": "room_too_noisy_for_safe_measurement",
        "failing_band_hz": [60.0, 171.0],
        "required_db": 20.0,
        "available_db": 14.5,
    }
    assert lease._correction_budget_exhausted(target_id) is False
    assert lease._target_refusal_pending(target_id) is True
    parity(lease)

    # State 2 (run 19's completion-time exhaustion shape): budget exhausted
    # with NO fresh solve refusal stored -- the envelope synthesizes the
    # measurement_window_unreachable refusal from the exhausted flag; the
    # restart must agree via the same write count.
    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    for _ in range(3):
        lease.record_solve_correction(
            "mono", "woofer", trigger="snr_shortfall", shortfall_db=2.0
        )
    assert lease._solve_refusal is None
    assert lease._correction_budget_exhausted(target_id) is True
    assert lease._target_refusal_pending(target_id) is True
    parity(lease)

    # State 3 (the preserve path): a correction exists but no refusal was
    # ever shown and the budget is not exhausted. Both readers must say
    # no-refusal -- this is what keeps the completed-insufficient
    # "JTS will play the next measurement louder" promise intact.
    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease.record_solve_correction(
        "mono", "woofer", trigger="completed_insufficient", shortfall_db=12.3
    )
    assert lease._solve_refusal is None
    assert lease._target_refusal_pending(target_id) is False
    parity(lease)


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


def test_solve_correction_snr_shortfall_stacks_and_bounds_at_two_writes(monkeypatch):
    """W2.2 (generalizes B1): escalating the assumed ambient (a LOUDER,
    less-negative dBFS figure) must monotonically RAISE the solved level --
    each shortfall rejection needs MORE headroom, never less. Unlike the
    pre-W2.2 "at most once" behavior, a SECOND shortfall now stacks on top
    of the first (bounded at _MAX_SOLVE_CORRECTION_WRITES writes); a THIRD
    rejection past the bound does not write a third correction -- the very
    next solve refuses pre-flight instead, and that refusal must NOT
    invalidate the driver's level lock."""

    from jasper.active_speaker.excitation_safety_plan import (
        DriverSweepGeneratorPlan,
    )
    from jasper.audio_measurement import level_solver
    from jasper.web.correction_crossover_backend import _MAX_SOLVE_CORRECTION_WRITES

    assert _MAX_SOLVE_CORRECTION_WRITES == 2

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    # A looser cap/quieter ambient than the tight regression fixture --
    # enough headroom that two +1 dB escalations both stay within every
    # ceiling (a tight fixture hits the PRE-EXISTING room_too_noisy refusal
    # on the second escalation, which is a different code path than the
    # one this test pins).
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-1.0, noise_floor_dbfs=-50.0
    )

    def _solve():
        return lease._solve_driver_level(
            "mono", "woofer", capture_geometry="near_field"
        )

    baseline = _solve()
    assert isinstance(baseline, level_solver.SolvedLevel)
    assert baseline.achieved_target is True

    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=1.0
    )
    write1 = _solve()
    assert isinstance(write1, level_solver.SolvedLevel)
    assert write1.achieved_target is True
    write1_total = write1.main_volume_db + write1.commissioning_gain_db
    baseline_total = baseline.main_volume_db + baseline.commissioning_gain_db
    assert write1_total == pytest.approx(baseline_total + 1.0)

    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=1.0
    )
    write2 = _solve()
    assert isinstance(write2, level_solver.SolvedLevel)
    assert write2.achieved_target is True
    write2_total = write2.main_volume_db + write2.commissioning_gain_db
    # Stacks on top of write1 (not a no-op, and not re-derived from scratch).
    assert write2_total == pytest.approx(write1_total + 1.0)

    # The stacked (twice-corrected) level still passes through admission:
    # every ceiling still binds.
    plan = DriverSweepGeneratorPlan(
        f1_hz=20.0,
        f2_hz=20_000.0,
        amplitude=10.0 ** (-12.0 / 20.0),
        duration_s=1.0,
        repeat_count=1,
        commissioning_gain_db=write2.commissioning_gain_db,
        main_volume_db=write2.main_volume_db,
    )
    assert plan.effective_peak_dbfs <= -8.0 + 1e-9  # driver-safety ceiling
    predicted_mic_peak = plan.effective_peak_dbfs + 1.9 - (-12.0)
    assert predicted_mic_peak <= level_solver.MIC_CLIP_CEILING_DBFS + 1e-9
    assert write2.main_volume_db <= -1.0 + 1e-9  # ramp cap

    # A THIRD rejection past the bound: no third correction is written; the
    # NEXT solve attempt refuses pre-flight instead of replaying a doomed
    # sweep a third time.
    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=1.0
    )
    write3 = _solve()
    assert isinstance(write3, level_solver.LevelSolveRefusal)
    assert write3.code == level_solver.REFUSAL_MEASUREMENT_WINDOW_UNREACHABLE

    # Refusal must NOT invalidate the driver's level lock -- same guarantee
    # as the existing room_too_noisy refusal.
    assert "near_field_driver:mono:woofer" in lease._outcomes
    assert (
        lease._outcomes["near_field_driver:mono:woofer"].ramp.locked_main_volume_db
        == -20.0
    )

    # A fourth rejection stays refused (idempotent) -- not a crash, and
    # still not a fourth guessed level.
    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=1.0
    )
    write4 = _solve()
    assert isinstance(write4, level_solver.LevelSolveRefusal)
    assert write4.code == level_solver.REFUSAL_MEASUREMENT_WINDOW_UNREACHABLE


def test_solve_correction_clip_trigger_deescalates_and_replaces_gain_source(
    monkeypatch,
):
    """W2.2 (hardware run 18): a clip rejection writes a NEGATIVE adjustment
    (de-escalation) sized from the clipped capture's OWN measured mic peak,
    and record_measured_gain's stored gain (once present) replaces the
    tone's gain_map_db for the solver's mic-clip ceiling ONLY -- both
    verified end to end through _solve_driver_level, not just the pure
    solve_level math (see test_audio_measurement_level_solver.py's
    regression pinned to the same run-18 numbers)."""

    from jasper.audio_measurement import level_solver

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets, commissioning_gain_db=0.0)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=-0.1, cap_db=-3.0, noise_floor_dbfs=-41.45
    )

    baseline = lease._solve_driver_level(
        "mono", "woofer", capture_geometry="near_field"
    )
    assert isinstance(baseline, level_solver.SolvedLevel)
    baseline_total = baseline.main_volume_db + baseline.commissioning_gain_db
    assert baseline_total == pytest.approx(-7.35, abs=0.01)

    # The clipped capture's own measured evidence: mic peak clamped to 0.0
    # dBFS, played at this exact baseline sum.
    lease.record_solve_correction(
        "mono", "woofer", trigger="clip", measured_mic_peak_dbfs=0.0
    )
    lease.record_measured_gain(
        "mono",
        "woofer",
        measured_mic_peak_dbfs=0.0,
        effective_peak_dbfs=-12.0 + baseline_total,
        clipped=True,
    )

    corrected = lease._solve_driver_level(
        "mono", "woofer", capture_geometry="near_field"
    )
    assert isinstance(corrected, level_solver.SolvedLevel)
    corrected_total = corrected.main_volume_db + corrected.commissioning_gain_db
    # Quieter than the clipped attempt -- the de-escalation, not louder.
    assert corrected_total < baseline_total
    assert corrected_total == pytest.approx(baseline_total - 15.0, abs=0.01)

    # The measured gain (padded) predicts exactly MIC_TARGET_PEAK_DBFS at
    # the corrected level -- the same identity the pure-solver regression
    # pins.
    measured_gain_db = lease._solve_measured_gain_db["mono:woofer"]
    assert corrected_total + measured_gain_db == pytest.approx(
        level_solver.MIC_TARGET_PEAK_DBFS, abs=0.05
    )


def test_measured_gain_cleared_by_set_completion_and_invalidate(
    monkeypatch,
):
    """W2.3: W2.2 item 2's stored measured gain shares the adjustment slot's
    TWO remaining clearing points -- set completion (clear_solve_correction)
    and a full invalidate (explicit flow reset) -- so a later, unrelated
    measurement never inherits a stale gain from an earlier attempt. A fresh
    ramp lock for the SAME geometry is deliberately no longer a clearing
    point as of W2.3 (hardware run 19) -- see
    test_fresh_ramp_lock_persists_that_targets_escalation."""

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-3.0, noise_floor_dbfs=-42.3
    )
    lease.record_measured_gain(
        "mono", "woofer", measured_mic_peak_dbfs=0.0, effective_peak_dbfs=-19.35,
        clipped=True,
    )
    assert "mono:woofer" in lease._solve_measured_gain_db
    assert "mono:woofer" in lease._solve_measured_peak_dbfs

    # 1. Set completion.
    lease.clear_solve_correction("mono", "woofer")
    assert "mono:woofer" not in lease._solve_measured_gain_db
    assert "mono:woofer" not in lease._solve_measured_peak_dbfs

    # 2. Full invalidate (explicit flow reset).
    lease.record_measured_gain(
        "mono", "woofer", measured_mic_peak_dbfs=0.0, effective_peak_dbfs=-19.35,
        clipped=True,
    )
    lease.invalidate_comparison_context()
    assert lease._solve_measured_gain_db == {}
    assert lease._solve_measured_peak_dbfs == {}


def test_between_set_invalidate_preserves_nonexhausted_and_clears_exhausted(
    monkeypatch,
):
    """W2.3 endpoint amendment: the between-set restart
    (invalidate_comparison_context(preserve_solve_corrections=True) -- the
    endpoint's non-continuing branch) distinguishes the two restarts by
    STORED STATE. A non-exhausted target's correction (the
    completed-insufficient terminal promised "JTS will play the next
    measurement louder") survives; an exhausted target (the placement
    refusal was showing) clears for a fresh evaluation."""

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    # Non-exhausted woofer (1 write), exhausted tweeter (2 writes + the
    # exhausted bump past the bound).
    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease.input_device = {"actual_device_id_hash": "mic-a"}
    lease.record_solve_correction(
        "mono", "woofer", trigger="completed_insufficient", shortfall_db=12.3
    )
    for _ in range(3):
        lease.record_solve_correction(
            "mono", "tweeter", trigger="snr_shortfall", shortfall_db=2.0
        )
    assert lease._correction_budget_exhausted("mono:woofer") is False
    assert lease._correction_budget_exhausted("mono:tweeter") is True

    lease.invalidate_comparison_context(preserve_solve_corrections=True)

    # Woofer preserved -- adjustment, write count, and mic identity binding.
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(12.3)}
    assert lease._solve_correction_writes == {"mono:woofer": 1}
    assert lease._solve_correction_device_key == {"mono:woofer": "mic-a"}
    # Tweeter cleared -- fresh evaluation; the synthesized refusal (keyed
    # off the same exhausted predicate) cannot latch across the restart.
    assert "mono:tweeter" not in lease._solve_adjustment_db
    assert "mono:tweeter" not in lease._solve_correction_writes
    assert lease._correction_budget_exhausted("mono:tweeter") is False
    # Everything else still reset exactly like the full invalidate.
    assert lease._targets == {}
    assert lease._outcomes == {}
    assert lease.context_id is None
    assert lease.input_device is None
    assert lease._solve_refusal is None

    # And the preserved correction still counts toward the bound afterwards:
    # re-configure (the endpoint re-freezes targets right after invalidate)
    # and stack a second write, then a third attempt exhausts.
    _configure_lease(lease, targets)
    lease.record_solve_correction(
        "mono", "woofer", trigger="completed_insufficient", shortfall_db=1.0
    )
    assert lease._solve_correction_writes == {"mono:woofer": 2}
    lease.record_solve_correction(
        "mono", "woofer", trigger="completed_insufficient", shortfall_db=1.0
    )
    assert lease._correction_budget_exhausted("mono:woofer") is True


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
    lease.record_solve_correction(
        "mono", "tweeter", trigger="snr_shortfall", shortfall_db=3.0
    )

    lease.invalidate_comparison_context()

    assert lease.level_match_snapshot()["solve_refusal"] is None
    assert lease._solve_adjustment_db == {}
    assert lease._solve_correction_writes == {}
    assert lease._solve_measured_gain_db == {}
    assert lease._solve_measured_peak_dbfs == {}


@pytest.mark.asyncio
async def test_fresh_ramp_lock_persists_that_targets_escalation(monkeypatch):
    """W2.3 (hardware run 19, replaces the old S2 "fresh-ramp-clears" rule):
    two full woofer repeat sets at the tester's stationary desk placement
    measured near-identical solve inputs before and after the household
    restarted the level check -- a fresh ramp re-measures the ROOM's
    ambient, not the mic-placement/leakage physics the correction is
    compensating for. Lock, escalate, re-lock the SAME target: the old
    escalation must SURVIVE, and the next solve must be LOUDER by exactly
    the persisted shortfall (not silently reset to the un-corrected level)."""

    from jasper.correction import level_match
    from jasper.audio_measurement.ramp import RampState
    from jasper.audio_measurement import level_solver

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    # A looser cap/quieter ambient than the tight regression fixture used
    # elsewhere -- enough headroom that a +5 dB escalation stays within
    # every ceiling via main_volume_db alone (see
    # test_solve_correction_snr_shortfall_stacks_and_bounds_at_two_writes'
    # identical rationale). A tight fixture pins main_volume_db at its cap
    # before the escalation lands, so the corrected total is NOT simply
    # baseline + shortfall_db -- a different, already-covered code path.
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-1.0, noise_floor_dbfs=-50.0
    )
    baseline = lease._solve_driver_level(
        "mono", "woofer", capture_geometry="near_field"
    )
    assert isinstance(baseline, level_solver.SolvedLevel)
    baseline_total = baseline.main_volume_db + baseline.commissioning_gain_db

    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=5.0
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(5.0)}

    fresh_outcome = SimpleNamespace(
        locked=True,
        ramp=SimpleNamespace(
            state=RampState.LOCKED,
            restored=True,
            locked_main_volume_db=-18.0,
            gain_map_db=1.9,
            cap_db=-1.0,
            noise_floor_dbfs=-50.0,
        ),
    )

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def run_for_geometry(self, geometry, **_ports):
            assert geometry == "near_field_driver:mono:woofer"
            return fresh_outcome

    monkeypatch.setattr(level_match, "LevelMatchSession", FakeSession)
    current = {"value": -30.0}

    async def get_volume():
        return current["value"]

    async def set_volume(value):
        current["value"] = value
        return True

    outcome = await lease.run_level_match(
        "near_field_driver:mono:woofer",
        get_main_volume_db=get_volume,
        set_main_volume_db=set_volume,
    )
    assert outcome is fresh_outcome
    # The escalation is NOT cleared by the re-lock.
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(5.0)}

    # And the NEXT solve against the fresh (re-locked) ramp is LOUDER by
    # exactly the persisted shortfall -- not silently re-derived from
    # scratch at the un-corrected level.
    corrected = lease._solve_driver_level(
        "mono", "woofer", capture_geometry="near_field"
    )
    assert isinstance(corrected, level_solver.SolvedLevel)
    corrected_total = corrected.main_volume_db + corrected.commissioning_gain_db
    assert corrected_total == pytest.approx(baseline_total + 5.0)


@pytest.mark.asyncio
async def test_solve_runs_once_per_sweep_and_reads_consume_it(monkeypatch):
    """N1: _acquire_sweep_volume computes and stores the SolvedLevel; the
    excitation-ledger and gain-override reads consume the stored result --
    exactly ONE solve (and one measurement.level_solved event) per sweep."""

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-3.0, noise_floor_dbfs=-42.3
    )
    solve_calls = {"count": 0}
    real_solve = lease._solve_driver_level

    def counting_solve(*args, **kwargs):
        solve_calls["count"] += 1
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(lease, "_solve_driver_level", counting_solve)
    current, get_v, set_v = await _volume_ports(-27.0)

    acquired = await lease.acquire_driver_sweep_volume("mono", "woofer", get_v, set_v)
    assert acquired is True
    reasserted = current["value"]

    ledger = lease.driver_sweep_locked_main_volume_db(
        "mono", "woofer", capture_geometry="near_field"
    )
    override = lease.solved_commissioning_gain_db(
        "mono", "woofer", capture_geometry="near_field"
    )

    assert solve_calls["count"] == 1
    assert ledger == pytest.approx(reasserted)
    assert override is not None
    stored = lease._active_sweep_solve
    assert stored is not None
    assert override == pytest.approx(stored[3].commissioning_gain_db)

    # The stored solve is scoped to the sweep window: finishing the sweep
    # clears it, and the ledger read falls back to the raw ramp lock.
    await lease.finish_sweep_volume(set_v, get_v)
    assert lease._active_sweep_solve is None
    assert lease.solved_commissioning_gain_db(
        "mono", "woofer", capture_geometry="near_field"
    ) is None
    assert lease.driver_sweep_locked_main_volume_db(
        "mono", "woofer", capture_geometry="near_field"
    ) == pytest.approx(-20.0)
    assert solve_calls["count"] == 1


def test_record_driver_capture_escalates_on_measured_shortfall(monkeypatch):
    """Sweep 1's OWN measured verdict misses despite the solve predicting a
    safe level -- the wrapper escalates the lease's ambient assumption by
    exactly the measured shortfall. Unlike the pre-W2.2 "at most once"
    behavior, a SECOND rejection for the SAME target now stacks (bounded at
    _MAX_SOLVE_CORRECTION_WRITES writes); a THIRD does not write a third
    correction."""

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
                    "clipping": False,
                    "snr_shortfall_db": 4.7,
                },
            },
        },
    )

    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(4.7)}

    # A second rejection for the SAME target STACKS (write 2 of 2).
    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(9.4)}

    # A third rejection past the bound does not stack a third bump.
    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(9.4)}
    assert lease._solve_correction_writes == {"mono:woofer": 3}


def test_record_driver_capture_clip_rejection_deescalates_and_stores_measured_gain(
    monkeypatch,
):
    """W2.2 (hardware run 18): a clip rejection writes a de-escalation from
    the rejected capture's OWN measured mic peak (peak_dbfs on the rejected
    attempt's admission_result, propagated through
    repeat_progress.latest_rejection) and, separately, refines the solver's
    stored mic-clip gain from the SAME evidence."""

    import jasper.web.correction_crossover_backend as backend_mod

    lease = backend_mod.CrossoverLevelLease()
    monkeypatch.setattr(backend_mod, "_LEVEL_LEASE", lease)
    _, profile, targets = _safety_profile_and_targets()
    _configure_lease(lease, targets, commissioning_gain_db=0.0)

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
                    "reject_reason": "unusable_capture",
                    "clipping": True,
                    "snr_shortfall_db": None,
                    "peak_dbfs": 0.0,
                    "effective_peak_dbfs": -19.35,
                },
            },
        },
    )

    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )

    # drop_db = (0.0 - (-12.0)) + 3.0 = 15.0 -- a de-escalation (negative).
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(-15.0)}
    assert lease._solve_correction_writes == {"mono:woofer": 1}
    # measured gain: 0.0 - (-19.35 - (-12.0)) + 3.0 (clipped allowance).
    assert lease._solve_measured_gain_db["mono:woofer"] == pytest.approx(
        10.35, abs=0.01
    )
    assert lease._solve_measured_peak_dbfs["mono:woofer"] == pytest.approx(0.0)


def test_record_driver_capture_clip_rejection_with_no_usable_peak_uses_zero(
    monkeypatch,
):
    """When the analyzer records no usable peak level for a clipped capture,
    the wrapper falls back to the conservative 0.0 dBFS measured value
    rather than skipping the correction."""

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
                    "reject_reason": "unusable_capture",
                    "clipping": True,
                    "snr_shortfall_db": None,
                    "peak_dbfs": None,
                    "effective_peak_dbfs": None,
                },
            },
        },
    )

    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )

    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(-15.0)}
    # No usable effective_peak_dbfs -- record_measured_gain has nothing to
    # compute from, so it does not fire (no crash, no bogus gain stored).
    assert lease._solve_measured_gain_db == {}


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

    assert lease._solve_adjustment_db == {}


def _run19_finalized_payload(estimated_snr_db: float) -> dict:
    """The exact shape ``_finalize_driver_repeat_set`` -> ``record_driver_
    acoustic_capture`` returns for hardware run 19: every attempt
    individually accepted (so ``repeat_progress.latest_rejection`` is
    absent -- W2.2's rejection-path correction never fires), yet the
    finalized WINNER capture's own ``acoustic.snr`` block still reads
    "insufficient" against the driver quality model's 20 dB warn floor."""

    return {
        "recorded": True,
        "verdict": "present",
        "acoustic": {
            "peak_dbfs": -20.0,
            "mic_clipping": False,
            "snr": {
                "schema_version": 1,
                "decision_class": "magnitude",
                "verdict": "insufficient",
                "worst_relevant": {
                    "band_id": "upper_bass",
                    "estimated_snr_db": estimated_snr_db,
                    "verdict": "insufficient",
                },
                "bands": [],
            },
        },
        "excitation": {"effective_peak_dbfs": -25.0},
    }


def test_record_driver_capture_writes_completion_correction_on_insufficient_aggregate(
    monkeypatch,
):
    """W2.3 (hardware run 19, pre-fix failing repro): two full woofer repeat
    sets, all 3 attempts accepted=true each time, per-attempt
    snr_verdict=insufficient (measured worst-band SNR 13.7/14.2/13.2 dB) --
    the aggregate finalizes with the SAME insufficient verdict. Zero
    level_solve_corrected events fired pre-fix, because #1552 only wired
    record_solve_correction from the per-attempt REJECTION path
    (repeat_progress.latest_rejection), and an accepted-but-insufficient
    finalization never rejects. This must now write ONE completion-time
    correction sized from the solver's OWN required threshold (floor 20 +
    margin 6 = 26 dB) minus the measured worst-band SNR."""

    import jasper.web.correction_crossover_backend as backend_mod
    from jasper.audio_measurement import level_solver
    from jasper.audio_measurement.quality_model import DRIVER as DRIVER_QUALITY_MODEL

    lease = backend_mod.CrossoverLevelLease()
    monkeypatch.setattr(backend_mod, "_LEVEL_LEASE", lease)
    _, profile, targets = _safety_profile_and_targets()
    _configure_lease(lease, targets)

    monkeypatch.setattr(
        backend_mod.web_measurement,
        "record_driver_capture",
        lambda *args, **kwargs: _run19_finalized_payload(13.7),
    )

    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )

    required_db = level_solver.driver_solve_requirement_db(DRIVER_QUALITY_MODEL)
    assert required_db == pytest.approx(26.0)
    assert lease._solve_adjustment_db == {
        "mono:woofer": pytest.approx(required_db - 13.7)
    }
    assert lease._solve_correction_writes == {"mono:woofer": 1}
    # Not cleared -- an insufficient finalization is NOT set-completion
    # success (W2.3).
    assert "mono:woofer" in lease._solve_adjustment_db


def test_record_driver_capture_completion_correction_stacks_across_relocked_sets(
    monkeypatch,
):
    """Run 19's second repeat set (after the household restarted the level
    check) measured a STILL-insufficient worst-band SNR. Each completed-
    insufficient finalization writes its OWN correction on top of the
    prior one (bounded at _MAX_SOLVE_CORRECTION_WRITES), exactly mirroring
    the rejection-path stacking behavior."""

    import jasper.web.correction_crossover_backend as backend_mod
    from jasper.audio_measurement import level_solver
    from jasper.audio_measurement.quality_model import DRIVER as DRIVER_QUALITY_MODEL

    lease = backend_mod.CrossoverLevelLease()
    monkeypatch.setattr(backend_mod, "_LEVEL_LEASE", lease)
    _, profile, targets = _safety_profile_and_targets()
    _configure_lease(lease, targets)
    required_db = level_solver.driver_solve_requirement_db(DRIVER_QUALITY_MODEL)

    monkeypatch.setattr(
        backend_mod.web_measurement,
        "record_driver_capture",
        lambda *args, **kwargs: _run19_finalized_payload(13.7),
    )
    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )
    first_total = lease._solve_adjustment_db["mono:woofer"]
    assert first_total == pytest.approx(required_db - 13.7)

    # Second set, after a re-lock -- measured worse (7.8 dB, per run 19's
    # second set). The correction STACKS rather than replacing the first.
    monkeypatch.setattr(
        backend_mod.web_measurement,
        "record_driver_capture",
        lambda *args, **kwargs: _run19_finalized_payload(7.8),
    )
    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )
    assert lease._solve_adjustment_db["mono:woofer"] == pytest.approx(
        first_total + (required_db - 7.8)
    )
    assert lease._solve_correction_writes == {"mono:woofer": 2}

    # A third completed-insufficient finalization is past the bound
    # (_MAX_SOLVE_CORRECTION_WRITES == 2): it does NOT write a third
    # correction, and the NEXT solve attempt for this target refuses
    # pre-flight (REFUSAL REACHABILITY) rather than replaying a doomed
    # sweep a third time -- without touching the driver's level lock.
    monkeypatch.setattr(
        backend_mod.web_measurement,
        "record_driver_capture",
        lambda *args, **kwargs: _run19_finalized_payload(13.7),
    )
    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )
    stacked_total = lease._solve_adjustment_db["mono:woofer"]
    assert stacked_total == pytest.approx(
        first_total + (required_db - 7.8)
    )  # unchanged -- the third write was exhausted, not applied
    assert lease._solve_correction_writes == {"mono:woofer": 3}

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-1.0, noise_floor_dbfs=-50.0
    )
    result = lease._solve_driver_level(
        "mono", "woofer", capture_geometry="near_field"
    )
    assert isinstance(result, level_solver.LevelSolveRefusal)
    assert result.code == level_solver.REFUSAL_MEASUREMENT_WINDOW_UNREACHABLE
    # The lock itself is untouched by the refusal.
    assert (
        lease._outcomes["near_field_driver:mono:woofer"].ramp.locked_main_volume_db
        == -20.0
    )


def test_solve_correction_cleared_by_relay_device_fingerprint_change(monkeypatch):
    """W2.3 item 2: a signed adjustment models a SPECIFIC microphone's
    physics at a specific position. Swapping the relay mic mid-comparison-
    set is the one non-explicit trigger that must still clear a target's
    bounded-correction state -- a different mic is different physics, so a
    stale correction from the OLD mic must not silently carry over."""

    topology, profile, targets = _safety_profile_and_targets()
    _patch_solve_environment(monkeypatch, topology, profile)

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-1.0, noise_floor_dbfs=-50.0
    )

    lease.input_device = {"actual_device_id_hash": "mic-a"}
    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=5.0
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(5.0)}

    # Same mic reconnecting (e.g. after invalidate cleared self.input_device
    # to None and the phone re-sent its identity) must NOT clear.
    lease.input_device = {"actual_device_id_hash": "mic-a"}
    lease._solve_driver_level("mono", "woofer", capture_geometry="near_field")
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(5.0)}

    # A DIFFERENT mic clears it -- checked on the next read...
    lease.input_device = {"actual_device_id_hash": "mic-b"}
    lease._solve_driver_level("mono", "woofer", capture_geometry="near_field")
    assert lease._solve_adjustment_db == {}
    assert lease._solve_correction_writes == {}

    # ...and equally on the next WRITE, if a write happens before any read.
    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=3.0
    )
    lease.input_device = {"actual_device_id_hash": "mic-c"}
    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=1.0
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(1.0)}
    assert lease._solve_correction_writes == {"mono:woofer": 1}


def test_record_driver_capture_set_completion_clears_correction_state(monkeypatch):
    """W2.2: once a target's repeat set reaches a terminal state (success or
    terminal refusal), its bounded-correction state clears -- a LATER,
    unrelated measurement of the same target starts with a clean budget."""

    import jasper.web.correction_crossover_backend as backend_mod

    lease = backend_mod.CrossoverLevelLease()
    monkeypatch.setattr(backend_mod, "_LEVEL_LEASE", lease)
    _, profile, targets = _safety_profile_and_targets()
    _configure_lease(lease, targets)
    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=4.7
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(4.7)}

    monkeypatch.setattr(
        backend_mod.web_measurement,
        "record_driver_capture",
        lambda *args, **kwargs: {
            "recorded": True,
            "acoustic": {"peak_dbfs": -20.0, "mic_clipping": False},
            "excitation": {"effective_peak_dbfs": -25.0},
        },
    )
    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )

    assert lease._solve_adjustment_db == {}
    assert lease._solve_correction_writes == {}
    # The accepted capture's own peak/effective_peak_dbfs still refined the
    # measured gain BEFORE set-completion cleared it (matches item 2's
    # "any capture" scope) -- clearing happens last, so the end state has
    # no stale gain.
    assert lease._solve_measured_gain_db == {}


def test_record_driver_capture_terminal_refusal_persists_correction_state(
    monkeypatch,
):
    """W2.3 (replaces the pre-W2.3 "terminal refusal also clears" rule): the
    insufficient-accepted-repeats terminal refusal is NOT a successful,
    sufficient finalization, so it must NOT clear a target's correction
    state either -- only clear_solve_correction's own three points (a
    sufficient recorded finalization, a device-fingerprint change, or an
    explicit flow reset) do. Most of that refusal's rejected attempts
    already ran through the rejection path above, so the correction they
    built up models a physical problem (loud room, bad placement) that is
    very likely still there on the next attempt for the SAME target."""

    import jasper.web.correction_crossover_backend as backend_mod

    lease = backend_mod.CrossoverLevelLease()
    monkeypatch.setattr(backend_mod, "_LEVEL_LEASE", lease)
    _, profile, targets = _safety_profile_and_targets()
    _configure_lease(lease, targets)
    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=4.7
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(4.7)}

    monkeypatch.setattr(
        backend_mod.web_measurement,
        "record_driver_capture",
        lambda *args, **kwargs: {
            "recorded": False,
            "status": "refused",
            "verdict": "insufficient_repeats",
        },
    )
    backend_mod.record_driver_capture(
        {"speaker_group_id": "mono", "role": "woofer"}, b"wav"
    )

    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(4.7)}
    assert lease._solve_correction_writes == {"mono:woofer": 1}
