from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from jasper.camilla_config_contract import PeqFilter
from jasper.dsp_apply import DspApplyState, dsp_write_epoch, record_dsp_apply_state
from jasper.output_topology import (
    DUAL_APPLE_ACTIVE_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
)
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputCardFact,
    classify_output_cards,
    write_state as write_output_hardware_state,
)
from jasper.sound.camilla_yaml import emit_sound_config
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


def _room_config(peqs: list[PeqFilter] | None = None) -> str:
    return emit_sound_config(
        SoundProfile(enabled=False),
        room_peqs=peqs or [],
    )


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
_ACTIVE_SPEAKER_UI_MODULE = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "sound-profile" / "js" / "active-speaker-ui.js"
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


def test_index_html_delegates_content_dsp_when_bonded_follower(monkeypatch):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    monkeypatch.setattr(
        sound_setup,
        "bonded_follower_leader_web_url",
        lambda path="/": "http://jts3.local/sound/",
    )

    html = sound_setup._index_html("csrf-token").decode()

    assert "Sound is controlled by the pair leader" in html
    assert "http://jts3.local/sound/" in html
    assert "/assets/sound-profile/js/main.js" not in html
    assert 'meta name="jts-csrf" content="csrf-token"' in html


def test_bonded_follower_rejects_content_dsp_mutations(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    try:
        server, base = _start_sound_server(tmp_path)
    except PermissionError:
        pytest.skip("environment does not allow loopback test server bind")
    try:
        resp = json_post_with_csrf(
            base,
            "/settings",
            {},
            expect_status=409,
        )
        payload = json.loads(resp.read().decode("utf-8"))
        assert "controlled on the pair leader" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_index_html_embeds_csrf_meta_for_json_posts():
    html = sound_setup._index_html("csrf-token").decode()
    # The token rides in the meta tag; the static module reads it and sends
    # X-CSRF-Token on every mutating POST.
    assert 'meta name="jts-csrf" content="csrf-token"' in html


def test_sound_active_speaker_ui_helpers_are_pure_module_boundary():
    js = _ACTIVE_SPEAKER_UI_MODULE.read_text()

    assert "export function activeSpeakerStepState" in js
    assert "export function defaultActiveSpeakerStep" in js
    assert "export function playbackResultMessage" in js
    assert "querySelector" not in js
    assert "document." not in js
    assert "fetch(" not in js
    assert "explicit lab backend" not in js


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
    assert "Active crossover setup" in js
    assert "/assets/sound-profile/js/active-speaker-ui.js" in js
    assert "./active-speaker/prepare-driver-test" in js
    assert "./active-speaker/measurements" in js
    assert "./active-speaker/baseline-profile" in js
    assert "./output-topology" in js
    assert "Test each driver" in js
    assert "Validate and apply" in js
    assert "Save active profile" in js
    assert "Build the speaker layout, add crossover info, confirm DAC outputs" in js
    assert "function defaultOutputStep()" in js
    assert "return defaultActiveSpeakerStep(outputStepContext(currentOutputTopology()));" in js
    helper_js = _ACTIVE_SPEAKER_UI_MODULE.read_text()
    assert "if (!ctx.driverResearchSatisfied) return 'research';" in helper_js
    assert "if (!ctx.outputIdentityComplete) return 'map';" in helper_js
    assert "if (!ctx.driverMeasurementsComplete) return 'safety';" in helper_js
    assert "return 'profile';" in helper_js
    assert "Finish the current card before opening" in js
    assert "output-step__chevron" in js
    assert "querySelectorAll('.output-step[open]')" in js
    assert "window.prompt" not in js


def test_sound_module_active_speaker_status_is_explicit_read_only():
    js = _SOUND_MODULE.read_text()

    assert 'from "/assets/sound-profile/js/active-speaker-ui.js"' in js
    assert "function refreshActiveSpeakerStatus()" not in js
    assert "fetch('./active-speaker/environment'" not in js
    assert "fetch('./active-speaker/safe-playback'" not in js
    assert "fetch('./active-speaker/staged-config'" not in js
    assert "fetch('./active-speaker/calibration-level'" in js
    assert "fetch('./active-speaker/bringup-preflight'" not in js
    assert "fetch('./active-speaker/startup-load'" in js
    assert "fetch('./active-speaker/tone-targets'" not in js
    assert "fetch('./active-speaker/commissioning-rehearsal'" not in js
    assert "fetch('./active-speaker/prepare-driver-test'" in js
    assert "fetch('./active-speaker/stage-config'" not in js
    assert "fetch('./active-speaker/check-path-safety'" not in js
    assert "fetch('./active-speaker/load-startup-config'" not in js
    assert "fetch('./active-speaker/rollback-startup-config'" not in js
    assert "activeSpeakerPost('./active-speaker/arm', 'Opening test controls')" not in js
    assert "function activeSpeakerPost(" not in js
    assert "function stopActiveSpeakerTest()" in js
    assert "fetch('./active-speaker/stop'" in js
    assert "fetch('./active-speaker/play-tone'" in js
    assert "fetch('./active-speaker/floor-audio-result'" in js
    assert "fetch('./active-speaker/driver-measurement'" in js
    assert "fetch('./active-speaker/summed-test'" in js
    assert "fetch('./active-speaker/summed-validation'" in js
    assert "fetch('./active-speaker/design-draft'" in js
    assert "fetch('./active-speaker/crossover-preview'" in js
    assert "fetch('./active-speaker/measurements'" in js
    assert "fetch('./active-speaker/baseline-profile'" in js
    assert "data-act=\"refresh-active-speaker\"" not in js
    assert "data-act=\"save-driver-design\"" in js
    assert "data-act=\"prepare-crossover-preview\"" in js
    assert "Save crossover settings" in js
    assert "Prepare crossover preview" in js
    assert "savedStatus === 'ready_for_review' && !driverResearch.dirty" in js
    assert "function driverResearchCanPreparePreview()" in js
    assert "function driverResearchStepSatisfied()" in js
    assert "driverResearchSatisfied: driverResearchStepSatisfied()" in js
    assert "if (!ctx.driverResearchSatisfied) return 'research';" in (
        _ACTIVE_SPEAKER_UI_MODULE.read_text()
    )
    assert "Driver details are optional for now. Continue with output mapping." in js
    assert "saved driver info" in js
    assert "Builds the crossover plan from your saved settings. No sound plays." in js
    assert "data-act=\"stop-active-speaker\"" in js
    assert "data-act=\"arm-active-speaker\"" not in js
    assert "data-act=\"stage-active-config\"" not in js
    assert "data-act=\"check-active-path-safety\"" not in js
    assert "active-speaker/check-path-safety" not in js
    assert "data-act=\"load-active-startup\"" not in js
    assert "data-act=\"rollback-active-startup\"" not in js
    assert "data-act=\"prepare-active-tone\"" not in js
    assert "data-act=\"verify-active-tone\"" not in js
    assert "class=\"btn btn--danger\" data-act=\"stop-active-speaker\"" in js
    assert "activeSpeaker.playback" not in js
    assert "var activeSpeakerSetupOpen = false;" in js
    assert "'<details class=\"advanced\" data-active-speaker-setup' + (open ? ' open' : '')" in js
    assert "activeSpeakerSetupOpen = !!ev.target.open;" in js
    assert "Getting ' + label + ' ready. No sound will play yet." in js
    assert "Ready to test ' + label + '. Start at the quietest level." in js
    assert "Test progress" in js
    assert "activeSpeakerLevelConfig()" in js
    assert "active-speaker-level__bar--running" in js
    assert "id=\"active-speaker-level\"" not in js
    assert "data-act=\"active-level\"" not in js
    assert "Back to quiet" not in js
    assert "Raise toward audible" not in js
    assert "Mic reading dBFS" not in js
    assert "data-act=\"active-auto-level\"" not in js
    assert "activeSpeakerAutoLevelLabel(autoLevel)" not in js
    assert "action: 'observe'" not in js
    assert "action: 'auto_step'" in js
    assert "Normal listening volume is untouched" not in js
    assert "The mic reading helps JTS decide whether to hold, lower, or raise" not in js
    assert "automatically tries a little louder" in js
    assert "Normal listening volume is untouched" not in js
    assert "if (requestedLevel != null) body.level_dbfs = requestedLevel" not in js
    assert "level_dbfs: requestedLevel == null ? cfg.value : requestedLevel" not in js
    assert "requested_level_dbfs" in js
    assert "friendlySetupIssue(issue)" in js
    assert "Test each driver" in js
    assert "active-speaker/prepare-driver-test" in js
    assert "syncPreparedOutputTopology(payload)" in js
    assert "fetch('./active-speaker/stage-config'" not in js
    assert "fetch('./active-speaker/check-path-safety'" not in js
    assert "fetch('./active-speaker/load-startup-config'" not in js
    assert "Getting ' + label + ' ready. No sound will play yet." in js
    assert "Choose first driver" in js
    assert "Choose the driver you want to hear first. JTS will prepare the quiet test before any sound can play." in js
    assert "Listen for this driver" in js
    assert "Exit test setup" not in js
    assert "No sound played. ' + e.message" in js
    assert "Listen for this driver" in js
    assert "active-speaker-actions--driver-test" in js
    assert "data-act=\"active-floor-result\"" in js
    assert "data-act=\"record-summed-validation\"" in js
    assert "data-act=\"compile-baseline-profile\"" in js
    assert "data-act=\"apply-baseline-profile\"" in js
    assert "I hear this driver" in js
    assert "I did not hear anything" not in js
    assert "Wrong driver" in js
    assert "Too loud / stop" not in js
    assert "driver-test result" in js
    assert "Did this driver make the sound?" not in js
    assert "If you hear nothing, wait" not in js
    assert "active-speaker/check-path-safety" not in js
    assert "Exit driver test setup and restore the previous DSP setup?" not in js
    assert "function renderActiveSpeakerPlan(plan)" not in js
    assert "function renderActiveSpeakerPlayback(playback)" not in js
    assert "Would play" not in js
    assert "Verify tone artifact" not in js
    assert "No audio was emitted by this backend." not in js
    assert "No preset channel targets available." not in js
    assert ">Prepare channel test</button>" not in js
    assert "No sound is playing" in js


def test_sound_module_output_topology_surface_is_no_audio_and_backend_owned():
    js = _SOUND_MODULE.read_text()

    assert "function renderOutputTopologySetup()" in js
    assert "function refreshOutputTopology(options)" in js
    assert "function saveOutputTopology(options)" in js
    assert "function updateOutputChannelIdentity(button)" in js
    assert "function saveOutputChannelProtectionState(groupId, role, nextStatus)" not in js
    assert "function outputReadinessFromPreparedDriver(payload, button)" in js
    assert "function checkOutputPlaybackReadiness(button)" in js
    assert "fetch('./output-topology'" in js
    assert "fetch('./active-speaker/channel-identity'" in js
    assert "fetch('./active-speaker/channel-protection'" not in js
    assert "fetch('./active-speaker/prepare-driver-test'" in js
    assert "fetch('./active-speaker/playback-readiness'" not in js
    assert 'data-act="mark-output-identity"' in js
    assert 'data-act="check-output-readiness"' in js
    assert 'data-protection-required="' not in js
    assert 'data-protection-status="' not in js
    assert "headers: jsonHeaders()" in js
    assert "Saved speaker layout. No sound was played." in js
    assert "Confirm each DAC output after you check the wiring." in js
    assert "Multi-DAC aggregate" in js
    assert "Composite clock" in js
    assert "supported" in js
    assert "needs attention" not in js
    assert "Confirm output" in js
    assert "'✓ ' + humanRole(channel.role) + ' confirmed'" in js
    assert "'Test ' + humanRole(channel.role)" in js
    assert "Continue to Test each driver; JTS will start it very quiet" in js
    assert "Getting ' + label + ' ready. No sound will play yet." in js
    assert "Hardware protected" not in js
    assert "Use software guard" not in js
    assert "software_guard_requested" in js
    assert 'data-act="mark-output-protection"' not in js
    assert "function updateOutputChannelProtection(button)" not in js
    assert "Check readiness" not in js
    assert "Playback readiness" not in js
    assert "Change protection" not in js
    assert "Change quiet-start" not in js
    assert "Use quiet-start" not in js
    assert "Use for first test" not in js
    assert "protected startup DSP" not in js
    assert "function renderOutputReadinessSummary(readiness)" in js
    assert "function renderOutputReadinessBlockers(readiness)" in js
    assert "function outputCurrentLevelAtFloor()" not in js
    assert "function outputFloorAudioConfirmedForReadiness(readiness)" not in js
    assert "function quietStartTargetLabel(target)" in js
    assert "function readinessTargetLockReason(readiness)" in js
    assert "How to continue" in js
    assert "This driver cannot be tested from here yet. Choose a woofer, mid, or subwoofer driver to continue." in js
    assert "Starting with the quietest short pulse." in js
    assert "Heard ' + targetLabel" in js
    assert "Test progress" in js
    assert "Role policy" not in js
    assert "Preconditions passed" not in js
    assert "Generate WAV (no sound)" not in js
    assert ">Start quiet test</button>" in js
    assert ">I hear this driver</button>" in js
    assert "Driver tests are not available on this install yet." in js
    assert "playbackResultMessage(playback, undefined, friendlySetupReason)" in js
    assert "Playback: ' + (issue.code" not in js
    assert "JTS could not get the test ready. No sound was played." in js
    assert "Save this speaker layout draft before confirming outputs." in js
    assert "Main speakers" in js
    assert "Speaker count" in js
    assert "Speaker type" in js
    assert "var outputTemplateDraftAxes = {layout: '', speakerMode: ''};" in js
    assert "function outputTemplateChoiceDisabled(count, axis, value, axes)" in js
    assert "function outputTemplateUnavailableReason(template, topology, hasSubwoofer)" in js
    assert "This install can test and apply up to " in js
    assert "Subwoofer active profiles are not available on this install yet." in js
    assert "Choose passive, active 2-way, or active 3-way to continue." in js
    assert "Refresh hardware to start a speaker layout." in js
    assert "renderOutputHardwareRefresh() +" in js
    assert "Test each driver" in js
    assert "data-act=\"output-template-axis\"" in js
    assert "output-template-grid" not in js
    assert "Save output map" not in js
    assert "Output setup" not in js
    assert "Mono active 2-way" in js
    assert "Stereo active 3-way" in js
    assert "Speaker layout is a draft." in js
    assert "active-speaker-issues--warning" in js
    assert "renderIssueList(issues, 5)" in js
    assert "function crossoverPreviewDisplayStatus(payload)" in js
    assert "JTS still checks the setup before any sound." in js
    assert "Starter stereo" not in js
    assert "Starter 2-way" not in js
    assert "protection_status: tweeter ? 'required_missing' : 'not_required'" in js
    assert "Saved speaker layout. No sound was played." in js


def test_active_speaker_setup_copy_has_no_backend_jargon():
    """The /sound/ active-speaker flow is a guided consumer setup, not an
    engineering console. User-facing copy must never leak backend vocabulary
    (CamillaDSP/YAML, "protected"/"safe path", rollout "slice", raw "evidence")
    and friendlySetupReason must never echo a raw snake_case code. See AGENTS.md
    "Web wizard conventions" and the active-crossover flow simplification."""
    js = _SOUND_MODULE.read_text()
    helper_js = _ACTIVE_SPEAKER_UI_MODULE.read_text()

    # No backend vocabulary in any user-visible string.
    for jargon in (
        "No YAML",
        "CamillaDSP baseline YAML",
        "A durable CamillaDSP baseline",
        "The baseline YAML is saved",
        "reloads CamillaDSP",
        "in this slice",
        "missing playback evidence",
        "the protected test setup",
        "the safe audio path",
        "safe test setup",
        "Polarity or delay issue",
        "was blocked before sound could play",
    ):
        assert jargon not in js, f"backend jargon leaked into main.js: {jargon!r}"

    # friendlySetupReason must collapse code-like strings to a calm sentence
    # instead of echoing the raw identifier (the old `text.replace(/_/g,' ')`).
    assert "return raw.replace(/_/g, ' ')" not in js
    assert "return outcome.replace(/_/g, ' ')" not in js
    assert "One setup step still needs finishing. Choose the driver again to continue." in js

    # The new consumer copy is present and stable.
    assert "Builds the crossover plan from your saved settings. No sound plays." in js
    assert "Save the measured crossover as your active speaker profile. No sound plays." in js
    assert "Your active speaker profile, built from the saved crossover and driver checks." in js
    assert "Your active speaker profile is saved. Apply it to start using it." in js
    assert "Sounds hollow or thin" in js

    # The pure vocabulary module owns the no-sound fallbacks and stays actionable.
    assert "Choose the driver again to try." in helper_js
    assert "did not complete" not in helper_js


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
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        str(tmp_path / "tone-artifacts"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", "wav_artifact")
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
    guarded = sound_setup._active_speaker_calibration_level_payload({
        "action": "set",
        "level_dbfs": -55,
    })
    plan = sound_setup._active_speaker_tone_plan_payload({
        "side": "mono",
        "driver_role": "tweeter",
    })
    level_plan = sound_setup._active_speaker_tone_plan_payload({
        "side": "mono",
        "driver_role": "tweeter",
    })
    playback = sound_setup._active_speaker_tone_playback_payload({
        "side": "mono",
        "driver_role": "tweeter",
    })
    status = sound_setup._active_speaker_safe_playback_payload()
    stopped = sound_setup._active_speaker_stop_payload()
    stopped_level = sound_setup._active_speaker_calibration_level_payload()

    assert armed["status"] == "armed"
    assert armed["playback_allowed"] is False
    assert targets["targets"]
    assert targets["calibration_level"]["test_signal"]["default_level_dbfs"] == -80.0
    assert guarded["test_signal"]["requested_level_dbfs"] == -79.0
    assert guarded["issues"][0]["code"] == "upward_step_limited"
    assert plan["status"] == "ready"
    assert plan["would_play"] is False
    assert plan["target"]["driver_role"] == "tweeter"
    assert plan["channel_map"]["output_count"] == 2
    assert plan["calibration_level"]["test_signal"]["requested_level_dbfs"] == -79.0
    assert level_plan["tone"]["level_dbfs"] == -79.0
    assert level_plan["calibration_level"]["test_signal"]["requested_level_dbfs"] == -79.0
    assert playback["playback"]["status"] == "completed"
    assert playback["playback"]["audio_emitted"] is False
    assert playback["playback"]["artifact"]["channel_count"] == 2
    assert playback["playback"]["artifact"]["target_output_index"] == 1
    assert playback["session"]["playback"]["status"] == "completed"
    assert status["status"] == "armed"
    assert stopped["status"] == "stopped"
    assert stopped["playback"]["status"] == "stopped"
    assert stopped["session_id"] == armed["session_id"]
    assert stopped["calibration_level"]["test_signal"]["requested_level_dbfs"] == -80.0
    assert stopped_level["test_signal"]["requested_level_dbfs"] == -80.0


def test_active_speaker_stop_payload_survives_level_reset_failure(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.active_speaker import calibration_level as level_mod

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
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

    def fail_reset(*args, **kwargs):
        raise OSError("state path is unavailable")

    sound_setup._active_speaker_arm_payload()
    monkeypatch.setattr(level_mod, "update_calibration_level_state", fail_reset)

    stopped = sound_setup._active_speaker_stop_payload()

    assert stopped["status"] == "stopped"
    assert stopped["playback"]["status"] == "stopped"
    assert stopped["calibration_level"]["status"] == "reset_failed"


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
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        str(tmp_path / "tone-artifacts"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", "wav_artifact")
    startup_load_state = tmp_path / "startup-load.json"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE",
        str(startup_load_state),
    )
    active_config_path = tmp_path / "active-startup.yml"
    prior_config_path = tmp_path / "prior.yml"
    active_config_path.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    prior_config_path.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    startup_load_state.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_startup_load_state",
            "status": "loaded",
            "loaded": True,
            "candidate_config_path": str(active_config_path),
            "active_config_path": str(active_config_path),
            "previous_config_path": str(prior_config_path),
            "rollback_available": True,
            "last_action": "load",
            "issues": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_environment_payload",
        lambda: {
            "status": "pass",
            "load_gate": "ready",
            "ok_to_load_active_config": True,
            "camilla_config": {
                "path": str(active_config_path),
                "classification": "active_startup_candidate",
            },
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
    })
    artifact = sound_setup._active_speaker_tone_playback_payload({
        "speaker_group_id": "left",
        "role": "woofer",
    })
    blocked_audio = sound_setup._active_speaker_tone_playback_payload({
        "speaker_group_id": "left",
        "role": "woofer",
        "audio": True,
    })

    assert armed["status"] == "armed"
    assert readiness["status"] == "preconditions_passed"
    assert readiness["preconditions_passed"] is True
    assert readiness["playback_allowed"] is False
    assert readiness["would_play"] is False
    assert readiness["tone_playback_implemented"] is False
    assert readiness["target"]["physical_output_index"] == 0
    assert readiness["calibration_level"]["test_signal"]["requested_level_dbfs"] == -80.0
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


def test_active_speaker_auto_level_step_uses_saved_target_and_floor_evidence(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.active_speaker.safe_playback import (
        arm_safe_playback_session,
        record_floor_audio_operator_result,
        record_safe_playback_result,
    )

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })
    arm_safe_playback_session({
        "status": "pass",
        "load_gate": "ready",
        "ok_to_load_active_config": True,
        "camilla_config": {
            "path": "/var/lib/camilladsp/configs/active.yml",
            "classification": "active_startup_candidate",
        },
        "safe_playback": {
            "status": "not_implemented",
            "playback_allowed": False,
        },
        "issues": [],
    })
    target = {
        "speaker_group_id": "mono",
        "role": "woofer",
        "driver_role": "woofer",
        "output_index": 0,
    }
    floor_playback = {
        "status": "completed",
        "playback_id": "floor-test-1",
        "backend": "aplay",
        "audio_emitted": True,
        "target": target,
        "tone": {"level_dbfs": -80.0},
        "issues": [],
    }
    record_safe_playback_result(floor_playback)
    record_floor_audio_operator_result(
        outcome="heard_correct_driver",
        playback_id="floor-test-1",
    )
    sound_setup._active_speaker_calibration_level_payload({
        "action": "observe",
        "observed_mic_dbfs": -58.0,
    })

    payload = sound_setup._active_speaker_calibration_level_payload({
        "action": "auto_step",
        "speaker_group_id": "mono",
        "role": "woofer",
    })

    assert payload["target"]["speaker_group_id"] == "mono"
    assert payload["target"]["output_index"] == 0
    assert payload["auto_level"]["action"] == "raise"
    assert payload["auto_level"]["floor_audio_confirmed"] is True
    assert payload["auto_level_applied_action"] == "ramp"
    assert payload["test_signal"]["requested_level_dbfs"] == -74.0
    assert payload["applied_delta_db"] == 6.0


def test_active_speaker_auto_level_step_holds_without_floor_evidence(
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
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })
    sound_setup._active_speaker_calibration_level_payload({
        "action": "set",
        "level_dbfs": -79.0,
        "observed_mic_dbfs": -58.0,
    })

    payload = sound_setup._active_speaker_calibration_level_payload({
        "action": "auto_step",
        "speaker_group_id": "mono",
        "role": "woofer",
    })

    assert payload["target"]["speaker_group_id"] == "mono"
    assert payload["auto_level"]["action"] == "hold_for_floor_confirmation"
    assert payload["auto_level"]["floor_audio_confirmed"] is False
    assert payload["auto_level_applied_action"] == "observe"
    assert payload["test_signal"]["requested_level_dbfs"] == -79.0
    assert payload["applied_delta_db"] == 0.0


