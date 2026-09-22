# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging

from pathlib import Path

import pytest

from jasper.active_speaker.measurement import (
    active_driver_targets,
    active_summed_targets,
    confirmed_driver_roles,
    current_driver_floor_evidence,
    load_measurement_state,
    record_driver_measurement,
    start_active_comparison_set,
)
from jasper.output_topology import OutputTopology
from tests._log_events import event_fields, event_records
from tests.active_speaker_fixtures import mono_output_topology


def _topology(
    *,
    tweeter_output: int = 1,
    tweeter_verified: bool = True,
) -> OutputTopology:
    return mono_output_topology(
        tweeter_output=tweeter_output,
        tweeter_verified=tweeter_verified,
        topology_name="Bench mono",
    )


def _safe_session(
    *,
    role: str,
    output_index: int,
    playback_id: str,
) -> dict:
    target = {
        "speaker_group_id": "mono",
        "role": role,
        "driver_role": role,
        "output_index": output_index,
    }
    return {
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
    }


def test_measurement_state_lists_active_driver_and_summed_targets(
    tmp_path: Path,
) -> None:
    topology = _topology()
    payload = load_measurement_state(
        topology,
        state_path=tmp_path / "measurements.json",
    )

    assert [target["target_id"] for target in active_driver_targets(topology)] == [
        "mono:woofer",
        "mono:tweeter",
    ]
    assert [target["speaker_group_id"] for target in active_summed_targets(topology)] == [
        "mono",
    ]
    assert payload["status"] == "needs_driver_measurements"
    assert payload["summary"]["required_driver_count"] == 2
    assert payload["permissions"]["may_not_play_audio"] is True
    assert payload["permissions"]["may_not_load_camilla"] is True


def test_driver_measurement_counts_correct_driver_without_requiring_mic(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    missing_mic = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-1",
            "test_level_dbfs": -72,
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-1",
        ),
        state_path=state_path,
        now="2026-06-14T12:00:00Z",
    )
    latest = missing_mic["summary"]["latest_driver_measurements"]["mono:woofer"]

    assert latest["captured"] is True
    assert "driver_measurement_mic_missing" in {
        issue["code"] for issue in latest["issues"]
    }
    assert missing_mic["summary"]["driver_measurements_complete"] is False
    assert missing_mic["summary"]["captured_driver_count"] == 1

    captured = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-2",
            "test_level_dbfs": -68,
            "observed_mic_dbfs": -42.5,
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-2",
        ),
        state_path=state_path,
        now="2026-06-14T12:01:00Z",
    )

    assert captured["summary"]["latest_driver_measurements"]["mono:woofer"][
        "captured"
    ] is True
    assert captured["summary"]["captured_driver_count"] == 1


def test_driver_measurement_never_trusts_client_capture_admission(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-1",
            "observed_mic_dbfs": -42.5,
            "capture_admission": {"admission_id": "client-forged"},
        },
        safe_session=_safe_session(
            role="woofer", output_index=0, playback_id="playback-1"
        ),
        state_path=tmp_path / "measurements.json",
    )

    record = state["summary"]["latest_driver_measurements"]["mono:woofer"]
    assert record["capture_admission"] is None


def test_confirmed_driver_roles_are_current_topology_captured_roles(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-woofer",
            "test_level_dbfs": -72,
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-woofer",
        ),
        state_path=state_path,
    )
    record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "tweeter",
            "outcome": "heard_wrong_driver",
            "playback_id": "playback-tweeter",
            "test_level_dbfs": -72,
        },
        safe_session=_safe_session(
            role="tweeter",
            output_index=1,
            playback_id="playback-tweeter",
        ),
        state_path=state_path,
    )

    assert confirmed_driver_roles(
        topology,
        speaker_group_id="mono",
        state_path=state_path,
    ) == ["woofer"]
    assert confirmed_driver_roles(
        _topology(tweeter_output=2),
        speaker_group_id="mono",
        state_path=state_path,
    ) == ["woofer"]


def _current_woofer_floor_state(tmp_path: Path):
    topology = _topology()
    state = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-woofer",
            "observed_mic_dbfs": -42.0,
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-woofer",
        ),
        state_path=tmp_path / "measurements.json",
    )
    return topology, state


