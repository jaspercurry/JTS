# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging

import pytest

from jasper.audio_measurement.null_walk import (
    MAX_COARSE_CANDIDATES,
    MAX_SCHEDULED_CANDIDATES,
    BoundedNullWalkSchedule,
    DspPredecessor,
    DspRestoreConfirmation,
    NullWalkError,
    NullWalkSpec,
    geometry_seed_us,
    run_null_walk as _run_null_walk,
    select_delay,
    select_scheduled_delay,
    summarize_candidate,
)


def _capture(depth: float, **acoustic_overrides):
    acoustic = {
        "null_depth_db": depth,
        "null_depth_capped": False,
        "mic_clipping": False,
        "calibrated": True,
        "expect_null": True,
        "crossover_fc_hz": 5000.0,
        "gating": {"applied": True},
        "above_validity_floor": True,
        "snr": {"decision_class": "alignment", "verdict": "ok"},
        # Deliberately present and absurd: arrival timing is never an input to
        # the selected delay.
        "ir_arrival_us": 999_999.0,
    }
    acoustic.update(acoustic_overrides)
    return {"acoustic": acoustic}


def _spec(*, fc=5000.0, seed=0.0, step=100.0):
    return NullWalkSpec(
        crossover_fc_hz=fc,
        geometry_seed_us=seed,
        positive_delay_target="upper",
        negative_delay_target="lower",
        step_us=step,
    )


def _predecessor(state=None):
    return DspPredecessor(
        state={"config_path": "/configs/entry.yml", "active_raw": state or "raw"},
    )


def _restore_recorder(restored):
    def restore(predecessor):
        restored.append(predecessor)
        return DspRestoreConfirmation(predecessor.state)

    return restore


async def _run_active_walk(spec, **kwargs):
    return await _run_null_walk(spec, scope="active_crossover", **kwargs)


def test_predecessor_fingerprint_is_derived_from_canonical_frozen_state():
    first = DspPredecessor(state={"path": "/entry.yml", "raw": {"b": 2, "a": 1}})
    second = DspPredecessor(state={"raw": {"a": 1, "b": 2}, "path": "/entry.yml"})

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_predecessor_state_access_cannot_mutate_the_frozen_rollback_anchor():
    predecessor = DspPredecessor(
        state={"path": "/entry.yml", "raw": {"filters": ["entry"]}}
    )
    state_copy = predecessor.state

    state_copy["raw"]["filters"].append("candidate")

    assert predecessor.state == {
        "path": "/entry.yml",
        "raw": {"filters": ["entry"]},
    }


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"raw": float("nan")},
        {"raw": object()},
        {1: "ambiguous-key"},
        {"raw": ("tuple-is-not-json",)},
    ],
)
def test_predecessor_requires_nonempty_canonical_json_state(state):
    with pytest.raises(NullWalkError, match="predecessor state"):
        DspPredecessor(state=state)


def test_geometry_bound_is_half_one_crossover_period_and_grid_contains_seed():
    spec = _spec(fc=1600.0, seed=250.0)

    assert spec.half_period_us == pytest.approx(312.5)
    assert spec.lower_bound_us == pytest.approx(-62.5)
    assert spec.upper_bound_us == pytest.approx(562.5)
    assert spec.candidate_delays_us() == (
        -50.0,
        50.0,
        150.0,
        250.0,
        350.0,
        450.0,
        550.0,
    )


def test_geometry_seed_is_only_the_path_plus_known_transport_bound_center():
    assert geometry_seed_us(0.343) == pytest.approx(1000.0)
    assert geometry_seed_us(
        -0.1715,
        signed_transport_difference_us=2500.0,
    ) == pytest.approx(2000.0)


def test_signed_grid_coordinates_map_to_non_negative_targeted_dsp_delays():
    spec = _spec()

    negative = spec.dsp_candidate(-100.0)
    zero = spec.dsp_candidate(0.0)
    positive = spec.dsp_candidate(100.0)

    assert (negative.delay_target, negative.delay_us) == ("lower", 100.0)
    assert (zero.delay_target, zero.delay_us) == (None, 0.0)
    assert (positive.delay_target, positive.delay_us) == ("upper", 100.0)
    assert negative.positive_delay_target == "upper"
    assert negative.negative_delay_target == "lower"


@pytest.mark.parametrize("step", [49.9, 100.1])
def test_step_must_stay_inside_the_pinned_50_to_100_microsecond_range(step):
    with pytest.raises(NullWalkError, match="step_us"):
        _spec(fc=2000.0, step=step)


def test_candidate_requires_five_good_gated_alignment_snr_captures():
    four = [_capture(20.0), _capture(20.2), _capture(19.9), _capture(20.1)]
    out = summarize_candidate(_spec(), 0.0, four)
    assert out["repeatable"] is False
    assert {issue["code"] for issue in out["issues"]} == {"captures_missing"}

    bad_snr = four + [
        _capture(20.0, snr={"decision_class": "alignment", "verdict": "reduced"})
    ]
    out = summarize_candidate(_spec(), 0.0, bad_snr)
    assert out["repeatable"] is False
    assert "alignment_snr_insufficient" in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"gating": {"applied": False}}, "gated_null_required"),
        (
            {"above_validity_floor": False},
            "below_validity_floor",
        ),
        ({"mic_clipping": True}, "clipping"),
        ({"null_depth_capped": True}, "null_depth_capped"),
    ],
)
def test_candidate_rejects_each_capture_quality_failure(override, code):
    captures = [_capture(20.0) for _ in range(5)]
    captures[-1] = _capture(20.0, **override)
    out = summarize_candidate(_spec(), 0.0, captures)
    assert out["repeatable"] is False
    assert code in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize("floor_value", [None, False])
