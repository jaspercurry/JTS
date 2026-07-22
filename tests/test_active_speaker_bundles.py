# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the active-speaker commissioning bundle (jasper/active_speaker/bundles.py).

Modeled on tests/test_correction_bundles.py, the room-correction sibling this
module ports its pattern from. Covers: info.json's required fields, artifact
manifest mechanics (reused verbatim from jasper.correction.bundles), the
capture/apply write paths, retention, and the fail-soft contract — a bundle
write failure must never block the capture/apply path recording it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jasper.active_speaker import bundles
from jasper.audio_measurement.excitation_artifacts import (
    ADMISSION_AUTHORITY_MARKER,
    AdmissionArtifactError,
    AdmissionArtifactErrorCode,
    create_admission_authority,
)
from jasper.correction.bundles import read_artifact_manifest
from tests.active_speaker_fixtures import mono_output_topology


def _topology():
    return mono_output_topology(topology_name="Bench mono")


def _open(tmp_path: Path, **kwargs):
    topology = kwargs.pop("topology", None) or _topology()
    return bundles.open_bundle(
        topology,
        calibration_id=kwargs.pop("calibration_id", ""),
        sessions_dir=tmp_path,
        **kwargs,
    )


def _driver_payload(
    *,
    group: str = "mono",
    role: str = "woofer",
    recorded: bool = True,
    measurement_id: str = "meas-1",
) -> dict:
    measurement = None
    if recorded:
        measurement = {
            "measurement_id": measurement_id,
            "speaker_group_id": group,
            "role": role,
            "captured": True,
            "outcome": "heard_correct_driver",
        }
    payload = {
        "verdict": "present" if recorded else "silent",
        "outcome": "heard_correct_driver" if recorded else None,
        "recorded": recorded,
        "skipped_reason": None if recorded else "silent",
        "passband_hz": [40.0, 400.0],
        "acoustic": {
            "kind": "jts_active_speaker_driver_acoustics",
            "verdict": "present" if recorded else "silent",
        },
        "excitation": {
            "schema_version": 1,
            "scope": "sweep_plus_role_varying_commission_gain",
        },
        "placement_proof": {
            "schema_version": 1,
            "policy_id": "driver_same_distance_v1",
        },
        "measurement": measurement,
    }
    if not recorded:
        # A skipped capture has no nested measurement record; the caller
        # (web_measurement.py) enriches the payload with the top-level
        # identity it already has in scope so append_capture can still file
        # the capture under the right group/role.
        payload["speaker_group_id"] = group
        payload["role"] = role
    return payload


def _summed_payload(*, group: str = "mono", fc_hz: float = 2500.0) -> dict:
    return {
        "verdict": "blend_ok",
        "outcome": "blend_ok",
        "recorded": True,
        "skipped_reason": None,
        "crossover_fc_hz": fc_hz,
        "acoustic": {
            "kind": "jts_active_speaker_summed_acoustics",
            "verdict": "blend_ok",
        },
        "excitation": {
            "schema_version": 1,
            "scope": "sweep_plus_applied_full_layer_a_graph",
        },
        "placement_proof": {
            "schema_version": 1,
            "policy_id": "summed_listening_position_v1",
        },
        "measurement": {
            "validation_id": "val-1",
            "speaker_group_id": group,
            "validated": True,
        },
    }


def _write_wav(path: Path, *, size: int = 64) -> Path:
    path.write_bytes(b"\x00" * size)
    return path


# --------------------------------------------------------------------------
# open_bundle
# --------------------------------------------------------------------------


def test_open_bundle_writes_every_required_info_field(tmp_path: Path) -> None:
    topology = _topology()
    info = _open(tmp_path, topology=topology, calibration_id="mic-1", now=1000.0)

    assert info is not None
    assert info["bundle_schema_version"] == bundles.BUNDLE_SCHEMA_VERSION
    assert info["kind"] == bundles.BUNDLE_KIND
    assert info["session_id"]
    assert len(info["session_id"]) == 12
    assert info["started_at"] == 1000.0
    assert info["state"] == "open"
    assert info["bundle_dir"] == str(tmp_path / info["session_id"])

    fp = info["fingerprints"]
    assert fp["topology_id"] == topology.topology_id
    assert isinstance(fp["topology_fingerprint"], str) and fp["topology_fingerprint"]
    assert fp["graph_fingerprint"] is None
    assert fp["output_assignments"] == [
        {"group_id": "mono", "role": "woofer", "physical_output_index": 0},
        {"group_id": "mono", "role": "tweeter", "physical_output_index": 1},
    ]
    assert fp["mic"] == {"calibration_id": "mic-1", "calibration_sha256": None}
    assert fp["comparison_set_fingerprint"] is None
    assert fp["build_sha"] is None or isinstance(fp["build_sha"], str)

    assert info["placement"] == {
        "policy_id": "driver_same_distance_v1",
        "acknowledged": False,
    }
    assert info["captures"] == []
    assert info["summed_captures"] == []
    assert info["repeat_progress"] == {}
    for reserved in (
        "proposal",
        "previous_values",
        "proposed_values",
        "corrections_provenance",
        "compile_validation",
        "apply",
        "rollback_target",
        "verification",
    ):
        assert info[reserved] is None

    # Persisted to disk, not just returned in-memory.
    on_disk = bundles._read_info(Path(info["bundle_dir"]))
    assert on_disk["session_id"] == info["session_id"]

    # Umask-proof: the bundle dir is explicitly chmod'd 0o750, not left at
    # whatever mode the writing daemon's umask happens to yield.
    assert Path(info["bundle_dir"]).stat().st_mode & 0o777 == 0o750


