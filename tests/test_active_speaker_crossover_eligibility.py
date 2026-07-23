# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy

import pytest

from jasper.active_speaker.capture_geometry import (
    comparison_set_fingerprint,
    driver_repeat_binding,
)
from jasper.active_speaker.crossover_eligibility import (
    RepeatProgress,
    automatic_measurement_eligibility,
    driver_acoustic_usable,
    driver_repeat_completed,
    mapping_sequence,
    render_repeat_progress,
    repeat_progress,
)


def _evidence() -> dict:
    comparison = {
        "schema_version": 2,
        "comparison_set_id": "1" * 32,
        "created_at": "2026-07-12T12:00:00Z",
        "topology_id": "topology-1",
        "profile_context_id": "profile-1",
        "setup_sha256": "2" * 64,
        "device_sha256": "3" * 64,
        "calibration_id": "",
        "driver_level_locks": {
            "mono:woofer": {
                "target_id": "mono:woofer",
                "speaker_group_id": "mono",
                "role": "woofer",
                "tone_frequency_hz": 100.0,
                "tone_peak_dbfs": -20.0,
                "commissioning_gain_db": 0.0,
                "locked_main_volume_db": -18.0,
            }
        },
    }
    comparison["fingerprint"] = comparison_set_fingerprint(comparison)
    target = {
        "speaker_group_id": "mono",
        "role": "woofer",
        "target_fingerprint": "6" * 64,
    }

    def proof(policy_id: str) -> dict:
        return {
            "schema_version": 1,
            "policy_id": policy_id,
            "accepted": True,
            "confirmation_source": "relay_begin_capture",
            "acknowledgement_binding_sha256": "4" * 64,
            "relay_session_id": "relay-woofer",
            "capture_protocol_version": 2,
            "capture_page_build": "20260712.1",
            "speaker_group_id": "mono",
            "role": "woofer",
            "target_fingerprint": "6" * 64,
            "comparison_set_id": comparison["comparison_set_id"],
            "comparison_set_fingerprint": comparison["fingerprint"],
        }

    def record(geometry: str) -> dict:
        fixed = geometry == "reference_axis"
        return {
            "speaker_group_id": "mono",
            "role": "woofer",
            "target_fingerprint": "6" * 64,
            "captured": True,
            "mic_clipping": False,
            "repeats": {
                "target": 3,
                "accepted": 3,
                "admission_attempts": 3,
            },
            "acoustic": {
                "capture_geometry": geometry,
                "verdict": "present",
                "mic_clipping": False,
                "gating": {
                    "applied": fixed,
                    "exempt_reason": None if fixed else "near_field",
                    "f_valid_floor_hz": 320.0 if fixed else None,
                },
                "overlap_levels": [{
                    "above_validity_floor": True,
                    "usable": True,
                }],
            },
            "placement_proof": proof(
                "driver_reference_axis_v1" if fixed else "driver_same_distance_v1"
            ),
        }

    bindings = dict(
        driver_repeat_binding(
            speaker_group_id="mono",
            role="woofer",
            target_fingerprint="6" * 64,
            capture_geometry=geometry,
        )
        for geometry in ("near_field", "reference_axis")
    )
    return {
        "topology_id": "topology-1",
        "profile_context_id": "profile-1",
        "driver_targets": [target],
        "measurements": {
            "active_comparison_set": comparison,
            "summary": {
                "latest_driver_measurements": {
                    "mono:woofer": record("near_field")
                },
                "latest_reference_axis_driver_measurements": {
                    "mono:woofer": record("reference_axis")
                },
            },
        },
        "repeat_state": {
            "targets": {
                target_id: {
                    "status": "completed",
                    "target_fingerprint": fingerprint,
                    "attempts": 3,
                    "results": [
                        {"attempt": attempt, "accepted": True}
                        for attempt in (1, 2, 3)
                    ],
                }
                for target_id, fingerprint in bindings.items()
            }
        },
    }


