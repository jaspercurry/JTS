// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Behavioral harness for two 2026-07-16 fixes on the browser orchestration
// path (capture-page/js/main.js):
//
//   1. The pre-tone quiet phase's countdown — the `ambient_started` host
//      event now carries `duration_s` (see
//      jasper.web.correction_crossover_flow.run_crossover_relay_transport's
//      `post_phase("ambient_started", duration_s=...)`), and the page renders
//      a live "the tone starts in about N seconds" countdown from it instead
//      of a fixed, unexplained-silence status line.
//   2. A phone-tappable Stop — `stopCapture()` (wired to the shared "stop"
//      button action) calls whichever capture leg's own `abort(reason)` is
//      currently live, landing on the SAME terminal "Measurement stopped."
//      screen for both the crossover_sweep leg (onStart) and the level_ramp
//      leg (onLevelRampStart).
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

// --- Minimal-but-faithful-enough document stub -------------------------------
// Real element tree: setting textContent stores a string (never parses
// markup), attributes land in a plain map readable via getAttribute.

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
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
    dispatch(ev) {
      for (const fn of this._listeners[ev] || []) fn({ preventDefault() {} });
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

function backLink(screenEl) {
  return screenEl.children.find((c) => c.tagName === "A") || null;
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
  return { state: "locked", terminal: true };
};
const buildAmbientStatsEvent = () => ({ ambient_stats: { schema: 1, run_token: "", duration_s: 0, clipped: false, bands: [] } });
`;

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
// 1. Ambient countdown: renders a live countdown from the host event's
//    duration_s, then switches to the tone-phase copy on transition — and
//    does not re-render identical text on every poll (dedup).
// ============================================================================
async function testAmbientCountdownRendersFromHostEventDuration() {
  statusHistory.length = 0;
  const { onStart } = await loadModule();

  globalThis.__recorder = makeRecorder();
  globalThis.document = {
    createElement: (tag) => makeNode(tag),
    getElementById: () => statusEl,
  };
  const statusEl = makeStatusEl();

  // Two identical `ambient_started` polls (proves the loop does not spam an
  // unchanged countdown string), then the tone starts, then the Pi ends the
  // capture — sweep_cancelled keeps this test clear of the crypto/upload leg
  // (mirrors capture_host_stop_lifecycle_test.mjs's use of the same phase).
  const phases = [
    { phase: "ambient_started", duration_s: 5 },
    { phase: "ambient_started", duration_s: 5 },
    { phase: "sweep_started" },
    { phase: "sweep_cancelled" },
  ];
  let call = 0;
  const client = {
    async postEvent() {},
    async fetchPhoneStatus() {
      const host_event = phases[Math.min(call, phases.length - 1)];
      call += 1;
      return { host_event };
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
    screenEl: makeScreenEl(),
    client,
  });

  const countdown = "Listening to the room… the tone starts in about 5 seconds";
  const countdownHits = statusHistory.filter((line) => line === countdown);
  assert.equal(
    countdownHits.length,
    1,
    "the countdown renders once, not once per poll of an unchanged remaining value",
  );
  assert.ok(
    statusHistory.includes("Playing the measurement tone…"),
    "the tone-phase transition gets its own copy",
  );
  assert.ok(
    !statusHistory.includes(
      "Measuring room noise — stay quiet and keep the phone still.",
    ),
    "a Pi that supplies duration_s never falls back to the static copy",
  );
  ok();
}

// ============================================================================
// 2. Fallback: an older Pi that omits duration_s degrades to the original
//    static copy — no countdown, no crash.
// ============================================================================
async function testAmbientPhaseWithoutDurationFallsBackToStaticCopy() {
  statusHistory.length = 0;
  const { onStart } = await loadModule();

  globalThis.__recorder = makeRecorder();
  globalThis.document = {
    createElement: (tag) => makeNode(tag),
    getElementById: () => statusEl,
  };
  const statusEl = makeStatusEl();

  const phases = [{ phase: "ambient_started" }, { phase: "sweep_cancelled" }];
  let call = 0;
  const client = {
    async postEvent() {},
    async fetchPhoneStatus() {
      const host_event = phases[Math.min(call, phases.length - 1)];
      call += 1;
      return { host_event };
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
    screenEl: makeScreenEl(),
    client,
  });

  assert.ok(
    statusHistory.includes(
      "Measuring room noise — stay quiet and keep the phone still.",
    ),
    "missing duration_s degrades to the original static copy",
  );
  assert.ok(
    !statusHistory.some((line) => line.includes("the tone starts in about")),
    "no countdown is rendered without a real duration",
  );
  ok();
}

// ============================================================================
// 3. Stop mid-sweep-capture: posts the SAME aborted/abort_reason envelope the
//    visibility-abort path already posts, and renders the terminal
//    "Measurement stopped." screen with the Back-to-speaker link.
// ============================================================================
async function testStopDuringSweepCapturePostsAbortAndRendersStoppedScreen() {
  const { onStart, stopCapture } = await loadModule();

  globalThis.__recorder = makeRecorder();
  globalThis.document = {
    createElement: (tag) => makeNode(tag),
    getElementById: () => statusEl,
  };
  const statusEl = makeStatusEl();

  const posted = [];
  const client = {
    async postEvent(event) {
      posted.push(event);
    },
    async fetchPhoneStatus() {
      return { host_event: {} };
    },
  };
  const screenEl = makeScreenEl();

  const p = onStart({
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
  // onStart runs synchronously up to its first await, so by the time this
  // line executes the shared abort() closure is already live — the SAME
  // reuse the visibility-abort path exercises, just triggered by a tap
  // instead of a `visibilitychange` event. Await BOTH promises: onStart's own
  // completion races independently of abort()'s side effects (the relay
  // post, the recorder close) settling, exactly like the real click handler,
  // which does not await stopCapture()'s return value either.
  const stopped = stopCapture();
  await Promise.all([p, stopped]);

  assert.deepEqual(
    posted.filter((e) => e.aborted).map((e) => e.abort_reason),
    ["stopped"],
    "Stop posts the same aborted/abort_reason envelope the visibility-abort path posts",
  );
  assert.equal(headingText(screenEl), "Measurement stopped.");
  const link = backLink(screenEl);
  assert.ok(link, "the terminal screen keeps the existing Back-to-speaker link");
  assert.equal(link.textContent, "Back to speaker");
  assert.equal(link.getAttribute("href"), "https://jts.local/correction/crossover/");
  ok();
}

// ============================================================================
// 4. Stop before/during the level-ramp leg reaches the SAME terminal screen
//    through onLevelRampStart's abort() closure.
// ============================================================================
async function testStopDuringLevelRampRendersStoppedScreen() {
  const { onLevelRampStart, stopCapture } = await loadModule();

  globalThis.__recorder = makeRecorder();
  globalThis.document = {
    createElement: (tag) => makeNode(tag),
    getElementById: () => statusEl,
  };
  const statusEl = makeStatusEl();

  const client = {
    async postEvent() {},
    async fetchPhoneStatus() {
      return { host_event: {} };
    },
  };
  const screenEl = makeScreenEl();
  const beginButton = makeNode("button");
  beginButton.disabled = false;
  const stopButton = makeNode("button");
  stopButton.disabled = false;
  const stopDisabledHistory = [];
  Object.defineProperty(stopButton, "disabled", {
    get() {
      return false;
    },
    set(v) {
      stopDisabledHistory.push(Boolean(v));
    },
  });
  const captureRefs = {
    buttons: [
      { action: "begin_capture", el: beginButton },
      { action: "stop", el: stopButton },
    ],
  };

  const p = onLevelRampStart({
    spec: {
      kind: "level_ramp",
      sample_rate_hz: 48000,
      run_token: "ramp-test",
    },
    captureRefs,
    screenEl,
    client,
  });
  const stopped = stopCapture();
  await Promise.all([p, stopped]);

  assert.equal(headingText(screenEl), "Measurement stopped.");
  assert.equal(
    beginButton.disabled,
    false,
    "setCaptureButtonsDisabled(ctx, false) in the finally block re-enables the other actions",
  );
  assert.deepEqual(
    stopDisabledHistory,
    [],
    "setCaptureButtonsDisabled never writes .disabled on the stop action, in either direction",
  );
  ok();
}

const tests = [
  testAmbientCountdownRendersFromHostEventDuration,
  testAmbientPhaseWithoutDurationFallsBackToStaticCopy,
  testStopDuringSweepCapturePostsAbortAndRendersStoppedScreen,
  testStopDuringLevelRampRendersStoppedScreen,
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
