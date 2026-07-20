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
const createMonoRecorder = async () => {
  if (globalThis.__recorderError) throw globalThis.__recorderError;
  return globalThis.__recorder;
};
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
  // FIDELITY (W6.10 gate blocker): the real relay's host-event slot is
  // last-write-wins and nothing clears it when the phone consumes a verdict;
  // the Pi authorizes asynchronously (~0.75 s poll cadence). So the phone's
  // FIRST status poll after posting a begin reads whatever is STILL in the
  // slot — after a rejected attempt, that is the stale rejected
  // capture_result. The original fake overwrote the slot synchronously inside
  // postEvent, which masked a phone-side bug that matched the stale verdict
  // and killed every first retry. Stage the admission verdict behind one
  // fetchPhoneStatus poll instead, so the retry path exercises the
  // real-world ordering: stale slot first, verdict on the next poll.
  let queuedAdmission = null;
  let pendingResult = null;
  const client = {
    async postEvent(event) {
      posted.push(event);
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        if (refuseAttempt && attempt === refuseAttempt) {
          queuedAdmission = {
            phase: "capture_refused",
            code: "budget_exceeded",
            error: "The household's driver repeat budget is exhausted.",
            index,
            attempt,
          };
        } else {
          queuedAdmission = { phase: "capture_authorized", index, attempt };
        }
      } else if (event.armed) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "sweep_complete" };
        pendingResult = { index, attempt, verdict: resultFor(index, attempt) };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      const status = { host_event: last };
      if (queuedAdmission) {
        // Promote the staged admission AFTER serving this poll — the caller
        // sees the stale slot once, the verdict on its next poll.
        last = queuedAdmission;
        queuedAdmission = null;
      }
      return status;
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

function planSpec({ target = 3, maxAttempts = 4, entries = null } = {}) {
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
    capture_plan: {
      // §5.7: a plan with `entries` is schema_version 2; the v1 plans every
      // other test in this file uses stay schema_version 1 (dormant, no
      // entries) — unchanged.
      schema_version: entries ? 2 : 1,
      capture_target: target,
      max_attempts: maxAttempts,
      ...(entries ? { entries } : {}),
    },
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
//
// REGRESSION PIN (W6.10 gate blocker): with makeFakePlanClient's staged
// admission, the retry begin's FIRST status poll reads the STALE rejected
// capture_result still sitting in the last-write-wins slot — the real relay
// ordering. A waitForCaptureAuthorized that treats a rejected capture_result
// as session-terminal turns this into "Link expired" after every first
// rejection; this test's completed round trip pins that it must not.
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

// ============================================================================
// 6 (S1). A generic error AFTER `armed` was posted (a transient putBlob
// failure here — the reviewer's reachable case) is TERMINAL: the previous
// screen's begin button must NOT stay live bound to the already-consumed
// (index, attempt) (a re-tap would post a begin the Pi refuses as
// begin_replayed → session-ending CaptureFailed, or worse re-record a
// sweep-less window). Stop state stays coherent: the session is ended, so
// a Stop tap after the terminal is a clean no-op.
// ============================================================================
async function testPostArmUploadFailureIsTerminalNotStaleRetry() {
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
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        client._last = { phase: "capture_authorized", index, attempt };
      } else if (event.armed) {
        client._last = { phase: "sweep_complete" };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: client._last || {} };
    },
    async putBlob() {
      // Transient relay hiccup — NOT a dead-session status (that path has
      // its own terminal), and NOT sweepFailed-flagged: the generic
      // catch-all must classify it terminal purely from armedPosted.
      const err = new Error("relay 500");
      err.status = 500;
      throw err;
    },
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Measurement failed");
  assert.ok(
    !ctx.screenEl.children.some((c) => c.tagName === "BUTTON"),
    "post-arm failure leaves no live begin button bound to the consumed attempt",
  );
  const link = backLink(ctx.screenEl);
  assert.ok(link, "the terminal still offers Back to speaker");
  // Stop state coherent: the session ended with the terminal, so Stop is a
  // clean no-op (stopCapture only acts while an abort handler is live).
  assert.equal(stopCapture(), undefined, "Stop after the terminal is a no-op");
  assert.ok(
    !posted.some((e) => e.aborted),
    "the no-op Stop never posts a late aborted event",
  );
  // Exactly one begin was ever posted — nothing on the terminal can replay it.
  assert.equal(posted.filter((e) => e.begin_capture && !e.armed).length, 1);
  ok();
}

