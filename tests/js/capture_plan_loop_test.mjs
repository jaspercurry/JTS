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
// offered); capture_set_exhausted; Stop mid-round; and (tests 28-37) the
// flow-simplification UX redesign — the one-instruction-per-step grammar,
// voluntary retakes and the window they must never outlive, the group-close
// confirm, and VERIFY's confirm-then-tone.
//
// The fake relay enforces the runner's ORDERING contract (not just its happy
// path), so a retake test proves something: a page that dropped the marker,
// or offered a retake after the window shut, gets refused exactly as the Pi
// would refuse it.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { RelayClient } from "../../capture-page/js/relay-client.js";
import { runTestFunctions } from "./run_test_functions.mjs";

// The EXACT rejection a real relay timeout raises, produced by driving the REAL
// RelayClient against a spec-accurate fetch (one that rejects with the signal's
// own `.reason`, like a browser's).
//
// Hand-rolling this is what let #1824 B1 through review: the fakes threw
// `DOMException("…", "AbortError")`, a shape production never produces, so the
// classifier they were supposed to exercise returned FALSE for every real
// timeout and every swallow site rethrew on the first slow poll. Deriving the
// fixture from the client means a future change to how the timeout reason is
// constructed re-breaks these tests instead of silently un-fixing the page.
async function productionRelayTimeoutError() {
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
  try {
    // 250 ms is _controlFetch's own floor, so this is the shortest real timeout.
    await client.fetchPhoneStatus({ timeoutMs: 1 });
  } catch (err) {
    return err;
  }
  throw new Error("harness: the real relay client did not time out");
}

const RELAY_TIMEOUT = await productionRelayTimeoutError();
// Guard the fixture itself: if this ever stops being the production shape, the
// tests below would go back to proving nothing.
assert.equal(RELAY_TIMEOUT.relayTimeout, true, "fixture is the tagged timeout");
assert.notEqual(
  RELAY_TIMEOUT.name, "AbortError",
  "fixture is the REAL shape, not the DOMException the old fakes threw",
);

// The legacy bare-abort shape — a browser that ignores `abort(reason)` and
// raises its own DOMException. Still classified, still covered.
const LEGACY_BARE_ABORT = new DOMException(
  "signal is aborted without reason.", "AbortError",
);

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
    removeEventListener(ev, fn) {
      this._listeners[ev] = (this._listeners[ev] || []).filter((f) => f !== fn);
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

// The step-screen grammar (flow-simplification §2.1) renders the counter as a
// small `cap-eyebrow` paragraph ABOVE the headline, so "the first <p>" is no
// longer the detail line — select by class instead.
function eyebrowText(screenEl) {
  const eyebrow = screenEl.children.find(
    (c) => c.tagName === "P" && String(c.className).includes("cap-eyebrow"),
  );
  return eyebrow ? eyebrow.textContent : "";
}

function noteText(screenEl) {
  const note = screenEl.children.find(
    (c) => c.tagName === "P" && String(c.className).includes("cap-note"),
  );
  return note ? note.textContent : "";
}

// Every note paragraph, in order (the countdown screen renders its live
// counter as a second one).
function noteTexts(screenEl) {
  return screenEl.children
    .filter((c) => c.tagName === "P" && String(c.className).includes("cap-note"))
    .map((c) => c.textContent);
}

// The demoted Stop control (§2.1): a text link inside its own `cap-stop`
// wrapper, on every page-owned step screen.
function stopLink(screenEl) {
  const wrap = screenEl.children.find(
    (c) => c.tagName === "DIV" && String(c.className).includes("cap-stop"),
  );
  return wrap ? wrap.children.find((c) => c.tagName === "BUTTON") || null : null;
}

// The action row's buttons, in render order (primary, then any secondaries).
function actionButtons(screenEl) {
  const row = screenEl.children.find(
    (c) => c.tagName === "DIV" && String(c.className).includes("cap-actions"),
  );
  return row ? row.children.filter((c) => c.tagName === "BUTTON") : [];
}

function actionLabels(screenEl) {
  return actionButtons(screenEl).map((b) => b.textContent);
}

// Let queued microtasks/promise jobs drain — used where a round parks on a
// tap (the §2.2 post-apply confirmation) so the test can inspect the screen
// and click while runPlanCapture is still suspended.
async function settle(rounds = 40) {
  for (let i = 0; i < rounds; i += 1) await Promise.resolve();
}

function fire(node, event = "click") {
  const listeners = (node && node._listeners && node._listeners[event]) || [];
  return Promise.all(listeners.map((fn) => fn()));
}

// The page's own <dialog> (index.html) behind the demoted Stop link. Modelled
// closely enough to exercise the real contract: showModal opens it, close()
// fires a `close` event (what ESC/backdrop dismissal does in a browser), and
// the accept/cancel buttons are separate nodes looked up by id.
function makeStopConfirmDialog() {
  const dialog = makeNode("dialog");
  dialog.opens = 0;
  dialog.open = false;
  dialog.showModal = () => {
    dialog.open = true;
    dialog.opens += 1;
  };
  dialog.close = () => {
    if (!dialog.open) return;
    dialog.open = false;
    void fire(dialog, "close");
  };
  return { dialog, accept: makeNode("button"), cancel: makeNode("button") };
}

// `getElementById` dispatches by id when a test opts into the stop-confirm
// dialog; every other id keeps returning the status element (the shape every
// pre-existing test in this file relies on, where the dialog is absent and
// confirmStopMeasuring() therefore fails open).
function installDocument(statusEl, confirm = null) {
  globalThis.document = {
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => {
      if (confirm) {
        if (id === "stop-confirm") return confirm.dialog;
        if (id === "stop-confirm-accept") return confirm.accept;
        if (id === "stop-confirm-cancel") return confirm.cancel;
      }
      return statusEl;
    },
  };
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

// `track` is created ONCE and always returned by the SAME reference from
// getAudioTracks() — unlike a fresh object-literal-per-call stub, this lets a
// test grab the exact track wireTrackEndedRecovery() attached `.onended` to
// and invoke it later to simulate the mic disconnecting (#1658).
function makeRecorder() {
  const track = {
    label: "Test microphone",
    onended: null,
    getSettings() {
      return {
        autoGainControl: false,
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        sampleRate: 48000,
      };
    },
  };
  // S1: a stub AudioContext with a counted resume() — main.js's per-round
  // resume() call is guarded (`recorder.context && typeof …resume === "function"`)
  // so fixtures that omit `.context` (e.g. makeRecorderThatDiesDuringRecording)
  // stay unaffected.
  const context = {
    resumes: 0,
    async resume() {
      context.resumes += 1;
    },
  };
  const recorder = {
    capturedChannelCount: 1,
    starts: 0,
    stops: 0,
    closes: 0,
    context,
    stream: {
      getAudioTracks() {
        return [track];
      },
    },
    start() {
      recorder.starts += 1;
    },
    async stop() {
      recorder.stops += 1;
      return new Float32Array(4800); // 100ms of silence @ 48kHz
    },
    async close() {
      recorder.closes += 1;
    },
  };
  return recorder;
}

const injected = `
const acceptedAcknowledgement = (spec, refs) => (
  spec && spec.acknowledgement
    ? { schema_version: 1, id: spec.acknowledgement.id, binding_id: spec.acknowledgement.binding_id, accepted: true }
    : null
);
const setText = (node, text) => { node.textContent = typeof text === "string" ? text : ""; };
const createMonoRecorder = async () => {
  globalThis.__recorderCalls = (globalThis.__recorderCalls || 0) + 1;
  if (globalThis.__recorderError) throw globalThis.__recorderError;
  // B1 regression harness: when a test parks this call on
  // globalThis.__recorderGate, it flags __recorderGateReached SYNCHRONOUSLY
  // (before awaiting) so a polling test loop can detect "we are now stuck
  // here, safe to fire Stop" without guessing a microtask-flush count.
  if (globalThis.__recorderGate) {
    globalThis.__recorderGateReached = true;
    await globalThis.__recorderGate;
  }
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
// #1658: supported defaults true (so the fallback hint stays silent for every
// test that does not opt in via globalThis.__wakeLockUnsupported), and every
// acquire/release is counted so the plan-loop tests can pin "once per
// session, not once per round".
const acquireWakeLock = async () => {
  globalThis.__wakeAcquireCalls = (globalThis.__wakeAcquireCalls || 0) + 1;
  // N-2 regression harness: when a test parks this call on
  // globalThis.__wakeAcquireGate, it flags __wakeAcquireGateReached
  // SYNCHRONOUSLY (before awaiting) so a polling test loop can detect "we
  // are now stuck here" without guessing a microtask-flush count — mirrors
  // the B1 __recorderGate mechanism above.
  if (globalThis.__wakeAcquireGate) {
    globalThis.__wakeAcquireGateReached = true;
    await globalThis.__wakeAcquireGate;
  }
  return {
    supported: globalThis.__wakeLockUnsupported !== true,
    release: async () => {
      globalThis.__wakeReleaseCalls = (globalThis.__wakeReleaseCalls || 0) + 1;
    },
  };
};
const watchVisibilityAbort = () => () => {};
// N2 regression harness: capture the onVisible callback main.js wires so a
// test can invoke it directly to simulate the page returning from a brief
// hide, instead of it being an unreachable no-op.
const watchVisibilityReacquire = (doc, onVisible) => {
  globalThis.__wakeReacquireCallback = onVisible;
  return () => {
    globalThis.__wakeReacquireCallback = null;
  };
};
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
  // FIDELITY (flow-simplification §2.6): mirror `_poll_capture_plan`'s
  // ORDERING contract, not just its happy path. The runner admits
  // (accepted_count + 1, attempts_used + 1) and exactly one other shape — a
  // begin for the just-accepted index, on the next attempt, marked
  // `retake: true`, and only while the next entry's begin has not been seen.
  // Anything else is refused as `begin_out_of_order`, which ends the whole
  // session. Enforcing it here is what makes a retake test prove something:
  // a page that forgot the marker (or offered a retake after the window shut)
  // fails instead of quietly passing against a permissive fake.
  //
  // WHAT THIS MIRROR DELIBERATELY OMITS — it is STRICTER than the runner, so
  // read a refusal here as "the page did something the runner might still
  // tolerate", never as proof it would fail on hardware. The real
  // `_poll_capture_plan` also keeps `processed` + a `phase`, which make a
  // REPEAT of the in-flight (index, attempt) a no-op rather than a refusal
  // (the phone re-posts its begin on every deferral, and the armed event
  // re-states it). This fake has no such tolerance: every begin is judged
  // against the ordering rule alone. A future test that exercises the polling
  // /re-post paths will need that tolerance added here, or it will see a
  // false-positive refusal.
  let attemptsUsed = 0;
  let nextBeginSeen = false;
  let currentRetake = false;
  let completions = 0;
  const client = {
    async postEvent(event) {
      posted.push(event);
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        const wantsRetake = event.begin_capture.retake === true;
        const inOrder = index === acceptedCount + 1 && attempt === attemptsUsed + 1;
        const admittedRetake =
          wantsRetake &&
          acceptedCount > 0 &&
          !nextBeginSeen &&
          index === acceptedCount &&
          attempt === attemptsUsed + 1;
        if (refuseAttempt && attempt === refuseAttempt) {
          queuedAdmission = {
            phase: "capture_refused",
            code: "budget_exceeded",
            error: "The household's driver repeat budget is exhausted.",
            index,
            attempt,
          };
        } else if (!inOrder && !admittedRetake) {
          queuedAdmission = {
            phase: "capture_refused",
            code: "begin_out_of_order",
            error: `expected capture ${acceptedCount + 1} attempt ${attemptsUsed + 1}`,
            index,
            attempt,
          };
        } else {
          attemptsUsed = attempt;
          currentRetake = admittedRetake;
          if (!admittedRetake) nextBeginSeen = true;
          queuedAdmission = { phase: "capture_authorized", index, attempt };
        }
      } else if (event.complete_capture_set === true) {
        // FIDELITY (two-stage work order D1): the household's explicit
        // set-completion signal. A HELD set — one whose last accepted verdict
        // carried `awaiting_confirm` — ends here and nowhere else; the Pi
        // closes the group, fits, and only then posts capture_set_complete.
        completions += 1;
        queuedAdmission = {
          phase: "capture_set_complete",
          accepted: acceptedCount,
          capture_target: target,
        };
      } else if (event.armed) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "sweep_complete" };
        pendingResult = {
          index,
          attempt,
          retake: currentRetake,
          verdict: resultFor(index, attempt),
        };
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
        const { index, attempt, retake, verdict } = pendingResult;
        pendingResult = null;
        // An accepted RETAKE replaces evidence rather than adding a capture,
        // so it must not advance the accepted count (that would finish the
        // set one capture short) and it leaves the retake window open.
        if (verdict.accepted && !retake) {
          acceptedCount += 1;
          nextBeginSeen = false;
        }
        // FIDELITY: the real runner relays the host's WHOLE verdict mapping
        // minus `accepted` (`_poll_capture_plan`'s capture_result post), not
        // just `error` — the v2 conductor's rejections carry `reason`,
        // `banner`, `code`, and a position-group `prompt`. Spreading here is
        // what lets a test see the fields the page must extract.
        const { accepted, ...verdictFields } = verdict;
        const resultEvent = {
          phase: "capture_result",
          index,
          attempt,
          accepted,
          ...verdictFields,
        };
        if (verdict.accepted && verdict.awaiting_confirm) {
          // HELD (work order D1): the Pi posts NOTHING further and stays in
          // awaiting_begin, so the last-write-wins slot keeps this verdict and
          // the phone's confirm screen is what the household reads. The set
          // ends on their signal, handled in postEvent above.
          last = resultEvent;
        } else if (verdict.accepted && acceptedCount >= target) {
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
  return {
    client, posted, blobPuts,
    acceptedCount: () => acceptedCount,
    completions: () => completions,
  };
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
  // RE-DERIVED for PR-T4 (work order D7): the fallback used to promise "the
  // speaker continues automatically", which is exactly false for a stage-1
  // session — it deliberately does not continue, it waits for the household's
  // decision. The replacement is true of every flow that reaches this shared
  // screen, so no flow needs a branch to be told the truth.
  assert.equal(
    noteText(ctx.screenEl),
    "All measurements done — the speaker page shows what happens next.",
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
  // §2.4 retry grammar: the eyebrow carries the counter plus "one more try",
  // the headline is the instruction (here the generic fallback — this plan
  // has no entry copy), the detail is the reason sentence. The fallback
  // headline does NOT count again: the eyebrow above it already did (M2).
  assert.equal(eyebrowText(ctx.screenEl), "Measurement 1 of 2 — one more try");
  assert.equal(headingText(ctx.screenEl), "Take that measurement again");
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
// 2a. A rejection carrying the Pi's OWN copy renders that copy, not the
//     generic line — and a position-group geometry retake's `prompt` (the
//     "move further out" instruction) HEADLINES the retry screen (§2.4: the
//     instruction owns the slot the household reads first; the reason
//     sentence becomes the detail underneath).
//
// REGRESSION PIN (round-1 review blocker B2): the page extracted only
// `accepted`/`error` from capture_result, so the v2 conductor's `reason` /
// `banner` / `prompt` were dropped on the floor and every rejection showed
// "That measurement didn't pass the speaker's quality check." For a geometry
// lock that sentence is FALSE (the capture is fine — the positions were too
// clustered) and, worse, actionless: the operator retook from the same spot,
// so spread never increased and the retake could not help.
// ============================================================================
async function testGeometryRetakeRendersTheServerSuppliedGuidance() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const REASON =
    "These spots were too close together to tell a real dip from an echo. " +
    "Take this one from further out and we will use it instead.";
  // RE-DERIVED for PR-T4 (#1805's 2026-07-28 ruling): the shipped rung is a
  // numeric ABSOLUTE pose now. This fixture mirrors
  // `CLOUD_GEOMETRY_RETRY_PROMPTS[0]` — the page renders whatever the Pi
  // sends, so the value here is only ever a realistic sample, but a sample in
  // a register the Pi has stopped emitting is a fixture that stops testing the
  // shipped path.
  const PROMPT =
    "Same measurement, wider spot: move the microphone 30 in (75 cm) to the " +
    "LEFT of the mark, at mark height, still pointed at the speaker.";

  const spec = planSpec({ target: 2, maxAttempts: 4 });
  const { client } = makeFakePlanClient({
    target: 2,
    maxAttempts: 4,
    resultFor: (index, attempt) =>
      attempt === 1
        ? {
            accepted: false,
            code: "cloud_geometry_locked",
            reason: REASON,
            banner: "",
            prompt: PROMPT,
          }
        : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  // Both halves reach the household, in the grammar's own slots: what to do
  // as the headline, what happened as the detail underneath.
  assert.equal(headingText(ctx.screenEl), PROMPT);
  assert.deepEqual(noteTexts(ctx.screenEl), [REASON]);
  assert.ok(
    !noteTexts(ctx.screenEl).some((t) => t.includes("quality check")),
    "the generic fallback must not appear when the Pi supplied its own copy",
  );
  // A move instruction means the tap is a placement confirmation, so it says
  // so — "Try again" is for a rejection the household does not have to act on.
  assert.deepEqual(actionLabels(ctx.screenEl), ["I’m there — play the tone"]);
  // #status no longer restates any of it (§2.1) — the screen carries it.
  assert.ok(
    !statusHistory.some((s) => String(s).includes(PROMPT)),
    "the status line stops duplicating the screen's own instruction",
  );

  const retry = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await retry._listeners.click[0]();
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 2 ✓");
  ok();
}

// ============================================================================
// 2a-ter. The honest per-position count (owner ruling #2086 item 2). The
// eyebrow used to read "— one more try" on EVERY rejection, forever, because
// the only counter the page had names the POSITION and a retried position does
// not move: on 2026-08-03 the screen said "step 6, one last time" while the
// flow was on its fifth attempt at that spot. The Pi now publishes the count;
// this pins that the page renders it, and renders who spent it.
// ============================================================================
async function testTheRetryEyebrowCountsThisPositionsExtraTries() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 6 });
  // Attempt 1 is the PLANNED capture (nothing spent). Attempts 2 and 3 are
  // extras — the first one asked for by the speaker, mirroring a geometry rung.
  const attemptsFor = {
    1: { used: 0, allowed: 3, left: 3, by_speaker: 0, by_household: 0 },
    2: { used: 1, allowed: 3, left: 2, by_speaker: 1, by_household: 0 },
    3: { used: 2, allowed: 3, left: 1, by_speaker: 1, by_household: 1 },
  };
  const { client } = makeFakePlanClient({
    target: 2,
    maxAttempts: 6,
    resultFor: (index, attempt) =>
      attempt <= 3
        ? { accepted: false, error: "SNR too low.", attempts: attemptsFor[attempt] }
        : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  // Nothing spent yet, so the NEXT tap is extra try 1 of 3 — never "one more".
  assert.equal(eyebrowText(ctx.screenEl), "Measurement 1 of 2 — extra try 1 of 3");
  assert.deepEqual(noteTexts(ctx.screenEl), ["SNR too low."]);

  let retry = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await retry._listeners.click[0]();
  assert.equal(eyebrowText(ctx.screenEl), "Measurement 1 of 2 — extra try 2 of 3");
  // …and the count says who spent what (ruling item 4): the household's
  // patience was partly spent on the speaker's behalf, so it is not billed
  // silently to them.
  assert.ok(
    noteTexts(ctx.screenEl).includes("JTS asked for 1 of those extra tries itself."),
    `expected the speaker's share, got: ${JSON.stringify(noteTexts(ctx.screenEl))}`,
  );

  retry = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await retry._listeners.click[0]();
  assert.equal(eyebrowText(ctx.screenEl), "Measurement 1 of 2 — extra try 3 of 3");
  ok();
}

