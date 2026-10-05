# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the active-speaker commissioning bundle (jasper/active_speaker/bundles.py).

Covers: info.json's required fields, artifact manifest mechanics (owned by
jasper.audio_measurement.bundles), the capture/apply write paths, retention,
and the fail-soft contract — a bundle write failure must never block the
capture/apply path recording it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jasper.active_speaker import bundles
from jasper.audio_measurement.admission.excitation_artifacts import (
    ADMISSION_AUTHORITY_MARKER,
    AdmissionArtifactError,
    AdmissionArtifactErrorCode,
    create_admission_authority,
)
from jasper.audio_measurement.bundles import read_artifact_manifest
from tests._log_events import event_fields
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


def _register(
    bundle_dir: Path,
    *,
    kind: str,
    payload: dict,
    relative_path: str | None = None,
    wav_bytes: int = 64,
) -> dict | None:
    """Write a WAV into the bundle, as a wired take does, then register it."""

    relative = relative_path or bundles.capture_artifact_relpath(kind, "mono", None)
    wav = bundle_dir / relative
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"\x00" * wav_bytes)
    return bundles.register_capture(
        bundle_dir, kind=kind, relative_path=relative, payload=payload
    )


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
    assert info["summed_captures"] == []
    assert info["verification"] is None

    # Persisted to disk, not just returned in-memory.
    on_disk = bundles._read_info(Path(info["bundle_dir"]))
    assert on_disk["session_id"] == info["session_id"]

    assert Path(info["bundle_dir"]).stat().st_mode & 0o7777 == 0o750


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
        fields = event_fields(caplog, "active_speaker.bundle_write_failed")
        assert fields["op"] == "open_bundle"
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
    event_fields(caplog, "active_speaker.bundle_write_failed")


@pytest.mark.parametrize("state", ["proposal_ready", "closed"])
def test_mark_state_validates_enum(tmp_path: Path, state: str) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])

    updated = bundles.mark_state(bundle_dir, state)
    assert updated["state"] == state

    with_bad_state = bundles.mark_state(bundle_dir, "not_a_real_state")
    assert with_bad_state is None
    assert bundles._read_info(bundle_dir)["state"] == state


# --------------------------------------------------------------------------
# register_capture
# --------------------------------------------------------------------------


def test_register_capture_records_wav_and_json_with_dependencies(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    relative = "summed/pre_minted_name.wav"

    entry = _register(
        bundle_dir, kind="summed", payload=_summed_payload(), relative_path=relative
    )

    assert entry is not None
    assert entry["artifact_path"] == relative
    wav_path = bundle_dir / entry["artifact_path"]
    json_path = bundle_dir / entry["capture_json_path"]
    assert json_path.is_file()

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


def test_register_capture_appends_compact_entry_to_summed_captures(
    tmp_path: Path,
) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    payload = _summed_payload(fc_hz=2500.0)
    payload["placement_proof"].update(
        {"accepted": True, "policy_id": info["placement"]["policy_id"]}
    )

    entry = _register(bundle_dir, kind="summed", payload=payload)

    reloaded = bundles._read_info(bundle_dir)
    assert reloaded["summed_captures"] == [entry]
    assert entry["kind"] == "summed"
    assert entry["group"] == "mono"
    assert entry["verdict"] == "blend_ok"
    assert entry["outcome"] == "blend_ok"
    assert entry["quality"] == payload["acoustic"]
    assert entry["excitation"] == payload["excitation"]
    assert entry["placement_ack"] == payload["placement_proof"]
    assert entry["measurement_id"] == "val-1"
    assert entry["crossover_fc_hz"] == 2500.0
    assert entry["artifact_path"].startswith("summed/")
    assert reloaded["placement"]["acknowledged"] is True


def test_register_capture_does_not_acknowledge_unaccepted_or_wrong_policy_proof(
    tmp_path: Path,
) -> None:
    for suffix, accepted, policy in (
        ("unaccepted", False, "driver_same_distance_v1"),
        ("wrong-policy", True, "summed_listening_position_v1"),
    ):
        info = _open(tmp_path / suffix)
        bundle_dir = Path(info["bundle_dir"])
        payload = _summed_payload()
        payload["placement_proof"].update(
            {
                "accepted": accepted,
                "policy_id": policy,
            }
        )
        _register(bundle_dir, kind="summed", payload=payload)

        assert bundles._read_info(bundle_dir)["placement"]["acknowledged"] is False


def test_register_capture_resolves_group_from_top_level_without_a_nested_measurement(
    tmp_path: Path,
) -> None:
    """A wired take registers its group at the top level with no nested
    measurement record; register_capture must still file it under that group."""

    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    payload = {
        "speaker_group_id": "mono",
        "phase": "measure",
        "measurement_status": "captured",
    }

    entry = _register(
        bundle_dir, kind=bundles.CAPTURE_KIND_SEQUENTIAL, payload=payload
    )

    assert entry is not None
    assert entry["kind"] == bundles.CAPTURE_KIND_SEQUENTIAL
    assert entry["group"] == "mono"
    assert entry["outcome"] is None
    assert entry["measurement_id"] is None


def test_register_capture_rejects_unsupported_kind(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])

    result = _register(bundle_dir, kind="bogus", payload=_summed_payload())
    assert result is None