def test_current_driver_floor_evidence_accepts_exact_current_target(
    tmp_path: Path,
) -> None:
    topology, state = _current_woofer_floor_state(tmp_path)

    evidence = current_driver_floor_evidence(
        topology,
        state,
        speaker_group_id="mono",
        role="woofer",
    )

    assert evidence["valid"] is True
    assert evidence["source"] == "durable_current_driver_measurement"
    assert evidence["playback_id"] == "playback-woofer"


def test_current_driver_floor_evidence_rejects_forged_matching_output(
    tmp_path: Path,
) -> None:
    topology, state = _current_woofer_floor_state(tmp_path)
    record = state["summary"]["latest_driver_measurements"]["mono:woofer"]
    # Reproducer: both the record and embedded confirmation agree on 999, but
    # the current topology owns output 0. Agreement with oneself is not enough.
    record["output_index"] = 999
    record["floor_confirmation"]["target"]["output_index"] = 999

    evidence = current_driver_floor_evidence(
        topology,
        state,
        speaker_group_id="mono",
        role="woofer",
    )

    assert evidence["valid"] is False
    assert evidence["reason"] == "driver_floor_confirmation_invalid"


@pytest.mark.parametrize(
    "malformed_issues",
    [{}, [None], [{}], [{"severity": "mystery"}]],
)
def test_current_driver_floor_evidence_rejects_malformed_issues_container(
    tmp_path: Path,
    malformed_issues,
) -> None:
    topology, state = _current_woofer_floor_state(tmp_path)
    record = state["summary"]["latest_driver_measurements"]["mono:woofer"]
    record["issues"] = malformed_issues

    evidence = current_driver_floor_evidence(
        topology,
        state,
        speaker_group_id="mono",
        role="woofer",
    )

    assert evidence["valid"] is False
    assert evidence["reason"] == "driver_floor_confirmation_invalid"


def test_sweep_evidence_never_clobbers_the_confirmation_gate(
    tmp_path: Path,
) -> None:
    """JTS3 run 13 -> run 14 (punch #29): recording sweep evidence for a
    driver used to overwrite ``latest_driver_measurements``'s only entry for
    that target -- the same slot ``current_driver_floor_evidence`` validated
    as "the operator's confirmation". A sweep capture's own per-capture
    playback id can never equal the original confirmation's, so its floor-
    confirmation check always mismatches and it records ``captured: False``.
    Once that became "latest", every subsequent measurement was refused
    pre-playback with "the saved driver confirmation is incomplete", even
    though the operator's ear-check was never actually invalidated.
    """
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    confirmed = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "confirm-1",
        },
        safe_session=_safe_session(
            role="woofer", output_index=0, playback_id="confirm-1"
        ),
        state_path=state_path,
        now="2026-07-16T05:41:12Z",
    )
    confirmation_record = confirmed["summary"]["latest_driver_confirmations"][
        "mono:woofer"
    ]
    assert confirmation_record["captured"] is True

    swept = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "sweep-1",
            "observed_mic_dbfs": -18.0,
            "acoustic": {"verdict": "present", "capture_geometry": "near_field"},
        },
        durable_floor_confirmation=confirmation_record["floor_confirmation"],
        state_path=state_path,
        now="2026-07-16T11:08:42Z",
    )
    sweep_record = swept["summary"]["latest_driver_measurements"]["mono:woofer"]
    # Reproduces the live JTS3 shape: the sweep's own playback id can never
    # match the original confirmation's, so it is recorded captured=False.
    assert sweep_record["captured"] is False
    assert sweep_record["acoustic"] is not None
    assert any(
        issue["code"] == "driver_measurement_playback_mismatch"
        for issue in sweep_record["issues"]
    )

    # THE REPRO: pre-fix, `current_driver_floor_evidence` read this same
    # clobbered record from `latest_driver_measurements` and refused. Post-fix
    # it reads the confirmation-only index, which the sweep never touched.
    evidence = current_driver_floor_evidence(
        topology,
        swept,
        speaker_group_id="mono",
        role="woofer",
    )
    assert evidence["valid"] is True
    assert evidence["playback_id"] == "confirm-1"

    # The by-ear write path still round-trips: the operator is still reported
    # as having confirmed the woofer, unaffected by the sweep evidence.
    assert confirmed_driver_roles(
        topology, speaker_group_id="mono", state_path=state_path
    ) == ["woofer"]