def test_active_speaker_floor_audio_result_requires_operator_confirmation(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.active_speaker.safe_playback import (
        arm_safe_playback_session,
        record_safe_playback_result,
    )

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    arm_safe_playback_session({
        "status": "pass",
        "load_gate": "ready",
        "ok_to_load_active_config": True,
        "camilla_config": {"classification": "active_startup_candidate"},
        "safe_playback": {"playback_allowed": False},
        "issues": [],
    })
    playback = {
        "status": "completed",
        "playback_id": "floor-test-web",
        "backend": "aplay",
        "audio_emitted": True,
        "target": {
            "speaker_group_id": "mono",
            "role": "woofer",
            "driver_role": "woofer",
            "output_index": 0,
        },
        "tone": {"level_dbfs": -80.0},
        "issues": [],
    }
    pending = record_safe_playback_result(playback)

    confirmed = sound_setup._active_speaker_floor_audio_result_payload({
        "outcome": "heard_correct_driver",
        "playback_id": "floor-test-web",
    })

    assert pending["quiet_start"]["status"] == "floor_pending_operator"
    assert confirmed["quiet_start"]["status"] == "floor_confirmed"
    assert confirmed["quiet_start"]["floor_audio_confirmed"] is True
    assert confirmed["quiet_start"]["last_operator_result"]["outcome"] == (
        "heard_correct_driver"
    )


def test_active_speaker_auto_level_step_does_not_bypass_tweeter_protection(
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
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
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
                        "driver_style": "compression_driver",
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

    payload = sound_setup._active_speaker_calibration_level_payload({
        "action": "auto_step",
        "speaker_group_id": "mono",
        "role": "tweeter",
        "observed_mic_dbfs": -58.0,
    })

    assert payload["auto_level"]["status"] == "blocked"
    assert payload["auto_level_applied_action"] == "observe"
    assert payload["test_signal"]["requested_level_dbfs"] == -80.0
    assert "high_frequency_protection_missing" in {
        issue["code"] for issue in payload["auto_level"]["issues"]
    }


def test_active_speaker_commissioning_rehearsal_payload_is_no_audio(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono speaker",
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_safe_playback_payload",
        lambda: {
            "status": "armed",
            "session_id": "safe-1",
            "expires_at": "2026-06-09T12:00:00Z",
        },
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_calibration_level_payload",
        lambda raw=None: {
            "test_signal": {
                "requested_level_dbfs": -80.0,
                "min_level_dbfs": -80.0,
            }
        },
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_bringup_preflight_payload",
        lambda: {
            "status": "manual_ready",
            "software_guard": {"status": "software_guard_ready"},
            "modes": {
                "manual_guarded_bringup": {
                    "required_gates": [
                        {"id": "output_topology_present", "passed": True},
                        {"id": "topology_has_no_unhandled_blockers", "passed": True},
                        {"id": "physical_identity_verified", "passed": True},
                        {"id": "protected_startup_config_staged", "passed": True},
                        {"id": "high_frequency_guard_accepted", "passed": True},
                    ]
                }
            },
            "issues": [],
        },
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_startup_load_payload",
        lambda: {
            "state": {
                "status": "loaded",
                "loaded": True,
                "rollback_available": True,
                "current_config_matches_loaded": True,
            },
            "preflight": {
                "status": "ready",
                "path_safety": {"load_gate": "ready"},
                "required_gates": [
                    {"id": "path_safety_ready", "passed": True},
                    {
                        "id": "path_safety_matches_current_startup_load",
                        "passed": True,
                    },
                ],
                "issues": [],
            },
        },
    )

    payload = sound_setup._active_speaker_commissioning_rehearsal_payload()

    assert payload["kind"] == "jts_active_speaker_commissioning_rehearsal"
    assert payload["status"] == "ready_for_target_check"
    assert payload["no_audio"] is True
    assert payload["steps"][7]["id"] == "target_readiness_checked"
    assert payload["steps"][7]["status"] == "next"


def _active_speaker_mono_topology_payload(
    *,
    protection_status: str,
    card_id: str | None = "DAC8",
) -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": card_id,
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
                        "protection_status": protection_status,
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    }


def _active_speaker_driver_research_payload(*, frequency_hz: float = 2500) -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_crossover_driver_research",
        "drivers": [
            {
                "role": "woofer",
                "model": "Epique E150HE-44",
                "recommended_lowpass_hz": frequency_hz,
                "sources": ["https://example.test/woofer"],
            },
            {
                "role": "tweeter",
                "model": "F110M-8",
                "recommended_highpass_hz": frequency_hz,
                "do_not_test_below_hz": 1200,
                "sources": ["https://example.test/tweeter"],
            },
        ],
        "crossover_candidates": [
            {
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": frequency_hz,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            }
        ],
    }


def _save_active_speaker_design_and_preview(*, frequency_hz: float = 2500) -> dict:
    sound_setup._active_speaker_design_draft_save_payload({
        "operator_inputs": {
            "woofer": "Dayton Epique E150HE-44",
            "tweeter": "Eminence F110M-8",
        },
        "driver_research": _active_speaker_driver_research_payload(
            frequency_hz=frequency_hz,
        ),
    })
    return sound_setup._active_speaker_crossover_preview_save_payload()


def test_active_speaker_prepare_driver_test_owns_internal_setup(
    monkeypatch,
    tmp_path: Path,
):
    import jasper.active_speaker.playback as active_playback
    from jasper.output_topology import load_output_topology

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", "aplay")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO", "1")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_TEST_PCM", "hw:Active")
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="required_missing")
    )
    calls: list[str] = []

    def fake_preview() -> dict:
        calls.append("preview")
        return {
            "status": "ready_for_protected_staging",
            "summary": {"ready_crossover_count": 1},
            "issues": [],
        }

    def fake_stage(raw: dict) -> dict:
        calls.append("stage")
        return {
            "status": "staged",
            "config": {"path": str(tmp_path / "active-startup.yml")},
            "issues": [],
        }

    async def fake_path_safety(*, camilla_factory) -> dict:
        calls.append("path")
        return {
            "report": {"status": "ready", "load_gate": "ready", "issues": []},
            "startup_load": {"state": {"status": "idle"}},
        }

    async def fake_load(*, camilla_factory) -> dict:
        calls.append("load")
        return {
            "load": {
                "status": "loaded",
                "loaded": True,
                "rollback_available": True,
            },
            "preflight": {"status": "ready", "issues": []},
        }

    def fake_arm() -> dict:
        calls.append("arm")
        return {
            "status": "armed",
            "session_id": "safe-1",
            "issues": [],
        }

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_crossover_preview_save_payload",
        fake_preview,
    )
    monkeypatch.setattr(
        active_playback,
        "tone_backend_status",
        lambda **kwargs: {
            "kind": "jts_active_speaker_tone_backend_status",
            "status": "audio_enabled",
            "audio_enabled": True,
            "issues": [],
        },
    )
    monkeypatch.setattr(sound_setup, "_active_speaker_stage_config_payload", fake_stage)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_check_path_safety_payload",
        fake_path_safety,
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_load_startup_config_payload",
        fake_load,
    )
    monkeypatch.setattr(sound_setup, "_active_speaker_arm_payload", fake_arm)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_calibration_level_payload",
        lambda raw=None: {
            "test_signal": {
                "requested_level_dbfs": -80.0,
                "min_level_dbfs": -80.0,
            }
        },
    )

    payload = asyncio.run(
        sound_setup._active_speaker_prepare_driver_test_payload(
            {"speaker_group_id": "mono", "role": "tweeter"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["target"]["role"] == "tweeter"
    assert payload["tone_backend"]["kind"] == "jts_active_speaker_tone_backend_status"
    assert calls == ["preview", "stage", "path", "load", "arm"]
    topology = load_output_topology()
    tweeter = topology.speaker_groups[0].channels[1]
    assert tweeter.protection_status == "software_guard_requested"


def test_active_speaker_prepare_driver_test_skips_startup_load_for_artifact_backend(
    monkeypatch,
    tmp_path: Path,
):
    import jasper.active_speaker.safe_playback as safe_playback
    import jasper.active_speaker.startup_load as startup_load

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", "wav_artifact")
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(
            protection_status="required_missing",
            card_id=None,
        )
    )
    calls: list[str] = []

    def fake_preview() -> dict:
        calls.append("preview")
        return {
            "status": "ready_for_protected_staging",
            "summary": {"ready_crossover_count": 1},
            "issues": [],
        }

    def fail_stage(raw: dict) -> dict:
        raise AssertionError("artifact-only prepare must not stage CamillaDSP")

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_crossover_preview_save_payload",
        fake_preview,
    )
    monkeypatch.setattr(sound_setup, "_active_speaker_stage_config_payload", fail_stage)
    monkeypatch.setattr(
        safe_playback,
        "load_safe_playback_state",
        lambda: {"status": "idle", "session_id": None},
    )
    monkeypatch.setattr(
        startup_load,
        "load_startup_load_state",
        lambda: {"status": "idle", "loaded": False},
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_calibration_level_payload",
        lambda raw=None: {
            "test_signal": {
                "requested_level_dbfs": -80.0,
                "min_level_dbfs": -80.0,
            }
        },
    )

    payload = asyncio.run(
        sound_setup._active_speaker_prepare_driver_test_payload(
            {"speaker_group_id": "mono", "role": "tweeter"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["target"]["role"] == "tweeter"
    assert payload["tone_backend"]["status"] == "artifact_only"
    assert payload["tone_backend"]["audio_enabled"] is False
    assert payload["session"]["status"] == "idle"
    assert payload["startup_load"]["load"]["status"] == "idle"
    assert "staged_config" not in payload
    assert calls == ["preview"]


def test_active_speaker_prepare_driver_test_preserves_direct_dac_retry_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jasper.active_speaker.calibration_level import update_calibration_level_state
    from jasper.active_speaker.safe_playback import (
        arm_safe_playback_session,
        record_floor_audio_operator_result,
        record_safe_playback_result,
    )

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    monkeypatch.delenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", raising=False)
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "mono_active",
        "name": "Mono active 2-way output",
        "status": "verified",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "sndrpihifiberry",
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main speaker",
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "main"},
    })
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_crossover_preview_save_payload",
        lambda: {
            "status": "ready_for_protected_staging",
            "summary": {"ready_crossover_count": 1},
            "issues": [],
        },
    )

    arm_safe_playback_session(sound_setup._active_speaker_direct_diagnostic_environment())
    target = {
        "speaker_group_id": "main",
        "role": "woofer",
        "driver_role": "woofer",
        "output_index": 0,
    }
    record_safe_playback_result({
        "status": "completed",
        "playback_id": "floor-1",
        "backend": "direct_dac",
        "audio_emitted": True,
        "target": target,
        "tone": {"level_dbfs": -80.0},
        "issues": [],
    })
    record_floor_audio_operator_result(outcome="silent", playback_id="floor-1")
    update_calibration_level_state(action="audible_ramp", requested_level_dbfs=-74.0)

    payload = asyncio.run(
        sound_setup._active_speaker_prepare_driver_test_payload(
            {"speaker_group_id": "main", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "ready"
    assert payload["session"]["status"] == "armed"
    assert payload["session"]["quiet_start"]["last_operator_result"]["outcome"] == (
        "silent"
    )
    assert payload["calibration_level"]["test_signal"]["requested_level_dbfs"] == -74.0


def test_active_speaker_prepare_driver_test_resets_stale_raised_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jasper.active_speaker.calibration_level import update_calibration_level_state
    from jasper.active_speaker.safe_playback import (
        arm_safe_playback_session,
        stop_safe_playback_session,
    )

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    monkeypatch.delenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", raising=False)
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "mono_active",
        "name": "Mono active 2-way output",
        "status": "verified",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "sndrpihifiberry",
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main speaker",
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "main"},
    })
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_crossover_preview_save_payload",
        lambda: {
            "status": "ready_for_protected_staging",
            "summary": {"ready_crossover_count": 1},
            "issues": [],
        },
    )

    arm_safe_playback_session(sound_setup._active_speaker_direct_diagnostic_environment())
    update_calibration_level_state(action="audible_ramp", requested_level_dbfs=-74.0)
    stop_safe_playback_session()

    payload = asyncio.run(
        sound_setup._active_speaker_prepare_driver_test_payload(
            {"speaker_group_id": "main", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "ready"
    assert payload["session"]["status"] == "armed"
    assert payload["calibration_level"]["test_signal"]["requested_level_dbfs"] == -80.0
    assert payload["calibration_level"]["last_action"] == "stop"


def test_active_speaker_prepare_driver_test_guards_sibling_tweeter(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.output_topology import load_output_topology

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", "wav_artifact")
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(
            protection_status="required_missing",
            card_id=None,
        )
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_crossover_preview_save_payload",
        lambda: {
            "status": "ready_for_protected_staging",
            "summary": {"ready_crossover_count": 1},
            "issues": [],
        },
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_calibration_level_payload",
        lambda raw=None: {
            "test_signal": {
                "requested_level_dbfs": -80.0,
                "min_level_dbfs": -80.0,
            }
        },
    )

    payload = asyncio.run(
        sound_setup._active_speaker_prepare_driver_test_payload(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    topology = load_output_topology()
    tweeter = topology.speaker_groups[0].channels[1]
    assert payload["status"] == "ready"
    assert payload["target"]["role"] == "woofer"
    assert tweeter.protection_status == "software_guard_requested"


def test_active_speaker_crossover_preview_refreshes_current_output_topology(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )

    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="required_missing")
    )
    blocked = _save_active_speaker_design_and_preview()
    assert blocked["status"] == "blocked"
    assert "tweeter_protection_unverified" in {
        issue["code"] for issue in blocked["issues"]
    }

    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(
            protection_status="software_guard_requested"
        )
    )
    refreshed = sound_setup._active_speaker_crossover_preview_save_payload()

    assert refreshed["status"] == "ready_for_protected_staging"
    assert "tweeter_protection_unverified" not in {
        issue["code"] for issue in refreshed["issues"]
    }
    filters = refreshed["groups"][0]["crossovers"][0]["filters"]
    tweeter_filter = next(
        item for item in filters
        if item["role"] == "tweeter"
    )
    assert tweeter_filter["channel"]["identity_verified"] is True
    assert tweeter_filter["channel"]["protection_status"] == "software_guard_requested"


def test_active_speaker_summed_test_records_current_artifact(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.active_speaker.measurement import record_driver_measurement
    from jasper.active_speaker.safe_playback import arm_safe_playback_session
    from jasper.output_topology import load_output_topology

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(tmp_path / "measurements.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        str(tmp_path / "tone-artifacts"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="present")
    )
    _save_active_speaker_design_and_preview()
    arm_safe_playback_session({
        "status": "pass",
        "load_gate": "ready",
        "ok_to_load_active_config": True,
        "camilla_config": {"classification": "active_startup_candidate"},
        "safe_playback": {"playback_allowed": False},
        "issues": [],
    })
    topology = load_output_topology()
    for role, output_index in (("woofer", 0), ("tweeter", 1)):
        playback_id = f"playback-{role}"
        target = {
            "speaker_group_id": "mono",
            "role": role,
            "driver_role": role,
            "output_index": output_index,
        }
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42,
                "playback_id": playback_id,
            },
            safe_session={
                "status": "armed",
                "quiet_start": {
                    "status": "floor_confirmed",
                    "floor_audio_confirmed": True,
                    "last_operator_result": {
                        "accepted": True,
                        "outcome": "heard_correct_driver",
                        "playback_id": playback_id,
                        "target": target,
                    },
                },
            },
        )

    payload = sound_setup._active_speaker_summed_test_payload({
        "speaker_group_id": "mono",
        "audio": False,
    })
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]

    assert payload["playback"]["status"] == "completed"
    assert payload["playback"]["audio_emitted"] is False
    assert payload["playback"]["artifact"]["target_output_indices"] == [0, 1]
    assert latest["captured"] is True
    assert latest["audio_emitted"] is False
    assert latest["target_output_indices"] == [0, 1]


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
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE", "hw:DAC8,0")
    saved = sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="required_missing")
    )

    preview_blocked = _save_active_speaker_design_and_preview()
    blocked = sound_setup._active_speaker_stage_config_payload({})
    protected = sound_setup._active_speaker_channel_protection_save_payload({
        "speaker_group_id": "mono",
        "role": "tweeter",
        "protection_present": True,
    })
    preview_ready = _save_active_speaker_design_and_preview()
    staged = sound_setup._active_speaker_stage_config_payload({})
    loaded = sound_setup._active_speaker_staged_config_payload()

    assert saved["output_topology"]["status"] == "blocked"
    assert preview_blocked["status"] == "blocked"
    assert blocked["status"] == "blocked"
    assert "crossover_preview_not_ready" in {
        issue["code"] for issue in blocked["issues"]
    }
    assert protected["output_topology"]["status"] == "verified"
    assert preview_ready["status"] == "ready_for_protected_staging"
    assert staged["status"] == "staged"
    assert staged["preset"]["source"]["mode"] == "crossover_preview"
    assert staged["config"]["basename"] == "active_staged.yml"
    assert staged["config"]["playback_device"] == "hw:DAC8,0"
    assert staged["config"]["tweeter_protective_highpass_hz"] == 5000
    assert staged["load"]["load_allowed"] is False
    assert Path(staged["config"]["path"]).exists()
    assert loaded["status"] == "staged"


