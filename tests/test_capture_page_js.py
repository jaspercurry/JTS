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
    "capture_stop_and_ambient_countdown_test.mjs",
    "capture_ambient_stats_test.mjs",
    "capture_plan_loop_test.mjs",
    "capture_calibration_confirm_test.mjs",
    "capture_defect_fixes_test.mjs",
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
        "capture_protocol_version": 3,
        "supported_capture_protocol_versions": [1, 2, 3],
        "capture_page_build": "20260717.3",
    }
    assert "main.js?v=20260717-3" in index_html
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    assert 'from "./render.js?v=20260711-1"' in main_js
    assert 'from "./measurement-audio.js?v=20260711-4"' in main_js
    assert 'from "./constraints.js?v=20260711-4"' in main_js
    assert 'from "./relay-client.js?v=20260717-1"' in main_js
    assert 'from "./level-events.js?v=20260716-1"' in main_js
    assert 'from "./ambient-stats.js?v=20260717-1"' in main_js
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
    phone-facing explanation instead of the raw server error code — and the
    INDETERMINATE outcome (agc_indeterminate: insufficient evidence, no AGC
    observed) gets its own honest copy that does not claim AGC was seen."""
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
    assert 'terminalError === "agc_indeterminate"' in ramp_complete
    assert (
        "JTS couldn't gather enough measurement evidence to verify this "
        "microphone's level accuracy. Try again, or use a different "
        "microphone or device." in ramp_complete
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
    ramp complete, bound-setup-expired), the XOVER-6 sweep_failed screen, the
    phone-initiated Stop terminal screen (renderStoppedScreen), the run-19
    dead-session terminal (renderSessionExpired), and the three new v3
    session-plan terminals (renderPlanAllDone, renderPlanRefused,
    renderPlanExhausted) all need the same fallback."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert main_js.count('linkButton("Back to speaker", returnUrl)') == 9
    assert main_js.count('text: "You can close this tab."') == 9


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


# ---------------------------------------------------------------------------
# Wave 2 (SPEC W2.3 session-spanning relay + W2.1 ambient stats + W2.2
# one-tap mic confirm — the no-ping-pong batch)
# ---------------------------------------------------------------------------


def test_capture_page_v3_plan_routes_begin_capture_to_the_plan_loop():
    """A capture_protocol_version=3 spec with a capture_plan wires the
    spec-rendered Start button to onPlanStart(); anything else (v1/v2, or a
    v3-shaped protocol number with no plan — impossible per
    CaptureSpec.validate() but checked defensively anyway) keeps today's
    single-capture onStart() untouched."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert (
        "const isPlanSpec = spec.capture_protocol_version === 3 && Boolean(spec.capture_plan);"
        in main_js
    )
    assert "begin_capture: () => (isPlanSpec ? onPlanStart(ctx) : onStart(ctx))," in main_js
    # v2 keeps its exact single-capture behavior — the "retry" action (a v2-
    # only affordance; v3 never emits it) is untouched, still onStart.
    assert "retry: () => onStart(ctx)," in main_js