def test_current_driver_floor_evidence_refuses_when_never_confirmed(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state = load_measurement_state(topology, state_path=tmp_path / "measurements.json")

    evidence = current_driver_floor_evidence(
        topology,
        state,
        speaker_group_id="mono",
        role="woofer",
    )

    assert evidence["valid"] is False
    assert evidence["reason"] == "driver_floor_confirmation_required"


def test_latest_wrong_driver_result_removes_confirmed_driver_role(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-woofer-ok",
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-woofer-ok",
        ),
        state_path=state_path,
    )
    assert confirmed_driver_roles(
        topology,
        speaker_group_id="mono",
        state_path=state_path,
    ) == ["woofer"]

    record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_wrong_driver",
            "playback_id": "playback-woofer-wrong",
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-woofer-wrong",
        ),
        state_path=state_path,
    )

    assert confirmed_driver_roles(
        topology,
        speaker_group_id="mono",
        state_path=state_path,
    ) == []


def test_driver_measurement_requires_accepted_floor_result_for_same_target(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    payload = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "tweeter",
            "outcome": "heard_correct_driver",
            "observed_mic_dbfs": -42,
            "playback_id": "playback-tweeter",
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-tweeter",
        ),
        state_path=state_path,
    )
    latest = payload["summary"]["latest_driver_measurements"]["mono:tweeter"]

    assert latest["captured"] is False
    assert "driver_measurement_target_mismatch" in {
        issue["code"] for issue in latest["issues"]
    }
    assert payload["summary"]["driver_measurements_complete"] is False


def test_measurements_do_not_carry_across_output_topology_changes(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "measurements.json"
    original = _topology()
    for role in ("woofer", "tweeter"):
        output_index = 0 if role == "woofer" else 1
        playback_id = f"playback-{role}"
        record_driver_measurement(
            original,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42,
                "playback_id": playback_id,
            },
            safe_session=_safe_session(
                role=role,
                output_index=output_index,
                playback_id=playback_id,
            ),
            state_path=state_path,
        )

    changed = _topology(tweeter_output=2, tweeter_verified=False)
    payload = load_measurement_state(changed, state_path=state_path)

    assert payload["summary"]["driver_measurements_complete"] is False
    assert payload["summary"]["captured_driver_count"] == 1
    assert payload["summary"]["stale_driver_record_count"] == 1
    assert payload["summary"]["missing_driver_targets"] == [
        target for target in active_driver_targets(changed)
        if target["role"] == "tweeter"
    ]
    assert "stale_measurement_evidence_ignored" in {
        issue["code"] for issue in payload["issues"]
    }


@pytest.mark.parametrize(
    "record_order",
    (("near_field", "reference_axis"), ("reference_axis", "near_field")),
)
def test_driver_latest_indexes_are_geometry_scoped_in_both_record_orders(
    tmp_path: Path,
    record_order: tuple[str, str],
) -> None:
    """A fixed-axis response can never shadow near-field level evidence."""
    from jasper.active_speaker.capture_geometry import (
        REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
    )

    topology = _topology()
    state_path = tmp_path / "measurements.json"
    created_at = {
        "near_field": "2026-07-12T12:00:00Z",
        "reference_axis": "2026-07-12T12:01:00Z",
    }
    for geometry in record_order:
        raw = {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "observed_mic_dbfs": -42.0,
            "playback_id": f"playback-{geometry}",
            "acoustic": {
                # The proof policy is authoritative when it says fixed-axis;
                # this deliberately exercises a mismatched legacy analyzer
                # stamp on the reference record.
                "capture_geometry": "near_field",
            },
        }
        if geometry == "reference_axis":
            raw["placement_proof"] = {
                "policy_id": REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
            }
        record_driver_measurement(
            topology,
            raw,
            safe_session=_safe_session(
                role="woofer",
                output_index=0,
                playback_id=f"playback-{geometry}",
            ),
            state_path=state_path,
            now=created_at[geometry],
        )

    payload = load_measurement_state(topology, state_path=state_path)
    assert payload["latest_by_target"]["mono:woofer"]["created_at"] == (
        created_at["near_field"]
    )
    assert payload["latest_reference_axis_by_target"]["mono:woofer"][
        "created_at"
    ] == created_at["reference_axis"]
    assert payload["summary"]["latest_driver_measurements"]["mono:woofer"][
        "created_at"
    ] == created_at["near_field"]
    assert payload["summary"]["latest_reference_axis_driver_measurements"][
        "mono:woofer"
    ]["created_at"] == created_at["reference_axis"]