def test_automatic_measurement_eligibility_accepts_only_complete_current_evidence():
    result = automatic_measurement_eligibility(**_evidence())

    assert result.ready is True
    assert result.reason is None
    assert result.missing == ()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda data: data.update(topology_id="changed"),
        lambda data: data.update(profile_context_id="changed"),
        lambda data: data["measurements"]["summary"][
            "latest_driver_measurements"
        ]["mono:woofer"].pop("mic_clipping"),
        lambda data: data["measurements"]["summary"][
            "latest_driver_measurements"
        ]["mono:woofer"]["acoustic"]["gating"].update({"applied": True}),
        lambda data: data["measurements"]["summary"][
            "latest_driver_measurements"
        ]["mono:woofer"]["acoustic"].update({"overlap_levels": "bad"}),
        lambda data: data["measurements"]["summary"][
            "latest_reference_axis_driver_measurements"
        ]["mono:woofer"]["acoustic"]["gating"].update(
            {"f_valid_floor_hz": None}
        ),
        lambda data: data["measurements"]["summary"][
            "latest_reference_axis_driver_measurements"
        ]["mono:woofer"].update({"placement_proof": {}}),
        lambda data: data.update(repeat_state={"targets": "bad"}),
        lambda data: data["repeat_state"]["targets"][
            "reference_axis/mono:woofer"
        ].update({"target_fingerprint": "f" * 64}),
    ),
)
def test_automatic_measurement_eligibility_fails_closed(mutate):
    data = copy.deepcopy(_evidence())
    mutate(data)

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert result.reason is not None
    assert result.missing


def test_completed_controller_cannot_authorize_two_accepted_acoustic_repeats():
    data = _evidence()
    data["measurements"]["summary"]["latest_driver_measurements"][
        "mono:woofer"
    ]["repeats"]["accepted"] = 2

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert "near_field:mono:woofer" in result.missing


def test_completed_controller_refuses_two_accepted_or_wrong_target():
    data = _evidence()
    entry = data["repeat_state"]["targets"]["reference_axis/mono:woofer"]
    entry.update({"accepted": 2, "target": 4})

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert "repeat:reference_axis:mono:woofer" in result.missing


@pytest.mark.parametrize(
    ("capture_geometry", "summary_key"),
    (
        ("near_field", "latest_driver_measurements"),
        ("reference_axis", "latest_reference_axis_driver_measurements"),
    ),
)
def test_acoustic_aggregate_refuses_fabricated_four_accepted(
    capture_geometry, summary_key
):
    data = _evidence()
    data["measurements"]["summary"][summary_key]["mono:woofer"]["repeats"][
        "accepted"
    ] = 4

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert f"{capture_geometry}:mono:woofer" in result.missing


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
def test_completed_controller_refuses_fabricated_four_accepted(
    capture_geometry,
):
    data = _evidence()
    target_id = (
        "mono:woofer"
        if capture_geometry == "near_field"
        else "reference_axis/mono:woofer"
    )
    entry = data["repeat_state"]["targets"][target_id]
    entry.update({
        "attempts": 4,
        "accepted": 4,
        "target": 3,
        "results": [
            {"attempt": attempt, "accepted": True}
            for attempt in (1, 2, 3, 4)
        ],
    })

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert f"repeat:{capture_geometry}:mono:woofer" in result.missing


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
@pytest.mark.parametrize(
    ("attempts", "results", "expected_ready"),
    (
        (
            3,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": True},
                {"attempt": 3, "accepted": True},
            ],
            True,
        ),
        (
            3,
            [{"attempt": 1, "accepted": True}] * 3,
            False,
        ),
        (
            3,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": True},
                {"attempt": 3, "accepted": True},
                7,
            ],
            False,
        ),
        (
            3,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 3, "accepted": True},
            ],
            False,
        ),
        (
            4,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": True},
                {"attempt": 3, "accepted": True},
            ],
            False,
        ),
        (
            4,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": False},
                {"attempt": 3, "accepted": True},
                {"attempt": 4, "accepted": True},
            ],
            True,
        ),
    ),
)
def test_completed_controller_requires_exact_attempt_coverage(
    capture_geometry, attempts, results, expected_ready
):
    data = _evidence()
    target_id = (
        "mono:woofer"
        if capture_geometry == "near_field"
        else "reference_axis/mono:woofer"
    )
    data["repeat_state"]["targets"][target_id].update(
        {
            "attempts": attempts,
            "results": results,
        }
    )

    result = automatic_measurement_eligibility(**data)

    assert result.ready is expected_ready
    missing_id = f"repeat:{capture_geometry}:mono:woofer"
    assert (missing_id in result.missing) is (not expected_ready)


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
def test_completed_controller_refuses_inflight_reservation(capture_geometry):
    data = _evidence()
    target_id = (
        "mono:woofer"
        if capture_geometry == "near_field"
        else "reference_axis/mono:woofer"
    )
    data["repeat_state"]["targets"][target_id]["inflight"] = "still-owned"

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert f"repeat:{capture_geometry}:mono:woofer" in result.missing