def test_candidate_requires_canonical_top_level_validity_floor_true(floor_value):
    captures = [_capture(20.0) for _ in range(5)]
    captures[-1]["acoustic"]["above_validity_floor"] = floor_value

    out = summarize_candidate(_spec(), 0.0, captures)

    assert out["repeatable"] is False
    assert "below_validity_floor" in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"calibrated": False}, "calibrated_mic_required"),
        ({"expect_null": False}, "reverse_null_required"),
        ({"crossover_fc_hz": 999.0}, "crossover_region_mismatch"),
        ({"crossover_fc_hz": None}, "crossover_region_mismatch"),
    ],
)
def test_candidate_requires_calibrated_reverse_null_for_the_spec_region(
    override,
    code,
):
    captures = [_capture(20.0) for _ in range(5)]
    captures[-1] = _capture(20.0, **override)

    out = summarize_candidate(_spec(), 0.0, captures)

    assert out["repeatable"] is False
    assert code in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("calibrated", "calibrated_mic_required"),
        ("expect_null", "reverse_null_required"),
        ("crossover_fc_hz", "crossover_region_mismatch"),
    ],
)
def test_candidate_refuses_missing_reverse_null_identity(field, code):
    captures = [_capture(20.0) for _ in range(5)]
    captures[-1]["acoustic"].pop(field)

    out = summarize_candidate(_spec(), 0.0, captures)

    assert out["repeatable"] is False
    assert code in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize("last_depth", [22.0, 22.1])
def test_candidate_refuses_two_db_or_greater_null_depth_spread(last_depth):
    out = summarize_candidate(
        _spec(),
        100.0,
        [
            _capture(20.0),
            _capture(20.2),
            _capture(20.0),
            _capture(20.1),
            _capture(last_depth),
        ],
    )
    assert out["spread_db"] >= 2.0
    assert out["repeatable"] is False
    assert "repeatability_low" in {issue["code"] for issue in out["issues"]}


def test_selection_uses_only_dsp_candidate_and_deepest_repeatable_null():
    spec = _spec()
    evidence = {
        -100.0: [_capture(v) for v in (15.0, 15.2, 14.9, 15.1, 15.0)],
        0.0: [_capture(v) for v in (19.8, 20.0, 20.1, 20.0, 19.9)],
        100.0: [_capture(v) for v in (17.0, 17.1, 16.9, 17.0, 17.2)],
    }

    out = select_delay(spec, evidence)

    assert out["status"] == "selected"
    assert out["selected_relative_delay_us"] == 0.0
    assert out["selected_delay_us"] == 0.0
    assert out["selected_delay_target"] is None
    assert out["selected_null_depth_db"] == pytest.approx(20.0)
    assert out["selected_delay_us"] != 999_999.0
    candidates = {row["relative_delay_us"]: row for row in out["candidates"]}
    assert candidates[-100.0]["delay_target"] == "lower"
    assert candidates[-100.0]["delay_us"] == 100.0
    assert candidates[100.0]["delay_target"] == "upper"
    assert candidates[100.0]["delay_us"] == 100.0


def test_selection_refuses_when_every_candidate_fails_repeatability():
    spec = _spec()
    evidence = {
        -100.0: [_capture(10.0)] * 5,
        0.0: [_capture(v) for v in (10.0, 12.0, 8.0, 11.0, 9.0)],
        100.0: [_capture(10.0)] * 5,
    }

    out = select_delay(spec, evidence)

    assert out["status"] == "refused"
    assert out["reason"] == "candidate_repeatability_failed"
    assert out["selected_delay_us"] is None


def test_selection_refuses_a_hole_in_the_declared_exhaustive_grid():
    spec = _spec()
    evidence = {0.0: [_capture(20.0)] * 5}

    out = select_delay(spec, evidence)

    assert out["status"] == "refused"
    assert out["reason"] == "candidate_evidence_incomplete"
    assert out["selected_delay_target"] is None


def test_selection_refuses_complete_grid_of_wrong_capture_kind():
    spec = _spec()
    wrong = _capture(
        20.0,
        calibrated=False,
        expect_null=False,
        crossover_fc_hz=999.0,
    )
    evidence = {candidate: [wrong] * 5 for candidate in spec.candidate_delays_us()}

    out = select_delay(spec, evidence)

    assert out["status"] == "refused"
    assert out["reason"] == "candidate_evidence_incomplete"
    assert out["selected_relative_delay_us"] is None


def test_selection_prefers_geometry_seed_inside_an_unresolvable_null_plateau():
    spec = _spec()
    evidence = {
        -100.0: [_capture(v) for v in (20.0, 20.2, 20.1, 20.0, 20.1)],
        0.0: [_capture(v) for v in (19.9, 20.1, 20.0, 20.0, 20.1)],
        100.0: [_capture(v) for v in (20.0, 20.2, 20.1, 20.0, 20.1)],
    }

    out = select_delay(spec, evidence)

    assert out["selected_delay_us"] == 0.0
    assert out["indistinguishable_delays_us"] == [-100.0, 0.0, 100.0]


