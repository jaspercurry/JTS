// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Capture-page orchestration (build step 3). Browser-only — the pure, testable
// pieces live in fragment.js / render.js / crypto.js / relay-client.js; this
// module wires them to the DOM, the microphone, and the relay.
//
// One screen, one tap (plan §5): the Start tap records passive room noise, starts
// the local sweep recording, and drops the `armed` flag the Pi polls before it
// plays the stimulus. The page and the Pi never talk directly — only through the
// relay.

import { RELAY_BASE } from "./config.js";
import { parseFragment, withinUploadCap } from "./fragment.js?v=20260711-3";
import {
  acceptedAcknowledgement,
  renderScreen,
} from "./render.js?v=20260711-1";
import { RelayClient } from "./relay-client.js?v=20260717-1";
import { importContentKey, encryptWav } from "./crypto.js";
import {
  constraintDecision,
  verifyRealizedConstraints,
} from "./constraints.js?v=20260711-4";
import { safeReturnUrl } from "./return-url.js";
import {
  acquireWakeLock,
  watchVisibilityAbort,
  watchVisibilityReacquire,
} from "./wakelock.js";
import { runLevelRampProtocol } from "./level-events.js?v=20260716-1";
import { inferCalibrationModel } from "./calibration-model.js?v=20260712-1";
import {
  assertCaptureProtocolCompatible,
  requiredCaptureProtocol,
  validateCapturePageIdentity,
} from "./capture-protocol.js";
import { verifyAndParseCaptureSpec } from "./transport-integrity.js?v=20260711-3";
import {
  loadBoundSetup,
  refreshBoundSetup,
  setupBindingId,
  storeBoundSetup,
} from "./setup-store.js";
import {
  createMonoRecorder,
  delayMs,
  float32ToWavBlob,
  rmsToDbfs,
} from "./measurement-audio.js?v=20260711-4";
import { buildAmbientStatsEvent } from "./ambient-stats.js?v=20260717-1";

const PAGE_VERSION_URL = new URL("../version.json", import.meta.url);

async function loadCapturePageIdentity() {
  const response = await globalThis.fetch(PAGE_VERSION_URL, { cache: "no-store" });
  if (!response.ok) throw new Error(`capture page version unavailable (${response.status})`);
  return validateCapturePageIdentity(await response.json());
}

// The input the household picked (empty = OS default). Keep it on the trusted
// capture origin so the level stage and following driver/sweep links use the
// same microphone without asking the household to rediscover it each time.
const DEVICE_STORAGE_KEY = "jts.capture.selected-device";
const SETUP_IDENTITY_SCHEMA = 1;
// Calibration files are ordinarily a few kilobytes. Keep enough headroom for
// real vendor files while staying well below the relay's 1 MiB event ceiling:
// the full text is sent exactly once for Pi validation, never in meter batches.
const MAX_CALIBRATION_TEXT_BYTES = 256 * 1024;
function storedDeviceId() {
  try {
    return globalThis.localStorage
      ? String(globalThis.localStorage.getItem(DEVICE_STORAGE_KEY) || "")
      : "";
  } catch {
    return "";
  }
}
function rememberDeviceId(value) {
  try {
    if (globalThis.localStorage) {
      globalThis.localStorage.setItem(DEVICE_STORAGE_KEY, String(value || ""));
    }
  } catch {
    /* privacy mode/storage denial: the in-memory choice still works */
  }
}
let selectedDeviceId = storedDeviceId();
let setupInputs = [];
let setupState = {
  total_positions: 5,
  calibration: { mode: "none" },
};
let setupIdentity = null;
let setupCaptureOnly = false;

function utf8Size(value) {
  return new TextEncoder().encode(String(value || "")).byteLength;
}

function collectsRoomPositions(spec) {
  return Boolean(spec && spec.setup_collect_positions === true);
}

function persistBoundSetup(spec, identity) {
  // Deliberately persist only a compact summary and digest. Raw serials and
  // uploaded file text exist in memory only until the one validation POST.
  const calibration = setupState.calibration || {};
  return storeBoundSetup(spec, identity, {
    total_positions: Number(setupState.total_positions) || 5,
    calibration: {
      mode: String(calibration.mode || "none"),
      model: String(calibration.model || ""),
    },
  });
}

async function sha256Hex(value) {
  const cryptoObj = globalThis.crypto || {};
  if (!cryptoObj.subtle || typeof cryptoObj.subtle.digest !== "function") {
    throw new Error("this browser cannot securely bind the microphone setup");
  }
  const digest = await cryptoObj.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(String(value)),
  );
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function buildSetupIdentity(spec) {
  const bindingId = setupBindingId(spec);
  if (!bindingId) return null;
  return {
    schema: SETUP_IDENTITY_SCHEMA,
    binding_id: bindingId,
    sha256: await sha256Hex(JSON.stringify(setupState)),
  };
}

function setupWirePayload() {
  if (setupIdentity) return { binding: setupIdentity };
  return setupCaptureOnly ? null : setupState;
}

function setStatus(message, kind = "info") {
  const el = document.getElementById("status");
  if (el) {
    el.textContent = message;
    el.dataset.kind = kind;
  }
}

// Fallback copy (#1658) for a phone whose browser has no Screen Wake Lock API,
// or whose request was rejected — the household still needs SOME signal to
// keep the screen on by hand, since the capture can run for several minutes.
const WAKE_LOCK_HINT_TEXT = "Keep your screen on — this takes about 4 minutes.";

function showWakeLockHint() {
  const el = document.getElementById("wakelock-hint");
  if (el) el.textContent = WAKE_LOCK_HINT_TEXT;
}

function hideWakeLockHint() {
  const el = document.getElementById("wakelock-hint");
  if (el) el.textContent = "";
}

// Wraps acquireWakeLock() with the fallback hint. Wake-lock failure is
// best-effort and must never break the capture flow — this never throws;
// `lock.supported` tells the caller whether a real lock was taken, exactly
// like the wrapped function.
async function acquireWakeLockWithHint() {
  const lock = await acquireWakeLock();
  if (lock.supported === false) {
    console.debug("capture page: screen wake lock unsupported, showing on-screen hint");
    showWakeLockHint();
  }
  return lock;
}

function setupValidationToken() {
  const cryptoObj = globalThis.crypto || {};
  if (typeof cryptoObj.randomUUID === "function") return cryptoObj.randomUUID();
  const random = Math.random().toString(36).slice(2);
  return `setup-${Date.now()}-${random}`;
}

// A control request that timed out (see relay-client.js's _controlFetch)
// rejects with a named Error, but stays defensive here too: an older browser
// or an AbortController used elsewhere could still surface its native
// "signal is aborted without reason." DOMException text verbatim, which read
// as gibberish to a household (run-19 defect). Treat ANY AbortError the same
// way regardless of its exact message.
function isRelayConnectivityAbort(err, message) {
  return (
    (err && err.name === "AbortError") ||
    /signal is aborted/i.test(message)
  );
}

// `retryAction` names the button the retry copy points at. The v1/v2 flows
// keep the default "Start" (their spec screens all render a Start-labeled
// begin button); the v3 plan loop passes the label of whatever begin
// affordance is actually on screen ("Next measurement" / "Try again" / the
// spec's own button label) — its screens have no button called Start.
function captureFailureMessage(err, retryAction = "Start") {
  const message = err && err.message ? String(err.message) : String(err);
  if (message === "not_found") {
    return (
      "This one-time capture link has expired. Return to the speaker page " +
      "and create a new phone capture link."
    );
  }
  if (isRelayConnectivityAbort(err, message)) {
    return (
      "Lost the connection to the speaker's measurement relay for a moment. " +
      `Tap ${retryAction} to try again.`
    );
  }
  // Trim a trailing period so wrapping a message that is already a full
  // sentence (e.g. FragmentError's own friendly text) never produces "..".
  return `Measurement failed: ${message.replace(/\.+$/, "")}. Tap ${retryAction} to try again.`;
}

// Whether `err` means the relay SESSION itself is gone (expired/purged) —
// offering "Tap Start to try again" against a dead session is a guaranteed
// second failure (run-19 defect): every phone-facing endpoint 404s
// "not_found" once the Pi's session TTL lapses or the Pi purges it, so
// retrying with the SAME dead link cannot ever succeed. Mirrors
// relayBootFailureMessage's status check below (boot-time equivalent).
function isDeadSessionError(err) {
  const status = Number(err && err.status);
  if ([401, 403, 404].includes(status)) return true;
  return String((err && err.message) || "") === "not_found";
}

function relayBootFailureMessage(err) {
  const message = String(err && err.message || "");
  const status = Number(err && err.status);
  if (
    [401, 403, 404].includes(status) ||
    message.includes("capture spec integrity") ||
    message.includes("capture spec is invalid")
  ) {
    return (
      "This authenticated measurement link is invalid, expired, or from an " +
      "older speaker version. Return to the speaker page and create a new link."
    );
  }
  if (message.includes("incompatible")) {
    return (
      `${message}. Return to the speaker and update it, publish the matching ` +
      "capture page, or create a fresh link from the speaker page before trying again."
    );
  }
  return (
    "Can't reach the measurement relay. New measurements need an internet " +
    "connection; any correction already applied to your speaker still works."
  );
}

async function waitForSetupValidation(ctx, token) {
  const pollMs = Math.max(100, Math.min(1000, Number(ctx.spec.progress_poll_ms) || 250));
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const status = await ctx.client.fetchPhoneStatus();
    const event = (status && status.host_event) || {};
    if (event.phase === "capture_incompatible") {
      throw new Error(event.error || "capture page is incompatible with this speaker");
    }
    if (String(event.setup_token || "") === token) {
      if (event.phase === "setup_validated") return;
      if (event.phase === "setup_validation_failed") {
        throw new Error(event.error || "speaker could not validate that calibration");
      }
    }
    await delayMs(pollMs);
  }
  throw new Error("speaker did not validate the calibration before the timeout");
}

async function validateSetupBeforeContinue(ctx, identity = null) {
  const calibration = setupState.calibration || {};
  // A bound flow validates even an uncalibrated phone mic: its acknowledgement
  // freezes the setup. Modern unbound Room capture-only specs instead carry
  // Pi-owned position progress and report the realized mic when they arm.
  if (
    !ctx.spec.setup_validation ||
    (!setupBindingId(ctx.spec) && calibration.mode === "none")
  ) return;
  const token = setupValidationToken();
  setStatus("Checking calibration on the speaker…", "info");
  await ctx.client.postEvent({
    setup_validate: true,
    setup_token: token,
    setup: setupState,
    setup_identity: identity,
  });
  await waitForSetupValidation(ctx, token);
}

async function bindSetupBeforeLevel(ctx) {
  if (!setupBindingId(ctx.spec) || ctx.spec.setup_validation !== true) {
    throw new Error(
      "the speaker capture protocol cannot validate a bound setup; update the speaker before running this level check",
    );
  }
  const identity = await buildSetupIdentity(ctx.spec);
  await validateSetupBeforeContinue(ctx, identity);
  setupIdentity = identity;
  if (
    identity &&
    collectsRoomPositions(ctx.spec) &&
    !persistBoundSetup(ctx.spec, identity)
  ) {
    throw new Error(
      "this browser could not retain the setup for the next room position; " +
        "allow site storage or use another browser",
    );
  }
}

async function blobToBytes(blob) {
  return new Uint8Array(await blob.arrayBuffer());
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "for") node.htmlFor = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value !== false && value !== null && value !== undefined) {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function button(label, onClick, secondary = false) {
  return el(
    "button",
    {
      type: "button",
      class: secondary ? "cap-button cap-button--secondary" : "cap-button",
      onclick: onClick,
      text: label,
    },
  );
}

function linkButton(label, href) {
  return el("a", {
    class: "cap-button",
    href,
    text: label,
  });
}

function setScreen(screenEl, children) {
  screenEl.replaceChildren(...children);
}

function renderCaptureComplete(ctx) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const children = [
    el("h1", { class: "cap-heading", text: "Measurement uploaded" }),
    el("p", {
      class: "cap-note",
      text: "Measurement uploaded. The speaker will continue automatically.",
    }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", {
      class: "cap-note",
      text: "You can close this tab.",
    }));
  }
  setScreen(ctx.screenEl, children);
}

