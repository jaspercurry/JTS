from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from jasper import audio_validation
from jasper.audio_profile_state import MicProbe
from jasper.audio_validation import (
    ArtifactLoadResult,
    ValidationArtifact,
    ValidationArtifactError,
    artifact_age,
    is_artifact_from_future,
    is_artifact_stale,
    load_artifact,
    load_latest_artifact,
    make_artifact,
    parse_artifact_payload,
    write_artifact,
    write_latest_pointer,
)


NOW = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)


def _artifact(**overrides) -> ValidationArtifact:
    values = {
        "validated_at": NOW,
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
        "profile": "xvf_chip_aec",
        "status": "pass",
        "checks": {
            "xvf_profile_readback": "pass",
            "drift_ppm_30m": 0.9,
            "outputd_chip_ref_health": {
                "status": "pass",
                "xruns": 0,
            },
        },
        "recommendation": "chip_aec_viable",
    }
    values.update(overrides)
    return ValidationArtifact(**values)


def test_write_and_load_artifact_round_trip(tmp_path):
    artifact = _artifact(notes=("direct fanout lab pass",))

    path = write_artifact(artifact, directory=tmp_path)
    result = load_artifact(path, now=NOW + timedelta(days=1))

    assert result.state == "loaded"
    assert result.artifact == artifact
    assert result.ok is True
    assert result.has_artifact is True
    assert result.path == path
    assert result.stale is False
    assert path.name.endswith("__xvf3800__apple_usb_c_dongle__xvf_chip_aec__pass.json")
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_latest_pointer_is_convenience_not_timestamped_record(tmp_path):
    artifact = _artifact(notes=("direct fanout lab pass",))

    durable_path = write_artifact(artifact, directory=tmp_path)
    latest_path = write_latest_pointer(artifact, directory=tmp_path)
    latest = load_artifact(latest_path, now=NOW + timedelta(days=1))

    assert durable_path.name != "latest.json"
    assert latest_path.name == "latest.json"
    assert latest.artifact == artifact


def test_write_artifact_sanitizes_filename_components(tmp_path):
    artifact = _artifact(
        mic_id="Seeed XVF3800 / test unit",
        dac_id="../Apple dongle",
        profile="xvf chip aec",
    )

    path = write_artifact(artifact, directory=tmp_path)

    assert path.parent == tmp_path
    assert "/" not in path.name
    assert ".." not in path.name


