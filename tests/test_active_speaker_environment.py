# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from jasper.active_speaker import ActiveSpeakerPreset, emit_active_speaker_program_config
from jasper.active_speaker.environment import (
    classify_camilla_config_text,
    parse_aplay_playback_devices,
    parse_camilla_statefile_config_path,
    probe_alsa_playback_devices,
    probe_active_speaker_environment,
)
from jasper.active_speaker.path_safety import (
    HARDWARE_PROBE_EVIDENCE_SOURCE,
    OPERATOR_EVIDENCE_SOURCE,
    PATH_SAFETY_EVIDENCE_KIND,
    requirements_payload,
)
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
from tests.test_active_speaker_profile import _two_way_preset


_APLAY_STDOUT = """
**** List of PLAYBACK Hardware Devices ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
  Subdevices: 8/8
card 3: DAC8 [USB Audio Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""


def _runner(
    argv: list[str] | tuple[str, ...],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    assert list(argv) == ["aplay", "-l"]
    assert timeout > 0
    return subprocess.CompletedProcess(argv, 0, stdout=_APLAY_STDOUT, stderr="")


def _valid_config(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(
        status=ValidationStatus.VALID,
        path=str(path),
    )


def _path_safety_evidence(source: str) -> dict:
    paths = {}
    for requirement in requirements_payload()["requirements"]:
        paths[requirement["id"]] = {check: True for check in requirement["checks"]}
    return {
        "artifact_schema_version": 1,
        "kind": PATH_SAFETY_EVIDENCE_KIND,
        "evidence_source": source,
        "paths": paths,
    }


def _write_path_safety(tmp_path: Path, source: str) -> Path:
    path = tmp_path / f"path-safety-{source}.json"
    path.write_text(json.dumps(_path_safety_evidence(source)), encoding="utf-8")
    return path


def _active_config_text() -> str:
    return """
---
# Auto-generated active-speaker startup config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_startup_config
# preset_id=test-active-v1
devices:
  samplerate: 48000
  chunksize: 1024
  target_level: 2048
  volume_limit: 0.0
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
  playback:
    type: Alsa
    channels: 4
    device: "hw:DAC8,0"
mixers:
  split_active_2way:
    channels: { in: 2, out: 4 }
"""


def _outputd_config_text() -> str:
    return """
---
devices:
  samplerate: 48000
  chunksize: 1024
  target_level: 2048
  volume_limit: 0.0
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
  playback:
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
"""


def test_parse_aplay_playback_devices_returns_suggested_hw_names() -> None:
    devices = parse_aplay_playback_devices(_APLAY_STDOUT)

    assert [device["card_id"] for device in devices] == ["Headphones", "DAC8"]
    assert devices[1]["suggested_hw_device"] == "hw:DAC8,0"
    assert devices[1]["suggested_plughw_device"] == "plughw:DAC8,0"


def test_parse_camilla_statefile_config_path_handles_quotes() -> None:
    assert (
        parse_camilla_statefile_config_path('config_path: "/tmp/active.yml"\n')
        == "/tmp/active.yml"
    )


def test_classify_camilla_config_text_distinguishes_active_outputd_and_custom() -> None:
    active = classify_camilla_config_text(_active_config_text())
    outputd = classify_camilla_config_text(_outputd_config_text())
    custom = classify_camilla_config_text("""
devices:
  volume_limit: 0.0
  playback:
    channels: 2
    device: "hw:SomeDAC,0"