def test_start_active_comparison_set_stamps_bundle_session_id(
    tmp_path: Path, caplog,
) -> None:
    """bundle_session_id joins a comparison set to a durable commissioning
    bundle (jasper.active_speaker.bundles) without becoming part of the
    fingerprinted, comparison-critical core — comparison_set_valid must
    still pass and the fingerprint must not depend on it."""

    from jasper.active_speaker.capture_geometry import (
        comparison_set_fingerprint,
        comparison_set_valid,
    )

    state_path = tmp_path / "measurements.json"
    topology = _topology()
    driver_level_locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        }
        for target in active_driver_targets(topology)
    }

    with caplog.at_level(logging.INFO, logger="jasper.active_speaker.measurement"):
        with_bundle = start_active_comparison_set(
            topology,
            profile_context_id="protected-profile",
            setup_sha256="a" * 64,
            device_sha256="b" * 64,
            calibration_id="",
            driver_level_locks=driver_level_locks,
            bundle_session_id="abc123def456",
            state_path=state_path,
            now="2026-07-11T12:00:00Z",
        )

    assert with_bundle["bundle_session_id"] == "abc123def456"
    assert comparison_set_valid(with_bundle) is True
    assert load_measurement_state(topology, state_path=state_path)[
        "active_comparison_set"
    ] == with_bundle
    fields = event_fields(caplog, "correction.crossover_session_started")
    assert fields["session"] == "abc123def456"

    # bundle_session_id sits outside _COMPARISON_SET_CORE_KEYS: changing it
    # (or removing it) on the SAME comparison set must not move the
    # fingerprint comparison_set_fingerprint recomputes.
    mutated = {**with_bundle, "bundle_session_id": "a-totally-different-id"}
    assert comparison_set_fingerprint(mutated) == with_bundle["fingerprint"]
    dropped = {k: v for k, v in with_bundle.items() if k != "bundle_session_id"}
    assert comparison_set_fingerprint(dropped) == with_bundle["fingerprint"]

    without_bundle = start_active_comparison_set(
        topology,
        profile_context_id="protected-profile",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks=driver_level_locks,
        state_path=state_path,
        now="2026-07-11T12:05:00Z",
    )

    assert "bundle_session_id" not in without_bundle


def test_driver_measurement_records_optional_bundle_ref(tmp_path: Path) -> None:
    """A recorded driver measurement carries the optional bundle join key
    ({session_id, artifact_path}) verbatim when a bundle is open, and stores
    None (not an absent key) when it is not — old state files without the
    key still round-trip through load_measurement_state."""

    topology = _topology(tweeter_output=1)
    state_path = tmp_path / "measurements.json"
    bundle_ref = {"session_id": "sess-1", "artifact_path": "captures/x.wav"}

    with_bundle = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "silent",
        },
        bundle_ref=bundle_ref,
        state_path=state_path,
        now="2026-07-11T12:00:00Z",
    )
    record_with_bundle = with_bundle["driver_measurements"][-1]
    assert record_with_bundle["bundle"] == bundle_ref

    without_bundle = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "silent",
        },
        state_path=state_path,
        now="2026-07-11T12:01:00Z",
    )
    record_without_bundle = without_bundle["driver_measurements"][-1]
    assert record_without_bundle["bundle"] is None

    # Round-trips through a fresh load, including the pre-existing record
    # that has no "bundle" key at all in this state file's prior shape.
    reloaded = load_measurement_state(topology, state_path=state_path)
    assert reloaded["driver_measurements"][-2]["bundle"] == bundle_ref
    assert reloaded["driver_measurements"][-1]["bundle"] is None