def test_write_artifact_cleans_temp_file_on_replace_error(tmp_path, monkeypatch):
    artifact = _artifact()

    def fail_replace(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr(audio_validation.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_artifact(artifact, directory=tmp_path)

    assert list(tmp_path.glob("*.tmp")) == []


def test_make_artifact_defaults_timestamp_to_timezone_aware_utc():
    artifact = make_artifact(
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        status="unknown",
        checks={},
        recommendation="run_validation",
    )

    assert artifact.validated_at.tzinfo is not None


def test_load_missing_returns_missing_result(tmp_path):
    result = load_artifact(tmp_path / "missing.json")

    assert result == ArtifactLoadResult(
        state="missing",
        path=tmp_path / "missing.json",
        errors=("artifact file does not exist",),
    )


def test_load_malformed_json_returns_malformed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    result = load_artifact(path)

    assert result.state == "malformed"
    assert result.artifact is None
    assert "invalid JSON" in result.errors[0]


@pytest.mark.parametrize(
    ("payload", "expected_issue"),
    [
        ({}, "schema_version"),
        (
            {
                "schema_version": 1,
                "validated_at": "2026-06-01T12:00:00",
                "hardware": {"mic_id": "xvf3800", "dac_id": "apple"},
                "profile": "xvf_chip_aec",
                "status": "pass",
                "checks": {},
                "recommendation": "ok",
            },
            "validated_at must include a timezone",
        ),
        (
            {
                "schema_version": 1,
                "validated_at": "2026-06-01T12:00:00Z",
                "hardware": {"mic_id": "xvf3800", "dac_id": "apple"},
                "profile": "xvf_chip_aec",
                "status": "maybe",
                "checks": {},
                "recommendation": "ok",
            },
            "status must be one of",
        ),
        (
            {
                "schema_version": 1,
                "validated_at": "2026-06-01T12:00:00Z",
                "hardware": {"mic_id": "xvf3800", "dac_id": "apple"},
                "profile": "xvf_chip_aec",
                "status": "pass",
                "checks": [],
                "recommendation": "ok",
            },
            "checks must be an object",
        ),
    ],
)
def test_parse_artifact_payload_reports_schema_issues(payload, expected_issue):
    with pytest.raises(ValidationArtifactError) as excinfo:
        parse_artifact_payload(payload)

    assert expected_issue in str(excinfo.value)


def test_parse_artifact_payload_accepts_optional_note_and_error_strings():
    artifact = parse_artifact_payload({
        "schema_version": 1,
        "validated_at": "2026-06-01T12:00:00Z",
        "hardware": {"mic_id": "xvf3800", "dac_id": "apple_usb_c_dongle"},
        "profile": "xvf_chip_aec",
        "status": "fail",
        "checks": {"drift_ppm_30m": 46.0},
        "recommendation": "use_software_aec3",
        "notes": "old feeder path",
        "errors": ["drift exceeded gate"],
    })

    assert artifact.notes == ("old feeder path",)
    assert artifact.errors == ("drift exceeded gate",)


def test_load_artifact_marks_stale_when_older_than_threshold(tmp_path):
    artifact = _artifact(validated_at=NOW - timedelta(days=31))
    path = write_artifact(artifact, directory=tmp_path)

    result = load_artifact(path, now=NOW)

    assert result.state == "stale"
    assert result.artifact == artifact
    assert result.ok is False
    assert result.has_artifact is True
    assert result.stale is True


def test_load_artifact_rejects_future_timestamps_beyond_skew(tmp_path):
    artifact = _artifact(validated_at=NOW + timedelta(minutes=10))
    path = write_artifact(artifact, directory=tmp_path)

    result = load_artifact(path, now=NOW)

    assert result.state == "future"
    assert result.artifact == artifact
    assert result.ok is False
    assert result.has_artifact is True
    assert "future" in result.errors[0]


def test_freshness_helpers_support_custom_thresholds():
    artifact = _artifact(validated_at=NOW - timedelta(hours=3))

    assert artifact_age(artifact, now=NOW) == timedelta(hours=3)
    assert is_artifact_stale(
        artifact,
        now=NOW,
        max_age=timedelta(hours=2),
    )
    assert not is_artifact_stale(
        artifact,
        now=NOW,
        max_age=timedelta(hours=4),
    )
    assert not is_artifact_stale(artifact, now=NOW, max_age=None)
    assert is_artifact_from_future(
        _artifact(validated_at=NOW + timedelta(minutes=10)),
        now=NOW,
    )
    assert not is_artifact_from_future(
        _artifact(validated_at=NOW + timedelta(minutes=1)),
        now=NOW,
    )


def test_load_latest_artifact_picks_newest_matching_valid_payload(tmp_path):
    old = _artifact(validated_at=NOW - timedelta(days=2), status="fail")
    newest_other = _artifact(
        validated_at=NOW,
        dac_id="hifiberry_dac8x",
        recommendation="use_software_aec3",
    )
    newest_match = _artifact(
        validated_at=NOW - timedelta(hours=1),
        status="pass",
    )
    write_artifact(old, directory=tmp_path)
    write_artifact(newest_other, directory=tmp_path)
    expected_path = write_artifact(newest_match, directory=tmp_path)

    result = load_latest_artifact(
        tmp_path,
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        now=NOW,
    )

    assert result.state == "loaded"
    assert result.artifact == newest_match
    assert result.path == expected_path


def test_load_latest_artifact_reports_malformed_when_no_valid_artifacts(tmp_path):
    (tmp_path / "bad.json").write_text("[]", encoding="utf-8")

    result = load_latest_artifact(tmp_path)

    assert result.state == "malformed"
    assert result.artifact is None
    assert "artifact must be a JSON object" in result.errors[0]


def test_load_latest_artifact_keeps_malformed_errors_when_valid_artifact_exists(
    tmp_path,
):
    (tmp_path / "bad.json").write_text("{bad", encoding="utf-8")
    artifact = _artifact()
    write_artifact(artifact, directory=tmp_path)

    result = load_latest_artifact(tmp_path, now=NOW)

    assert result.state == "loaded"
    assert result.artifact == artifact
    assert result.errors


def test_load_latest_artifact_surfaces_future_artifact_even_with_older_pass(
    tmp_path,
):
    old = _artifact(validated_at=NOW - timedelta(days=1))
    future = _artifact(validated_at=NOW + timedelta(minutes=10))
    write_artifact(old, directory=tmp_path)
    expected_path = write_artifact(future, directory=tmp_path)

    result = load_latest_artifact(tmp_path, now=NOW)

    assert result.state == "future"
    assert result.artifact == future
    assert result.path == expected_path
    assert result.ok is False


def test_validation_artifact_rejects_non_json_check_values():
    with pytest.raises(ValidationArtifactError) as excinfo:
        _artifact(checks={"not_json": object()})

    assert "not_json" in str(excinfo.value)


def test_validation_artifact_rejects_non_finite_check_values():
    with pytest.raises(ValidationArtifactError) as excinfo:
        _artifact(checks={"drift_ppm_30m": math.nan})

    assert "drift_ppm_30m" in str(excinfo.value)


def test_to_dict_uses_manually_inspectable_schema():
    artifact = _artifact(errors=("drift unstable",))

    data = artifact.to_dict()

    assert data["schema_version"] == 1
    assert data["validated_at"] == "2026-06-01T16:00:00Z"
    assert data["hardware"] == {
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }
    assert data["profile"] == "xvf_chip_aec"
    assert data["status"] == "pass"
    assert data["recommendation"] == "chip_aec_viable"
    assert data["errors"] == ["drift unstable"]
    json.dumps(data)


def _active_chip_inputs() -> dict:
    return {
        "now": NOW,
        "mode_env": {
            "JASPER_AEC_MODE": "auto",
            "JASPER_WAKE_LEG_RAW": "1",
            "JASPER_WAKE_LEG_DTLN": "0",
            "JASPER_WAKE_LEG_CHIP_AEC": "1",
        },
        "system_env": {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "udp:9888",
            "JASPER_OUTPUTD_BACKEND": "alsa",
            "JASPER_OUTPUTD_DAC_PCM": "outputd_dac",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
        },
        "mic_probe": MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
        ),
        "service_states": {
            "jasper-outputd.service": "active",
            "jasper-aec-bridge.service": "active",
            "jasper-aec-init.service": "active",
            "jasper-voice.service": "active",
        },
        "outputd_status": {
            "backend": "alsa",
            "dac": {"pcm": "outputd_dac", "sample_rate": 48000},
            "reference_outputs": {
                "chip_ref_pcm": "plughw:CARD=Array,DEV=0",
                "chip_ref_sample_rate": 16000,
                "chip_ref_period_frames": 320,
                "chip_ref_buffer_frames": 1280,
                "udp_target": "127.0.0.1:9891",
            },
        },
        "bridge_stats": {
            "schema_version": 1,
            "updated_epoch_sec": NOW.timestamp(),
            "counters": {
                "frames_processed": 42,
                "ref_starved_frames": 0,
                "queue_drops": {"mic": 0, "chip": 0, "raw0": 0, "usb": 0, "ref": 0},
                "udp_send_drops_by_leg": {"on": 0, "chip_aec_150": 0, "chip_aec_210": 0},
                "packets_sent_by_leg": {"on": 10, "chip_aec_150": 10, "chip_aec_210": 10},
            },
        },
        "voice_wake_legs": {"on", "chip_aec_150", "chip_aec_210"},
        "bridge_journal_text": "",
    }


def _outputd_sample(
    *,
    reference_sequence: int,
    dac_frames_written: int = 48_000,
    dac_xruns: int = 0,
    content_xruns: int = 0,
    clipped_samples: int = 0,
    progress_age_ms: int = 20,
) -> dict:
    sample = dict(_active_chip_inputs()["outputd_status"])
    sample.update({
        "content": {"xrun_count": content_xruns},
        "dac": {
            "pcm": "outputd_dac",
            "sample_rate": 48000,
            "frames_written": dac_frames_written,
            "xrun_count": dac_xruns,
        },
        "mix": {
            "reference_sequence": reference_sequence,
            "clipped_samples": clipped_samples,
        },
        "watchdog": {"last_progress_age_ms": progress_age_ms},
    })
    return sample


def _bridge_sample(
    *,
    frames_processed: int,
    ref_starved_frames: int = 0,
    queue_drops: int = 0,
    udp_drops: int = 0,
) -> dict:
    return {
        "schema_version": 1,
        "updated_epoch_sec": NOW.timestamp(),
        "counters": {
            "frames_processed": frames_processed,
            "ref_starved_frames": ref_starved_frames,
            "queue_drops": {"mic": queue_drops, "chip": 0, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {
                "on": udp_drops,
                "chip_aec_150": 0,
                "chip_aec_210": 0,
            },
            "packets_sent_by_leg": {"on": 10, "chip_aec_150": 10, "chip_aec_210": 10},
        },
    }


def _chip_readback(sys_delay: int = 12) -> dict:
    return {
        "SHF_BYPASS": [0],
        "AUDIO_MGR_SYS_DELAY": [sys_delay],
        "AEC_ASROUTONOFF": [1],
        "AEC_FIXEDBEAMSONOFF": [1],
        "AEC_FIXEDBEAMSGATING": [1],
    }


def _outputd_stability_inputs() -> dict:
    return {
        "now": NOW,
        "system_env": {
            "JASPER_OUTPUTD_BACKEND": "alsa",
            "JASPER_OUTPUTD_DAC_PCM": "outputd_dac",
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
        },
        "service_states": {
            "jasper-outputd.service": "active",
            "jasper-camilla.service": "active",
            "jasper-fanin.service": "active",
            "jasper-aec-bridge.service": "inactive",
            "jasper-aec-init.service": "inactive",
            "jasper-voice.service": "inactive",
        },
    }


def test_chip_aec_readiness_snapshot_uses_schema_helper_without_full_pass():
    artifact = audio_validation.build_chip_aec_readiness_artifact(
        **_active_chip_inputs(),
    )

    assert isinstance(artifact, ValidationArtifact)
    assert artifact.schema_version == audio_validation.CURRENT_SCHEMA_VERSION
    assert artifact.profile == "xvf_chip_aec"
    assert artifact.status == "warn"
    assert artifact.mic_id == "xvf3800"
    assert artifact.dac_id == "apple_usb_c_dongle"
    assert artifact.checks["runtime_identity"]["status"] == "pass"
    assert artifact.checks["runtime_identity"]["required"] is False
    assert "system_hostname" in artifact.checks["runtime_identity"]["observed"]
    assert artifact.checks["runtime_profile"]["status"] == "pass"
    assert artifact.checks["dac_reference"]["status"] == "pass"
    assert artifact.checks["wake_legs"]["status"] == "pass"
    assert artifact.checks["measured_drift_delay"]["status"] == "not_run"
    assert artifact.recommendation == "run_hardware_validation"
    assert "readiness_snapshot" in artifact.notes[0]


def test_chip_aec_readiness_fails_when_outputd_reference_missing():
    inputs = _active_chip_inputs()
    inputs["outputd_status"] = {
        "backend": "alsa",
        "dac": {"pcm": "outputd_dac", "sample_rate": 48000},
        "reference_outputs": {
            "chip_ref_pcm": None,
            "chip_ref_sample_rate": 16000,
            "udp_target": None,
        },
    }

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.status == "fail"
    assert artifact.checks["dac_reference"]["status"] == "fail"
    assert artifact.recommendation == "fix_outputd_chip_reference_before_chip_aec"


def test_chip_aec_readiness_unknown_runtime_recommends_observability_fix():
    inputs = _active_chip_inputs()
    inputs["outputd_status"] = {}

    artifact = audio_validation.build_chip_aec_readiness_artifact(**inputs)

    assert artifact.status == "warn"
    assert artifact.checks["dac_reference"]["status"] == "unknown"
    assert (
        artifact.recommendation
        == "fix_runtime_observability_before_hardware_validation"
    )


def test_chip_aec_hardware_validation_passive_evidence_warns_until_drift_probe():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=14, dac_frames_written=5000),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        chip_readback=_chip_readback(),
        chip_convergence_polls=[
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [0]},
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
        ],
        duration_seconds=10,
    )

    assert artifact.status == "warn"
    assert artifact.checks["outputd_reference_health"]["status"] == "pass"
    assert artifact.checks["bridge_counter_window"]["status"] == "pass"
    assert artifact.checks["chip_profile_readback"]["status"] == "pass"
    assert artifact.checks["chip_convergence"]["status"] == "pass"
    assert artifact.checks["measured_drift_delay"]["status"] == "not_run"
    assert artifact.recommendation == "run_drift_delay_validation"
    assert "No playback stimulus was generated." in artifact.notes
    assert "No XVF chip settings were written or persisted." in artifact.notes