// ============================================================================
// 2a-quater. Ruling #2086 item 3's VISIBLE half. When the Pi gives a position
// up it sends `accepted` — the relay's only "this slot is done" signal — plus
// `unresolved`. Without the second field the household reads the advance as a
// tick: continue-and-imply, the quiet cousin of kill-and-lie. The screen must
// say the spot was left out, and must not offer a retake of a position whose
// tries are exactly what ran out.
// ============================================================================
async function testAnUnresolvedPositionSaysSoInsteadOfTicking() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  // No per-entry copy, so the page's own fallback headline is what renders —
  // and that fallback is the one carrying the "✓".
  const spec = planSpec({ target: 3, maxAttempts: 8 });
  const { client } = makeFakePlanClient({
    target: 3,
    maxAttempts: 8,
    resultFor: (index, attempt) =>
      index === 1
        ? {
            accepted: true,
            unresolved: {
              index: 1,
              code: "locate_failed",
              diagnosis: "JTS could hear the speaker, but couldn't line up the test tones in the recording.",
            },
            attempts: { used: 3, allowed: 3, left: 0, by_speaker: 0, by_household: 3 },
          }
        : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 3 — left out");
  assert.ok(
    noteTexts(ctx.screenEl).some((t) =>
      t.includes("could hear the speaker") && t.includes("left that spot out and moved on")),
    `expected the left-out sentence, got: ${JSON.stringify(noteTexts(ctx.screenEl))}`,
  );
  // Only the forward control — no retake of a spot with no tries left.
  assert.deepEqual(actionLabels(ctx.screenEl), ["Next measurement"]);
  ok();
}

// ============================================================================
// 2a-bis. Backwards compatibility in both directions for the same extraction:
// a Pi that sends only `error` (every non-v2 kind, and any older build) still
// renders exactly one paragraph with that text, and a rejection carrying
// NOTHING still gets the generic fallback. Without this the fix above could
// have silently required the new fields.
// ============================================================================
async function testRejectionCopyFallsBackWhenThePiSendsNoGuidance() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 4 });
  const { client } = makeFakePlanClient({
    target: 2,
    maxAttempts: 4,
    resultFor: (index, attempt) => (attempt === 1 ? { accepted: false } : { accepted: true }),
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.deepEqual(
    noteTexts(ctx.screenEl),
    ["That measurement didn't pass the speaker's quality check."],
    "no server copy ⇒ exactly one generic paragraph, unchanged from before",
  );
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
    "the countdown owns the FORWARD path — no forward begin affordance until it "
      + "elapses or cancels (the Retake secondary posts a BACKWARD begin and is "
      + "deliberately not registered here)",
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
function makeSessionOverOnBeginClient() {
  let last = {};
  const posted = [];
  return {
    posted,
    async postEvent(event) {
      posted.push(event);
      if (event.begin_capture && !event.armed) {
        last = { phase: "capture_set_exhausted", accepted: 0, capture_target: 1 };
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
  // This client publishes no `expires_at` (an older relay), so the page has no
  // evidence about the link's clock and keeps exactly this copy — the absence
  // case that testTheSpeakerEndedItIsNotCalledAnExpiryBelow depends on.
  assert.equal(headingText(ctx.screenEl), "Link expired");
  ok();
}

// A session the SPEAKER ended, while the link itself was still live, must not
// be reported to the household as an expiry (issue #2083). Same terminal path
// as above; the only difference is that the relay published a deadline still in
// the future, which is the page's one piece of evidence that no clock ran out.
//
// This is the screen the incident put in front of a household whose link was
// still live, after ONE transient status stall killed their session.
async function testTheSpeakerEndedItIsNotCalledAnExpiry() {
  statusHistory.length = 0;
  const mod = await loadModule();
  const { onPlanStart } = mod;
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const client = makeSessionOverOnBeginClient();
  const live = Date.now() + 10 * 60 * 1000; // the link has ~10 minutes left
  const realFetch = client.fetchPhoneStatus.bind(client);
  client.fetchPhoneStatus = async () => ({ ...(await realFetch()), expires_at: live });
  const ctx = makeCtx(spec, client);

  try {
    await onPlanStart(ctx);

    assert.equal(headingText(ctx.screenEl), "The speaker ended this session");
    const note = ctx.screenEl.children.find((c) => c.tagName === "P");
    assert.match(note.textContent, /speaker ended this measurement/i);
    assert.match(note.textContent, /Return to the speaker page/i);
    // The false claim this exists to stop, in both of its wordings.
    assert.ok(!/expired/i.test(note.textContent), note.textContent);
    assert.ok(!note.textContent.includes("15 minutes"), note.textContent);
  } finally {
    // The harness shares ONE module instance across every test in this file, so
    // the deadline recorded above would otherwise leak forward and flip a later
    // test's expected copy. Put it back in the past.
    mod.notePhoneStatus({ expires_at: Date.now() - 1000 });
  }
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

// ============================================================================
// 15 (#1658 Fix 1 + Fix 2). Session-wide resources: the mic stream/graph and
// the screen wake lock are each acquired ONCE across a whole multi-round
// plan — never once per capture — and released/closed exactly once, at the
// terminal screen. Regression pin for the iOS getUserMedia-renegotiation
// level-step bug (Fix 2) and the "phones sleep mid-session" wake-lock bug
// (Fix 1).
// ============================================================================
async function testSessionWideResourcesAcquiredOnceReleasedOnce() {
  statusHistory.length = 0;
  globalThis.__recorderCalls = 0;
  globalThis.__wakeAcquireCalls = 0;
  globalThis.__wakeReleaseCalls = 0;
  const { onPlanStart } = await loadModule();
  const recorder = makeRecorder();
  globalThis.__recorder = recorder;
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 3, maxAttempts: 4 });
  const { client } = makeFakePlanClient({ target: 3, maxAttempts: 4 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  let next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();
  next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();

  assert.equal(headingText(ctx.screenEl), "All measurements done");
  assert.equal(globalThis.__recorderCalls, 1, "one getUserMedia call covers all 3 captures");
  assert.equal(recorder.starts, 6, "ambient + sweep start once per round across 3 rounds");
  assert.equal(recorder.stops, 6, "ambient + sweep stop once per round across 3 rounds");
  assert.equal(recorder.closes, 1, "the mic stream closes exactly once, at session end");
  assert.equal(globalThis.__wakeAcquireCalls, 1, "one wake-lock request for the whole session");
  assert.equal(globalThis.__wakeReleaseCalls, 1, "the wake lock releases exactly once, at session end");
  ok();
}

// ============================================================================
// 16 (#1658 Fix 1). When the Wake Lock API is unsupported (or the request is
// rejected), a one-line hint appears on the session screen instead of doing
// nothing silently — and it clears once the session reaches its terminal
// screen.
// ============================================================================
async function testWakeLockHintShowsWhenUnsupportedAndClearsAtTerminal() {
  statusHistory.length = 0;
  globalThis.__wakeLockUnsupported = true;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  const hintHistory = [];
  const hintEl = {};
  let hintText = "";
  Object.defineProperty(hintEl, "textContent", {
    get() { return hintText; },
    set(v) { hintText = String(v); hintHistory.push(hintText); },
  });
  globalThis.document = {
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => (id === "wakelock-hint" ? hintEl : statusEl),
  };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const { client } = makeFakePlanClient({ target: 1, maxAttempts: 1 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "All measurements done");
  assert.ok(
    hintHistory.includes("Keep your screen on — this takes about 4 minutes."),
    `expected the fallback hint to have shown at some point, got: ${JSON.stringify(hintHistory)}`,
  );
  assert.equal(hintEl.textContent, "", "the hint clears once the session reaches its terminal screen");
  globalThis.__wakeLockUnsupported = false;
  ok();
}

// ============================================================================
// 17 (#1658 Fix 2, track-ended recovery). A mic track that ends BETWEEN
// rounds (a USB mic unplugged, the OS revoking the track) triggers exactly
// one reacquire attempt. When it succeeds, the NEXT round transparently
// reuses the replacement stream — no fresh createMonoRecorder call of its
// own, and no error surfaced to the household.
// ============================================================================
async function testTrackEndedMidSessionReacquiresTransparently() {
  statusHistory.length = 0;
  globalThis.__recorderCalls = 0;
  const { onPlanStart } = await loadModule();
  const recorderA = makeRecorder();
  globalThis.__recorder = recorderA;
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 2 });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 2 ✓");
  assert.equal(ctx.recorder, recorderA);
  assert.equal(globalThis.__recorderCalls, 1);

  // The mic disconnects while the phone is idling on the "Next measurement"
  // screen — wireTrackEndedRecovery's onended handler tries one reacquire.
  const recorderB = makeRecorder();
  globalThis.__recorder = recorderB;
  const track = recorderA.stream.getAudioTracks()[0];
  assert.equal(typeof track.onended, "function", "wireTrackEndedRecovery attaches onended");
  await track.onended();

  assert.equal(recorderA.closes, 1, "the dead stream's own graph is closed during recovery");
  assert.equal(ctx.recorder, recorderB, "the reacquire replaced the dead stream");
  assert.equal(globalThis.__recorderCalls, 2, "exactly one reacquire attempt");

  const next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();

  assert.equal(headingText(ctx.screenEl), "All measurements done");
  assert.equal(globalThis.__recorderCalls, 2, "round 2 transparently reused the reacquired stream");
  assert.equal(recorderB.starts, 2, "round 2 recorded normally on the replacement (ambient + sweep)");
  ok();
}

// ============================================================================
// 18 (#1658 Fix 2, track-ended recovery failure). When the track dies AND the
// one reacquire attempt also fails, the failure rides the SAME existing
// pre-arm error surface a mic-permission failure already uses (mirrors
// testPreArmFailureKeepsRetryLiveAndStopWired) — the round never reached
// `armed`, so the household can plug in a working mic and retry rather than
// facing a dead terminal screen.
// ============================================================================
async function testTrackEndedReacquireFailureSurfacesOnNextRound() {
  statusHistory.length = 0;
  const { onPlanStart, stopCapture } = await loadModule();
  const recorderA = makeRecorder();
  globalThis.__recorder = recorderA;
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 2 });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 2 ✓");

  globalThis.__recorderError = new Error("mic reacquire failed");
  const track = recorderA.stream.getAudioTracks()[0];
  await track.onended();
  assert.equal(ctx.recorder, null, "the dead stream is discarded, not reused");
  assert.ok(ctx.recorderFailure, "the failed reacquire is recorded for the next round to surface");
  globalThis.__recorderError = null;

  const next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();

  const lastStatus = statusHistory[statusHistory.length - 1];
  assert.ok(
    lastStatus.includes("mic reacquire failed"),
    `expected the recorded reacquire failure to surface, got: ${lastStatus}`,
  );
  assert.equal(
    headingText(ctx.screenEl),
    "Measurement 1 of 2 ✓",
    "the round stays retriable — no terminal screen for a pre-arm failure",
  );
  const stopped = stopCapture();
  assert.ok(stopped, "Stop stays live after the surfaced pre-arm failure");
  await stopped;
  ok();
}

// ============================================================================
// 19 (#1658 Fix 2, "never silently record dead air"). A track that ends
// WHILE a round is actively recording (not between rounds) must fail that
// round via the terminal failure surface rather than trust the (silently
// zeroed) samples a dead track produces. Uses a bespoke recorder fixture
// whose stop() flags __trackEnded, matching what wireTrackEndedRecovery's
// onended handler would have already set on the real recorder object by the
// time stop() resolves.
// ============================================================================
function makeRecorderThatDiesDuringRecording() {
  const track = {
    label: "Test microphone",
    onended: null,
    getSettings() {
      return {
        autoGainControl: false,
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        sampleRate: 48000,
      };
    },
  };
  const recorder = {
    capturedChannelCount: 1,
    stream: { getAudioTracks() { return [track]; } },
    start() {},
    async stop() {
      recorder.__trackEnded = true;
      return new Float32Array(4800);
    },
    async close() {},
  };
  return recorder;
}

async function testTrackEndedDuringActiveRoundFailsRatherThanUploadingDeadAir() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorderThatDiesDuringRecording();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const client = {
    _last: {},
    async postEvent(event) {
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        client._last = { phase: "capture_authorized", index, attempt };
      } else if (event.armed) {
        client._last = { phase: "sweep_complete" };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: client._last };
    },
    async putBlob() {
      throw new Error("must never upload a dead-air capture");
    },
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Measurement failed");
  assert.ok(
    noteText(ctx.screenEl).includes("microphone disconnected"),
    `expected the disconnected-mic failure, got: ${noteText(ctx.screenEl)}`,
  );
  ok();
}

// ============================================================================
// 20 (B1, adversarial-review blocker). Stop firing WHILE createMonoRecorder()
// is still pending — Stop's relay POST winning the race against getUserMedia
// + worklet compile — must not orphan the recorder it eventually resolves
// with. releasePlanSessionResources already ran and nulled ctx.recorder by
// then; runPlanCapture must close the late-arriving recorder rather than
// assigning it (the reviewer's repro: recorder_close_count 0, orphaned live
// recorder, mic stays hot after "Measurement stopped").
// ============================================================================
async function testStopDuringRecorderAcquisitionClosesTheOrphanedStream() {
  statusHistory.length = 0;
  globalThis.__recorderGateReached = false;
  let releaseGate;
  globalThis.__recorderGate = new Promise((resolve) => { releaseGate = resolve; });
  const { onPlanStart, stopCapture } = await loadModule();
  const recorder = makeRecorder();
  globalThis.__recorder = recorder;
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const { client } = makeFakePlanClient({ target: 1, maxAttempts: 1 });
  const ctx = makeCtx(spec, client);

  const p = onPlanStart(ctx);
  // Advance until execution is parked INSIDE createMonoRecorder(), awaiting
  // our gate — the one promise in this whole chain that never resolves on
  // its own, so this loop cannot overshoot past it.
  for (let i = 0; i < 200 && !globalThis.__recorderGateReached; i += 1) {
    await Promise.resolve();
  }
  assert.ok(globalThis.__recorderGateReached, "test setup reached the pending createMonoRecorder call");

  const stopped = stopCapture();
  assert.ok(stopped, "Stop is wired while acquisition is pending");
  await stopped;
  assert.equal(headingText(ctx.screenEl), "Measurement stopped.");

  // Now let createMonoRecorder resolve — B1's guard must close the
  // late-arriving recorder instead of assigning it to ctx.recorder.
  releaseGate();
  await p;

  assert.equal(recorder.closes, 1, "the orphaned recorder is closed, not leaked");
  assert.equal(ctx.recorder, null, "ctx.recorder is never assigned once the session ended");

  globalThis.__recorderGate = null;
  ok();
}

// ============================================================================
// 21 (B1, identical guard in wireTrackEndedRecovery). The same race in the
// track-ended reacquire path: Stop firing while the reacquire's own
// createMonoRecorder() is pending must close the replacement rather than
// assign it to ctx.recorder.
// ============================================================================
async function testStopDuringTrackEndedReacquireClosesTheOrphanedReplacement() {
  statusHistory.length = 0;
  const { onPlanStart, stopCapture } = await loadModule();
  const recorderA = makeRecorder();
  globalThis.__recorder = recorderA;
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 2 });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 2 ✓");

  // The mic disconnects while idling on "Next measurement" — the recovery
  // handler closes recorderA immediately, then reaches for a replacement;
  // gate THAT createMonoRecorder call so Stop can fire while it is pending.
  globalThis.__recorderGateReached = false;
  let releaseGate;
  globalThis.__recorderGate = new Promise((resolve) => { releaseGate = resolve; });
  const recorderB = makeRecorder();
  globalThis.__recorder = recorderB;
  const track = recorderA.stream.getAudioTracks()[0];
  const recovered = track.onended();

  for (let i = 0; i < 200 && !globalThis.__recorderGateReached; i += 1) {
    await Promise.resolve();
  }
  assert.ok(globalThis.__recorderGateReached, "test setup reached the pending reacquire");

  const stopped = stopCapture();
  await stopped;
  assert.equal(headingText(ctx.screenEl), "Measurement stopped.");

  releaseGate();
  await recovered;

  assert.equal(recorderB.closes, 1, "the orphaned replacement is closed, not leaked");
  assert.equal(ctx.recorder, null, "ctx.recorder is never assigned once the session ended");

  globalThis.__recorderGate = null;
  ok();
}