def _repeat_summary():
    return {
        "repeat_group_id": "repeat-fixture",
        "target": 3, "accepted": 3, "rejected": 0,
        "recaptured": False, "needed_recapture": False,
        "aggregate": "median_magnitude", "spread_db_p90": 0.2, "confidence": "normal",
        "per_repeat": [{
            "index": index, "attempt": index + 1,
            "verdict": "heard_correct_driver", "accepted": True, "reject_reason": None,
            "artifact_path": Path(f"repeat_captures/r{index}.wav"),
            "estimated_snr_db": None, "clipping": False, "above_validity_floor": True,
            "level_dbfs": level, "capture_admission": {"admission_id": f"admission-r{index}"},
        } for index, level in enumerate((-30.0, -30.2, -29.9))],
        "aggregate_repeat": {
            "verdict": "heard_correct_driver",
            "acoustic": {"observed_mic_dbfs": -30.0, "mic_clipping": False},
        },
    }


def test_recorded_driver_record_is_the_repeat_aggregate(tmp_path: Path) -> None:
    topology = _topology(tweeter_output=1)
    state_path = tmp_path / "measurements.json"
    aggregate = _repeat_summary()
    aggregate["aggregate_repeat"].update({
        "wav_path": tmp_path / "r0.wav", "bundle_dir": tmp_path / "bundle",
        "analysis_kwargs": {"captured_wav": tmp_path / "r0.wav"}, "preset": object(),
    })
    # The Lane B floor verdict is tri-state. A rejected unknown fixed-axis
    # attempt can coexist with accepted evidence and must remain ``null`` in
    # the compact durable projection, never be rewritten as safely above.
    aggregate["per_repeat"][0]["above_validity_floor"] = None
    aggregate["per_repeat"][0]["accepted"] = False
    aggregate["per_repeat"][0]["reject_reason"] = "validity_floor_unknown"
    aggregate["accepted"] = 2
    aggregate["rejected"] = 1
    aggregate["confidence"] = "reduced"
    winner = aggregate["aggregate_repeat"]
    assert winner is not None

    state = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": winner["verdict"],
            "acoustic": winner["acoustic"],
            "playback_id": "play-1",
            "repeats": aggregate,
        },
        safe_session=_safe_session(
            role="woofer", output_index=0, playback_id="play-1"
        ),
        state_path=state_path,
        now="2026-07-11T12:00:00Z",
    )

    record = state["driver_measurements"][-1]
    assert record["repeats"]["repeat_group_id"] == aggregate["repeat_group_id"]
    assert record["repeats"]["accepted"] == 2
    assert record["repeats"]["confidence"] == "reduced"
    assert len(record["repeats"]["per_repeat"]) == 3
    assert record["repeats"]["per_repeat"][0]["above_validity_floor"] is None
    assert "aggregate_repeat" not in record["repeats"]
    assert [entry["artifact_path"] for entry in record["repeats"]["per_repeat"]] == [
        "repeat_captures/r0.wav",
        "repeat_captures/r1.wav",
        "repeat_captures/r2.wav",
    ]
    assert [
        entry["capture_admission"]["admission_id"]
        for entry in record["repeats"]["per_repeat"]
    ] == ["admission-r0", "admission-r1", "admission-r2"]

    # Re-read the actual file, not only the returned in-memory state. Before
    # the fix, json.dumps failed here on aggregate_repeat.analysis_kwargs's
    # PosixPath and no durable measurement was written after three accepted
    # hardware repeats.
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    durable_repeats = persisted["driver_measurements"][-1]["repeats"]
    assert durable_repeats == record["repeats"]
    assert "aggregate_repeat" not in durable_repeats

    # The latest-wins pointer resolves to this exact aggregate record.
    latest = state["summary"]["latest_driver_measurements"]["mono:woofer"]
    assert latest is record
    assert latest["repeats"]["repeat_group_id"] == aggregate["repeat_group_id"]

    # Per-repeat evidence is compact index/authoritative-attempt metadata
    # (verdict/accepted/reject_reason/
    # artifact_path/...) -- no full acoustic curves for the non-winning
    # repeats leak into measurement state; that lives only in the bundle.
    for entry in record["repeats"]["per_repeat"]:
        assert "acoustic" not in entry
        assert set(entry) == {
            "index",
            "attempt",
            "verdict",
            "accepted",
            "reject_reason",
            "artifact_path",
            "estimated_snr_db",
            "clipping",
            "above_validity_floor",
                "level_dbfs",
                "capture_admission",
            }