def test_public_repeat_projection_preserves_completed_eligibility():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    data = _evidence()
    store = CrossoverLevelLease()
    for target_id, entry in data["repeat_state"]["targets"].items():
        entry["target_id"] = target_id
        entry["inflight"] = None
    store.set_durable_repeat_progress(data["repeat_state"])
    data["repeat_state"] = store.repeat_snapshot()["durable"]

    result = automatic_measurement_eligibility(**data)

    assert result.ready is True


@pytest.mark.parametrize("value", (None, "bad", {}, 3, True))
def test_mapping_sequence_and_repeat_progress_reject_malformed_types(value):
    assert mapping_sequence(value) == ()

    progress = repeat_progress(
        {
            "targets": {
                "mono:woofer": {
                    "attempts": value,
                    "accepted": value,
                    "target": value,
                }
            },
            "failures": value,
        },
        "mono:woofer",
    )

    expected_count = 3 if type(value) is int else 0
    assert progress.attempts == expected_count
    assert progress.accepted == expected_count
    assert progress.target == 3
    assert progress.failure == {}
    assert progress.completed is False
    assert progress.last_result == {}


def test_repeat_progress_exposes_completed_status_and_last_result():
    """The envelope's driver-step derivation must be able to tell "the repeat
    set finished all its bounded attempts" apart from "still in progress" --
    the durable ledger's own ``status`` field is the only place that
    distinction lives, and it must survive the safe/bounded projection."""
    progress = repeat_progress(
        {
            "targets": {
                "mono:woofer": {
                    "status": "completed",
                    "attempts": 3,
                    "accepted": 3,
                    "target": 3,
                    "results": [
                        {"attempt": 1, "accepted": True},
                        {
                            "attempt": 2,
                            "accepted": True,
                            "estimated_snr_db": 8.4,
                            "snr_verdict": "insufficient",
                        },
                    ],
                }
            },
            "failures": {},
        },
        "mono:woofer",
    )

    assert progress.completed is True
    assert progress.last_result == {
        "attempt": 2,
        "accepted": True,
        "estimated_snr_db": 8.4,
        "snr_verdict": "insufficient",
    }


def test_render_repeat_progress_uses_human_terms_not_repeat_n():
    """The progress sentence must never say "Repeat N" -- that leaked the
    repeat-ledger counter into user-facing copy. It stays a plain count of
    accepted-vs-target measurements."""
    zero_attempts = render_repeat_progress(
        RepeatProgress(
            attempts=0, accepted=0, target=3, failure={}, completed=False,
            last_result={},
        )
    )
    assert zero_attempts == " JTS takes 3 stationary repeats."
    assert "Repeat" not in zero_attempts

    with_attempts = render_repeat_progress(
        RepeatProgress(
            attempts=2, accepted=2, target=3, failure={}, completed=False,
            last_result={},
        )
    )
    assert with_attempts == " 2 of 3 measurements accepted."
    assert "Repeat" not in with_attempts


@pytest.mark.parametrize("status", ("active", "ready", "refused", "aborted", None))
def test_repeat_progress_completed_is_false_for_every_other_status(status):
    progress = repeat_progress(
        {"targets": {"mono:woofer": {"status": status, "attempts": 1}}, "failures": {}},
        "mono:woofer",
    )

    assert progress.completed is False