def test_active_speaker_path_safety_payload_writes_no_audio_evidence(
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
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
        str(tmp_path / "path_safety.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE", "hw:DAC8,0")
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(
            protection_status="software_guard_requested",
        )
    )
    preview = _save_active_speaker_design_and_preview()
    staged = sound_setup._active_speaker_stage_config_payload({})
    fake = FakeCamilla(staged["config"]["path"])

    assert preview["status"] == "ready_for_protected_staging"
    assert staged["status"] == "staged"
    assert staged["preset"]["source"]["mode"] == "crossover_preview"
    payload = asyncio.run(
        sound_setup._active_speaker_check_path_safety_payload(
            camilla_factory=lambda: fake,
        )
    )

    evidence_path = Path(payload["evidence_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["report"]["ok_to_load_active_config"] is True
    assert payload["startup_load"]["preflight"]["path_safety"]["load_gate"] == "ready"
    assert evidence["evidence_mode"] == "startup_load_preflight"
    assert fake.set_calls == []


def test_active_speaker_stage_config_rejects_non_string_playback_device() -> None:
    with pytest.raises(ValueError, match="playback_device must be a string"):
        sound_setup._active_speaker_stage_config_payload({
            "playback_device": {"device": "hw:DAC8,0"},
        })


def test_active_speaker_stage_config_route_requires_current_preview(
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
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE", "hw:DAC8,0")
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="present")
    )

    payload = sound_setup._active_speaker_stage_config_payload({})

    assert payload["status"] == "blocked"
    assert payload["config"]["exists"] is False
    assert payload["preset"]["source"]["mode"] == "crossover_preview"
    assert "crossover_preview_not_ready" in {
        issue["code"] for issue in payload["issues"]
    }


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


def test_output_topology_payload_serializes_with_populated_hardware_state(
    monkeypatch,
    tmp_path: Path,
):
    """A populated output-hardware state file must not 502 the route.

    Regression: ``load_state`` returns a frozen ``OutputHardwareState`` when a
    state file exists (every real Pi), and ``_send_json`` emits the payload
    with plain ``json.dumps`` — which can't encode the dataclass. Embedding it
    raw produced "Object of type OutputHardwareState is not JSON serializable"
    -> HTTP 502 on ``/sound/output-topology``. The prior payload test only
    exercised the no-file (``None``) path, so the defect shipped untested.
    """
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    card = OutputCardFact(
        card_id="A",
        pcm="hw:A,0",
        device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        label="Apple USB-C dongle",
        has_playback=True,
    )
    write_output_hardware_state(classify_output_cards([card]))

    envelope = sound_setup._output_topology_payload()

    # The exact serialization _send_json performs — this raised the 502.
    json.dumps(envelope)
    hardware = envelope["output_hardware"]
    assert isinstance(hardware, dict)
    assert hardware["status"] == "ready"
    assert envelope["active_playback_route"]["kind"] == (
        "jts_active_speaker_playback_route_capability"
    )
    assert envelope["active_playback_route"]["playback_device_source"] == (
        "topology_direct_dac"
    )
    assert envelope["active_playback_route"]["transport_channel_count"] == 2


def test_output_hardware_state_only_loaded_inside_conversion_boundary():
    """Payload builders must go through ``_output_hardware_dict``.

    Pins the fix shape: ``load_output_hardware_state`` returns a frozen
    dataclass that plain ``json.dumps`` can't encode, so the only call site
    in this module is the helper that converts it. A new payload embedding
    the loader directly re-ships the 502.
    """
    source = Path(sound_setup.__file__).read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in source.splitlines()
        if "load_output_hardware_state(" in line
    ]
    assert lines == ["hardware = load_output_hardware_state()"], (
        "load_output_hardware_state() called outside _output_hardware_dict(); "
        f"route new payloads through the helper: {lines}"
    )


