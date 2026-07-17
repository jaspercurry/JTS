// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Behavioral harness for the session-spanning capture-plan loop (protocol
// v3, SPEC W2.3 — capture-page/js/main.js's onPlanStart/runPlanCapture and
// friends). Drives onPlanStart directly (mirrors
// capture_host_stop_lifecycle_test.mjs / capture_stop_and_ambient_countdown_
// test.mjs's approach of calling the orchestration function without a full
// boot()), against a small scripted fake relay that reacts to begin_capture
// / armed / blob-PUT exactly like jasper/capture_relay/session.py's
// run_capture_plan does (mirrors tests/test_capture_relay_plan.py's
// PhonePlanDriver, from the Pi side instead of the phone side).
//
// Covers: full 3-of-3 accepted round trip; a capture_result rejection ->
// "Try again" -> eventual acceptance; a capture_refused terminal (no retry
// offered); capture_set_exhausted; Stop mid-round.

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

// --- Minimal-but-faithful-enough document stub (mirrors
// capture_stop_and_ambient_countdown_test.mjs) --------------------------------

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    // el()'s generic children-append path checks `child.nodeType` to decide
    // between appending a real node vs. wrapping a string in a text node
    // (document.createTextNode) — mark these stub nodes as real elements
    // (DOM's Node.ELEMENT_NODE = 1) so button()/el(tag, attrs, [child, ...])
    // append child NODES directly instead of falling through to the
    // (unstubbed) createTextNode path.
    nodeType: 1,
    className: "",
    _attrs: {},
    children: [],
    _listeners: {},
    disabled: false,
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

function noteText(screenEl) {
  const note = screenEl.children.find((c) => c.tagName === "P");
  return note ? note.textContent : "";
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
      return new Float32Array(4800); // 100ms of silence @ 48kHz
    },
    async close() {},
  };
}

const injected = `
const acceptedAcknowledgement = (spec, refs) => (
  spec && spec.acknowledgement
    ? { schema_version: 1, id: spec.acknowledgement.id, binding_id: spec.acknowledgement.binding_id, accepted: true }
    : null
);
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
const buildAmbientStatsEvent = (samples, sampleRate, runToken, durationS) => ({
  ambient_stats: { schema: 1, run_token: String(runToken || ""), duration_s: durationS, clipped: false, bands: [] },
});
const importContentKey = async (b64) => ({ b64 });
const encryptWav = async (key, wavBytes) => ({
  blob: new Uint8Array([1, 2, 3, 4]),
  plaintextLen: wavBytes.length,
  sha256: "a".repeat(64),
});
const float32ToWavBlob = () => ({ async arrayBuffer() { return new Uint8Array([9, 9, 9]).buffer; } });
const withinUploadCap = () => true;
`;

async function loadModule() {
  const dataUrl =
    "data:text/javascript;base64," +
    Buffer.from(injected + withoutImports, "utf8").toString("base64");
  return import(dataUrl);
}

// --- Fake relay: reacts to begin_capture/armed/putBlob like
// jasper/capture_relay/session.py's run_capture_plan (Pi side), scripted
// per-attempt for the scenario under test. --------------------------------

function makeFakePlanClient({ target, maxAttempts, resultFor = () => ({ accepted: true }), refuseAttempt = null }) {
  const posted = [];
  const blobPuts = [];
  let acceptedCount = 0;
  let last = {};
  let pendingResult = null;
  const client = {
    async postEvent(event) {
      posted.push(event);
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        if (refuseAttempt && attempt === refuseAttempt) {
          last = {
            phase: "capture_refused",
            code: "budget_exceeded",
            error: "The household's driver repeat budget is exhausted.",
            index,
            attempt,
          };
        } else {
          last = { phase: "capture_authorized", index, attempt };
        }
      } else if (event.armed) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "sweep_complete" };
        pendingResult = { index, attempt, verdict: resultFor(index, attempt) };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: last };
    },
    async putBlob(blob, plaintextLen, sha256, captureIndex) {
      blobPuts.push({ length: blob.length, plaintextLen, sha256, captureIndex });
      if (pendingResult) {
        const { index, attempt, verdict } = pendingResult;
        pendingResult = null;
        if (verdict.accepted) acceptedCount += 1;
        const resultEvent = {
          phase: "capture_result",
          index,
          attempt,
          accepted: verdict.accepted,
          error: verdict.error,
        };
        if (verdict.accepted && acceptedCount >= target) {
          last = resultEvent;
          // The Pi posts capture_result THEN capture_set_complete in
          // immediate succession; the phone's next poll sees whichever
          // landed last on the last-write-wins slot — mirror that by
          // advancing straight to the terminal on the FOLLOWING poll.
          queueMicrotask(() => {
            last = { phase: "capture_set_complete", accepted: acceptedCount, capture_target: target };
          });
        } else if (!verdict.accepted && attempt >= maxAttempts) {
          last = resultEvent;
          queueMicrotask(() => {
            last = {
              phase: "capture_set_exhausted",
              accepted: acceptedCount,
              capture_target: target,
              attempts: attempt,
            };
          });
        } else {
          last = resultEvent;
        }
      }
      return { ok: true, capture_index: captureIndex };
    },
  };
  return { client, posted, blobPuts, acceptedCount: () => acceptedCount };
}