// ============================================================================
// 7 (S1). A generic error BEFORE `armed` (mic permission denied here) keeps
// the live retry — the round never started on the Pi, so re-tapping the
// begin affordance is safe and correct — and Stop stays WIRED (the session
// is still alive). The failure copy names the actual on-screen affordance,
// never a nonexistent "Start" button (N3).
// ============================================================================
async function testPreArmFailureKeepsRetryLiveAndStopWired() {
  statusHistory.length = 0;
  const { onPlanStart, stopCapture } = await loadModule();
  // Mic open rejection — the canonical pre-arm failure.
  globalThis.__recorderError = new Error("Permission denied");
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
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: client._last || {} };
    },
    async putBlob() {
      throw new Error("must not upload before arming");
    },
  };
  const ctx = makeCtx(spec, client);
  // The spec screen's own begin button is still the live affordance for
  // round 1 — give ctx.captureRefs the same shape boot's renderScreen
  // produces so planRetryAffordance can name it.
  const beginButton = makeNode("button");
  beginButton.textContent = "I've positioned the mic — measure Woofer driver";
  ctx.captureRefs = { buttons: [{ action: "begin_capture", el: beginButton }], levelMeters: [] };

  await onPlanStart(ctx);

  const lastStatus = statusHistory[statusHistory.length - 1];
  assert.ok(
    lastStatus.includes("Tap I've positioned the mic — measure Woofer driver to try again"),
    `pre-arm failure copy names the actual affordance, got: ${lastStatus}`,
  );
  assert.ok(
    !lastStatus.includes("Tap Start to try again"),
    "the plan flow never points at a nonexistent Start button",
  );
  // Stop is still wired: the session survived the pre-arm failure.
  const stopped = stopCapture();
  assert.ok(stopped, "Stop stays live after a pre-arm failure");
  await stopped;
  assert.equal(headingText(ctx.screenEl), "Measurement stopped.");
  assert.deepEqual(
    posted.filter((e) => e.aborted).map((e) => e.abort_reason),
    ["stopped"],
  );
  globalThis.__recorderError = null;
  ok();
}

// ============================================================================
// 8. Per-capture entries (§5.7, crossover-measurement-productization-
// design.md): `entryForIndex` is a pure, directly-exported helper — pin its
// 1-based-wire -> 0-based-entry lookup and its null fallbacks.
// ============================================================================
async function testEntryForIndexMapsOneBasedWireIndexToZeroBasedEntry() {
  const { entryForIndex } = await loadModule();
  const entries = [
    { index: 0, kind_label: "check", duration_ms: 5000 },
    { index: 1, kind_label: "measure", duration_ms: 6000, screen: { title: "Measure" } },
  ];
  const spec = planSpec({ target: 2, entries });

  assert.equal(entryForIndex(spec, 1), entries[0]);
  assert.equal(entryForIndex(spec, 2), entries[1]);
  assert.equal(entryForIndex(spec, 3), null, "out of range -> null");
  assert.equal(entryForIndex(planSpec({ target: 2 }), 1), null, "v1 spec (no entries) -> null");
  assert.equal(entryForIndex(null, 1), null);
  ok();
}

// ============================================================================
// 9. An entry's own `screen` copy (title/body) drives the "ready for the next
// measurement" screen instead of the generic "Measurement N of target ✓" —
// proves the v3 loop reads the UPCOMING entry, not the current one.
// ============================================================================
async function testEntryScreenCopyDrivesTheNextMeasurementScreen() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({
    target: 2,
    maxAttempts: 2,
    entries: [
      { index: 0, kind_label: "check", duration_ms: 25000 },
      {
        index: 1,
        kind_label: "verify",
        duration_ms: 15000,
        screen: { title: "Ready for VERIFY", body: "Stand back and stay quiet." },
      },
    ],
  });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  // Capture 1 accepted -> the UPCOMING capture (index 2, entries[1]) supplies
  // its own screen copy instead of the generic "Measurement 1 of 2 ✓".
  assert.equal(headingText(ctx.screenEl), "Ready for VERIFY");
  assert.equal(noteText(ctx.screenEl), "Stand back and stay quiet.");
  ok();
}

