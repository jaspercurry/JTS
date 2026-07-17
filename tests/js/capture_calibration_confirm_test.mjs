// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the one-tap household-mic confirm (capture-page/js/main.js —
// Wave-2 CaptureSpec.default_setup_calibration #1540, amended per the
// coordinator's adjudication to SUBMIT {mode: "stored", calibration_id}
// when the Pi marks the hint `resolvable: true`).
//
// Covers the three adjudicated contracts:
//   1. Confirm submits the stored payload shape EXACTLY as the Pi's
//      mode="stored" branch expects: setup.calibration =
//      {mode: "stored", calibration_id, model} riding the ordinary
//      setup_validate event.
//   2. resolvable absent/false (an older Pi build) renders today's full
//      picker — the compat pin; the confirm screen never appears.
//   3. A Pi rejection of the stored resolution (record went stale between
//      spec mint and submit) falls back to the full picker with a plain
//      sentence — no dead end — and the failed one-tap is not re-offered.
//
// Plus the pure validDefaultSetupHint/calibrationModelLabel helpers.
// Mirrors capture_plan_loop_test.mjs's strip-and-inject + DOM-stub harness.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const raw = readFileSync(resolve(here, "../../capture-page/js/main.js"), "utf8");
const withoutImports = raw
  .replace(
    /^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']+["'];\s*/gm,
    "",
  )
  .replace(/^import\s+[^;\n]+\s+from\s+["'][^"']+["'];\s*/gm, "")
  .replace(
    /^const PAGE_VERSION_URL = .*;$/m,
    'const PAGE_VERSION_URL = new URL("https://capture.test/version.json");',
  );
if (/^import\s/m.test(withoutImports)) {
  throw new Error("unhandled import in main.js — update the harness strip rule");
}

const injected = `
const acceptedAcknowledgement = () => null;
const createMonoRecorder = async () => { throw new Error("unused"); };
const delayMs = async () => {};
const safeReturnUrl = () => "";
const rmsToDbfs = () => -120;
const verifyRealizedConstraints = () => ({ clean: true, dirtyFlags: [] });
const constraintDecision = () => ({ action: "proceed", degraded: false, reason: "" });
const acquireWakeLock = async () => ({ release: async () => {} });
const watchVisibilityAbort = () => () => {};
const buildAmbientStatsEvent = () => ({});
const importContentKey = async () => ({});
const encryptWav = async () => ({ blob: new Uint8Array(), plaintextLen: 0, sha256: "" });
const float32ToWavBlob = () => ({ async arrayBuffer() { return new Uint8Array().buffer; } });
const withinUploadCap = () => true;
const inferCalibrationModel = () => null;
const renderScreen = () => ({ buttons: [], levelMeters: [] });
const runLevelRampProtocol = async () => ({ state: "locked", terminal: true });
// setup-store.js stubs (the real module is browser-storage backed). The
// binding-id derivation mirrors the real setupBindingId so the validation
// gate in validateSetupBeforeContinue behaves faithfully for unbound specs.
const setupBindingId = (spec) => {
  const value = String(
    (spec && spec.setup_binding_id) ||
      (spec && spec.kind === "level_ramp" && spec.run_token) ||
      "",
  ).trim();
  return /^[A-Za-z0-9_-]{12,160}$/.test(value) ? value : "";
};
const loadBoundSetup = () => null;
const storeBoundSetup = () => true;
const refreshBoundSetup = () => true;
`;

// Module state (setupState, storedHintFailed) is per-instance; a unique
// cache-buster per load gives each behavioral test a fresh module (data:
// URL imports are cached by exact URL).
let loadCount = 0;
async function loadModule() {
  loadCount += 1;
  const src = `${injected}${withoutImports}\n// cache-bust ${loadCount}`;
  const dataUrl =
    "data:text/javascript;base64," + Buffer.from(src, "utf8").toString("base64");
  return import(dataUrl);
}

// --- DOM stub (mirrors capture_plan_loop_test.mjs) ----------------------------

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    nodeType: 1,
    className: "",
    _attrs: {},
    children: [],
    _listeners: {},
    disabled: false,
    value: "",
    files: [],
    style: { setProperty() {} },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...items) {
      for (const item of items) this.children.push(item);
    },
    replaceChildren(...items) {
      this.children = items;
    },
    setAttribute(k, v) {
      this._attrs[String(k)] = String(v);
    },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(this._attrs, k)
        ? this._attrs[k]
        : null;
    },
    addEventListener(ev, fn) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
    },
  };
  let text = "";
  Object.defineProperty(node, "textContent", {
    get() {
      return text;
    },
    set(v) {
      text = String(v);
      node.children.length = 0;
    },
  });
  return node;
}

function makeScreenEl() {
  return {
    children: [],
    replaceChildren(...items) {
      this.children = items;
    },
  };
}

function headingText(screenEl) {
  const heading = screenEl.children.find((c) => c.tagName === "H1");
  return heading ? heading.textContent : "";
}