def test_outputd_stability_profile_passes_without_chip_aec_or_voice():
    inputs = _outputd_stability_inputs()
    artifact = audio_validation.build_outputd_stability_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=16, dac_frames_written=7000),
        ],
        duration_seconds=10,
    )

    assert artifact.profile == audio_validation.DAC8X_OUTPUTD_STABILITY_PROFILE
    assert artifact.status == "pass"
    assert artifact.mic_id == "not_applicable"
    assert artifact.dac_id == "hifiberry_dac8x"
    assert artifact.checks["service_state"]["status"] == "pass"
    assert artifact.checks["dac_output"]["status"] == "pass"
    assert artifact.checks["outputd_reference_health"]["status"] == "pass"
    assert "runtime_profile" not in artifact.checks
    assert "bridge_counter_window" not in artifact.checks
    assert "chip_profile_readback" not in artifact.checks
    assert artifact.recommendation == "outputd_dac_stability_validated"


def test_outputd_stability_profile_accepts_string_sample_rate_from_status():
    inputs = _outputd_stability_inputs()
    artifact = audio_validation.build_outputd_stability_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            {
                **_outputd_sample(reference_sequence=10, dac_frames_written=1000),
                "dac": {"pcm": "outputd_dac", "sample_rate": "48000"},
            },
            _outputd_sample(reference_sequence=16, dac_frames_written=7000),
        ],
        duration_seconds=10,
    )

    assert artifact.checks["dac_output"]["status"] == "pass"
    assert artifact.checks["dac_output"]["observed"]["sample_rate"] == 48000


