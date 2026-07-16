# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run the capture-page JS harnesses inside the pytest CI lane.

The static capture page (Cloudflare Pages) is JavaScript, but its security- and
contract-critical pieces are pure modules exercised by Node harnesses:

  - the fixed DATA renderer (XSS-inert: <script>/onerror=/javascript:/hostile
    component types render inert) — the plan §15 acceptance test;
  - the E2E crypto wire format (AES-256-GCM, IV-prepended, plaintext integrity);
  - the relay client request contract; and
  - the fragment parser.

Bridging them through pytest (mirroring ``tests/test_sound_setup.py``) keeps the
page covered by the existing Python CI matrix with no extra CI wiring.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper.audio_measurement.calibration import SUPPORTED_MODELS

_JS_DIR = Path(__file__).resolve().parent / "js"
_NODE = shutil.which("node")
_REPO = Path(__file__).resolve().parents[1]

_HARNESSES = [
    "capture_render_test.mjs",
    "capture_crypto_test.mjs",
    "capture_relay_client_test.mjs",
    "capture_fragment_test.mjs",
    "capture_constraints_test.mjs",
    "capture_wakelock_test.mjs",
    "capture_return_url_test.mjs",
    "capture_level_events_test.mjs",
    "capture_setup_store_test.mjs",
    "capture_calibration_model_test.mjs",
    "capture_protocol_test.mjs",
    "capture_transport_integrity_test.mjs",
    "capture_host_stop_lifecycle_test.mjs",
]


@pytest.mark.parametrize("harness", _HARNESSES)
def test_capture_page_harness(harness: str):
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_JS_DIR / harness)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True, out
    assert out["passed"] >= 1, out


def test_capture_page_expired_link_message_points_back_to_speaker():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'message === "not_found"' in main_js
    assert "This one-time capture link has expired." in main_js
    assert "Return to the speaker page" in main_js


def test_capture_page_distinguishes_invalid_link_from_network_failure():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function relayBootFailureMessage(err)" in main_js
    assert "[401, 403, 404].includes(status)" in main_js
    assert 'message.includes("capture spec integrity")' in main_js
    assert "This authenticated measurement link is invalid" in main_js
    assert "Can't reach the measurement relay" in main_js
    assert "setStatus(relayBootFailureMessage(err), \"error\")" in main_js


def test_capture_page_version_contract_is_published_and_cache_busted():
    version = json.loads((_REPO / "capture-page/version.json").read_text())
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")
    build_sh = (_REPO / "capture-page/build.sh").read_text(encoding="utf-8")

    assert version == {
        "schema_version": 1,
        "capture_protocol_version": 2,
        "supported_capture_protocol_versions": [1, 2],
        "capture_page_build": "20260716.1",
    }
    assert "main.js?v=20260716-1" in index_html
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    assert 'from "./render.js?v=20260711-1"' in main_js
    assert 'from "./measurement-audio.js?v=20260711-4"' in main_js
    assert 'from "./constraints.js?v=20260711-4"' in main_js
    assert 'from "./relay-client.js?v=20260715-3"' in main_js
    assert 'from "./level-events.js?v=20260716-1"' in main_js
    assert 'cp "${HERE}/version.json" "${DIST}/version.json"' in build_sh


def test_capture_page_treats_host_stop_as_expected_control_flow():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'phase === "sweep_cancelled"' in main_js
    assert "Measurement stopped safely. The speaker page shows what happens next." in main_js
    assert "if (sweepCompleted === false) return;" in main_js


def test_capture_page_csp_allows_version_handshake_and_relay():
    """The compatibility handshake is same-origin; relay traffic is not."""
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'connect-src \'self\' https://relay.jasper.tech' in index_html
    assert 'new URL("../version.json", import.meta.url)' in main_js


def test_capture_page_completion_renders_return_cta():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")

    assert "safeReturnUrl" in main_js
    assert "Back to speaker" in main_js
    assert "renderCaptureComplete(ctx)" in main_js
    assert "display: inline-flex;" in index_html