@pytest.mark.parametrize(
    "malformation",
    [
        "non_artifact_path",
        "absolute_artifact_path",
        "traversal_artifact_path",
        "non_mapping_summary",
        "summary_extra_key",
        "per_repeat_extra_key",
        "per_repeat_not_mapping",
        "nan_spread",
    ],
)
def test_malformed_repeat_summary_is_rejected_before_state_write(
    tmp_path: Path, malformation: str
) -> None:
    topology = _topology(tweeter_output=1)
    state_path = tmp_path / "measurements.json"
    aggregate = _repeat_summary()
    winner = aggregate["aggregate_repeat"]
    if malformation == "non_artifact_path":
        aggregate["confidence"] = tmp_path / "runtime-only"
    elif malformation == "absolute_artifact_path":
        aggregate["per_repeat"][0]["artifact_path"] = tmp_path / "r0.wav"
    elif malformation == "traversal_artifact_path":
        aggregate["per_repeat"][0]["artifact_path"] = "../outside.wav"
    elif malformation == "non_mapping_summary":
        aggregate = tmp_path / "runtime-only"
    elif malformation == "summary_extra_key":
        aggregate["runtime_only"] = object()
    elif malformation == "per_repeat_extra_key":
        aggregate["per_repeat"][0]["runtime_only"] = object()
    elif malformation == "per_repeat_not_mapping":
        aggregate["per_repeat"][0] = "not-an-object"
    elif malformation == "nan_spread":
        aggregate["spread_db_p90"] = float("nan")

    with pytest.raises(ValueError, match="repeat summary"):
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": "woofer",
                "outcome": winner["verdict"],
                "acoustic": winner["acoustic"],
                "playback_id": "play-1",
                "repeats": aggregate,
            },
            safe_session=_safe_session(
                role="woofer", output_index=0, playback_id="play-1"
            ),
            state_path=state_path,
        )
    assert not state_path.exists()


# --- lifecycle events (lane E, docs/active-crossover-information-design.md
# "Structured events") -------------------------------------------------------


def test_start_active_comparison_set_emits_session_started_event(
    tmp_path: Path, caplog,
) -> None:
    state_path = tmp_path / "measurements.json"
    topology = _topology()
    driver_level_locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        }
        for target in active_driver_targets(topology)
    }

    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.measurement",
    ):
        comparison_set = start_active_comparison_set(
            topology,
            profile_context_id="protected-profile",
            setup_sha256="a" * 64,
            device_sha256="b" * 64,
            calibration_id="cal-1",
            driver_level_locks=driver_level_locks,
            state_path=state_path,
            now="2026-07-11T12:00:00Z",
        )

    fields = event_fields(caplog, "correction.crossover_session_started")
    # group(s) via topology: _topology() has exactly one active group, "mono".
    assert fields["group"] == "mono"
    assert fields["calibration_id"] == "cal-1"
    assert fields["comparison_set_fingerprint"] == comparison_set["fingerprint"]
    # No bundle exists yet (SC-4 lands in a later lane), so session is omitted
    # rather than rendered as a literal "session=null".
    assert "session" not in fields


def test_start_active_comparison_set_raises_before_persisting_emits_no_event(
    tmp_path: Path, caplog,
) -> None:
    # Incomplete driver level locks raise before the state is ever persisted;
    # no event should fire for a call that never actually started a session.
    state_path = tmp_path / "measurements.json"
    topology = _topology()

    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.measurement",
    ):
        with pytest.raises(ValueError, match="incomplete"):
            start_active_comparison_set(
                topology,
                profile_context_id="protected-profile",
                setup_sha256="a" * 64,
                device_sha256="b" * 64,
                calibration_id="",
                driver_level_locks={},
                state_path=state_path,
            )

    assert not event_records(caplog, "correction.crossover_session_started")


def test_complete_driver_measurements_are_ready_for_baseline(tmp_path: Path) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    for output_index, role in enumerate(("woofer", "tweeter")):
        playback_id = f"playback-{role}"
        record_driver_measurement(
            topology,
            {"speaker_group_id": "mono", "role": role,
             "outcome": "heard_correct_driver", "playback_id": playback_id},
            safe_session=_safe_session(
                role=role, output_index=output_index, playback_id=playback_id,
            ),
            state_path=state_path,
        )

    state = load_measurement_state(topology, state_path=state_path)
    assert state["summary"]["driver_measurements_complete"] is True
    assert state["summary"]["captured_driver_count"] == 2
    assert state["status"] == "ready_for_baseline"