def test_active_speaker_design_draft_route_persists_saved_topology_research(
    monkeypatch,
    tmp_path: Path,
):
    topology_path = tmp_path / "output_topology.json"
    draft_path = tmp_path / "design_draft.json"
    preview_path = tmp_path / "crossover_preview.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE", str(draft_path))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(preview_path),
    )
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })

    payload = sound_setup._active_speaker_design_draft_save_payload({
        "operator_inputs": {
            "woofer": "Dayton Epique E150HE-44",
            "tweeter": "Eminence F110M-8",
        },
        "driver_research": {
            "artifact_schema_version": 1,
            "kind": "jts_active_crossover_driver_research",
            "drivers": [
                {
                    "role": "woofer",
                    "model": "Epique E150HE-44",
                    "recommended_lowpass_hz": 2500,
                    "sources": ["https://example.test/woofer"],
                },
                {
                    "role": "tweeter",
                    "model": "F110M-8",
                    "recommended_highpass_hz": 2500,
                    "do_not_test_below_hz": 1200,
                    "sources": ["https://example.test/tweeter"],
                },
            ],
            "crossover_candidates": [
                {
                    "between_roles": ["woofer", "tweeter"],
                    "frequency_hz": 2500,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                }
            ],
        },
    })
    loaded = sound_setup._active_speaker_design_draft_payload()

    assert payload["kind"] == "jts_active_speaker_design_draft"
    assert payload["status"] == "ready_for_review"
    assert payload["summary"]["driver_count"] == 2
    assert payload["summary"]["crossover_candidate_count"] == 1
    assert payload["safety"]["no_audio"] is True
    assert loaded["status"] == "ready_for_review"
    assert json.loads(draft_path.read_text(encoding="utf-8"))["status"] == (
        "ready_for_review"
    )

    preview = sound_setup._active_speaker_crossover_preview_save_payload()
    loaded_preview = sound_setup._active_speaker_crossover_preview_payload()

    assert preview["kind"] == "jts_active_speaker_crossover_preview"
    assert preview["status"] == "ready_for_protected_staging"
    assert preview["safety"]["no_audio"] is True
    assert preview["permissions"]["may_not_emit_camilla_yaml"] is True
    assert loaded_preview["status"] == "ready_for_protected_staging"
    assert json.loads(preview_path.read_text(encoding="utf-8"))["status"] == (
        "ready_for_protected_staging"
    )

    stale_draft = json.loads(draft_path.read_text(encoding="utf-8"))
    stale_draft["driver_research"]["crossover_candidates"][0]["frequency_hz"] = 3200
    stale_draft["updated_at"] = "2026-06-10T12:45:00Z"
    draft_path.write_text(json.dumps(stale_draft), encoding="utf-8")

    stale_preview = sound_setup._active_speaker_crossover_preview_payload()

    assert stale_preview["status"] == "stale"
    assert stale_preview["permissions"]["may_prepare_protected_startup_config"] is False
    assert "crossover_preview_stale_design_draft" in {
        issue["code"] for issue in stale_preview["issues"]
    }