function planSpec({ target = 3, maxAttempts = 4 } = {}) {
  return {
    kind: "crossover_sweep",
    sample_rate_hz: 48000,
    duration_ms: 20000,
    post_roll_ms: 0,
    constraints: {},
    validity: { clean_capture: "refuse" },
    run_token: "run-test",
    return_url: "https://jts.local/correction/crossover/",
    acknowledgement: {
      schema_version: 1,
      id: "placement_woofer",
      binding_id: "placement_abcdefghijklmnopqrstuv",
      label: "The mic is fixed on-axis — measure Woofer driver",
    },
    capture_plan: { schema_version: 1, capture_target: target, max_attempts: maxAttempts },
    capture_protocol_version: 3,
  };
}

function makeCtx(spec, client) {
  return {
    spec,
    client,
    contentKeyB64: "unused",
    screenEl: makeScreenEl(),
    captureRefs: {},
  };
}

let passed = 0;
function ok() {
  passed += 1;
}

// ============================================================================
// 1. Full 3-of-3 accepted round trip.
// ============================================================================
async function testFullAcceptedRoundTripEndsAllDone() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 3, maxAttempts: 4 });
  const { client, posted, blobPuts } = makeFakePlanClient({ target: 3, maxAttempts: 4 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  // Round 1 landed on "Measurement 1 of 3 ✓" — tap Next.
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 3 ✓");
  let next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();

  assert.equal(headingText(ctx.screenEl), "Measurement 2 of 3 ✓");
  next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();

  // Third (final) capture completes the set directly.
  assert.equal(headingText(ctx.screenEl), "All measurements done");
  assert.equal(
    noteText(ctx.screenEl),
    "All measurements done — the speaker continues automatically.",
  );
  const link = backLink(ctx.screenEl);
  assert.ok(link, "the terminal screen offers Back to speaker");
  assert.equal(link.getAttribute("href"), "https://jts.local/correction/crossover/");

  const beginEvents = posted.filter((e) => e.begin_capture && !e.armed);
  assert.deepEqual(
    beginEvents.map((e) => [e.begin_capture.index, e.begin_capture.attempt]),
    [[1, 1], [2, 2], [3, 3]],
  );
  const armedEvents = posted.filter((e) => e.armed);
  assert.deepEqual(
    armedEvents.map((e) => [e.begin_capture.index, e.begin_capture.attempt, e.acknowledgement.accepted]),
    [[1, 1, true], [2, 2, true], [3, 3, true]],
  );
  assert.deepEqual(blobPuts.map((b) => b.captureIndex), [0, 1, 2]);
  ok();
}

// ============================================================================
// 2. A capture_result rejection renders "Try again" (SAME slot, next
//    attempt); retrying succeeds and the set eventually completes.
// ============================================================================
async function testRejectedResultOffersTryAgainSameSlot() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 4 });
  const { client, posted, blobPuts } = makeFakePlanClient({
    target: 2,
    maxAttempts: 4,
    resultFor: (index, attempt) => (attempt === 1 ? { accepted: false, error: "SNR too low." } : { accepted: true }),
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 2 needs another try");
  assert.equal(noteText(ctx.screenEl), "SNR too low.");
  let retry = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await retry._listeners.click[0]();

  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 2 ✓");
  let next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();

  assert.equal(headingText(ctx.screenEl), "All measurements done");

  const beginEvents = posted.filter((e) => e.begin_capture && !e.armed);
  assert.deepEqual(
    beginEvents.map((e) => [e.begin_capture.index, e.begin_capture.attempt]),
    [[1, 1], [1, 2], [2, 3]],
    "the retry re-uses index 1 with a fresh attempt number",
  );
  assert.deepEqual(blobPuts.map((b) => b.captureIndex), [0, 1, 2]);
  ok();
}

// ============================================================================
// 2b. A timed-out result poll renders a TERMINAL screen (renderSweepFailed's
// shape), never leaving a stale "Next measurement"/"Try again" button whose
// closure references an (index, attempt) pair the Pi may have already moved
// past — a retry there risks a fatal begin_replayed refusal. Simulates the
// timeout by having the fake status poll throw the same `.sweepFailed`-
// flagged error waitForCaptureResult's real deadline path throws, since
// exercising the actual 30s+ deadline would require real wall-clock time.
// ============================================================================
async function testTimedOutResultPollRendersTerminalNotStaleRetry() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 3, maxAttempts: 4 });
  const posted = [];
  const client = {
    async postEvent(event) {
      posted.push(event);
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        client._last = { phase: "capture_authorized", index, attempt };
      } else if (event.armed) {
        client._last = { phase: "sweep_complete" };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      if (client._postUpload) {
        const failure = new Error(
          "the speaker did not respond with a result for this measurement before the timeout",
        );
        failure.sweepFailed = true;
        throw failure;
      }
      return { host_event: client._last || {} };
    },
    async putBlob() {
      client._postUpload = true;
      return { ok: true };
    },
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Measurement failed");
  assert.ok(
    !ctx.screenEl.children.some((c) => c.tagName === "BUTTON"),
    "a timed-out result poll offers no stale-state retry button",
  );
  const link = backLink(ctx.screenEl);
  assert.ok(link, "still offers Back to speaker");
  ok();
}