function findButtons(node, out = []) {
  for (const child of node.children || []) {
    if (child.tagName === "BUTTON") out.push(child);
    findButtons(child, out);
  }
  return out;
}

function buttonLabeled(screenEl, label) {
  return findButtons({ children: screenEl.children }).find(
    (b) => b.textContent === label,
  ) || null;
}

const statusHistory = [];
function makeStatusEl() {
  const el = { dataset: {} };
  let text = "";
  Object.defineProperty(el, "textContent", {
    get() {
      return text;
    },
    set(v) {
      text = String(v);
      statusHistory.push(v);
    },
  });
  return el;
}

function installDom() {
  const statusEl = makeStatusEl();
  globalThis.document = {
    createElement: (tag) => makeNode(tag),
    getElementById: () => statusEl,
  };
  return statusEl;
}

const HINT = Object.freeze({
  mode: "serial",
  model: "minidsp_umik2",
  serial_display: "8494",
  calibration_id: "vendor-minidsp_umik2-abc123",
  resolvable: true,
});

function specWithHint(hintOverrides = {}, specOverrides = {}) {
  const hint = { ...HINT, ...hintOverrides };
  for (const [key, value] of Object.entries(hintOverrides)) {
    if (value === undefined) delete hint[key];
  }
  return {
    kind: "room_sweep",
    sample_rate_hz: 48000,
    setup_validation: true,
    calibration_models: [
      { key: "minidsp_umik2", label: "miniDSP UMIK-2", aliases: [] },
    ],
    default_setup: { calibration: hint },
    ...specOverrides,
  };
}

// A relay client whose phone-status echoes the last posted setup_token with
// the scripted validation outcome — the same shape the Pi's
// setup_validated/setup_validation_failed host events take.
function makeSetupClient({ outcome = "setup_validated", error = "" } = {}) {
  const posted = [];
  return {
    posted,
    async postEvent(event) {
      posted.push(event);
      return { ok: true };
    },
    async fetchPhoneStatus() {
      const last = posted[posted.length - 1] || {};
      return {
        host_event: {
          phase: outcome,
          setup_token: last.setup_token,
          ...(error ? { error } : {}),
        },
      };
    },
  };
}

let passed = 0;
function ok() {
  passed += 1;
}

// ---- validDefaultSetupHint (pure) --------------------------------------------

async function testResolvableSerialHintIsAccepted() {
  const { validDefaultSetupHint } = await loadModule();
  const hint = validDefaultSetupHint(specWithHint());
  assert.ok(hint);
  assert.equal(hint.mode, "serial");
  assert.equal(hint.model, "minidsp_umik2");
  ok();
}

async function testResolvableUploadHintIsAccepted() {
  const { validDefaultSetupHint } = await loadModule();
  const spec = specWithHint({ mode: "upload", serial_display: "" });
  assert.ok(validDefaultSetupHint(spec));
  ok();
}

async function testHintWithoutResolvableIsRejected() {
  const { validDefaultSetupHint } = await loadModule();
  // An older Pi build never mints `resolvable` — the hint is unusable for a
  // stored submit, so it must not be offered (compat pin, coordinator item 3).
  assert.equal(validDefaultSetupHint(specWithHint({ resolvable: undefined })), null);
  assert.equal(validDefaultSetupHint(specWithHint({ resolvable: false })), null);
  assert.equal(validDefaultSetupHint(specWithHint({ resolvable: "true" })), null);
  ok();
}

async function testMissingDefaultSetupIsNull() {
  const { validDefaultSetupHint } = await loadModule();
  assert.equal(validDefaultSetupHint({}), null);
  assert.equal(validDefaultSetupHint({ default_setup: {} }), null);
  assert.equal(validDefaultSetupHint(null), null);
  ok();
}

async function testUnknownModeOrMissingCalibrationIdIsRejected() {
  const { validDefaultSetupHint } = await loadModule();
  assert.equal(validDefaultSetupHint(specWithHint({ mode: "none" })), null);
  assert.equal(validDefaultSetupHint(specWithHint({ calibration_id: "" })), null);
  ok();
}

// ---- calibrationModelLabel (pure) --------------------------------------------

async function testModelLabelResolutionAndFallbacks() {
  const { calibrationModelLabel } = await loadModule();
  const spec = specWithHint();
  assert.equal(calibrationModelLabel(spec, "minidsp_umik2"), "miniDSP UMIK-2");
  assert.equal(calibrationModelLabel({ calibration_models: [] }, "unknown_key"), "unknown_key");
  assert.equal(calibrationModelLabel({}, ""), "microphone");
  ok();
}

// ---- behavioral: the adjudicated stored-submit contract ----------------------