def _dual_apple_hardware() -> dict:
    return {
        "device_id": DUAL_APPLE_ACTIVE_DEVICE_ID,
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "left_dac",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FHL2FN3AC",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "right_dac",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FLL2FN3A3",
                "physical_output_indexes": [2, 3],
            },
        ],
        "clock_domain_evidence": {
            "evidence_kind": "dual_apple_usb_c_dac_drift_measurement",
            "measurement_id": "scarlett-ticks-900s-repeat-buffered",
            "status": "passed",
            "duration_seconds": 900,
            "sample_rate_hz": 48000,
            "offset_frames": -7,
            "max_offset_delta_frames": 0,
            "drift_ppm": 0,
            "xrun_count": 0,
            "dac_serials": [
                "DWH53530FHL2FN3AC",
                "DWH53530FLL2FN3A3",
            ],
        },
    }


def test_sound_output_topology_payload_uses_observed_dual_apple_hardware_state(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    write_output_hardware_state(
        classify_output_cards([
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FHL2FN3AC",
                usb_path="usb1/1-2",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
            OutputCardFact(
                card_id="A_1",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FLL2FN3A3",
                usb_path="usb1/1-1",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
        ]),
        path=tmp_path / "output_hardware.json",
    )

    envelope = sound_setup._output_topology_payload()
    payload = envelope["output_topology"]

    assert payload["hardware"]["device_id"] == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert payload["hardware"]["device_label"] == "Dual Apple USB-C DAC 4-channel pair"
    assert payload["hardware"]["physical_output_count"] == 4
    assert payload["hardware"]["child_devices"][0]["serial"] == "DWH53530FHL2FN3AC"
    assert envelope["clock_domain"]["status"] == "dual_apple_composite_clock"
    assert envelope["clock_domain"]["composite_clock_supported"] is True
    assert envelope["clock_domain"]["measured_composite_supported"] is False
    assert "clock_evidence_missing" in {
        issue["code"] for issue in envelope["clock_domain"]["issues"]
    }
    assert payload["safety"]["sound_tests_allowed"] is False


def test_sound_output_topology_save_accepts_measured_dual_apple_hardware(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    write_output_hardware_state(
        classify_output_cards([
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FHL2FN3AC",
                usb_path="usb1/1-2",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
            OutputCardFact(
                card_id="A_1",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FLL2FN3A3",
                usb_path="usb1/1-1",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
        ]),
        path=tmp_path / "output_hardware.json",
    )

    payload = sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple_pair",
        "name": "Dual Apple stereo active pair",
        "status": "draft",
        "hardware": _dual_apple_hardware(),
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
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 2,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 3,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    },
                ],
            },
        ],
        "routing": {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
        },
    })

    topology = payload["output_topology"]

    assert topology["status"] == "verified"
    assert topology["hardware"]["physical_output_count"] == 4
    assert payload["clock_domain"]["status"] == "dual_apple_composite_clock"
    assert payload["clock_domain"]["measured_composite_supported"] is True
    assert payload["clock_domain"]["multi_device_aggregate_supported"] is False
    assert payload["channel_identity"]["verified_channel_count"] == 4
    assert topology["safety"]["sound_tests_allowed"] is False


