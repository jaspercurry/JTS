from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from jasper.correction.camilla_yaml import emit_correction_config
from jasper.correction.peq import PEQ
from jasper.dsp_apply import DspApplyState, dsp_write_epoch, record_dsp_apply_state
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND
from jasper.sound.profile import (
    ParametricBand,
    SimpleEq,
    SoundProfile,
    load_profile,
    load_profile_library,
    save_profile,
)
from jasper.sound.settings import SoundSettings, load_sound_settings
from jasper.web import sound_setup

from ._web_test_helpers import json_post_with_csrf, request_with_csrf


def _record_dsp_epoch(path: Path, op_id: str) -> None:
    record_dsp_apply_state(
        DspApplyState(
            schema_version=1,
            op_id=op_id,
            source="test",
            phase="done",
            result="success",
            started_at="2026-05-28T00:00:00Z",
            finished_at="2026-05-28T00:00:01Z",
            prior_config_path=None,
            candidate_config_path="/tmp/test.yml",
        ),
        state_path=path,
    )


class FakeCamilla:
    def __init__(self, current_path: str, *, fail_set: bool = False) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None
        self.set_calls: list[str] = []
        self.active_raw_values: list[str] = []
        self.fail_set = fail_set

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.loaded_path or self.current_path

    async def set_config_file_path(self, path: str, *, best_effort: bool = False) -> bool:
        self.set_calls.append(path)
        self.loaded_path = path
        if self.fail_set and not best_effort:
            raise RuntimeError("reload failed")
        return True

    async def set_active_config_raw(
        self, config: str, *, best_effort: bool = False,
    ) -> bool:
        self.active_raw_values.append(config)
        if self.fail_set and not best_effort:
            raise RuntimeError("live update failed")
        return True


class FakeCamillaWithoutLiveRaw:
    def __init__(self, current_path: str) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None
        self.set_calls: list[str] = []

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.loaded_path or self.current_path

    async def set_config_file_path(self, path: str, *, best_effort: bool = False) -> bool:
        self.set_calls.append(path)
        self.loaded_path = path
        return True


_SOUND_MODULE = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "sound-profile" / "js" / "main.js"
)
_SOUND_CSS = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "sound-profile" / "sound.css"
)
_SOUND_HARNESS = Path(__file__).resolve().parent / "js" / "sound_profile_harness.mjs"
_NODE = shutil.which("node")


