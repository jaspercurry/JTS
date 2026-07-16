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
import { RelayClient } from "./relay-client.js?v=20260715-3";
import { importContentKey, encryptWav } from "./crypto.js";
import {
  constraintDecision,
  verifyRealizedConstraints,
} from "./constraints.js?v=20260711-4";
import { safeReturnUrl } from "./return-url.js";
import { acquireWakeLock, watchVisibilityAbort } from "./wakelock.js";
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

function setupValidationToken() {
  const cryptoObj = globalThis.crypto || {};
  if (typeof cryptoObj.randomUUID === "function") return cryptoObj.randomUUID();
  const random = Math.random().toString(36).slice(2);
  return `setup-${Date.now()}-${random}`;
}

function captureFailureMessage(err) {
  const message = err && err.message ? String(err.message) : String(err);
  if (message === "not_found") {
    return (
      "This one-time capture link has expired. Return to the speaker page " +
      "and create a new phone capture link."
    );
  }
  // Trim a trailing period so wrapping a message that is already a full
  // sentence (e.g. FragmentError's own friendly text) never produces "..".
  return `Measurement failed: ${message.replace(/\.+$/, "")}. Tap Start to try again.`;
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
    selectedDeviceId = "";
    rememberDeviceId("");
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

function renderCalibration(screenEl, ctx) {
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
      if ((setupState.calibration || {}).mode === "upload") {
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
    if (ctx.spec.kind === "level_ramp") {
      if (collectsRoomPositions(ctx.spec)) {
        renderPositionCount(screenEl, ctx);
        return;
      }
      try {
        await bindSetupBeforeLevel(ctx);
      } catch (err) {
        setStatus(captureFailureMessage(err), "error");
        return;
      }
      renderLevelReady(screenEl, ctx);
    } else {
      try {
        await validateSetupBeforeContinue(ctx);
      } catch (err) {
        setStatus(captureFailureMessage(err), "error");
        return;
      }
      renderPositionCount(screenEl, ctx);
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
    handlers: { begin_capture: () => onLevelRampStart(ctx) },
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
  };
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
  for (const ref of buttons) ref.el.disabled = Boolean(disabled);
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
    setStatus(
      reason === "backgrounded"
        ? "Level check stopped — this phone's screen must stay on."
        : `Level check stopped — ${reason}.`,
      "error",
    );
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

    wakeLock = await acquireWakeLock();
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
    if (!aborted) setStatus(captureFailureMessage(err), "error");
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
    setCaptureButtonsDisabled(ctx, false);
  }
}

async function waitForSweepComplete(client, spec, isAborted) {
  const timeoutMs = Math.max(5000, Number(spec.duration_ms) || 20000);
  const pollMs = Math.max(100, Math.min(1000, Number(spec.progress_poll_ms) || 250));
  const deadline = Date.now() + timeoutMs;
  let lastPhase = "";
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
        setStatus(
          "Measuring room noise — stay quiet and keep the phone still.",
          "recording",
        );
      } else if (phase === "sweep_started") {
        setStatus("Tone is playing — stay quiet and keep the phone still.", "recording");
      } else if (phase === "sweep_complete") {
        setStatus("Tone finished — capturing the room tail.", "recording");
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
    setStatus(
      reason === "backgrounded"
        ? "Measurement stopped — this phone's screen must stay on. Tap Start to try again."
        : `Measurement stopped — ${reason}. Tap Start to try again.`,
      "error",
    );
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
    wakeLock = await acquireWakeLock();
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
    await client.postEvent({
      armed: true,
      degraded: decision.degraded,
      device: captureDevice,
      noise_floor: noise,
      setup: setupWirePayload(),
      acknowledgement,
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
  }
}

async function boot() {
  const screenEl = document.getElementById("screen");
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
    ctx.captureRefs = renderScreen(screenEl, spec, {
      handlers: {
        begin_capture: () => onStart(ctx),
        retry: () => onStart(ctx),
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
    selectedDeviceId = "";
    rememberDeviceId("");
  }
  select.addEventListener("change", () => {
    selectedDeviceId = select.value;
    rememberDeviceId(selectedDeviceId);
  });
  wrap.appendChild(select);
  if (beforeEl && beforeEl.parentNode) {
    beforeEl.parentNode.insertBefore(wrap, beforeEl);
  }
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}

export { boot, onStart, onLevelRampStart, relayBootFailureMessage };