def test_active_speaker_tone_backend_defaults_to_direct_dac_from_topology(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.delenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", raising=False)
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "mono_active",
        "name": "Mono active 2-way output",
        "status": "verified",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "sndrpihifiberry",
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main speaker",
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "main"},
    })

    status = sound_setup._active_speaker_tone_backend_status()

    assert status["status"] == "audio_enabled"
    assert status["backend"] == "direct_dac"
    assert status["test_pcm"] == "hw:CARD=sndrpihifiberry,DEV=0"
    assert status["channel_count"] == 8
    assert status["requires_protected_startup"] is False

    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", "wav_artifact")
    stale_env_status = sound_setup._active_speaker_tone_backend_status()
    assert stale_env_status["status"] == "audio_enabled"
    assert stale_env_status["backend"] == "direct_dac"
    assert stale_env_status["channel_count"] == 8


def test_active_speaker_direct_dac_play_tone_refreshes_expired_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    monkeypatch.delenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", raising=False)
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "mono_active",
        "name": "Mono active 2-way output",
        "status": "verified",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "sndrpihifiberry",
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main speaker",
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "main"},
    })
    (tmp_path / "safe-playback.json").write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_safe_playback_session",
            "status": "expired",
            "session_id": "old-session",
        }),
        encoding="utf-8",
    )

    class FakeDirectBackend:
        backend_id = "direct_dac"
        audio_backend = True
        requires_protected_startup = False

        def start(self, plan, *, playback_id, now_epoch):
            return {
                "backend": self.backend_id,
                "status": "completed",
                "audio_emitted": True,
                "artifact": {"wav_basename": "test.wav"},
            }

        def stop(self, *, playback_id, reason, now_epoch):
            return {"backend": self.backend_id, "status": "stopped"}

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_tone_backend_for_topology",
        lambda topology: FakeDirectBackend(),
    )

    payload = sound_setup._active_speaker_tone_playback_payload({
        "speaker_group_id": "main",
        "role": "woofer",
        "audio": True,
    })

    assert payload["playback"]["status"] == "completed"
    assert payload["playback"]["audio_emitted"] is True
    assert payload["session"]["status"] == "armed"
    assert payload["session"]["session_id"] != "old-session"
    assert "safe_session_not_armed" not in {
        issue["code"] for issue in payload["playback"].get("issues", [])
    }