def test_plateau_comparison_uses_both_candidates_measurement_spread():
    spec = _spec()
    evidence = {
        -100.0: [_capture(v) for v in (19.6, 19.8, 20.5, 21.0, 21.4)],
        0.0: [_capture(v) for v in (19.95, 20.0, 20.0, 20.0, 20.05)],
        100.0: [_capture(v) for v in (18.0, 18.1, 18.0, 18.1, 18.0)],
    }

    out = select_delay(spec, evidence)

    assert out["best_measured_null_depth_db"] == pytest.approx(20.5)
    assert out["selected_relative_delay_us"] == 0.0
    assert out["indistinguishable_delays_us"] == [-100.0, 0.0]


def test_evidence_outside_candidate_grid_is_refused():
    spec = _spec()
    with pytest.raises(NullWalkError, match="bounded candidate grid"):
        select_delay(spec, {25.0: [_capture(20.0)] * 5})


@pytest.mark.asyncio
async def test_runner_applies_each_exact_candidate_and_always_restores():
    spec = _spec()
    applied = []
    restored = []
    predecessor = _predecessor()

    async def apply_candidate(candidate):
        applied.append(candidate.relative_delay_us)

    async def capture(candidate, index):
        return _capture(20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01)

    out = await _run_active_walk(
        spec,
        apply_candidate=apply_candidate,
        capture_null=capture,
        snapshot_predecessor=lambda: predecessor,
        restore_predecessor=_restore_recorder(restored),
    )

    assert applied == list(spec.candidate_delays_us())
    assert restored == [predecessor]
    assert out["selected_delay_us"] == 0.0


@pytest.mark.asyncio
async def test_runner_freezes_snapshot_before_first_candidate_mutation():
    entry_state = {
        "config_path": "/configs/entry.yml",
        "active_raw": {"filters": ["entry"]},
    }
    external = DspPredecessor(entry_state)
    restored = []

    def apply_candidate(_candidate):
        # Simulate a host retaining and changing its own nested snapshot object
        # while it installs candidate state. The transaction-owned predecessor
        # must remain the value captured at entry.
        entry_state["active_raw"]["filters"].append("candidate")

    await _run_active_walk(
        _spec(),
        apply_candidate=apply_candidate,
        capture_null=lambda candidate, index: _capture(
            20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
        ),
        snapshot_predecessor=lambda: external,
        restore_predecessor=_restore_recorder(restored),
    )

    assert restored[0].fingerprint == external.fingerprint
    assert restored[0].state == {
        "config_path": "/configs/entry.yml",
        "active_raw": {"filters": ["entry"]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["active_crossover", "bass_management"])
async def test_runner_emits_scoped_bounded_lifecycle_events_without_snapshot_payload(
    caplog,
    scope,
):
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.null_walk")

    await _run_null_walk(
        _spec(),
        scope=scope,
        apply_candidate=lambda _candidate: True,
        capture_null=lambda candidate, index: _capture(
            20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
        ),
        snapshot_predecessor=lambda: _predecessor(state="secret-active-raw"),
        restore_predecessor=lambda predecessor: DspRestoreConfirmation(
            predecessor.state
        ),
    )

    messages = [record.getMessage() for record in caplog.records]
    assert sum("event=correction.delay_walk_started" in m for m in messages) == 1
    assert sum("event=correction.delay_walk_restored" in m for m in messages) == 1
    assert sum("event=correction.delay_walk_completed" in m for m in messages) == 1
    assert all(f"scope={scope}" in message for message in messages)
    assert all("secret-active-raw" not in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["room", None, ["active_crossover"]])
async def test_runner_refuses_unknown_scope_before_dsp_mutation(scope):
    applied = []

    with pytest.raises(NullWalkError, match="scope must be one of"):
        await _run_null_walk(
            _spec(),
            scope=scope,  # type: ignore[arg-type]
            apply_candidate=lambda candidate: applied.append(candidate),
            capture_null=lambda _candidate, _index: _capture(20.0),
            snapshot_predecessor=_predecessor,
            restore_predecessor=_restore_recorder([]),
        )

    assert applied == []


@pytest.mark.asyncio
async def test_runner_restores_when_capture_fails():
    spec = _spec()
    restored = []
    predecessor = _predecessor()

    async def fail(_delay, _index):
        raise RuntimeError("relay lost")

    with pytest.raises(RuntimeError, match="relay lost"):
        await _run_active_walk(
            spec,
            apply_candidate=lambda _delay: None,
            capture_null=fail,
            snapshot_predecessor=lambda: predecessor,
            restore_predecessor=_restore_recorder(restored),
        )

    assert restored == [predecessor]


@pytest.mark.asyncio
async def test_runner_rejects_apply_callback_explicit_failure_and_restores():
    restored = []
    predecessor = _predecessor()
    with pytest.raises(NullWalkError, match="apply_candidate reported failure"):
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: False,
            capture_null=lambda _candidate, _index: _capture(20.0),
            snapshot_predecessor=lambda: predecessor,
            restore_predecessor=_restore_recorder(restored),
        )
    assert restored == [predecessor]