// 1. Confirm submits setup.calibration = {mode: "stored", calibration_id,
//    model} on the ordinary setup_validate event — the exact payload the
//    Pi's mode="stored" branch resolves via the household-mic record.
async function testConfirmSubmitsStoredPayloadShape() {
  statusHistory.length = 0;
  const { renderCalibration } = await loadModule();
  installDom();
  const client = makeSetupClient();
  const screenEl = makeScreenEl();
  const ctx = { spec: specWithHint(), client, screenEl };

  renderCalibration(screenEl, ctx);
  assert.equal(headingText(screenEl), "Using miniDSP UMIK-2 · 8494");

  const confirm = buttonLabeled(screenEl, "One tap to confirm");
  assert.ok(confirm, "the confirm screen offers the one-tap primary action");
  await confirm._listeners.click[0]();

  assert.equal(client.posted.length, 1, "confirm submits exactly one setup validation");
  const event = client.posted[0];
  assert.equal(event.setup_validate, true);
  assert.ok(event.setup_token, "the validation is token-scoped like every setup submit");
  assert.deepEqual(event.setup.calibration, {
    mode: "stored",
    calibration_id: "vendor-minidsp_umik2-abc123",
    model: "minidsp_umik2",
  });
  // The flow advanced past calibration — the validated stored setup lands
  // on the position-count screen exactly like a picker-validated one.
  assert.equal(headingText(screenEl), "Listening positions");
  ok();
}

// 2. resolvable absent → today's full picker renders directly (old-Pi compat
//    pin): no confirm screen, no stored submit possible.
async function testHintWithoutResolvableRendersTodaysPicker() {
  statusHistory.length = 0;
  const { renderCalibration } = await loadModule();
  installDom();
  const client = makeSetupClient();
  const screenEl = makeScreenEl();
  const ctx = {
    spec: specWithHint({ resolvable: undefined }),
    client,
    screenEl,
  };

  renderCalibration(screenEl, ctx);

  assert.equal(headingText(screenEl), "Calibration");
  assert.equal(buttonLabeled(screenEl, "One tap to confirm"), null);
  assert.equal(client.posted.length, 0, "nothing is submitted by merely rendering");
  ok();
}

// 3. The Pi rejects the stored resolution (record went stale between spec
//    mint and submit) → fall back to the full picker with a plain sentence,
//    and never re-offer the failed one-tap on a later calibration render.
async function testStoredRejectionFallsBackToPickerWithPlainSentence() {
  statusHistory.length = 0;
  const { renderCalibration } = await loadModule();
  installDom();
  const client = makeSetupClient({
    outcome: "setup_validation_failed",
    error: "that stored calibration is no longer available",
  });
  const screenEl = makeScreenEl();
  const ctx = { spec: specWithHint(), client, screenEl };

  renderCalibration(screenEl, ctx);
  const confirm = buttonLabeled(screenEl, "One tap to confirm");
  await confirm._listeners.click[0]();

  assert.equal(headingText(screenEl), "Calibration", "falls back to the full picker");
  assert.equal(
    statusHistory[statusHistory.length - 1],
    "The speaker couldn't use the saved microphone calibration. Set up the microphone manually instead.",
  );

  // A later un-flagged render (e.g. Back navigation) must NOT re-offer the
  // guaranteed-to-fail one-tap.
  renderCalibration(screenEl, ctx);
  assert.equal(headingText(screenEl), "Calibration");
  assert.equal(buttonLabeled(screenEl, "One tap to confirm"), null);
  ok();
}

// 3b. "Use a different microphone" still routes to the full picker without
//     posting anything.
async function testUseDifferentMicrophoneFallsBackWithoutSubmit() {
  statusHistory.length = 0;
  const { renderCalibration } = await loadModule();
  installDom();
  const client = makeSetupClient();
  const screenEl = makeScreenEl();
  const ctx = { spec: specWithHint(), client, screenEl };

  renderCalibration(screenEl, ctx);
  const different = buttonLabeled(screenEl, "Use a different microphone");
  assert.ok(different);
  different._listeners.click[0]();

  assert.equal(headingText(screenEl), "Calibration");
  assert.equal(client.posted.length, 0);
  ok();
}

const tests = [
  testResolvableSerialHintIsAccepted,
  testResolvableUploadHintIsAccepted,
  testHintWithoutResolvableIsRejected,
  testMissingDefaultSetupIsNull,
  testUnknownModeOrMissingCalibrationIdIsRejected,
  testModelLabelResolutionAndFallbacks,
  testConfirmSubmitsStoredPayloadShape,
  testHintWithoutResolvableRendersTodaysPicker,
  testStoredRejectionFallsBackToPickerWithPlainSentence,
  testUseDifferentMicrophoneFallsBackWithoutSubmit,
];

let failure = null;
for (const t of tests) {
  try {
    await t();
  } catch (e) {
    failure = { test: t.name, error: String(e && e.stack ? e.stack : e) };
    break;
  }
}

if (failure) {
  console.error(failure.error);
  console.log(JSON.stringify({ ok: false, ...failure }));
  process.exit(1);
} else {
  console.log(JSON.stringify({ ok: true, passed }));
}
