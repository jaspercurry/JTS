# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``CrossoverLevelLease``'s bounded per-target level correction (W2.2/W2.3).

Pins the ONE signed adjustment slot per target: what writes it (an SNR
shortfall, a clip rejection, an accepted-but-insufficient finalization), how
writes stack up to ``_MAX_SOLVE_CORRECTION_WRITES`` and then read as
exhausted, and the three things that clear it -- set completion, a relay mic
fingerprint change, and an explicit flow reset.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.measurement import active_driver_targets
from jasper.audio_measurement.ramp import RampLockKind, RampState
from jasper.web.correction_crossover_backend import CrossoverLevelLease
from tests.active_speaker_fixtures import mono_output_topology


def _safety_profile_and_targets(
    *, woofer_max_effective_peak_dbfs: float = -8.0
):
    topology = mono_output_topology()
    common = {
        "hard_excitation_band_hz": [20, 20_000],
        "measurement_band_hz": [20, 20_000],
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
                # #2603: one declared low limit; the hard floor and the
                # protective high-pass both derive from it.
                "recommended_highpass_hz": 1500,
                # -65.0 is the tweeter class default
                # (``driver_protection.max_auto_level_dbfs`` for
                # HIGH_FREQUENCY_ROLES). Declaring it is no longer REQUIRED --
                # the key is optional since 2026-08-23, and a declared value is
                # honoured verbatim rather than clamped to this figure -- but
                # declaring exactly the class default still reads as the
                # delegation, which is what this fixture wants.
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
        saved_at="2026-07-13T12:00:00Z",
    )
    targets = {t["role"]: t for t in active_driver_targets(topology)}
    return topology, profile, targets


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


def _stored_refusal(role: str) -> dict:
    """A refusal as it sits on ``CrossoverLevelLease._solve_refusal``."""

    return {
        "target_id": f"mono:{role}",
        "role": role,
        "code": "room_too_noisy_for_safe_measurement",
        "failing_band_hz": [60.0, 171.0],
        "required_db": 20.0,
        "available_db": 14.5,
    }


def test_refusal_pending_predicate_across_the_three_stored_states():
    """W2.4 pin on the between-set restart's clear/preserve decision.

    ``CrossoverLevelLease._target_refusal_pending`` reads ``_solve_refusal``
    plus the exhausted write count directly, and decides whether the restart
    clears a REFUSED target's correction state. This pins its verdict across
    the three representative stored states, because a wrong answer either
    preserves a doomed correction behind a refusal the household already saw
    (run 20's dead loop) or clears a correction with no refusal ever shown.

    This used to be a *parity* pin against a second reader,
    ``crossover_envelope._active_level_solve_refusal``, which re-derived the
    same OR from ``level_match_snapshot()``'s projections. That reader had no
    production caller -- nothing rendered the ``solve_refusal`` field it read
    -- so it was deleted, and with it the divergence risk the parity half
    guarded. One reader now owns the decision; the state assertions below are
    what survive and are what actually bind behaviour.
    """

    _topology, _profile, targets = _safety_profile_and_targets()
    target_id = "mono:woofer"

    # State 1 (run 20's shape): genuine pre-flight refusal at writes=1 --
    # one write BELOW the exhausted bound. The predicate must say refusal.
    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease.record_solve_correction(
        "mono", "woofer", trigger="completed_insufficient", shortfall_db=12.3
    )
    lease._solve_refusal = _stored_refusal("woofer")
    assert lease._correction_budget_exhausted(target_id) is False
    assert lease._target_refusal_pending(target_id) is True

    # State 2 (run 19's completion-time exhaustion shape): budget exhausted
    # with NO fresh solve refusal stored -- exhaustion alone is the signal;
    # the
    # predicate must agree via the same write count.
    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    for _ in range(3):
        lease.record_solve_correction(
            "mono", "woofer", trigger="snr_shortfall", shortfall_db=2.0
        )
    assert lease._solve_refusal is None
    assert lease._correction_budget_exhausted(target_id) is True
    assert lease._target_refusal_pending(target_id) is True

    # State 3 (the preserve path): a correction exists but no refusal was
    # ever shown and the budget is not exhausted. The predicate must say
    # no-refusal -- this is what keeps the completed-insufficient
    # "JTS will play the next measurement louder" promise intact.
    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease.record_solve_correction(
        "mono", "woofer", trigger="completed_insufficient", shortfall_db=12.3
    )
    assert lease._solve_refusal is None
    assert lease._target_refusal_pending(target_id) is False


def test_measured_gain_cleared_by_set_completion_and_invalidate():
    """W2.3: W2.2 item 2's stored measured gain shares the adjustment slot's
    TWO remaining clearing points -- set completion (clear_solve_correction)
    and a full invalidate (explicit flow reset) -- so a later, unrelated
    measurement never inherits a stale gain from an earlier attempt. A fresh
    ramp lock for the SAME geometry is deliberately no longer a clearing
    point as of W2.3 (hardware run 19) -- see
    test_fresh_ramp_lock_persists_that_targets_escalation."""

    _topology, _profile, targets = _safety_profile_and_targets()

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
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