@pytest.mark.asyncio
async def test_runner_treats_apply_callback_self_cancellation_as_failure():
    restored = []

    async def cancel_apply(_candidate):
        raise asyncio.CancelledError

    with pytest.raises(NullWalkError, match="candidate DSP apply cancelled itself"):
        await _run_active_walk(
            _spec(),
            apply_candidate=cancel_apply,
            capture_null=lambda _candidate, _index: _capture(20.0),
            snapshot_predecessor=_predecessor,
            restore_predecessor=_restore_recorder(restored),
        )

    assert len(restored) == 1


@pytest.mark.asyncio
async def test_runner_rejects_restore_callback_explicit_failure(caplog):
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.null_walk")

    with pytest.raises(NullWalkError, match="must return DspRestoreConfirmation"):
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: None,
            capture_null=lambda candidate, index: _capture(
                20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
            ),
            snapshot_predecessor=_predecessor,
            restore_predecessor=lambda _predecessor: False,
        )
    assert any(
        "event=correction.delay_walk_restore_failed" in message
        and "failure_code=invalid_confirmation" in message
        for message in (record.getMessage() for record in caplog.records)
    )


@pytest.mark.asyncio
async def test_runner_rejects_restore_confirmation_for_different_graph(caplog):
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.null_walk")

    with pytest.raises(NullWalkError, match="confirmed the wrong DSP predecessor"):
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: None,
            capture_null=lambda candidate, index: _capture(
                20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
            ),
            snapshot_predecessor=_predecessor,
            restore_predecessor=lambda _predecessor: DspRestoreConfirmation(
                {"config_path": "/configs/not-entry.yml", "active_raw": "raw"}
            ),
        )
    assert any(
        "event=correction.delay_walk_restore_failed" in message
        and "failure_code=readback_mismatch" in message
        for message in (record.getMessage() for record in caplog.records)
    )


@pytest.mark.asyncio
async def test_runner_treats_restore_callback_self_cancellation_as_failure(caplog):
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.null_walk")

    async def cancel_restore(_predecessor):
        raise asyncio.CancelledError

    with pytest.raises(NullWalkError, match="predecessor restore cancelled itself"):
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=lambda candidate, index: _capture(
                20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
            ),
            snapshot_predecessor=_predecessor,
            restore_predecessor=cancel_restore,
        )
    assert any(
        "event=correction.delay_walk_restore_failed" in message
        and "failure_code=self_cancelled" in message
        for message in (record.getMessage() for record in caplog.records)
    )


@pytest.mark.asyncio
async def test_runner_reports_capture_and_restore_failures_together(caplog):
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.null_walk")

    def fail_capture(_candidate, _index):
        raise RuntimeError("relay lost")

    def fail_restore(_predecessor):
        raise RuntimeError("restore lost")

    with pytest.raises(BaseExceptionGroup) as caught:
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: None,
            capture_null=fail_capture,
            snapshot_predecessor=_predecessor,
            restore_predecessor=fail_restore,
        )
    assert [str(exc) for exc in caught.value.exceptions] == [
        "relay lost",
        "restore lost",
    ]
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "event=correction.delay_walk_restore_failed" in message
        and "failure_code=other" in message
        for message in messages
    )
    assert any("event=correction.delay_walk_failed" in message for message in messages)
    assert all("relay lost" not in message for message in messages)
    assert all("restore lost" not in message for message in messages)


@pytest.mark.asyncio
async def test_runner_cancellation_during_capture_restores_before_propagating(caplog):
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.null_walk")
    capture_started = asyncio.Event()
    restored = []
    predecessor = _predecessor()

    async def capture(_candidate, _index):
        capture_started.set()
        await asyncio.Future()

    task = asyncio.create_task(
        _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=capture,
            snapshot_predecessor=lambda: predecessor,
            restore_predecessor=_restore_recorder(restored),
        )
    )
    await capture_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert restored == [predecessor]
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "event=correction.delay_walk_restored" in message
        and "trigger=cancelled" in message
        for message in messages
    )
    assert any(
        "event=correction.delay_walk_cancelled" in message for message in messages
    )
    assert all(
        "failure_code=" not in message
        for message in messages
        if "event=correction.delay_walk_cancelled" in message
    )


@pytest.mark.asyncio
async def test_runner_cancellation_settles_candidate_apply_before_restore():
    apply_started = asyncio.Event()
    release_apply = asyncio.Event()
    order = []
    predecessor = _predecessor()

    async def apply_candidate(_candidate):
        apply_started.set()
        await release_apply.wait()
        order.append("apply_finished")
        return True

    def restore(entry):
        order.append("restored")
        return DspRestoreConfirmation(entry.state)

    task = asyncio.create_task(
        _run_active_walk(
            _spec(),
            apply_candidate=apply_candidate,
            capture_null=lambda _candidate, _index: _capture(20.0),
            snapshot_predecessor=lambda: predecessor,
            restore_predecessor=restore,
        )
    )
    await apply_started.wait()
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert order == []
    release_apply.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert order == ["apply_finished", "restored"]