function renderStoppedScreen(ctx) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const children = [
    el("h1", { class: "cap-heading", text: "Measurement stopped." }),
    el("p", {
      class: "cap-note",
      text: "You stopped this measurement. The speaker page shows what happens next.",
    }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus("Measurement stopped.", "info");
}

// The relay session died mid-flow (expired TTL, or the Pi purged it) — see
// isDeadSessionError(). A "Tap Start to try again" retry against a dead
// session is a guaranteed second failure, so this renders a terminal screen
// with no retry affordance instead (run-19 defect).
function renderSessionExpired(ctx) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const children = [
    el("h1", { class: "cap-heading", text: "Link expired" }),
    el("p", {
      class: "cap-note",
      text: "This measurement link expired — return to the speaker page to start again.",
    }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus(
    "This measurement link expired — return to the speaker page to start again.",
    "error",
  );
}

// The Stop button's one job: call whichever capture leg's own `abort(reason)`
// is currently live, with the SAME "reason" vocabulary the visibility-abort
// path already uses (capture_host_stop_lifecycle_test.mjs;
// capture-page/js/level-events.js's aborted/abort_reason superset) — no new
// protocol, just a second trigger for the existing one. `onStart` and
// `onLevelRampStart` each point this at their own local `abort` closure while
// their capture is live, and clear it in `finally` so a stray tap after
// completion is a no-op.
let activeAbort = null;

function stopCapture() {
  // Returns the abort() promise (rather than firing-and-forgetting it) so a
  // caller — a test, or `onclick`'s own no-op handling of the return value —
  // can await its side effects (the relay post, the terminal screen) settling.
  return typeof activeAbort === "function" ? activeAbort("stopped") : undefined;
}

// XOVER-6 interim: sweep_failed used to bubble up through onStart's generic
// catch and leave the Start-button screen on-screen — a dead affordance,
// since a retry replays the same (now stale) spec/run_token rather than
// fetching a fresh one. Render a terminal screen instead, matching the
// renderLevelRampComplete pattern: state the outcome, then point at the
// speaker page rather than offering a retry that cannot work. A proper fix
// (session refresh so retry is live again) is a separate, larger change.
function renderSweepFailed(ctx, err) {
  const message = (err && err.message ? String(err.message) : String(err)).replace(/\.+$/, "");
  const returnUrl = safeReturnUrl(ctx.spec);
  const children = [
    el("h1", { class: "cap-heading", text: "Measurement failed" }),
    el("p", {
      class: "cap-note",
      text: `${message}. The speaker page shows what happens next.`,
    }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus("Measurement failed. The speaker page shows what happens next.", "error");
}

function renderLevelRampComplete(ctx, ramp) {
  const state = String((ramp && ramp.state) || "error");
  const terminalError = String((ramp && ramp.error) || "").trim();
  const returnUrl = safeReturnUrl(ctx.spec);
  const messages = {
    locked: {
      heading: "Level matched",
      note: "Level matched. The speaker will continue on its own — you can put this phone down.",
      status: "Level matched. The speaker continues on its own.",
      kind: "done",
    },
    maxed_out: {
      heading: "Level check needs attention",
      note: "The speaker reached its safe software limit. The speaker page shows what happens next.",
      status: "Safe software limit reached. The speaker page shows what happens next.",
      kind: "error",
    },
    aborted: {
      heading: "Level check stopped",
      note: "The level check was stopped. The speaker page shows what happens next.",
      status: "Level check stopped.",
      kind: "error",
    },
    cancelled: {
      heading: "Level check cancelled",
      note: "The speaker cancelled this level check. The speaker page shows what happens next.",
      status: "Level check cancelled.",
      kind: "error",
    },
    error: {
      heading: "Level check failed",
      note: "The speaker could not lock a safe level. The speaker page shows what happens next.",
      status: "Level check failed. The speaker page shows what happens next.",
      kind: "error",
    },
  };
  const message = messages[state] || messages.error;
  if (state === "error" && terminalError === "agc_suspected") {
    // The Pi observed a flat reported-vs-commanded staircase slope — this
    // phone's mic chain IS adjusting gain automatically. Friendly copy
    // instead of the raw server code.
    message.note =
      "Your phone is adjusting microphone levels automatically, which prevents accurate measurement. Try a different phone or a USB measurement microphone.";
    message.status = `Level check failed — ${message.note}`;
  } else if (state === "error" && terminalError === "agc_indeterminate") {
    // The Pi could not gather enough staircase evidence to render a verdict
    // either way (e.g. the level locked too close to the starting volume).
    // No AGC was observed — the copy must not claim it was.
    message.note =
      "JTS couldn't gather enough measurement evidence to verify this microphone's level accuracy. Try again, or use a different microphone or device.";
    message.status = `Level check failed — ${message.note}`;
  } else if (state === "error" && terminalError) {
    message.note = terminalError;
    message.status = `Level check failed — ${terminalError}`;
  }
  const children = [
    el("h1", { class: "cap-heading", text: message.heading }),
    el("p", { class: "cap-note", text: message.note }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus(message.status, message.kind);
}

async function enumerateAudioInputs() {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  if (!nav || !nav.mediaDevices || !nav.mediaDevices.enumerateDevices) return [];
  try {
    return (await nav.mediaDevices.enumerateDevices())
      .filter((d) => d.kind === "audioinput");
  } catch {
    return [];
  }
}

async function requestMicPermissionForSetup(spec) {
  const recorder = await createMonoRecorder({
    sampleRate: spec.sample_rate_hz || 48000,
    deviceId: selectedDeviceId,
  });
  await recorder.close();
  return enumerateAudioInputs();
}

function renderIntro(screenEl, ctx) {
  const levelRamp = ctx.spec.kind === "level_ramp";
  setScreen(screenEl, [
    el("h1", {
      class: "cap-heading",
      text: levelRamp ? "Set a safe measurement level" : "Room measurement",
    }),
    el("ol", { class: "cap-steps" }, levelRamp
      ? [
          el("li", { text: "Allow microphone access on this phone." }),
          el("li", { text: "Choose the microphone and calibration." }),
          ...(collectsRoomPositions(ctx.spec)
            ? [el("li", { text: "Choose how many listening positions to measure." })]
            : []),
          el("li", { text: "Place the microphone where the speaker shows you." }),
          el("li", { text: "JTS will rise slowly from quiet and lock the level." }),
        ]
      : [
          el("li", { text: "Allow microphone access on this phone." }),
          el("li", { text: "Choose the microphone and calibration." }),
          el("li", { text: "Pick how many listening positions to measure." }),
          el("li", { text: "Stay quiet while JTS records noise and plays each sweep." }),
        ]),
    button("Continue", () => renderPermission(screenEl, ctx)),
  ]);
  setStatus("Ready to set up the phone microphone.", "info");
}

function renderPermission(screenEl, ctx) {
  const levelRamp = ctx.spec.kind === "level_ramp";
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Microphone permission" }),
    el("p", {
      class: "cap-note",
      text: levelRamp
        ? "Your browser will ask to use the microphone. Tap Allow so JTS can list available inputs and check the level."
        : "Your browser will ask to use the microphone. Tap Allow so JTS can list available inputs and record the sweep.",
    }),
    button("Allow microphone", async () => {
      try {
        setStatus("Opening microphone…", "info");
        const inputs = await requestMicPermissionForSetup(ctx.spec);
        renderMicChoice(screenEl, ctx, inputs);
      } catch (err) {
        setStatus(captureFailureMessage(err), "error");
      }
    }),
  ]);
}

function renderMicChoice(screenEl, ctx, inputs) {
  setupInputs = Array.isArray(inputs) ? inputs : setupInputs;
  const select = el("select", { id: "phone-mic-select" });
  select.appendChild(el("option", { value: "", text: "Automatic / phone default" }));
  for (const input of setupInputs) {
    if (!input.label) continue;
    select.appendChild(el("option", {
      value: input.deviceId,
      text: input.label,
    }));
  }
  select.value = selectedDeviceId;
  if (select.value !== selectedDeviceId) {
    // The remembered device isn't in THIS render's enumerated list — either
    // it's unplugged right now, or the browser minted a new deviceId for
    // this session (some browsers rotate it per top-level navigation). Fall
    // back to Automatic for THIS render only; do NOT erase the stored
    // preference — run-19 telemetry showed 3 of 5 sessions silently losing a
    // good remembered choice to exactly this write, even though the same
    // physical mic would have matched again next time.
    selectedDeviceId = "";
  }
  select.addEventListener("change", () => {
    selectedDeviceId = select.value;
    rememberDeviceId(selectedDeviceId);
  });
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Choose microphone" }),
    el("p", {
      class: "cap-note",
      text: "If a USB-C measurement mic is plugged into the phone, choose it here. Otherwise leave Automatic.",
    }),
    el("label", { class: "cap-field" }, [
      el("span", { text: "Microphone" }),
      select,
    ]),
    el("div", { class: "cap-actions" }, [
      button("Continue", () => renderCalibration(screenEl, ctx)),
      button("Back", () => renderPermission(screenEl, ctx), true),
    ]),
  ]);
  setStatus("Microphone permission granted.", "done");
}

// Wave-2 household-mic prefill hint (jasper.correction.household_mic via
// CaptureSpec.default_setup_calibration, #1540). The one-tap Confirm SUBMITS
// {mode: "stored", calibration_id} for the Pi to resolve via the household-
// mic record, so the hint is only offered when the Pi marked it RESOLVABLE:
// `resolvable === true` is minted by the Pi's stored-mode build only when
// the calibration_id currently resolves on disk. A hint WITHOUT the marker
// (an older Pi build, or an ID the Pi could not resolve at spec-mint time)
// renders the plain full picker instead — the pre-Wave-2 behavior, pinned
// as the compat path.
function validDefaultSetupHint(spec) {
  const hint = spec && spec.default_setup && spec.default_setup.calibration;
  if (!hint || typeof hint !== "object") return null;
  if (hint.mode !== "serial" && hint.mode !== "upload") return null;
  if (hint.resolvable !== true) return null;
  if (!hint.calibration_id) return null;
  return hint;
}

// W6.12: a `crossover_sweep` capture (both the legacy per-driver flow and
// the v2 capture-plan session) has NO calibration-picker screen of its own
// — `boot()` renders straight to the fixed DATA screen for this kind. The
// legacy flow gets its calibration from the `level_ramp` level-match page
// visited FIRST in the same tab (this module's `setupState` survives that
// in-tab hash navigation); a v2 session has no preceding page, so it never
// had anywhere to apply the SAME household-mic hint `renderCalibrationConfirm`
// offers as a one-tap Confirm for level_ramp/room_sweep. Apply it SILENTLY
// here instead — a v2 session is designed around a minimal, fixed tap
// count, so there is no screen to hang a confirm button on, and the
// household already confirmed this calibration once when it was first set
// up. Never overrides a choice already present for this page load (a fresh
// `setupState.calibration.mode !== "none"` means something already claimed
// it — defensive, since nothing sets it before `boot()` calls this today).
function applyDefaultCalibrationHintSilently(spec) {
  if (String((setupState.calibration || {}).mode || "none") !== "none") return;
  const hint = validDefaultSetupHint(spec);
  if (!hint) return;
  setupState.calibration = {
    mode: "stored",
    calibration_id: String(hint.calibration_id),
    model: String(hint.model || ""),
  };
}

function calibrationModelLabel(spec, modelKey) {
  const models = Array.isArray(spec.calibration_models) ? spec.calibration_models : [];
  const found = models.find((model) => model && model.key === modelKey);
  return (found && found.label) || String(modelKey || "").trim() || "microphone";
}

// The post-calibration advance shared by the picker's Continue tail and the
// one-tap stored Confirm: move to the next screen for this spec kind,
// running whichever setup validation the flow validates eagerly. Throws on
// a validation failure — each caller owns its failure UX (the picker shows
// captureFailureMessage; the stored confirm falls back to the picker).
async function continueFromCalibration(screenEl, ctx) {
  if (ctx.spec.kind === "level_ramp") {
    if (collectsRoomPositions(ctx.spec)) {
      renderPositionCount(screenEl, ctx);
      return;
    }
    await bindSetupBeforeLevel(ctx);
    renderLevelReady(screenEl, ctx);
    return;
  }
  await validateSetupBeforeContinue(ctx);
  renderPositionCount(screenEl, ctx);
}

// Once a stored one-tap submit has failed, never re-offer the confirm
// screen in this page session — a Back-navigation replay of the same
// guaranteed-to-fail tap would loop the household.
let storedHintFailed = false;

function usedStoredCalibration() {
  return String((setupState.calibration || {}).mode || "") === "stored";
}

// The Pi could not use the stored household calibration — the record went
// stale between spec mint and submit, or the resolution failed. Never a
// dead end (adjudicated rejection contract): drop back to the full picker
// with a plain sentence so the household can set the microphone up
// manually. setStatus runs AFTER renderCalibration so the sentence wins
// over the picker's own default status line.
function fallBackFromStoredCalibration(screenEl, ctx) {
  storedHintFailed = true;
  setupState.calibration = { mode: "none" };
  renderCalibration(screenEl, ctx, { skipHint: true });
  setStatus(
    "The speaker couldn't use the saved microphone calibration. Set up the microphone manually instead.",
    "error",
  );
}

// One-tap confirm screen for a remembered household mic. Confirm SUBMITS
// the stored setup — setup.calibration = {mode: "stored", calibration_id}
// (plus model, display-only) — which the Pi's stored-mode branch resolves
// via the household-mic record (resolve_household_mic_calibration). The
// spec only offers this screen with `resolvable: true`, minted when that
// resolution succeeded at spec-mint time (see validDefaultSetupHint), so a
// rejection here means the record went stale in between — handled by
// falling back to the full picker, never a dead end.
function renderCalibrationConfirm(screenEl, ctx, hint) {
  const label = calibrationModelLabel(ctx.spec, hint.model);
  const serialDisplay = String(hint.serial_display || "").trim();
  const heading = serialDisplay ? `Using ${label} · ${serialDisplay}` : `Using ${label}`;
  const confirm = button("One tap to confirm", async () => {
    confirm.disabled = true;
    try {
      setupState.calibration = {
        mode: "stored",
        calibration_id: String(hint.calibration_id),
        model: String(hint.model || ""),
      };
      try {
        await continueFromCalibration(screenEl, ctx);
      } catch {
        fallBackFromStoredCalibration(screenEl, ctx);
      }
    } finally {
      confirm.disabled = false;
    }
  });
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: heading }),
    el("p", {
      class: "cap-note",
      text: "This is the microphone JTS remembers from last time.",
    }),
    el("div", { class: "cap-actions" }, [
      confirm,
      button("Use a different microphone", () => {
        setupState.calibration = { mode: "none" };
        renderCalibration(screenEl, ctx, { skipHint: true });
      }, true),
    ]),
  ]);
  setStatus(
    `Using ${label}${serialDisplay ? " · " + serialDisplay : ""} — one tap to confirm, or choose a different microphone.`,
    "info",
  );
}