// ============================================================================
// 10. A `capture_deferred` host event (§5.7) is a NON-terminal soft-hold: the
// page renders a waiting screen (no begin button — see renderPlanDeferred)
// and automatically retries the SAME begin_capture after a short poll,
// rather than surfacing an error or requiring a tap. Mirrors
// tests/test_capture_relay_plan.py's Python-side deferred coverage.
// ============================================================================
function makeDeferredThenAcceptClient({ target = 1 } = {}) {
  const posted = [];
  let last = {};
  let deferredOnce = false;
  let pendingResult = null;
  let acceptedCount = 0;
  const client = {
    async postEvent(event) {
      posted.push(event);
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        if (!deferredOnce) {
          deferredOnce = true;
          last = {
            phase: "capture_deferred",
            index,
            attempt,
            code: "not_ready",
            error: "Waiting for the previous step to finish.",
          };
        } else {
          last = { phase: "capture_authorized", index, attempt };
        }
      } else if (event.armed) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "sweep_complete" };
        pendingResult = { index, attempt };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: last };
    },
    async putBlob() {
      if (pendingResult) {
        const { index, attempt } = pendingResult;
        pendingResult = null;
        acceptedCount += 1;
        const resultEvent = { phase: "capture_result", index, attempt, accepted: true };
        if (acceptedCount >= target) {
          last = resultEvent;
          queueMicrotask(() => {
            last = {
              phase: "capture_set_complete",
              accepted: acceptedCount,
              capture_target: target,
            };
          });
        } else {
          last = resultEvent;
        }
      }
      return { ok: true, capture_index: 0 };
    },
  };
  return { client, posted };
}

async function testDeferredBeginRendersWaitingScreenAndRetriesAutomatically() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const { client, posted } = makeDeferredThenAcceptClient({ target: 1 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  // The SAME (index=1, attempt=1) pair posted twice — the deferred retry,
  // never a new attempt number and never a Stop/error.
  const beginEvents = posted.filter((e) => e.begin_capture && !e.armed);
  assert.deepEqual(
    beginEvents.map((e) => [e.begin_capture.index, e.begin_capture.attempt]),
    [[1, 1], [1, 1]],
  );
  assert.ok(
    statusHistory.some((s) =>
      s.includes("Waiting — Waiting for the previous step to finish.")
    ),
    `expected a deferred waiting status, got: ${JSON.stringify(statusHistory)}`,
  );
  // The set completed normally after the retry succeeded.
  assert.equal(headingText(ctx.screenEl), "All measurements done");
  ok();
}

// ============================================================================
// 11. Blocker #4a (auto-advance on_apply): after an accepted capture whose NEXT
// entry is on_apply, the hold state OWNS the whole screen — the entry's own
// title, NO begin affordance (a stale "Next measurement" pill/button was the
// round-2 defect) — and a begin is auto-scheduled (the deferred loop posts it
// as liveness; no tap).
// ============================================================================
async function testOnApplyNextEntryHoldsScreenWithNoBeginAffordance() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({
    target: 2,
    maxAttempts: 2,
    entries: [
      { index: 0, kind_label: "check", duration_ms: 25000, screen: { auto_advance: "tap" } },
      {
        index: 1,
        kind_label: "verify",
        duration_ms: 15000,
        screen: {
          title: "Waiting for apply",
          body: "Apply the measured crossover on the speaker page.",
          auto_advance: "on_apply",
        },
      },
    ],
  });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Waiting for apply");
  assert.equal(
    ctx.captureRefs.buttons.length,
    0,
    "the hold state must not render a begin affordance (blocker #4a)",
  );
  // The begin is auto-scheduled (liveness) — clear it so the harness does not
  // spin the never-authorized deferred loop.
  assert.notEqual(ctx.autoAdvanceTimer, null, "on_apply auto-schedules the next begin");
  clearTimeout(ctx.autoAdvanceTimer);
  ctx.autoAdvanceTimer = null;
  ok();
}

// ============================================================================
// 11b. W6.12 nit: the on_apply hold's "Waiting for apply" heading advances to
// "Verifying…" once the deferral resolves and recording actually starts.
// Before this fix the heading stayed "Waiting for apply" through the WHOLE
// verify capture (the sweep runs for several seconds) — a household glancing
// at the phone mid-recording read a heading describing a wait that already
// ended. Unit-tested directly against the two functions involved
// (renderPlanDeferred sets the hold screen + captures the heading node;
// runPlanCapture calls advanceDeferredHoldHeading once beginAndAwaitAuthorization
// resolves as authorized) rather than through the full async capture loop,
// which has no real pause point between authorization and completion once
// the fake relay auto-resolves everything.
// ============================================================================
async function testDeferredHoldHeadingAdvancesWhenRecordingStarts() {
  const { renderPlanDeferred, advanceDeferredHoldHeading } = await loadModule();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({
    target: 1,
    maxAttempts: 1,
    entries: [
      {
        index: 0,
        kind_label: "verify",
        duration_ms: 15000,
        screen: {
          title: "Waiting for apply",
          body: "Apply the measured crossover on the speaker page.",
          auto_advance: "on_apply",
        },
      },
    ],
  });
  const ctx = { spec, screenEl: makeScreenEl(), captureRefs: {} };

  renderPlanDeferred(ctx, { index: 1, target: 1 });
  assert.equal(headingText(ctx.screenEl), "Waiting for apply");

  advanceDeferredHoldHeading(ctx);
  assert.equal(headingText(ctx.screenEl), "Verifying…");
  ok();
}