def test_chip_aec_hardware_validation_zero_convergence_is_not_observed():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_frames_written=1000),
            _outputd_sample(reference_sequence=14, dac_frames_written=5000),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        chip_readback=_chip_readback(),
        chip_convergence_polls=[
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [0]},
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [0]},
        ],
        duration_seconds=10,
    )

    assert artifact.status == "warn"
    assert artifact.checks["chip_convergence"]["status"] == "not_observed"
    assert "nothing meaningful" in artifact.checks["chip_convergence"]["summary"]
    assert artifact.recommendation == "run_drift_delay_validation"


def test_chip_aec_hardware_validation_fails_on_outputd_xrun_window():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10, dac_xruns=0),
            _outputd_sample(reference_sequence=14, dac_xruns=1),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        duration_seconds=10,
    )

    assert artifact.status == "fail"
    assert artifact.checks["outputd_reference_health"]["status"] == "fail"
    assert artifact.recommendation == "fix_outputd_reference_health_before_chip_validation"
    assert "outputd_reference_health" in artifact.errors[0]


def test_chip_aec_hardware_validation_gates_chip_poll_until_ref_health_passes():
    inputs = _active_chip_inputs()
    artifact = audio_validation.build_chip_aec_hardware_validation_artifact(
        **inputs,
        outputd_status_samples=[
            _outputd_sample(reference_sequence=10),
            _outputd_sample(reference_sequence=10),
        ],
        bridge_stats_samples=[
            _bridge_sample(frames_processed=100),
            _bridge_sample(frames_processed=140),
        ],
        chip_readback=_chip_readback(),
        chip_convergence_polls=[
            {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
        ],
        duration_seconds=10,
    )

    assert artifact.status == "warn"
    assert artifact.checks["outputd_reference_health"]["status"] == "warn"
    assert artifact.checks["chip_profile_readback"]["status"] == "not_run"
    assert artifact.checks["chip_convergence"]["status"] == "not_run"
    assert artifact.recommendation == "review_outputd_reference_health_before_chip_validation"


def test_run_chip_aec_hardware_validation_refuses_inactive_without_force(monkeypatch):
    inputs = _active_chip_inputs()
    mode_env = dict(inputs["mode_env"])
    mode_env["JASPER_WAKE_LEG_CHIP_AEC"] = "0"

    monkeypatch.setattr(audio_validation, "_read_mode_env", lambda: mode_env)
    monkeypatch.setattr(audio_validation, "_read_system_env", lambda: inputs["system_env"])
    monkeypatch.setattr(audio_validation, "_probe_xvf_mic", lambda: inputs["mic_probe"])
    monkeypatch.setattr(
        audio_validation,
        "_collect_service_states",
        lambda: inputs["service_states"],
    )
    monkeypatch.setattr(
        audio_validation,
        "_query_outputd_status",
        lambda _socket: inputs["outputd_status"],
    )
    monkeypatch.setattr(audio_validation, "_read_bridge_stats", lambda: inputs["bridge_stats"])
    monkeypatch.setattr(
        audio_validation,
        "_read_voice_wake_legs",
        lambda: inputs["voice_wake_legs"],
    )
    monkeypatch.setattr(audio_validation, "_recent_bridge_journal", lambda: "")

    result = audio_validation.run_chip_aec_hardware_validation(
        report_only=True,
        now=NOW,
    )

    assert result.refused is True
    assert result.artifact is None
    assert "not the active runtime profile" in result.refusal_reason


def test_run_chip_aec_hardware_validation_report_only_does_not_write(monkeypatch):
    inputs = _active_chip_inputs()
    wrote: list[str] = []

    monkeypatch.setattr(audio_validation, "_read_mode_env", lambda: inputs["mode_env"])
    monkeypatch.setattr(audio_validation, "_read_system_env", lambda: inputs["system_env"])
    monkeypatch.setattr(audio_validation, "_probe_xvf_mic", lambda: inputs["mic_probe"])
    monkeypatch.setattr(
        audio_validation,
        "_collect_service_states",
        lambda: inputs["service_states"],
    )
    monkeypatch.setattr(
        audio_validation,
        "_query_outputd_status",
        lambda _socket: inputs["outputd_status"],
    )
    monkeypatch.setattr(audio_validation, "_read_bridge_stats", lambda: inputs["bridge_stats"])
    monkeypatch.setattr(
        audio_validation,
        "_read_voice_wake_legs",
        lambda: inputs["voice_wake_legs"],
    )
    monkeypatch.setattr(audio_validation, "_recent_bridge_journal", lambda: "")
    monkeypatch.setattr(
        audio_validation,
        "write_artifact",
        lambda *_args, **_kwargs: wrote.append("artifact"),
    )
    monkeypatch.setattr(
        audio_validation,
        "write_latest_pointer",
        lambda *_args, **_kwargs: wrote.append("latest"),
    )

    result = audio_validation.run_chip_aec_hardware_validation(
        report_only=True,
        now=NOW,
    )

    assert result.refused is False
    assert result.artifact is not None
    assert result.artifact.checks["outputd_reference_health"]["status"] == "not_run"
    assert wrote == []


def test_run_chip_aec_hardware_validation_uses_one_bounded_window(
    monkeypatch,
    tmp_path,
):
    inputs = _active_chip_inputs()
    outputd_samples = iter([
        _outputd_sample(reference_sequence=10, dac_frames_written=1000),
        _outputd_sample(reference_sequence=11, dac_frames_written=2000),
        _outputd_sample(reference_sequence=15, dac_frames_written=6000),
    ])
    bridge_samples = iter([
        _bridge_sample(frames_processed=100),
        _bridge_sample(frames_processed=110),
        _bridge_sample(frames_processed=150),
    ])
    sleeps: list[float] = []
    chip_poll_durations: list[float] = []

    monkeypatch.setattr(audio_validation, "_read_mode_env", lambda: inputs["mode_env"])
    monkeypatch.setattr(audio_validation, "_read_system_env", lambda: inputs["system_env"])
    monkeypatch.setattr(audio_validation, "_probe_xvf_mic", lambda: inputs["mic_probe"])
    monkeypatch.setattr(
        audio_validation,
        "_collect_service_states",
        lambda: inputs["service_states"],
    )
    monkeypatch.setattr(
        audio_validation,
        "_query_outputd_status",
        lambda _socket: next(outputd_samples),
    )
    monkeypatch.setattr(audio_validation, "_read_bridge_stats", lambda: next(bridge_samples))
    monkeypatch.setattr(
        audio_validation,
        "_read_voice_wake_legs",
        lambda: inputs["voice_wake_legs"],
    )
    monkeypatch.setattr(audio_validation, "_recent_bridge_journal", lambda: "")
    monkeypatch.setattr(audio_validation.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        audio_validation,
        "_read_chip_profile_parameters",
        lambda: _chip_readback(),
    )

    def poll_chip(**kwargs):
        chip_poll_durations.append(kwargs["duration_seconds"])
        return [{audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]}]

    monkeypatch.setattr(audio_validation, "_poll_chip_convergence", poll_chip)

    result = audio_validation.run_chip_aec_hardware_validation(
        directory=tmp_path,
        duration_seconds=10,
        now=NOW,
    )

    assert result.refused is False
    assert result.path is not None
    assert sleeps == [1.0]
    assert chip_poll_durations == [9.0]
    assert result.artifact is not None
    assert result.artifact.checks["outputd_reference_health"]["status"] == "pass"