function renderCalibration(screenEl, ctx, { skipHint = false } = {}) {
  const hint = !skipHint && !storedHintFailed ? validDefaultSetupHint(ctx.spec) : null;
  if (hint && String((setupState.calibration || {}).mode || "none") === "none") {
    renderCalibrationConfirm(screenEl, ctx, hint);
    return;
  }
  const calibrationModels = Array.isArray(ctx.spec.calibration_models)
    ? ctx.spec.calibration_models.filter((model) => (
        model &&
        typeof model.key === "string" &&
        model.key &&
        typeof model.label === "string" &&
        model.label
      ))
    : [];
  if (String((setupState.calibration || {}).mode || "none") === "none") {
    const selected = setupInputs.find((input) => input.deviceId === selectedDeviceId);
    const inferred = inferCalibrationModel(
      calibrationModels,
      String((selected && selected.label) || ""),
    );
    if (inferred) {
      setupState.calibration = {
        mode: "serial",
        model: inferred.key,
        serial: "",
      };
    }
  }
  const mode = el("select", { id: "calibration-mode" }, [
    el("option", { value: "none", text: "No calibration / phone built-in mic" }),
    el("option", { value: "serial", text: "Known measurement mic serial" }),
    el("option", { value: "upload", text: "Upload calibration file" }),
  ]);
  mode.value = String((setupState.calibration || {}).mode || "none");
  const details = el("div");
  const renderDetails = () => {
    details.replaceChildren();
    if (mode.value === "serial") {
      const serial = el("input", {
        id: "calibration-serial",
        type: "text",
        autocomplete: "off",
        placeholder: "Serial number",
      });
      const model = el("select", { id: "calibration-model" });
      for (const option of calibrationModels) {
        model.appendChild(el("option", { value: option.key, text: option.label }));
      }
      model.value = String((setupState.calibration || {}).model || "");
      serial.value = String((setupState.calibration || {}).serial || "");
      details.append(
        el("label", { class: "cap-field" }, [el("span", { text: "Mic model" }), model]),
        el("label", { class: "cap-field" }, [el("span", { text: "Serial number" }), serial]),
      );
    } else if (mode.value === "upload") {
      const file = el("input", {
        id: "calibration-file",
        type: "file",
        accept: ".txt,.cal,.frd,.csv,.omm,text/plain",
      });
      details.append(el("label", { class: "cap-field" }, [
        el("span", { text: "Calibration file" }),
        file,
      ]));
      // Only true when a file was ACTUALLY loaded this session (saveAndContinue
      // keeps .content only after a real upload). The mode:"upload"-without-
      // content state comes from boot's bound-setup restore (loadBoundSetup
      // rehydrates only the compact {mode, model} summary — file text is
      // deliberately never persisted); without the .content check this note
      // would wrongly imply a file is already selected there. (The one-tap
      // confirm is unrelated: renderCalibrationConfirm sets mode:"stored".)
      if ((setupState.calibration || {}).mode === "upload" && (setupState.calibration || {}).content) {
        details.append(el("p", {
          class: "cap-note",
          text: "Choose the file again only if you want to replace the current selection.",
        }));
      }
    }
  };
  mode.addEventListener("change", renderDetails);
  renderDetails();

  const saveAndContinue = async () => {
    if (mode.value === "serial") {
      const modelEl = document.getElementById("calibration-model");
      if (!modelEl || !modelEl.value) {
        setStatus("No supported measurement-mic models were provided by the speaker.", "error");
        return;
      }
      setupState.calibration = {
        mode: "serial",
        model: modelEl.value,
        serial: document.getElementById("calibration-serial").value.trim(),
      };
      if (!setupState.calibration.serial) {
        setStatus("Enter the microphone serial number.", "error");
        return;
      }
    } else if (mode.value === "upload") {
      const file = document.getElementById("calibration-file").files[0];
      const priorUpload = (setupState.calibration || {}).mode === "upload"
        ? setupState.calibration
        : null;
      if (!file && priorUpload && priorUpload.content) {
        setupState.calibration = priorUpload;
      } else if (!file) {
        setStatus("Choose a calibration file.", "error");
        return;
      } else {
        if (Number(file.size || 0) > MAX_CALIBRATION_TEXT_BYTES) {
          setStatus(
            "That calibration file is too large. Choose a text calibration file smaller than 256 KiB.",
            "error",
          );
          return;
        }
        const content = await file.text();
        if (utf8Size(content) > MAX_CALIBRATION_TEXT_BYTES) {
          setStatus(
            "That calibration file is too large. Choose a text calibration file smaller than 256 KiB.",
            "error",
          );
          return;
        }
        setupState.calibration = {
          mode: "upload",
          filename: file.name,
          content,
        };
      }
    } else {
      setupState.calibration = { mode: "none" };
    }
    try {
      await continueFromCalibration(screenEl, ctx);
    } catch (err) {
      setStatus(captureFailureMessage(err), "error");
    }
  };

  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Calibration" }),
    el("p", {
      class: "cap-note",
      text: "Calibration is applied after recording on the speaker. It is fine to continue without it when using the phone mic.",
    }),
    el("label", { class: "cap-field" }, [
      el("span", { text: "Calibration source" }),
      mode,
    ]),
    details,
    el("div", { class: "cap-actions" }, [
      button("Continue", saveAndContinue),
      button("Back", () => renderMicChoice(screenEl, ctx, setupInputs), true),
    ]),
  ]);
  setStatus("Choose the calibration that matches the microphone.", "info");
}

function renderLevelReady(screenEl, ctx) {
  const back = button("Back", () => renderCalibration(screenEl, ctx), true);
  // The Pi-owned spec is the single source of truth for measurement geometry.
  // Reusing the fixed DATA renderer here prevents the guided setup shell from
  // replacing an exact near-field/listening-position instruction with generic
  // copy after microphone selection.
  ctx.captureRefs = renderScreen(screenEl, ctx.spec, {
    handlers: { begin_capture: () => onLevelRampStart(ctx), stop: stopCapture },
  });
  screenEl.appendChild(el("div", { class: "cap-actions" }, [back]));
  ctx.captureRefs.buttons.push({ action: null, el: back });
  setStatus("Ready. Tap Start level check.", "info");
}

function renderBoundRoomReady(screenEl, ctx) {
  const position = Number(ctx.spec.position) || 0;
  const total = Number(
    (ctx.boundSetup && ctx.boundSetup.summary && ctx.boundSetup.summary.total_positions) ||
      ctx.spec.total_positions,
  ) || 0;
  const positionLabel = position > 0 && total >= position
    ? `position ${position} of ${total}`
    : "this room position";
  const trustRepeat = ctx.spec.presentation_variant === "trust_repeat";
  const heading = trustRepeat
    ? "Ready to repeat the main seat"
    : `Ready for ${positionLabel}`;
  const instruction = trustRepeat
    ? "Keep the same microphone selected and return it to the main listening position. This extra capture checks that the result is trustworthy."
    : "The speaker has set this position. Keep the same microphone selected and place it where the speaker shows you.";
  const start = button("Start measurement", async () => {
    start.disabled = true;
    try {
      await onStart(ctx);
    } finally {
      start.disabled = false;
    }
  });
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: heading }),
    el("p", {
      class: "cap-note",
      text: instruction,
    }),
    el("div", { class: "cap-actions" }, [start]),
  ]);
  ctx.captureRefs = {
    buttons: [{ action: "begin_capture", el: start }],
    levelMeters: [],
  };
  setStatus(
    trustRepeat ? "Ready for the main-seat trust check." : `Ready to measure ${positionLabel}.`,
    "info",
  );
}

function renderPositionCount(screenEl, ctx) {
  const levelRamp = ctx.spec.kind === "level_ramp";
  const positions = el("select", { id: "position-count" }, [
    el("option", { value: "1", text: "1 position / quick check" }),
    el("option", { value: "3", text: "3 positions" }),
    el("option", { value: "5", text: "5 positions / recommended" }),
    el("option", { value: "7", text: "7 positions / large couch" }),
  ]);
  positions.value = String(setupState.total_positions || 5);
  positions.addEventListener("change", () => {
    setupState.total_positions = Number(positions.value) || 5;
  });
  const continueFromPositions = async () => {
    setupState.total_positions = Number(positions.value) || 5;
    if (!levelRamp) {
      await onStart(ctx);
      return;
    }
    try {
      await bindSetupBeforeLevel(ctx);
    } catch (err) {
      // The position-collecting level_ramp flow is the one path where the
      // one-tap stored Confirm's validation is DEFERRED to this bind (the
      // confirm advances straight to the position count). Keep the stored
      // rejection contract here too: fall back to the full picker with the
      // plain sentence, never a dead end.
      if (usedStoredCalibration()) {
        fallBackFromStoredCalibration(screenEl, ctx);
        return;
      }
      setStatus(captureFailureMessage(err), "error");
      return;
    }
    renderLevelReady(screenEl, ctx);
  };
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Listening positions" }),
    el("p", {
      class: "cap-note",
      text: "Five measurements across the listening area is the default. Move the phone roughly a head-width between positions.",
    }),
    el("label", { class: "cap-field" }, [
      el("span", { text: "Measurements" }),
      positions,
    ]),
    el("div", { class: "cap-actions" }, [
      button(levelRamp ? "Continue to level check" : "Start measurement", continueFromPositions),
      button("Back", () => renderCalibration(screenEl, ctx), true),
    ]),
  ]);
  setStatus(
    levelRamp ? "Choose the listening area before checking the level." : "Ready to measure position 1.",
    "info",
  );
}

function samplesRmsDbfs(samples) {
  if (!samples || !samples.length) return null;
  let sumSquares = 0;
  for (const sample of samples) sumSquares += sample * sample;
  return rmsToDbfs(Math.sqrt(sumSquares / samples.length));
}

async function captureAmbientNoise(recorder, spec) {
  const durationMs = Math.max(300, Math.min(2000, Number(spec.noise_floor_ms) || 800));
  setStatus("Measuring room noise — stay quiet.", "recording");
  recorder.start();
  await delayMs(durationMs);
  const samples = await recorder.stop({ timeoutMs: 5000 });
  return {
    duration_ms: durationMs,
    rms_dbfs: samplesRmsDbfs(samples),
    // Raw samples for ambientStatsFieldsFor() below. Callers building the
    // `noise_floor` wire payload must NOT spread this whole object — only
    // duration_ms/rms_dbfs are safe/meaningful on the wire (a Float32Array
    // does not survive JSON.stringify intact).
    samples,
  };
}

// Per-octave-band ambient-noise stats (Wave 2, W2.1/W2.4 closed-loop SNR
// level solve — jasper.audio_measurement.level_solver.parse_ambient_stats_event).
// Scoped to driver sweeps (crossover_sweep) since that solver only ever runs
// per-driver; room_sweep/balance_burst/sync_marker have no such consumer.
// Returns `{}` (spread-safe, no-op) for every other kind or an empty/failed
// capture, so this rides for free on every capture protocol version — v1,
// v2, and the v3 plan loop all call captureAmbientNoise() the same way.
function ambientStatsFieldsFor(spec, noise) {
  if (spec.kind !== "crossover_sweep" || !noise || !noise.samples || !noise.samples.length) {
    return {};
  }
  return buildAmbientStatsEvent(
    noise.samples,
    spec.sample_rate_hz || 48000,
    spec.run_token,
    noise.duration_ms / 1000,
  );
}

function inspectRecorder(recorder, spec) {
  const track = recorder.stream.getAudioTracks ? recorder.stream.getAudioTracks()[0] : null;
  const settings = track && track.getSettings ? track.getSettings() : {};
  const realized = verifyRealizedConstraints(
    settings,
    spec,
    recorder.capturedChannelCount,
  );
  return {
    track,
    settings,
    decision: constraintDecision(realized, spec),
    device: {
      label: (track && track.label) || "",
      device_id: settings.deviceId || "",
      source_channel_count: realized.sourceChannelCount,
      captured_channel_count: realized.capturedChannelCount,
    },
  };
}

function updateLevelMeters(ctx, level) {
  const meters = (ctx.captureRefs && ctx.captureRefs.levelMeters) || [];
  const rms = Number(level && level.rms_dbfs);
  const percent = Number.isFinite(rms)
    ? Math.max(0, Math.min(100, ((rms + 60) / 54) * 100))
    : 0;
  for (const meter of meters) meter.style.width = `${percent.toFixed(1)}%`;
}

