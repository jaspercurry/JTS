// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Behavioral harness for three run-19 field-telemetry defects fixed in
// capture-page/js/main.js:
//
//   (a) mic-picker persistence: covered by string assertions in
//       tests/test_capture_page_js.py (renderMicChoice/buildMicPicker are
//       not exported; the fix is "this exact destructive write no longer
//       exists", which a string test pins directly and precisely).
//   (b) the raw "signal is aborted without reason." AbortSignal leak ->
//       friendly copy (isRelayConnectivityAbort, exercised here through
//       onStart's status history).
//   (c) a dead relay session no longer offers "Tap Start to try again" — it
//       renders the terminal renderSessionExpired() screen instead
//       (isDeadSessionError, exercised here through onStart AND
//       onLevelRampStart's catch blocks).
//
// Mirrors capture_host_stop_lifecycle_test.mjs's import-stripping harness.

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

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    nodeType: 1,
    className: "",
    _attrs: {},
    children: [],
    _listeners: {},
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

function makeRecorder() {
  return {
    capturedChannelCount: 1,
    stream: {
      getAudioTracks() {
        return [{
          label: "Test microphone",
          getSettings() {
            return {
              autoGainControl: false,
              channelCount: 1,
              echoCancellation: false,
              noiseSuppression: false,
              sampleRate: 48000,
            };
          },
        }];
      },
    },
    start() {},
    async stop() {
      return new Float32Array([0]);
    },
    async close() {},
  };
}

const injected = `
const acceptedAcknowledgement = () => null;
const createMonoRecorder = async () => globalThis.__recorder;
const delayMs = async () => {};
const safeReturnUrl = (spec) => {
  const raw = spec && typeof spec.return_url === "string" ? spec.return_url.trim() : "";
  if (!raw) return "";
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
};
const rmsToDbfs = (rms) => Number(rms) > 0 ? 20 * Math.log10(Number(rms)) : -120;
const verifyRealizedConstraints = (settings, spec, capturedChannelCount) => ({
  settings,
  sourceChannelCount: settings.channelCount || null,
  capturedChannelCount,
  dirtyFlags: [],
  sampleRateOk: true,
  channelsOk: true,
  clean: true,
});
const constraintDecision = () => ({ action: "proceed", degraded: false, reason: "" });
const acquireWakeLock = async () => ({ release: async () => {} });
const watchVisibilityAbort = () => () => {};
const runLevelRampProtocol = async (opts) => {
  if (opts.isAborted()) return { state: "aborted", terminal: true };
  throw globalThis.__rampError;
};
const buildAmbientStatsEvent = () => ({});
`;

async function loadModule() {
  const dataUrl =
    "data:text/javascript;base64," +
    Buffer.from(injected + withoutImports, "utf8").toString("base64");
  return import(dataUrl);
}

let passed = 0;
function ok() {
  passed += 1;
}

// ============================================================================
// isDeadSessionError — direct unit coverage of the predicate itself.
// ============================================================================
async function testIsDeadSessionErrorClassifiesRelayStatusCodes() {
  const { isDeadSessionError } = await loadModule();
  for (const status of [401, 403, 404]) {
    assert.equal(isDeadSessionError({ status, message: "unauthorized" }), true, `status ${status}`);
  }
  assert.equal(isDeadSessionError({ status: 500, message: "server error" }), false);
  assert.equal(isDeadSessionError({ message: "not_found" }), true);
  assert.equal(isDeadSessionError({ message: "some other failure" }), false);
  assert.equal(isDeadSessionError(new Error("recording timed out")), false);
  ok();
}

// ============================================================================
// (c) A dead session mid-capture renders renderSessionExpired(), not the
// generic "Tap Start to try again" copy — onStart's flow.
// ============================================================================
async function testDeadSessionDuringSweepRendersLinkExpiredNotRetry() {
  statusHistory.length = 0;
  const { onStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };
  const screenEl = makeScreenEl();

  const client = {
    async postEvent() {},
    async fetchPhoneStatus() {
      const err = new Error("not_found");
      err.status = 404;
      throw err;
    },
  };

  await onStart({
    spec: {
      kind: "crossover_sweep",
      sample_rate_hz: 48000,
      constraints: {},
      validity: { clean_capture: "refuse" },
      run_token: "run-test",
      return_url: "https://jts.local/correction/crossover/",
    },
    contentKeyB64: "unused",
    captureRefs: {},
    screenEl,
    client,
  });

  assert.equal(headingText(screenEl), "Link expired");
  assert.ok(
    statusHistory.includes(
      "This measurement link expired — return to the speaker page to start again.",
    ),
  );
  assert.ok(
    !statusHistory.some((line) => line.includes("Tap Start to try again")),
    "a dead session never offers the guaranteed-to-fail retry copy",
  );
  ok();
}

// ============================================================================
// (c) Same fix on the level-ramp leg — onLevelRampStart's catch block.
// ============================================================================
async function testDeadSessionDuringLevelRampRendersLinkExpired() {
  statusHistory.length = 0;
  const { onLevelRampStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };
  const screenEl = makeScreenEl();

  const err = new Error("not_found");
  err.status = 404;
  globalThis.__rampError = err;

  await onLevelRampStart({
    spec: { kind: "level_ramp", sample_rate_hz: 48000, run_token: "ramp-test" },
    captureRefs: {},
    screenEl,
    client: { async postEvent() {}, async fetchPhoneStatus() { return { host_event: {} }; } },
  });

  assert.equal(headingText(screenEl), "Link expired");
  ok();
}

// ============================================================================
// (b) A relay control-fetch AbortError never leaks
// "signal is aborted without reason." to the household.
// ============================================================================
async function testAbortErrorDuringSweepGetsFriendlyCopy() {
  statusHistory.length = 0;
  const { onStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };
  const screenEl = makeScreenEl();

  const client = {
    async postEvent() {},
    async fetchPhoneStatus() {
      const err = new DOMException("signal is aborted without reason.", "AbortError");
      throw err;
    },
  };

  await onStart({
    spec: {
      kind: "crossover_sweep",
      sample_rate_hz: 48000,
      constraints: {},
      validity: { clean_capture: "refuse" },
      run_token: "run-test",
    },
    contentKeyB64: "unused",
    captureRefs: {},
    screenEl,
    client,
  });

  assert.ok(
    statusHistory.some((line) => line.includes("Lost the connection")),
    `expected friendly connectivity copy, got: ${JSON.stringify(statusHistory)}`,
  );
  assert.ok(
    !statusHistory.some((line) => line.includes("signal is aborted")),
    "the raw DOMException text never reaches the household",
  );
  ok();
}

const tests = [
  testIsDeadSessionErrorClassifiesRelayStatusCodes,
  testDeadSessionDuringSweepRendersLinkExpiredNotRetry,
  testDeadSessionDuringLevelRampRendersLinkExpired,
  testAbortErrorDuringSweepGetsFriendlyCopy,
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