def test_run_outputd_stability_profile_does_not_probe_chip_or_voice(
    monkeypatch,
    tmp_path,
):
    inputs = _outputd_stability_inputs()
    outputd_samples = iter([
        _outputd_sample(reference_sequence=10, dac_frames_written=1000),
        _outputd_sample(reference_sequence=17, dac_frames_written=8000),
    ])
    sleeps: list[float] = []

    monkeypatch.setattr(audio_validation, "_read_system_env", lambda: inputs["system_env"])
    monkeypatch.setattr(
        audio_validation,
        "_collect_service_states",
        lambda: inputs["service_states"],
    )
    monkeypatch.setattr(
        audio_validation,
        "_query_outputd_status",
        lambda _socket: next(outputd_samples),
    )
    monkeypatch.setattr(audio_validation.time, "sleep", lambda seconds: sleeps.append(seconds))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("chip-AEC probe path should not run for outputd stability")

    monkeypatch.setattr(audio_validation, "_read_mode_env", forbidden)
    monkeypatch.setattr(audio_validation, "_probe_xvf_mic", forbidden)
    monkeypatch.setattr(audio_validation, "_read_bridge_stats", forbidden)
    monkeypatch.setattr(audio_validation, "_read_voice_wake_legs", forbidden)
    monkeypatch.setattr(audio_validation, "_read_chip_profile_parameters", forbidden)
    monkeypatch.setattr(audio_validation, "_poll_chip_convergence", forbidden)

    result = audio_validation.run_audio_hardware_validation(
        profile=audio_validation.DAC8X_OUTPUTD_STABILITY_PROFILE,
        directory=tmp_path,
        duration_seconds=10,
        now=NOW,
    )

    assert result.refused is False
    assert result.path is not None
    assert sleeps == [10]
    assert result.artifact is not None
    assert result.artifact.status == "pass"
    assert result.artifact.profile == audio_validation.DAC8X_OUTPUTD_STABILITY_PROFILE