@pytest.mark.asyncio
async def test_runner_preserves_apply_failure_that_finishes_after_cancellation():
    apply_started = asyncio.Event()
    release_apply = asyncio.Event()
    restored = []

    async def apply_candidate(_candidate):
        apply_started.set()
        await release_apply.wait()
        raise RuntimeError("candidate load failed")

    task = asyncio.create_task(
        _run_active_walk(
            _spec(),
            apply_candidate=apply_candidate,
            capture_null=lambda _candidate, _index: _capture(20.0),
            snapshot_predecessor=_predecessor,
            restore_predecessor=_restore_recorder(restored),
        )
    )
    await apply_started.wait()
    task.cancel()
    release_apply.set()

    with pytest.raises(BaseExceptionGroup) as caught:
        await task

    assert any(
        isinstance(error, asyncio.CancelledError) for error in caught.value.exceptions
    )
    assert any(
        isinstance(error, RuntimeError) and str(error) == "candidate load failed"
        for error in caught.value.exceptions
    )
    assert len(restored) == 1


@pytest.mark.asyncio
async def test_runner_restores_for_unexpected_base_exception():
    class FatalWalkAbort(BaseException):
        pass

    restored = []

    def abort(_candidate, _index):
        raise FatalWalkAbort("host shutdown")

    with pytest.raises(FatalWalkAbort, match="host shutdown"):
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=abort,
            snapshot_predecessor=_predecessor,
            restore_predecessor=_restore_recorder(restored),
        )

    assert len(restored) == 1


@pytest.mark.asyncio
async def test_runner_repeated_cancellation_cannot_interrupt_restore():
    capture_started = asyncio.Event()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    restored = []
    predecessor = _predecessor()

    async def capture(_candidate, _index):
        capture_started.set()
        await asyncio.Future()

    async def restore(entry):
        restore_started.set()
        await release_restore.wait()
        restored.append(entry)
        return DspRestoreConfirmation(entry.state)

    task = asyncio.create_task(
        _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=capture,
            snapshot_predecessor=lambda: predecessor,
            restore_predecessor=restore,
        )
    )
    await capture_started.wait()
    task.cancel()
    await restore_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert restored == []
    release_restore.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert restored == [predecessor]


@pytest.mark.asyncio
async def test_runner_cancellation_during_success_cleanup_waits_for_restore():
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    restored = []
    predecessor = _predecessor()

    async def restore(entry):
        restore_started.set()
        await release_restore.wait()
        restored.append(entry)
        return DspRestoreConfirmation(entry.state)

    task = asyncio.create_task(
        _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=lambda candidate, index: _capture(
                20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
            ),
            snapshot_predecessor=lambda: predecessor,
            restore_predecessor=restore,
        )
    )
    await restore_started.wait()
    task.cancel()
    release_restore.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert restored == [predecessor]


@pytest.mark.asyncio
async def test_runner_preserves_cleanup_cancellation_when_restore_fails_after_success(
    caplog,
):
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.null_walk")
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()

    async def fail_restore(_entry):
        restore_started.set()
        await release_restore.wait()
        raise RuntimeError("restore")

    task = asyncio.create_task(
        _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=lambda candidate, index: _capture(
                20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
            ),
            snapshot_predecessor=_predecessor,
            restore_predecessor=fail_restore,
        )
    )
    await restore_started.wait()
    task.cancel("cleanup")
    await asyncio.sleep(0)
    release_restore.set()

    with pytest.raises(BaseExceptionGroup) as caught:
        await task

    assert type(caught.value) is BaseExceptionGroup
    assert [(type(error), str(error)) for error in caught.value.exceptions] == [
        (asyncio.CancelledError, "cleanup"),
        (RuntimeError, "restore"),
    ]
    assert any(
        "event=correction.delay_walk_restore_failed" in message
        and "trigger=cancelled" in message
        for message in (record.getMessage() for record in caplog.records)
    )


@pytest.mark.asyncio
async def test_runner_preserves_body_failure_cleanup_cancellation_and_restore_failure():
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()

    def fail_capture(_candidate, _index):
        raise RuntimeError("body")

    async def fail_restore(_entry):
        restore_started.set()
        await release_restore.wait()
        raise RuntimeError("restore")

    task = asyncio.create_task(
        _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=fail_capture,
            snapshot_predecessor=_predecessor,
            restore_predecessor=fail_restore,
        )
    )
    await restore_started.wait()
    task.cancel("cleanup")
    await asyncio.sleep(0)
    release_restore.set()

    with pytest.raises(BaseExceptionGroup) as caught:
        await task

    assert type(caught.value) is BaseExceptionGroup
    assert [(type(error), str(error)) for error in caught.value.exceptions] == [
        (RuntimeError, "body"),
        (asyncio.CancelledError, "cleanup"),
        (RuntimeError, "restore"),
    ]


@pytest.mark.asyncio
async def test_runner_preserves_repeated_cancellation_and_restore_failure():
    capture_started = asyncio.Event()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()

    async def capture(_candidate, _index):
        capture_started.set()
        await asyncio.Future()

    async def fail_restore(_entry):
        restore_started.set()
        await release_restore.wait()
        raise RuntimeError("restore")

    task = asyncio.create_task(
        _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=capture,
            snapshot_predecessor=_predecessor,
            restore_predecessor=fail_restore,
        )
    )
    await capture_started.wait()
    task.cancel("original")
    await restore_started.wait()
    task.cancel("cleanup")
    await asyncio.sleep(0)
    release_restore.set()

    with pytest.raises(BaseExceptionGroup) as caught:
        await task

    assert type(caught.value) is BaseExceptionGroup
    assert [(type(error), str(error)) for error in caught.value.exceptions] == [
        (asyncio.CancelledError, "original"),
        (asyncio.CancelledError, "cleanup"),
        (RuntimeError, "restore"),
    ]