def test_active_speaker_direct_dac_play_tone_preserves_silent_retry_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    monkeypatch.delenv("JASPER_ACTIVE_SPEAKER_TONE_BACKEND", raising=False)
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "mono_active",
        "name": "Mono active 2-way output",
        "status": "verified",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "sndrpihifiberry",
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main speaker",
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "main"},
    })

    class FakeDirectBackend:
        backend_id = "direct_dac"
        audio_backend = True
        requires_protected_startup = False

        def start(self, plan, *, playback_id, now_epoch):
            return {
                "backend": self.backend_id,
                "status": "completed",
                "audio_emitted": True,
                "artifact": {"wav_basename": "test.wav"},
            }

        def stop(self, *, playback_id, reason, now_epoch):
            return {"backend": self.backend_id, "status": "stopped"}

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_tone_backend_for_topology",
        lambda topology: FakeDirectBackend(),
    )

    floor = sound_setup._active_speaker_tone_playback_payload({
        "speaker_group_id": "main",
        "role": "woofer",
        "audio": True,
    })
    assert floor["playback"]["status"] == "completed"
    assert floor["playback"]["tone"]["level_dbfs"] == -80.0

    silent = sound_setup._active_speaker_floor_audio_result_payload({
        "outcome": "silent",
        "playback_id": floor["playback"]["playback_id"],
    })
    assert silent["quiet_start"]["last_operator_result"]["outcome"] == "silent"

    level = sound_setup._active_speaker_calibration_level_payload({
        "level_dbfs": -74,
    })
    assert level["test_signal"]["requested_level_dbfs"] == -79.0

    retry = sound_setup._active_speaker_tone_playback_payload({
        "speaker_group_id": "main",
        "role": "woofer",
        "audio": True,
    })

    assert retry["playback"]["status"] == "completed"
    assert retry["playback"]["audio_emitted"] is True
    assert retry["playback"]["tone"]["level_dbfs"] == -79.0
    assert "floor_audio_not_confirmed" not in {
        issue["code"] for issue in retry["playback"].get("issues", [])
    }


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