def test_capture_page_waits_for_pi_sweep_completion():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'phase === "ambient_started"' in main_js
    assert "Measuring room noise — stay quiet and keep the phone still." in main_js
    assert "fetchPhoneStatus" in main_js
    assert 'phase === "sweep_complete"' in main_js
    assert "recordWindowMs" not in main_js


def test_capture_page_serial_models_match_pi_registry_keys():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "spec.calibration_models" in main_js
    for key in SUPPORTED_MODELS:
        assert f'value: "{key}"' not in main_js
        assert f'value: \'{key}\'' not in main_js
    for stale in (
        "minidsp_umik_1",
        "minidsp_umik_2",
        "dayton_imm_6c",
        "dayton_umm_6",
    ):
        assert stale not in main_js


def test_capture_page_preflights_guided_setup_before_start():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "validateSetupBeforeContinue(ctx)" in main_js
    assert "setup_validate: true" in main_js
    assert "setup_token" in main_js
    assert 'event.phase === "setup_validation_failed"' in main_js
    assert 'event.phase === "setup_validated"' in main_js
    assert "renderPositionCount(screenEl, ctx)" in main_js


def test_capture_page_level_ramp_uses_meter_protocol_without_wav_upload():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert (
        'import { runLevelRampProtocol } from "./level-events.js?v=20260716-1"'
        in main_js
    )
    assert 'spec.kind === "level_ramp"' in main_js
    assert "onLevelRampStart(ctx)" in main_js

    start = main_js.index("async function onLevelRampStart")
    end = main_js.index("async function waitForSweepComplete", start)
    level_path = main_js[start:end]
    assert "runLevelRampProtocol" in level_path
    assert "float32ToWavBlob" not in level_path
    assert "encryptWav" not in level_path
    assert "putBlob" not in level_path


def test_capture_page_compares_spec_to_normalized_mono_capture_width():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    constraints_js = (_REPO / "capture-page/js/constraints.js").read_text(
        encoding="utf-8",
    )
    measurement_js = (
        _REPO / "deploy/assets/shared/js/measurement-audio.js"
    ).read_text(encoding="utf-8")

    assert "capturedChannelCount: 1" in measurement_js
    assert "var ch=inp[0]&&inp[0][0]" in measurement_js
    assert "recorder.capturedChannelCount" in main_js
    assert "source_channel_count: realized.sourceChannelCount" in main_js
    assert "captured_channel_count: realized.capturedChannelCount" in main_js
    assert "capturedChannelCount = null" in constraints_js
    assert "checkedChannelCount === wantChannels" in constraints_js


def test_capture_page_level_ramp_uses_guided_mic_calibration_setup():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'spec.kind === "room_sweep" || spec.kind === "level_ramp"' in main_js
    assert 'ctx.spec.kind === "level_ramp"' in main_js
    assert "renderMicChoice(screenEl, ctx, inputs)" in main_js
    assert "renderCalibration(screenEl, ctx)" in main_js
    assert "renderLevelReady(screenEl, ctx)" in main_js
    level_ready_start = main_js.index("function renderLevelReady")
    level_ready_end = main_js.index("function renderBoundRoomReady", level_ready_start)
    level_ready_path = main_js[level_ready_start:level_ready_end]
    assert "renderScreen(screenEl, ctx.spec" in level_ready_path
    assert "onLevelRampStart(ctx)" in level_ready_path
    assert "Place the microphone as shown" not in level_ready_path

    start = main_js.index("async function onLevelRampStart")
    end = main_js.index("async function waitForSweepComplete", start)
    level_path = main_js[start:end]
    assert "setup: setupWirePayload()" in level_path
    assert "device: capture.device" in level_path


