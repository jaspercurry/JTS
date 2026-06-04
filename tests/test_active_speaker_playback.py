from __future__ import annotations

import json
import os
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from jasper.active_speaker import ActiveSpeakerPreset, build_safe_tone_plan
from jasper.active_speaker.playback import (
    AplayTonePlaybackBackend,
    NullTonePlaybackBackend,
    WavArtifactTonePlaybackBackend,
    start_tone_playback,
    stop_tone_playback,
    tone_backend_status,
)
from jasper.active_speaker.safe_playback import (
    arm_safe_playback_session,
    record_safe_playback_result,
    stop_safe_playback_session,
)


def _preset() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "playback-test-v1",
        "name": "Playback test preset",
        "way_count": 2,
        "channel_map": {
            "layout": "mono",
            "outputs": [
                {
                    "index": 0,
                    "side": "mono",
                    "driver_role": "woofer",
                    "label": "mono woofer",
                    "startup_muted": True,
                },
                {
                    "index": 1,
                    "side": "mono",
                    "driver_role": "tweeter",
                    "label": "mono tweeter",
                    "startup_muted": True,
                },
            ],
        },
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "tweeter": {"manufacturer": "Example", "model": "Tweeter"},
        },
        "crossover_regions": [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": 1600,
            "target_type": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "delay_range_ms": [0.0, 0.5],
            "null_depth_threshold_db": 25,
        }],
        "safety": {
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    })


def _environment(*, ok: bool = True) -> dict:
    return {
        "status": "pass" if ok else "blocked",
        "load_gate": "ready" if ok else "blocked",
        "ok_to_load_active_config": ok,
        "safe_playback": {
            "status": "not_implemented",
            "playback_allowed": False,
        },
        "issues": [],
    }


def _plan() -> dict:
    return build_safe_tone_plan(
        _preset(),
        safe_session={"status": "armed", "session_id": "session-test"},
        environment_report=_environment(),
        side="mono",
        driver_role="tweeter",
        requested_level_dbfs=-55,
        requested_duration_ms=120,
    )


def test_wav_artifact_backend_renders_only_target_output_channel(
    tmp_path: Path,
) -> None:
    plan = _plan()
    backend = WavArtifactTonePlaybackBackend(artifact_dir=tmp_path)

    result = start_tone_playback(
        plan,
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=backend,
        now=lambda: 1000,
    )

    assert result["status"] == "completed"
    assert result["backend"] == "wav_artifact"
    assert result["audio_emitted"] is False
    artifact = result["artifact"]
    assert artifact["channel_count"] == 2
    assert artifact["target_output_index"] == 1
    assert artifact["sample_rate_hz"] == 48_000
    assert artifact["duration_ms"] == 120
    assert artifact["peak_dbfs"] <= -54.0

    with wave.open(artifact["wav_path"], "rb") as wav:
        assert wav.getnchannels() == 2
        assert wav.getframerate() == 48_000
        assert wav.getnframes() == artifact["frame_count"]
        raw = wav.readframes(wav.getnframes())
    samples = [item[0] for item in struct.iter_unpack("<h", raw)]
    channel_0 = samples[0::2]
    channel_1 = samples[1::2]
    assert max(abs(sample) for sample in channel_0) == 0
    assert max(abs(sample) for sample in channel_1) > 0

    metadata = json.loads(Path(artifact["metadata_path"]).read_text())
    assert metadata["audio_emitted"] is False
    assert metadata["target"]["output_index"] == 1
    assert metadata["wav"]["channel_count"] == 2


def test_wav_artifact_backend_enforces_writer_caps_for_direct_use(
    tmp_path: Path,
) -> None:
    plan = _plan()
    plan["tone"]["duration_ms"] = 60_000
    plan["tone"]["level_dbfs"] = 12
    plan["tone"]["ramp_ms"] = 60_000
    backend = WavArtifactTonePlaybackBackend(
        artifact_dir=tmp_path,
        sample_rate_hz=192_000,
    )

    result = backend.start(plan, playback_id="direct-caps", now_epoch=1000)
    artifact = result["artifact"]

    assert result["audio_emitted"] is False
    assert artifact["sample_rate_hz"] == 48_000
    assert artifact["duration_ms"] == 500
    assert artifact["frame_count"] == 24_000
    assert artifact["peak_dbfs"] <= -44.0
    metadata = json.loads(Path(artifact["metadata_path"]).read_text())
    assert metadata["tone"]["level_dbfs"] == -45.0
    assert metadata["tone"]["duration_ms"] == 500


def test_wav_artifact_backend_rejects_oversized_direct_channel_map(
    tmp_path: Path,
) -> None:
    plan = _plan()
    plan["channel_map"]["output_count"] = 99
    backend = WavArtifactTonePlaybackBackend(artifact_dir=tmp_path)

    with pytest.raises(ValueError, match="channel count 99 exceeds"):
        backend.start(plan, playback_id="too-wide", now_epoch=1000)

    assert list(tmp_path.iterdir()) == []