def test_sound_channel_protection_route_accepts_software_guard_request(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0},
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "required_missing",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })

    payload = sound_setup._active_speaker_channel_protection_save_payload({
        "speaker_group_id": "mono",
        "role": "tweeter",
        "protection_status": "software_guard_requested",
    })
    saved = json.loads(path.read_text(encoding="utf-8"))
    tweeter = saved["speaker_groups"][0]["channels"][1]

    assert payload["output_topology"]["status"] == "valid"
    assert tweeter["protection_status"] == "software_guard_requested"
    assert "tweeter_software_guard_requested" in {
        issue["code"] for issue in payload["output_topology"]["evaluation"]["warnings"]
    }


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
    monkeypatch.setenv("JASPER_AUDIO_DAC_CARD", "sndrpihifiberry")
    try:
        server, base = _start_sound_server(tmp_path)
    except PermissionError:
        pytest.skip("environment does not allow loopback test server bind")
    try:
        get_resp = urllib.request.urlopen(f"{base}/output-topology")
        get_payload = json.loads(get_resp.read().decode("utf-8"))
        assert get_payload["output_topology"]["status"] == "draft"
        # Stage 2: the DAC8x declares an active outputd lane, so the route
        # resolves to that lane (not a direct-DAC route) at its full width.
        assert get_payload["active_playback_route"]["playback_device_source"] == (
            "outputd_active_lane"
        )
        assert get_payload["active_playback_route"]["transport_channel_count"] == 8

        post_resp = request_with_csrf(
            base,
            "/output-topology",
            json.dumps(get_payload["output_topology"]).encode("utf-8"),
            content_type="application/json",
        )
        post_payload = json.loads(post_resp.read().decode("utf-8"))
        assert post_payload["output_topology"]["safety"]["sound_tests_allowed"] is False
        assert post_payload["active_playback_route"]["transport_channel_count"] == 8
    finally:
        server.shutdown()
        server.server_close()


def test_active_speaker_measurement_and_baseline_http_routes_are_exposed(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(tmp_path / "measurements.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
        str(tmp_path / "baseline_profile.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(tmp_path / "active_speaker_baseline.yml"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", "hifiberry_dac8x")
    try:
        server, base = _start_sound_server(tmp_path)
    except PermissionError:
        pytest.skip("environment does not allow loopback test server bind")
    try:
        measurement_resp = urllib.request.urlopen(
            f"{base}/active-speaker/measurements"
        )
        measurement_payload = json.loads(measurement_resp.read().decode("utf-8"))
        profile_resp = urllib.request.urlopen(
            f"{base}/active-speaker/baseline-profile"
        )
        profile_payload = json.loads(profile_resp.read().decode("utf-8"))

        assert measurement_payload["permissions"]["may_not_play_audio"] is True
        assert profile_payload["kind"] == "jts_active_speaker_baseline_profile_candidate"
        assert profile_payload["permissions"]["may_apply"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_active_speaker_crossover_preview_http_route_is_csrf_protected_no_audio(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )
    try:
        server, base = _start_sound_server(tmp_path)
    except PermissionError:
        pytest.skip("environment does not allow loopback test server bind")
    try:
        topology = {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "bench_mono",
            "name": "Bench mono",
            "status": "draft",
            "hardware": {
                "device_id": "hifiberry_dac8x",
                "device_label": "HiFiBerry DAC8x",
                "physical_output_count": 8,
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
                            "protection_status": "software_guard_requested",
                        },
                    ],
                }
            ],
            "routing": {"mono_group_id": "mono"},
        }
        research = {
            "artifact_schema_version": 1,
            "kind": "jts_active_crossover_driver_research",
            "drivers": [
                {"role": "woofer", "model": "Epique E150HE-44"},
                {
                    "role": "tweeter",
                    "model": "F110M-8",
                    "recommended_highpass_hz": 2500,
                    "do_not_test_below_hz": 1200,
                },
            ],
            "crossover_candidates": [
                {
                    "between_roles": ["woofer", "tweeter"],
                    "frequency_hz": 2500,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                }
            ],
        }
        json_post_with_csrf(base, "/output-topology", topology)
        json_post_with_csrf(
            base,
            "/active-speaker/design-draft",
            {"operator_inputs": {}, "driver_research": research},
        )

        resp = json_post_with_csrf(base, "/active-speaker/crossover-preview", {})
        payload = json.loads(resp.read().decode("utf-8"))

        assert payload["status"] == "ready_for_protected_staging"
        assert payload["safety"]["no_audio"] is True
        assert payload["permissions"]["may_not_emit_camilla_yaml"] is True
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


def test_sound_module_renders_first_active_crossover_step_without_scary_copy():
    if _NODE is None:
        pytest.skip("node not on PATH")

    proc = subprocess.run(
        [_NODE, str(_SOUND_HARNESS), str(_SOUND_MODULE), "active-crossover-flow"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["ok"] is True
    assert {"activeCrossoverFirstStepRendered": True} in out["results"]


def test_sound_css_marks_live_sources_with_red_dots():
    js = _SOUND_MODULE.read_text()
    css = _SOUND_CSS.read_text()

    assert "btn.classList.toggle('is-live', v === view);" in js
    assert ".app-header__tabs .segmented__btn.is-live::after" in css
    assert ".profile-row__dot--on" in css
    assert "background: var(--destructive);" in css
    assert ".active-speaker-issues--warning" in css
    assert ".active-speaker-issue--blocker" in css
    assert ".active-speaker-note" in css
    assert ".output-sequence__item--needs-action .output-sequence__marker" not in css

def test_sound_module_draws_a_single_response_curve_with_no_overlays():
    js = _SOUND_MODULE.read_text()
    render_start = js.index("function renderGraph(payload, enabled)")
    render_end = js.index("  // Render the graph", render_start)
    render_body = js[render_start:render_end]

    # One line only: the summed curve + fill. No per-band component overlay is
    # drawn, even for the expanded band (its dot + width shading mark it).
    assert "drawArea(payload.preview" in render_body
    assert "drawPath(curvePts, 'curve')" in render_body
    assert "component selected" not in render_body
    assert "comp.advanced" not in render_body
    assert "bandComponent" not in render_body
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
    assert "components" not in payload  # single-line graph: no per-band overlay data
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
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
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
    current.write_text(_room_config())
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
    current.write_text(_room_config())
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
    current.write_text(_room_config())
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
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
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
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
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
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
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
    current.write_text(_room_config())
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
    current.write_text(_room_config())
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
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
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