def test_level_check_restart_invalidates_stale_completed_insufficient_evidence(
    tmp_path,
):
    """Verifies the invalidation machinery a driver-level-check restart relies
    on (``jasper.web.correction_setup._handle_crossover_relay_level_match``'s
    inner ``_run()``, the "not fixed_axis_request and not continuing" branch:
    ``repeat_admission.invalidate()`` then
    ``measurement.clear_active_comparison_set()`` before a fresh comparison
    set is minted, logged as ``event=correction.crossover_comparison_set_invalidated
    reason=new_level_match_started``). A woofer repeat set that completed
    3/3 with an insufficient median must not survive that restart: the
    ledger itself is wiped, and the stale acoustic record's placement proof
    (bound to the OLD comparison set) fails ``capture_proof_valid`` against
    the freshly-minted one -- so the honest-terminal render's own "Restart
    driver level check" action cannot be undone by a stale record leaking
    back into ``driver_acoustic_usable``."""
    from jasper.active_speaker import repeat_admission
    from jasper.active_speaker.crossover_eligibility import driver_acoustic_usable
    from jasper.active_speaker.capture_geometry import normalized_placement_proof

    repeat_path = tmp_path / "repeat.json"
    target = {
        "speaker_group_id": "mono",
        "role": "woofer",
        "target_fingerprint": "6" * 64,
    }

    def comparison(seed: str) -> dict:
        core = {
            "schema_version": 2,
            "comparison_set_id": seed * 32,
            "created_at": "2026-07-12T12:00:00Z",
            "topology_id": "topology-1",
            "profile_context_id": "profile-1",
            "setup_sha256": "2" * 64,
            "device_sha256": "3" * 64,
            "calibration_id": "",
            "driver_level_locks": {
                "mono:woofer": {
                    "target_id": "mono:woofer",
                    "speaker_group_id": "mono",
                    "role": "woofer",
                    "tone_frequency_hz": 100.0,
                    "tone_peak_dbfs": -20.0,
                    "commissioning_gain_db": 0.0,
                    "locked_main_volume_db": -18.0,
                }
            },
        }
        core["fingerprint"] = comparison_set_fingerprint(core)
        return core

    old_comparison = comparison("1")

    # Drive the repeat ledger to "completed" with an insufficient median,
    # the same shape `_finalize_driver_repeat_set` persists for 3/3 accepted
    # repeats whose aggregate SNR never cleared the floor.
    repeat_admission.activate(old_comparison, path=repeat_path)
    for attempt in (1, 2, 3):
        reservation = repeat_admission.reserve(
            old_comparison,
            target_id="mono:woofer",
            target_fingerprint=target["target_fingerprint"],
            path=repeat_path,
        )
        repeat_admission.finish(
            old_comparison,
            target_id="mono:woofer",
            target_fingerprint=target["target_fingerprint"],
            token=reservation["token"],
            result={"accepted": True, "snr_verdict": "insufficient"},
            status="ready" if attempt == 3 else "active",
            path=repeat_path,
        )
    repeat_admission.complete(
        old_comparison,
        target_id="mono:woofer",
        target_fingerprint=target["target_fingerprint"],
        path=repeat_path,
    )
    assert (
        repeat_admission.snapshot(old_comparison, path=repeat_path)["targets"][
            "mono:woofer"
        ]["status"]
        == "completed"
    )

    old_record = {
        "speaker_group_id": "mono",
        "role": "woofer",
        "target_fingerprint": target["target_fingerprint"],
        "captured": True,
        "mic_clipping": False,
        "repeats": {"target": 3, "accepted": 3, "admission_attempts": 3},
        "acoustic": {
            "verdict": "present",
            "capture_geometry": "near_field",
            "mic_clipping": False,
            "gating": {
                "applied": False,
                "exempt_reason": "near_field",
                "f_valid_floor_hz": None,
            },
            "overlap_levels": [{"above_validity_floor": True, "usable": True}],
        },
        "placement_proof": normalized_placement_proof(
            policy_id="driver_same_distance_v1",
            acknowledgement_binding="ack",
            relay_session_id="relay-1",
            capture_page={
                "capture_protocol_version": 2,
                "capture_page_build": "20260711.1",
            },
            speaker_group_id="mono",
            role="woofer",
            target_fingerprint=target["target_fingerprint"],
            comparison_set=old_comparison,
        ),
    }
    assert driver_acoustic_usable(
        old_record, old_comparison, target, capture_geometry="near_field"
    )

    # The exact pair the restart branch calls before minting a fresh
    # comparison set.
    repeat_admission.invalidate(path=repeat_path)
    new_comparison = comparison("9")

    wiped = repeat_admission.snapshot(path=repeat_path)
    assert wiped["targets"] == {}
    assert new_comparison["comparison_set_id"] != old_comparison["comparison_set_id"]
    assert not driver_acoustic_usable(
        old_record, new_comparison, target, capture_geometry="near_field"
    )