def test_open_bundle_info_json_is_a_manifest_artifact(tmp_path: Path) -> None:
    info = _open(tmp_path, calibration_id="")
    bundle_dir = Path(info["bundle_dir"])

    manifest = read_artifact_manifest(bundle_dir)
    assert manifest["bundle_schema_version"] == bundles.BUNDLE_SCHEMA_VERSION
    assert manifest["bundle_schema_version"] == info["bundle_schema_version"]
    paths = {entry["path"] for entry in manifest["artifacts"]}
    assert "info.json" in paths
    entry = next(e for e in manifest["artifacts"] if e["path"] == "info.json")
    assert entry["kind"] == "metadata"
    assert entry["sensitivity"] == "config"
    assert len(entry["sha256"]) == 64
    assert entry["byte_size"] == (bundle_dir / "info.json").stat().st_size


def test_new_bundle_is_reopenable_production_admission_authority(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])

    first = bundles.open_bundle_admission_authority(
        bundle_dir,
        expected_session_id=info["session_id"],
    )
    second = bundles.open_bundle_admission_authority(
        bundle_dir,
        expected_session_id=info["session_id"],
    )

    assert first == second
    assert first.directory == bundle_dir
    assert first.marker.relative_path == ADMISSION_AUTHORITY_MARKER


def test_historical_bundle_without_marker_is_never_upgraded(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    marker = bundle_dir / ADMISSION_AUTHORITY_MARKER
    marker.unlink()

    with pytest.raises(AdmissionArtifactError) as raised:
        bundles.open_bundle_admission_authority(
            bundle_dir,
            expected_session_id=info["session_id"],
        )

    assert raised.value.code is AdmissionArtifactErrorCode.AUTHORITY_MISSING
    assert not marker.exists()


def test_open_bundle_mints_a_fresh_session_each_call(tmp_path: Path) -> None:
    first = _open(tmp_path)
    second = _open(tmp_path)
    assert first["session_id"] != second["session_id"]


def test_open_bundle_marks_prior_open_bundle_abandoned(tmp_path: Path) -> None:
    first = _open(tmp_path, now=1000.0)
    second = _open(tmp_path, now=2000.0)

    reloaded_first = bundles._read_info(Path(first["bundle_dir"]))
    assert reloaded_first["state"] == "abandoned"
    reloaded_second = bundles._read_info(Path(second["bundle_dir"]))
    assert reloaded_second["state"] == "open"


def test_open_bundle_does_not_abandon_already_applied_bundles(
    tmp_path: Path,
) -> None:
    first = _open(tmp_path, now=1000.0)
    bundles.mark_state(Path(first["bundle_dir"]), "applied")

    _open(tmp_path, now=2000.0)

    reloaded_first = bundles._read_info(Path(first["bundle_dir"]))
    assert reloaded_first["state"] == "applied"


def test_open_bundle_uses_env_sessions_dir_when_no_override(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path))
    info = bundles.open_bundle(_topology(), calibration_id="")
    assert info is not None
    assert Path(info["bundle_dir"]).parent == tmp_path


def test_open_bundle_prefers_explicit_calibration_sha_over_lookup(
    tmp_path: Path,
) -> None:
    info = _open(
        tmp_path,
        calibration_id="mic-1",
        mic_calibration_sha256="deadbeef" * 8,
    )
    assert info["fingerprints"]["mic"]["calibration_sha256"] == "deadbeef" * 8


def test_open_bundle_returns_none_and_warns_on_write_failure(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    # A directory permission failure surfaces as an OSError from
    # write_json_artifact -> record_artifact's mkdir/stat; the fail-soft
    # wrapper must swallow it (never blocking the comparison-set flow) and
    # log a WARN event instead of raising.
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir(mode=0o500)
    try:
        with caplog.at_level(logging.WARNING):
            result = bundles.open_bundle(
                _topology(), calibration_id="", sessions_dir=sessions_root
            )
        assert result is None
        assert "event=active_speaker.bundle_write_failed" in caplog.text
        assert "op=open_bundle" in caplog.text
    finally:
        sessions_root.chmod(0o700)


# --------------------------------------------------------------------------
# attach_comparison_set / mark_state
# --------------------------------------------------------------------------


def test_attach_comparison_set_backfills_fingerprint(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])

    updated = bundles.attach_comparison_set(
        bundle_dir,
        comparison_set_id="cs-1",
        comparison_set_fingerprint="f" * 64,
    )

    assert updated["fingerprints"]["comparison_set_id"] == "cs-1"
    assert updated["fingerprints"]["comparison_set_fingerprint"] == "f" * 64
    reloaded = bundles._read_info(bundle_dir)
    assert reloaded["fingerprints"]["comparison_set_fingerprint"] == "f" * 64