def test_register_capture_is_fail_soft_when_info_json_is_missing(
    tmp_path: Path, caplog
) -> None:
    """The pinned promise: a bundle-dir write failure never blocks the
    capture path. Here the bundle directory exists (so the WAV's manifest
    entry succeeds) but has no info.json — register_capture must still
    return None + WARN, mid-write, rather than raise once it reaches the
    sidecar/info.json step."""

    bundle_dir = tmp_path / "partial-bundle"
    bundle_dir.mkdir()

    with caplog.at_level(logging.WARNING):
        result = _register(bundle_dir, kind="summed", payload=_summed_payload())

    assert result is None
    fields = event_fields(caplog, "active_speaker.bundle_write_failed")
    assert fields["op"] == "register_capture"
    assert (
        read_artifact_manifest(bundle_dir)["bundle_schema_version"]
        == bundles.LEGACY_PARTIAL_BUNDLE_SCHEMA_VERSION
        == 5
    )


# --------------------------------------------------------------------------
# list_bundles / summarize_bundle
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


def test_summarize_bundle_reports_counts_and_size(tmp_path: Path) -> None:
    info = _open(tmp_path)
    bundle_dir = Path(info["bundle_dir"])
    _register(
        bundle_dir, kind="summed", payload=_summed_payload(), wav_bytes=256 * 1024
    )

    summary = bundles.summarize_bundle(bundle_dir)

    assert summary["summed_capture_count"] == 1
    assert summary["bundle_size_bytes"] >= 256 * 1024
    assert summary["has_artifact_manifest"] is True
    assert summary["artifact_count"] >= 2  # info.json + the WAV (+ its JSON)


def test_summarize_bundle_raises_for_non_directory(tmp_path: Path) -> None:
    from jasper.audio_measurement.bundles import BundleError

    with pytest.raises(BundleError):
        bundles.summarize_bundle(tmp_path / "nope")


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

    fields = event_fields(caplog, "active_speaker.bundle_write_failed")
    assert fields["op"] == "enforce_retention"


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


def test_capture_artifact_relpath_shape() -> None:
    path = bundles.capture_artifact_relpath("summed", "mono", None)
    assert path.startswith("summed/summed_mono_")
    assert path.endswith(".wav")
    assert "_none_" not in path


def test_capture_artifact_relpath_is_unique_per_call() -> None:
    a = bundles.capture_artifact_relpath("summed", "mono", None)
    b = bundles.capture_artifact_relpath("summed", "mono", None)
    assert a != b