def _real_write_path_topology():
    """A mono active-2-way topology built the same way the production
    commissioning flow measures against -- not a synthetic status dict."""
    from tests.active_speaker_fixtures import mono_output_topology

    return mono_output_topology(
        tweeter_output=1, tweeter_verified=True, topology_name="Bench mono"
    )


def _real_comparison_set(topology, *, state_path):
    from jasper.active_speaker.measurement import (
        active_driver_targets,
        start_active_comparison_set,
    )

    targets = active_driver_targets(topology)
    locks = {
        t["target_id"]: {
            "target_id": t["target_id"],
            "speaker_group_id": t["speaker_group_id"],
            "role": t["role"],
            "tone_frequency_hz": 100.0,
            "tone_peak_dbfs": -20.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -18.0,
        }
        for t in targets
    }
    return start_active_comparison_set(
        topology,
        profile_context_id="profile-1",
        setup_sha256="2" * 64,
        device_sha256="3" * 64,
        calibration_id="",
        driver_level_locks=locks,
        state_path=state_path,
        now="2026-07-16T05:00:00Z",
    )


def _real_floor_confirmation(
    topology,
    *,
    state_path,
    role="woofer",
    output_index=0,
    playback_id="floor-confirm-1",
    now="2026-07-16T05:41:12Z",
):
    """Record the by-ear driver-level-check ceremony through the real write
    path -- mirrors ``web_commissioning.confirm_driver_test``."""
    from jasper.active_speaker.measurement import record_driver_measurement

    target = {
        "speaker_group_id": "mono",
        "role": role,
        "driver_role": role,
        "output_index": output_index,
    }
    confirmed = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": role,
            "outcome": "heard_correct_driver",
            "playback_id": playback_id,
        },
        safe_session={
            "status": "armed",
            "quiet_start": {
                "status": "floor_confirmed",
                "floor_audio_confirmed": True,
                "current_target": target,
                "last_operator_result": {
                    "accepted": True,
                    "outcome": "heard_correct_driver",
                    "playback_id": playback_id,
                    "target": target,
                },
            },
        },
        state_path=state_path,
        now=now,
    )
    return confirmed["summary"]["latest_driver_confirmations"][f"mono:{role}"]