def test_capture_page_plan_loop_derives_named_screens_for_every_outcome():
    """Pins the plan loop's screen vocabulary: accepted-but-not-final (Next),
    rejected (Try again, SAME slot next attempt), refused (terminal, no
    retry), exhausted (terminal, distinct from success), and the final
    success terminal — matching SPEC W2.3's choreography."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'text: `Measurement ${index} of ${target} ✓`' in main_js
    assert 'text: `Measurement ${index} of ${target} needs another try`' in main_js
    assert "await runPlanCapture(ctx, { index, attempt: attempt + 1 });" in main_js
    assert "await runPlanCapture(ctx, { index: index + 1, attempt: attempt + 1 });" in main_js
    assert '"All measurements done — the speaker continues automatically."' in main_js
    assert 'text: "Measurement refused"' in main_js
    assert 'text: "Reached the attempt limit"' in main_js
    # Refusal and exhaustion never route through the success text.
    refused_start = main_js.index("function renderPlanRefused")
    refused_end = main_js.index("function renderPlanExhausted", refused_start)
    assert "All measurements done" not in main_js[refused_start:refused_end]


def test_capture_page_plan_loop_timeouts_are_terminal_not_stale_retries():
    """A begin-authorization or result-poll timeout in the plan loop must
    render a terminal screen (renderSweepFailed's shape — no button), not
    leave the previous "Next measurement"/"Try again" screen up with a
    button closure still bound to an (index, attempt) the Pi's own state may
    have already moved past. Retrying that stale pair risks a fatal
    begin_replayed refusal (run_capture_plan ends the whole session on ANY
    capture_refused)."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("async function waitForCaptureAuthorized(")
    end = main_js.index("async function waitForCaptureResult(", start)
    authorized_body = main_js[start:end]
    assert "failure.sweepFailed = true;" in authorized_body
    assert "throw failure;" in authorized_body

    start = main_js.index("async function waitForCaptureResult(")
    end = main_js.index("async function runPlanCapture(", start)
    result_body = main_js[start:end]
    assert "failure.sweepFailed = true;" in result_body
    assert "throw failure;" in result_body
    # The result wait scales with the recording window rather than reusing
    # the tight admission-latency budget — the Pi's own consume_capture()
    # analysis pass has no hard ceiling from run_capture_plan's poll loop.
    assert "Math.max(30000, Number(spec.duration_ms) || 30000)" in result_body


def test_capture_page_plan_loop_post_arm_errors_are_terminal_pre_arm_retries():
    """S1 (adversarial review of this PR): runPlanCapture's generic catch-all
    used to leave the previous "Next measurement"/"Try again" button live and
    bound to the SAME (index, attempt) already posted — a re-tap after e.g. a
    transient putBlob failure posts a begin the Pi refuses (begin_replayed /
    out_of_order → session-ending CaptureFailed), or worse re-records a
    sweep-less window. The catch now splits on whether `armed` was posted:
    post-arm generic errors render the terminal failure screen (mirroring
    the timeout paths); pre-arm errors (mic permission denied, a begin-post
    hiccup) keep the live retry — correct there, the round never started on
    the Pi — with Stop still wired and copy naming the ACTUAL on-screen
    affordance (N3). Behavior exercised in capture_plan_loop_test.mjs; these
    pins keep the wiring from silently regressing."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("async function runPlanCapture(ctx, { index, attempt }) {")
    end = main_js.index("async function onPlanStart(ctx)", start)
    run_body = main_js[start:end]
    assert "let armedPosted = false;" in run_body
    assert "armedPosted = true;" in run_body
    # armedPosted is set BEFORE the armed post's await — a lost response may
    # still have armed the Pi, so a failed post must classify as post-arm.
    assert run_body.index("armedPosted = true;") < run_body.index("armed: true,")
    assert "} else if (armedPosted) {" in run_body
    assert "setStatus(captureFailureMessage(err, planRetryAffordance(ctx)), \"error\");" in run_body
    # The post-arm upload-cap refusal is terminal too (sweepFailed routing),
    # and the pre-arm clean-capture refusal keeps the session alive: exactly
    # the sweepFailed/deadSession/armedPosted terminal branches call
    # endPlanSession inside the catch, never the pre-arm else.
    assert "failure.sweepFailed = true;" in run_body
    assert "function planRetryAffordance(ctx) {" in main_js
    # captureFailureMessage's affordance parameter defaults to the v1/v2
    # flows' real Start button.
    assert 'function captureFailureMessage(err, retryAction = "Start") {' in main_js


def test_capture_page_plan_loop_blob_upload_carries_the_capture_index():
    """Each admitted attempt's blob rides capture_index = attempt - 1 (SPEC
    W2.3) — a retried slot must never clobber the prior attempt's upload."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "await client.putBlob(blob, plaintextLen, sha256, attempt - 1);" in main_js


def test_capture_page_plan_loop_acknowledgement_captured_once_not_per_round():
    """The placement acknowledgement is derived ONCE at plan start (from the
    spec-rendered checkbox) and threaded through every round's armed event —
    there is no per-round checkbox on the page-owned Next/Try-again screens."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("async function onPlanStart(ctx)")
    end = main_js.index("// The whole capture leg, behind the single Start tap.", start)
    plan_start_body = main_js[start:end]
    assert "acceptedAcknowledgement(ctx.spec, ctx.captureRefs)" in plan_start_body
    assert "ctx.planAcknowledgement = acknowledgement;" in plan_start_body
    assert "acknowledgement: ctx.planAcknowledgement," in main_js


def test_capture_page_plan_loop_stop_stays_wired_across_rounds():
    """activeAbort is set ONCE in onPlanStart and persists across every
    round's async gaps (the idle time between "Next measurement" taps), only
    clearing at a genuine terminal outcome (endPlanSession) or Stop itself —
    never per-round, which would leave Stop dead while idling between
    captures."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "activeAbort = controller.abort;" in main_js
    assert "function endPlanSession(ctx) {" in main_js
    assert "if (activeAbort === state.abort) activeAbort = null;" in main_js


def test_capture_page_ambient_stats_rides_the_armed_event_not_a_separate_post():
    """The relay's phone-event slot is last-write-wins: a standalone
    ambient_stats event posted before `armed` would almost always be
    overwritten before the Pi's ~0.75s poll ever saw it. ambientStatsFieldsFor
    is spread directly into the SAME already-awaited armed postEvent call in
    both onStart (v1/v2) and the plan loop (v3) — zero extra network round
    trips, "must not delay the capture sequence" for free."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert main_js.count("...ambientStatsFieldsFor(spec, noise),") == 2
    assert 'spec.kind !== "crossover_sweep"' in main_js


def test_capture_page_one_tap_mic_confirm_renders_when_hint_is_valid():
    """Wave-2 household-mic prefill hint (CaptureSpec.default_setup_calibration,
    #1540, adjudicated stored-submit amendment): the calibration screen shows
    "Using {label}{· serial}" as the primary action with a safe "Use a
    different microphone" fallback to today's full picker. Only offered when
    the Pi marked the hint `resolvable: true` (the stored-mode Pi build mints
    that only when the calibration_id currently resolves); a hint without the
    marker — an older Pi — renders the plain full picker (compat pin)."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function validDefaultSetupHint(spec) {" in main_js
    assert "if (hint.resolvable !== true) return null;" in main_js
    assert "function renderCalibrationConfirm(screenEl, ctx, hint) {" in main_js
    assert (
        "const heading = serialDisplay ? `Using ${label} · ${serialDisplay}` : `Using ${label}`;"
        in main_js
    )
    assert 'button("One tap to confirm", async () => {' in main_js
    assert 'button("Use a different microphone", () => {' in main_js
    assert "renderCalibration(screenEl, ctx, { skipHint: true });" in main_js
    # The gate: renderCalibration only shows the hint screen on a FRESH
    # visit (calibration.mode still "none"), never after the household has
    # already picked something (Back navigation from a later step).
    assert (
        'if (hint && String((setupState.calibration || {}).mode || "none") === "none") {'
        in main_js
    )


def test_capture_page_one_tap_confirm_submits_stored_and_falls_back_on_rejection():
    """Adjudicated stored-submit contract (amendment to the original
    stop-and-report): Confirm submits setup.calibration = {mode: "stored",
    calibration_id} (+ model, display-only) through the SAME shared
    post-calibration advance the picker's Continue uses
    (continueFromCalibration → validateSetupBeforeContinue /
    bindSetupBeforeLevel), and a Pi rejection (the household-mic record went
    stale between spec mint and submit) falls back to the full picker with a
    plain sentence — never a dead end — including the one DEFERRED-validation
    path (a position-collecting level_ramp validates at the position screen's
    bind). A failed one-tap is not re-offered within the page session
    (storedHintFailed). Behavior is exercised end-to-end in
    tests/js/capture_calibration_confirm_test.mjs; these pins keep the wiring
    from silently regressing."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("function renderCalibrationConfirm(screenEl, ctx, hint) {")
    end = main_js.index("function renderCalibration(screenEl, ctx", start)
    confirm_body = main_js[start:end]
    assert 'mode: "stored",' in confirm_body
    assert "calibration_id: String(hint.calibration_id)," in confirm_body
    assert 'model: String(hint.model || ""),' in confirm_body
    assert "await continueFromCalibration(screenEl, ctx);" in confirm_body
    assert "fallBackFromStoredCalibration(screenEl, ctx);" in confirm_body

    # One shared advance for both the picker Continue and the stored Confirm.
    assert "async function continueFromCalibration(screenEl, ctx) {" in main_js
    assert main_js.count("await continueFromCalibration(screenEl, ctx);") == 2

    # The rejection fallback: plain sentence, picker re-render, no re-offer.
    assert "function fallBackFromStoredCalibration(screenEl, ctx) {" in main_js
    assert (
        "The speaker couldn't use the saved microphone calibration. "
        "Set up the microphone manually instead." in main_js
    )
    assert "let storedHintFailed = false;" in main_js
    assert "storedHintFailed = true;" in main_js
    assert (
        "const hint = !skipHint && !storedHintFailed ? validDefaultSetupHint(ctx.spec) : null;"
        in main_js
    )

    # The deferred-validation path (position-collecting level_ramp) keeps the
    # same rejection contract at its bind.
    assert "if (usedStoredCalibration()) {" in main_js


def test_capture_page_upload_note_requires_actually_loaded_content():
    """The upload-mode picker's "Choose the file again only if you want to
    replace the current selection" note requires calibration.content, not
    just mode === "upload" — the note must only appear after a REAL upload
    landed in setupState.calibration this session, never for a mode value
    that arrived without file content (saveAndContinue's reuse branch also
    requires .content, so the two stay consistent)."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert (
        '(setupState.calibration || {}).mode === "upload" && (setupState.calibration || {}).content'
        in main_js
    )


def test_capture_page_mic_picker_never_erases_the_stored_preference():
    """Run-19 defect (a): renderMicChoice/buildMicPicker used to call
    rememberDeviceId("") the moment the remembered device wasn't in THIS
    render's enumerated list (unplugged right now, or a browser-rotated
    deviceId) — permanently erasing a good preference even though the same
    physical mic would have matched again next session. The in-memory
    fallback to Automatic stays (selectedDeviceId = ""); only the
    destructive localStorage write is gone."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert main_js.count('rememberDeviceId("");') == 0
    assert main_js.count("selectedDeviceId = \"\";") >= 2
    assert "never erase the stored" in main_js or "do NOT erase the stored" in main_js


def test_capture_page_dead_relay_session_never_offers_a_doomed_retry():
    """Run-19 defect (c): every phone-facing relay endpoint 404s "not_found"
    once a session's TTL lapses or the Pi purges it, so "Tap Start to try
    again" against a dead session is a guaranteed second failure.
    isDeadSessionError() is checked before the generic captureFailureMessage
    fallback in onStart, onLevelRampStart, and the plan loop's begin/result
    polls + top-level catch."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function isDeadSessionError(err) {" in main_js
    assert "function renderSessionExpired(ctx) {" in main_js
    assert (
        "This measurement link expired — return to the speaker page to start again."
        in main_js
    )
    assert main_js.count("isDeadSessionError(err)") >= 4
    assert main_js.count("renderSessionExpired(ctx);") >= 4


def test_capture_page_abort_signal_never_leaks_the_raw_dom_exception():
    """Run-19 defect (b): relay-client.js's _controlFetch now aborts with a
    named Error so a timed-out control request never surfaces the browser's
    default "signal is aborted without reason." text; main.js additionally
    normalizes ANY AbortError defensively (isRelayConnectivityAbort)."""
    relay_client_js = (_REPO / "capture-page/js/relay-client.js").read_text(encoding="utf-8")
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "controller.abort(" in relay_client_js
    assert "new Error(" in relay_client_js
    assert "function isRelayConnectivityAbort(err, message) {" in main_js
    assert (
        "Lost the connection to the speaker's measurement relay for a moment."
        in main_js
    )


def test_capture_page_blob_put_supports_an_optional_capture_index():
    """relay-client.js's putBlob() gains an optional 4th `captureIndex` arg
    that appends `?index=N`; omitted stays byte-identical to the pre-Wave-2
    single-capture request (no query string at all)."""
    relay_client_js = (_REPO / "capture-page/js/relay-client.js").read_text(encoding="utf-8")

    assert "async putBlob(blob, plaintextLen, sha256Hex, captureIndex) {" in relay_client_js
    assert '`/blob?index=${captureIndex}`' in relay_client_js