def test_chip_aec_runner_name_remains_compatibility_wrapper(monkeypatch):
    calls = []

    def fake_run_audio_hardware_validation(**kwargs):
        calls.append(kwargs)
        return audio_validation.HardwareValidationRun(
            artifact=None,
            refused=True,
            refusal_reason="test",
        )

    monkeypatch.setattr(
        audio_validation,
        "run_audio_hardware_validation",
        fake_run_audio_hardware_validation,
    )

    result = audio_validation.run_chip_aec_hardware_validation(
        profile=audio_validation.CHIP_AEC_PROFILE,
        duration_seconds=3,
        force=True,
    )

    assert result.refused is True
    assert calls == [{
        "profile": audio_validation.CHIP_AEC_PROFILE,
        "directory": None,
        "duration_seconds": 3,
        "poll_interval_seconds": audio_validation.DEFAULT_CHIP_POLL_INTERVAL_SECONDS,
        "report_only": False,
        "force": True,
        "allow_long": False,
        "stdout": False,
        "now": None,
    }]


def test_latest_artifact_summary_reads_timestamped_artifacts(tmp_path):
    artifact = audio_validation.build_chip_aec_readiness_artifact(
        **_active_chip_inputs(),
    )
    path = audio_validation.write_artifact(artifact, directory=tmp_path)
    summary = audio_validation.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )

    assert path.name != "latest.json"
    assert summary["state"] == "current"
    assert summary["status"] == "warn"
    assert summary["artifact_path"] == str(path)
    assert summary["hardware"] == {
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }
    assert summary["check_statuses"]["measured_drift_delay"] == "not_run"