@pytest.mark.asyncio
async def test_runner_restore_timeout_fails_loudly_and_is_bounded(caplog):
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.null_walk")

    async def never_restore(_predecessor):
        await asyncio.Future()

    with pytest.raises(NullWalkError, match="predecessor restore timed out"):
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda _candidate: True,
            capture_null=lambda candidate, index: _capture(
                20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
            ),
            snapshot_predecessor=_predecessor,
            restore_predecessor=never_restore,
            restore_timeout_s=1.0,
        )
    assert any(
        "event=correction.delay_walk_restore_failed" in message
        and "failure_code=timeout" in message
        for message in (record.getMessage() for record in caplog.records)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_s", [0.9, 30.1, float("nan")])
async def test_runner_refuses_invalid_restore_timeout_before_dsp(timeout_s):
    applied = []

    with pytest.raises(NullWalkError, match="restore_timeout_s"):
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda candidate: applied.append(candidate),
            capture_null=lambda _candidate, _index: _capture(20.0),
            snapshot_predecessor=_predecessor,
            restore_predecessor=_restore_recorder([]),
            restore_timeout_s=timeout_s,
        )

    assert applied == []


@pytest.mark.asyncio
async def test_runner_refuses_invalid_snapshot_before_dsp_mutation():
    applied = []
    restored = []

    with pytest.raises(NullWalkError, match="must return DspPredecessor"):
        await _run_active_walk(
            _spec(),
            apply_candidate=lambda candidate: applied.append(candidate),
            capture_null=lambda _candidate, _index: _capture(20.0),
            snapshot_predecessor=lambda: {"config": "not-authoritative"},
            restore_predecessor=_restore_recorder(restored),
        )

    assert applied == []
    assert restored == []


@pytest.mark.asyncio
async def test_runner_refuses_unbounded_low_frequency_exhaustive_walk_before_dsp():
    spec = _spec(fc=80.0)
    applied = []
    with pytest.raises(NullWalkError, match="candidate budget"):
        await _run_active_walk(
            spec,
            apply_candidate=lambda candidate: applied.append(candidate),
            capture_null=lambda _delay, _index: _capture(20.0),
            snapshot_predecessor=_predecessor,
            restore_predecessor=lambda predecessor: DspRestoreConfirmation(
                predecessor.state
            ),
        )
    assert applied == []


def test_candidate_budget_is_preflighted_arithmetically_at_exact_boundary():
    exact_25 = _spec(fc=400.0)
    refused_27 = _spec(fc=370.0)

    assert exact_25.candidate_count == 25
    assert len(exact_25.candidate_delays_us()) == 25
    assert refused_27.candidate_count == 27
    with pytest.raises(NullWalkError, match="candidate budget"):
        refused_27.candidate_delays_us()


def test_walk_refuses_before_dsp_when_any_candidate_exceeds_delay_ceiling():
    spec = _spec(fc=5000.0, seed=20_000.0)

    with pytest.raises(NullWalkError, match="20 ms delay ceiling"):
        spec.candidate_delays_us()


def test_divisible_half_period_includes_bounds_and_fragment_does_not():
    divisible = _spec(fc=5000.0)
    fragment = _spec(fc=4000.0)

    assert divisible.candidate_delays_us() == (-100.0, 0.0, 100.0)
    assert fragment.lower_bound_us == -125.0
    assert fragment.upper_bound_us == 125.0
    assert fragment.candidate_delays_us() == (-100.0, 0.0, 100.0)


def test_350_hz_schedule_is_bounded_symmetric_deterministic_and_locally_refined():
    spec = _spec(fc=350.0, seed=37.5)

    assert spec.candidate_count == 29
    with pytest.raises(NullWalkError, match="candidate budget"):
        spec.candidate_delays_us()

    first_coarse = spec.coarse_candidate_delays_us()
    second_coarse = spec.coarse_candidate_delays_us()
    schedule = BoundedNullWalkSchedule(spec, refinement_anchor_us=37.5)

    assert first_coarse == second_coarse
    assert len(first_coarse) <= MAX_COARSE_CANDIDATES
    assert first_coarse[0] == spec.fine_grid_coordinate(spec.fine_grid_index_min)
    assert first_coarse[-1] == spec.fine_grid_coordinate(spec.fine_grid_index_max)
    assert 37.5 in first_coarse
    assert tuple(value - 37.5 for value in first_coarse) == pytest.approx(
        tuple(-(value - 37.5) for value in reversed(first_coarse))
    )
    assert schedule.refinement_delays_us == (-62.5, 137.5)
    assert len(schedule.scheduled_delays_us) <= MAX_SCHEDULED_CANDIDATES
    assert schedule.scheduled_delays_us == tuple(
        sorted({*first_coarse, *schedule.refinement_delays_us})
    )
    assert spec.dsp_candidate(-62.5).relative_delay_us == -62.5