function setCaptureButtonsDisabled(ctx, disabled) {
  const buttons = (ctx.captureRefs && ctx.captureRefs.buttons) || [];
  // Only the level-ramp leg (onLevelRampStart) calls this; it disables
  // Start/Back for the ramp's duration, and Stop is skipped so it stays
  // tappable through that window. The sweep leg (onStart) never calls it —
  // its Start stays enabled during capture (pre-existing behavior; the
  // sweep-leg interaction model is being redesigned in Wave 2's
  // session-spanning work, so no new disable logic here).
  for (const ref of buttons) {
    if (ref.action === "stop") continue;
    ref.el.disabled = Boolean(disabled);
  }
}

async function onLevelRampStart(ctx) {
  const { spec, client } = ctx;
  let recorder = null;
  let streamer = null;
  let wakeLock = null;
  let disposeWatch = () => {};
  let aborted = false;
  setCaptureButtonsDisabled(ctx, true);

  const abort = async (reason) => {
    if (aborted) return;
    aborted = true;
    if (reason === "stopped") {
      renderStoppedScreen(ctx);
    } else {
      setStatus(
        reason === "backgrounded"
          ? "Level check stopped — this phone's screen must stay on."
          : `Level check stopped — ${reason}.`,
        "error",
      );
    }
    if (streamer) await streamer.abort(reason);
    if (recorder) {
      try {
        await recorder.close();
      } catch {
        /* already closed */
      }
      recorder = null;
    }
  };
  activeAbort = abort;

  try {
    setStatus("Starting microphone…", "info");
    recorder = await createMonoRecorder({
      sampleRate: spec.sample_rate_hz || 48000,
      deviceId: selectedDeviceId,
    });
    const capture = inspectRecorder(recorder, spec);
    if (capture.decision.action === "refuse") {
      await recorder.close();
      recorder = null;
      setStatus(
        `This phone can't run a clean level check (${capture.decision.reason}). ` +
          "Try a different phone or measurement microphone.",
        "error",
      );
      return;
    }
    // The current relay protocol has no safe manual-lock acknowledgement, so a
    // browser that AFFIRMATIVELY reports AGC on (autoGainControl === true) is
    // refused outright — a rising tone through a browser-confirmed
    // time-varying gain cannot be interpreted as a stable acoustic gain map.
    // A browser that never reports the setting either way
    // (autoGainControl === undefined — every WebKit/iOS build; getSettings()
    // simply omits the key) is NOT the same thing as a proven AGC-on: it
    // proceeds as "unattested", and the Pi verifies chain linearity
    // empirically from the ramp's own staircase (ramp.py) instead of trusting
    // a browser flag that iOS never supplies.
    const realizedAgc = capture.settings.autoGainControl;
    if (realizedAgc === true) {
      await recorder.close();
      recorder = null;
      try {
        await client.postEvent({
          level_refused: {
            schema: 1,
            run_token: String(spec.run_token || ""),
            reason: "agc_not_proven_off",
          },
        });
      } catch {
        /* the visible refusal remains authoritative if the relay is unavailable */
      }
      setStatus(
        "This browser cannot prove automatic microphone gain is off, so JTS will not play the level tone. Use a browser or USB microphone that reports automatic gain control disabled.",
        "error",
      );
      return;
    }
    const agcAttested = realizedAgc === false;

    wakeLock = await acquireWakeLockWithHint();
    disposeWatch = watchVisibilityAbort(
      typeof document !== "undefined" ? document : null,
      (reason) => {
        void abort(reason);
      },
    );
    setStatus(
      capture.decision.degraded
        ? `Checking level at lower confidence — ${capture.decision.reason}.`
        : "Checking level — the speaker will rise slowly from quiet.",
      "recording",
    );

    const ramp = await runLevelRampProtocol({
      client,
      recorder,
      spec,
      // agcAttested (realized AGC=false) rides unchanged; an unattested
      // browser (AGC neither proven off nor reported on) posts agcFrozen:false
      // + agcUnattested:true so the Pi can empirically verify the chain from
      // the ramp's own staircase instead of trusting an absent browser flag.
      agcFrozen: agcAttested,
      agcUnattested: !agcAttested,
      context: {
        setup: setupWirePayload(),
        device: capture.device,
      },
      onStreamer: (value) => {
        streamer = value;
      },
      onLevel: (level) => updateLevelMeters(ctx, level),
      onProgress: (event) => {
        if (event.state === "settling") {
          setStatus("Holding the tone while the microphone settles…", "recording");
        } else if (event.state === "confirming") {
          setStatus("Level found — confirming it is stable…", "recording");
        }
      },
      isAborted: () => aborted,
    });
    if (!aborted) renderLevelRampComplete(ctx, ramp);
  } catch (err) {
    if (!aborted) {
      if (isDeadSessionError(err)) {
        renderSessionExpired(ctx);
      } else {
        setStatus(captureFailureMessage(err), "error");
      }
    }
  } finally {
    disposeWatch();
    if (recorder) {
      try {
        await recorder.close();
      } catch {
        /* already closed */
      }
    }
    if (wakeLock) await wakeLock.release();
    hideWakeLockHint();
    setCaptureButtonsDisabled(ctx, false);
    if (activeAbort === abort) activeAbort = null;
  }
}

async function waitForSweepComplete(client, spec, isAborted) {
  const timeoutMs = Math.max(5000, Number(spec.duration_ms) || 20000);
  const pollMs = Math.max(100, Math.min(1000, Number(spec.progress_poll_ms) || 250));
  const deadline = Date.now() + timeoutMs;
  let lastPhase = "";
  // The Pi's own quiet-reference pause is now right-sized per driver (a short
  // tweeter sweep no longer inherits the longest driver's ~14 s pause — see
  // jasper.active_speaker.test_signal_plan.driver_ambient_duration_s), which
  // made the prior fixed "Measuring room noise" status read as unexplained
  // silence of unknown length. The `ambient_started` host event already
  // carries `duration_s` (relayed verbatim — see RelayClient.post_host_event's
  // docstring); render a live countdown from it instead of adding a new spec
  // field. An older Pi build that omits `duration_s` falls back to the
  // original static copy — no countdown, same as before.
  let ambientDeadlineMs = null;
  let lastCountdownSeconds = null;
  while (Date.now() < deadline) {
    if (isAborted()) return false;
    const status = await client.fetchPhoneStatus();
    const event = status && status.host_event || {};
    const phase = String(event.phase || "");
    if (phase === "capture_incompatible") {
      throw new Error(event.error || "capture page is incompatible with this speaker");
    }
    if (phase && phase !== lastPhase) {
      lastPhase = phase;
      if (phase === "ambient_started") {
        const durationS = Number(event.duration_s);
        ambientDeadlineMs =
          Number.isFinite(durationS) && durationS > 0
            ? Date.now() + durationS * 1000
            : null;
        lastCountdownSeconds = null;
        if (!ambientDeadlineMs) {
          setStatus(
            "Measuring room noise — stay quiet and keep the phone still.",
            "recording",
          );
        }
      } else if (phase === "sweep_started") {
        ambientDeadlineMs = null;
        setStatus("Playing the measurement tone…", "recording");
      } else if (phase === "sweep_complete") {
        setStatus("Tone finished — capturing the room tail.", "recording");
      }
    }
    if (phase === "ambient_started" && ambientDeadlineMs) {
      const remaining = Math.max(
        0,
        Math.ceil((ambientDeadlineMs - Date.now()) / 1000),
      );
      if (remaining !== lastCountdownSeconds) {
        lastCountdownSeconds = remaining;
        setStatus(
          `Listening to the room… the tone starts in about ${remaining} seconds`,
          "recording",
        );
      }
    }
    if (phase === "sweep_complete") return true;
    if (phase === "sweep_cancelled") {
      setStatus("Measurement stopped safely. The speaker page shows what happens next.", "info");
      return false;
    }
    if (phase === "sweep_failed") {
      // Marked so onStart's catch renders a terminal failure screen instead
      // of leaving the dead Start button visible (XOVER-6 interim — see
      // renderSweepFailed).
      const failure = new Error(event.error || "speaker sweep failed");
      failure.sweepFailed = true;
      throw failure;
    }
    await delayMs(pollMs);
  }
  throw new Error("speaker did not finish the sweep before the recording timeout");
}

// ============================================================================
// Session-spanning capture plans (capture protocol v3, SPEC W2.3) — the
// ping-pong killer. One relay session now covers a driver's whole repeat
// SET: after each accepted capture the phone shows "Measurement N of target
// ✓" with a single "Next measurement" tap, instead of the household
// returning to the wizard between every repeat. v2 specs (no capture_plan)
// keep today's flow through onStart() above, which stays WIRE-COMPATIBLE
// with every deployed Pi: its armed event changed inert-additively (the
// noise_floor object is now built explicitly with the same two fields, and
// crossover_sweep captures gained the ambient_stats key, which older Pis
// simply ignore) — no field changed meaning or shape. The loop below is a
// fully separate code path, dormant until a Pi build starts emitting
// capture_protocol_version=3 + capture_plan (see
// jasper/capture_relay/spec.py's CapturePlan docstring).
// ============================================================================

function planTargetAndAttempts(spec) {
  const plan = (spec && spec.capture_plan) || {};
  return {
    target: Math.max(1, Number(plan.capture_target) || 1),
    maxAttempts: Math.max(1, Number(plan.max_attempts) || 1),
  };
}

// Per-capture heterogeneity (§5.7): `spec.capture_plan.entries` is 0-based
// (index 0..capture_target-1); the wire protocol's `begin_capture.index` is
// 1-based (SPEC W2.3) — mirrors jasper/capture_relay/spec.py's
// `CapturePlan.entry_for_index`. Returns `null` for a v1/v2 spec (no
// entries) or an index with no matching entry. An entry's `duration_ms` is
// the capture's DECLARED acoustic length — presentation data (progress/
// countdown copy, wired in the crossover-v2 flow), never the recording
// deadline: waitForSweepComplete's timeout stays the spec-level
// `duration_ms` backstop for every plan.
function entryForIndex(spec, index) {
  const entries = spec && spec.capture_plan && spec.capture_plan.entries;
  if (!Array.isArray(entries)) return null;
  return entries.find((e) => e && Number(e.index) === index - 1) || null;
}

// How long the phone should wait between a `capture_deferred` host event and
// re-posting the SAME `begin_capture`. A deferral means the Pi is between
// phases (e.g. parked awaiting the household's Apply tap), not a fast ledger
// check, so this is slower than the general status-poll cadence.
const CAPTURE_DEFERRED_RETRY_POLL_MS = 1500;

function stopButtonEl() {
  return el("button", {
    type: "button",
    class: "cap-button cap-button--danger",
    text: "Stop",
    onclick: () => {
      void stopCapture();
    },
  });
}

// Session-wide resources a v3 capture plan holds across EVERY round (#1658):
// the mic stream/graph a capture session reuses instead of reopening per
// attempt (Fix 2 — avoids the iOS getUserMedia-renegotiation level step
// between captures), and the screen wake lock held for the whole session
// rather than re-acquired per round (Fix 1). Idempotent — safe to call more
// than once — and called from every terminal path: the success/refusal/
// exhaustion/failure terminals via endPlanSession below, Stop/backgrounded
// via makePlanController's abort.
async function releasePlanSessionResources(ctx) {
  ctx.sessionEnded = true;
  // N-a: neither flag means anything once the session is over — clear both
  // so a later, unrelated re-entry can never resurface a stale pre-arm-retry
  // state or a stale reacquire-failure from this session.
  ctx.parkedAtRetriableFailure = false;
  ctx.recorderFailure = null;
  if (typeof ctx.disposeSessionVisibilityWatch === "function") {
    ctx.disposeSessionVisibilityWatch();
  }
  ctx.disposeSessionVisibilityWatch = null;
  hideWakeLockHint();
  const recorder = ctx.recorder;
  ctx.recorder = null;
  if (recorder) {
    try {
      await recorder.close();
    } catch {
      /* already closed */
    }
  }
  const wakeLock = ctx.wakeLock;
  ctx.wakeLock = null;
  if (wakeLock) {
    try {
      await wakeLock.release();
    } catch {
      /* already released */
    }
  }
}

// The Wake Lock API auto-releases the moment the document goes hidden — a
// Control Center swipe or a notification banner, not a genuine background —
// so re-request it once the phone returns to the foreground, as long as this
// plan session (ctx.sessionEnded) hasn't already ended.
async function reacquireSessionWakeLock(ctx) {
  // N-b: a rapid visibility flicker (hide/show/hide/show within one brief
  // gesture) can fire this twice with overlapping in-flight requests; an
  // in-flight latch keeps the second call a no-op rather than letting
  // whichever resolves last silently orphan the other's sentinel (that
  // orphan would self-heal on the NEXT hide either way, but the latch is
  // simple enough to just make it airtight).
  if (ctx.sessionEnded || ctx.reacquiringWakeLock) return;
  ctx.reacquiringWakeLock = true;
  try {
    const lock = await acquireWakeLockWithHint();
    if (ctx.sessionEnded) {
      try {
        await lock.release();
      } catch {
        /* already released */
      }
      return;
    }
    // N2: the browser already dropped ctx.wakeLock's OWN sentinel when the
    // page hid (that is why we are here re-acquiring) — release it anyway
    // before overwriting the reference so our wrapper's idempotent
    // release() flag is flipped for the stale sentinel too, best-effort.
    try {
      await ctx.wakeLock?.release();
    } catch {
      /* already released */
    }
    ctx.wakeLock = lock;
  } finally {
    ctx.reacquiringWakeLock = false;
  }
}