def test_attach_comparison_set_is_fail_soft_for_missing_bundle(
    tmp_path: Path, caplog
) -> None:
    with caplog.at_level(logging.WARNING):
        result = bundles.attach_comparison_set(
            tmp_path / "does-not-exist",
            comparison_set_id="cs-1",
            comparison_set_fingerprint="f" * 64,
        )
    assert result is None
    assert "event=active_speaker.bundle_write_failed" in caplog.text


def test_mark_state_validates_enum(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])

    updated = bundles.mark_state(bundle_dir, "proposal_ready")
    assert updated["state"] == "proposal_ready"

    with_bad_state = bundles.mark_state(bundle_dir, "not_a_real_state")
    assert with_bad_state is None
    assert bundles._read_info(bundle_dir)["state"] == "proposal_ready"


# --------------------------------------------------------------------------
# append_capture
# --------------------------------------------------------------------------


def test_append_capture_records_wav_and_json_with_dependencies(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "src.wav")
    payload = _driver_payload()

    entry = bundles.append_capture(
        bundle_dir, kind="driver", wav_source_path=wav, payload=payload
    )

    assert entry is not None
    wav_path = bundle_dir / entry["artifact_path"]
    assert wav_path.is_file()
    assert wav_path.read_bytes() == wav.read_bytes()
    json_path = bundle_dir / entry["capture_json_path"]
    assert json_path.is_file()

    # Umask-proof: the captures/ subdir is explicitly chmod'd 0o750.
    assert wav_path.parent.stat().st_mode & 0o777 == 0o750

    manifest = read_artifact_manifest(bundle_dir)
    assert manifest["bundle_schema_version"] == bundles.BUNDLE_SCHEMA_VERSION
    by_path = {a["path"]: a for a in manifest["artifacts"]}
    assert entry["artifact_path"] in by_path
    wav_entry = by_path[entry["artifact_path"]]
    assert wav_entry["kind"] == "capture_wav"
    assert wav_entry["sensitivity"] == "private_raw_audio"
    assert wav_entry["byte_size"] == wav_path.stat().st_size
    assert len(wav_entry["sha256"]) == 64

    json_entry = by_path[entry["capture_json_path"]]
    assert json_entry["kind"] == "capture_analysis"
    assert json_entry["sensitivity"] == "derived"
    assert json_entry["dependencies"] == [entry["artifact_path"]]


def test_append_capture_appends_compact_entry_with_driver_role(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "src.wav")
    payload = _driver_payload(group="mono", role="woofer")
    payload["placement_proof"]["accepted"] = True

    entry = bundles.append_capture(
        bundle_dir, kind="driver", wav_source_path=wav, payload=payload
    )

    reloaded = bundles._read_info(bundle_dir)
    assert reloaded["captures"] == [entry]
    assert reloaded["summed_captures"] == []
    assert entry["group"] == "mono"
    assert entry["role"] == "woofer"
    assert entry["verdict"] == "present"
    assert entry["outcome"] == "heard_correct_driver"
    assert entry["quality"] == payload["acoustic"]
    assert entry["excitation"] == payload["excitation"]
    assert entry["placement_ack"] == payload["placement_proof"]
    assert entry["measurement_id"] == "meas-1"
    assert reloaded["placement"]["acknowledged"] is True


def test_append_capture_does_not_acknowledge_unaccepted_or_wrong_policy_proof(
    tmp_path: Path,
) -> None:
    for suffix, accepted, policy in (
        ("unaccepted", False, "driver_same_distance_v1"),
        ("wrong-policy", True, "summed_listening_position_v1"),
    ):
        root = tmp_path / suffix
        info = _open(root)
        bundle_dir = Path(info["bundle_dir"])
        payload = _driver_payload(group="mono", role="woofer")
        payload["placement_proof"].update(
            {
                "accepted": accepted,
                "policy_id": policy,
            }
        )
        bundles.append_capture(
            bundle_dir,
            kind="driver",
            wav_source_path=_write_wav(root / "src.wav"),
            payload=payload,
        )

        assert bundles._read_info(bundle_dir)["placement"]["acknowledged"] is False