// ============================================================================
// 22 (S1, reviewer should-fix). The reused AudioContext can be auto-suspended
// between rounds (Android Chrome backgrounding a tab; possibly iOS
// foreground idle) without its mic track ever reaching `ended` — the signal
// wireTrackEndedRecovery relies on. Each round must explicitly resume() the
// context before recording.
// ============================================================================
async function testContextResumesBeforeEachRoundsRecording() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  const recorder = makeRecorder();
  globalThis.__recorder = recorder;
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 2 });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(recorder.context.resumes, 1, "round 1 resumes the context once before recording");
  const next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();

  assert.equal(headingText(ctx.screenEl), "All measurements done");
  assert.equal(recorder.context.resumes, 2, "round 2 resumes the reused context again");
  ok();
}

// ============================================================================
// 23 (N2, reviewer nit). reacquireSessionWakeLock releases the PRIOR wake
// lock sentinel (best-effort) before overwriting ctx.wakeLock with the fresh
// one — the browser already dropped the old sentinel internally, but our own
// reference/idempotent-release flag should not just be silently discarded.
// ============================================================================
async function testReacquireReleasesThePriorWakeLockSentinelBeforeOverwriting() {
  statusHistory.length = 0;
  globalThis.__wakeAcquireCalls = 0;
  globalThis.__wakeReleaseCalls = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 2 });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 2 ✓");
  assert.equal(globalThis.__wakeAcquireCalls, 1);
  assert.equal(globalThis.__wakeReleaseCalls, 0, "nothing released yet mid-session");
  const priorLock = ctx.wakeLock;
  assert.ok(priorLock, "onPlanStart acquired the session wake lock");
  assert.equal(typeof globalThis.__wakeReacquireCallback, "function");

  // Simulate the page returning to visible after a brief hide (Control
  // Center swipe) — the browser already auto-released the OLD sentinel;
  // main.js's watchVisibilityReacquire wiring re-requests a fresh one. Poll
  // on the actual end-state (ctx.wakeLock changing) rather than the acquire
  // counter — that counter settles a tick BEFORE the release-then-assign
  // tail of reacquireSessionWakeLock finishes running.
  globalThis.__wakeReacquireCallback();
  for (let i = 0; i < 200 && ctx.wakeLock === priorLock; i += 1) {
    await Promise.resolve();
  }

  assert.equal(globalThis.__wakeAcquireCalls, 2, "the reacquire requested a fresh lock");
  assert.equal(
    globalThis.__wakeReleaseCalls, 1,
    "the STALE prior sentinel is released before being overwritten (N2)",
  );
  assert.notEqual(ctx.wakeLock, priorLock, "ctx.wakeLock now holds the fresh sentinel");
  ok();
}

// ============================================================================
// 24 (N3, reviewer nit). A fast double-tap of the initial Start button must
// not spin up a second session — the second onPlanStart call is a guarded
// no-op while the first session's controller is still alive (fired back to
// back, in the SAME synchronous stretch, before either has a chance to
// yield — the worst-case double-tap shape).
// ============================================================================
async function testDoubleTapOnPlanStartDoesNotStartASecondSession() {
  statusHistory.length = 0;
  globalThis.__wakeAcquireCalls = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const { client } = makeFakePlanClient({ target: 1, maxAttempts: 1 });
  const ctx = makeCtx(spec, client);

  const p1 = onPlanStart(ctx);
  const p2 = onPlanStart(ctx); // the double-tap
  await Promise.all([p1, p2]);

  assert.equal(headingText(ctx.screenEl), "All measurements done");
  assert.equal(globalThis.__wakeAcquireCalls, 1, "only the FIRST tap's session ever acquired a wake lock");
  ok();
}