def _start_sound_server(tmp_path: Path):
    server = sound_setup.make_server(
        ("127.0.0.1", 0),
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "sound_profiles.json",
        config_dir=tmp_path / "configs",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_index_html_renders_canonical_sound_page():
    html = sound_setup._index_html().decode()

    # Canonical design system + page shell.
    assert "/assets/app.css" in html
    assert "/assets/sound-profile/sound.css?v=" in html  # page CSS linked, not inlined
    assert "<style>" not in html
    assert 'class="app-header__title">Sound profile' in html

    # Off / Saved / Draft tabs are the live source (server-rendered chrome).
    assert 'id="tab-off"' in html
    assert 'id="tab-saved"' in html
    assert 'id="tab-draft"' in html

    # The editor itself is a static ES module (served + revalidated by nginx),
    # not inline script — same delivery model as /system/.
    assert '<script type="module" src="/assets/sound-profile/js/main.js">' in html
    assert "<script>" not in html  # no inline logic left in the page


def test_index_html_embeds_csrf_meta_for_json_posts():
    html = sound_setup._index_html("csrf-token").decode()
    # The token rides in the meta tag; the static module reads it and sends
    # X-CSRF-Token on every mutating POST.
    assert 'meta name="jts-csrf" content="csrf-token"' in html


def test_sound_module_preserves_editor_behaviour():
    """The EQ editor moved from inline _SOUND_JS into a static module. Guard
    the load-bearing pieces so the relocation can't silently drop them: the
    5-band Simple field names, the backend endpoints + epoch handshake, the
    CSRF-via-meta wiring, and no legacy prompt() flow."""
    js = _SOUND_MODULE.read_text()
    assert "sub_bass_db" in js
    assert "presence_db" in js
    for path in (
        "./preview", "./live-draft", "./apply",
        "./profiles/save", "./profiles/rename", "./profiles/delete",
    ):
        assert path in js, f"sound module no longer references {path}"
    assert "dsp_write_epoch: dspWriteEpoch" in js
    assert "function cancelLiveDrafts()" in js
    assert "jsonHeaders()" in js
    assert "meta[name=jts-csrf]" in js  # CSRF read from the tag, not substituted
    assert "Active crossover commissioning" in js
    assert "./active-speaker/environment" in js
    assert "./output-topology" in js
    assert "Check environment" in js
    assert "safe_playback" in js
    assert "Environment checks and staging will not play tones" in js
    assert "window.prompt" not in js


def test_sound_module_active_speaker_status_is_explicit_read_only():
    js = _SOUND_MODULE.read_text()

    assert "function refreshActiveSpeakerStatus()" in js
    assert "fetch('./active-speaker/environment'" in js
    assert "fetch('./active-speaker/safe-playback'" in js
    assert "fetch('./active-speaker/staged-config'" in js
    assert "fetch('./active-speaker/tone-targets'" in js
    assert "fetch('./active-speaker/stage-config'" in js
    assert "activeSpeakerPost('./active-speaker/arm', 'Arming')" in js
    assert "activeSpeakerPost('./active-speaker/stop', 'Stopping')" in js
    assert "fetch('./active-speaker/tone-plan'" in js
    assert "fetch('./active-speaker/play-tone'" in js
    assert "data-act=\"refresh-active-speaker\"" in js
    assert "data-act=\"arm-active-speaker\"" in js
    assert "data-act=\"stop-active-speaker\"" in js
    assert "data-act=\"stage-active-config\"" in js
    assert "data-act=\"prepare-active-tone\"" in js
    assert "data-act=\"verify-active-tone\"" in js
    assert "class=\"btn btn--danger\" data-act=\"stop-active-speaker\"" in js
    assert "activeSpeaker.playback" in js
    assert "'<details class=\"advanced\"' + (open ? ' open' : '')" in js
    assert "safe.playback_allowed ? 'Allowed' : 'Not allowed yet'" in js
    assert "Calibration level" in js
    assert "activeSpeakerLevelConfig()" in js
    assert "active-speaker-level" in js
    assert "Normal listening volume is untouched" in js
    assert "level_dbfs: activeSpeakerLevelConfig().value" in js
    assert "requested_level_dbfs" in js
    assert "isFinite(returnedLevel) ? returnedLevel" in js
    assert "function renderActiveSpeakerIssues(envIssues, sessionIssues)" in js
    assert "row[0] + ': ' + (issue.code || 'issue')" in js
    assert "function renderActiveSpeakerStagedConfig(staged)" in js
    assert "Protected startup config" in js
    assert "Staged startup" in js
    assert "Stage protected config" in js
    assert "This writes a candidate file only; it will not load CamillaDSP or play sound." in js
    assert "function renderActiveSpeakerPlan(plan)" in js
    assert "function renderActiveSpeakerPlayback(playback)" in js
    assert "Would play" in js
    assert "Verify tone artifact" in js
    assert "No audio was emitted by this backend." in js
    assert "No preset channel targets available." in js
    assert ">Prepare channel test</button>" not in js
    assert "Arming records the safety state only; it does not play sound." in js
    assert "Artifact checks are available" in js
    assert "explicit lab enablement" in js
    assert "reload CamillaDSP" in js
    assert "play tones" in js


def test_sound_module_output_topology_surface_is_no_audio_and_backend_owned():
    js = _SOUND_MODULE.read_text()

    assert "function renderOutputTopologySetup()" in js
    assert "function refreshOutputTopology(options)" in js
    assert "function saveOutputTopology()" in js
    assert "function updateOutputChannelIdentity(button)" in js
    assert "function updateOutputChannelProtection(button)" in js
    assert "function checkOutputPlaybackReadiness(button)" in js
    assert "fetch('./output-topology'" in js
    assert "fetch('./active-speaker/channel-identity'" in js
    assert "fetch('./active-speaker/channel-protection'" in js
    assert "fetch('./active-speaker/playback-readiness'" in js
    assert 'data-act="mark-output-identity"' in js
    assert 'data-act="mark-output-protection"' in js
    assert 'data-act="check-output-readiness"' in js
    assert "headers: jsonHeaders()" in js
    assert "Saving this map does not play sound or reload CamillaDSP." in js
    assert "Backend validation owns the final decision." in js
    assert "Physical verification is operator evidence." in js
    assert "Multi-DAC aggregate" in js
    assert "not enabled" in js
    assert "Mark verified" in js
    assert "Mark protection" in js
    assert "Check readiness" in js
    assert "Playback readiness" in js
    assert "Preconditions passed" in js
    assert "Verify artifact" in js
    assert "Play low-level test" in js
    assert "The last readiness check failed" in js
    assert "Save this output setup draft before recording physical verification evidence." in js
    assert "Sound tests remain disabled for this setup surface." in js
    assert "Setup template" in js
    assert "Mono active 2-way" in js
    assert "Stereo active 3-way" in js
    assert "Output setup template is a draft." in js
    assert "Starter stereo" not in js
    assert "Starter 2-way" not in js
    assert "protection_status: tweeter ? 'required_missing' : 'not_required'" in js
    assert "Saved output setup. No sound was played." in js


def test_active_speaker_environment_payload_uses_configured_evidence_path(
    monkeypatch,
):
    calls = {}

    def fake_probe(**kwargs):
        calls.update(kwargs)
        return {
            "status": "blocked",
            "load_gate": "path_safety_evidence_missing",
            "blocker_count": 2,
            "safe_playback": {"playback_allowed": False},
        }

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
        "/tmp/path-safety.json",
    )
    monkeypatch.setattr(
        "jasper.active_speaker.environment.probe_active_speaker_environment",
        fake_probe,
    )

    payload = sound_setup._active_speaker_environment_payload()

    assert payload["status"] == "blocked"
    assert calls == {
        "path_safety_evidence_path": "/tmp/path-safety.json",
    }