def test_append_capture_summed_kind_omits_role_and_carries_fc(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "summed.wav")
    payload = _summed_payload(fc_hz=2500.0)

    entry = bundles.append_capture(
        bundle_dir, kind="summed", wav_source_path=wav, payload=payload
    )

    reloaded = bundles._read_info(bundle_dir)
    assert reloaded["summed_captures"] == [entry]
    assert reloaded["captures"] == []
    assert "role" not in entry
    assert entry["crossover_fc_hz"] == 2500.0
    assert entry["measurement_id"] == "val-1"
    assert entry["artifact_path"].startswith("summed/")


def test_append_capture_resolves_group_role_from_top_level_when_unrecorded(
    tmp_path: Path,
) -> None:
    """A skipped (recorded=False) capture has no nested measurement record;
    append_capture must still file it under the caller-supplied identity."""

    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "src.wav")
    payload = _driver_payload(group="mono", role="tweeter", recorded=False)
    assert payload["measurement"] is None

    entry = bundles.append_capture(
        bundle_dir, kind="driver", wav_source_path=wav, payload=payload
    )

    assert entry is not None
    assert entry["group"] == "mono"
    assert entry["role"] == "tweeter"
    assert entry["outcome"] is None
    assert entry["measurement_id"] is None


def test_append_capture_uses_relative_path_when_given(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "src.wav")
    payload = _driver_payload()

    entry = bundles.append_capture(
        bundle_dir,
        kind="driver",
        wav_source_path=wav,
        payload=payload,
        relative_path="captures/pre_minted_name.wav",
    )

    assert entry["artifact_path"] == "captures/pre_minted_name.wav"
    assert (bundle_dir / "captures" / "pre_minted_name.wav").is_file()


def test_append_capture_copies_never_moves_source(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "src.wav")

    bundles.append_capture(
        bundle_dir, kind="driver", wav_source_path=wav, payload=_driver_payload()
    )

    assert wav.exists()  # source untouched — web_measurement owns its own retention


def test_append_capture_rejects_missing_source(tmp_path: Path, caplog) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])

    with caplog.at_level(logging.WARNING):
        entry = bundles.append_capture(
            bundle_dir,
            kind="driver",
            wav_source_path=tmp_path / "missing.wav",
            payload=_driver_payload(),
        )

    assert entry is None
    assert "event=active_speaker.bundle_write_failed" in caplog.text
    reloaded = bundles._read_info(bundle_dir)
    assert reloaded["captures"] == []


def test_append_capture_rejects_oversized_source(tmp_path: Path, caplog) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "huge.wav", size=bundles.MAX_CAPTURE_WAV_BYTES + 1)

    with caplog.at_level(logging.WARNING):
        entry = bundles.append_capture(
            bundle_dir, kind="driver", wav_source_path=wav, payload=_driver_payload()
        )

    assert entry is None
    assert "event=active_speaker.bundle_write_failed" in caplog.text


def test_max_capture_wav_bytes_matches_web_measurement_cap() -> None:
    """bundles.py hand-mirrors web_measurement's browser-capture-store cap
    (a bundle copy is never larger than the capture it was made from). This
    is a mirror-drift pin: it catches the two constants silently diverging,
    not a functional dependency (bundles.py does not import web_measurement
    at module load, to stay light and cycle-free)."""

    from jasper.active_speaker import web_measurement

    assert bundles.MAX_CAPTURE_WAV_BYTES == web_measurement.MAX_CAPTURE_WAV_BYTES


def test_append_capture_rejects_source_that_is_not_a_filesystem_path(
    tmp_path: Path, caplog
) -> None:
    """A wav_source_path that Path() itself cannot construct (e.g. a caller
    bug passing None) must fail soft exactly like the missing/oversized
    guard, never raise TypeError out of append_capture."""

    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])

    with caplog.at_level(logging.WARNING):
        entry = bundles.append_capture(
            bundle_dir,
            kind="driver",
            wav_source_path=None,
            relative_path="captures/x.wav",
            payload={},
        )

    assert entry is None
    assert "event=active_speaker.bundle_write_failed" in caplog.text
    assert "op=append_capture" in caplog.text
    manifest = read_artifact_manifest(bundle_dir)
    assert not any(a["path"] == "captures/x.wav" for a in manifest["artifacts"])
    assert not (bundle_dir / "captures" / "x.wav").exists()


def test_append_capture_rejects_unsupported_kind(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "src.wav")

    result = bundles.append_capture(
        bundle_dir, kind="bogus", wav_source_path=wav, payload=_driver_payload()
    )
    assert result is None


def test_append_capture_is_fail_soft_when_info_json_is_missing(
    tmp_path: Path, caplog
) -> None:
    """The pinned promise: a bundle-dir write failure never blocks the
    capture path. Here the bundle directory exists (so the WAV copy and its
    manifest entry succeed) but has no info.json — append_capture must still
    return None + WARN, mid-write, rather than raise once it reaches the
    info.json read/rewrite step."""

    bundle_dir = tmp_path / "partial-bundle"
    bundle_dir.mkdir()
    wav = _write_wav(tmp_path / "src.wav")

    with caplog.at_level(logging.WARNING):
        result = bundles.append_capture(
            bundle_dir, kind="driver", wav_source_path=wav, payload=_driver_payload()
        )

    assert result is None
    assert "event=active_speaker.bundle_write_failed" in caplog.text
    assert "op=append_capture" in caplog.text
    # The WAV copy ran to completion before the info.json step failed.
    assert list(bundle_dir.glob("captures/*.wav"))