async function testAdvanceDeferredHoldHeadingIsANoOpWhenNothingHeld() {
  const { advanceDeferredHoldHeading } = await loadModule();
  // check/measure never call renderPlanDeferred, so captureRefs.heading is
  // never set for them — must not throw, must not invent a heading.
  advanceDeferredHoldHeading({ captureRefs: {} });
  advanceDeferredHoldHeading({});
  ok();
}

// ============================================================================
// 12. Blocker #4b (auto-advance countdown): after an accepted capture whose
// NEXT entry is countdown, the page shows a VISIBLE cancelable countdown (the
// policy was carried but never rendered) — the entry copy, a "Starting in N…"
// counter, no begin affordance — with the auto-begin armed on an interval.
// ============================================================================
async function testCountdownNextEntryShowsVisibleCancelableCountdown() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({
    target: 2,
    maxAttempts: 2,
    entries: [
      { index: 0, kind_label: "check", duration_ms: 25000, screen: { auto_advance: "tap" } },
      {
        index: 1,
        kind_label: "measure",
        duration_ms: 15000,
        screen: { title: "Measuring", auto_advance: "countdown", countdown_s: "5" },
      },
    ],
  });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Measuring");
  const paras = ctx.screenEl.children.filter((c) => c.tagName === "P");
  assert.ok(
    paras.some((p) => p.textContent.includes("Starting in 5")),
    `expected a visible countdown, got: ${JSON.stringify(paras.map((p) => p.textContent))}`,
  );
  assert.equal(
    ctx.captureRefs.buttons.length,
    0,
    "the countdown owns the screen — no begin affordance until it elapses or cancels",
  );
  assert.notEqual(ctx.autoAdvanceInterval, null, "the countdown arms an interval");
  clearInterval(ctx.autoAdvanceInterval);
  ctx.autoAdvanceInterval = null;
  ok();
}

// ============================================================================
// 13. Blocker #3 (phone side): a SESSION-terminal host event
// (capture_set_exhausted — what the watchdog-collapse relay-death arm now posts)
// arriving while the phone is waiting to begin must end the session (the "Link
// expired" terminal), not leave it polling a dead session forever. Round 2:
// "the phone saw nothing" during a collapse in the hold.
// ============================================================================
function makeSessionOverOnBeginClient(terminal = {}) {
  let last = {};
  const posted = [];
  return {
    posted,
    async postEvent(event) {
      posted.push(event);
      if (event.begin_capture && !event.armed) {
        last = {
          phase: "capture_set_exhausted",
          accepted: 0,
          capture_target: 1,
          ...terminal,
        };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: last };
    },
    async putBlob() {
      return { ok: true };
    },
  };
}

async function testSessionTerminalDuringWaitEndsTheSession() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const client = makeSessionOverOnBeginClient();
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  // Terminal "Link expired" — not a stuck waiting screen, and no stale retry.
  assert.equal(headingText(ctx.screenEl), "Link expired");
  ok();
}

