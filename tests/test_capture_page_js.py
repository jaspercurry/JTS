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
        "capture_page_build": "20260714.1",
    }
    assert "main.js?v=20260714-1" in index_html
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    assert 'from "./render.js?v=20260711-1"' in main_js
    assert 'from "./measurement-audio.js?v=20260711-4"' in main_js
    assert 'from "./constraints.js?v=20260711-4"' in main_js
    assert 'cp "${HERE}/version.json" "${DIST}/version.json"' in build_sh


def test_capture_page_treats_host_stop_as_expected_control_flow():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'phase === "sweep_cancelled"' in main_js
    assert "Measurement stopped safely. Return to the speaker" in main_js
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

    assert 'import { runLevelRampProtocol } from "./level-events.js"' in main_js
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
    assert "capture.settings.autoGainControl !== false" in main_js
    assert 'reason: "agc_not_proven_off"' in main_js
    assert "JTS will not play the level tone" in main_js


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
    assert "Level matched — return to the speaker for the next step." in main_js