def _real_sweep_capture(
    topology,
    *,
    state_path,
    comparison_set,
    floor_confirmation,
    playback_id,
    snr_db,
    usable,
    now,
    role="woofer",
):
    """Record one WINNER acoustic capture through the real write path --
    mirrors what ``web_measurement._finalize_driver_repeat_set`` durably
    persists for an accepted 3/3 repeat set, including the sweep's own
    (necessarily different) playback id relative to the floor confirmation
    (see ``crossover_eligibility._FLOOR_REPLAY_MISMATCH_ISSUE_CODE``).
    """
    from jasper.active_speaker.measurement import (
        _target_lookup,
        record_driver_measurement,
    )

    target = _target_lookup(topology)[f"mono:{role}"]
    target_fingerprint = target["target_fingerprint"]
    placement_proof = {
        "schema_version": 1,
        "policy_id": "driver_same_distance_v1",
        "accepted": True,
        "confirmation_source": "relay_begin_capture",
        "acknowledgement_binding_sha256": "5" * 64,
        "relay_session_id": f"relay-{role}",
        "capture_protocol_version": 2,
        "capture_page_build": "20260711.1",
        "speaker_group_id": "mono",
        "role": role,
        "target_fingerprint": target_fingerprint,
        "captured": True,
        "comparison_set_id": comparison_set["comparison_set_id"],
        "comparison_set_fingerprint": comparison_set["fingerprint"],
    }
    swept = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": role,
            "outcome": "heard_correct_driver",
            "playback_id": playback_id,
            "observed_mic_dbfs": -18.0,
            "acoustic": {
                "verdict": "present",
                "capture_geometry": "near_field",
                "mic_clipping": False,
                "gating": {
                    "applied": False,
                    "exempt_reason": "near_field",
                    "f_valid_floor_hz": None,
                },
                "overlap_levels": [{
                    "fc_hz": 2000.0,
                    "above_validity_floor": True,
                    "usable": usable,
                    "snr_verdict": "ok" if usable else "insufficient",
                }],
            },
            "placement_proof": placement_proof,
            "repeats": {
                "repeat_group_id": f"grp-{playback_id}",
                "target": 3,
                "accepted": 3,
                "rejected": 0,
                "recaptured": False,
                "needed_recapture": False,
                "aggregate": "median",
                "spread_db_p90": 0.4,
                "confidence": "normal",
                "admission_attempts": 3,
                "per_repeat": [
                    {
                        "index": i,
                        "attempt": i + 1,
                        "verdict": "present",
                        "accepted": True,
                        "reject_reason": None,
                        "artifact_path": f"captures/{playback_id}-{i}.wav",
                        "estimated_snr_db": snr_db,
                        "clipping": False,
                        "above_validity_floor": True,
                        "level_dbfs": -18.0,
                        "capture_admission": None,
                    }
                    for i in range(3)
                ],
            },
        },
        durable_floor_confirmation=floor_confirmation["floor_confirmation"],
        state_path=state_path,
        now=now,
    )
    record = swept["summary"]["latest_driver_measurements"][f"mono:{role}"]
    return record, target_fingerprint, swept["active_comparison_set"]


def test_accepted_production_capture_is_usable_despite_captured_false(tmp_path):
    """JTS3 run 22 pre-fix repro: a genuinely accepted 3/3 woofer near-field
    repeat set (60 dB SNR, overlap usable) built through the REAL
    ``measurement.record_driver_measurement`` write path -- not a synthetic
    status dict -- still comes back ``captured: False`` on the durable
    record (the sweep's own playback id can never equal the earlier,
    separately-recorded floor confirmation's -- pinned by
    ``test_active_speaker_measurement.py::
    test_sweep_evidence_never_clobbers_the_confirmation_gate``). Before the
    fix, gating eligibility on ``record.get("captured") is True`` refused
    this driver forever; ``driver_acoustic_usable`` must accept it."""
    topology = _real_write_path_topology()
    state_path = tmp_path / "measurements.json"
    comparison_set = _real_comparison_set(topology, state_path=state_path)
    floor_confirmation = _real_floor_confirmation(topology, state_path=state_path)

    record, target_fingerprint, active_comparison_set = _real_sweep_capture(
        topology,
        state_path=state_path,
        comparison_set=comparison_set,
        floor_confirmation=floor_confirmation,
        playback_id="sweep-3",
        snr_db=60.9,
        usable=True,
        now="2026-07-17T11:08:42Z",
    )

    # The write-path quirk this fix works around, still exactly reproduced.
    assert record["captured"] is False
    assert any(
        issue["code"] == "driver_measurement_playback_mismatch"
        for issue in record["issues"]
    )

    target = {
        "speaker_group_id": "mono",
        "role": "woofer",
        "target_fingerprint": target_fingerprint,
    }
    assert driver_acoustic_usable(
        record, active_comparison_set, target, capture_geometry="near_field"
    ) is True


