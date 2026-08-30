// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Behavioral harness for three run-19 field-telemetry defects fixed in
// capture-page/js/main.js:
//
//   (a) mic-picker persistence and repeated boot cleanup: exercised through
//       the real boot boundary in capture_boot_contract_test.mjs.
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
import { loadCapturePage } from "./_capture_page_module.mjs";
import { runTestFunctions } from "./run_test_functions.mjs";

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
const setText = (node, text) => { node.textContent = typeof text === "string" ? text : ""; };
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

function loadModule() {
  return loadCapturePage({ dependencySource: injected });
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
async function testAbortErrorDuringSweepGetsFriendlyCopy(
  makeError = () => new DOMException("signal is aborted without reason.", "AbortError"),
) {
  statusHistory.length = 0;
  const { onStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };
  const screenEl = makeScreenEl();

  const client = {
    async postEvent() {},
    async fetchPhoneStatus() {
      throw makeError();
    },
  };

  await onStart({
    spec: {
      kind: "crossover_sweep",
      sample_rate_hz: 48000,
      // #1824 D1: a single aborted poll is now absorbed and the RECORDING
      // WINDOW is the bound, so this scenario (every poll aborts, forever)
      // resolves when the window runs out. Pin the window at its floor so the
      // harness spends 5 s here rather than the 20 s default — the assertion
      // is about the copy an unrecoverable outage produces, not the length of
      // the wait.
      duration_ms: 5000,
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
  // …and the household is told what is happening WHILE it happens, not only
  // at the end (#1824 D1): the page keeps polling behind a "still measuring"
  // line instead of ending the capture on the first slow request.
  assert.ok(
    statusHistory.some((line) => line.includes("reconnecting")),
    "a connectivity blip renders the reconnecting line while it retries",
  );
  assert.ok(
    !statusHistory.some((line) => line.includes("signal is aborted")),
    "the raw DOMException text never reaches the household",
  );
  ok();
}

// …and the shape PRODUCTION actually raises (#1824 B1). The run-19 fix aborts
// with a named reason, so fetch rejects with THAT — an ordinary Error, name
// "Error", no "signal is aborted" text — and the classifier's two original
// tests both stopped matching. The DOMException case above kept passing the
// whole time, which is exactly why it never surfaced. Derive the fixture from
// the REAL client so it cannot drift back into a fiction.
async function testTheProductionTimeoutShapeAlsoGetsFriendlyCopy() {
  const { RelayClient } = await import("../../capture-page/js/relay-client.js");
  const client = new RelayClient({
    baseUrl: "https://relay.test",
    sessionId: "cap_harness",
    uploadToken: "tok",
    fetchImpl: (_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener(
        "abort", () => reject(init.signal.reason), { once: true },
      );
    }),
  });
  let production = null;
  try {
    await client.fetchPhoneStatus({ timeoutMs: 1 });
  } catch (err) {
    production = err;
  }
  assert.ok(production, "the real client produced a timeout");
  assert.notEqual(production.name, "AbortError", "fixture is the REAL shape");
  await testAbortErrorDuringSweepGetsFriendlyCopy(() => production);
}

const tests = [
  testIsDeadSessionErrorClassifiesRelayStatusCodes,
  testDeadSessionDuringSweepRendersLinkExpiredNotRetry,
  testDeadSessionDuringLevelRampRendersLinkExpired,
  testAbortErrorDuringSweepGetsFriendlyCopy,
  testTheProductionTimeoutShapeAlsoGetsFriendlyCopy,
];

await runTestFunctions(tests, () => passed);