""")

    assert active["classification"] == "active_startup_candidate"
    assert active["active_split"]["mixer_output_channels"] == 4
    assert outputd["classification"] == "jts_outputd_stereo"
    assert custom["classification"] == "unknown_custom"
    assert custom["issues"][0]["code"] == "unknown_custom_camilla_config"


def test_classify_active_config_blocks_playback_split_channel_mismatch() -> None:
    text = _active_config_text().replace(
        'channels: 4\n    device: "hw:DAC8,0"',
        'channels: 3\n    device: "hw:DAC8,0"',
    )

    active = classify_camilla_config_text(text)

    assert active["classification"] == "active_startup_candidate"
    assert "active_playback_channels_mismatch" in {
        issue["code"] for issue in active["issues"]
    }


def test_classify_active_config_accepts_safe_dump_block_split_channels() -> None:
    text = _active_config_text().replace(
        "    channels: { in: 2, out: 4 }",
        "    channels:\n      in: 2\n      out: 4",
    )

    active = classify_camilla_config_text(text)

    assert active["active_split"]["mixer_output_channels"] == 4
    assert "active_split_output_channels_missing" not in {
        issue["code"] for issue in active["issues"]
    }


def test_classify_active_config_rejects_block_split_with_wrong_input_count() -> None:
    text = _active_config_text().replace(
        "    channels: { in: 2, out: 4 }",
        "    channels:\n      in: 999\n      out: 4",
    )

    active = classify_camilla_config_text(text)

    assert active["active_split"]["mixer_output_channels"] is None
    assert "active_split_output_channels_missing" in {
        issue["code"] for issue in active["issues"]
    }


def test_classify_active_config_blocks_missing_active_split() -> None:
    text = _active_config_text().split("mixers:", 1)[0]

    active = classify_camilla_config_text(text)

    assert active["classification"] == "active_startup_candidate"
    assert "active_split_missing" in {issue["code"] for issue in active["issues"]}


def test_program_config_mixer_satisfies_active_split_ecosystem_contract() -> None:
    """W6 hardware run 4 finding I, the environment-contract half: the
    renamed program-graph mixer (jasper.active_speaker.camilla_yaml's
    _emit_role_routed_mixer, now emitted as split_active_{way}way) must also
    satisfy _ACTIVE_SPLIT_RE -- the runtime ecosystem's OWN active-config
    recognizer -- not just resolve the pipeline's Mixer reference (pinned
    separately in tests/test_active_speaker_emit_gate.py). Drives the real
    emitter output through classify_camilla_config_text end to end, rather
    than the hand-built _active_config_text() fixture above, so a future
    rename that satisfies the pipeline but drifts from the ecosystem
    vocabulary would fail here."""
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    text = emit_active_speaker_program_config(
        preset,
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:CARD=DAC8x,DEV=0",
    )

    active = classify_camilla_config_text(text)

    assert active["classification"] == "active_startup_candidate"
    assert active["active_split"]["present"] is True
    assert active["active_split"]["way_count"] == 2
    assert active["active_split"]["mixer_output_channels"] == 2
    issue_codes = {issue["code"] for issue in active["issues"]}
    assert "active_split_missing" not in issue_codes
    assert "active_split_output_channels_missing" not in issue_codes
    assert "active_playback_channels_mismatch" not in issue_codes


def test_alsa_probe_failure_has_stable_issue_count() -> None:
    def missing_aplay(
        argv: list[str] | tuple[str, ...],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("aplay")

    report = probe_alsa_playback_devices(runner=missing_aplay)

    assert report["available"] is False
    assert report["issue_count"] == 1
    assert report["issues"][0]["code"] == "aplay_missing"


def test_probe_blocks_current_outputd_config_without_path_safety(
    tmp_path: Path,
) -> None:
    config = tmp_path / "outputd.yml"
    config.write_text(_outputd_config_text(), encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")

    report = probe_active_speaker_environment(
        statefile_path=statefile,
        runner=_runner,
        validate=_valid_config,
    )

    assert report["status"] == "blocked"
    assert report["camilla_config"]["classification"] == "jts_outputd_stereo"
    assert report["ok_to_load_active_config"] is False
    assert report["safe_playback"]["playback_allowed"] is False
    assert report["safe_playback"]["status"] == "not_implemented"
    assert {issue["code"] for issue in report["issues"]} >= {
        "path_safety_evidence_missing",
        "active_startup_candidate_required",
    }


def test_probe_operator_path_safety_still_requires_hardware_probe(
    tmp_path: Path,
) -> None:
    config = tmp_path / "active.yml"
    config.write_text(_active_config_text(), encoding="utf-8")
    evidence = _write_path_safety(tmp_path, OPERATOR_EVIDENCE_SOURCE)

    report = probe_active_speaker_environment(
        config_path=config,
        path_safety_evidence_path=evidence,
        runner=_runner,
        validate=_valid_config,
    )

    assert report["status"] == "blocked"
    assert report["load_gate"] == "hardware_probe_required"
    assert report["ok_to_load_active_config"] is False
    assert "path_safety_load_gate_not_ready" in {
        issue["code"] for issue in report["issues"]
    }


def test_probe_can_pass_when_active_config_and_hardware_evidence_are_valid(
    tmp_path: Path,
) -> None:
    config = tmp_path / "active.yml"
    config.write_text(_active_config_text(), encoding="utf-8")
    evidence = _write_path_safety(tmp_path, HARDWARE_PROBE_EVIDENCE_SOURCE)

    report = probe_active_speaker_environment(
        config_path=config,
        path_safety_evidence_path=evidence,
        runner=_runner,
        validate=_valid_config,
    )

    assert report["status"] == "pass"
    assert report["load_gate"] == "ready"
    assert report["ok_to_load_active_config"] is True
    assert report["blocker_count"] == 0
    assert report["safe_playback"]["playback_allowed"] is False
    assert report["safe_playback"]["load_gate"] == "ready"
    assert {
        gate["id"]: gate["passed"]
        for gate in report["safe_playback"]["required_gates"]
    } == {
        "active_startup_candidate": True,
        "validated_config": True,
        "hardware_probe_path_safety": True,
        "physical_channel_identity": False,
        "level_limited_tone_generator": False,
    }


def test_probe_custom_config_blocks_guided_active_flow(tmp_path: Path) -> None:
    config = tmp_path / "custom.yml"
    config.write_text(
        """
devices:
  volume_limit: 0.0
  playback:
    channels: 2
    device: "hw:SomeDAC,0"
""",
        encoding="utf-8",
    )
    evidence = _write_path_safety(tmp_path, HARDWARE_PROBE_EVIDENCE_SOURCE)

    report = probe_active_speaker_environment(
        config_path=config,
        path_safety_evidence_path=evidence,
        runner=_runner,
        validate=_valid_config,
    )

    assert report["camilla_config"]["classification"] == "unknown_custom"
    assert report["status"] == "blocked"
    assert "unknown_custom_camilla_config" in {
        issue["code"] for issue in report["issues"]
    }