// Reused across every capture in a v3 session, the mic stream can still die
// mid-session (a USB mic unplugged, the OS revoking the track) — a dying
// MediaStreamTrack does not error the audio graph, it just goes silent, so
// without this a later capture would silently upload dead air. One reacquire
// attempt; if that also fails, the failure rides the EXISTING capture-failure
// surface — it is thrown from the point of use (runPlanCapture) and caught by
// its own existing catch, the same as any other capture error.
function wireTrackEndedRecovery(ctx, recorder, spec) {
  const track = recorder.stream && recorder.stream.getAudioTracks
    ? recorder.stream.getAudioTracks()[0]
    : null;
  if (!track) return;
  track.onended = async () => {
    if (ctx.recorder !== recorder) return; // already superseded — nothing to do
    recorder.__trackEnded = true;
    ctx.recorder = null;
    try {
      await recorder.close();
    } catch {
      /* already closed, or the graph never fully opened */
    }
    try {
      const replacement = await createMonoRecorder({
        sampleRate: spec.sample_rate_hz || 48000,
        deviceId: selectedDeviceId,
      });
      if (ctx.sessionEnded) {
        // B1: Stop/backgrounded fired while THIS reacquire was in flight —
        // the session already tore down. Close it rather than assigning it
        // to ctx.recorder, which would orphan a live mic stream with nothing
        // left to close it (mirrors runPlanCapture's identical guard below).
        try {
          await replacement.close();
        } catch {
          /* already closed */
        }
        return;
      }
      wireTrackEndedRecovery(ctx, replacement, spec);
      ctx.recorder = replacement;
    } catch (err) {
      ctx.recorderFailure = err instanceof Error ? err : new Error(String(err));
    }
  };
}

// One persistent abort controller spanning every round of the set — unlike
// onStart's `abort` closure (scoped to one invocation), this must stay live
// across the async gaps between "Next measurement" taps so Stop remains
// wired even while the phone is just idling on a between-captures screen.
function makePlanController(ctx) {
  const state = {
    aborted: false,
  };
  state.abort = async (reason) => {
    if (state.aborted) return;
    state.aborted = true;
    // A pending auto-advance countdown / scheduled begin must never fire a
    // begin against a session the household just stopped.
    clearAutoAdvance(ctx);
    if (reason === "stopped") {
      renderStoppedScreen(ctx);
    } else {
      setStatus(
        reason === "backgrounded"
          ? "Measurement stopped — this phone's screen must stay on."
          : `Measurement stopped — ${reason}.`,
        "error",
      );
    }
    try {
      await ctx.client.postEvent({ aborted: true, abort_reason: reason });
    } catch {
      /* the Pi also times out if it never hears the abort */
    }
    await releasePlanSessionResources(ctx);
    if (activeAbort === state.abort) activeAbort = null;
  };
  return state;
}

// The whole plan concluded (accepted set, exhausted budget, refusal, or an
// unrecoverable error) — stop offering Stop against a session nothing is
// polling anymore, and release the session-wide mic stream + wake lock.
async function endPlanSession(ctx) {
  if (ctx.planController && activeAbort === ctx.planController.abort) {
    activeAbort = null;
  }
  await releasePlanSessionResources(ctx);
}

function renderPlanNext(ctx, { index, attempt, target }) {
  // The UPCOMING capture's own entry (§5.7), when the plan carries one —
  // title/body copy only; a v1/v2 plan (or an entry with no `screen`) falls
  // back to the generic "Measurement N of target" copy, unchanged.
  const upcoming = entryForIndex(ctx.spec, index + 1);
  const screenCopy = (upcoming && upcoming.screen) || {};
  const heading = String(screenCopy.title || `Measurement ${index} of ${target} ✓`);
  const body = String(screenCopy.body || "Ready for the next measurement.");
  const next = button("Next measurement", async () => {
    next.disabled = true;
    try {
      await runPlanCapture(ctx, { index: index + 1, attempt: attempt + 1 });
    } finally {
      next.disabled = false;
    }
  });
  setScreen(ctx.screenEl, [
    el("h1", { class: "cap-heading", text: heading }),
    el("p", { class: "cap-note", text: body }),
    el("div", { class: "cap-actions" }, [next, stopButtonEl()]),
  ]);
  ctx.captureRefs = { buttons: [{ action: "begin_capture", el: next }], levelMeters: [] };
  setStatus(`Measurement ${index} of ${target} done. Tap Next measurement when ready.`, "done");
}

function renderPlanRetry(ctx, { index, attempt, target, reason }) {
  // The CURRENT capture's own entry, if any — only its title informs the
  // heading; the rejection `reason` always wins the body (more important
  // than generic per-entry copy).
  const current = entryForIndex(ctx.spec, index);
  const screenCopy = (current && current.screen) || {};
  const heading = String(
    screenCopy.title || `Measurement ${index} of ${target} needs another try`,
  );
  const message = reason || "That measurement didn't pass the speaker's quality check.";
  const retry = button("Try again", async () => {
    retry.disabled = true;
    try {
      await runPlanCapture(ctx, { index, attempt: attempt + 1 });
    } finally {
      retry.disabled = false;
    }
  });
  setScreen(ctx.screenEl, [
    el("h1", { class: "cap-heading", text: heading }),
    el("p", { class: "cap-note", text: message }),
    el("div", { class: "cap-actions" }, [retry, stopButtonEl()]),
  ]);
  ctx.captureRefs = { buttons: [{ action: "begin_capture", el: retry }], levelMeters: [] };
  setStatus(`Measurement ${index} needs another try — ${message}`, "error");
}

// A distinct soft-hold screen (§5.7): the Pi is between phases (e.g. parked
// awaiting the household's Apply tap before VERIFY) and asked the phone to
// wait, not to act. No begin affordance here — `runPlanCapture`'s deferred
// handling re-posts the SAME begin_capture automatically after a short poll,
// so this is purely a waiting state with Stop still wired. Prefers the waiting
// entry's own title ("Waiting for apply") so the hold state reads coherently.
// The heading node is captured on `ctx.captureRefs.heading` — W6.12:
// `runPlanCapture` advances it to "Verifying…" once the hold actually
// resolves and recording starts, so the household is not staring at
// "Waiting for apply" through the whole verify capture.
function renderPlanDeferred(ctx, { index, target, reason }) {
  const entry = entryForIndex(ctx.spec, index);
  const screenCopy = (entry && entry.screen) || {};
  const heading = String(screenCopy.title || `Measurement ${index} of ${target}`);
  const message = reason || String(screenCopy.body ||
    "Waiting for the speaker to be ready for the next measurement.");
  const headingEl = el("h1", { class: "cap-heading", text: heading });
  setScreen(ctx.screenEl, [
    headingEl,
    el("p", { class: "cap-note", text: message }),
    el("div", { class: "cap-actions" }, [stopButtonEl()]),
  ]);
  ctx.captureRefs = { buttons: [], levelMeters: [], heading: headingEl };
  setStatus(`Waiting — ${message}`, "info");
}

// W6.12: once the on_apply hold's deferral actually resolves and recording
// starts, advance the still-visible "Waiting for apply" heading — otherwise
// it covers the WHOLE verify capture (the sweep runs for several seconds),
// so a household glancing at the phone mid-recording still read a heading
// describing a wait that already ended. `heading` is only ever set by
// renderPlanDeferred, so this is a safe no-op for every entry that never
// held (check/measure) or whose hold already moved on to a later screen.
function advanceDeferredHoldHeading(ctx) {
  if (ctx.captureRefs && ctx.captureRefs.heading) {
    ctx.captureRefs.heading.textContent = "Verifying…";
  }
}

// §5.2 auto-advance policy carried per-entry in `screen.auto_advance` (page
// policy, opaque to the wire schema): the FIRST capture requires a tap; MEASURE
// auto-advances behind a visible cancelable countdown; VERIFY arms on the
// apply-complete host event. Mirrors jasper/active_speaker/crossover_v2_flow.py.
const AUTO_ADVANCE_TAP = "tap";
const AUTO_ADVANCE_COUNTDOWN = "countdown";
const AUTO_ADVANCE_ON_APPLY = "on_apply";

function nextEntryAutoAdvance(ctx, nextIndex) {
  const entry = entryForIndex(ctx.spec, nextIndex);
  const screenCopy = (entry && entry.screen) || {};
  return String(screenCopy.auto_advance || AUTO_ADVANCE_TAP);
}

// Cancel any pending auto-advance countdown / scheduled begin. Called before a
// fresh round, on Stop, and on a re-boot (a stale timer must never fire a begin
// against a new or dead session).
function clearAutoAdvance(ctx) {
  if (ctx.autoAdvanceTimer != null) {
    clearTimeout(ctx.autoAdvanceTimer);
    ctx.autoAdvanceTimer = null;
  }
  if (ctx.autoAdvanceInterval != null) {
    clearInterval(ctx.autoAdvanceInterval);
    ctx.autoAdvanceInterval = null;
  }
}

// Start the next capture as a FRESH runPlanCapture (a macrotask, so the current
// round's finally cleanup has already run) — mirrors a "Next measurement" tap
// without the tap. Used for the on_apply hold (post the begin immediately as
// liveness) and after a countdown elapses.
function scheduleAutoBegin(ctx, { index, attempt }) {
  clearAutoAdvance(ctx);
  const controller = ctx.planController;
  ctx.autoAdvanceTimer = setTimeout(() => {
    ctx.autoAdvanceTimer = null;
    if (controller && controller.aborted) return;
    void runPlanCapture(ctx, { index, attempt });
  }, 0);
}

// §5.2 visible, cancelable countdown between an accepted capture and the next
// (MEASURE). Renders the upcoming entry's copy plus a live "Starting in N…"
// counter with a Cancel that drops back to a manual begin affordance; on
// elapse it auto-begins. Blocker #4b — the policy was carried but never shown.
function renderPlanCountdown(ctx, { index, attempt, target, nextIndex, nextAttempt }) {
  clearAutoAdvance(ctx);
  const entry = entryForIndex(ctx.spec, nextIndex);
  const screenCopy = (entry && entry.screen) || {};
  const heading = String(screenCopy.title || `Measurement ${nextIndex} of ${target}`);
  const body = String(screenCopy.body || "Starting the next measurement.");
  let seconds = Math.max(1, Number(screenCopy.countdown_s) || 5);
  const counter = el("p", { class: "cap-note", text: `Starting in ${seconds}…` });
  const begin = () => {
    clearAutoAdvance(ctx);
    void runPlanCapture(ctx, { index: nextIndex, attempt: nextAttempt });
  };
  const cancel = button("Cancel", () => {
    // Cancel drops to a manual begin affordance (the countdown is cancelable
    // per §5.2) — the household starts when ready.
    clearAutoAdvance(ctx);
    renderPlanNext(ctx, { index, attempt, target });
  }, true);
  setScreen(ctx.screenEl, [
    el("h1", { class: "cap-heading", text: heading }),
    el("p", { class: "cap-note", text: body }),
    counter,
    el("div", { class: "cap-actions" }, [cancel, stopButtonEl()]),
  ]);
  ctx.captureRefs = { buttons: [], levelMeters: [] };
  setStatus(`Next measurement starts in ${seconds}s — tap Cancel to hold.`, "info");
  ctx.autoAdvanceInterval = setInterval(() => {
    if (ctx.planController && ctx.planController.aborted) {
      clearAutoAdvance(ctx);
      return;
    }
    seconds -= 1;
    if (seconds <= 0) {
      begin();
      return;
    }
    counter.textContent = `Starting in ${seconds}…`;
  }, 1000);
}

// After an accepted capture, route to the next screen by the UPCOMING entry's
// auto-advance policy (§5.2). on_apply → the hold owns the screen and the
// deferred loop auto-posts the begin (no tap); countdown → a cancelable
// countdown; tap (or a plan with no policy) → the manual "Next measurement".
function advanceAfterAccepted(ctx, { index, attempt, target }) {
  const nextIndex = index + 1;
  const nextAttempt = attempt + 1;
  const policy = nextEntryAutoAdvance(ctx, nextIndex);
  if (policy === AUTO_ADVANCE_ON_APPLY) {
    renderPlanDeferred(ctx, { index: nextIndex, target });
    scheduleAutoBegin(ctx, { index: nextIndex, attempt: nextAttempt });
    return;
  }
  if (policy === AUTO_ADVANCE_COUNTDOWN) {
    renderPlanCountdown(ctx, { index, attempt, target, nextIndex, nextAttempt });
    return;
  }
  renderPlanNext(ctx, { index, attempt, target });
}