async function testReviewHoldTimeoutNamesTheTimeoutOnThePhone() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const client = makeSessionOverOnBeginClient({
    code: "review_hold_timed_out",
    reason: "The review wait timed out. Return to the speaker page and start the measurement again.",
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Review timed out");
  const note = ctx.screenEl.children.find((child) => child.className === "cap-note");
  assert.match(note.textContent, /review wait timed out/i);
  assert.match(note.textContent, /start the measurement again/i);
  assert.notEqual(headingText(ctx.screenEl), "Link expired");
  ok();
}

// ============================================================================
// 14. W6.13: a v2 crossover_sweep session has no calibration-picker/confirm
// screen to post setup from (unlike level_ramp's Continue tap), so the
// silently-applied household-mic calibration
// (applyDefaultCalibrationHintSilently, boot()) previously never reached the
// wire until the LATER `armed` event — well after the first begin_capture
// was admitted and CHECK's resolution ran. The fix PIGGYBACKS `setup` on the
// begin event itself (beginAndAwaitAuthorization): the relay's phone-event
// slot is last-write-wins, so a standalone setup post would be overwritten
// by the begin within one write-RTT (the same overwrite class the
// ambient_stats piggyback comment documents) — riding the SAME event is
// order-race-proof by construction. Pins two things: (a) EVERY begin event
// carries the applied calibration in `setup`; (b) a LATER spec's default
// hint never clobbers a calibration this page load already claimed (the
// W6.12 guard) — the begin still posts the ORIGINAL choice.
//
// setupState is a module-scoped variable shared by every test in this FILE
// (loadModule()'s data: URL is byte-identical across calls, so Node's ESM
// cache returns the SAME module instance every time — see the file's use of
// applyDefaultCalibrationHintSilently below). This is the only test in the
// file that touches calibration, so it owns BOTH scenarios in one function
// rather than risking order-dependent leakage across two.
// ============================================================================
async function testEveryBeginCarriesTheAppliedCalibrationAndNeverClobbersAnExplicitChoice() {
  statusHistory.length = 0;
  const { onPlanStart, applyDefaultCalibrationHintSilently } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  // --- (a) a fresh page load with a resolvable default hint applies it
  // silently (boot()'s call, mirrored here) and every begin event carries
  // it — including the retry begin after a rejected attempt.
  const specWithHint = planSpec({ target: 2, maxAttempts: 3 });
  specWithHint.default_setup = {
    calibration: {
      mode: "serial",
      calibration_id: "cal-household",
      model: "minidsp_umik2",
      resolvable: true,
    },
  };
  applyDefaultCalibrationHintSilently(specWithHint);

  const { client, posted } = makeFakePlanClient({
    target: 2,
    maxAttempts: 3,
    resultFor: (index, attempt) => (
      attempt === 1 ? { accepted: false, error: "SNR too low." } : { accepted: true }
    ),
  });
  const ctx = makeCtx(specWithHint, client);
  await onPlanStart(ctx);
  // Attempt 1 rejected -> "Try again" -> attempt 2 accepted -> "Next" ->
  // attempt 3 accepted (set complete): three begin posts total.
  let next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();
  next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();
  assert.equal(headingText(ctx.screenEl), "All measurements done");

  const beginEvents = posted.filter((e) => e.begin_capture && !e.armed);
  assert.equal(
    beginEvents.length, 3,
    `expected three begin posts (reject, retry, next), got: ${JSON.stringify(posted)}`,
  );
  for (const event of beginEvents) {
    assert.deepEqual(
      event.setup && event.setup.calibration,
      { mode: "stored", calibration_id: "cal-household", model: "minidsp_umik2" },
      `every begin event must piggyback the applied calibration, got: ${JSON.stringify(event)}`,
    );
  }

  // --- (b) a DIFFERENT default hint arriving in a later spec (a fresh
  // boot() in the same tab, or a subsequent session) must never clobber the
  // calibration this page load already claimed (W6.12's existing guard) —
  // and the begin must still post the ORIGINAL choice, not the new hint.
  const specWithDifferentHint = planSpec({ target: 1, maxAttempts: 1 });
  specWithDifferentHint.default_setup = {
    calibration: {
      mode: "serial",
      calibration_id: "cal-different",
      model: "minidsp_umik2",
      resolvable: true,
    },
  };
  applyDefaultCalibrationHintSilently(specWithDifferentHint);

  const { client: client2, posted: posted2 } = makeFakePlanClient({ target: 1, maxAttempts: 1 });
  const ctx2 = makeCtx(specWithDifferentHint, client2);
  await onPlanStart(ctx2);

  const beginEvents2 = posted2.filter((e) => e.begin_capture && !e.armed);
  assert.equal(beginEvents2.length, 1);
  assert.deepEqual(
    beginEvents2[0].setup.calibration,
    { mode: "stored", calibration_id: "cal-household", model: "minidsp_umik2" },
    "a later default hint must never clobber the calibration this page load already claimed",
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
  testPostArmUploadFailureIsTerminalNotStaleRetry,
  testPreArmFailureKeepsRetryLiveAndStopWired,
  testEntryForIndexMapsOneBasedWireIndexToZeroBasedEntry,
  testEntryScreenCopyDrivesTheNextMeasurementScreen,
  testDeferredBeginRendersWaitingScreenAndRetriesAutomatically,
  testOnApplyNextEntryHoldsScreenWithNoBeginAffordance,
  testDeferredHoldHeadingAdvancesWhenRecordingStarts,
  testAdvanceDeferredHoldHeadingIsANoOpWhenNothingHeld,
  testCountdownNextEntryShowsVisibleCancelableCountdown,
  testSessionTerminalDuringWaitEndsTheSession,
  testReviewHoldTimeoutNamesTheTimeoutOnThePhone,
  testEveryBeginCarriesTheAppliedCalibrationAndNeverClobbersAnExplicitChoice,
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