// ============================================================================
// 25 (B-1, round-2 adversarial-review blocker). A re-tap of the SAME
// begin_capture button after round 1's mic-permission-denied pre-arm failure
// must re-enter capture, not dead-end behind the N3 re-entrancy guard
// (reviewer repro on the prior branch: recorderCalls delta 0, empty
// heading). The retry must also REUSE the session's existing controller and
// wake lock rather than re-acquiring them — a fresh acquire here would
// orphan the originals (the same leak class B1/N2 fixed elsewhere).
// ============================================================================
async function testRetapAfterPreArmMicDeniedReentersCaptureAndReusesSessionResources() {
  statusHistory.length = 0;
  globalThis.__recorderCalls = 0;
  globalThis.__wakeAcquireCalls = 0;
  const { onPlanStart } = await loadModule();
  // Round 1's first attempt: mic permission denied — the canonical, most
  // common first-run pre-arm failure.
  globalThis.__recorderError = new Error("Permission denied");
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  // target: 2 (not 1) so the retry's success lands on a NON-terminal
  // "Measurement 1 of 2" screen — that lets us check ctx.wakeLock's identity
  // BEFORE the session's own end-of-plan teardown nulls it out, which a
  // target-of-1 plan would already have done by the time this function
  // could inspect it.
  const spec = planSpec({ target: 2, maxAttempts: 3 });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 3 });
  const ctx = makeCtx(spec, client);
  // The spec screen's own begin button is still the live affordance for
  // round 1 — give ctx.captureRefs the same shape boot's renderScreen
  // produces so planRetryAffordance can name it (mirrors
  // testPreArmFailureKeepsRetryLiveAndStopWired's setup).
  const beginButton = makeNode("button");
  beginButton.textContent = "I've positioned the mic — measure Woofer driver";
  ctx.captureRefs = { buttons: [{ action: "begin_capture", el: beginButton }], levelMeters: [] };

  await onPlanStart(ctx);

  const firstFailureStatus = statusHistory[statusHistory.length - 1];
  assert.ok(
    firstFailureStatus.includes("Tap I've positioned the mic — measure Woofer driver to try again"),
    `expected the pre-arm retry copy, got: ${firstFailureStatus}`,
  );
  assert.equal(globalThis.__recorderCalls, 1, "the first attempt DID try to open the mic");
  assert.equal(globalThis.__wakeAcquireCalls, 1, "the session's own wake lock was acquired once");
  assert.ok(!ctx.recorder, "no recorder survives a failed acquisition");
  assert.ok(ctx.planController, "the session controller is still the live one");
  const priorController = ctx.planController;
  const priorWakeLock = ctx.wakeLock;
  assert.ok(priorWakeLock, "the session's wake lock is still held");

  // The household fixes the mic (grants permission / plugs in a working
  // one) and re-taps the SAME begin_capture button.
  globalThis.__recorderError = null;
  globalThis.__recorder = makeRecorder();
  await onPlanStart(ctx);

  assert.equal(
    headingText(ctx.screenEl), "Measurement 1 of 2 ✓",
    "the re-tap re-entered capture and the session advanced (round 1 accepted)",
  );
  assert.equal(globalThis.__recorderCalls, 2, "the retry DID attempt to open the mic again");
  assert.equal(globalThis.__wakeAcquireCalls, 1, "the retry reused the session's existing wake lock — no re-acquire");
  assert.equal(ctx.planController, priorController, "the retry reused the existing controller, not a fresh one");
  assert.equal(ctx.wakeLock, priorWakeLock, "the retry reused the existing wake-lock sentinel, not a fresh one");

  // Finish the plan to confirm the session keeps advancing normally from
  // here — not just that the retry's own round was accepted.
  const next = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await next._listeners.click[0]();
  assert.equal(headingText(ctx.screenEl), "All measurements done");
  ok();
}

// ============================================================================
// 26 (N-1, round-3 review). A re-tap of the SAME begin_capture button after
// the HOST cancels the sweep (sweep_cancelled — a TRUE session end via
// endPlanSession, unlike the pre-arm-failure "park") must stay fully inert:
// no fresh wake-lock acquire, no fresh controller. sweep_cancelled never
// re-renders the screen (only setStatus runs), so the original button is
// still there to be re-tapped. Pins the simplified guard
// (`if (ctx.planController && !isRetry) return;`, isRetry no longer also
// checking !ctx.sessionEnded) against exactly the fall-through the reviewer
// found: parkedAtRetriableFailure is never set for this path, so isRetry
// stays false and the tap is blocked before doing any work.
// ============================================================================
async function testRetapAfterHostCancelledSweepStaysInert() {
  statusHistory.length = 0;
  globalThis.__wakeAcquireCalls = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  // A discriminating stub: without this, hideWakeLockHint()'s own
  // getElementById("wakelock-hint") call (from releasePlanSessionResources'
  // end-of-session teardown) would return the SAME statusEl and clobber the
  // very "Measurement stopped safely…" status text this test checks.
  const hintEl = { set textContent(_v) {}, get textContent() { return ""; } };
  globalThis.document = {
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => (id === "wakelock-hint" ? hintEl : statusEl),
  };

  const spec = planSpec({ target: 1, maxAttempts: 1 });
  const client = {
    async postEvent(event) {
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        client._last = { phase: "capture_authorized", index, attempt };
      } else if (event.armed) {
        // The Pi cancels the sweep right after arming.
        client._last = { phase: "sweep_cancelled" };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: client._last || {} };
    },
    async putBlob() {
      throw new Error("must not upload after a host-cancelled sweep");
    },
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(
    statusHistory[statusHistory.length - 1],
    "Measurement stopped safely. The speaker page shows what happens next.",
  );
  assert.equal(globalThis.__wakeAcquireCalls, 1, "the initial session acquired the wake lock once");
  assert.ok(ctx.sessionEnded, "a host-cancelled sweep is a TRUE session end (via endPlanSession)");
  assert.equal(
    ctx.parkedAtRetriableFailure, false,
    "sweep_cancelled is a post-arm outcome, never a pre-arm-failure park",
  );
  assert.ok(ctx.planController, "planController is never nulled — only the session is marked ended");

  // Re-tap the SAME (never re-rendered) begin_capture button.
  await onPlanStart(ctx);

  assert.equal(
    globalThis.__wakeAcquireCalls, 1,
    "the re-tap after a true session end stays fully inert — no fresh wake-lock acquire",
  );
  ok();
}

// ============================================================================
// 27 (N-2, round-3 review). Two overlapping reacquireSessionWakeLock calls
// (a rapid visibility flicker firing the callback twice before the first
// acquire settles) must coalesce to a SINGLE acquire — the in-flight latch
// (ctx.reacquiringWakeLock) makes the second, overlapping call a no-op
// rather than letting both race to overwrite ctx.wakeLock and orphan one
// sentinel.
// ============================================================================
async function testConcurrentReacquireCallsCoalesceToOneAcquireNoOrphan() {
  statusHistory.length = 0;
  globalThis.__wakeAcquireCalls = 0;
  globalThis.__wakeReleaseCalls = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const statusEl = makeStatusEl();
  globalThis.document = { createElement: (tag) => makeNode(tag), getElementById: () => statusEl };

  const spec = planSpec({ target: 2, maxAttempts: 2 });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 2 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(headingText(ctx.screenEl), "Measurement 1 of 2 ✓");
  assert.equal(globalThis.__wakeAcquireCalls, 1, "the session's own initial acquire");
  const priorLock = ctx.wakeLock;
  assert.ok(priorLock);
  assert.equal(typeof globalThis.__wakeReacquireCallback, "function");

  // Gate the NEXT acquire so the test can catch it mid-flight, then fire the
  // reacquire callback TWICE in a row — a rapid hide/show/hide/show.
  globalThis.__wakeAcquireGateReached = false;
  let releaseGate;
  globalThis.__wakeAcquireGate = new Promise((resolve) => { releaseGate = resolve; });
  globalThis.__wakeReacquireCallback();
  for (let i = 0; i < 200 && !globalThis.__wakeAcquireGateReached; i += 1) {
    await Promise.resolve();
  }
  assert.ok(globalThis.__wakeAcquireGateReached, "test setup reached the pending first reacquire");
  assert.equal(globalThis.__wakeAcquireCalls, 2, "the first reacquire's request was made");

  // The SECOND callback fires while the first is still in flight — the
  // in-flight latch must make this an immediate no-op (no second acquire
  // attempt is even started).
  globalThis.__wakeReacquireCallback();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(globalThis.__wakeAcquireCalls, 2, "the overlapping second call did not start a second acquire");

  releaseGate();
  for (let i = 0; i < 200 && ctx.wakeLock === priorLock; i += 1) {
    await Promise.resolve();
  }

  assert.notEqual(ctx.wakeLock, priorLock, "the first reacquire's fresh lock is now held");
  assert.equal(globalThis.__wakeAcquireCalls, 2, "exactly one reacquire total, coalesced from two callback fires");
  assert.equal(
    globalThis.__wakeReleaseCalls, 1,
    "exactly the ORIGINAL prior sentinel was released — nothing left orphaned",
  );

  globalThis.__wakeAcquireGate = null;
  ok();
}

// ============================================================================
// 28-37. The flow-simplification UX redesign (PR-U2,
// docs/flat-linearization-flow-simplification-plan.md §§2.1-2.6): the
// one-instruction-per-step grammar, voluntary retakes and their window, the
// group-close confirm, and VERIFY's confirm-then-tone.
// ============================================================================

// A miniature of the express plan the Pi builds (build_v2_capture_plan): CHECK
// (tap), one prompted cloud position (tap), VERIFY (on_apply, with the NEW
// confirm_* keys and the done screen an M=1 plan moves onto it). Same screen
// vocabulary as production, three captures instead of seven.
const CLOUD_HEADLINE = "A hand’s width LEFT of the mark";
const VERIFY_CONFIRM_HEADLINE = "Back on the mark, holding still?";

function guidedPlanSpec({ target = 3, maxAttempts = 6, verifyConfirm = true } = {}) {
  const verifyScreen = {
    progress: `Measurement ${target} of ${target}`,
    title: "Applying",
    body: "Applying the measured crossover. Put the phone back on the mark.",
    auto_advance: "on_apply",
    done_title: "Your speaker is tuned",
    done_body: "Confirmed at the mark and applied.",
  };
  if (verifyConfirm) {
    verifyScreen.confirm_title = VERIFY_CONFIRM_HEADLINE;
    verifyScreen.confirm_body = "Same spot, same height, pointed at the speaker.";
  }
  return planSpec({
    target,
    maxAttempts,
    entries: [
      {
        index: 0,
        kind_label: "check",
        duration_ms: 25000,
        screen: {
          progress: `Measurement 1 of ${target}`,
          title: "Stand the phone about 1 m in front of the speaker.",
          body: "Stay quiet — JTS listens to the room first.",
          auto_advance: "tap",
        },
      },
      {
        index: 1,
        kind_label: "cloud_measure",
        duration_ms: 16000,
        screen: {
          progress: `Measurement 2 of ${target}`,
          title: CLOUD_HEADLINE,
          body: "Same height, still pointed at the speaker.",
          auto_advance: "tap",
        },
      },
      { index: 2, kind_label: "verify", duration_ms: 16000, screen: verifyScreen },
    ],
  });
}

function primaryButton(ctx) {
  const entry = (ctx.captureRefs.buttons || []).find((b) => b.action === "begin_capture");
  return entry ? entry.el : null;
}

// Drain macrotasks (scheduleAutoBegin's setTimeout) as well as microtasks
// until the screen reaches the expected state — the auto-posted VERIFY begin
// runs OUTSIDE the promise the test awaited, so there is nothing to await.
async function waitFor(predicate, label, rounds = 600) {
  for (let i = 0; i < rounds; i += 1) {
    if (predicate()) return;
    // 5 ms rather than 0: one scenario waits on a real 1 s countdown interval
    // (the auto-begin), and a zero-delay spin never lets the clock advance far
    // enough. Everything else resolves in a poll or two either way.
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`timed out waiting for ${label}`);
}

// ============================================================================
// 28 (§2.1). Every step screen renders ONE grammar: the counter as a small
// eyebrow, the instruction as the headline, one supporting clause, a single
// full-width primary whose label confirms the placement, and Stop demoted to
// a text link. `#status` stops carrying counters — it used to number the same
// walk a second time, and disagree.
// ============================================================================
async function testStepScreenRendersTheOneInstructionGrammar() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(eyebrowText(ctx.screenEl), "Measurement 2 of 3");
  assert.equal(headingText(ctx.screenEl), CLOUD_HEADLINE);
  assert.equal(noteText(ctx.screenEl), "Same height, still pointed at the speaker.");
  assert.deepEqual(actionLabels(ctx.screenEl), [
    "I’m there — play the tone",
    "Retake this measurement",
  ]);
  const stop = stopLink(ctx.screenEl);
  assert.ok(stop, "Stop is present on every step screen");
  assert.equal(stop.textContent, "Stop measuring");
  assert.equal(stop.className, "cap-stop-link", "Stop is a text link, not a danger button");
  // The eyebrow is the ONLY counter now.
  assert.ok(
    !statusHistory.some((s) => /Measurement \d+ of \d+/.test(String(s))),
    `#status must not count the walk: ${JSON.stringify(statusHistory)}`,
  );
  assert.equal(statusHistory[statusHistory.length - 1], "");
  ok();
}