def test_wav_artifact_backend_prunes_old_artifact_sets(tmp_path: Path) -> None:
    for idx in range(3):
        for suffix in (".wav", ".json"):
            path = tmp_path / f"tone_old_{idx}{suffix}"
            path.write_text("old")
            os.utime(path, (1000 + idx, 1000 + idx))
    backend = WavArtifactTonePlaybackBackend(
        artifact_dir=tmp_path,
        artifact_retention=2,
    )

    result = backend.start(_plan(), playback_id="fresh", now_epoch=2000)
    artifact = result["artifact"]

    assert artifact["retention_keep"] == 2
    assert artifact["retention_removed"] == 4
    assert (tmp_path / "tone_fresh.wav").exists()
    assert (tmp_path / "tone_fresh.json").exists()
    assert (tmp_path / "tone_old_2.wav").exists()
    assert (tmp_path / "tone_old_2.json").exists()
    assert not (tmp_path / "tone_old_0.wav").exists()
    assert not (tmp_path / "tone_old_1.wav").exists()


def test_start_tone_playback_passes_bounded_tone_to_backend() -> None:
    class RecordingBackend:
        backend_id = "recording-test"

        def __init__(self) -> None:
            self.plan = None

        def start(self, plan, *, playback_id, now_epoch):
            self.plan = plan
            return {
                "backend": self.backend_id,
                "status": "completed",
                "audio_emitted": False,
                "artifact": None,
            }

        def stop(self, *, playback_id, reason, now_epoch):
            return {"status": "stopped", "audio_emitted": False}

    plan = _plan()
    plan["tone"]["duration_ms"] = 60_000
    plan["tone"]["level_dbfs"] = 12
    plan["tone"]["frequency_hz"] = -1
    plan["tone"]["ramp_ms"] = 60_000
    plan["tone"]["waveform"] = "square"
    backend = RecordingBackend()

    result = start_tone_playback(
        plan,
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=backend,
        now=lambda: 1000,
    )

    expected_tone = {
        "waveform": "sine",
        "frequency_hz": 20.0,
        "level_dbfs": -45.0,
        "duration_ms": 500,
        "ramp_ms": 250,
    }
    assert result["status"] == "completed"
    assert result["tone"] == expected_tone
    assert backend.plan is not None
    for key, value in expected_tone.items():
        assert backend.plan["tone"][key] == value
    assert backend.plan["tone"]["band_limit"] == {
        "type": "highpass",
        "highpass_hz": 1600.0,
    }


def test_start_tone_playback_blocks_without_armed_session(tmp_path: Path) -> None:
    result = start_tone_playback(
        _plan(),
        safe_session={"status": "idle"},
        backend=WavArtifactTonePlaybackBackend(artifact_dir=tmp_path),
    )

    assert result["status"] == "blocked"
    assert result["audio_emitted"] is False
    assert result["artifact"] is None
    assert "safe_session_not_armed" in {
        issue["code"] for issue in result["issues"]
    }


def test_null_backend_and_stop_contract_do_not_emit_audio() -> None:
    result = start_tone_playback(
        _plan(),
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=NullTonePlaybackBackend(),
        now=lambda: 1000,
    )
    stopped = stop_tone_playback(
        playback_id=result["playback_id"],
        backend=NullTonePlaybackBackend(),
        now=lambda: 1001,
    )

    assert result["status"] == "completed"
    assert result["audio_emitted"] is False
    assert stopped["status"] == "stopped"
    assert stopped["playback_id"] == result["playback_id"]
    assert stopped["audio_emitted"] is False


def test_tone_backend_status_requires_explicit_audio_enablement() -> None:
    default = tone_backend_status({})
    blocked = tone_backend_status({
        "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
        "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
    })
    enabled = tone_backend_status({
        "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
        "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
        "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
    })

    assert default["status"] == "artifact_only"
    assert default["audio_enabled"] is False
    assert blocked["status"] == "blocked"
    assert "audio_not_operator_enabled" in {
        issue["code"] for issue in blocked["issues"]
    }
    assert enabled["status"] == "audio_enabled"
    assert enabled["audio_enabled"] is True
    assert enabled["test_pcm"] == "hw:Active"


def test_tone_backend_status_blocks_forbidden_main_lane_test_pcm() -> None:
    blocked = tone_backend_status({
        "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
        "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
        "JASPER_ACTIVE_SPEAKER_TEST_PCM": "plug:jasper_out",
    })

    assert blocked["status"] == "blocked"
    assert blocked["audio_enabled"] is False
    assert "test_pcm_forbidden_main_lane" in {
        issue["code"] for issue in blocked["issues"]
    }