def test_bounded_schedule_contract_holds_across_supported_grid_widths():
    for step_us, maximum_half_width_steps in ((100.0, 200), (50.0, 400)):
        for half_width_steps in range(maximum_half_width_steps + 1):
            crossover_fc_hz = (
                1_000_000.0 / step_us
                if half_width_steps == 0
                else 1_000_000.0 / (2.0 * half_width_steps * step_us)
            )
            spec = _spec(fc=crossover_fc_hz, step=step_us)
            coarse = spec.coarse_candidate_delays_us()

            assert spec.steps_each_side == half_width_steps
            assert coarse == spec.coarse_candidate_delays_us()
            assert len(coarse) <= MAX_COARSE_CANDIDATES
            assert coarse[0] == spec.fine_grid_coordinate(spec.fine_grid_index_min)
            assert coarse[-1] == spec.fine_grid_coordinate(spec.fine_grid_index_max)
            assert 0.0 in coarse
            assert coarse == tuple(-value for value in reversed(coarse))

            for anchor in coarse:
                schedule = BoundedNullWalkSchedule(
                    spec,
                    refinement_anchor_us=anchor,
                )
                anchor_index = spec.fine_grid_index(anchor)
                refinement_indexes = {
                    spec.fine_grid_index(value)
                    for value in schedule.refinement_delays_us
                }

                assert len(schedule.refinement_delays_us) <= 2
                assert len(schedule.scheduled_delays_us) <= MAX_SCHEDULED_CANDIDATES
                assert refinement_indexes <= {anchor_index - 1, anchor_index + 1}
                assert not set(schedule.refinement_delays_us) & set(coarse)
                assert schedule.scheduled_delays_us == tuple(
                    sorted({*coarse, *schedule.refinement_delays_us})
                )


def test_bounded_spec_and_schedule_have_strict_fingerprinted_roundtrips():
    spec = _spec(fc=350.0)
    schedule = BoundedNullWalkSchedule(spec, refinement_anchor_us=0.0)

    assert "candidate_delays_us" not in spec.to_dict()
    assert NullWalkSpec.from_mapping(spec.to_dict()) == spec
    assert (
        BoundedNullWalkSchedule.from_mapping(schedule.to_dict(), spec=spec) == schedule
    )
    assert len(spec.fingerprint) == 64
    assert len(schedule.fingerprint) == 64

    tampered_spec = spec.to_dict()
    tampered_spec["fingerprint"] = "0" * 64
    with pytest.raises(NullWalkError, match="exact canonical grid"):
        NullWalkSpec.from_mapping(tampered_spec)

    tampered_schedule = schedule.to_dict()
    tampered_schedule["scheduled_delays_us"] = list(
        reversed(tampered_schedule["scheduled_delays_us"])
    )
    with pytest.raises(NullWalkError, match="exact canonical schedule"):
        BoundedNullWalkSchedule.from_mapping(tampered_schedule, spec=spec)

    wrong_container = schedule.to_dict()
    wrong_container["coarse_delays_us"] = tuple(wrong_container["coarse_delays_us"])
    with pytest.raises(NullWalkError, match="coordinate fields must be lists"):
        BoundedNullWalkSchedule.from_mapping(wrong_container, spec=spec)


def test_refinement_anchor_must_be_an_explicit_coarse_coordinate():
    spec = _spec(fc=350.0)

    # 100 us is a fine coordinate, but the 350 Hz first phase uses a 200 us
    # coarse stride. The host must persist and name the coarse winner before
    # Shared admits its immediate fine neighbours.
    with pytest.raises(NullWalkError, match="exact coarse schedule coordinate"):
        BoundedNullWalkSchedule(spec, refinement_anchor_us=100.0)
    with pytest.raises(NullWalkError, match="numeric"):
        BoundedNullWalkSchedule(spec, refinement_anchor_us=None)


def test_refinement_schedule_selects_deepest_complete_repeatable_coarse_anchor():
    spec = _spec(fc=350.0)
    evidence = {
        coordinate: [_capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)] * 5
        for coordinate in spec.coarse_candidate_delays_us()
    }
    tied = BoundedNullWalkSchedule.from_coarse_evidence(spec, evidence)
    assert tied.refinement_anchor_us == 0.0

    evidence[400.0] = [_capture(30.0, crossover_fc_hz=spec.crossover_fc_hz)] * 5

    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, evidence)

    assert schedule.refinement_anchor_us == 400.0
    assert schedule.refinement_delays_us == (300.0, 500.0)

    missing = dict(evidence)
    missing.pop(spec.coarse_candidate_delays_us()[0])
    with pytest.raises(NullWalkError, match="exact coarse schedule"):
        BoundedNullWalkSchedule.from_coarse_evidence(spec, missing)

    unrepeatable = dict(evidence)
    unrepeatable[400.0] = [
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz),
        _capture(23.0, crossover_fc_hz=spec.crossover_fc_hz),
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz),
        _capture(23.0, crossover_fc_hz=spec.crossover_fc_hz),
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz),
    ]
    with pytest.raises(NullWalkError, match="complete repeatable evidence"):
        BoundedNullWalkSchedule.from_coarse_evidence(spec, unrepeatable)


@pytest.mark.parametrize("relative_delay_us", [50.0, 1500.0, True])
def test_nonallocating_fine_grid_membership_refuses_offgrid_or_out_of_bounds(
    relative_delay_us,
):
    spec = _spec(fc=350.0)

    with pytest.raises(NullWalkError):
        spec.dsp_candidate(relative_delay_us)