def test_fresh_accepted_capture_wins_over_stale_insufficient_siblings(tmp_path):
    """Mirrors the run-22 durable store shape: several older, insufficient-
    SNR near-field attempts for the SAME target/fingerprint (JTS3's indices
    [2]-[9]) followed by a fresh, accepted, usable capture (indices
    [10]/[11]). The selection (``measurement._latest_current_driver_records``)
    must resolve to the fresh record, not a stale one -- and the fresh
    record, once resolved, must be usable. Evaluating a stale record
    directly must still refuse (the acoustic-quality gate is untouched)."""
    topology = _real_write_path_topology()
    state_path = tmp_path / "measurements.json"
    comparison_set = _real_comparison_set(topology, state_path=state_path)
    floor_confirmation = _real_floor_confirmation(topology, state_path=state_path)

    stale_record = None
    for index in range(3):
        stale_record, target_fingerprint, _ = _real_sweep_capture(
            topology,
            state_path=state_path,
            comparison_set=comparison_set,
            floor_confirmation=floor_confirmation,
            playback_id=f"stale-sweep-{index}",
            snr_db=10.5 - index,
            usable=False,
            now=f"2026-07-14T0{index}:00:00Z",
        )

    fresh_record, target_fingerprint, active_comparison_set = _real_sweep_capture(
        topology,
        state_path=state_path,
        comparison_set=comparison_set,
        floor_confirmation=floor_confirmation,
        playback_id="fresh-sweep",
        snr_db=60.9,
        usable=True,
        now="2026-07-17T11:08:42Z",
    )

    # Selection resolved to the fresh record (distinguishable by its own
    # sweep playback id), not one of the three stale ones written first.
    assert fresh_record["playback_id"] == "fresh-sweep"
    assert stale_record["playback_id"] != fresh_record["playback_id"]

    target = {
        "speaker_group_id": "mono",
        "role": "woofer",
        "target_fingerprint": target_fingerprint,
    }
    assert driver_acoustic_usable(
        fresh_record, active_comparison_set, target, capture_geometry="near_field"
    ) is True
    # The stale, insufficient-SNR record on its own is correctly still
    # refused -- the fix did not loosen the acoustic-quality gate.
    assert driver_acoustic_usable(
        stale_record, active_comparison_set, target, capture_geometry="near_field"
    ) is False


# --- #1513: infra-phase failures must not consume the acceptance budget ------

_1513_FP = "6" * 64
_1513_TARGET = {
    "speaker_group_id": "mono",
    "role": "woofer",
    "target_fingerprint": _1513_FP,
}


def _transport(attempt: int) -> dict:
    return {
        "attempt": attempt,
        "accepted": False,
        "reject_reason": "capture_failed",
        "phase": "transport",
        "audio_emitted": False,
    }


def _acoustic_reject(attempt: int) -> dict:
    return {
        "attempt": attempt,
        "accepted": False,
        "reject_reason": "level_outlier",
        "audio_emitted": True,
    }


def _accept(attempt: int) -> dict:
    return {"attempt": attempt, "accepted": True, "audio_emitted": True}


def _repeat_targets(geometry: str, attempts: int, results: list) -> dict:
    target_id, target_fingerprint = driver_repeat_binding(
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint=_1513_FP,
        capture_geometry=geometry,
    )
    return {
        target_id: {
            "status": "completed",
            "target_fingerprint": target_fingerprint,
            "attempts": attempts,
            "inflight": None,
            "results": results,
        }
    }