// ============================================================================
// 29 (§2.6). A voluntary retake re-measures the capture that JUST completed:
// the begin carries the `retake` marker (the ONLY shape the runner admits for
// an already-accepted index), the armed event re-states it, and acceptance
// REPLACES rather than counting — the set still needs its full target.
// ============================================================================
async function testRetakeRepeatsTheJustAcceptedSlotWithTheMarker() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client, posted, blobPuts } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  const retake = actionButtons(ctx.screenEl)[1];
  assert.equal(retake.textContent, "Retake this measurement");
  assert.ok(
    String(retake.className).includes("cap-button--secondary"),
    "the retake control is the quieter secondary, never the primary",
  );
  await fire(retake);

  const begins = posted.filter((e) => e.begin_capture && !e.armed);
  assert.deepEqual(
    begins.map((e) => [e.begin_capture.index, e.begin_capture.attempt, e.begin_capture.retake]),
    [[1, 1, undefined], [1, 2, true]],
    "the retake re-uses the accepted index on the next attempt, marked",
  );
  const armed = posted.filter((e) => e.armed);
  assert.equal(armed[armed.length - 1].begin_capture.retake, true);
  assert.deepEqual(blobPuts.map((b) => b.captureIndex), [0, 1]);

  // Accepted — the flow moves on to the NEXT entry, and the retaken slot is
  // still retakeable (a household can be unhappy twice).
  assert.equal(headingText(ctx.screenEl), CLOUD_HEADLINE);
  await fire(primaryButton(ctx));
  const afterNext = posted.filter((e) => e.begin_capture && !e.armed).slice(-1)[0];
  assert.deepEqual(
    [afterNext.begin_capture.index, afterNext.begin_capture.attempt],
    [2, 3],
    "an accepted retake did not consume a capture slot",
  );
  ok();
}

// ============================================================================
// 30 (§2.6, review finding N4). THE RETAKE WINDOW. The runner shuts its own
// window the moment work moves on — the next entry's begin, or (work order D1)
// the household's set-completion signal — and refuses a later retake as
// `begin_out_of_order`, killing the session. So the page must never offer (or
// honour) a retake past that point: the control disappears with the screen,
// and a tap on a node captured beforehand is inert.
//
// Driven on a plan that HAS an entry past the group, so this is also the
// legacy-conductor half of the release-ordering contract: the confirmation
// rides the next begin there. Test 55 below drives the measure-only half.
// ============================================================================
async function testRetakeWindowShutsOnceTheNextBeginIsPosted() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client, posted } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    // The prompted cloud position closes the group but does NOT close it out:
    // the Pi stashes the combine and waits for a begin past the group.
    resultFor: (index) =>
      index === 2
        ? { accepted: true, group_complete: "cloud_measure", awaiting_confirm: true }
        : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx));

  // The group-close confirm (§2.6): the final prompted position is accepted
  // but retakeable, because the fit + apply have not run yet.
  assert.equal(headingText(ctx.screenEl), "All spots measured — ready to continue?");
  assert.deepEqual(actionLabels(ctx.screenEl), ["Continue", "Retake this measurement"]);
  const staleRetake = actionButtons(ctx.screenEl)[1];

  await fire(primaryButton(ctx)); // Continue -> the next entry's begin
  await waitFor(
    () => posted.some((e) => e.begin_capture && !e.armed && e.begin_capture.index === 3),
    "the confirming begin",
  );
  assert.ok(
    !posted.some((e) => e.complete_capture_set === true),
    "a plan WITH a next entry confirms through that begin, not the new signal",
  );
  assert.equal(ctx.retakeSlot, null, "the window shuts the moment work moves on");

  const before = posted.length;
  await fire(staleRetake);
  assert.equal(
    posted.length,
    before,
    "a retake tap that outlived the window posts nothing (it could only end the session)",
  );
  ok();
}

// ============================================================================
// 31 (§2.6). Retakes ride the slot's EXISTING attempt budget, so the control
// disappears when that budget cannot cover another attempt — rather than
// offering a tap the Pi would refuse.
// ============================================================================
async function testRetakeIsNotOfferedWhenTheAttemptBudgetIsSpent() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec({ target: 2, maxAttempts: 2 });
  const { client } = makeFakePlanClient({
    target: 2,
    maxAttempts: 2,
    resultFor: (index, attempt) =>
      attempt === 1 ? { accepted: false, reason: "Too noisy." } : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx)); // the failure retry consumes attempt 2 of 2

  assert.deepEqual(
    actionLabels(ctx.screenEl),
    ["I’m there — play the tone"],
    "no retake offer once the plan's own attempt budget is spent",
  );
  assert.equal(ctx.retakeSlot, null);
  ok();
}

// ============================================================================
// 32 (§2.6). The group-close confirm's Continue is what moves work on — the
// tap the Pi's fit waits behind. On a plan that carries an entry PAST the
// group (an older conductor's 16-entry shape) that move is still the next
// begin, which is the compatibility half of the release-ordering contract:
// the page publishes before the Pi and must be correct against both.
// ============================================================================
async function testGroupConfirmContinueAdvancesWhenTheresAnEntryPastTheGroup() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client, posted } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    resultFor: (index) =>
      index === 2 ? { accepted: true, awaiting_confirm: true } : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx));

  // Nothing has been posted for the next entry yet — the confirm is the gate.
  assert.ok(
    !posted.some((e) => e.begin_capture && e.begin_capture.index === 3),
    "the group stays open until the household confirms",
  );
  await fire(primaryButton(ctx));
  await waitFor(
    () => posted.some((e) => e.begin_capture && !e.armed && e.begin_capture.index === 3),
    "the confirming begin",
  );
  assert.ok(
    !posted.some((e) => e.complete_capture_set === true),
    "an entry past the group means the begin still carries the confirmation",
  );
  ok();
}

// ============================================================================
// 33 (§2.2, the step-11 fix). VERIFY is BEGIN-FIRST, THEN CONFIRM: the begin
// posts immediately (each deferred re-post re-arms the host's hold clock —
// sitting tap-first would hit REVIEW_HOLD_BUDGET_S and kill the session), the
// hold screen instructs the walk back, and the tone waits for the tap. The
// page arms no timer of its own while it waits: the budget is the host's
// `awaiting_arm` phase (120 s), which is what makes a 60 s walk back safe.
// ============================================================================
async function testVerifyArmsOnlyAfterTheHouseholdConfirms() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client, posted } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx));
  await waitFor(
    () => headingText(ctx.screenEl) === VERIFY_CONFIRM_HEADLINE,
    "the post-apply confirmation screen",
  );

  // The begin is already in (liveness); the TONE is not.
  assert.ok(
    posted.some((e) => e.begin_capture && !e.armed && e.begin_capture.index === 3),
    "the begin posts immediately, as it always did",
  );
  assert.ok(
    !posted.some((e) => e.armed && e.begin_capture.index === 3),
    "the verify sweep must not fire before the household confirms",
  );
  assert.equal(eyebrowText(ctx.screenEl), "Measurement 3 of 3");
  assert.equal(noteText(ctx.screenEl), "Same spot, same height, pointed at the speaker.");
  assert.deepEqual(actionLabels(ctx.screenEl), ["I’m there — play the tone"]);
  assert.equal(ctx.autoAdvanceTimer ?? null, null, "no page-side deadline races the host's");
  assert.equal(ctx.autoAdvanceInterval ?? null, null);

  // A long walk back changes nothing — the wait is event-driven.
  await settle();
  assert.ok(!posted.some((e) => e.armed && e.begin_capture.index === 3));

  await fire(primaryButton(ctx));
  await waitFor(
    () => headingText(ctx.screenEl) === "Your speaker is tuned",
    "the done screen",
  );
  assert.ok(posted.some((e) => e.armed && e.begin_capture.index === 3));
  ok();
}

// ============================================================================
// 34 (§2.2, the compatibility half). An entry with no `confirm_*` keys — every
// plan built before the redesign, and every non-VERIFY entry — arms the moment
// the Pi authorizes, byte-for-byte as today. The new behavior rides new keys
// precisely so this stays true.
// ============================================================================
async function testAnEntryWithoutConfirmCopyStillAutoArms() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec({ verifyConfirm: false });
  const { client, posted } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx));
  await waitFor(
    () => headingText(ctx.screenEl) === "Your speaker is tuned",
    "the done screen (no confirmation tap involved)",
  );
  assert.ok(posted.some((e) => e.armed && e.begin_capture.index === 3));
  ok();
}

// ============================================================================
// 35 (§2.6). A REJECTED retake keeps the marker on its retry. Without it the
// retry is a begin for the just-accepted index with no marker — exactly what
// the runner refuses as out-of-order, which ends the session. The fake relay
// enforces that contract, so this fails loudly if the marker is dropped.
// ============================================================================
async function testARejectedRetakeKeepsItsMarkerOnTheRetry() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client, posted } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    resultFor: (index, attempt) =>
      attempt === 2 ? { accepted: false, reason: "A truck went past." } : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(actionButtons(ctx.screenEl)[1]); // Retake -> rejected

  assert.equal(eyebrowText(ctx.screenEl), "Measurement 1 of 3 — one more try");
  assert.equal(noteText(ctx.screenEl), "A truck went past.");
  await fire(primaryButton(ctx));

  const begins = posted.filter((e) => e.begin_capture && !e.armed);
  assert.deepEqual(
    begins.map((e) => [e.begin_capture.index, e.begin_capture.attempt, e.begin_capture.retake]),
    [[1, 1, undefined], [1, 2, true], [1, 3, true]],
  );
  assert.equal(
    headingText(ctx.screenEl),
    CLOUD_HEADLINE,
    "the retried retake was admitted and accepted, not refused",
  );
  ok();
}

// ============================================================================
// 36 (§2.3). The consent announcement is DERIVED from the plan the Pi signed —
// count and minutes both — using the same arithmetic as the wake-lock hint and
// the Pi's own consent line. A one-capture plan (the re-verify re-arm) says
// nothing rather than "1 measurements".
// ============================================================================
async function testPlanAnnouncementDerivesItsCountAndMinutes() {
  const { planAnnouncementText, planEstimatedMinutes } = await loadModule();

  // 25 + 16 + 16 s of audio, plus 20 s of allowance each -> 117 s -> 2 minutes.
  const spec = guidedPlanSpec();
  assert.equal(planEstimatedMinutes(spec), 2);
  assert.equal(planAnnouncementText(spec), "3 measurements, about 2 minutes.");

  // …but never twice. A current speaker's consent copy already opens with the
  // same derived sentence (plus the tier name the phone has no field for), so
  // the page's line stands down rather than repeating it two lines apart.
  const announced = guidedPlanSpec();
  announced.ui = {
    screen: [
      { type: "heading", text: "Tune your speaker" },
      {
        type: "steps",
        items: [
          "Quick tune: 3 measurements, about 2 minutes",
          "Put the phone about 1 m in front of the speaker",
        ],
      },
    ],
  };
  assert.equal(planAnnouncementText(announced), "");
  // A speaker whose copy quotes DIFFERENT numbers is not this sentence — the
  // page still announces the plan it was actually sent.
  const other = guidedPlanSpec();
  other.ui = { screen: [{ type: "steps", items: ["Full measurement: 16 measurements, about 11 minutes"] }] };
  assert.equal(planAnnouncementText(other), "3 measurements, about 2 minutes.");

  const reverify = planSpec({
    target: 1,
    maxAttempts: 3,
    entries: [{ index: 0, kind_label: "verify", duration_ms: 16000, screen: {} }],
  });
  assert.equal(planAnnouncementText(reverify), "");
  // No entry table (the legacy repeat-set and single-capture kinds): nothing
  // to derive from, so nothing claimed.
  assert.equal(planAnnouncementText(planSpec({ target: 3 })), "");
  assert.equal(planEstimatedMinutes(planSpec({ target: 3 })), 0);
  ok();
}

// ============================================================================
// 37 (§2.1). Stop's demotion keeps its destructiveness: the text link opens
// the page's own danger-styled <dialog>, and only its confirm button stops the
// session. (A browser with no <dialog> support fails open — every other test
// in this file runs without one and Stop acts immediately there.)
// ============================================================================
async function testStopLinkConfirmsBeforeAbandoningTheSession() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  const confirm = makeStopConfirmDialog();
  installDocument(makeStatusEl(), confirm);

  const spec = guidedPlanSpec();
  const { client, posted } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  const stop = stopLink(ctx.screenEl);

  // Dismissing the dialog (Keep measuring / ESC / backdrop) leaves the
  // session exactly as it was.
  const dismissed = fire(stop);
  assert.equal(confirm.dialog.opens, 1, "the tap opens the confirm, never stops outright");
  await fire(confirm.cancel);
  await dismissed;
  assert.ok(!posted.some((e) => e.aborted), "a cancelled confirm posts no abort");
  assert.equal(headingText(ctx.screenEl), CLOUD_HEADLINE, "the step screen survives");

  const confirmed = fire(stop);
  assert.equal(confirm.dialog.opens, 2);
  await fire(confirm.accept);
  await confirmed;
  assert.deepEqual(
    posted.filter((e) => e.aborted).map((e) => e.abort_reason),
    ["stopped"],
  );
  assert.equal(headingText(ctx.screenEl), "Measurement stopped.");
  ok();
}