def test_tone_backend_status_allows_dedicated_active_test_pcm() -> None:
    enabled = tone_backend_status({
        "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
        "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
        "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
    })

    assert enabled["status"] == "audio_enabled"
    assert enabled["audio_enabled"] is True
    assert "test_pcm_forbidden_main_lane" not in {
        issue["code"] for issue in enabled["issues"]
    }


def test_aplay_backend_refuses_forbidden_main_lane_pcm(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="protected main lane"):
        AplayTonePlaybackBackend(
            pcm="plug:jasper_out",
            artifact_dir=tmp_path,
            runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 0),
        )


def test_audio_backend_blocks_when_readiness_did_not_authorize_audio(
    tmp_path: Path,
) -> None:
    result = start_tone_playback(
        _plan(),
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=AplayTonePlaybackBackend(
            pcm="hw:Active",
            artifact_dir=tmp_path,
            runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 0),
        ),
        allow_audio=True,
        now=lambda: 1000,
    )

    assert result["status"] == "blocked"
    assert result["audio_emitted"] is False
    assert result["artifact"] is None
    assert "playback_not_allowed_by_readiness" in {
        issue["code"] for issue in result["issues"]
    }


def test_aplay_backend_runs_generated_artifact_when_audio_is_authorized(
    tmp_path: Path,
) -> None:
    calls = []

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    plan = {
        **_plan(),
        "playback_allowed": True,
        "would_play": True,
        "tone_playback_implemented": True,
        "target": {
            **_plan()["target"],
            "driver_role": "woofer",
            "output_index": 0,
        },
    }

    result = start_tone_playback(
        plan,
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=AplayTonePlaybackBackend(
            pcm="hw:Active",
            aplay_binary="/usr/bin/aplay",
            artifact_dir=tmp_path,
            runner=runner,
        ),
        allow_audio=True,
        now=lambda: 1000,
    )

    assert result["status"] == "completed"
    assert result["backend"] == "aplay"
    assert result["audio_emitted"] is True
    assert result["audio_device"] == {"pcm": "hw:Active", "command": "aplay"}
    assert result["artifact"]["wav_basename"].startswith("tone_")
    assert calls
    assert calls[0][0][:4] == ["/usr/bin/aplay", "-q", "-D", "hw:Active"]
    assert calls[0][0][4].endswith(".wav")


def test_audio_backend_refuses_tweeter_in_first_audible_slice(
    tmp_path: Path,
) -> None:
    plan = {
        **_plan(),
        "playback_allowed": True,
        "would_play": True,
        "tone_playback_implemented": True,
    }

    result = start_tone_playback(
        plan,
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=AplayTonePlaybackBackend(
            pcm="hw:Active",
            artifact_dir=tmp_path,
            runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 0),
        ),
        allow_audio=True,
        now=lambda: 1000,
    )

    assert result["status"] == "blocked"
    assert "tweeter_audio_not_enabled" in {
        issue["code"] for issue in result["issues"]
    }


def test_start_tone_playback_reports_backend_failures_without_audio() -> None:
    class FailingBackend:
        backend_id = "failing-test"

        def start(self, plan, *, playback_id, now_epoch):
            raise PermissionError("cannot write artifact")

        def stop(self, *, playback_id, reason, now_epoch):
            return {"status": "stopped", "audio_emitted": False}

    result = start_tone_playback(
        _plan(),
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=FailingBackend(),
        now=lambda: 1000,
    )

    assert result["status"] == "failed"
    assert result["backend"] == "failing-test"
    assert result["audio_emitted"] is False
    assert result["artifact"] is None
    assert result["issues"] == [{
        "severity": "blocker",
        "code": "tone_backend_failed",
        "message": (
            "tone playback backend failed; successful audio emission "
            "was not confirmed: PermissionError"
        ),
    }]


def test_safe_playback_state_records_and_stops_playback_result(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "safe-playback.json"
    armed = arm_safe_playback_session(
        _environment(),
        state_path=state_path,
        now=lambda: 1000,
    )
    result = start_tone_playback(
        _plan(),
        safe_session=armed,
        backend=NullTonePlaybackBackend(),
        now=lambda: 1001,
    )

    recorded = record_safe_playback_result(
        result,
        state_path=state_path,
        now=lambda: 1002,
    )
    stopped = stop_safe_playback_session(
        state_path=state_path,
        now=lambda: 1003,
    )

    assert recorded["status"] == "armed"
    assert recorded["playback"]["status"] == "completed"
    assert recorded["playback"]["audio_emitted"] is False
    assert recorded["playback"]["target"]["output_index"] == 1
    assert stopped["status"] == "stopped"
    assert stopped["playback"]["status"] == "stopped"
    assert stopped["playback"]["playback_id"] == result["playback_id"]