def test_latest_artifact_summary_prefers_latest_pointer(tmp_path):
    artifact = audio_validation.build_chip_aec_readiness_artifact(
        **_active_chip_inputs(),
    )
    audio_validation.write_artifact(artifact, directory=tmp_path)
    latest_path = audio_validation.write_latest_pointer(artifact, directory=tmp_path)

    summary = audio_validation.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )

    assert summary["artifact_path"] == str(latest_path)
    assert summary["status"] == "warn"
    assert "reason" not in summary


def test_latest_artifact_summary_falls_back_from_stale_mismatched_latest(
    tmp_path,
):
    stale_other = make_artifact(
        validated_at=NOW - timedelta(days=31),
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_software_aec3",
        status="pass",
        checks={"measured_drift_delay": {"status": "pass"}},
        recommendation="use_software_aec3",
    )
    matching = make_artifact(
        validated_at=NOW,
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        status="warn",
        checks={"measured_drift_delay": {"status": "not_run"}},
        recommendation="run_hardware_validation",
    )
    matching_path = audio_validation.write_artifact(matching, directory=tmp_path)
    audio_validation.write_latest_pointer(stale_other, directory=tmp_path)

    summary = audio_validation.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )

    assert summary["state"] == "current"
    assert summary["artifact_path"] == str(matching_path)
    assert summary["profile"] == "xvf_chip_aec"
    assert summary["reason"].startswith("latest.json ignored:")