def _completed(geometry: str, attempts: int, results: list) -> bool:
    return driver_repeat_completed(
        _1513_TARGET,
        _repeat_targets(geometry, attempts, results),
        capture_geometry=geometry,
    )


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
def test_acoustic_rejections_consume_budget_but_transport_failures_do_not(
    capture_geometry,
):
    # One acoustic reject (a tone played) is the single allowed non-accept.
    assert _completed(
        capture_geometry,
        4,
        [_accept(1), _acoustic_reject(2), _accept(3), _accept(4)],
    ) is True
    # A second acoustic reject spends a fifth audio attempt -> over the audible
    # budget -> refused. The acoustic gate is unchanged for real captures.
    assert _completed(
        capture_geometry,
        5,
        [_accept(1), _acoustic_reject(2), _accept(3), _acoustic_reject(4),
         _accept(5)],
    ) is False
    # Two TRANSPORT failures across the same five reservations are refunded, so
    # only three measurement attempts count -> completed. The exact scenario
    # #1513 makes possible: two infra failures no longer block three accepts.
    assert _completed(
        capture_geometry,
        5,
        [_transport(1), _transport(2), _accept(3), _accept(4), _accept(5)],
    ) is True


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
def test_unknown_audio_is_not_refunded_in_eligibility(capture_geometry):
    def unknown_reject(attempt: int) -> dict:
        return {"attempt": attempt, "accepted": False}  # audio_emitted absent

    # Fail-closed: two unknown-audio rejects consume the budget just like real
    # acoustic rejects -> five measurement attempts -> refused.
    assert _completed(
        capture_geometry,
        5,
        [_accept(1), unknown_reject(2), _accept(3), unknown_reject(4),
         _accept(5)],
    ) is False
    # The same shape with PROVEN no-audio is refunded -> completed. Only
    # audio_emitted is False reopens headroom.
    assert _completed(
        capture_geometry,
        5,
        [_accept(1), _transport(2), _accept(3), _transport(4), _accept(5)],
    ) is True


def _inject_repeat_results(data: dict, results: list, attempts: int) -> None:
    for target_id in ("mono:woofer", "reference_axis/mono:woofer"):
        data["repeat_state"]["targets"][target_id].update(
            {"attempts": attempts, "results": results}
        )


def _set_admission_attempts(data: dict, value: int) -> None:
    for key in (
        "latest_driver_measurements",
        "latest_reference_axis_driver_measurements",
    ):
        data["measurements"]["summary"][key]["mono:woofer"]["repeats"][
            "admission_attempts"
        ] = value


def test_two_transport_failures_and_three_accepts_pass_both_eligibility_gates():
    data = _evidence()
    _inject_repeat_results(
        data,
        [_transport(1), _transport(2), _accept(3), _accept(4), _accept(5)],
        attempts=5,
    )
    result = automatic_measurement_eligibility(**data)

    assert result.ready is True
    assert result.missing == ()
    # The repeat-completion gate honours the refund: five reservations, three
    # measurement attempts, three accepts.
    assert driver_repeat_completed(
        _1513_TARGET,
        data["repeat_state"]["targets"],
        capture_geometry="near_field",
    ) is True
    # The record-side gate keeps admission_attempts within [3, 4] because it
    # records the MEASUREMENT count (3), not the raw five reservations.
    near_record = data["measurements"]["summary"]["latest_driver_measurements"][
        "mono:woofer"
    ]
    assert near_record["repeats"]["admission_attempts"] == 3
    assert driver_acoustic_usable(
        near_record,
        data["measurements"]["active_comparison_set"],
        _1513_TARGET,
        capture_geometry="near_field",
    ) is True


def test_mixed_transport_and_acoustic_outlier_run_is_eligible():
    data = _evidence()
    _inject_repeat_results(
        data,
        [_transport(1), _acoustic_reject(2), _accept(3), _accept(4), _accept(5)],
        attempts=5,
    )
    # Four measurement attempts (one acoustic outlier + three accepts); the
    # transport failure is refunded and the outlier is the one allowed
    # non-accept.
    _set_admission_attempts(data, 4)

    result = automatic_measurement_eligibility(**data)

    assert result.ready is True
    assert result.missing == ()


@pytest.mark.parametrize(
    ("admission_attempts", "ready"),
    ((3, True), (4, True), (5, False)),
)
def test_driver_acoustic_usable_gates_admission_attempts_on_measurement_budget(
    admission_attempts, ready
):
    # admission_attempts == 4 is a set that survived a refunded transport
    # failure (four measurement attempts); it must still pass. admission_attempts
    # == 5 is what the pre-fix raw reservation counter would have leaked in —
    # it must fail, or the record-side gate would reject a legitimately
    # completed set.
    data = _evidence()
    _set_admission_attempts(data, admission_attempts)

    result = automatic_measurement_eligibility(**data)

    assert result.ready is ready