def test_low_frequency_schedule_reaches_aligned_dsp_bounds_and_fails_beyond_them():
    exactly_bounded = _spec(fc=25.0)
    coarse = exactly_bounded.coarse_candidate_delays_us()
    schedule = BoundedNullWalkSchedule(
        exactly_bounded,
        refinement_anchor_us=0.0,
    )

    assert coarse[0] == -20_000.0
    assert coarse[-1] == 20_000.0
    assert len(coarse) <= MAX_COARSE_CANDIDATES
    assert len(schedule.scheduled_delays_us) <= MAX_SCHEDULED_CANDIDATES

    beyond_dsp = _spec(fc=24.0)
    with pytest.raises(NullWalkError, match="20 ms delay ceiling"):
        beyond_dsp.coarse_candidate_delays_us()

    epsilon_over = _spec(fc=500_000.0, seed=20_000.000001)
    with pytest.raises(NullWalkError, match="20 ms delay ceiling"):
        epsilon_over.coarse_candidate_delays_us()


def test_existing_exhaustive_paths_remain_capped_after_nonallocating_membership():
    spec = _spec(fc=350.0)
    complete_evidence = {
        spec.fine_grid_coordinate(index): [_capture(20.0)] * 5
        for index in range(spec.fine_grid_index_min, spec.fine_grid_index_max + 1)
    }

    assert spec.dsp_candidate(0.0).delay_us == 0.0
    with pytest.raises(NullWalkError, match="candidate budget"):
        select_delay(spec, complete_evidence)


def test_scheduled_selection_reuses_plateau_policy_without_relaxing_grid_cap():
    spec = _spec(fc=350.0)
    coarse = {
        coordinate: [
            _capture(10.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in spec.coarse_candidate_delays_us()
    }
    coarse[400.0] = [
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
    ] * 5
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, coarse)
    evidence = dict(coarse)
    evidence[300.0] = [
        _capture(value, crossover_fc_hz=spec.crossover_fc_hz)
        for value in (20.0, 20.2, 20.1, 20.0, 20.1)
    ]
    evidence[500.0] = [
        _capture(value, crossover_fc_hz=spec.crossover_fc_hz)
        for value in (15.0, 15.1, 15.0, 15.1, 15.0)
    ]

    result = select_scheduled_delay(spec, schedule, evidence)

    assert spec.candidate_count == 29
    with pytest.raises(NullWalkError, match="candidate budget"):
        select_delay(spec, evidence)
    assert result["status"] == "selected"
    assert result["selected_relative_delay_us"] == 300.0
    assert result["indistinguishable_delays_us"] == [300.0, 400.0]
    assert result["schedule"] == schedule.to_dict()
    assert [row["relative_delay_us"] for row in result["candidates"]] == list(
        schedule.scheduled_delays_us
    )


def test_scheduled_selection_requires_exact_schedule_coverage():
    spec = _spec(fc=350.0)
    coarse = {
        coordinate: [
            _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in spec.coarse_candidate_delays_us()
    }
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, coarse)
    evidence = {
        coordinate: [
            _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in schedule.scheduled_delays_us
    }

    missing = dict(evidence)
    missing.pop(schedule.scheduled_delays_us[0])
    with pytest.raises(NullWalkError, match="cover the exact schedule"):
        select_scheduled_delay(spec, schedule, missing)

    outside = dict(evidence)
    outside[300.0] = [
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
    ] * 5
    with pytest.raises(NullWalkError, match="outside the exact schedule"):
        select_scheduled_delay(spec, schedule, outside)


def test_scheduled_selection_refuses_incomplete_or_unrepeatable_coordinates():
    spec = _spec(fc=350.0)
    coarse = {
        coordinate: [
            _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in spec.coarse_candidate_delays_us()
    }
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, coarse)
    evidence = {
        coordinate: [
            _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in schedule.scheduled_delays_us
    }

    refinement = schedule.refinement_delays_us[0]
    incomplete = dict(evidence)
    incomplete[refinement] = incomplete[refinement][:-1]
    assert select_scheduled_delay(spec, schedule, incomplete)["reason"] == (
        "candidate_evidence_incomplete"
    )

    unrepeatable = dict(evidence)
    unrepeatable[refinement] = [
        _capture(value, crossover_fc_hz=spec.crossover_fc_hz)
        for value in (20.0, 23.0, 20.0, 23.0, 20.0)
    ]
    assert select_scheduled_delay(spec, schedule, unrepeatable)["reason"] == (
        "candidate_repeatability_failed"
    )


def test_scheduled_selection_rejects_a_schedule_for_another_spec():
    spec = _spec(fc=350.0)
    other = _spec(fc=300.0)
    schedule = BoundedNullWalkSchedule(
        other,
        refinement_anchor_us=0.0,
    )

    with pytest.raises(NullWalkError, match="different null-walk spec"):
        select_scheduled_delay(spec, schedule, {})


def test_scheduled_selection_rederives_refinement_from_coarse_evidence():
    spec = _spec(fc=350.0)
    wrong_schedule = BoundedNullWalkSchedule(
        spec,
        refinement_anchor_us=spec.coarse_candidate_delays_us()[0],
    )
    evidence = {
        coordinate: [
            _capture(
                30.0 if coordinate == 400.0 else 20.0,
                crossover_fc_hz=spec.crossover_fc_hz,
            )
        ]
        * 5
        for coordinate in wrong_schedule.scheduled_delays_us
    }

    with pytest.raises(NullWalkError, match="does not match its coarse evidence"):
        select_scheduled_delay(spec, wrong_schedule, evidence)