// ============================================================================
// 38 (review BLOCKER B1). A REJECTED voluntary retake must have a forward
// path. The slot is ALREADY accepted, and the design's fail-safe is that a
// rejected retake leaves the original take standing — so "Try again" cannot be
// the only control, or the household re-measures something that does not need
// it and can burn the attempt budget until the session dies with the fit and
// apply never fired. "Keep the earlier measurement and continue" posts the
// ordinary forward begin, which is exactly the pair the runner expects after a
// rejected retake (accepted_count unchanged, next_begin_seen still false) —
// the fake relay enforces that, so this fails if the arithmetic is wrong.
// ============================================================================
async function testARejectedRetakeCanKeepTheEarlierTakeAndContinue() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client, posted } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    resultFor: (index, attempt) => {
      if (attempt === 3) return { accepted: false, reason: "A truck went past." };
      return index === 2
        ? { accepted: true, awaiting_confirm: true }
        : { accepted: true };
    },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx)); // capture 2 -> the group-close confirm
  assert.equal(headingText(ctx.screenEl), "All spots measured — ready to continue?");
  await fire(actionButtons(ctx.screenEl)[1]); // Retake the final position -> rejected

  assert.equal(eyebrowText(ctx.screenEl), "Measurement 2 of 3 — one more try");
  // No server move-instruction on this rejection, so the primary stays the
  // plain "Try again" — and the escape sits beside it.
  assert.deepEqual(actionLabels(ctx.screenEl), [
    "Try again",
    "Keep the earlier measurement and continue",
  ]);

  // Keep the accepted take: back to the SAME group-close confirm the retake
  // was launched from, with the offer live again (the runner's window is still
  // open and the budget still covers it).
  await fire(actionButtons(ctx.screenEl)[1]);
  assert.equal(headingText(ctx.screenEl), "All spots measured — ready to continue?");
  assert.deepEqual(actionLabels(ctx.screenEl), ["Continue", "Retake this measurement"]);

  // …and the session still completes: Continue posts the confirming begin,
  // VERIFY confirms, done. (This plan carries an entry past the group; the
  // measure-only shape's completion path is tests 55-57.)
  await fire(primaryButton(ctx));
  await waitFor(
    () => headingText(ctx.screenEl) === VERIFY_CONFIRM_HEADLINE,
    "the post-apply confirmation",
  );
  await fire(primaryButton(ctx));
  await waitFor(() => headingText(ctx.screenEl) === "Your speaker is tuned", "the done screen");

  const begins = posted.filter((e) => e.begin_capture && !e.armed);
  assert.deepEqual(
    begins.map((e) => [e.begin_capture.index, e.begin_capture.attempt, e.begin_capture.retake]),
    [[1, 1, undefined], [2, 2, undefined], [2, 3, true], [3, 4, undefined]],
    "the forward begin after the rejected retake is (accepted+1, attempts+1)",
  );
  ok();
}

// ============================================================================
// 39 (review SHOULD-FIX S2). A pre-arm failure DURING a retake must not leave
// the FORWARD primary as the live affordance: tapping it posts a different
// (index, attempt) while the Pi may be sitting in awaiting_arm on the retake,
// which the runner refuses as out-of-order — fatal. The retake offer is
// re-armed instead, so the live control re-posts the IDENTICAL pair, and the
// failure copy names that control.
// ============================================================================
async function testPreArmFailureDuringARetakeReArmsTheRetakeNotTheForwardPath() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  globalThis.__recorderError = null;
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client, posted } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  const retake = actionButtons(ctx.screenEl)[1];
  // The mic dies between rounds — the canonical pre-arm failure — so the
  // retake round fails after its begin was already admitted.
  ctx.recorder = null;
  globalThis.__recorderError = new Error("Permission denied");
  await fire(retake);
  globalThis.__recorderError = null;

  const lastStatus = statusHistory[statusHistory.length - 1];
  assert.ok(
    lastStatus.includes("Tap Retake this measurement to try again"),
    `the copy must name the retake control, got: ${lastStatus}`,
  );
  assert.equal(ctx.retakeSlot.index, 1, "the retake offer is live again");
  // …AND IT IS ON SCREEN. `ctx.retakeSlot` is bookkeeping and the `retake`
  // handle above is a node the page may since have replaced — asserting
  // either one proves the household can act only if the control is still in
  // the screen tree. It was not: PR-T4 gave a retake its own affordance-free
  // in-progress screen, and this arm left THAT up under copy naming a button
  // that was no longer rendered — action labels [] where the pre-T4 build had
  // both, with Stop the only remaining tap mid-walk. Read what is rendered.
  assert.deepEqual(
    actionLabels(ctx.screenEl),
    ["I’m there — play the tone", "Retake this measurement"],
    "the failure copy must name a control the household can actually see",
  );

  // Re-tapping it re-posts the IDENTICAL pair (the runner tolerates that),
  // never the forward one the visible primary would have posted. Re-read the
  // control off the screen for the same reason.
  const liveRetake = actionButtons(ctx.screenEl).find(
    (b) => b.textContent === "Retake this measurement",
  );
  assert.ok(liveRetake, "the re-armed retake is a rendered control");
  await fire(liveRetake);
  const begins = posted.filter((e) => e.begin_capture && !e.armed);
  assert.deepEqual(
    begins.map((e) => [e.begin_capture.index, e.begin_capture.attempt, e.begin_capture.retake]),
    [[1, 1, undefined], [1, 2, true], [1, 2, true]],
  );
  assert.ok(
    !posted.some((e) => e.begin_capture && e.begin_capture.index === 2),
    "the forward begin was never posted while the Pi awaited the retake",
  );
  ok();
}

// ============================================================================
// 40 (review SHOULD-FIX S2, second half). A countdown's auto-begin IS the
// forward path, so its screen renders no begin affordance — a pre-arm failure
// there used to leave the household on a frozen countdown with copy naming
// "the measurement button", which does not exist. Drop back to the manual
// screen the countdown's own Cancel produces, and name ITS control.
// ============================================================================
async function testPreArmFailureOnTheCountdownScreenNamesARealControl() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  globalThis.__recorderError = null;
  installDocument(makeStatusEl());

  const spec = planSpec({
    target: 3,
    maxAttempts: 6,
    entries: [
      { index: 0, kind_label: "check", duration_ms: 25000, screen: { progress: "Measurement 1 of 3", title: "On the mark", auto_advance: "tap" } },
      { index: 1, kind_label: "measure", duration_ms: 16000, screen: { progress: "Measurement 2 of 3", title: "Hold still", auto_advance: "countdown", countdown_s: "1" } },
      { index: 2, kind_label: "verify", duration_ms: 16000, screen: { progress: "Measurement 3 of 3", title: "Last one", auto_advance: "tap" } },
    ],
  });
  const { client } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.equal(headingText(ctx.screenEl), "Hold still", "the countdown owns the screen");
  assert.equal(ctx.captureRefs.buttons.length, 0, "…with no forward begin affordance");

  // The countdown's own auto-begin fires into a dead mic.
  ctx.recorder = null;
  globalThis.__recorderError = new Error("Permission denied");
  await waitFor(
    () => statusHistory.some((s) => String(s).includes("try again")),
    "the pre-arm failure copy",
  );
  globalThis.__recorderError = null;

  const named = statusHistory[statusHistory.length - 1];
  assert.ok(
    !named.includes("the measurement button"),
    `the copy must not name a button that does not exist, got: ${named}`,
  );
  const live = primaryButton(ctx);
  assert.ok(live, "the screen now carries a live begin affordance");
  assert.ok(
    named.includes(`Tap ${live.textContent} to try again`),
    `the copy names the control now on screen, got: ${named}`,
  );
  ok();
}

// ============================================================================
// 41 (review M1, TRANSFORMED for issue #2090). The offering screen is not
// replaced until the new round's verdict lands, so the Retake control it
// rendered is still on screen while the next capture runs — with its window
// shut. This used to assert the control was DISABLED there. That is what made
// the press SILENT: a disabled button fires no click, so the owner's retake
// press during the 2026-08-03 verify produced no retake AND no explanation.
// The control must now ANSWER — it may not act (posting past the window is
// refused by the runner as `begin_out_of_order`, which ends the session), and
// it may not swallow the press either.
// ============================================================================
async function testAShutRetakeWindowAnswersThePressInsteadOfSwallowingIt() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = guidedPlanSpec();
  const { client, posted } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  const retake = actionButtons(ctx.screenEl)[1];
  assert.equal(retake.disabled, false);

  await fire(primaryButton(ctx)); // the forward begin shuts the window
  const beginsBefore = posted.filter((p) => p && p.begin_capture).length;
  statusHistory.length = 0;
  await fire(retake);

  const said = statusHistory.map((s) => String(s.text || s)).join(" | ");
  assert.ok(
    /Too late to redo that spot/.test(said),
    `a shut retake window must say why, got: ${said}`,
  );
  // …and must not post the begin that would kill the session.
  assert.equal(
    posted.filter((p) => p && p.begin_capture).length,
    beginsBefore,
    "a refused retake must not reach the Pi",
  );
  ok();
}

// ============================================================================
// 42 (review M6). Tapping Retake on the countdown screen cancels the
// countdown, so its live "Starting in N…" counter stops being true — it must
// be cleared rather than frozen on screen for the whole retake.
// ============================================================================
async function testRetakeOnTheCountdownScreenClearsItsFrozenCounter() {
  statusHistory.length = 0;
  const { onPlanStart, entryForIndex } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = planSpec({
    target: 3,
    maxAttempts: 6,
    entries: [
      { index: 0, kind_label: "check", duration_ms: 25000, screen: { progress: "Measurement 1 of 3", title: "On the mark", auto_advance: "tap" } },
      { index: 1, kind_label: "measure", duration_ms: 16000, screen: { progress: "Measurement 2 of 3", title: "Hold still", auto_advance: "countdown", countdown_s: "5" } },
      { index: 2, kind_label: "verify", duration_ms: 16000, screen: { progress: "Measurement 3 of 3", title: "Last one", auto_advance: "tap" } },
    ],
  });
  assert.ok(entryForIndex(spec, 2), "fixture sanity: the countdown entry exists");
  const { client, posted } = makeFakePlanClient({ target: 3, maxAttempts: 6 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  assert.ok(
    noteTexts(ctx.screenEl).some((t) => t.includes("Starting in 5")),
    "the countdown is live before the tap",
  );
  const counter = ctx.screenEl.children.find(
    (c) => c.tagName === "P" && String(c.textContent).includes("Starting in"),
  );
  await fire(actionButtons(ctx.screenEl)[1]); // Retake, from the countdown screen

  assert.equal(counter.textContent, "", "the cancelled countdown's counter is cleared");
  const begins = posted.filter((e) => e.begin_capture && !e.armed);
  assert.deepEqual(
    begins.map((e) => [e.begin_capture.index, e.begin_capture.attempt, e.begin_capture.retake]),
    [[1, 1, undefined], [1, 2, true]],
    "the retake posts the backward begin, never the countdown's forward one",
  );
  ok();
}

// ============================================================================
// 43 (#1824 D1). ONE slow relay poll must cost one poll interval, not the
// session. Forensics from cap_lMo1I-yxZqZyQ6lr4nuZlA: capture 5 of 7 played
// fully on the Pi while a single control fetch out of ~60 exceeded
// relay-client.js's flat RELAY_CONTROL_TIMEOUT_MS — the bare await inside the
// sweep wait threw, and the whole express session died mid-tone. The blip is
// absorbed, the true phase line comes back when the relay answers, and the
// capture completes normally.
// ============================================================================
function makeSweepBlipClient({ blips = 1, error = RELAY_TIMEOUT } = {}) {
  const posted = [];
  let last = {};
  let armedPolls = 0;
  let blipsLeft = blips;
  return {
    posted,
    blipsUsed: () => blips - blipsLeft,
    async postEvent(event) {
      posted.push(event);
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "capture_authorized", index, attempt };
      } else if (event.armed) {
        last = { phase: "sweep_started" };
        armedPolls = 0;
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      if (last.phase === "sweep_started") {
        armedPolls += 1;
        // The blip lands mid-tone, AFTER the phone has rendered the sweep
        // line — the shape the forensics captured.
        if (armedPolls === 2 && blipsLeft > 0) {
          blipsLeft -= 1;
          throw error;
        }
        if (armedPolls >= 4) last = { phase: "sweep_complete" };
      }
      return { host_event: last };
    },
    async putBlob() {
      last = { phase: "capture_set_complete", accepted: 1, capture_target: 1 };
      return { ok: true };
    },
  };
}

async function testOneAbortedPollMidSweepDoesNotEndTheSession(
  error = RELAY_TIMEOUT,
) {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = planSpec({ target: 1, maxAttempts: 2 });
  const client = makeSweepBlipClient({ blips: 1, error });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(client.blipsUsed(), 1, "fixture sanity: the blip really fired");
  assert.equal(
    headingText(ctx.screenEl),
    "All measurements done",
    "a single aborted poll must not end a capture the speaker completed",
  );
  assert.ok(
    statusHistory.includes("The speaker is still measuring — reconnecting…"),
    `expected the reconnecting line, got: ${JSON.stringify(statusHistory)}`,
  );
  // The true phase line is restored once the relay answers again — otherwise
  // "reconnecting…" would sit frozen for the rest of the tone (the sweep line
  // renders once, on transition, and nothing else repaints it).
  assert.equal(
    statusHistory.filter((line) => line === "Playing the measurement tone…").length,
    2,
    "the sweep line is rendered, replaced by the blip copy, then put back",
  );
  assert.equal(
    client.posted.filter((e) => e.begin_capture && !e.armed).length,
    1,
    "the recovery re-polls the SAME capture; it never re-begins",
  );
  ok();
}

// 43b. The same scenario against the LEGACY bare-abort shape (a browser that
// ignores `abort(reason)` and raises its own DOMException). Both shapes must
// classify — the tag is the primary test, this is the fallback.
async function testTheLegacyBareAbortShapeIsAlsoAbsorbed() {
  await testOneAbortedPollMidSweepDoesNotEndTheSession(LEGACY_BARE_ABORT);
}

// ============================================================================
// 44 (#1824 D2). The armed post itself is re-sent across a connectivity abort
// rather than ending the round: the Pi is holding this exact (index, attempt)
// in `awaiting_arm`, the relay's phone-event slot is last-write-wins, and the
// recorder is already running — so the recovery happens IN PLACE, with no
// screen teardown and no second begin.
// ============================================================================
async function testArmedPostAbortRetriesTheSameCaptureInPlace() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = planSpec({ target: 1, maxAttempts: 2 });
  const posted = [];
  let last = {};
  let armedPosts = 0;
  const client = {
    async postEvent(event) {
      if (event.armed) {
        armedPosts += 1;
        if (armedPosts === 1) {
          // The POST landed in the relay's slot or it did not — the phone
          // cannot tell. Re-posting the identical event is the only way to
          // use the slot the speaker is holding open.
          throw RELAY_TIMEOUT;
        }
        last = { phase: "sweep_complete" };
      } else if (event.begin_capture) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "capture_authorized", index, attempt };
      }
      posted.push(event);
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: last };
    },
    async putBlob() {
      last = { phase: "capture_set_complete", accepted: 1, capture_target: 1 };
      return { ok: true };
    },
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(armedPosts, 2, "the armed event is re-sent once after the abort");
  assert.equal(
    headingText(ctx.screenEl),
    "All measurements done",
    "a blip on the armed post is recoverable, never the terminal screen",
  );
  assert.equal(
    posted.filter((e) => e.begin_capture && !e.armed).length,
    1,
    "the retry re-posts `armed`, never a second begin for a consumed slot",
  );
  const armed = posted.filter((e) => e.armed);
  assert.deepEqual(
    armed.map((e) => [e.begin_capture.index, e.begin_capture.attempt]),
    [[1, 1]],
    "the re-post carries the SAME (index, attempt)",
  );
  ok();
}

