# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import stat
import struct
import wave
from pathlib import Path

import pytest

from jasper.active_speaker import (
    TONE_PLAN_KIND,
    ActiveSpeakerPreset,
    driver_test_signal_plan,
)
from jasper.active_speaker.calibration_level import MAX_TEST_LEVEL_DBFS
from jasper.active_speaker.playback import (
    NullTonePlaybackBackend,
    WavArtifactTonePlaybackBackend,
    start_tone_playback,
    stop_tone_playback,
    tone_backend_status,
)
from jasper.active_speaker.safe_playback import (
    _write_state,
    arm_safe_playback_session,
    record_safe_playback_result,
    stop_safe_playback_session,
)


def test_safe_playback_state_writer_publishes_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "safe-playback.json"
    _write_state(path, {"status": "idle"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert json.loads(path.read_text()) == {"status": "idle"}


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
    signal = driver_test_signal_plan(_preset(), "tweeter")
    return {
        "artifact_schema_version": 1,
        "kind": TONE_PLAN_KIND,
        "status": "ready",
        "would_play": False,
        "playback_allowed": False,
        "tone_playback_implemented": False,
        "channel_map": {"layout": "mono", "output_count": 2},
        "target": {
            "side": "mono",
            "driver_role": "tweeter",
            "output_index": 1,
            "label": "mono tweeter",
        },
        "tone": {
            "waveform": "sine",
            "frequency_hz": signal["frequency_hz"],
            "level_dbfs": -55.0,
            "duration_ms": 120,
            "ramp_ms": 20,
            "band_limit": signal["band_limit"],
            "signal_plan": signal,
        },
        "driver_protection": signal["driver_protection"],
        "safety": {
            "safe_session_id": "session-test",
            "requires_emergency_stop": True,
            "prepared_only": True,
        },
        "issues": [],
    }


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
    assert metadata["audible_test"]["policy_version"] == (
        "driver_protection_auto_level_v1"
    )
    assert metadata["audible_test"]["target_role"] == "tweeter"
    assert metadata["driver_protection"]["role_class"] == "high_frequency"
    assert metadata["safety"] == {
        "protected_startup_loaded": False,
        "safe_session_id": "session-test",
    }
    assert metadata["wav"]["channel_count"] == 2


def test_wav_artifact_backend_honors_declared_active_lane_width(
    tmp_path: Path,
) -> None:
    plan = _plan()
    plan["channel_map"]["output_count"] = 4
    backend = WavArtifactTonePlaybackBackend(artifact_dir=tmp_path)

    result = start_tone_playback(
        plan,
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=backend,
        now=lambda: 1000,
    )

    artifact = result["artifact"]
    assert artifact["channel_count"] == 4
    with wave.open(artifact["wav_path"], "rb") as wav:
        assert wav.getnchannels() == 4
        raw = wav.readframes(wav.getnframes())
    samples = [item[0] for item in struct.iter_unpack("<h", raw)]
    assert max(abs(sample) for sample in samples[0::4]) == 0
    assert max(abs(sample) for sample in samples[1::4]) > 0
    assert max(abs(sample) for sample in samples[2::4]) == 0
    assert max(abs(sample) for sample in samples[3::4]) == 0


def test_wav_artifact_metadata_recomputes_stale_driver_protection(
    tmp_path: Path,
) -> None:
    plan = {
        **_plan(),
        "target": {
            **_plan()["target"],
            "driver_role": "woofer",
            "output_index": 0,
        },
    }
    backend = WavArtifactTonePlaybackBackend(artifact_dir=tmp_path)

    result = start_tone_playback(
        plan,
        safe_session={"status": "armed", "session_id": "session-test"},
        backend=backend,
        now=lambda: 1000,
    )

    metadata = json.loads(Path(result["artifact"]["metadata_path"]).read_text())
    assert metadata["target"]["output_index"] == 0
    assert metadata["audible_test"]["target_role"] == "woofer"
    assert metadata["driver_protection"]["role"] == "woofer"
    assert metadata["driver_protection"]["role_class"] == "low_frequency"


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
    assert artifact["peak_dbfs"] <= MAX_TEST_LEVEL_DBFS + 0.5
    metadata = json.loads(Path(artifact["metadata_path"]).read_text())
    assert metadata["tone"]["level_dbfs"] == MAX_TEST_LEVEL_DBFS
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
        "level_dbfs": MAX_TEST_LEVEL_DBFS,
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
        "highpass_hz": 5000.0,
    }


def test_wav_artifact_preview_does_not_require_armed_session(tmp_path: Path) -> None:
    result = start_tone_playback(
        _plan(),
        safe_session={"status": "idle"},
        backend=WavArtifactTonePlaybackBackend(artifact_dir=tmp_path),
    )

    assert result["status"] == "completed"
    assert result["audio_emitted"] is False
    assert result["artifact"]["wav_path"]
    assert "safe_session_not_armed" not in {
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


def test_tone_backend_status_requires_explicit_audio_lab_pcm() -> None:
    default = tone_backend_status({})
    blocked = tone_backend_status({
        "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
    })
    selected = tone_backend_status({
        "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
        "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
    })

    assert default["status"] == "artifact_only"
    assert default["audio_enabled"] is False
    assert blocked["status"] == "blocked"
    assert "test_pcm_required" in {
        issue["code"] for issue in blocked["issues"]
    }
    # The operator-typed value is still parsed and echoed back...
    assert selected["test_pcm"] == "hw:Active"
    assert selected["tone_backend_env"] == "JASPER_AUDIO_LAB_TONE_BACKEND"
    assert selected["test_pcm_env"] == "JASPER_AUDIO_LAB_TEST_PCM"
    assert "allow_audio_env" not in selected


def test_a_well_formed_aplay_selection_is_reported_as_not_wired() -> None:
    """The honesty half: selecting aplay must never report audio_enabled.

    No production path consults this selection -- every ``start_tone_playback``
    call site passes ``backend=None``, so the tone renders to a WAV artifact
    whatever the knob says. Reporting ``audio_enabled: true`` told the operator
    the opposite; a named blocker tells the truth.
    """

    selected = tone_backend_status({
        "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
        "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
    })

    assert selected["status"] == "blocked"
    assert selected["audio_enabled"] is False
    assert selected["tone_playback_implemented"] is False
    assert selected["audio_backend"] is None
    assert "tone_backend_not_wired" in {
        issue["code"] for issue in selected["issues"]
    }


def test_tone_backend_status_blocks_forbidden_main_lane_test_pcm() -> None:
    blocked = tone_backend_status({
        "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
        "JASPER_AUDIO_LAB_TEST_PCM": "plug:jasper_out",
    })

    assert blocked["status"] == "blocked"
    assert blocked["audio_enabled"] is False
    assert "test_pcm_forbidden_main_lane" in {
        issue["code"] for issue in blocked["issues"]
    }


def test_tone_backend_status_allows_dedicated_active_test_pcm() -> None:
    """A dedicated lab PCM is not the forbidden-lane failure.

    It is still blocked -- nothing wires an audio backend -- but for the
    not-wired reason, never for targeting a daemon-owned lane.
    """

    selected = tone_backend_status({
        "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
        "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
    })

    assert "test_pcm_forbidden_main_lane" not in {
        issue["code"] for issue in selected["issues"]
    }
    assert {issue["code"] for issue in selected["issues"]} == {
        "tone_backend_not_wired"
    }


def test_tone_backend_status_treats_removed_direct_dac_backend_as_unknown() -> None:
    blocked = tone_backend_status({
        "JASPER_AUDIO_LAB_TONE_BACKEND": "direct_dac",
        "JASPER_AUDIO_LAB_TEST_PCM": "hw:CARD=DAC8,DEV=0",
    })

    assert blocked["status"] == "blocked"
    assert blocked["audio_enabled"] is False
    assert "unknown_tone_backend" in {
        issue["code"] for issue in blocked["issues"]
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
            "was not confirmed: PermissionError: cannot write artifact"
        ),
    }]
    assert result["backend_error"] == {
        "type": "PermissionError",
        "message": "cannot write artifact",
    }


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