// `index` (the just-completed FINAL wire index, 1-based) is optional — most
// callers of this shared plan-completion screen (room sweep, sync, balance)
// have no per-flow completion copy and get the generic text below. Owner
// ruling (2026-07-20): the crossover-v2 flow's own auto-apply means the
// household never sees a browser-tab Apply step, so its own end screen must
// say the outcome plainly and point at the speaker page for undo/compare —
// carried as `done_title`/`done_body` on the LAST plan entry (the VERIFY
// entry in jasper.active_speaker.crossover_v2_flow.build_v2_capture_plan) so
// this shared screen needs no flow-specific branch.
function renderPlanAllDone(ctx, { index } = {}) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const entry = typeof index === "number" ? entryForIndex(ctx.spec, index) : null;
  const screenCopy = (entry && entry.screen) || {};
  const heading = String(screenCopy.done_title || "All measurements done");
  const body = String(
    screenCopy.done_body ||
      "All measurements done — the speaker continues automatically.",
  );
  const children = [
    el("h1", { class: "cap-heading", text: heading }),
    el("p", { class: "cap-note", text: body }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus(body, "done");
}

function renderPlanRefused(ctx, admission) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const message = admission.error || "The speaker refused this measurement.";
  const children = [
    el("h1", { class: "cap-heading", text: "Measurement refused" }),
    el("p", {
      class: "cap-note",
      text: `${message} The speaker page shows what happens next.`,
    }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus(`Measurement refused — ${message}`, "error");
}

function renderPlanExhausted(ctx, verdict) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const children = [
    el("h1", { class: "cap-heading", text: "Reached the attempt limit" }),
    el("p", {
      class: "cap-note",
      text:
        `The speaker reached its measurement attempt limit (${verdict.accepted} of ` +
        `${verdict.target} accepted). The speaker page shows what happens next.`,
    }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus(
    "Reached the measurement attempt limit. The speaker page shows what happens next.",
    "error",
  );
}

// Poll for the Pi's admission verdict on a just-posted `begin_capture`.
// `capture_refused` is ALWAYS terminal for the whole session (run_capture_plan
// on the Pi exits with an exception the instant it refuses a begin — see
// jasper/capture_relay/session.py's `_poll_capture_plan`), so this never
// offers/expects a same-attempt retry; it is the phone's ONE chance to see
// why nothing will start.
async function waitForCaptureAuthorized(client, spec, index, attempt, isAborted) {
  const pollMs = Math.max(100, Math.min(1000, Number(spec.progress_poll_ms) || 250));
  // Admission is a quick ledger read (jasper.active_speaker.repeat_admission),
  // never acoustic work, so 20s is already generous — kept flat rather than
  // scaled off spec.duration_ms (the RECORDING window's own budget, a
  // different concern from admission latency).
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (isAborted()) return { aborted: true };
    let status;
    try {
      status = await client.fetchPhoneStatus();
    } catch (err) {
      if (isDeadSessionError(err)) return { deadSession: true };
      throw err;
    }
    const event = (status && status.host_event) || {};
    const phase = String(event.phase || "");
    if (phase === "capture_incompatible") {
      throw new Error(event.error || "capture page is incompatible with this speaker");
    }
    if (
      phase === "capture_authorized" &&
      Number(event.index) === index &&
      Number(event.attempt) === attempt
    ) {
      return { authorized: true };
    }
    if (phase === "capture_refused") {
      return {
        refused: true,
        code: String(event.code || ""),
        error: String(event.error || "The speaker refused this measurement."),
      };
    }
    if (phase === "capture_set_exhausted") {
      // The whole SESSION ended while we were waiting to begin — a watchdog
      // collapse during the "waiting for apply" hold posts capture_set_exhausted
      // (W6.10 blocker #3). Treat it as terminal so the deferred-retry loop
      // stops instead of polling a dead session forever, rather than waiting
      // out the 20s admission timeout. Deliberately NOT extended to a rejected
      // `capture_result`: the host-event slot is last-write-wins and nothing
      // clears it when the phone consumes a verdict, so a retry begin's FIRST
      // poll reads the PREVIOUS attempt's stale rejected verdict (the real Pi
      // authorizes asynchronously ~0.75 s later) — matching on it would kill
      // every first retry (the W6.10 gate blocker). A catch-all failure that
      // posts a terminal capture_result still resolves via the purge-driven
      // 404 (deadSession) a few seconds later.
      return {
        sessionOver: true,
        reason: String(event.reason || event.error || "The measurement ended."),
      };
    }
    if (
      phase === "capture_deferred" &&
      Number(event.index) === index &&
      Number(event.attempt) === attempt
    ) {
      // NON-terminal soft-hold (§5.7) — distinct from capture_refused above:
      // the Pi is between phases, not refusing. The caller re-posts the
      // SAME begin_capture after a short poll; this does not end the loop.
      return {
        deferred: true,
        code: String(event.code || ""),
        reason: String(event.error || "Waiting for the speaker."),
      };
    }
    await delayMs(pollMs);
  }
  // Marked terminal (mirrors the v2 sweep_failed pattern, XOVER-6) rather
  // than left as a generic status update: a plain error here would leave
  // the STALE "Next measurement"/"Try again" screen on-screen with its
  // button closure still bound to THIS (index, attempt) pair. A retry tap
  // would re-post the same begin_capture — usually a harmless no-op the Pi
  // ignores while still "awaiting_arm", but if the Pi's own state already
  // moved past it, the replay is refused as `begin_replayed`, which is
  // FATAL to the whole session (any capture_refused ends run_capture_plan).
  // Render a clean terminal instead of risking that guaranteed second
  // failure.
  const failure = new Error(
    "the speaker did not respond to the next-measurement request before the timeout",
  );
  failure.sweepFailed = true;
  throw failure;
}

// Poll for the Pi's verdict on the just-uploaded blob. Watches for
// capture_result/capture_set_complete/capture_set_exhausted in the SAME
// loop (not a strict sequence) because the Pi may post both `capture_result`
// and the following `capture_set_complete`/`capture_set_exhausted` before the
// phone's next poll — the relay's host_event slot is last-write-wins, so a
// poll can observe either one depending on timing.
async function waitForCaptureResult(client, spec, index, attempt, target, isAborted) {
  const pollMs = Math.max(100, Math.min(1000, Number(spec.progress_poll_ms) || 250));
  // The Pi's own run_capture_plan loop does not bound consume_capture()'s
  // own run time against its poll deadline (the deadline check only runs
  // BEFORE the next iteration, after consume_capture already returned) — so
  // a slow deconvolution/SNR pass on a loaded Pi has no hard Pi-side ceiling
  // here. Give this wait real headroom rather than reusing the tight
  // admission-latency budget above; scales with the recording window
  // (spec.duration_ms) like waitForSweepComplete's own timeout does, with a
  // floor comfortably above typical analysis time.
  const deadline = Date.now() + Math.max(30000, Number(spec.duration_ms) || 30000);
  while (Date.now() < deadline) {
    if (isAborted()) return { aborted: true };
    let status;
    try {
      status = await client.fetchPhoneStatus();
    } catch (err) {
      if (isDeadSessionError(err)) return { deadSession: true };
      throw err;
    }
    const event = (status && status.host_event) || {};
    const phase = String(event.phase || "");
    if (phase === "capture_incompatible") {
      throw new Error(event.error || "capture page is incompatible with this speaker");
    }
    if (phase === "capture_set_complete") {
      return {
        setComplete: true,
        accepted: Number(event.accepted) || 0,
        target: Number(event.capture_target) || target,
      };
    }
    if (phase === "capture_set_exhausted") {
      return {
        setExhausted: true,
        accepted: Number(event.accepted) || 0,
        target: Number(event.capture_target) || target,
        attempts: Number(event.attempts) || attempt,
      };
    }
    if (
      phase === "capture_result" &&
      Number(event.index) === index &&
      Number(event.attempt) === attempt
    ) {
      return {
        accepted: event.accepted === true,
        error: event.error ? String(event.error) : "",
      };
    }
    await delayMs(pollMs);
  }
  // Terminal, not a stale retry — see waitForCaptureAuthorized's matching
  // comment. By this point the blob has ALREADY been pulled/decrypted (or
  // is still being analyzed) on the Pi; a stale "Next measurement"/"Try
  // again" tap referencing this same (index, attempt) risks the same
  // begin_replayed refusal if the Pi's own state already advanced.
  const failure = new Error(
    "the speaker did not respond with a result for this measurement before the timeout",
  );
  failure.sweepFailed = true;
  throw failure;
}

// Post `begin_capture {index, attempt}` and await the Pi's verdict, RETRYING
// automatically on a `capture_deferred` soft-hold (§5.7) — the Pi replies
// "not yet" while it is between phases, so the phone reposts the IDENTICAL
// begin after a short poll rather than surfacing an error or requiring a
// tap. Renders the waiting screen for each deferral. Returns the same shape
// `waitForCaptureAuthorized` does (authorized / refused / deferred never
// escapes this function / aborted / deadSession).
//
// W6.13: `setup` PIGGYBACKS on every begin post. A v2 capture-plan session
// has no calibration-picker/confirm screen to post setup from — unlike the
// legacy level_ramp flow, whose Continue tap (validateSetupBeforeContinue)
// posts it well before any capture — so until this fix the silently-applied
// household-mic calibration (`applyDefaultCalibrationHintSilently`, boot())
// only ever reached the wire inside the much later `armed` event. Riding the
// begin itself (not a separate standalone post) matters because the relay's
// phone-event slot is last-write-wins: a standalone setup event would be
// overwritten by this begin within one write-RTT, usually before the Pi's
// ~0.75 s poll ever saw it — the exact overwrite class the ambient_stats
// piggyback comment in onStart documents. On EVERY begin (not just
// the first) so deferral re-posts and later rounds keep the slot carrying
// setup no matter which event a Pi poll lands on; `armed` still carries the
// identical setup as belt-and-suspenders.
async function beginAndAwaitAuthorization(ctx, { index, attempt }) {
  const { spec, client } = ctx;
  const controller = ctx.planController;
  const { target } = planTargetAndAttempts(spec);
  for (;;) {
    setStatus(`Requesting measurement ${index} of ${target}…`, "info");
    await client.postEvent({
      begin_capture: { index, attempt },
      setup: setupWirePayload(),
    });
    if (controller.aborted) return { aborted: true };
    const admission = await waitForCaptureAuthorized(
      client, spec, index, attempt, () => controller.aborted,
    );
    if (controller.aborted || admission.aborted) return { aborted: true };
    if (admission.deferred) {
      renderPlanDeferred(ctx, { index, target, reason: admission.reason });
      await delayMs(CAPTURE_DEFERRED_RETRY_POLL_MS);
      if (controller.aborted) return { aborted: true };
      continue; // re-post the SAME begin_capture and try again
    }
    return admission;
  }
}

// The label of the live begin affordance on the current plan screen —
// "Next measurement" / "Try again" / the spec's own Start-button label —
// so a pre-arm failure's retry copy names a button that actually exists
// (the plan screens have no button called "Start").
function planRetryAffordance(ctx) {
  const begin = ((ctx.captureRefs && ctx.captureRefs.buttons) || []).find(
    (entry) => entry && entry.action === "begin_capture",
  );
  const label = begin && begin.el ? String(begin.el.textContent || "").trim() : "";
  return label || "the measurement button";
}

// One capture round: begin -> Pi admission -> quiet window + sweep + upload
// (index-aware) -> Pi verdict -> the next screen. Invoked once from
// onPlanStart (index 1, attempt 1) and thereafter from a "Next measurement" /
// "Try again" tap.
async function runPlanCapture(ctx, { index, attempt }) {
  const { spec, client } = ctx;
  const controller = ctx.planController;
  const { target } = planTargetAndAttempts(spec);
  // A fresh round cancels any pending auto-advance (e.g. a countdown, or a Cancel
  // that dropped to the manual tap then the tap fired) so no stale timer fires.
  clearAutoAdvance(ctx);
  let disposeWatch = () => {};
  // Whether this round's `armed` post was ATTEMPTED (set just before the
  // await — a lost response may still have armed the Pi). It splits the
  // generic catch below: before arming, the round has not started on the
  // Pi and the on-screen begin affordance is safe to re-tap; after arming,
  // the Pi may already have played this attempt's stimulus, so a re-tap of
  // the SAME (index, attempt) is never sound — it is either refused
  // (begin_replayed → session-ending CaptureFailed), rejected by the
  // Worker's one-upload-per-index guard (409), or worst, re-records a
  // sweep-less window that uploads as a silently-wrong capture.
  let armedPosted = false;

  try {
    const admission = await beginAndAwaitAuthorization(ctx, { index, attempt });
    if (controller.aborted || admission.aborted) return;
    if (admission.deadSession) {
      renderSessionExpired(ctx);
      await endPlanSession(ctx);
      return;
    }
    if (admission.sessionOver) {
      // The session collapsed while we were holding/awaiting (blocker #3) —
      // terminal, exactly like a dead session; the speaker page shows the
      // specific reason and how to start over.
      renderSessionExpired(ctx);
      await endPlanSession(ctx);
      return;
    }
    if (admission.refused) {
      renderPlanRefused(ctx, admission);
      await endPlanSession(ctx);
      return;
    }

    advanceDeferredHoldHeading(ctx);
    // Fix (#1658): the mic stream + audio graph are acquired ONCE per session
    // and reused across every capture instead of reopening getUserMedia per
    // attempt — iOS renegotiates the input chain on each fresh stream, which
    // measurably shifts level/spectrum between captures. ctx.recorderFailure
    // is set only when a background reacquire already ran and failed
    // (wireTrackEndedRecovery, below) — surface that now rather than
    // silently trying to open yet another stream.
    if (ctx.recorderFailure) {
      const failure = ctx.recorderFailure;
      ctx.recorderFailure = null;
      throw failure;
    }
    let recorder = ctx.recorder;
    if (!recorder) {
      setStatus("Starting microphone…", "info");
      recorder = await createMonoRecorder({
        sampleRate: spec.sample_rate_hz || 48000,
        deviceId: selectedDeviceId,
      });
      if (controller.aborted || ctx.sessionEnded) {
        // B1: Stop (or a backgrounded abort) won the race against
        // getUserMedia + worklet compile — the session already tore down
        // while this was in flight. Close it here rather than assigning it
        // to ctx.recorder, which would orphan a live mic stream with
        // nothing left to close it (reviewer-demonstrated: mic stayed hot
        // after "Measurement stopped" until page reload).
        await recorder.close();
        return;
      }
      wireTrackEndedRecovery(ctx, recorder, spec);
    }
    const capture = inspectRecorder(recorder, spec);
    if (capture.decision.action === "refuse") {
      await recorder.close();
      ctx.recorder = null;
      // Pre-arm refusal: the round never started on the Pi, so keep the
      // on-screen begin affordance live AND Stop wired (no endPlanSession)
      // — plugging in a USB measurement mic and re-tapping is a legitimate
      // recovery, and Stop must keep working while the household decides.
      // For round 1 specifically, that affordance is the SAME begin_capture
      // button onPlanStart itself was invoked from — mark the session as
      // parked at a retriable failure so a re-tap re-enters capture instead
      // of being swallowed by onPlanStart's re-entrancy guard (B-1).
      ctx.parkedAtRetriableFailure = true;
      setStatus(
        `This phone can't run a clean measurement (${capture.decision.reason}). ` +
          "Try a different phone, or use a calibrated USB mic on the speaker.",
        "error",
      );
      return;
    }
    ctx.recorder = recorder;
    // S1: a context held across the whole session can be auto-suspended
    // between rounds (Android Chrome backgrounding a tab; possibly iOS
    // foreground idle) WITHOUT its mic track ever reaching `ended` — the
    // signal wireTrackEndedRecovery relies on — so resume it explicitly
    // before recording. Idempotent when already running; guarded so a test
    // stub with no `.context` stays a no-op.
    if (recorder.context && typeof recorder.context.resume === "function") {
      await recorder.context.resume();
    }

    disposeWatch = watchVisibilityAbort(
      typeof document !== "undefined" ? document : null,
      (reason) => {
        void controller.abort(reason);
      },
    );

    const noise = await captureAmbientNoise(recorder, spec);
    if (controller.aborted) return;

    recorder.start();
    setStatus(
      capture.decision.degraded
        ? `Recording at lower confidence — ${capture.decision.reason}. Waiting for the speaker.`
        : "Recording — waiting for the speaker to start.",
      "recording",
    );

    armedPosted = true;
    await client.postEvent({
      armed: true,
      degraded: capture.decision.degraded,
      device: capture.device,
      noise_floor: { duration_ms: noise.duration_ms, rms_dbfs: noise.rms_dbfs },
      begin_capture: { index, attempt },
      setup: setupWirePayload(),
      acknowledgement: ctx.planAcknowledgement,
      ...ambientStatsFieldsFor(spec, noise),
    });

    const sweepCompleted = await waitForSweepComplete(client, spec, () => controller.aborted);
    if (sweepCompleted === false) {
      await endPlanSession(ctx);
      return;
    }
    await delayMs(Math.max(0, Number(spec.post_roll_ms) || 700));
    if (controller.aborted) return;
    const samples = await recorder.stop({ timeoutMs: 5000 });
    if (controller.aborted) return;
    if (recorder.__trackEnded) {
      // The mic track died during THIS round's own recording window — a
      // dying track does not error the audio graph, it just goes silent, so
      // trust nothing recorded after that point. armedPosted is already
      // true, so this routes to the terminal failure screen below, exactly
      // like any other post-arm failure — never a silent dead-air upload.
      throw new Error("the microphone disconnected during this measurement");
    }

    setStatus("Encrypting and uploading…", "info");
    const wavBytes = await blobToBytes(
      float32ToWavBlob(samples, spec.sample_rate_hz || 48000),
    );
    const key = await importContentKey(ctx.contentKeyB64);
    const { blob, plaintextLen, sha256 } = await encryptWav(key, wavBytes);
    if (!withinUploadCap(blob.length, spec)) {
      // Post-arm, so terminal (S1): the sweep already played for this
      // attempt; a re-tap of the same round can never produce a sound
      // capture. sweepFailed routes the catch to renderSweepFailed.
      const failure = new Error("this recording is too large to upload");
      failure.sweepFailed = true;
      throw failure;
    }
    // Each admitted attempt's blob rides its OWN relay key
    // (capture_index = attempt - 1) so a retried slot never clobbers the
    // prior attempt's upload.
    await client.putBlob(blob, plaintextLen, sha256, attempt - 1);
    if (controller.aborted) return;

    setStatus("Speaker is checking this measurement…", "info");
    const verdict = await waitForCaptureResult(
      client, spec, index, attempt, target, () => controller.aborted,
    );
    if (controller.aborted || verdict.aborted) return;
    if (verdict.deadSession) {
      renderSessionExpired(ctx);
      await endPlanSession(ctx);
      return;
    }
    if (verdict.setComplete || (verdict.accepted && index >= target)) {
      renderPlanAllDone(ctx, { index });
      await endPlanSession(ctx);
      return;
    }
    if (verdict.setExhausted) {
      renderPlanExhausted(ctx, verdict);
      await endPlanSession(ctx);
      return;
    }
    if (verdict.accepted) {
      // Route by the UPCOMING entry's auto-advance policy (§5.2): a hold that
      // owns the screen (on_apply), a cancelable countdown, or the manual tap.
      advanceAfterAccepted(ctx, { index, attempt, target });
    } else {
      renderPlanRetry(ctx, { index, attempt, target, reason: verdict.error });
    }
  } catch (err) {
    if (!controller.aborted) {
      if (err && err.sweepFailed) {
        renderSweepFailed(ctx, err);
        await endPlanSession(ctx);
      } else if (isDeadSessionError(err)) {
        renderSessionExpired(ctx);
        await endPlanSession(ctx);
      } else if (armedPosted) {
        // Post-arm generic failure (S1 — e.g. a transient putBlob error or
        // a recorder.stop timeout after the sweep played): TERMINAL, mirror
        // the timeout paths. Leaving the previous "Next measurement"/"Try
        // again" button live would let a re-tap post a begin for the SAME
        // already-consumed (index, attempt) — see armedPosted's comment for
        // why that is never sound.
        renderSweepFailed(ctx, err);
        await endPlanSession(ctx);
      } else {
        // Pre-arm failure (mic permission denied, a transient begin-post
        // or authorization hiccup): the round never started on the Pi, so
        // the on-screen begin affordance is safe to re-tap. Keep it live,
        // keep Stop wired (no endPlanSession), and name the ACTUAL button
        // in the copy — these screens have no button called "Start". For
        // round 1, that affordance is onPlanStart's own begin_capture
        // button — mark the retriable-failure park (B-1, see the matching
        // guard in onPlanStart) so the re-tap re-enters capture.
        ctx.parkedAtRetriableFailure = true;
        setStatus(captureFailureMessage(err, planRetryAffordance(ctx)), "error");
      }
    }
  } finally {
    disposeWatch();
  }
}

// Entry point wired to the spec's own `begin_capture` button (rendered by
// the standard DATA renderer, same as v2's onStart) for a v3
// (capture_protocol_version=3 + capture_plan) spec. Captures the operator's
// placement acknowledgement ONCE, up front — the acknowledgement's identity
// (id/binding_id) is fixed by the spec, not re-derived per round, since a
// repeat SET measures the SAME physical placement multiple times and there
// is no per-round checkbox to re-tick on the page-owned "Next measurement" /
// "Try again" screens.
async function onPlanStart(ctx) {
  // N3 + B-1: guard re-entrancy WITHOUT dead-ending the documented retry
  // affordance. The initial begin_capture button stays wired to onPlanStart
  // for as long as round 1 has never successfully authorized+armed — which
  // includes the THREE pre-arm-failure paths in runPlanCapture (mic
  // permission denied, a clean-capture refusal, a transient begin-post
  // hiccup) that keep this exact button on screen as "Tap … to try again"
  // (planRetryAffordance) without ever ending the session. Those paths set
  // ctx.parkedAtRetriableFailure so THIS tap is recognized as the legitimate
  // retry it is, distinct from a genuine accidental double-tap (which must
  // stay inert — a round is either about to start or already in flight, and
  // starting a SECOND session on top of it would leak the first's wake lock
  // + visibility watcher).
  //
  // isRetry does not also check !ctx.sessionEnded: releasePlanSessionResources
  // (the SOLE writer of sessionEnded=true) always clears
  // parkedAtRetriableFailure in the same breath, so parkedAtRetriableFailure
  // being true already guarantees the session has not ended — checking both
  // was a redundant conjunct that let a re-tap after a TRUE session end (e.g.
  // the host cancelling the sweep, sweep_cancelled, which ends the session via
  // endPlanSession without ever replacing this screen) fall through into a
  // wasted acquire-then-immediately-release of a fresh wake lock instead of
  // being blocked outright. planController being set is already sufficient to
  // know "this is not the very first tap".
  const isRetry = Boolean(ctx.planController) && ctx.parkedAtRetriableFailure;
  if (ctx.planController && !isRetry) return;
  let acknowledgement = null;
  try {
    acknowledgement = acceptedAcknowledgement(ctx.spec, ctx.captureRefs);
  } catch {
    setStatus("Confirm the microphone placement before starting.", "error");
    return;
  }
  ctx.planAcknowledgement = acknowledgement;

  if (isRetry) {
    // Re-enter capture WITHOUT re-acquiring anything already held by the
    // still-live session — a fresh controller/wake-lock/visibility-watch
    // here would orphan the originals (the same leak class B1/N2 fixed
    // elsewhere). The mic is the only thing that actually needs a fresh
    // attempt; runPlanCapture's own reuse-or-create step handles that.
    ctx.parkedAtRetriableFailure = false;
    ctx.recorderFailure = null; // N-a: never resurface an unrelated stale failure
    await runPlanCapture(ctx, { index: 1, attempt: 1 });
    return;
  }

  const controller = makePlanController(ctx);
  ctx.planController = controller;
  // Wire Stop BEFORE any await below — Stop must be live from the instant
  // Start is tapped, not gated behind the wake-lock request settling.
  activeAbort = controller.abort;
  // Fix 1 (#1658): the wake lock is held for the WHOLE session (every round's
  // idle gaps included), not re-acquired per round — acquired right here, in
  // the tap that started the session, per the Wake Lock API's user-gesture
  // expectation. It auto-releases whenever the page hides; the visibility
  // watch below re-requests it once the phone returns, silently, unless the
  // session has already ended.
  const wakeLock = await acquireWakeLockWithHint();
  if (ctx.sessionEnded) {
    // Stop/backgrounded fired while this request was in flight — the session
    // already tore down (releasePlanSessionResources already ran). Release
    // this lock rather than leaking it: an unreleased lock would otherwise
    // keep the screen on with nothing left running.
    try {
      await wakeLock.release();
    } catch {
      /* already released */
    }
    return;
  }
  ctx.wakeLock = wakeLock;
  ctx.disposeSessionVisibilityWatch = watchVisibilityReacquire(
    typeof document !== "undefined" ? document : null,
    () => {
      void reacquireSessionWakeLock(ctx);
    },
    () => !ctx.sessionEnded,
  );
  await runPlanCapture(ctx, { index: 1, attempt: 1 });
}

// The whole capture leg, behind the single Start tap.
async function onStart(ctx) {
  const { spec, client, contentKeyB64 } = ctx;
  let acknowledgement = null;
  try {
    acknowledgement = acceptedAcknowledgement(spec, ctx.captureRefs);
  } catch {
    setStatus("Confirm the microphone placement before starting.", "error");
    return;
  }
  let recorder = null;
  let wakeLock = null;
  let disposeWatch = () => {};
  let aborted = false;

  // Abort path (step 7): if the page is backgrounded mid-capture, stop, surface
  // the failure visibly, and tell the Pi on its next relay poll — never upload
  // garbage.
  const abort = async (reason) => {
    if (aborted) return;
    aborted = true;
    if (reason === "stopped") {
      renderStoppedScreen(ctx);
    } else {
      setStatus(
        reason === "backgrounded"
          ? "Measurement stopped — this phone's screen must stay on. Tap Start to try again."
          : `Measurement stopped — ${reason}. Tap Start to try again.`,
        "error",
      );
    }
    try {
      await client.postEvent({ aborted: true, abort_reason: reason });
    } catch {
      /* the Pi also times out if it never hears the abort */
    }
    if (recorder) {
      try {
        await recorder.close();
      } catch {
        /* already closed */
      }
      recorder = null;
    }
  };
  activeAbort = abort;

  try {
    setStatus("Starting microphone…", "info");
    // getUserMedia must be inside this user gesture (iOS). EC/AGC/NS are forced
    // off by measurement-audio's mono constraints.
    recorder = await createMonoRecorder({
      sampleRate: spec.sample_rate_hz || 48000,
      deviceId: selectedDeviceId,
    });

    // Measurement validity is loud (step 6, §9): verify the REALIZED constraints
    // — WebKit has historically ignored echoCancellation:false. Decide per the
    // spec's per-kind policy before wasting the user's time recording.
    const capture = inspectRecorder(recorder, spec);
    const { decision } = capture;
    // Which mic actually recorded (track.label is the device name, available once
    // permission is granted). The Pi uses this to decide whether a loaded vendor
    // calibration applies — the phone built-in mic ⇒ refuse it, a USB measurement
    // mic ⇒ apply it. It rides the opaque `armed` event below, not the E2E WAV.
    const captureDevice = capture.device;
    if (decision.action === "refuse") {
      await recorder.close();
      recorder = null;
      setStatus(
        `This phone can't run a clean measurement (${decision.reason}). ` +
          "Try a different phone, or use a calibrated USB mic on the speaker.",
        "error",
      );
      return;
    }

    // Hold the screen on for the capture; if it backgrounds anyway, abort
    // visibly and let the Pi observe the failure through the relay.
    wakeLock = await acquireWakeLockWithHint();
    disposeWatch = watchVisibilityAbort(
      typeof document !== "undefined" ? document : null,
      (reason) => {
        void abort(reason);
      },
    );
    const noise = await captureAmbientNoise(recorder, spec);
    if (aborted) return;

    recorder.start();
    setStatus(
      decision.degraded
        ? `Recording at lower confidence — ${decision.reason}. Waiting for the speaker.`
        : "Recording — waiting for the speaker to start.",
      "recording",
    );

    // Drop `armed` so the Pi plays the stimulus inside our window. `degraded`
    // rides along so the Pi can mark a capability-fallback capture lower-confidence.
    // ambientStatsFieldsFor() rides on the SAME already-awaited post (never a
    // separate one): the relay's phone-event slot is last-write-wins, so a
    // standalone ambient_stats event posted just before this one would
    // almost always be overwritten before the Pi's ~0.75s poll ever saw it.
    // Piggybacking costs zero extra network round trips — "must not delay
    // the capture sequence" for free.
    await client.postEvent({
      armed: true,
      degraded: decision.degraded,
      device: captureDevice,
      noise_floor: { duration_ms: noise.duration_ms, rms_dbfs: noise.rms_dbfs },
      setup: setupWirePayload(),
      acknowledgement,
      ...ambientStatsFieldsFor(spec, noise),
    });

    // Record until the Pi reports that the real sweep finished, then keep a
    // short tail. `duration_ms` is now the hard timeout, not the normal stop
    // condition.
    const sweepCompleted = await waitForSweepComplete(client, spec, () => aborted);
    if (sweepCompleted === false) return;
    await delayMs(Math.max(0, Number(spec.post_roll_ms) || 700));
    if (aborted) return;
    const samples = await recorder.stop({ timeoutMs: 5000 });
    if (aborted) return;
    await recorder.close();
    recorder = null;

    setStatus("Encrypting and uploading…", "info");
    const wavBytes = await blobToBytes(
      float32ToWavBlob(samples, spec.sample_rate_hz || 48000),
    );
    const key = await importContentKey(contentKeyB64);
    const { blob, plaintextLen, sha256 } = await encryptWav(key, wavBytes);
    // Page half of the dual size cap (§8): fail loud locally rather than after a
    // wasted upload that the Worker would 413.
    if (!withinUploadCap(blob.length, spec)) {
      setStatus(
        "This recording is too large to upload. Try a shorter measurement.",
        "error",
      );
      return;
    }
    await client.putBlob(blob, plaintextLen, sha256);

    // Refresh browser-held setup only for legacy bound capture-only flows.
    // Modern unbound Room specs no-op here; the Pi owns their position and mic
    // identity. Never extend the bound flow's absolute privacy limit.
    if (setupCaptureOnly) refreshBoundSetup(spec);

    renderCaptureComplete(ctx);
    setStatus("Done — your speaker is analyzing the measurement.", "done");
  } catch (err) {
    if (!aborted) {
      if (err && err.sweepFailed) {
        renderSweepFailed(ctx, err);
      } else if (isDeadSessionError(err)) {
        renderSessionExpired(ctx);
      } else {
        setStatus(captureFailureMessage(err), "error");
      }
    }
  } finally {
    // Host Stop is expected control flow and returns above without throwing.
    // Close here so every exit path tears down the mic track, worklet, and
    // AudioContext; abort() already nulls the recorder after doing the same.
    if (recorder) {
      try {
        await recorder.close();
      } catch {
        /* already closed */
      }
      recorder = null;
    }
    disposeWatch();
    if (wakeLock) await wakeLock.release();
    hideWakeLockHint();
    if (activeAbort === abort) activeAbort = null;
  }
}

async function boot() {
  const screenEl = document.getElementById("screen");
  // buildMicPicker() inserts its "Microphone:" selector as a SIBLING just
  // before `screenEl`, not as a child of it — so it lives outside what
  // setScreen()'s replaceChildren() below clears. Without this removal, a
  // hashchange re-boot (onHashChange -> bootFromHash -> boot) left the PRIOR
  // boot's picker in place and stacked a second one beside it.
  if (micPickerEl && micPickerEl.parentNode) {
    micPickerEl.remove();
  }
  micPickerEl = null;
  // Own the screen with a clear, NON-interactive loading affordance for the
  // couple of seconds boot takes (blocker #4d). This also clears any stale
  // controls — e.g. a bfcache-restored DOM whose handlers are not yet
  // re-attached would otherwise present dead, tap-swallowing buttons — so the
  // household never taps a control that silently does nothing.
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Connecting to your speaker…" }),
    el("p", { class: "cap-note", text: "One moment — getting this measurement ready." }),
  ]);
  let handle;
  try {
    handle = parseFragment(globalThis.location ? globalThis.location.hash : "");
  } catch (err) {
    setStatus(captureFailureMessage(err), "error");
    return;
  }

  const client = new RelayClient({
    baseUrl: RELAY_BASE,
    sessionId: handle.sessionId,
    uploadToken: handle.uploadToken,
  });

  let spec;
  try {
    setStatus("Connecting to your speaker…", "info");
    const [pageIdentity, specText] = await Promise.all([
      loadCapturePageIdentity(),
      client.fetchSpecText(),
    ]);
    const verified = await verifyAndParseCaptureSpec(specText, {
      contentKeyB64: handle.contentKeyB64,
      sessionId: handle.sessionId,
      specMac: handle.specMac,
    });
    spec = verified.spec;
    assertCaptureProtocolCompatible(spec, pageIdentity);
    client.setCapturePageIdentity(pageIdentity);
    client.setTransportIntegrity(verified.integrity, {
      required: Number(requiredCaptureProtocol(spec)) >= 2,
    });
  } catch (err) {
    setStatus(relayBootFailureMessage(err), "error");
    return;
  }

  let boundSetup = null;
  setupCaptureOnly = spec.kind === "room_sweep" && spec.setup_validation === false;
  if (setupCaptureOnly) {
    const bindingId = setupBindingId(spec);
    boundSetup = loadBoundSetup(spec) || (!bindingId ? {
      identity: null,
      summary: { total_positions: Number(spec.total_positions) || 1 },
    } : null);
    if (bindingId && !boundSetup) {
      const returnUrl = safeReturnUrl(spec);
      const children = [
        el("h1", { class: "cap-heading", text: "Run the level check again" }),
        el("p", {
          class: "cap-note",
          text: "This phone no longer has the setup identity from the safe level check. Return to the speaker and run that check again before measuring.",
        }),
      ];
      if (returnUrl) {
        children.push(linkButton("Back to speaker", returnUrl));
      } else {
        children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
      }
      setScreen(screenEl, children);
      setStatus("Measurement setup expired — run the level check again.", "error");
      return;
    }
    setupIdentity = boundSetup.identity || null;
    setupState = {
      total_positions: Number(boundSetup.summary && boundSetup.summary.total_positions) || 5,
      calibration: {
        mode: String(
          (boundSetup.summary && boundSetup.summary.calibration &&
            boundSetup.summary.calibration.mode) || "none",
        ),
        model: String(
          (boundSetup.summary && boundSetup.summary.calibration &&
            boundSetup.summary.calibration.model) || "",
        ),
      },
    };
  }

  const ctx = {
    spec,
    client,
    contentKeyB64: handle.contentKeyB64,
    screenEl,
    boundSetup,
  };
  if (setupCaptureOnly) {
    renderBoundRoomReady(screenEl, ctx);
  } else if (spec.kind === "room_sweep" || spec.kind === "level_ramp") {
    renderIntro(screenEl, ctx);
  } else {
    // Session-spanning capture plans (protocol v3, SPEC W2.3): the Pi marker
    // + capture_plan are both required together (CaptureSpec.validate()), so
    // checking capture_plan alone is sufficient, but check both defensively.
    const isPlanSpec = spec.capture_protocol_version === 3 && Boolean(spec.capture_plan);
    // W6.12: this branch (crossover_sweep, legacy or v2 plan) has no
    // calibration-picker screen — apply the household-mic hint silently
    // before the first `armed`/`setup` event carries setupWirePayload().
    applyDefaultCalibrationHintSilently(spec);
    ctx.captureRefs = renderScreen(screenEl, spec, {
      handlers: {
        begin_capture: () => (isPlanSpec ? onPlanStart(ctx) : onStart(ctx)),
        retry: () => onStart(ctx),
        stop: stopCapture,
      },
    });
    void buildMicPicker(screenEl);
    setStatus(
      spec.acknowledgement
        ? "Ready. Follow the placement steps, confirm them, then start."
        : "Ready. Stand at your listening position and tap Start.",
      "info",
    );
  }
}