// ============================================================================
// 44b (#1824 S3). When the RESULT window is spent entirely blind on swallowed
// connectivity errors, the terminal must name the outage — not guess that the
// speaker "did not respond with a result", which is a verdict about a speaker
// we could not hear from. Same honesty the sweep wait's expiry already gives.
async function testResultWaitSpentBlindReportsTheOutageNotAVerdict() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  // A short window so the blind wait resolves inside the harness.
  const spec = planSpec({ target: 1, maxAttempts: 2 });
  spec.duration_ms = 1000;
  let last = {};
  let uploaded = false;
  const client = {
    async postEvent(event) {
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "capture_authorized", index, attempt };
      } else if (event.armed) {
        last = { phase: "sweep_complete" };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      // Everything after the upload is blind: the relay stopped answering
      // exactly when the speaker started analyzing.
      if (uploaded) throw RELAY_TIMEOUT;
      return { host_event: last };
    },
    async putBlob() {
      uploaded = true;
      return { ok: true };
    },
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Measurement failed");
  assert.ok(
    noteText(ctx.screenEl).includes("Lost the connection"),
    `expected the outage named, got: ${noteText(ctx.screenEl)}`,
  );
  assert.ok(
    !noteText(ctx.screenEl).includes("did not respond with a result"),
    "never guess about a speaker the phone could not hear from",
  );
  // …and the household saw it happening, not just at the end.
  assert.ok(
    statusHistory.some((line) => line.includes("still checking this measurement")),
    "the result-wait blip renders its own reconnecting line",
  );
  ok();
}

// 45 (#1821, phone half). A TERMINAL `capture_result` posted while the phone
// is waiting for the tone (the Pi's play seam refusing — see
// jasper.web.correction_crossover_v2's _post_terminal_failure_host_event) is
// read and named. Until this fix the sweep wait recognized only sweep_*/
// ambient phases, polled straight through the refusal into the Pi's session
// purge, and rendered "this link expired" over a speaker that had said
// exactly why it stopped.
// ============================================================================
async function testTerminalCaptureResultMidSweepNamesTheReason() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = planSpec({ target: 1, maxAttempts: 2 });
  const reason =
    "This speaker's profile is not confirmed, so it cannot play a measurement.";
  let last = {};
  let uploads = 0;
  const client = {
    async postEvent(event) {
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "capture_authorized", index, attempt };
      } else if (event.armed) {
        const { index, attempt } = event.begin_capture;
        last = {
          phase: "capture_result",
          index,
          attempt,
          accepted: false,
          code: "program_unplayable",
          reason,
        };
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      return { host_event: last };
    },
    async putBlob() {
      uploads += 1;
      return { ok: true };
    },
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);

  assert.equal(headingText(ctx.screenEl), "Measurement failed");
  assert.notEqual(
    headingText(ctx.screenEl),
    "Link expired",
    "a named refusal must never be reported as an expired link",
  );
  assert.ok(
    noteText(ctx.screenEl).includes(reason),
    `expected the speaker's own reason, got: ${noteText(ctx.screenEl)}`,
  );
  assert.equal(uploads, 0, "a refused capture never uploads a sweep-less window");
  ok();
}

// ============================================================================
// 46 (#1821, the matching hazard). The relay's host-event slot is
// last-write-wins and nothing clears it when the phone consumes a verdict, so
// a RETRY's first sweep poll reads the PREVIOUS attempt's rejected verdict.
// Reading that as this attempt's refusal would kill every retry — the exact
// trap waitForCaptureAuthorized documents. The stale verdict is ignored
// because its (index, attempt) is not the armed one.
// ============================================================================
async function testStaleRejectedVerdictMidSweepIsIgnored() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = planSpec({ target: 1, maxAttempts: 3 });
  const stale = {
    phase: "capture_result",
    index: 1,
    attempt: 1,
    accepted: false,
    reason: "Too quiet — move the phone closer.",
  };
  let last = {};
  let armedAttempt = 0;
  let sweepPolls = 0;
  const client = {
    async postEvent(event) {
      if (event.begin_capture && !event.armed) {
        const { index, attempt } = event.begin_capture;
        last = { phase: "capture_authorized", index, attempt };
      } else if (event.armed) {
        armedAttempt = event.begin_capture.attempt;
        sweepPolls = 0;
        // Attempt 2 arms with the PREVIOUS attempt's rejection still sitting
        // in the slot; attempt 1 runs normally.
        last = armedAttempt === 1 ? { phase: "sweep_complete" } : stale;
      }
      return { ok: true };
    },
    async fetchPhoneStatus() {
      if (last === stale) {
        sweepPolls += 1;
        if (sweepPolls >= 2) last = { phase: "sweep_complete" };
        return { host_event: stale };
      }
      return { host_event: last };
    },
    async putBlob(blob, plaintextLen, sha256, captureIndex) {
      last = captureIndex === 0
        ? { ...stale }
        : { phase: "capture_set_complete", accepted: 1, capture_target: 1 };
      return { ok: true };
    },
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  // Attempt 1 was rejected: the page offers the same slot again.
  assert.equal(headingText(ctx.screenEl), "Take that measurement again");
  const retry = ctx.captureRefs.buttons.find((b) => b.action === "begin_capture").el;
  await retry._listeners.click[0]();

  assert.equal(
    headingText(ctx.screenEl),
    "All measurements done",
    "the retry must survive the stale verdict its first sweep poll reads",
  );
  ok();
}

// ============================================================================
// 47 (#1823 / #1824 D5). The auto-advance countdown blanks its own counter on
// the way into the capture. The owner's field run saw "Starting in 1…" still
// on screen while measurement 2 was audibly playing: the countdown screen's
// notes survive into the round (the round's own progress goes to #status), so
// nothing else ever repainted it. The cancel/retake path already cleared it;
// the auto-begin path did not.
// ============================================================================
async function testAutoBeginClearsTheCountdownCounter() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = planSpec({
    target: 2,
    maxAttempts: 3,
    entries: [
      { index: 0, kind_label: "check", duration_ms: 25000, screen: { title: "On the mark", auto_advance: "tap" } },
      {
        index: 1,
        kind_label: "measure",
        duration_ms: 16000,
        // 1 s so the harness waits one real interval tick for the auto-begin
        // rather than the shipped 5.
        screen: { title: "Hold still", auto_advance: "countdown", countdown_s: "1" },
      },
    ],
  });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 3 });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  const counter = ctx.screenEl.children.find(
    (c) => c.tagName === "P" && String(c.textContent).includes("Starting in"),
  );
  assert.ok(counter, "fixture sanity: the countdown rendered its counter");

  await new Promise((resolve) => setTimeout(resolve, 1200));
  await settle();

  assert.equal(
    counter.textContent,
    "",
    "the elapsed countdown blanks its counter before the capture starts",
  );
  assert.equal(ctx.autoAdvanceInterval, null, "and leaves no live interval behind");
  ok();
}

// ============================================================================
// 55 (two-stage work order D1). **THE ORDERING PIN.** In a MEASURE-ONLY plan
// the final cloud position IS `capture_target`, so `verdict.accepted && index
// >= target` is true on the very capture whose verdict says the group is still
// open. If the completion branch is tested first — as it was before PR-T3 —
// the household reads "All measurements done" instead of the confirm screen
// the whole apply decision rests on. `awaitingConfirm` must win.
// ============================================================================
function measureOnlyPlanSpec({ target = 3, maxAttempts = 6 } = {}) {
  // A stage-1 shape in miniature: CHECK, cloud positions, and NOTHING after —
  // the last entry is a cloud position and carries no done copy, exactly as
  // build_v2_capture_plan emits it.
  const entries = [
    {
      index: 0,
      kind_label: "check",
      duration_ms: 25000,
      screen: {
        progress: `Measurement 1 of ${target}`,
        title: "Stand the phone about 1 m in front of the speaker.",
        body: "Stay quiet — JTS listens to the room first.",
        auto_advance: "tap",
      },
    },
  ];
  for (let i = 1; i < target; i += 1) {
    entries.push({
      index: i,
      kind_label: "cloud_measure",
      duration_ms: 16000,
      screen: {
        progress: `Measurement ${i + 1} of ${target}`,
        title: CLOUD_HEADLINE,
        body: "Same height, still pointed at the speaker.",
        auto_advance: "tap",
      },
    });
  }
  return planSpec({ target, maxAttempts, entries });
}

async function testTheFinalHeldCaptureRendersTheConfirmNotAllDone() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = measureOnlyPlanSpec();
  const { client, posted } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    // The LAST index of the plan — which is also its target — closes the group
    // and holds it open for the household.
    resultFor: (index) =>
      index === 3
        ? { accepted: true, group_complete: "cloud_measure", awaiting_confirm: true }
        : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx));
  await fire(primaryButton(ctx));

  assert.equal(
    headingText(ctx.screenEl),
    "All spots measured — ready to continue?",
    "the confirm screen wins over the completion branch on the target capture",
  );
  assert.notEqual(headingText(ctx.screenEl), "All measurements done");
  assert.deepEqual(actionLabels(ctx.screenEl), ["Continue", "Retake this measurement"]);
  // Nothing has been signalled yet: the decision is the household's.
  assert.ok(!posted.some((e) => e.complete_capture_set === true));
  ok();
}

// ============================================================================
// 56 (D1). Continue posts the signal, the page waits for the Pi to actually
// close the set, and only THEN shows the end screen. Waiting matters: the Pi
// runs the session's slowest analysis (the group combine plus the fit) inside
// that window, and a page that claimed "done" immediately would be claiming a
// candidate exists before one does.
// ============================================================================
async function testContinueSignalsThenWaitsForTheSetToClose() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = measureOnlyPlanSpec();
  const { client, posted, completions } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    resultFor: (index) =>
      index === 3 ? { accepted: true, awaiting_confirm: true } : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx));
  await fire(primaryButton(ctx));
  await fire(primaryButton(ctx)); // Continue

  await waitFor(
    () => headingText(ctx.screenEl) === "All measurements done",
    "the end screen",
  );
  assert.equal(completions(), 1, "signalled exactly once");
  assert.ok(
    !posted.some((e) => e.begin_capture && e.begin_capture.index === 4),
    "no begin past the target — the runner would refuse one",
  );
  ok();
}