def test_latest_artifact_summary_falls_back_from_malformed_latest(tmp_path):
    matching = make_artifact(
        validated_at=NOW,
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        status="warn",
        checks={"measured_drift_delay": {"status": "not_run"}},
        recommendation="run_hardware_validation",
    )
    matching_path = audio_validation.write_artifact(matching, directory=tmp_path)
    (tmp_path / "latest.json").write_text("{bad", encoding="utf-8")

    summary = audio_validation.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )

    assert summary["state"] == "current"
    assert summary["artifact_path"] == str(matching_path)
    assert summary["profile"] == "xvf_chip_aec"
    assert "latest.json ignored: invalid JSON" in summary["reason"]


def test_latest_artifact_summary_marks_stale_and_profile_mismatch(tmp_path):
    artifact = make_artifact(
        validated_at=NOW - timedelta(days=31),
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        status="warn",
        checks={"measured_drift_delay": {"status": "not_run"}},
        recommendation="run_hardware_validation",
    )
    artifact_path = audio_validation.write_artifact(artifact, directory=tmp_path)

    stale = audio_validation.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )
    mismatch = audio_validation.latest_artifact_summary(
        path=artifact_path,
        requested_profile="xvf_software_aec3",
        now=NOW,
    )

    assert stale["state"] == "stale"
    assert mismatch["state"] == "mismatch"
    assert mismatch["available"] is True