def test_capture_page_supports_bound_and_pi_owned_capture_only_setup():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    level_js = (_REPO / "capture-page/js/level-events.js").read_text(
        encoding="utf-8",
    )

    setup_store_js = (_REPO / "capture-page/js/setup-store.js").read_text(
        encoding="utf-8",
    )

    assert 'SETUP_STORAGE_KEY = "jts.capture.bound-setup.v2"' in setup_store_js
    assert "SETUP_IDLE_TTL_MS" in setup_store_js
    assert "SETUP_ABSOLUTE_TTL_MS" in setup_store_js
    assert "refreshBoundSetup(spec)" in main_js
    assert "setup_binding_id" in setup_store_js
    assert "setup_collect_positions" in main_js
    assert 'spec.kind === "room_sweep" && spec.setup_validation === false' in main_js
    assert "if (setupCaptureOnly)" in main_js
    assert "renderBoundRoomReady(screenEl, ctx)" in main_js
    assert "setup_identity: identity" in main_js
    assert "persistBoundSetup(ctx.spec, identity)" in main_js
    assert "setup: setupWirePayload()" in main_js
    assert "Raw serials/calibration text are forbidden" in level_js
    assert "validated compact setup binding" in level_js


def test_capture_page_names_the_signed_room_trust_repeat():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'ctx.spec.presentation_variant === "trust_repeat"' in main_js
    assert "Ready to repeat the main seat" in main_js
    assert "Ready for the main-seat trust check." in main_js
    assert "This extra capture checks that the result is trustworthy." in main_js


def test_capture_page_rejects_oversize_calibration_and_unproven_agc():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "MAX_CALIBRATION_TEXT_BYTES" in main_js
    assert "file.size" in main_js
    assert "utf8Size(content)" in main_js
    assert "smaller than 256 KiB" in main_js
    assert 'reason: "agc_not_proven_off"' in main_js
    assert "JTS will not play the level tone" in main_js


def test_capture_page_level_ramp_agc_gate_only_refuses_explicit_on():
    """iOS/WebKit never reports autoGainControl (getSettings() omits the key),
    so gating on `!== false` refused every iPhone. Only an explicit `true`
    (the browser affirmatively reports AGC on) refuses now; undefined/null
    proceeds as unattested and is empirically verified server-side from the
    ramp's own staircase (jasper/audio_measurement/ramp.py) instead."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("async function onLevelRampStart")
    end = main_js.index("async function waitForSweepComplete", start)
    level_path = main_js[start:end]

    assert "capture.settings.autoGainControl !== false" not in level_path
    assert "const realizedAgc = capture.settings.autoGainControl;" in level_path
    assert "if (realizedAgc === true) {" in level_path
    assert "const agcAttested = realizedAgc === false;" in level_path
    assert "agcFrozen: agcAttested," in level_path
    assert "agcUnattested: !agcAttested," in level_path
    # The explicit-on refusal copy is unchanged — only the gate condition
    # narrowed from "not proven false" to "proven true".
    assert (
        "This browser cannot prove automatic microphone gain is off, so JTS "
        "will not play the level tone." in level_path
    )


def test_capture_page_level_ramp_shows_friendly_agc_suspected_copy():
    """The Pi's empirical slope-verification failure (agc_suspected) gets a
    phone-facing explanation instead of the raw server error code."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("function renderLevelRampComplete")
    end = main_js.index("async function enumerateAudioInputs", start)
    ramp_complete = main_js[start:end]

    assert 'terminalError === "agc_suspected"' in ramp_complete
    assert (
        "Your phone is adjusting microphone levels automatically, which "
        "prevents accurate measurement. Try a different phone or a USB "
        "measurement microphone." in ramp_complete
    )


def test_capture_page_infers_calibration_from_pi_registry_without_serial():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "inferCalibrationModel(" in main_js
    assert "calibrationModels," in main_js
    assert 'mode: "serial"' in main_js
    assert "model: inferred.key" in main_js
    assert "umik-2" not in main_js.lower()
    assert "minidsp_umik2" not in main_js
    assert 'serial: ""' in main_js
    assert "if (!setupState.calibration.serial)" in main_js
    assert "Enter the microphone serial number." in main_js
    assert "sessionStorage" not in main_js