// ============================================================================
// 57 (D1; strengthened after the PR-T3 gate's blocker B1). A REFUSED group
// close is TERMINAL and must render as such.
//
// The Pi publishes the refusal and RE-RAISES: by the time the phone reads it,
// the failure is persisted, the volume abandoned and the relay session purged.
// The first version of this screen folded `capture_refused` into the generic
// failure bucket, so "JTS could not stand behind this correction" rendered
// under a move-the-mic headline with a live "Try again" that could only post
// into a purged session. The assertions here are therefore POSITIVE about the
// terminal (a negative pair is satisfied by the wrong screen too): the
// refusal's own heading, its sentence, and the ABSENCE of any begin
// affordance.
// ============================================================================
async function testARefusedGroupCloseSurfacesRatherThanHanging() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const spec = measureOnlyPlanSpec();
  const { client } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    resultFor: (index) =>
      index === 3 ? { accepted: true, awaiting_confirm: true } : { accepted: true },
  });
  const realPost = client.postEvent;
  client.postEvent = async function (event) {
    const out = await realPost.call(this, event);
    if (event.complete_capture_set === true) {
      // The Pi refused ON the confirmation instead of closing the set.
      this.__refuse = true;
    }
    return out;
  };
  const realStatus = client.fetchPhoneStatus;
  client.fetchPhoneStatus = async function () {
    if (this.__refuse) {
      return {
        host_event: {
          phase: "capture_refused",
          code: "correction_unaccountable",
          error: "JTS could not stand behind this correction.",
        },
      };
    }
    return realStatus.call(this);
  };
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx));
  await fire(primaryButton(ctx));
  await fire(primaryButton(ctx)); // Continue

  await waitFor(
    () => headingText(ctx.screenEl) === "Measurement refused",
    "the terminal refusal screen",
  );
  // The predicate's own sentence reaches the household…
  assert.ok(
    noteTexts(ctx.screenEl).some((t) => t.includes("could not stand behind")),
    "the refusal's own words render",
  );
  // …on the TERMINAL screen, which offers nothing to tap into a dead session.
  // Asserted against what is actually ON SCREEN rather than ctx bookkeeping:
  // every terminal in this page replaces the screen wholesale (setScreen), so
  // the household's only affordance is the "Back to speaker" link.
  assert.deepEqual(actionLabels(ctx.screenEl), []);
  assert.equal(
    ctx.screenEl.children.filter((c) => c.tagName === "BUTTON").length,
    0,
    "a purged session is offered no button of any kind",
  );
  assert.notEqual(headingText(ctx.screenEl), "All measurements done");
  ok();
}

// ============================================================================
// 58 (D10, PR-T4). The group-confirm's DETAIL line is the last thing a
// household reads before the interlude, so it has to set the interlude up.
//
// It branches on the same predicate the tap itself branches on — is there an
// entry past this group? — which is what keeps the page correct against BOTH
// conductors. The page publishes first (README "Release order"), so a new
// bundle legitimately meets an old conductor whose plan still carries VERIFY
// past the group, and there JTS really does tune next.
// ============================================================================
async function testTheConfirmDetailSaysWhoDecidesNext() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  // Measure-only (the shipped stage-1 shape): the final position IS the
  // target, so nothing is tuned next — the household decides.
  const spec = measureOnlyPlanSpec();
  const { client } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    resultFor: (index) =>
      index === 3
        ? { accepted: true, group_complete: "cloud_measure", awaiting_confirm: true }
        : { accepted: true },
  });
  const ctx = makeCtx(spec, client);
  await onPlanStart(ctx);
  await fire(primaryButton(ctx));
  await fire(primaryButton(ctx));

  const detail = noteTexts(ctx.screenEl).join(" ");
  assert.ok(
    !detail.includes("JTS tunes the speaker next"),
    "a measure-only plan must not promise a tune it deliberately will not do",
  );
  assert.match(detail, /you decide what to do about it/i);
  assert.match(detail, /speaker page/i);
  // The retake escape hatch survives — it is the reason this screen exists.
  assert.match(detail, /Retake this spot first/);

  // …and the LEGACY shape (an entry past the group) keeps the true sentence.
  statusHistory.length = 0;
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());
  const legacy = measureOnlyPlanSpec({ target: 3, maxAttempts: 6 });
  legacy.capture_plan.capture_target = 2;
  const legacyClient = makeFakePlanClient({
    target: 2,
    maxAttempts: 6,
    resultFor: (index) =>
      index === 2
        ? { accepted: true, group_complete: "cloud_measure", awaiting_confirm: true }
        : { accepted: true },
  });
  const legacyCtx = makeCtx(legacy, legacyClient.client);
  await onPlanStart(legacyCtx);
  await fire(primaryButton(legacyCtx));

  assert.equal(
    headingText(legacyCtx.screenEl), "All spots measured — ready to continue?",
  );
  assert.ok(
    noteTexts(legacyCtx.screenEl).join(" ").includes("JTS tunes the speaker next"),
    "with an entry past the group the old sentence is the true one",
  );
  ok();
}

// ============================================================================
// 59 (owner field note, 2026-07-29). A retake NAMES THE STEP BEING RETAKEN.
//
// "After pressing retry, the top-of-screen copy describes the next action
// rather than the step being retried." Every screen that offers a retake is
// about a different index than the retake itself — `renderPlanNext` shows the
// UPCOMING position's instruction — and `runPlanCapture` paints no screen of
// its own, so the household re-measured position N while reading position
// N+1's instruction.
// ============================================================================
async function testARetakeRendersTheStepItIsRetakingNotTheNextOne() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  installDocument(makeStatusEl());

  const entries = [0, 1].map((i) => ({
    index: i,
    kind_label: "cloud_measure",
    duration_ms: 17000,
    screen: {
      progress: `Measurement ${i + 1} of 2`,
      title: i === 0 ? "SPOT ONE" : "SPOT TWO",
      auto_advance: "tap",
    },
  }));
  const spec = planSpec({ target: 2, maxAttempts: 4, entries });
  const { client } = makeFakePlanClient({ target: 2, maxAttempts: 4 });
  const ctx = makeCtx(spec, client);

  // The heading AT THE MOMENT THE MICROPHONE STARTS is the one the household
  // is actually reading while they hold the mic — after the round completes
  // the page has legitimately moved on, so a post-hoc read would prove
  // nothing about the window the defect lives in.
  const headingsAtRecord = [];
  const recorder = globalThis.__recorder;
  const start = recorder.start.bind(recorder);
  recorder.start = () => {
    headingsAtRecord.push(headingText(ctx.screenEl));
    return start();
  };

  await onPlanStart(ctx);
  // The offering screen is about the UPCOMING spot — that is the whole trap.
  assert.equal(headingText(ctx.screenEl), "SPOT TWO");
  const retake = actionButtons(ctx.screenEl).find(
    (b) => b.textContent === "Retake this measurement",
  );
  assert.ok(retake, "the retake offer is on screen");

  await fire(retake);

  // The retake's own recording window read SPOT ONE — the slot actually being
  // re-measured — where before it read the next position's instruction.
  assert.ok(headingsAtRecord.length >= 2, headingsAtRecord.join(" | "));
  assert.equal(
    headingsAtRecord[headingsAtRecord.length - 1], "SPOT ONE",
    "the retake screen must name the step being retaken, not the next one",
  );
  ok();
}

// ============================================================================
// 60 (PR-T4 gate blocker B1). The same repair, from the GROUP-CLOSE CONFIRM —
// the worst place to strand a household, because the whole apply decision sits
// behind that screen's Continue.
//
// A retake of the cloud's FINAL position is offered by `renderPlanGroupConfirm`,
// not `renderPlanNext`, so the screen the repair has to put back is a different
// one. `ctx.retakeAwaitingConfirm` is what tells them apart — the same flag
// `keepEarlierTakeControl` already routes on.
// ============================================================================
async function testPreArmFailureDuringAFinalPositionRetakeRestoresTheConfirm() {
  statusHistory.length = 0;
  const { onPlanStart } = await loadModule();
  globalThis.__recorder = makeRecorder();
  globalThis.__recorderError = null;
  installDocument(makeStatusEl());

  const spec = measureOnlyPlanSpec();
  const { client, posted } = makeFakePlanClient({
    target: 3,
    maxAttempts: 6,
    resultFor: (index) =>
      index === 3
        ? { accepted: true, group_complete: "cloud_measure", awaiting_confirm: true }
        : { accepted: true },
  });
  const ctx = makeCtx(spec, client);

  await onPlanStart(ctx);
  await fire(primaryButton(ctx));
  await fire(primaryButton(ctx));
  assert.equal(headingText(ctx.screenEl), "All spots measured — ready to continue?");

  const retake = actionButtons(ctx.screenEl).find(
    (b) => b.textContent === "Retake this measurement",
  );
  assert.ok(retake, "the final position is retakeable at the confirm");

  ctx.recorder = null;
  globalThis.__recorderError = new Error("Permission denied");
  await fire(retake);
  globalThis.__recorderError = null;

  // Back on the confirm, with BOTH its controls — the decision the whole
  // two-stage flow rests on is reachable again, and the copy names a control
  // that is on the screen.
  assert.equal(headingText(ctx.screenEl), "All spots measured — ready to continue?");
  assert.deepEqual(actionLabels(ctx.screenEl), ["Continue", "Retake this measurement"]);
  assert.ok(
    statusHistory[statusHistory.length - 1].includes(
      "Tap Retake this measurement to try again",
    ),
  );
  // …and no forward begin was posted while the Pi may still await the retake.
  assert.ok(
    !posted.some((e) => e.begin_capture && e.begin_capture.index === 4),
    "the repair never posts past the held group",
  );
  ok();
}

const tests = [
  testFullAcceptedRoundTripEndsAllDone,
  testRejectedResultOffersTryAgainSameSlot,
  testGeometryRetakeRendersTheServerSuppliedGuidance,
  testTheRetryEyebrowCountsThisPositionsExtraTries,
  testAnUnresolvedPositionSaysSoInsteadOfTicking,
  testRejectionCopyFallsBackWhenThePiSendsNoGuidance,
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
  testTheSpeakerEndedItIsNotCalledAnExpiry,
  testEveryBeginCarriesTheAppliedCalibrationAndNeverClobbersAnExplicitChoice,
  testSessionWideResourcesAcquiredOnceReleasedOnce,
  testWakeLockHintShowsWhenUnsupportedAndClearsAtTerminal,
  testTrackEndedMidSessionReacquiresTransparently,
  testTrackEndedReacquireFailureSurfacesOnNextRound,
  testTrackEndedDuringActiveRoundFailsRatherThanUploadingDeadAir,
  testStopDuringRecorderAcquisitionClosesTheOrphanedStream,
  testStopDuringTrackEndedReacquireClosesTheOrphanedReplacement,
  testContextResumesBeforeEachRoundsRecording,
  testReacquireReleasesThePriorWakeLockSentinelBeforeOverwriting,
  testDoubleTapOnPlanStartDoesNotStartASecondSession,
  testRetapAfterPreArmMicDeniedReentersCaptureAndReusesSessionResources,
  testRetapAfterHostCancelledSweepStaysInert,
  testConcurrentReacquireCallsCoalesceToOneAcquireNoOrphan,
  testStepScreenRendersTheOneInstructionGrammar,
  testRetakeRepeatsTheJustAcceptedSlotWithTheMarker,
  testRetakeWindowShutsOnceTheNextBeginIsPosted,
  testRetakeIsNotOfferedWhenTheAttemptBudgetIsSpent,
  testGroupConfirmContinueAdvancesWhenTheresAnEntryPastTheGroup,
  testVerifyArmsOnlyAfterTheHouseholdConfirms,
  testAnEntryWithoutConfirmCopyStillAutoArms,
  testARejectedRetakeKeepsItsMarkerOnTheRetry,
  testPlanAnnouncementDerivesItsCountAndMinutes,
  testStopLinkConfirmsBeforeAbandoningTheSession,
  testARejectedRetakeCanKeepTheEarlierTakeAndContinue,
  testPreArmFailureDuringARetakeReArmsTheRetakeNotTheForwardPath,
  testPreArmFailureOnTheCountdownScreenNamesARealControl,
  testAShutRetakeWindowAnswersThePressInsteadOfSwallowingIt,
  testRetakeOnTheCountdownScreenClearsItsFrozenCounter,
  testOneAbortedPollMidSweepDoesNotEndTheSession,
  testTheLegacyBareAbortShapeIsAlsoAbsorbed,
  testArmedPostAbortRetriesTheSameCaptureInPlace,
  testResultWaitSpentBlindReportsTheOutageNotAVerdict,
  testTerminalCaptureResultMidSweepNamesTheReason,
  testStaleRejectedVerdictMidSweepIsIgnored,
  testAutoBeginClearsTheCountdownCounter,
  testTheFinalHeldCaptureRendersTheConfirmNotAllDone,
  testContinueSignalsThenWaitsForTheSetToClose,
  testARefusedGroupCloseSurfacesRatherThanHanging,
  testTheConfirmDetailSaysWhoDecidesNext,
  testARetakeRendersTheStepItIsRetakingNotTheNextOne,
  testPreArmFailureDuringAFinalPositionRetakeRestoresTheConfirm,
];

await runTestFunctions(tests, () => passed);