def test_active_speaker_safe_playback_payloads_are_no_audio(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        str(tmp_path / "tone-artifacts"),
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_environment_payload",
        lambda: {
            "status": "pass",
            "load_gate": "ready",
            "ok_to_load_active_config": True,
            "camilla_config": {
                "classification": "active_startup_candidate",
                "path": "/tmp/active.yml",
            },
            "safe_playback": {
                "status": "not_implemented",
                "playback_allowed": False,
            },
            "issues": [],
        },
    )

    armed = sound_setup._active_speaker_arm_payload()
    targets = sound_setup._active_speaker_tone_targets_payload()
    plan = sound_setup._active_speaker_tone_plan_payload({
        "side": "mono",
        "driver_role": "tweeter",
    })
    level_plan = sound_setup._active_speaker_tone_plan_payload({
        "side": "mono",
        "driver_role": "tweeter",
        "level_dbfs": -55,
    })
    playback = sound_setup._active_speaker_tone_playback_payload({
        "side": "mono",
        "driver_role": "tweeter",
        "level_dbfs": -55,
    })
    status = sound_setup._active_speaker_safe_playback_payload()
    stopped = sound_setup._active_speaker_stop_payload()

    assert armed["status"] == "armed"
    assert armed["playback_allowed"] is False
    assert targets["targets"]
    assert targets["calibration_level"]["test_signal"]["default_level_dbfs"] == -80.0
    assert plan["status"] == "ready"
    assert plan["would_play"] is False
    assert plan["target"]["driver_role"] == "tweeter"
    assert plan["channel_map"]["output_count"] == 2
    assert plan["calibration_level"]["test_signal"]["requested_level_dbfs"] == -80.0
    assert level_plan["tone"]["level_dbfs"] == -55.0
    assert level_plan["calibration_level"]["test_signal"]["requested_level_dbfs"] == -55.0
    assert playback["playback"]["status"] == "completed"
    assert playback["playback"]["audio_emitted"] is False
    assert playback["playback"]["artifact"]["channel_count"] == 2
    assert playback["playback"]["artifact"]["target_output_index"] == 1
    assert playback["session"]["playback"]["status"] == "completed"
    assert status["status"] == "armed"
    assert stopped["status"] == "stopped"
    assert stopped["playback"]["status"] == "stopped"
    assert stopped["session_id"] == armed["session_id"]


def test_active_speaker_playback_readiness_payload_is_no_audio(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        str(tmp_path / "tone-artifacts"),
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_environment_payload",
        lambda: {
            "status": "pass",
            "load_gate": "ready",
            "ok_to_load_active_config": True,
            "safe_playback": {
                "status": "not_implemented",
                "playback_allowed": False,
            },
            "issues": [],
        },
    )
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    },
                ],
            }
        ],
        "routing": {"main_left_group_id": "left"},
    })

    armed = sound_setup._active_speaker_arm_payload()
    readiness = sound_setup._active_speaker_playback_readiness_payload({
        "speaker_group_id": "left",
        "role": "woofer",
        "level_dbfs": -60,
    })
    artifact = sound_setup._active_speaker_tone_playback_payload({
        "speaker_group_id": "left",
        "role": "woofer",
        "level_dbfs": -60,
    })
    blocked_audio = sound_setup._active_speaker_tone_playback_payload({
        "speaker_group_id": "left",
        "role": "woofer",
        "level_dbfs": -60,
        "audio": True,
    })

    assert armed["status"] == "armed"
    assert readiness["status"] == "preconditions_passed"
    assert readiness["preconditions_passed"] is True
    assert readiness["playback_allowed"] is False
    assert readiness["would_play"] is False
    assert readiness["tone_playback_implemented"] is False
    assert readiness["target"]["physical_output_index"] == 0
    assert readiness["calibration_level"]["test_signal"]["requested_level_dbfs"] == -60
    assert artifact["plan"]["source"] == "output_topology"
    assert artifact["plan"]["target"]["output_index"] == 0
    assert artifact["playback"]["backend"] == "wav_artifact"
    assert artifact["playback"]["audio_emitted"] is False
    assert artifact["playback"]["artifact"]["target_output_index"] == 0
    assert blocked_audio["playback"]["status"] == "blocked"
    assert blocked_audio["playback"]["audio_emitted"] is False
    assert "audio_backend_not_enabled" in {
        issue["code"] for issue in blocked_audio["playback"]["issues"]
    }