# --------------------------------------------------------------------------
# record_apply
# --------------------------------------------------------------------------


def _candidate(*, status: str = "applied", fingerprint: str = "cand-fp") -> dict:
    return {
        "status": status,
        "source": {"fingerprint": fingerprint, "topology_fingerprint": "topo-fp"},
        "proposal": {"note": "example"},
        "corrections_provenance": {
            "woofer": {
                "gain_db": "measured",
                "delay_ms": "manual",
                "inverted": "manual",
            }
        },
        "validation": {"status": "valid", "ok_to_apply": True},
    }


def test_record_apply_marks_applied_on_success(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    candidate = _candidate(status="applied")
    apply_state = {"result": "ok", "op_id": "abc"}

    updated = bundles.record_apply(
        bundle_dir,
        candidate=candidate,
        apply_state=apply_state,
        rollback_target={"config_path": "/prior/config.yml"},
    )

    assert updated["state"] == "applied"
    assert updated["fingerprints"]["graph_fingerprint"] == "cand-fp"
    assert updated["proposal"] == {"note": "example"}
    assert updated["corrections_provenance"] == candidate["corrections_provenance"]
    assert updated["compile_validation"] == candidate["validation"]
    assert updated["apply"] == apply_state
    assert updated["rollback_target"] == {"config_path": "/prior/config.yml"}
    assert (bundle_dir / "proposal.json").is_file()
    assert (bundle_dir / "apply.json").is_file()


def test_record_apply_marks_failed_when_apply_state_missing(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    candidate = _candidate(status="blocked")

    updated = bundles.record_apply(
        bundle_dir, candidate=candidate, apply_state=None, rollback_target=None
    )

    assert updated["state"] == "failed"
    assert updated["apply"] is None
    assert (bundle_dir / "proposal.json").is_file()
    assert not (bundle_dir / "apply.json").exists()


def test_record_apply_marks_failed_when_status_not_applied(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    candidate = _candidate(status="apply_failed")
    apply_state = {"result": "load_failed"}

    updated = bundles.record_apply(
        bundle_dir, candidate=candidate, apply_state=apply_state, rollback_target=None
    )

    assert updated["state"] == "failed"
    assert updated["apply"] == apply_state


def test_record_apply_never_overwrites_an_existing_graph_fingerprint(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    bundles.record_apply(
        bundle_dir,
        candidate=_candidate(status="applied", fingerprint="first-fp"),
        apply_state={"result": "ok"},
        rollback_target=None,
    )

    updated = bundles.record_apply(
        bundle_dir,
        candidate=_candidate(status="applied", fingerprint="second-fp"),
        apply_state={"result": "ok"},
        rollback_target=None,
    )

    assert updated["fingerprints"]["graph_fingerprint"] == "first-fp"


# --------------------------------------------------------------------------
# list_bundles / summarize_bundle / latest_bundle
# --------------------------------------------------------------------------


def test_list_bundles_sorts_newest_first_and_skips_bad_json(
    tmp_path: Path,
) -> None:
    _open(tmp_path, now=1000.0)
    newest = _open(tmp_path, now=2000.0)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "info.json").write_text("not json")

    found = bundles.list_bundles(tmp_path)

    assert [b["session_id"] for b in found][0] == newest["session_id"]
    assert len(found) == 2  # the malformed dir is skipped


def test_list_bundles_skips_directory_with_no_info_json(tmp_path: Path) -> None:
    _open(tmp_path)
    (tmp_path / "empty-dir").mkdir()

    found = bundles.list_bundles(tmp_path)

    assert len(found) == 1


def test_list_bundles_limits_result_count(tmp_path: Path) -> None:
    for i in range(3):
        _open(tmp_path, now=float(i))

    found = bundles.list_bundles(tmp_path, limit=1)

    assert len(found) == 1


def test_list_bundles_treats_missing_sessions_dir_as_empty(
    tmp_path: Path,
) -> None:
    assert bundles.list_bundles(tmp_path / "missing") == []
    assert bundles.latest_bundle(tmp_path / "missing") is None


def test_summarize_bundle_reports_counts_and_size(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "src.wav")
    bundles.append_capture(
        bundle_dir, kind="driver", wav_source_path=wav, payload=_driver_payload()
    )

    summary = bundles.summarize_bundle(bundle_dir)

    assert summary["capture_count"] == 1
    assert summary["summed_capture_count"] == 0
    assert summary["bundle_size_bytes"] > 0
    assert summary["has_artifact_manifest"] is True
    assert summary["artifact_count"] >= 2  # info.json + the WAV (+ its JSON)


def test_summarize_bundle_raises_for_non_directory(tmp_path: Path) -> None:
    from jasper.correction.bundles import BundleError

    with pytest.raises(BundleError):
        bundles.summarize_bundle(tmp_path / "nope")


def test_latest_bundle_returns_the_newest(tmp_path: Path) -> None:
    _open(tmp_path, now=1000.0)
    newest = _open(tmp_path, now=5000.0)

    found = bundles.latest_bundle(tmp_path)

    assert found is not None
    assert found["session_id"] == newest["session_id"]


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------


def test_enforce_retention_deletes_oldest_first_by_started_at(
    tmp_path: Path,
) -> None:
    oldest = _open(tmp_path, now=1000.0)
    bundles.mark_state(Path(oldest["bundle_dir"]), "applied")
    middle = _open(tmp_path, now=2000.0)
    bundles.mark_state(Path(middle["bundle_dir"]), "applied")
    newest = _open(tmp_path, now=3000.0)
    bundles.mark_state(Path(newest["bundle_dir"]), "applied")

    bundles.enforce_retention(tmp_path, max_bytes=10**9, max_bundles=2)

    assert not Path(oldest["bundle_dir"]).exists()
    assert Path(middle["bundle_dir"]).exists()
    assert Path(newest["bundle_dir"]).exists()


def test_enforce_retention_counts_and_deletes_partial_authority_dirs(
    tmp_path: Path,
) -> None:
    partial = tmp_path / "partial-session"
    create_admission_authority(
        partial,
        bundle_kind=bundles.BUNDLE_KIND,
        bundle_id=partial.name,
    )
    assert not (partial / "info.json").exists()

    bundles.enforce_retention(tmp_path, max_bytes=0, max_bundles=0)

    assert not partial.exists()


def test_enforce_retention_never_evicts_the_open_session(
    tmp_path: Path,
) -> None:
    old_applied = _open(tmp_path, now=1000.0)
    bundles.mark_state(Path(old_applied["bundle_dir"]), "applied")
    still_open = _open(tmp_path, now=500.0)  # older by timestamp, but OPEN

    # Aggressive cap that would otherwise evict everything.
    bundles.enforce_retention(tmp_path, max_bytes=1, max_bundles=1)

    assert Path(still_open["bundle_dir"]).exists()
    reloaded = bundles._read_info(Path(still_open["bundle_dir"]))
    assert reloaded["state"] == "open"


def test_enforce_retention_protects_the_single_newest_bundle(
    tmp_path: Path,
) -> None:
    older = _open(tmp_path, now=1000.0)
    bundles.mark_state(Path(older["bundle_dir"]), "applied")
    newest = _open(tmp_path, now=2000.0)
    bundles.mark_state(Path(newest["bundle_dir"]), "applied")

    bundles.enforce_retention(tmp_path, max_bytes=1, max_bundles=1)

    assert not Path(older["bundle_dir"]).exists()
    assert Path(newest["bundle_dir"]).exists()


def test_enforce_retention_respects_env_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BUNDLES", "1")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BYTES", str(10**9))
    old = _open(tmp_path, now=1000.0)
    bundles.mark_state(Path(old["bundle_dir"]), "applied")
    newest = _open(tmp_path, now=2000.0)
    bundles.mark_state(Path(newest["bundle_dir"]), "applied")

    bundles.enforce_retention(tmp_path)

    assert not Path(old["bundle_dir"]).exists()
    assert Path(newest["bundle_dir"]).exists()


def test_enforce_retention_is_fail_soft(tmp_path: Path, caplog, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(bundles, "_iter_retention_dirs", boom)

    with caplog.at_level(logging.WARNING):
        bundles.enforce_retention(tmp_path)  # must not raise

    assert "event=active_speaker.bundle_write_failed" in caplog.text
    assert "op=enforce_retention" in caplog.text


def test_env_int_falls_back_on_invalid_or_non_positive(monkeypatch) -> None:
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BUNDLES", "not-a-number")
    assert bundles._sessions_max_bundles() == bundles.DEFAULT_SESSIONS_MAX_BUNDLES

    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BUNDLES", "0")
    assert bundles._sessions_max_bundles() == bundles.DEFAULT_SESSIONS_MAX_BUNDLES

    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BUNDLES", "-4")
    assert bundles._sessions_max_bundles() == bundles.DEFAULT_SESSIONS_MAX_BUNDLES

    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BUNDLES", "7")
    assert bundles._sessions_max_bundles() == 7


# --------------------------------------------------------------------------
# capture_artifact_relpath
# --------------------------------------------------------------------------


def test_capture_artifact_relpath_shape_for_driver_and_summed() -> None:
    driver_path = bundles.capture_artifact_relpath("driver", "mono", "woofer")
    assert driver_path.startswith("captures/driver_mono_woofer_")
    assert driver_path.endswith(".wav")

    summed_path = bundles.capture_artifact_relpath("summed", "mono", None)
    assert summed_path.startswith("summed/summed_mono_")
    assert "_none_" not in summed_path


def test_capture_artifact_relpath_is_unique_per_call() -> None:
    a = bundles.capture_artifact_relpath("driver", "mono", "woofer")
    b = bundles.capture_artifact_relpath("driver", "mono", "woofer")
    assert a != b


# --------------------------------------------------------------------------
# integration: commissioning_capture's bundle_ref pass-through
# --------------------------------------------------------------------------


def test_record_driver_acoustic_capture_threads_bundle_ref_onto_the_record(
    tmp_path: Path,
) -> None:
    """commissioning_capture.record_driver_acoustic_capture forwards
    bundle_ref verbatim to the measurement record — the join key a bundle
    reader uses to associate durable evidence with the session it came
    from. This is a pure pass-through: it must never depend on bundles.py
    or fail because of it."""

    from jasper.active_speaker.commissioning_capture import (
        record_driver_acoustic_capture,
    )
    from jasper.active_speaker.driver_acoustics import (
        VERDICT_PRESENT,
        DriverAcousticResult,
    )

    topology = _topology()

    def fake_analyze(*_args, **_kwargs) -> DriverAcousticResult:
        return DriverAcousticResult(
            verdict=VERDICT_PRESENT,
            present=True,
            observed_mic_dbfs=-30.0,
            peak_dbfs=-20.0,
            in_band_db=-10.0,
            out_of_band_db=-25.0,
            band_separation_db=15.0,
            passband_hz=(40.0, 400.0),
            mic_clipping=False,
            quality={},
        )

    seen_kwargs: dict = {}

    def fake_record(_topology, raw, **kwargs) -> dict:
        seen_kwargs.update(kwargs)
        return {"recorded": True, "bundle": kwargs.get("bundle_ref")}

    from jasper.active_speaker.tone_plan import load_active_speaker_preset

    preset = load_active_speaker_preset(None)

    bundle_ref = {"session_id": "abc123", "artifact_path": "captures/x.wav"}
    payload = record_driver_acoustic_capture(
        topology,
        preset,
        speaker_group_id="mono",
        role="woofer",
        captured_wav=tmp_path / "unused.wav",
        sweep_meta={"amplitude_dbfs": -18.0},
        test_level_dbfs=0.0,
        analyze=fake_analyze,
        record=fake_record,
        bundle_ref=bundle_ref,
    )

    assert payload["recorded"] is True
    assert seen_kwargs.get("bundle_ref") == bundle_ref


# --------------------------------------------------------------------------
# Step 2: repeat captures — repeat_captures/ manifest entries
# --------------------------------------------------------------------------


def test_append_repeat_capture_records_wav_and_json_under_repeat_captures(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "repeat.wav")
    payload = {
        "verdict": "present",
        "acoustic": {"observed_mic_dbfs": -30.0, "mic_clipping": False},
    }

    entry = bundles.append_repeat_capture(
        bundle_dir, index=0, wav_source_path=wav, payload=payload
    )

    assert entry is not None
    assert entry["artifact_path"].startswith("repeat_captures/")
    wav_path = bundle_dir / entry["artifact_path"]
    assert wav_path.is_file()
    assert wav_path.read_bytes() == wav.read_bytes()
    json_path = bundle_dir / entry["quality_json_path"]
    assert json_path.is_file()

    manifest = read_artifact_manifest(bundle_dir)
    by_path = {a["path"]: a for a in manifest["artifacts"]}
    assert entry["artifact_path"] in by_path
    assert by_path[entry["artifact_path"]]["kind"] == "capture_wav"
    assert by_path[entry["artifact_path"]]["sensitivity"] == "private_raw_audio"
    json_entry = by_path[entry["quality_json_path"]]
    assert json_entry["kind"] == "repeat_capture_analysis"
    assert json_entry["dependencies"] == [entry["artifact_path"]]

    # A repeat capture is NOT added to info.json's captures/summed_captures
    # compact lists -- it's raw evidence only, indexed via the winning
    # capture's aggregate_driver_repeats per_repeat[] array instead.
    reloaded = bundles._read_info(bundle_dir)
    assert reloaded["captures"] == []
    assert reloaded["summed_captures"] == []


def test_append_repeat_capture_uses_relative_path_when_given(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    wav = _write_wav(tmp_path / "repeat.wav")

    entry = bundles.append_repeat_capture(
        bundle_dir,
        index=2,
        wav_source_path=wav,
        payload={"verdict": "present"},
        relative_path="repeat_captures/pre_minted.wav",
    )

    assert entry["artifact_path"] == "repeat_captures/pre_minted.wav"
    assert (bundle_dir / "repeat_captures" / "pre_minted.wav").is_file()


def test_append_repeat_capture_multiple_attempts_coexist(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    paths = set()
    for index in range(3):
        wav = _write_wav(tmp_path / f"repeat_{index}.wav", size=32 + index)
        entry = bundles.append_repeat_capture(
            bundle_dir,
            index=index,
            wav_source_path=wav,
            payload={"verdict": "present", "index": index},
        )
        assert entry is not None
        paths.add(entry["artifact_path"])
    assert len(paths) == 3  # every attempt gets a distinct file
    manifest = read_artifact_manifest(bundle_dir)
    repeat_entries = [
        a for a in manifest["artifacts"] if a["path"].startswith("repeat_captures/")
    ]
    # 3 WAVs + 3 quality JSONs.
    assert len(repeat_entries) == 6


def test_repeat_progress_is_compact_bounded_and_durable(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    per_repeat = [
        {
            "index": index,
            "accepted": index != 2,
            "reject_reason": "level_outlier" if index == 2 else None,
            "artifact_path": f"repeat_captures/{index}.wav",
            "estimated_snr_db": 31.0 + index,
            "clipping": False,
            "above_validity_floor": True,
            "level_dbfs": -30.0 + index / 10,
            "full_acoustic_curve_must_not_be_copied": [1, 2, 3],
        }
        for index in range(5)
    ]

    entry = bundles.record_repeat_progress(
        bundle_dir,
        comparison_set_id="c" * 32,
        target_fingerprint="driver-fp",
        target_id="mono:woofer",
        attempts=4,
        accepted=3,
        target=3,
        per_repeat=per_repeat,
        status="active",
    )

    assert entry is not None
    assert entry["attempts"] == 4
    assert len(entry["per_repeat"]) == 4
    assert all(
        "full_acoustic_curve_must_not_be_copied" not in repeat
        for repeat in entry["per_repeat"]
    )
    reloaded = bundles._read_info(bundle_dir)["repeat_progress"]["mono:woofer"]
    assert reloaded == entry


def test_append_repeat_capture_rejects_missing_source(tmp_path: Path, caplog) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])

    with caplog.at_level(logging.WARNING):
        entry = bundles.append_repeat_capture(
            bundle_dir,
            index=0,
            wav_source_path=tmp_path / "missing.wav",
            payload={"verdict": "present"},
        )

    assert entry is None
    assert "event=active_speaker.bundle_write_failed" in caplog.text
    assert "op=append_repeat_capture" in caplog.text


def test_append_repeat_capture_is_fail_soft_when_info_json_is_missing(
    tmp_path: Path, caplog
) -> None:
    # append_repeat_capture never touches info.json, but the WAV copy step
    # itself must still degrade gracefully rather than raise if the bundle
    # directory disappears mid-write.
    bundle_dir = tmp_path / "partial-bundle"
    bundle_dir.mkdir()
    wav = _write_wav(tmp_path / "repeat.wav")

    entry = bundles.append_repeat_capture(
        bundle_dir, index=0, wav_source_path=wav, payload={"verdict": "present"}
    )

    # No info.json requirement -- this succeeds even without an opened
    # bundle, since repeat evidence has no compact list to update.
    assert entry is not None
    assert (
        bundles.read_artifact_manifest(bundle_dir)["bundle_schema_version"]
        == bundles.LEGACY_PARTIAL_BUNDLE_SCHEMA_VERSION
        == 5
    )
    assert (bundle_dir / entry["artifact_path"]).is_file()


def test_repeat_captures_count_toward_bundle_size_and_retention(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    bundles.append_repeat_capture(
        bundle_dir,
        index=0,
        wav_source_path=_write_wav(tmp_path / "r.wav", size=256),
        payload={"verdict": "present"},
    )

    summary = bundles.summarize_bundle(bundle_dir)

    assert summary["bundle_size_bytes"] >= 256


def test_record_driver_repeat_aggregate_event_fields_match_bundle_promise(
    caplog,
) -> None:
    """Cross-module pin: commissioning_capture.record_driver_repeat_aggregate
    (the lane D step-2 emitter per SC-5) logs the exact field set the
    bundle/SC-5 contract requires: session, group, role, accepted, rejected,
    spread."""

    from jasper.active_speaker.commissioning_capture import (
        record_driver_repeat_aggregate,
    )

    repeats = [
        {
            "verdict": "present",
            "acoustic": {"observed_mic_dbfs": level, "mic_clipping": False},
        }
        for level in (-30.0, -30.1, -29.9)
    ]

    with caplog.at_level(logging.INFO):
        record_driver_repeat_aggregate(
            speaker_group_id="mono",
            role="tweeter",
            repeats=repeats,
            session_id="sess-9",
        )

    assert "event=correction.crossover_repeats_aggregated" in caplog.text
    for expected in (
        "session=sess-9",
        "group=mono",
        "role=tweeter",
        "accepted=3",
        "rejected=0",
    ):
        assert expected in caplog.text