def test_capture_page_level_completion_does_not_promise_wrong_next_step():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "ready for the measurement sweep" not in main_js
    assert "Level matched. The speaker continues on its own." in main_js


def test_capture_page_terminal_screens_describe_outcome_not_command_return():
    """Owner-directed reframe: terminal screens describe what happens next —
    the household never needs to physically return to the speaker, since the
    wizard auto-advances on its own. Pins the PHONE-1/XOVER-6 copy."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("function renderLevelRampComplete")
    end = main_js.index("async function enumerateAudioInputs", start)
    ramp_complete = main_js[start:end]
    assert "Return to the speaker" not in ramp_complete
    assert (
        "Level matched. The speaker will continue on its own — "
        "you can put this phone down." in ramp_complete
    )
    assert "The speaker page shows what happens next." in ramp_complete

    assert (
        "Measurement uploaded. The speaker will continue automatically."
        in main_js
    )
    assert "You can close this tab." in main_js


def test_capture_page_sweep_failed_renders_terminal_screen_not_dead_start():
    """XOVER-6 interim: sweep_failed used to leave the Start-button screen
    visible with a retry that replays a stale spec/run_token. It must now
    render a terminal outcome screen instead, like ramp failures do."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function renderSweepFailed(ctx, err)" in main_js
    assert "failure.sweepFailed = true" in main_js
    assert "if (err && err.sweepFailed) {" in main_js
    assert "renderSweepFailed(ctx, err);" in main_js

    start = main_js.index("function renderSweepFailed")
    end = main_js.index("async function enumerateAudioInputs", start)
    sweep_failed_path = main_js[start:end]
    assert "Tap Start to try again" not in sweep_failed_path
    assert "The speaker page shows what happens next." in sweep_failed_path


def test_capture_page_no_return_link_falls_back_to_close_tab_copy():
    """PHONE-2: when safeReturnUrl() is empty, the terminal screens that
    otherwise render a Back-to-speaker button must not silently drop the CTA
    with no replacement copy. 3 pre-existing call sites (capture complete,
    ramp complete, bound-setup-expired) plus the new XOVER-6 sweep_failed
    screen, which needs the same fallback."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert main_js.count('linkButton("Back to speaker", returnUrl)') == 4
    assert main_js.count('text: "You can close this tab."') == 4


def test_capture_page_setup_continue_and_fragment_errors_use_friendly_helper():
    """PHONE-3: the calibration-continue, position-count-continue, and
    fragment-parse error paths used to surface raw exception text with their
    own ad hoc ternary instead of the shared captureFailureMessage() helper."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert (
        'setStatus(err && err.message ? String(err.message) : String(err), "error")'
        not in main_js
    )

    start = main_js.index("handle = parseFragment(")
    end = main_js.index("client = new RelayClient(", start)
    boot_fragment_path = main_js[start:end]
    assert "setStatus(captureFailureMessage(err), \"error\");" in boot_fragment_path


def test_capture_page_names_the_device_instead_of_ambiguous_this_page():
    """Item 6: backgrounded-abort copy said 'stay on this page', ambiguous
    about which device. Name the phone explicitly."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "must stay on this page" not in main_js
    assert "this phone's screen must stay on" in main_js


def test_crossover_candidate_review_collapses_provenance_hashes():
    """PHONE-4: renderCandidateReview() lives in the Pi-served /correction/
    crossover wizard (deploy/assets/correction/js/crossover/main.js), not
    capture-page/ — the reviewer's cited surface is what actually renders
    candidate hashes to the household. Raw fingerprints/algorithm id+version
    move behind a collapsed <details> disclosure; the region/driver rows stay
    primary, plain-language copy."""
    crossover_js = (
        _REPO / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")

    assert "el('details', {class: 'candidate-provenance'}" in crossover_js
    assert "el('summary', {text: 'Technical details'})" in crossover_js
    assert "evidence.algorithm_id" in crossover_js