def test_active_speaker_protection_and_stage_config_payloads_are_no_load(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH",
        str(tmp_path / "active_staged.yml"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE", "hw:DAC8,0")
    saved = sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "required_missing",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })

    blocked = sound_setup._active_speaker_stage_config_payload({})
    protected = sound_setup._active_speaker_channel_protection_save_payload({
        "speaker_group_id": "mono",
        "role": "tweeter",
        "protection_present": True,
    })
    staged = sound_setup._active_speaker_stage_config_payload({})
    loaded = sound_setup._active_speaker_staged_config_payload()

    assert saved["output_topology"]["status"] == "blocked"
    assert blocked["status"] == "blocked"
    assert "tweeter_protection_required" in {
        issue["code"] for issue in blocked["issues"]
    }
    assert protected["output_topology"]["status"] == "verified"
    assert staged["status"] == "staged"
    assert staged["config"]["basename"] == "active_staged.yml"
    assert staged["config"]["playback_device"] == "hw:DAC8,0"
    assert staged["config"]["tweeter_protective_highpass_hz"] == 5000
    assert staged["load"]["load_allowed"] is False
    assert Path(staged["config"]["path"]).exists()
    assert loaded["status"] == "staged"


def test_active_speaker_stage_config_rejects_non_string_playback_device() -> None:
    with pytest.raises(ValueError, match="playback_device must be a string"):
        sound_setup._active_speaker_stage_config_payload({
            "playback_device": {"device": "hw:DAC8,0"},
        })


def test_sound_output_topology_payload_is_no_audio_draft(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", "hifiberry_dac8x")
    monkeypatch.setenv("JASPER_AUDIO_DAC_CARD", "sndrpihifiberry")

    envelope = sound_setup._output_topology_payload()
    payload = envelope["output_topology"]

    assert payload["kind"] == OUTPUT_TOPOLOGY_KIND
    assert payload["status"] == "draft"
    assert payload["hardware"]["physical_output_count"] == 8
    assert envelope["clock_domain"]["status"] == "single_device_clock"
    assert envelope["clock_domain"]["multi_device_aggregate_supported"] is False
    assert payload["safety"]["sound_tests_allowed"] is False
    assert payload["evaluation"]["warnings"][0]["code"] == "no_speaker_groups"


def test_sound_output_topology_save_validates_and_persists_complete_contract(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    raw = {
        "output_topology": {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "living_room",
            "name": "Living room",
            "status": "draft",
            "hardware": {
                "device_id": "hifiberry_dac8x",
                "device_label": "HiFiBerry DAC8x",
                "physical_output_count": 8,
            },
            "speaker_groups": [
                {
                    "id": "left",
                    "label": "Left speaker",
                    "kind": "left",
                    "mode": "full_range_passive",
                    "channels": [
                        {
                            "role": "full_range",
                            "physical_output_index": 0,
                            "identity_verified": True,
                        }
                    ],
                }
            ],
            "routing": {"main_left_group_id": "left"},
        }
    }

    payload = sound_setup._save_output_topology_payload(raw)
    topology = payload["output_topology"]
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert topology["status"] == "verified"
    assert topology["evaluation"]["assigned_output_count"] == 1
    assert topology["safety"]["sound_tests_allowed"] is False
    assert saved["status"] == "verified"
    assert saved["speaker_groups"][0]["channels"][0]["human_output_label"] == (
        "DAC output 1"
    )
    assert payload["channel_identity"]["verified_channel_count"] == 1
    assert payload["clock_domain"]["status"] == "single_device_clock"


def test_sound_channel_identity_route_marks_saved_topology_only(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            }
        ],
        "routing": {"main_left_group_id": "left"},
    })

    payload = sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left",
        "role": "full_range",
        "identity_verified": True,
    })
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert payload["channel_identity"]["status"] == "verified"
    assert payload["channel_identity"]["verified_channel_count"] == 1
    assert payload["clock_domain"]["multi_device_aggregate_supported"] is False
    assert payload["output_topology"]["status"] == "verified"
    assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is True

    payload = sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left",
        "role": "full_range",
        "identity_verified": False,
    })
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert payload["channel_identity"]["status"] == "needs_verification"
    assert payload["channel_identity"]["verified_channel_count"] == 0
    assert payload["output_topology"]["status"] == "valid"
    assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is False


@pytest.mark.parametrize(
    "raw",
    [
        {"speaker_group_id": "left", "role": "full_range"},
        {
            "speaker_group_id": "left",
            "role": "full_range",
            "identity_verified": "false",
        },
        [
            {
                "speaker_group_id": "left",
                "role": "full_range",
                "identity_verified": True,
            }
        ],
    ],
)
def test_sound_channel_identity_save_requires_explicit_boolean(
    monkeypatch,
    tmp_path: Path,
    raw,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            }
        ],
        "routing": {"main_left_group_id": "left"},
    })

    with pytest.raises(ValueError, match="identity|object"):
        sound_setup._active_speaker_channel_identity_save_payload(raw)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is False