def test_between_set_invalidate_preserves_nonexhausted_and_clears_exhausted():
    """W2.3 endpoint amendment: the between-set restart
    (invalidate_comparison_context(preserve_solve_corrections=True) -- the
    endpoint's non-continuing branch) distinguishes the two restarts by
    STORED STATE. A non-exhausted target's correction (the
    completed-insufficient terminal promised "JTS will play the next
    measurement louder") survives; an exhausted target (the placement
    refusal was showing) clears for a fresh evaluation."""

    _topology, _profile, targets = _safety_profile_and_targets()

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


def test_new_level_match_run_clears_solve_state():
    lease = CrossoverLevelLease()
    _, _profile, targets = _safety_profile_and_targets()
    _configure_lease(lease, targets)
    lease._solve_refusal = _stored_refusal("tweeter")
    lease.record_solve_correction(
        "mono", "tweeter", trigger="snr_shortfall", shortfall_db=3.0
    )
    lease.record_measured_gain(
        "mono", "tweeter", measured_mic_peak_dbfs=0.0, effective_peak_dbfs=-19.35,
        clipped=True,
    )
    assert lease.level_match_snapshot()["solve_refusal"] is not None

    lease.invalidate_comparison_context()

    assert lease.level_match_snapshot()["solve_refusal"] is None
    assert lease._solve_adjustment_db == {}
    assert lease._solve_correction_writes == {}
    assert lease._solve_measured_gain_db == {}
    assert lease._solve_measured_peak_dbfs == {}


async def test_fresh_ramp_lock_persists_that_targets_escalation(monkeypatch):
    """W2.3 (hardware run 19, replaces the old S2 "fresh-ramp-clears" rule):
    two full woofer repeat sets at the tester's stationary desk placement
    measured near-identical inputs before and after the household restarted
    the level check -- a fresh ramp re-measures the ROOM's ambient, not the
    mic-placement/leakage physics the correction is compensating for. Lock,
    escalate, re-lock the SAME target: the escalation and its write count
    must SURVIVE the re-lock rather than silently resetting."""

    from jasper.correction import level_match
    from jasper.audio_measurement.ramp import RampState

    _topology, _profile, targets = _safety_profile_and_targets()

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-1.0, noise_floor_dbfs=-50.0
    )
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
    # The re-lock replaced the geometry's level...
    assert lease.driver_sweep_locked_main_volume_db(
        "mono", "woofer", capture_geometry="near_field"
    ) == pytest.approx(-18.0)
    # ...but the escalation is NOT cleared by it, and still counts toward
    # the bounded write budget.
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(5.0)}
    assert lease._solve_correction_writes == {"mono:woofer": 1}


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
    # correction, and the target reads as budget-exhausted so the flow stops
    # replaying a doomed sweep -- without touching the driver's level lock.
    lease._outcomes["near_field_driver:mono:woofer"] = _ramp_outcome(
        locked=-20.0, gain_map_db=1.9, cap_db=-1.0, noise_floor_dbfs=-50.0
    )
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
    assert lease._correction_budget_exhausted("mono:woofer") is True
    assert lease._target_refusal_pending("mono:woofer") is True
    assert lease.driver_sweep_locked_main_volume_db(
        "mono", "woofer", capture_geometry="near_field"
    ) == pytest.approx(-20.0)


def test_solve_correction_cleared_by_relay_device_fingerprint_change():
    """W2.3 item 2: a signed adjustment models a SPECIFIC microphone's
    physics at a specific position. Swapping the relay mic mid-comparison-
    set is the one non-explicit trigger that must still clear a target's
    bounded-correction state -- a different mic is different physics, so a
    stale correction from the OLD mic must not silently carry over."""

    _topology, _profile, targets = _safety_profile_and_targets()

    lease = CrossoverLevelLease()
    _configure_lease(lease, targets)

    lease.input_device = {"actual_device_id_hash": "mic-a"}
    lease.record_solve_correction(
        "mono", "woofer", trigger="snr_shortfall", shortfall_db=5.0
    )
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(5.0)}

    # Same mic reconnecting (e.g. after invalidate cleared self.input_device
    # to None and the phone re-sent its identity) must NOT clear.
    lease.input_device = {"actual_device_id_hash": "mic-a"}
    lease._reconcile_solve_correction_device("mono:woofer")
    assert lease._solve_adjustment_db == {"mono:woofer": pytest.approx(5.0)}

    # A DIFFERENT mic clears it on reconciliation...
    lease.input_device = {"actual_device_id_hash": "mic-b"}
    lease._reconcile_solve_correction_device("mono:woofer")
    assert lease._solve_adjustment_db == {}
    assert lease._solve_correction_writes == {}

    # ...and equally on the next WRITE, which reconciles before it stacks.
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