// ============================================================================
// 3. A capture_refused admission is terminal — no retry offered.
// ============================================================================
async function testRefusedBeginRendersTerminalWithNoRetry() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 3, maxAttempts: 4 });
  const { client } = makeFakePlanClient({ target: 3, maxAttempts: 4, refuseAttempt: 1 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Measurement refused");
  assert.equal(
    noteText(ctx.screenEl),
    "The household's driver repeat budget is exhausted. The speaker page shows what happens next.",
  );
  assert.ok(
    !ctx.screenEl.children.some((c) => c.tagName === "BUTTON"),
    "a refusal offers no retry/next button — the Pi has stopped polling",
  );
  const link = backLink(ctx.screenEl);
  assert.ok(link, "the refusal terminal still offers Back to speaker");
  ok();
}

// ============================================================================
// 4. capture_set_exhausted renders a distinct "reached the attempt limit"
//    terminal (not the success copy).
// ============================================================================
async function testExhaustedBudgetRendersDistinctTerminal() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 3, maxAttempts: 2 });
  const { client } = makeFakePlanClient({
    target: 3,
    maxAttempts: 2,
    resultFor: () => ({ accepted: false, error: "SNR too low." }),
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  // Attempt 1 rejected -> "Try again" -> attempt 2 rejected -> budget spent.
  let retry = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await retry._listeners.click[0]();

  assert.equal(headingText(ctx.screenEl), "Reached the attempt limit");
  assert.ok(
    noteText(ctx.screenEl).includes("0 of 3 accepted"),
    `expected an accepted/target summary, got: ${noteText(ctx.screenEl)}`,
  );
  assert.notEqual(headingText(ctx.screenEl), "All measurements done");
  ok();
}

// ============================================================================
// 5. Stop mid-round aborts the WHOLE session: posts aborted, renders the
//    shared Stopped screen, and a later "Next measurement" tap never fires
//    (there is no such button on that terminal screen).
// ============================================================================
async function testStopMidRoundAbortsWholeSession() {
  statusHistory.length = 0;
  const { onPlanStart, stopCapture } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 3, maxAttempts: 4 });
  const posted = [];
  const client = {
    async postEvent(event) {
      posted.push(event);
      return { ok: true };
    },
    async fetchPhoneStatus() {
      // Never authorizes — the plan stays parked in "Requesting measurement
      // 1 of 3…" so Stop is exercised mid-round, matching the sweep-capture
      // Stop test's shape in capture_stop_and_ambient_countdown_test.mjs.
      return { host_event: {} };
    },
    async putBlob() {
      throw new Error("must not upload after Stop");
    },
  };
  const ctx = makeCtx(spec, client);

  const p = onPlanStart(ctx);
  // onPlanStart runs synchronously up to its first await (postEvent), so by
  // the time this line executes the plan controller's abort() is live.
  const stopped = stopCapture();
  await Promise.all([p, stopped]);

  assert.equal(headingText(ctx.screenEl), "Measurement stopped.");
  assert.deepEqual(
    posted.filter((e) => e.aborted).map((e) => e.abort_reason),
    ["stopped"],
  );
  ok();
}

const tests = [
  testFullAcceptedRoundTripEndsAllDone,
  testRejectedResultOffersTryAgainSameSlot,
  testTimedOutResultPollRendersTerminalNotStaleRetry,
  testRefusedBeginRendersTerminalWithNoRetry,
  testExhaustedBudgetRendersDistinctTerminal,
  testStopMidRoundAbortsWholeSession,
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