// The currently-inserted mic-picker element, if any — tracked so a re-boot
// can remove the PRIOR boot's picker (see boot() above) instead of appending
// a second one beside it, since the picker lives outside the boot-render
// root (`screenEl`) that setScreen() clears.
let micPickerEl = null;

// Best-effort input picker for a USB-C measurement mic plugged into the phone.
// Progressive enhancement: it appears only when the browser exposes ≥2 labeled
// audio inputs (Android Chrome typically does). It stays hidden when labels are
// gated behind mic permission (notably iOS Safari pre-permission) or there is one
// input — there the OS default is used, which is the USB mic when one is plugged
// in. Either way the actually-used device is reported in the `armed` event, so the
// Pi's device-aware calibration gate works with or without this picker.
async function buildMicPicker(beforeEl) {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  if (!nav || !nav.mediaDevices || !nav.mediaDevices.enumerateDevices) return;
  let devices;
  try {
    devices = await nav.mediaDevices.enumerateDevices();
  } catch {
    return; // enumerate blocked/unsupported — fall back to the OS default input
  }
  const inputs = devices.filter((d) => d.kind === "audioinput" && d.label);
  if (inputs.length < 2) return; // nothing useful to choose; keep the OS default
  const wrap = document.createElement("label");
  wrap.className = "mic-picker";
  wrap.append("Microphone: ");
  const select = document.createElement("select");
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "Automatic (recommended)";
  select.appendChild(auto);
  for (const d of inputs) {
    const opt = document.createElement("option");
    opt.value = d.deviceId;
    opt.textContent = d.label; // browser-provided → textContent, never innerHTML
    select.appendChild(opt);
  }
  select.value = selectedDeviceId;
  if (select.value !== selectedDeviceId) {
    // Same fix as renderMicChoice above: a mismatch this render is not
    // proof the remembered device is gone for good — never overwrite the
    // stored preference here (run-19 defect).
    selectedDeviceId = "";
  }
  select.addEventListener("change", () => {
    selectedDeviceId = select.value;
    rememberDeviceId(selectedDeviceId);
  });
  wrap.appendChild(select);
  if (beforeEl && beforeEl.parentNode) {
    beforeEl.parentNode.insertBefore(wrap, beforeEl);
    micPickerEl = wrap;
  }
}