def test_sound_channel_identity_http_route_rejects_non_boolean_evidence(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            }
        ],
        "routing": {"main_left_group_id": "left"},
    })

    try:
        server, base = _start_sound_server(tmp_path)
    except PermissionError:
        pytest.skip("environment does not allow loopback test server bind")
    try:
        resp = json_post_with_csrf(
            base,
            "/active-speaker/channel-identity",
            {
                "speaker_group_id": "left",
                "role": "full_range",
                "identity_verified": "false",
            },
            expect_status=400,
        )
        payload = json.loads(resp.read().decode("utf-8"))
        saved = json.loads(path.read_text(encoding="utf-8"))

        assert "identity_verified must be a boolean" in payload["error"]
        assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_sound_output_topology_http_route_is_csrf_protected_and_no_audio(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", "hifiberry_dac8x")
    try:
        server, base = _start_sound_server(tmp_path)
    except PermissionError:
        pytest.skip("environment does not allow loopback test server bind")
    try:
        get_resp = urllib.request.urlopen(f"{base}/output-topology")
        get_payload = json.loads(get_resp.read().decode("utf-8"))
        assert get_payload["output_topology"]["status"] == "draft"

        post_resp = request_with_csrf(
            base,
            "/output-topology",
            json.dumps(get_payload["output_topology"]).encode("utf-8"),
            content_type="application/json",
        )
        post_payload = json.loads(post_resp.read().decode("utf-8"))
        assert post_payload["output_topology"]["safety"]["sound_tests_allowed"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_sound_module_treats_saved_tab_as_live_lane_with_flat_fallback():
    js = _SOUND_MODULE.read_text()
    set_view_start = js.index("function setView(v)")
    set_view_end = js.index("function applySavedSelection", set_view_start)
    set_view_body = js[set_view_start:set_view_end]
    reconcile_start = js.index("async function reconcileLiveSource()")
    reconcile_end = js.index("async function applyProfile", reconcile_start)
    reconcile_body = js[reconcile_start:reconcile_end]
    delete_start = js.index("async function deleteEntry(id)")
    delete_end = js.index("async function loadState()", delete_start)
    delete_body = js[delete_start:delete_end]
    load_start = js.index("async function loadState()")
    load_body = js[load_start:]

    assert "var DEFAULT_SAVED_ID = 'stock:flat';" in js
    assert "function selectedSavedEntry()" in js
    assert "function selectedSavedProfile()" in js
    assert "function requestLiveSource(options)" in js
    assert "function reconcileLiveSource()" in js
    assert "requestLiveSource({immediate: true});" in set_view_body
    assert "if (view === 'saved')" in reconcile_body
    assert "return applySavedSelection(options.okMsg, seq);" in reconcile_body
    assert "if (act === 'browse-presets') { setView('saved'); }" in js
    assert "selectedId = fallbackSavedId();" in delete_body
    assert "requestLiveSource({immediate: true});" in delete_body
    assert "selectedId = findIdFor(applied);" in load_body


def test_sound_module_replays_latest_tab_intent_after_apply_finishes():
    if _NODE is None:
        pytest.skip("node not on PATH")

    proc = subprocess.run(
        [_NODE, str(_SOUND_HARNESS), str(_SOUND_MODULE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["applyProfileIds"] == ["stock:flat"]
    assert out["liveDraftRequests"] == 1
    assert out["liveDraftEpoch"] == "apply-1"
    assert out["liveTabMarked"] is True


def test_sound_css_marks_live_sources_with_red_dots():
    js = _SOUND_MODULE.read_text()
    css = _SOUND_CSS.read_text()

    assert "btn.classList.toggle('is-live', v === view);" in js
    assert ".app-header__tabs .segmented__btn.is-live::after" in css
    assert ".profile-row__dot--on" in css
    assert "background: var(--destructive);" in css


def test_sound_module_draws_only_expanded_peq_component_curve():
    js = _SOUND_MODULE.read_text()
    render_start = js.index("function renderGraph(payload, enabled)")
    render_end = js.index("  // Render the graph", render_start)
    render_body = js[render_start:render_end]

    assert "function expandedPeqBandIndex()" in js
    assert "item.index === expandedBand" in render_body
    assert "component selected" in render_body
    assert "(comp.advanced || []).forEach" not in render_body
    assert "drawPath(comp.curve" not in render_body
    assert "drawPath(comp.simple" not in render_body
    # Band dots are anchored to the summed curve and only drawn when enabled.
    assert "if (enabled) html += drawBandMarkers(curvePts);" in render_body


def test_sound_module_anchors_band_dots_to_the_summed_curve():
    js = _SOUND_MODULE.read_text()
    markers_start = js.index("function drawBandMarkers(summed)")
    markers_end = js.index("function expandedPeqBandIndex()", markers_start)
    markers_body = js[markers_start:markers_end]

    assert "var expandedBand = expandedPeqBandIndex();" in markers_body
    assert "i === expandedBand" in markers_body
    # The dot sits ON the curve (summedDbAt), not at the band's raw gain — the
    # fix for the shelf/cut "dot floats off the line" bug.
    assert "summedDbAt(summed, fx)" in markers_body
    assert "band-dot" in markers_body
    # Only the expanded band adds a guide line + width shading; no per-band
    # marker lines clutter the default view.
    assert "band-guide" in markers_body
    assert "band-marker" not in markers_body
    assert "(b.type || 'Peaking') === 'Peaking'" in markers_body

    css = _SOUND_CSS.read_text()
    assert ".band-guide" in css
    assert ".band-marker " not in css
    assert ".band-width.selected" not in css


def test_sound_module_reset_draft_and_simple_zero_detent():
    """Draft reset is the user-facing revert action, and Simple sliders get a
    tiny release-time zero detent so neutral is easy without per-band buttons."""
    js = _SOUND_MODULE.read_text()
    assert "Reset draft" in js
    assert 'data-act="reset-draft"' in js
    assert "function resetDraft()" in js
    assert "Discard" not in js
    assert "var ZERO_DETENT_DB = 0.1;" in js
    assert "Math.abs(next) <= ZERO_DETENT_DB" in js
    assert "ev.target.getAttribute('data-field')" in js


def test_sound_readouts_are_not_fake_edit_controls():
    """Readouts are display-only; exact numeric editing was intentionally not
    shipped, so they must not masquerade as text-edit buttons."""
    js = _SOUND_MODULE.read_text()
    css = _SOUND_CSS.read_text()
    assert "range__readout-value" in js
    assert "simple-col__readout-value" in js
    assert "readout-btn" not in js
    assert "readout-input" not in js
    assert "cursor: text" not in css


def test_sound_module_prefers_explicit_profile_identity_then_stock_matches():
    js = _SOUND_MODULE.read_text()
    fn_start = js.index("function findIdFor(profile)")
    fn_end = js.index("function sourceProfile()", fn_start)
    body = js[fn_start:fn_end]

    explicit_identity = body.index("profile.profile_id && entryById(profile.profile_id)")
    stock_match = body.index("e.kind === 'stock'")
    custom_match = body.index("e.kind === 'custom'")

    assert explicit_identity < stock_match < custom_match


def test_state_payload_contains_stock_curves_profiles_and_preview(tmp_path: Path):
    payload = sound_setup._state_payload(
        SoundProfile(curve_id="harman"),
        library_path=tmp_path / "sound_profiles.json",
        include_library=True,
    )

    assert [curve["id"] for curve in payload["curves"]] == ["flat", "harman", "bk"]
    assert [entry["id"] for entry in payload["profile_library"][:3]] == [
        "stock:flat",
        "stock:harman",
        "stock:bk",
    ]
    assert payload["profile"]["curve_id"] == "harman"
    assert payload["preview"]
    assert payload["components"]["curve"]
    assert payload["limits"]["max_parametric_bands"] == 8
    # Cut-filter Q ceiling is exposed so the UI's Width slider can bound HP/LP.
    assert payload["limits"]["cut_max_q"] == 1.4
    assert payload["headroom_db"] > 0


def test_sound_module_hides_uncontrollable_band_controls():
    js = _SOUND_MODULE.read_text()
    band_row = js[js.index("function bandRow(band, index)"):js.index("function typeBtn(")]
    # All six band types are offered.
    for t in ("Lowshelf", "Peaking", "Highshelf", "Highpass", "Lowpass", "Notch"):
        assert "typeBtn('" + t + "'" in band_row
    # Gain is hidden for cut/notch (no gain term); Width is hidden for shelves
    # (slope fixed at 6 dB/oct, so the control would be inert).
    assert "gainless ? '' : rangeRow('Gain'" in band_row
    assert "shelf ? '' : rangeRow('Width'" in band_row


def test_sound_module_bounds_cut_filter_width_with_cut_max_q():
    js = _SOUND_MODULE.read_text()
    # The Width slider and its clamp use a per-type ceiling for HP/LP, sourced
    # from limits.cut_max_q (SSOT in jasper/sound/profile.py CUT_MAX_Q).
    assert "function bandQMax(type)" in js
    assert "limits.cut_max_q" in js
    assert "rangeRow('Width', band.q, limits.min_q, bandQMax(type)" in js
    assert "clamp(ev.target.value, limits.min_q, bandQMax(band.type))" in js


def test_state_filter_count_signals_effective_eq_for_initial_view():
    # filter_count drives the page's initial Off-vs-Saved tab: 0 means no
    # effective EQ (bypassed OR flat) -> open Off; >0 -> open Saved with the
    # applied profile marked active.
    assert sound_setup._state_payload(SoundProfile())["filter_count"] == 0
    assert sound_setup._state_payload(
        SoundProfile(enabled=False, curve_id="harman")
    )["filter_count"] == 0
    assert sound_setup._state_payload(
        SoundProfile(curve_id="harman")
    )["filter_count"] > 0
    assert sound_setup._state_payload(
        SoundProfile(simple_eq=SimpleEq(bass_db=3.0))
    )["filter_count"] > 0
    # A cuts-only EQ has zero headroom but is still an effective EQ -- this is
    # why the signal is filter_count, not headroom_db.
    cuts_only = sound_setup._state_payload(SoundProfile(simple_eq=SimpleEq(mid_db=-3.0)))
    assert cuts_only["headroom_db"] == 0
    assert cuts_only["filter_count"] > 0


async def test_apply_profile_preserves_active_room_peqs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([PEQ(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"

    payload = await sound_setup._apply_profile(
        SoundProfile(curve_id="bk", simple_eq=SimpleEq(treble_db=1.5)),
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.loaded_path is not None
    generated = Path(fake.loaded_path).read_text()
    assert Path(fake.loaded_path).name == "sound_current.yml"
    assert "room_peq_1:" in generated
    assert "sound_curve_bk_bass:" in generated
    assert payload["preserved_room_peqs"] == 1
    assert payload["dsp_write_epoch"] == payload["last_dsp_apply"]["op_id"]
    assert load_profile(profile_path).curve_id == "bk"


async def test_apply_profile_no_trim_by_default_so_boosts_boost(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([]))
    fake = FakeCamilla(str(current))

    payload = await sound_setup._apply_profile(
        SoundProfile(simple_eq=SimpleEq(bass_db=6.0)),
        profile_path=tmp_path / "sound_profile.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    generated = Path(fake.loaded_path).read_text()
    assert "sound_simple_bass:" in generated
    assert "sound_preamp" not in generated  # default: boosts boost
    assert payload["output_trim_db"] == 0


async def test_apply_profile_emits_output_trim_when_match_loudness_on(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    settings_path = tmp_path / "sound_settings.json"
    settings_path.write_text('{"match_loudness": true}')
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([]))
    fake = FakeCamilla(str(current))

    payload = await sound_setup._apply_profile(
        SoundProfile(simple_eq=SimpleEq(bass_db=6.0)),
        profile_path=tmp_path / "sound_profile.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    generated = Path(fake.loaded_path).read_text()
    assert "sound_preamp:" in generated  # loudness comp applied as output trim
    assert payload["output_trim_db"] > 0


async def test_apply_settings_reapplies_with_trim_without_restamping_profile(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    # An applied profile with a boost, stamped at a fixed time.
    save_profile(
        SoundProfile(
            simple_eq=SimpleEq(bass_db=6.0), updated_at="2020-01-01T00:00:00+00:00"
        ),
        profile_path,
    )

    payload = await sound_setup._apply_settings(
        SoundSettings(match_loudness=True),
        profile_path=profile_path,
        library_path=tmp_path / "lib.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    generated = Path(fake.loaded_path).read_text()
    assert "sound_preamp:" in generated  # match-loudness trim applied
    assert payload["output_trim_db"] > 0
    assert "warning" not in payload
    assert load_sound_settings(settings_path).match_loudness is True
    # The profile JSON is untouched: not re-stamped, not overwritten.
    assert load_profile(profile_path).updated_at == "2020-01-01T00:00:00+00:00"


async def test_apply_settings_warns_but_keeps_settings_on_reapply_failure(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([PEQ(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current), fail_set=True)  # reload fails

    payload = await sound_setup._apply_settings(
        SoundSettings(headroom_trim_db=6.0),
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "lib.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert "warning" in payload
    # Settings persist despite the re-apply failure (no revert, no silent loss).
    assert load_sound_settings(settings_path).headroom_trim_db == 6.0


async def test_audition_profile_loads_draft_without_persisting(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([PEQ(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    # match-loudness on -> the audition gets a loudness-weighted output trim.
    settings_path = tmp_path / "sound_settings.json"
    settings_path.write_text('{"match_loudness": true}')
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    draft = SoundProfile(
        curve_id="harman",
        parametric_bands=(ParametricBand(freq_hz=1000.0, gain_db=3.0, q=1.0),),
    )

    payload = await sound_setup._audition_profile(
        draft,
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.loaded_path is not None
    assert Path(fake.loaded_path).name == "sound_audition.yml"
    generated = Path(fake.loaded_path).read_text()
    assert "sound_curve_harman_bass:" in generated
    assert "sound_advanced_1:" in generated
    assert "sound_preamp:" in generated  # match-loudness trim applied
    assert payload["audition_profile"]["curve_id"] == "harman"
    assert payload["output_trim_db"] > 0
    assert payload["dsp_write_epoch"] == payload["last_dsp_apply"]["op_id"]
    assert not profile_path.exists()


async def test_live_draft_profile_updates_active_config_without_persisting(
    tmp_path: Path,
    monkeypatch,
):
    state_path = tmp_path / "dsp_apply_state.json"
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state_path))
    _record_dsp_epoch(state_path, "epoch-1")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(emit_correction_config([PEQ(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    draft = SoundProfile(curve_id="harman", simple_eq=SimpleEq(bass_db=2.0))

    payload = await sound_setup._live_draft_profile(
        draft,
        expected_dsp_write_epoch=dsp_write_epoch(),
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.set_calls == []
    assert len(fake.active_raw_values) == 1
    assert "sound_curve_harman_bass:" in fake.active_raw_values[0]
    assert "room_peq_1:" in fake.active_raw_values[0]
    # Default settings -> no output trim, so boosts boost (no global preamp).
    assert "sound_preamp" not in fake.active_raw_values[0]
    assert payload["live_status"] == "live"
    assert payload["live_method"] == "active_config_raw"
    assert payload["dsp_write_epoch"] == "epoch-1"
    assert payload["preserved_room_peqs"] == 1
    assert payload["output_trim_db"] == 0
    assert not profile_path.exists()


async def test_live_draft_profile_skips_stale_epoch_without_touching_audio(
    tmp_path: Path,
    monkeypatch,
):
    state_path = tmp_path / "dsp_apply_state.json"
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(state_path),
    )
    _record_dsp_epoch(state_path, "newer-apply")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(emit_correction_config([]))
    fake = FakeCamilla(str(current))
    draft = SoundProfile(curve_id="bk", simple_eq=SimpleEq(treble_db=1.0))

    payload = await sound_setup._live_draft_profile(
        draft,
        expected_dsp_write_epoch="older-apply",
        profile_path=tmp_path / "sound_profile.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.active_raw_values == []
    assert fake.set_calls == []
    assert payload["live_status"] == "stale"
    assert payload["live_method"] == "skipped_stale_epoch"
    assert payload["dsp_write_epoch"] == "newer-apply"


async def test_live_draft_profile_reports_unavailable_without_reload(
    tmp_path: Path,
    monkeypatch,
):
    state_path = tmp_path / "dsp_apply_state.json"
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state_path))
    _record_dsp_epoch(state_path, "epoch-1")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(emit_correction_config([]))
    fake = FakeCamillaWithoutLiveRaw(str(current))
    draft = SoundProfile(curve_id="bk", simple_eq=SimpleEq(treble_db=1.0))

    payload = await sound_setup._live_draft_profile(
        draft,
        expected_dsp_write_epoch="epoch-1",
        profile_path=tmp_path / "sound_profile.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.loaded_path is None
    assert fake.set_calls == []
    assert payload["live_status"] == "unavailable"
    assert payload["live_method"] == "active_config_raw_unavailable"


async def test_apply_profile_rejects_unknown_active_config(tmp_path: Path):
    current = tmp_path / "custom.yml"
    current.write_text("# handmade\n")
    fake = FakeCamilla(str(current))

    try:
        await sound_setup._apply_profile(
            SoundProfile(simple_eq=SimpleEq(bass_db=1.0)),
            profile_path=tmp_path / "sound_profile.json",
            config_dir=tmp_path / "configs",
            camilla_factory=lambda: fake,
        )
    except RuntimeError as e:
        assert "custom config" in str(e)
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected unknown config rejection")


async def test_apply_profile_rolls_back_when_reload_fails(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([PEQ(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current), fail_set=True)

    try:
        await sound_setup._apply_profile(
            SoundProfile(simple_eq=SimpleEq(bass_db=1.0)),
            profile_path=tmp_path / "sound_profile.json",
            config_dir=config_dir,
            camilla_factory=lambda: fake,
        )
    except RuntimeError as e:
        assert "reload failed" in str(e)
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected reload failure")

    assert fake.set_calls[-1] == str(current)
    assert not (tmp_path / "sound_profile.json").exists()


def test_profile_library_route_helpers_create_rename_delete(tmp_path: Path):
    library_path = tmp_path / "sound_profiles.json"

    created = sound_setup.save_named_profile(
        SoundProfile(curve_id="harman"),
        name="Library Test",
        path=library_path,
    )
    renamed = sound_setup.rename_named_profile(
        created.id,
        name="Library Renamed",
        path=library_path,
    )
    sound_setup.delete_named_profile(renamed.id, path=library_path)

    assert load_profile_library(library_path) == ()