// The fragment (`#…`) the current wizard instance booted from. A hashchange to
// a DIFFERENT fragment (a freshly-scanned QR / a new link, or the speaker page
// swapping the link) must re-initialize the whole wizard — a page navigated by
// fragment alone never re-runs its module, so without this the new session
// would never load (blocker #4c).
let currentBootHash = null;

function readBootHash() {
  return globalThis.location ? globalThis.location.hash : "";
}

async function bootFromHash() {
  currentBootHash = readBootHash();
  await boot();
}

function onHashChange() {
  if (readBootHash() === currentBootHash) return;
  // Tear down any in-flight capture on the OLD session first so its loop cannot
  // clobber the fresh boot's render; the re-boot then rebuilds the wizard state
  // from the new fragment.
  if (typeof activeAbort === "function") void activeAbort("restarted");
  void bootFromHash();
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  if (typeof window.addEventListener === "function") {
    window.addEventListener("hashchange", onHashChange);
    // bfcache restore (Back/Forward): the DOM is restored with buttons whose
    // handlers are NOT re-bound, so taps would be silently swallowed (blocker
    // #4d). Re-boot to re-attach handlers against a fresh wizard state.
    window.addEventListener("pageshow", (event) => {
      if (event && event.persisted) void bootFromHash();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootFromHash);
  } else {
    void bootFromHash();
  }
}

export {
  boot,
  onStart,
  onLevelRampStart,
  onPlanStart,
  relayBootFailureMessage,
  stopCapture,
  isDeadSessionError,
  validDefaultSetupHint,
  applyDefaultCalibrationHintSilently,
  setupWirePayload,
  calibrationModelLabel,
  renderCalibration,
  entryForIndex,
  renderPlanDeferred,
  advanceDeferredHoldHeading,
};
