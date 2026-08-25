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
  setText,
} from "./render.js?v=20260802-1";
import { RelayClient } from "./relay-client.js?v=20260808-1";
import { importContentKey, encryptWav } from "./crypto.js";
import {
  constraintDecision,
  verifyRealizedConstraints,
} from "./constraints.js?v=20260731-1";
import { safeReturnUrl } from "./return-url.js";
import {
  acquireWakeLock,
  watchVisibilityAbort,
  watchVisibilityReacquire,
} from "./wakelock.js";
import {
  createIntegrityWatch,
  summarizeCaptureIntegrity,
  witnessedZeroFillSplice,
  AUTO_RETAKE_MESSAGE,
  AUTO_RETAKE_SPENT_NOTE,
  AUTO_RETAKE_ZERO_FILL_REASON,
  INTEGRITY_LOST_FOCUS_MESSAGE,
  INTEGRITY_LOST_FOCUS_NOTE,
} from "./capture-integrity.js?v=20260815-5";
import { runLevelRampProtocol } from "./level-events.js?v=20260815-4";
import { inferCalibrationModel } from "./calibration-model.js?v=20260712-1";
import {
  assertCaptureProtocolCompatible,
  validateCapturePageIdentity,
} from "./capture-protocol.js?v=20260727-1";
import { verifyAndParseCaptureSpec } from "./transport-integrity.js?v=20260727-1";
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
} from "./measurement-audio.js?v=20260815-4";
import { buildAmbientStatsEvent } from "./ambient-stats.js?v=20260815-4";

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
// FALLBACK end-to-end post-upload wait, used only for a spec that carries no
// `result_wait_s` — i.e. a Pi predating that field, whose Fc sweep is bounded
// by the 70 s budget of that era. This page no longer owns the number for a
// current Pi: it reads the Pi-minted wait off the spec (see `resultWaitMs`),
// so a Pi-side budget change can never strand a page nobody republished.
//
// Keep this at the pre-field era's value. Raising it would only make this page
// wait longer for Pis that cannot possibly need it, and lowering it would break
// the very speakers it exists for.
//
// Scale, for whoever touches it next: on ten banked jts3 rounds the governed
// wall — `event=capture_relay.captured` to `event=correction.crossover_v2_result`
// — measured 67.95-81.28 s, and 73.29-81.28 s for the five rounds that swept
// all six corners. A floor, not a cost: a healthy round answers inside it and
// moves on, so this only bites when the speaker has genuinely gone quiet.
const CAPTURE_RESULT_WAIT_BUDGET_MS = 90000;

// The wait this capture actually runs under: the Pi's own published figure when
// it sent one, else the fallback above. Never the larger of the two — the Pi is
// the owner, and a future Pi that legitimately shortens its analysis must be
// able to say so.
function resultWaitMs(spec) {
  const published = Number(spec && spec.result_wait_s);
  return published > 0 ? published * 1000 : CAPTURE_RESULT_WAIT_BUDGET_MS;
}
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
    setText(el, message);
    el.dataset.kind = kind;
  }
}

// `#status` is the TRANSIENT STATE channel and nothing else (§2.1): a step
// screen's counter and instruction live in the screen's own eyebrow/headline,
// so the status line goes quiet rather than restating them in different words.
// An empty status collapses (index.html's `#status:empty`) instead of leaving
// a tinted bar with nothing in it.
function clearStatus() {
  setStatus("", "info");
}

// Fallback copy (#1658) for a phone whose browser has no Screen Wake Lock API,
// or whose request was rejected — the household still needs SOME signal to
// keep the screen on by hand, since the capture can run for several minutes.
const WAKE_LOCK_HINT_TEXT = "Keep your screen on — this takes about 4 minutes.";

// Per prompted capture, on top of its own acoustic length: reading the move
// prompt, walking the mic, settling, tapping. An allowance, not a measurement —
// nobody has timed a household walking a cloud yet. It only has to keep the
// estimate from reading as absurdly short; the wake lock itself is held for the
// whole session either way.
const WAKE_LOCK_PER_CAPTURE_OVERHEAD_MS = 20000;

// Whole minutes this plan is DISPLAYED as taking, or 0 when the spec carries
// no entry table to estimate from (the legacy level-ramp and single-capture
// kinds — never a fake "0 minutes" presented as a duration; callers treat 0 as
// "say nothing"). Every displayed duration on this page derives from HERE, and
// the Pi's own consent copy derives from the same arithmetic
// (jasper/capture_relay/spec.py's CapturePlan.estimated_minutes, whose test
// reads WAKE_LOCK_PER_CAPTURE_OVERHEAD_MS out of this file) — so the phone and
// the speaker can never quote two different promises about one session.
function planEstimatedMinutes(spec) {
  const entries = (spec && spec.capture_plan && spec.capture_plan.entries) || null;
  if (!Array.isArray(entries) || entries.length === 0) return 0;
  const totalMs = entries.reduce(
    (sum, entry) =>
      sum + (Number(entry && entry.duration_ms) || 0) + WAKE_LOCK_PER_CAPTURE_OVERHEAD_MS,
    0,
  );
  return Math.max(1, Math.ceil(totalMs / 60000));
}

// The screen-on hint's duration estimate, derived from the plan the Pi actually
// sent rather than a constant. The hardcoded "about 4 minutes" was written for
// the 3-capture flow; a 16-capture spatial cloud runs several times that, and a
// household who trusts the number and lets the screen sleep loses the session.
// No plan (the legacy level-ramp and single-capture kinds) keeps the original
// string byte-for-byte.
function wakeLockHintText(spec) {
  const minutes = planEstimatedMinutes(spec);
  if (!minutes) return WAKE_LOCK_HINT_TEXT;
  return `Keep your screen on — this takes about ${minutes} minutes.`;
}

// The plan announcement (flow-simplification §2.3): say what the household is
// about to do BEFORE the first tone — how many measurements, and how long —
// rather than letting them infer it by counting prompts. Both numbers are
// DERIVED from the plan the Pi signed; nothing here is hand-written.
//
// The page derives this rather than relying only on the speaker's own consent
// line because the page ships FIRST (capture-page/README.md "Release order"):
// against a speaker that predates the tier line, this is the whole
// announcement. On a current speaker the two lines say the same numbers from
// the same arithmetic (they cannot disagree) — the speaker's names the
// instrument, "Quick tune"/"Full measurement", which the phone has no field
// for; this one is the lead the household reads first. A one-capture plan
// (the re-verify re-arm) says nothing — its consent copy leads with how cheap
// it is, and "1 measurements" would fight that.
function planAnnouncementText(spec) {
  const minutes = planEstimatedMinutes(spec);
  if (!minutes) return "";
  const { target } = planTargetAndAttempts(spec);
  if (target < 2) return "";
  const sentence = `${target} measurements, about ${minutes} minutes`;
  return specScreenSays(spec, sentence) ? "" : `${sentence}.`;
}

// …and never twice. A current speaker's consent copy CONTAINS this same
// derived sentence, behind the tier name and a stage qualifier ("Quick tune,
// this session: 6 measurements, about 5 minutes") — the phone has no field for
// either, so where both exist the speaker's line is the better one and this
// page adds nothing. The qualifier sits IN FRONT of the numbers precisely so
// the needle below still finds them (jasper/capture_relay/spec.py's
// `_guided_tier_step` says so on its own side). The needle is
// the string the page JUST derived, not a guess at the speaker's wording:
// both sides compute it from the same two plan numbers, so a match means the
// household would otherwise read one sentence twice, two lines apart.
function specScreenSays(spec, sentence) {
  const screen = (spec && spec.ui && spec.ui.screen) || [];
  if (!Array.isArray(screen)) return false;
  return screen.some((component) => {
    if (!component || typeof component !== "object") return false;
    if (typeof component.text === "string" && component.text.includes(sentence)) return true;
    return (
      Array.isArray(component.items) &&
      component.items.some((item) => typeof item === "string" && item.includes(sentence))
    );
  });
}

// Render it directly under the spec's own heading, so the announcement is the
// first thing read and the placement instruction/acknowledgement/start button
// follow it in order (§2.3). Defensive about the host DOM shape: a screen root
// without the usual heading simply gets the line first.
function insertPlanAnnouncement(screenEl, spec) {
  const text = planAnnouncementText(spec);
  if (!text || !screenEl || typeof screenEl.insertBefore !== "function") return;
  const heading =
    typeof screenEl.querySelector === "function" ? screenEl.querySelector("h1") : null;
  const anchor =
    heading && heading.parentNode === screenEl ? heading.nextSibling : screenEl.firstChild;
  screenEl.insertBefore(el("p", { class: "cap-announce", text }), anchor || null);
}

function showWakeLockHint(spec) {
  const el = document.getElementById("wakelock-hint");
  if (el) setText(el, wakeLockHintText(spec));
}

function hideWakeLockHint() {
  const el = document.getElementById("wakelock-hint");
  if (el) setText(el, "");
}

// Wraps acquireWakeLock() with the fallback hint. Wake-lock failure is
// best-effort and must never break the capture flow — this never throws;
// `lock.supported` tells the caller whether a real lock was taken, exactly
// like the wrapped function.
// `spec` is optional and only sizes the fallback hint's duration estimate;
// omitting it keeps the original constant copy.
async function acquireWakeLockWithHint(spec = null) {
  const lock = await acquireWakeLock();
  if (lock.supported === false) {
    console.debug("capture page: screen wake lock unsupported, showing on-screen hint");
    showWakeLockHint(spec);
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
// rejects with the reason it was aborted with — a TAGGED `RelayTimeoutError`
// whose `name` is "RelayTimeoutError" and whose message says nothing about
// aborting. `relayTimeout` is therefore the ONLY reliable test, and the two
// legacy checks below are the fallback for a browser that ignores
// `abort(reason)` and raises its own "signal is aborted without reason."
// DOMException instead (the run-19 gibberish).
//
// Getting this wrong is not cosmetic: between the run-19 named-reason change
// and #1824 this function returned false for every real timeout, so the
// friendly-copy branch it exists to reach was unreachable in production.
// Never re-derive this from `name` or the message text — the string is for
// humans and may be reworded freely.
function isRelayConnectivityAbort(err, message) {
  return (
    (err && err.relayTimeout === true) ||
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
      "and create a new measurement link."
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

// The TERMINAL screen's copy (renderSweepFailed), where there is no retry to
// point at. A classified failure gets the household sentence its family
// already owns; only a genuinely unclassified error falls through to the raw
// message. Before #1824 D2 every post-arm error printed `err.message`
// verbatim, so a relay abort reached the household as "Measurement failed:
// timed out waiting for the speaker's measurement relay" — the internals of a
// 3 s fetch budget, presented as the speaker's verdict.
function sweepFailureMessage(err) {
  const message = err && err.message ? String(err.message) : String(err);
  if (isRelayConnectivityAbort(err, message)) {
    return "Lost the connection to the speaker's measurement relay during this measurement";
  }
  return message.replace(/\.+$/, "");
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

// The link's own deadline, as last published by the relay (absolute ms since
// epoch, from `getPhoneStatus` in relay/src/worker.js). `null` until a poll
// answers.
//
// Why the page keeps it (issue #2083): a dead-session 401/403/404 has TWO
// causes that look identical on the wire — the TTL lapsed, or the Pi ended the
// session and purged it — and the terminal screen used to assert the first
// unconditionally. When one transient stall killed a live session mid-walk,
// the household was told their link had expired while it was still live. This
// is the only evidence the page has for telling those apart, and it is
// deliberately used in one direction only: it can prove a link had NOT lapsed
// yet, which is enough to stop the page making a claim it cannot support.
let lastSeenExpiresAt = null;

function notePhoneStatus(status) {
  const expiresAt = Number(status && status.expires_at);
  if (Number.isFinite(expiresAt) && expiresAt > 0) lastSeenExpiresAt = expiresAt;
  return status;
}

// `notePhoneStatus` is the deadline's ONLY writer, and every phone-status read
// in THIS module goes through here so no polling loop of main.js's own can
// forget to record it.
//
// One reader on the page deliberately stays outside: level-events.js's
// `runLevelRampProtocol` calls `client.fetchPhoneStatus()` directly. That is
// safe rather than merely tolerated, and the reason is structural — `expires_at`
// is minted once at registration (`relay/src/worker.js`'s only writer of it) and
// never refreshed, so a poll that skips the recorder cannot be carrying news;
// and every reachable path into the ramp passes through
// `waitForSetupValidation` → `pollPhoneStatus` first, so the deadline is already
// recorded before the ramp's loop starts. If either of those ever stops being
// true, route the ramp through this function.
// `test_capture_page_records_the_link_deadline_on_every_status_poll` pins the
// full call-site set across capture-page/js/**, so a NEW direct reader anywhere
// on the page fails rather than quietly joining the exception.
async function pollPhoneStatus(client) {
  return notePhoneStatus(await client.fetchPhoneStatus());
}

// Whether the link's TTL is known to have lapsed. Answers `true` when no poll
// ever landed, because "never observed" is not evidence of anything and the
// unchanged link-expired copy is the safe default there (it is also what a page
// talking to a relay that publishes no `expires_at` keeps getting).
//
// Compares the phone's clock against a server-minted deadline, so a badly-set
// phone clock can misread this. The error is one-directional by construction: a
// phone running slow calls a genuine expiry "the speaker ended it", which still
// sends the household to the speaker page for a fresh link. The reverse — the
// false expiry claim this exists to stop — needs the phone to be running FAST,
// past a deadline the relay was still honouring.
function linkDeadlinePassed() {
  if (lastSeenExpiresAt === null) return true;
  return Date.now() >= lastSeenExpiresAt;
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
    const status = await pollPhoneStatus(ctx.client);
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
    else if (key === "text") setText(node, value);
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
  // Name the clock ONLY when the clock is what ran out (work order D8, issue
  // #1807; corrected by issue #2083). A 401/403/404 says the session is gone,
  // never WHY — the Pi purges on every terminal failure too, so the TTL story
  // this screen used to tell unconditionally was a guess dressed as a fact, and
  // a wrong one for the incident that motivated the fix. `linkDeadlinePassed`
  // is the evidence: past the relay's own published deadline, the lapse is real
  // and gets named; short of it, the honest statement is that the speaker ended
  // the session, without inventing a cause the page cannot see. The step budget
  // is deliberately not mentioned in either branch: it ends the session through
  // the Pi's own session-over event (renderPlanExhausted's `budget`), not
  // through a dead relay session, and naming the wrong clock is worse than
  // naming none.
  const budget = planTimeBudget(ctx.spec);
  const lapsed = linkDeadlinePassed();
  let heading;
  let message;
  if (!lapsed) {
    heading = "The speaker ended this session";
    message =
      "The speaker ended this measurement — it may have lost its connection," +
      " or been stopped. Return to the speaker page to start again.";
  } else {
    heading = "Link expired";
    message = budget
      ? `This measurement link lasts ${approxMinutes(budget.sessionS)} from when` +
        " it is created, and this one is past that — return to the speaker page" +
        " to start again."
      : "This measurement link expired — return to the speaker page to start again.";
  }
  const children = [
    el("h1", { class: "cap-heading", text: heading }),
    el("p", { class: "cap-note", text: message }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus(message, "error");
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
  const message = sweepFailureMessage(err);
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
      note: "Level matched. The speaker will continue on its own — you can put the microphone down.",
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
    // The Pi observed a flat reported-vs-commanded staircase slope — something
    // in the recording chain IS adjusting gain automatically. The copy names
    // the BROWSER, because that is where AGC lives: a dedicated measurement mic
    // has none, so blaming the microphone would send the household to replace
    // the one part that is behaving. (No model names here — this file must not
    // embed a mic catalogue; see the Pi-registry inference contract in
    // tests/test_capture_page_js.py.) Friendly copy instead of the raw code.
    //
    // "browser or device" rather than "browser": on iOS every browser is
    // WebKit, so a household that dutifully installs a second browser lands on
    // the same engine, the same AGC, and the same failure — with no remedy
    // left to try. Naming the DEVICE keeps a way out open where switching
    // browsers structurally cannot help. (#1941 delta-gate NIT-A.)
    message.note =
      "This browser is adjusting the microphone level, which prevents accurate measurement. Try a different browser or device, or a USB measurement microphone.";
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
          el("li", { text: "Allow microphone access in this browser." }),
          el("li", { text: "Choose the microphone and calibration." }),
          ...(collectsRoomPositions(ctx.spec)
            ? [el("li", { text: "Choose how many listening positions to measure." })]
            : []),
          el("li", { text: "Place the microphone where the speaker shows you." }),
          el("li", { text: "JTS will rise slowly from quiet and lock the level." }),
        ]
      : [
          el("li", { text: "Allow microphone access in this browser." }),
          el("li", { text: "Choose the microphone and calibration." }),
          el("li", { text: "Pick how many listening positions to measure." }),
          el("li", { text: "Stay quiet while JTS records noise and plays each sweep." }),
        ]),
    button("Continue", () => renderPermission(screenEl, ctx)),
  ]);
  setStatus("Ready to set up the microphone.", "info");
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
  select.appendChild(el("option", { value: "", text: "Automatic / browser default" }));
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
      text: "If a USB-C measurement mic is plugged into this device, choose it here. Otherwise leave Automatic.",
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
    el("option", { value: "none", text: "No calibration / built-in mic" }),
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
      text: "Calibration is applied after recording on the speaker. It is fine to continue without it when using a built-in mic.",
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

function renderRoomReady(
  screenEl,
  ctx,
  { showBack = false, startCapture = onStart } = {},
) {
  let starting = false;
  const start = async () => {
    if (starting) return;
    starting = true;
    // Stop remains live once onStart installs activeAbort. Every other control
    // is navigation or another start/retry affordance and must freeze together;
    // otherwise Back can render a fresh Start while this capture is awaiting
    // its mic/relay work.
    const frozenControls = (ctx.captureRefs.buttons || [])
      .filter((entry) => entry.action !== "stop")
      .map((entry) => entry.el);
    for (const entry of frozenControls) entry.disabled = true;
    try {
      await startCapture(ctx);
    } finally {
      starting = false;
      for (const entry of frozenControls) entry.disabled = false;
    }
  };
  // The setup wizard owns microphone/calibration selection. Once setup is
  // complete, the Pi-owned capture spec is the one source of truth for the
  // measurement-ready screen, including position and trust-repeat copy.
  ctx.captureRefs = renderScreen(screenEl, ctx.spec, {
    handlers: { begin_capture: start, retry: start, stop: stopCapture },
  });
  if (showBack) {
    const back = button("Back", () => {
      if (!starting) renderPositionCount(screenEl, ctx);
    }, true);
    screenEl.appendChild(el("div", { class: "cap-actions" }, [back]));
    ctx.captureRefs.buttons.push({ action: null, el: back });
  }
  setStatus("Ready. Tap Start measurement.", "info");
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
      renderRoomReady(screenEl, ctx, { showBack: true });
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
      text: "Five measurements across the listening area is the default. Move the microphone roughly a head-width between positions.",
    }),
    el("label", { class: "cap-field" }, [
      el("span", { text: "Measurements" }),
      positions,
    ]),
    el("div", { class: "cap-actions" }, [
      button(levelRamp ? "Continue to level check" : "Continue to measurement", continueFromPositions),
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

// The PHONE's own pre-arm floor window — a sub-second reading taken before the
// speaker plays anything, uploaded as `noise_floor` and read as the SNR
// reference for this capture. It is not the speaker's ambient window
// (waitForSweepComplete's `ambient_started`, whose quiet request is the
// speaker's call because only the speaker knows which window is playing), and
// it is not the session's room-noise measurement either.
//
// `note` lets the Pi override the request PER ENTRY (work order D8, issue
// #1835): the default asks for quiet because on almost every capture a sweep
// follows immediately, but CHECK's window is the session's room-noise
// measurement and is deliberately taken BEFORE anyone is hushed — the gain
// solve reads it, so a pre-hushed room makes it under-drive. THREE windows,
// three purposes; this changes one of them and collapses none.
async function captureAmbientNoise(recorder, spec, note = "") {
  const durationMs = Math.max(300, Math.min(2000, Number(spec.noise_floor_ms) || 800));
  setStatus(note || "Measuring room noise — stay quiet.", "recording");
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
          ? "Level check stopped — this screen must stay on."
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
        `This device can't run a clean level check (${capture.decision.reason}). ` +
          "Try a different device or measurement microphone.",
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

// What the phone says while a relay control request is timing out mid-capture.
// The speaker is NOT waiting on the phone here — it is playing the program on
// its own clock — so the honest line is that the measurement continues and the
// page is reconnecting, never a failure.
const RELAY_RECONNECTING_MESSAGE = "The speaker is still measuring — reconnecting…";
// The same idea one step later in the round, where the speaker is analyzing an
// uploaded capture rather than playing.
const RELAY_RECONNECTING_RESULT_MESSAGE =
  "The speaker is still checking this measurement — reconnecting…";
// What the phone shows between the upload and the speaker's verdict; named so
// the reconnect path above can put it back verbatim.
const CAPTURE_CHECKING_MESSAGE = "Speaker is checking this measurement…";

// The GROUP CLOSE is a different, longer job than one capture's verdict — the
// combine plus the fit across every position — and saying "this measurement"
// there named the wrong work at the exact moment the household is waiting
// longest (the owner's 2026-07-29 field note: it "ran long … with no hint
// whether it was stalled").
const CAPTURE_CLOSING_MESSAGE = "Speaker is working out what it heard…";

// How long the page waits before each re-send of a POST-ARM control request
// that hit a relay connectivity abort — one entry per re-send, so the ladder's
// LENGTH is the retry count and the two can never disagree. Three immediate
// re-sends (four tries total) against relay-client.js's 3 s abort absorbs ~12 s
// of blip — well inside the Pi's 120 s hold on the armed slot, and short enough
// that a genuinely dead relay still fails while the household is still holding
// the phone. Zero delay is right HERE because the speaker is mid-program and
// every second blind is a second of the round already spent.
const RELAY_RECONNECT_BACKOFF_MS = [0, 0, 0];

// The PRE-ARM ladder (issue #2517), where the speaker is not playing anything
// yet and the page can afford to wait out a real blip rather than re-hammer a
// relay that just refused. Three re-sends at 2 s / 4 s / 8 s.
const RELAY_BEGIN_RECONNECT_BACKOFF_MS = [2000, 4000, 8000];

// …and the WALL CLOCK that actually bounds that ladder. Rung count alone does
// not, and this arithmetic is load-bearing —
// docs/HANDOFF-crossover-measurement-v2.md delegates the safety argument for
// the pre-arm retry to this comment rather than restating it.
//
// What one retry-eligible attempt can cost:
//   * up to RELAY_CONTROL_TIMEOUT_MS (3 s) for the begin post to abort; PLUS
//   * ~23 s in the admission wait if the post lands and a later poll is what
//     blips. `waitForCaptureAuthorized` opens a fresh 20 s deadline per call
//     and rethrows a connectivity abort from any poll inside it — and its
//     deadline check runs BEFORE each poll, so the final poll starts just
//     inside 20 s and then carries its own 3 s timeout on top (measured 22.9 s
//     at the shipped constants). The 20 s deadline is not the wait's ceiling.
//   => ~26 s per attempt, not the ~3 s a post-only reading suggests.
//
// So the rung count alone bounds this at 4 × 26 s + 14 s of backoff ≈ 118 s,
// which is effectively the whole of the Pi's 120 s `awaiting_arm` budget
// (jasper/capture_relay/session.py's DEFAULT_TIMEOUT_S). That budget is set
// ONCE at admission and refreshed by nothing the page does here — the re-posts
// this ladder sends are the runner's "nothing to do" no-op branch, which
// touches no deadline — so an unbounded-in-time ladder could spend the very
// window it exists to protect and hand the household a tap prompt with seconds
// left on it.
//
// 30 s of wall clock, checked before each re-send (and before its backoff, so
// a rung that cannot fit is never started), bounds the whole exchange at
// budget + one trailing attempt ≈ 30 + 26 = ~56 s. Note the tail sits OUTSIDE
// the budget by construction: the budget gates when the last re-send may
// START, and that attempt then runs its own full length. The manual affordance
// therefore always appears with ≳64 s of the Pi's window intact — ample for a
// tap to re-post, take its ambient window, and arm. A genuinely transient blip
// still gets every rung: in the fast-fail family, where each attempt ends on
// its 3 s post abort and never reaches the admission wait, the fourth starts
// at ~23 s (3+2, 3+4, 3+8) — inside the budget. Only SLOW attempts are cut
// short, which is exactly the case where spending more would cost the
// household the fallback it is being cut short to preserve.
const RELAY_BEGIN_RECONNECT_BUDGET_MS = 30000;

// What the phone says while it is quietly re-trying the begin exchange. The
// household has NOT been asked to do anything here — distinct on purpose from
// captureFailureMessage's "Tap … to try again", which is what the page falls
// back to once this ladder is spent.
const RELAY_RECONNECTING_BEGIN_MESSAGE =
  "Lost the connection to the speaker for a moment — retrying…";

// Re-run one relay control request across connectivity aborts. Used where the
// request is IDEMPOTENT on the Pi (the armed event's last-write-wins slot),
// never where a repeat would double an effect. Any other error propagates
// untouched on the first raise — this widens no other failure class.
// `budgetMs`, when set, bounds the ladder by WALL CLOCK as well as by rung
// count — necessary wherever one attempt's own duration is unbounded by this
// function (see RELAY_BEGIN_RECONNECT_BUDGET_MS). Both bounds end the ladder
// the same way: the last error is raised, so the caller's existing failure
// handling — the manual affordance, on the pre-arm path — is reached
// identically whichever bound bit first.
async function withRelayReconnect(request, isAborted, {
  backoffMs = RELAY_RECONNECT_BACKOFF_MS,
  budgetMs = null,
  message = RELAY_RECONNECTING_MESSAGE,
  tone = "recording",
} = {}) {
  let lastError = null;
  // Anchored at ENTRY, not at the first failure: the attempt that blips has
  // already spent its own share of the Pi's window, and a bound that ignored
  // it would not be a bound on the exchange.
  const deadline = budgetMs ? Date.now() + budgetMs : null;
  // One initial attempt, plus one re-send per rung of the backoff ladder.
  for (let attempt = 0; attempt <= backoffMs.length; attempt += 1) {
    if (isAborted()) return undefined;
    try {
      return await request();
    } catch (err) {
      if (!isRelayConnectivityAbort(err, String((err && err.message) || err))) {
        throw err;
      }
      lastError = err;
      setStatus(message, tone);
      const wait = backoffMs[attempt];
      if (wait === undefined) break; // rungs spent — surface the failure
      // Counting the backoff itself, so a rung that cannot fit inside the
      // budget is never started rather than started and overrun.
      if (deadline !== null && Date.now() + wait >= deadline) break;
      // Named in the console as well as on screen: an unattended remote
      // session has nobody reading the status line, and a remote inspection
      // of the live tab is how #2517 was diagnosed in the first place.
      console.debug(
        `capture page: relay connectivity blip, re-sending in ${wait} ms ` +
          `(re-send ${attempt + 1} of ${backoffMs.length})`,
      );
      if (wait > 0) await delayMs(wait);
    }
  }
  throw lastError;
}

// `armed` ({index, attempt}) is the capture this wait belongs to, supplied by
// the plan loop. It is what makes reading a `capture_result` mid-wait safe: the
// relay's host-event slot is last-write-wins and nothing clears it when the
// phone consumes a verdict, so an un-matched read would see the PREVIOUS
// attempt's stale verdict on this attempt's first poll (the same hazard
// waitForCaptureAuthorized documents). Omitted by the single-capture path,
// which then behaves exactly as it did before.
async function waitForSweepComplete(client, spec, isAborted, armed = null) {
  const timeoutMs = Math.max(5000, Number(spec.duration_ms) || 20000);
  const pollMs = Math.max(100, Math.min(1000, Number(spec.progress_poll_ms) || 250));
  let deadline = Date.now() + timeoutMs;
  let lastPhase = "";
  // The last true phase line, so a swallowed connectivity blip can put it back
  // rather than leaving "reconnecting…" frozen on screen for the rest of a
  // sweep (the sweep phase renders once, on transition — there is no later
  // repaint to rescue it).
  let phaseLine = "";
  // The swallowed abort still in force, or null once the relay answers again.
  // Held so a window that expires while the page is blind reports the honest
  // cause (lost connectivity) rather than accusing the speaker of never
  // finishing a sweep it may well have played.
  let reconnecting = null;
  const showPhase = (text) => {
    phaseLine = text;
    setStatus(text, "recording");
  };
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
  // Whether THIS room-listening window is one the household should be quiet
  // for. The two windows are opposites and only the speaker knows which is
  // playing: MEASURE/VERIFY's 1 s pre-pilot window sits after the courtesy
  // beeps and must be quiet, while CHECK's 12 s window is the SESSION's
  // room-noise measurement, deliberately taken BEFORE anyone is asked to hush
  // (jasper.audio_measurement.program's module docstring). Asking for quiet
  // during that one would edit the floor it is measuring — the gain solve and
  // the ambient band-floor report both read it. Absent (an older Pi) resolves
  // to false: not asking is the harmless direction, asking is not.
  let ambientQuietRequested = false;
  while (Date.now() < deadline) {
    if (isAborted()) return false;
    let status;
    try {
      status = await pollPhoneStatus(client);
    } catch (err) {
      // ONE slow control request must cost one poll interval, not the session
      // (#1824: a single fetch out of ~60 exceeded relay-client.js's flat
      // RELAY_CONTROL_TIMEOUT_MS on capture 5 of 7 and killed a live express
      // run while the speaker played the whole program and held its slot for
      // another 120 s). Swallow the connectivity abort and keep polling the
      // SAME capture — `deadline` above is the real bound. Mirrors
      // waitForCaptureAuthorized/waitForCaptureResult's classify-then-decide
      // shape; every other error (a dead session, an incompatible page) still
      // propagates to the caller's terminal handling.
      if (!isRelayConnectivityAbort(err, String((err && err.message) || err))) {
        throw err;
      }
      reconnecting = err;
      setStatus(RELAY_RECONNECTING_MESSAGE, "recording");
      await delayMs(pollMs);
      continue;
    }
    if (reconnecting) {
      reconnecting = null;
      // The relay answered again: put the true phase line back. A countdown
      // phase repaints itself on its next whole second anyway; this covers the
      // static lines (sweep in progress, room tail) that do not.
      if (phaseLine) setStatus(phaseLine, "recording");
    }
    const event = status && status.host_event || {};
    const phase = String(event.phase || "");
    if (phase === "capture_incompatible") {
      throw new Error(event.error || "capture page is incompatible with this speaker");
    }
    if (
      phase === "capture_result" &&
      event.accepted === false &&
      armed &&
      Number(event.index) === Number(armed.index) &&
      Number(event.attempt) === Number(armed.attempt)
    ) {
      // A terminal refusal posted WHILE the phone was waiting for the tone
      // (jasper.web.correction_crossover_v2's _post_terminal_failure_host_event
      // — e.g. the play seam refused before any audio). This is the only
      // capture_result a matching (index, attempt) can produce here: an
      // ordinary quality verdict cannot exist yet, because this attempt's blob
      // has not been uploaded. Until #1821 the loop ignored it, polled on into
      // the Pi's session purge, and rendered "this link has expired" over a
      // speaker that had said exactly why it stopped.
      const failure = new Error(
        event.reason || event.banner || event.error ||
          "The speaker stopped this measurement.",
      );
      failure.sweepFailed = true;
      throw failure;
    }
    if (phase && phase !== lastPhase) {
      lastPhase = phase;
      if (phase === "prelude_started") {
        // The courtesy prelude (the beeps, then the settle the household is
        // asked to go quiet in). Its own phase because it is AUDIBLE and it is
        // not the tone: before #1824 D4 the Pi posted `sweep_started` at arm
        // time and the phone said "Playing the measurement tone…" through
        // ~4.6 s of beeps and silence. An older Pi that posts no prelude phase
        // simply keeps the pre-arm line until the tone starts, as before.
        //
        // "three" MIRRORS jasper.audio_measurement.program's
        // COURTESY_TONE_BEEP_COUNT. Deliberately spelled out rather than sent
        // over the wire — a household counts beeps, it does not parse a field,
        // and the count has one value. The pairing is pinned by
        // test_capture_page_beep_copy_matches_the_composed_beep_count, which
        // fails if the constant ever moves.
        showPhase("Listen for three beeps, then stay quiet — the tone follows.");
      } else if (phase === "ambient_started") {
        const durationS = Number(event.duration_s);
        ambientDeadlineMs =
          Number.isFinite(durationS) && durationS > 0
            ? Date.now() + durationS * 1000
            : null;
        lastCountdownSeconds = null;
        ambientQuietRequested = event.quiet_requested === true;
        if (!ambientDeadlineMs) {
          showPhase(
            ambientQuietRequested
              ? "Measuring room noise — stay quiet and keep the microphone still."
              : "Measuring room noise — keep the microphone still.",
          );
        }
      } else if (phase === "sweep_started") {
        ambientDeadlineMs = null;
        // Re-anchor the recording window on the OBSERVED start of the tone.
        // `timeoutMs` is the whole program plus CAPTURE_ENTRY_MARGIN_MS
        // (jasper.active_speaker.crossover_v2_flow), but the phone starts
        // counting it at `armed` — so the Pi's arm-poll latency (~0.75 s),
        // the host-event posts and ordinary jitter all come out of that flat
        // 2 s margin, leaving 0.6-1.4 s of measured slack on a cloud capture
        // (#1824 D6). Anchoring here spends at most the program's own
        // pre-stimulus lead-in again, and only when the speaker has already
        // told us the tone is playing.
        deadline = Date.now() + timeoutMs;
        showPhase("Playing the measurement tone…");
      } else if (phase === "sweep_complete") {
        showPhase("Tone finished — capturing the room tail.");
      }
    }
    if (phase === "ambient_started" && ambientDeadlineMs) {
      // Floored at 1, not 0: the boundary tick would otherwise render "about 0
      // seconds", which is not a duration. The phase flips to the tone (or the
      // beeps) within one poll of here anyway, so "about 1 second" is the last
      // thing shown and it stays true to the nearest second.
      const remaining = Math.max(
        1,
        Math.ceil((ambientDeadlineMs - Date.now()) / 1000),
      );
      if (remaining !== lastCountdownSeconds) {
        lastCountdownSeconds = remaining;
        // Describes the QUIET WINDOW, not what follows it. The earlier copy
        // ("the tone starts in about N seconds") was true only of a program
        // whose room-listening window runs straight into the stimulus. The
        // real CHECK program opens with a 12 s window and then plays the
        // courtesy beeps before the tone (see
        // jasper.audio_measurement.program's `_insert_courtesy_prelude`), so
        // that sentence would have been wrong by the whole prelude.
        //
        // And the quiet REQUEST is the speaker's call, not the page's — see
        // ambientQuietRequested. CHECK's window is measuring the room the
        // household is actually living in; hushing them for it would change
        // the reading.
        const seconds = `${remaining} second${remaining === 1 ? "" : "s"}`;
        showPhase(
          ambientQuietRequested
            ? `Listening to the room… stay quiet for about ${seconds}`
            : `Listening to the room for about ${seconds}`,
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
  // Still blind when the window ran out: the relay outage IS the failure, and
  // its classified copy is what the household needs (a "the speaker did not
  // finish" sentence would be a guess about a speaker we could not hear from).
  if (reconnecting) throw reconnecting;
  throw new Error("speaker did not finish the sweep before the recording timeout");
}

// ============================================================================
// Session-spanning capture plans (SPEC W2.3) — the
// ping-pong killer. One relay session now covers a driver's whole repeat
// SET: after each accepted capture the phone shows "Measurement N of target
// ✓" with a single "Next measurement" tap, instead of the household
// returning to the wizard between every repeat. Plan-free specs
// keep today's flow through onStart() above, which stays WIRE-COMPATIBLE
// with every deployed Pi: its armed event changed inert-additively (the
// noise_floor object is now built explicitly with the same two fields, and
// crossover_sweep captures gained the ambient_stats key, which older Pis
// simply ignore) — no field changed meaning or shape. The loop below is a
// fully separate code path, taken only for a spec carrying a capture_plan
// (see jasper/capture_relay/spec.py's CapturePlan docstring).
// ============================================================================

function planTargetAndAttempts(spec) {
  const plan = (spec && spec.capture_plan) || {};
  return {
    target: Math.max(1, Number(plan.capture_target) || 1),
    maxAttempts: Math.max(1, Number(plan.max_attempts) || 1),
  };
}

// ---------------------------------------------------------------------------
// The honest per-position attempt count (owner ruling #2086, 2026-08-03).
//
// The Pi puts `attempts` on every capture verdict: how many EXTRA tries this
// spot has already spent, out of how many it is allowed, and how many of them
// the speaker asked for itself. It exists because the counter this page had —
// `screen.progress`, frozen at plan-build time — names the POSITION, and a
// position that is retried does not move: on 2026-08-03 the phone read "step 6,
// one last time" while the flow was on its fifth attempt at that spot and the
// twelfth of the session. Numbers rather than a sentence, because the §2.1
// grammar makes the eyebrow the page's slot.
//
// Absent (an older Pi) → null, and every caller keeps today's copy.
// ---------------------------------------------------------------------------
function attemptCount(raw) {
  if (!raw || typeof raw !== "object") return null;
  const allowed = Number(raw.allowed);
  const used = Number(raw.used);
  if (!Number.isFinite(allowed) || !Number.isFinite(used)) return null;
  return {
    used,
    allowed,
    left: Number.isFinite(Number(raw.left)) ? Number(raw.left) : allowed - used,
    bySpeaker: Number(raw.by_speaker) || 0,
    byHousehold: Number(raw.by_household) || 0,
  };
}

// The eyebrow suffix naming which try the household is about to make. `used` is
// what this position has ALREADY spent, so the next one is `used + 1` — the one
// place this page does arithmetic on the Pi's numbers, kept here so there is
// one of it. Empty when the Pi published no count (today's copy stands).
function extraTrySuffix(attempts) {
  if (!attempts) return " — one more try";
  if (attempts.left <= 0) return " — no more tries at this spot";
  return ` — extra try ${attempts.used + 1} of ${attempts.allowed}`;
}

// "Who spent what", only when it is not all the household (ruling item 4). A
// geometry rung is the SPEAKER asking for a wider take of a capture that was
// otherwise fine; billing that silently to the household's patience is what the
// ruling calls out.
function extraTryAttribution(attempts) {
  if (!attempts || attempts.bySpeaker <= 0) return "";
  return `JTS asked for ${attempts.bySpeaker} of those extra tries itself.`;
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

// This entry's own pre-arm floor-window copy, or "" for the page's default.
// Read off the entry's `screen` map — an OPEN map by contract ("the schema
// bounds size and value types, never the keys — the capture page decides what
// to render"), so a Pi that does not send it leaves today's copy in place and
// a page that does not know it ignores it. Both directions are safe, which is
// why this key needs no release ordering of its own.
function entryNoiseNote(spec, index) {
  const entry = entryForIndex(spec, index);
  const screen = (entry && entry.screen) || {};
  return screen.noise_note ? String(screen.noise_note) : "";
}

// How long the phone should wait between a `capture_deferred` host event and
// re-posting the SAME `begin_capture`. A deferral means the Pi is between
// phases (e.g. parked awaiting the household's Apply tap), not a fast ledger
// check, so this is slower than the general status-poll cadence.
const CAPTURE_DEFERRED_RETRY_POLL_MS = 1500;

// How long the phone waits for the Pi to close a HELD capture set after the
// household's "Continue" (two-stage work order D1). The Pi is doing the
// session's slowest analysis in that window — the group combine plus the fit —
// and this deliberately MIRRORS the Pi's own between-step inactivity budget
// (`capture_relay.session.DEFAULT_TIMEOUT_S`, 120 s) rather than introducing a
// tighter one, so the phone never gives up on work the speaker is still doing.
// It is not a session time budget: nothing here changes how long any session
// may live.
const CAPTURE_SET_COMPLETE_TIMEOUT_MS = 120000;

// ---------------------------------------------------------------------------
// Time budgets (work order D8, issue #1807) — WHICH clock is running.
//
// A household mid-session wanted to move furniture and had no idea how long
// the open page allowed; they found out by hitting an expiry. Two clocks are
// running and neither was ever on screen: the per-step phone-inactivity budget
// (the Pi's `DEFAULT_TIMEOUT_S`, refreshed on every tap) and the relay
// session's TTL (`ttl_s`, from the moment the link was minted).
//
// THE PAGE INVENTS NEITHER NUMBER. Both arrive on `spec.time_budget`, set by
// jasper/capture_relay/correction_adapter.py at mint time, so the page cannot
// become one more copy of the 900 (see the work order's relay-TTL trap — this
// PR publishes that value, it does not change it). Publishing it also
// collapsed the two PYTHON copies onto `session.DEFAULT_TTL_S`; the
// duplication is not gone entirely, because relay/src/worker.js keeps its own
// deliberately — a separate release that must bound whatever any Pi asks for.
// A spec without the field says nothing at all rather than guessing, which is
// also what makes a new page safe against an older Pi.
// ---------------------------------------------------------------------------

function planTimeBudget(spec) {
  const budget = spec && spec.time_budget;
  if (!budget || typeof budget !== "object") return null;
  const stepS = Number(budget.step_s);
  const sessionS = Number(budget.session_s);
  if (!(stepS > 0) || !(sessionS > 0)) return null;
  return { stepS, sessionS };
}

// "about 2 minutes" — whole minutes, because a household reading "how long can
// I pause?" is not timing itself to the second, and a to-the-second figure the
// page cannot actually track would be false precision.
function approxMinutes(seconds) {
  const minutes = Math.max(1, Math.round(Number(seconds) / 60));
  return `about ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

// The one sentence every plan step screen carries, or "" when the Pi did not
// say.
//
// Deliberately NOT a live countdown, and that is a PRODUCT CHOICE rather than
// a capability gap — the page could render one. The relay publishes an
// absolute `expires_at` on every phone-status poll (relay/src/worker.js's
// `getPhoneStatus`, set at mint as `now + ttl * 1000`), so the link's deadline
// is knowable to the millisecond. Two reasons not to spend it:
//
//   * It would count the WRONG CLOCK. What a household is actually racing
//     between taps is the per-step budget, which the Pi refreshes on every tap
//     and publishes no deadline for — a countdown of the 15-minute link would
//     tick reassuringly through a step window that is about to end the
//     session, which is worse than saying nothing.
//   * A clock on a measuring screen is pressure applied to a task that wants a
//     steady hand and a household willing to walk to the next spot. D8 asks
//     the page to answer "how long can I pause?", not to hurry anyone.
//
// So it states both budgets, says which instant each runs from, and lets the
// household do the arithmetic. `expires_at` stays available if a future rung
// decides an expiry-imminent WARNING (not a countdown) earns its place.
function timeBudgetLine(spec) {
  const budget = planTimeBudget(spec);
  if (!budget) return "";
  return (
    `You have ${approxMinutes(budget.stepS)} between taps, and this link ` +
    `lasts ${approxMinutes(budget.sessionS)} from when it was created.`
  );
}

// ---------------------------------------------------------------------------
// No dead air on a blind wait (work order D9, issue #1840 + the owner's
// 2026-07-29 field note).
//
// Three of this page's waits are BLIND: the speaker is working and posts
// nothing until it has an answer. Analysis alone ran 20-40 s per capture and
// longer at the group close, under one static line — the owner did not know
// whether to keep waiting or give up, which is the same complaint as a retry
// that sits still until a 120 s watchdog fires. The fix is the same for both:
// show the time spent AND the time this wait will actually allow, so a stall
// is legible as a stall.
//
// Elapsed rather than remaining, deliberately: elapsed is a fact the page
// owns, while "remaining" implies the speaker is on a schedule it never
// promised. The limit is stated beside it so the number has a scale.
// ---------------------------------------------------------------------------

function _waitLimitLabel(seconds) {
  if (seconds >= 90) {
    const minutes = Math.round(seconds / 60);
    return `${minutes} min`;
  }
  return `${Math.round(seconds)}s`;
}

// Returns a tick() to call from a poll loop. It repaints at most once a second
// so a 250 ms poll cadence does not thrash the status channel, and the CALLER
// decides when to tick — a loop that also renders reconnect lines simply skips
// the tick while it is showing one, rather than the two fighting for the slot.
function waitProgress(baseMessage, deadlineMs, { kind = "info" } = {}) {
  const startedMs = Date.now();
  const limit = _waitLimitLabel(Math.max(1, (deadlineMs - startedMs) / 1000));
  let lastSecond = -1;
  return function tick() {
    const elapsed = Math.floor((Date.now() - startedMs) / 1000);
    if (elapsed === lastSecond) return;
    lastSecond = elapsed;
    setStatus(`${baseMessage} ${elapsed}s so far, up to ${limit}.`, kind);
  };
}

// Page-local danger confirm for the demoted Stop control (§2.1). The capture
// page shares NOTHING with the Pi's /assets/shared/js/dialog.js — different
// origin, different bundle, and a CSP that admits no external anything — so
// this is the page's own minimal <dialog>, declared statically in index.html
// (copy included, since Stop is the only thing it ever confirms).
//
// Fails OPEN: a browser with no <dialog>/showModal gets today's behavior, an
// immediate stop. The confirm guards a STRAY tap on a control that now sits on
// every step screen; it is not a permission gate, and trapping a household in
// a session they cannot end would be the worse failure.
function confirmStopMeasuring() {
  const dialog = document.getElementById("stop-confirm");
  if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(true);
  // A confirm is already up (a double-tap on the link): showModal() would
  // throw InvalidStateError, and stacking a second confirm is not a decision
  // the household made. Treat the extra tap as no answer.
  if (dialog.open) return Promise.resolve(false);
  const accept = document.getElementById("stop-confirm-accept");
  const cancel = document.getElementById("stop-confirm-cancel");
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      accept?.removeEventListener("click", onAccept);
      cancel?.removeEventListener("click", onCancel);
      dialog.removeEventListener("close", onClose);
      try {
        dialog.close();
      } catch {
        /* already closed (ESC / backdrop) */
      }
      resolve(value);
    };
    // ESC and the backdrop both close the dialog without a choice — that is a
    // cancel, never a stop.
    const onAccept = () => finish(true);
    const onCancel = () => finish(false);
    const onClose = () => finish(false);
    accept?.addEventListener("click", onAccept);
    cancel?.addEventListener("click", onCancel);
    dialog.addEventListener("close", onClose);
    dialog.showModal();
  });
}

// Stop, demoted (§2.1): a small text link rather than the page's one
// full-weight danger button. It appears on EVERY step screen — 16 times in a
// full session — and at equal weight it competed with the primary it sits
// next to, so a stray tap could kill a session outright. The destructiveness
// moves to the confirm above rather than being dropped.
function stopLinkEl() {
  return el("div", { class: "cap-stop" }, [
    el("button", {
      type: "button",
      class: "cap-stop-link",
      text: "Stop measuring",
      onclick: async () => {
        if (await confirmStopMeasuring()) await stopCapture();
      },
    }),
  ]);
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
  // Nothing is retakeable once the session is over (§2.6), and a round parked
  // on the confirmation tap is released rather than left suspended — the
  // terminal-path counterpart to the same release in makePlanController's
  // abort, for the terminals that never go through it.
  shutRetakeWindow(ctx);
  resolvePendingConfirm(ctx);
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
    const lock = await acquireWakeLockWithHint(ctx.spec);
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
    // …and a round parked on the post-apply confirmation tap (§2.2) must be
    // released, or its runPlanCapture would stay suspended on a promise
    // nothing will ever resolve. It re-checks `controller.aborted` the moment
    // it wakes, so nothing runs past this point.
    resolvePendingConfirm(ctx);
    if (reason === "stopped") {
      renderStoppedScreen(ctx);
    } else {
      setStatus(
        reason === "backgrounded"
          ? "Measurement stopped — this screen must stay on."
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

// ---------------------------------------------------------------------------
// The step-screen grammar (flow-simplification §2.1)
//
//   [eyebrow]  Measurement 4 of 6          <- screen.progress; the ONLY counter
//   [headline] Move the microphone 16 in   <- the instruction, prominent; a
//              (40 cm) to the LEFT of the     COMPLETE pose, never a delta
//              mark, at mark height.          on the previous one
//   [detail]   one short supporting clause <- may be empty
//   [action]   I'm there — play the tone   <- ONE full-width primary
//   [retake]   Retake this measurement     <- secondary, quieter (optional)
//   [budget]   about 2 minutes between…    <- quieter still; only when the Pi
//                                             published its budgets (D8)
//   [stop]     Stop measuring              <- small text link -> danger confirm
//
// Every page-owned plan screen renders these in the same DOM slots, so the
// instruction is always in the same place on the phone. It inverts what
// shipped before: the counter headlined and the instruction whispered
// underneath, while `#status` counted the same walk a second time and
// disagreed. `#status` is deliberately NOT part of this grammar any more — it
// carries transient state only ("Speaker is checking this measurement…").
// ---------------------------------------------------------------------------

// The confirming tap's label: the tap IS the placement confirmation, so it
// says so. Only for a plan that carries per-entry screens (the guided walk,
// where a move actually happened); a plan with no entry table is the legacy
// "N repeats of one spec" shape, whose byte-identical copy stays.
const STEP_PRIMARY_LABEL = "I’m there — play the tone";

function stepPrimaryLabel(entry) {
  return entry && entry.screen ? STEP_PRIMARY_LABEL : "Next measurement";
}

function renderStepScreen(ctx, {
  progress = "",
  headline,
  detail = "",
  notes = [],
  primary = null,
  secondary = null,
  extraActions = [],
  captureHeading = false,
}) {
  const headingEl = el("h1", { class: "cap-heading", text: String(headline) });
  const children = [];
  if (progress) children.push(el("p", { class: "cap-eyebrow", text: String(progress) }));
  children.push(headingEl);
  if (detail) children.push(el("p", { class: "cap-note", text: String(detail) }));
  for (const note of notes) if (note) children.push(note);
  const actions = [primary, secondary, ...extraActions].filter(Boolean);
  if (actions.length) children.push(el("div", { class: "cap-actions" }, actions));
  // The budget line sits UNDER the action and above Stop (work order D8): it
  // answers "how long can I pause?", which is a question about the control the
  // household is looking at, and it must not compete with the instruction for
  // the headline/detail slots the §2.1 grammar reserves. Absent when the Pi
  // published no budget, so the grammar is unchanged on every older pairing.
  const budget = timeBudgetLine(ctx.spec);
  if (budget) children.push(el("p", { class: "cap-note cap-budget", text: budget }));
  children.push(stopLinkEl());
  setScreen(ctx.screenEl, children);
  ctx.captureRefs = {
    // Only the forward primary registers as the begin affordance: it is what
    // planRetryAffordance() names in a pre-arm failure message, and what the
    // auto-advance blockers pin as "absent while the screen is not the
    // household's to act on".
    buttons: primary ? [{ action: "begin_capture", el: primary }] : [],
    levelMeters: [],
    // Only screens that go on to own the recording window hand their heading
    // to advanceDeferredHoldHeading (see renderPlanDeferred).
    ...(captureHeading ? { heading: headingEl } : {}),
  };
}

// ---------------------------------------------------------------------------
// Voluntary retakes (§2.6) — "I sneezed / a truck passed / I wasn't where I
// meant to be", not a failure retry.
//
// THE WINDOW IS THE WHOLE CONTRACT. jasper/capture_relay/session.py's
// `_poll_capture_plan` admits exactly one extra begin shape — the
// just-accepted index, on the next attempt, carrying `retake: true` — and
// ONLY while the next entry's begin has not been seen (its `next_begin_seen`,
// which flips on an admitted OR merely deferred begin, including the VERIFY
// hold's auto-posted one). Past that point a retake begin is refused as
// `begin_out_of_order`, and ANY refusal ends the whole session. So an offer
// that outlived the window would be a button whose only possible outcome is
// killing the run: `ctx.retakeSlot` is cleared by every begin this page posts
// and re-armed only by a fresh accepted verdict, and `canRetake` is checked
// again inside the tap (a countdown's auto-begin can win the race).
// ---------------------------------------------------------------------------

// What a retake actually depends on is HOST-SIDE per-index take retention: an
// accepted retake must REPLACE the slot's take, and a rejected one must leave
// the original standing. Only the v2 crossover conductor provides that, and
// the signal this page can see for it is the heterogeneous plan that conductor
// builds (schema_version 2, per-entry screens) — a proxy, not the guarantee
// itself. The pre-entries "N repeats of one spec" plan has no retention
// contract, so it gets no offer. If another host ever ships an
// entries-carrying plan without retention, THIS predicate is the line that has
// to change, not the screens.
function planSupportsRetake(spec) {
  const entries = spec && spec.capture_plan && spec.capture_plan.entries;
  return Array.isArray(entries) && entries.length > 0;
}

function canRetake(ctx, index) {
  return Boolean(ctx.retakeSlot) && ctx.retakeSlot.index === index;
}

// Re-arm (or shut) the offer from an accepted verdict. `attempt` is the
// attempt just consumed, so a retake would be `attempt + 1` — offered only
// while the plan's own attempt budget AND this position's own extra-try budget
// cover it. The second half is the ruling's (#2086): a retake IS one of the
// position's three extras, so once they are gone the control must be absent
// rather than sitting there leading to a refusal.
function armRetakeSlot(ctx, { index, attempt, attempts = null }) {
  const { maxAttempts } = planTargetAndAttempts(ctx.spec);
  const extrasLeft = !attempts || attempts.left > 0;
  ctx.retakeSlot =
    planSupportsRetake(ctx.spec) && extrasLeft && attempt + 1 <= maxAttempts
      ? { index, attempt }
      : null;
}

// Shut the window. The offering screen is not replaced until the new round's
// verdict lands, so the "Retake this measurement" control the household is
// looking at stays on screen through the whole next capture.
//
// It used to be DISABLED here, to keep a live-looking control from doing
// nothing when tapped. That traded one silent failure for another: a disabled
// button cannot fire a click, so the press produced no reaction at all — the
// owner pressed retake mid-verify on 2026-08-03 and the session journal
// recorded no retake and no refusal, just an auto-advance (issue #2090). The
// control now stays tappable and ANSWERS: `retakeControl`'s handler re-checks
// the window and says why it is closed. Silence is the only wrong outcome.
function shutRetakeWindow(ctx) {
  ctx.retakeSlot = null;
}

const RETAKE_LABEL = "Retake this measurement";
// Why a retake press arrived too late. Names the state the household can see
// (the next measurement started) rather than the protocol fact behind it
// (`next_begin_seen` closed the runner's admission window).
const RETAKE_TOO_LATE_MESSAGE =
  "Too late to redo that spot — the next measurement already started.";

// `onTap` lets the offering screen clear its own transient state before the
// round starts — the countdown's live "Starting in N…" counter, which would
// otherwise sit frozen through the whole retake.
function retakeControl(ctx, { index, attempt, onTap = null }) {
  if (!canRetake(ctx, index)) return null;
  const retake = button(RETAKE_LABEL, async () => {
    // Re-checked inside the tap: the next round may have begun between render
    // and tap (a countdown firing its own begin, or the household tapping the
    // forward primary first), which shuts the window on the Pi — a retake
    // posted past it is refused as `begin_out_of_order`, which ends the whole
    // session. So the press must NOT reach the Pi. It must also not vanish:
    // this branch used to `return` silently, which is issue #2090.
    if (!canRetake(ctx, index)) {
      setStatus(RETAKE_TOO_LATE_MESSAGE, "error");
      return;
    }
    // …and stop that countdown from firing behind this tap.
    clearAutoAdvance(ctx);
    if (onTap) onTap();
    retake.disabled = true;
    // NAME THE STEP BEING RETAKEN (owner field note, 2026-07-29: "after
    // pressing retry, the top-of-screen copy describes the next action rather
    // than the step being retried"). Every screen that OFFERS a retake is
    // about a different index than the retake itself — `renderPlanNext` shows
    // the UPCOMING position's instruction, the group-close confirm shows the
    // set — and `runPlanCapture` paints no screen of its own, so without this
    // the household re-measured position 6 while reading position 7's
    // instruction. Rendered here rather than inside `runPlanCapture` because a
    // retake is the only entry into it whose caller's screen is wrong.
    renderRetakeInProgress(ctx, { index });
    try {
      await runPlanCapture(ctx, { index, attempt: attempt + 1, retake: true });
    } finally {
      retake.disabled = false;
    }
  }, true);
  return retake;
}

// The in-progress screen for a voluntary retake: THIS index's own copy, with
// no affordance (the round is already running).
//
// Deliberately NOT `captureHeading` — that slot belongs to the deferred apply
// hold, whose heading `advanceDeferredHoldHeading` advances to "Verifying…"
// when the hold resolves. Handing it this heading would replace the very
// instruction this screen exists to show.
// `note` is the WHY, for the one caller that has one: a retake this page fired
// itself says so here rather than starting a round the household never asked
// for and never sees explained.
function renderRetakeInProgress(ctx, { index, note = "" }) {
  const entry = entryForIndex(ctx.spec, index);
  const screenCopy = (entry && entry.screen) || {};
  const { target } = planTargetAndAttempts(ctx.spec);
  renderStepScreen(ctx, {
    progress: `${String(screenCopy.progress || `Measurement ${index} of ${target}`)} — again`,
    headline: String(screenCopy.title || "Measuring this spot again"),
    detail: String(screenCopy.body || ""),
    notes: note ? [el("p", { class: "cap-note", text: note })] : [],
  });
}

// The sentence a household reads when JTS gave up on the spot they just
// measured and carried on (owner ruling #2086 item 3). Without it the advance
// past an unresolved position is indistinguishable from a clean one, which is
// the "kill-and-lie"'s quieter cousin: continue-and-imply.
const UNRESOLVED_POSITION_OUTCOME =
  "JTS left that spot out and the group continued. The speaker page shows what it "
  + "worked out from the rest.";

function unresolvedPositionNote(unresolved) {
  const diagnosis = String((unresolved && unresolved.diagnosis) || "").trim();
  if (diagnosis) return `${diagnosis} ${UNRESOLVED_POSITION_OUTCOME}`;
  return `JTS could not get a clean read at that spot, so ${UNRESOLVED_POSITION_OUTCOME}`;
}

function renderPlanNext(ctx, { index, attempt, target, unresolved = null }) {
  // The UPCOMING capture's own entry (§5.7), when the plan carries one —
  // its progress/title/body drive the grammar; a v1/v2 plan (or an entry with
  // no `screen`) falls back to the generic "Measurement N of target" copy,
  // unchanged.
  const upcoming = entryForIndex(ctx.spec, index + 1);
  const screenCopy = (upcoming && upcoming.screen) || {};
  const next = button(stepPrimaryLabel(upcoming), async () => {
    next.disabled = true;
    try {
      await runPlanCapture(ctx, { index: index + 1, attempt: attempt + 1 });
    } finally {
      next.disabled = false;
    }
  });
  renderStepScreen(ctx, {
    progress: String(screenCopy.progress || ""),
    // A "✓" would be a claim, and an unresolved position did not earn one. The
    // upcoming entry's own title wins as it always has; this only replaces the
    // entry-less fallback, which is the one that ticks.
    headline: String(
      screenCopy.title
      || (unresolved
        ? `Measurement ${index} of ${target} — left out`
        : `Measurement ${index} of ${target} ✓`),
    ),
    detail: String(screenCopy.body || "Ready for the next measurement."),
    notes: unresolved
      ? [el("p", { class: "cap-note", text: unresolvedPositionNote(unresolved) })]
      : [],
    primary: next,
    // Nothing to retake: the flow gave this spot up because its tries ran out,
    // so offering one more would be the dead affordance again.
    secondary: unresolved ? null : retakeControl(ctx, { index, attempt }),
  });
  // The screen carries the counter and the instruction; #status stops
  // repeating them (§2.1 — the two counters used to disagree).
  clearStatus();
}

// "All spots done — continue" (§2.6). The pre-apply cloud's final position is
// accepted but its group is NOT closed yet: the Pi stashed the combine and is
// waiting for a begin PAST the group before it fits and applies. That pause is
// the whole point — until this screen existed, the final position was the one
// capture in the session a household could not choose to redo, because the
// speaker was already being retuned by the time they could ask.
//
// Deliberately does not name a spot count: the page has no trusted field for
// the group's size (`kind_label` is display/telemetry only by contract), and
// inventing one from it would be a sentence that quietly goes wrong when a
// plan shape changes.
function renderPlanGroupConfirm(ctx, { index, attempt, target, unresolved = null }) {
  const current = entryForIndex(ctx.spec, index);
  const screenCopy = (current && current.screen) || {};
  const proceed = button("Continue", async () => {
    // The tap is spent the moment it lands — this screen is replaced either
    // way (the completion wait, then the end screen or a failure), so the
    // button is disabled rather than re-enabled in a finally.
    proceed.disabled = true;
    // WHICH move "Continue" is, read off the PLAN rather than assumed
    // (release-ordering contract, README "A new phone event both sides
    // need"). The page publishes before the Pi, so a new bundle legitimately
    // meets an OLD conductor whose plan still carries VERIFY past this group;
    // there the confirmation rides that next begin exactly as it always did.
    // On a measure-only plan there IS no next entry — its final position is
    // the capture target — so the confirmation is its own event. One
    // condition, derived from the spec in hand, and the page is correct
    // against both conductors instead of only the newer one.
    if (entryForIndex(ctx.spec, index + 1)) {
      advanceAfterAccepted(ctx, { index, attempt, target });
      return;
    }
    await completePlanCaptureSet(ctx, { index, attempt, target });
  });
  renderStepScreen(ctx, {
    progress: String(screenCopy.progress || ""),
    headline: unresolved
      ? `Measurement ${index} of ${target} — left out`
      : "All spots measured — ready to continue?",
    // THE LAST THING A HOUSEHOLD READS BEFORE THE INTERLUDE, so it is the
    // sentence that has to set the interlude up (work order D10). On a
    // measure-only plan JTS tunes nothing next — it works out what it heard
    // and the HOUSEHOLD decides — and saying otherwise here was the false
    // sentence PR-T3 deliberately left for this rung rather than improvising.
    //
    // Branched on the SAME predicate the tap itself branches on, so the page
    // stays correct against both conductors (README's release-ordering
    // contract: the page publishes first, so a new bundle legitimately meets
    // an OLD conductor whose plan still carries VERIFY past this group — and
    // there JTS really does tune next).
    detail: unresolved
      ? unresolvedPositionNote(unresolved)
      : (entryForIndex(ctx.spec, index + 1)
        ? "JTS tunes the speaker next. Retake this spot first if you want to."
        : "JTS works out what it heard, then you decide what to do about it "
          + "back on the speaker page. Retake this spot first if you want to."),
    primary: proceed,
    // Exhaustion settled the unresolved spot; another retake is unavailable.
    secondary: unresolved ? null : retakeControl(ctx, { index, attempt }),
  });
  clearStatus();
}

// The household's explicit "I am done measuring" (two-stage work order D1).
//
// Before the split this tap called `advanceAfterAccepted`, i.e. posted a
// begin for the NEXT entry — VERIFY's — and the Pi inferred the confirmation
// from that begin's index being past the cloud group. A measure-only plan has
// no next entry (its final cloud position IS `capture_target`), so there is no
// begin to carry the meaning and `parse_begin_capture` would refuse one
// anyway. The signal is its own authenticated event instead: the Pi, which has
// been holding the set open for exactly this, closes the group, fits the
// candidate, and ends the session — and the household goes back to the speaker
// page to decide whether to apply it.
async function completePlanCaptureSet(ctx, { index, attempt, target }) {
  const controller = ctx.planController;
  try {
    setStatus(CAPTURE_CLOSING_MESSAGE, "info");
    await ctx.client.postEvent({ complete_capture_set: true });
    if (controller && controller.aborted) return;
    const verdict = await waitForCaptureSetComplete(
      ctx.client, ctx.spec, () => Boolean(controller && controller.aborted),
    );
    if (controller && controller.aborted) return;
    if (verdict.deadSession) {
      renderSessionExpired(ctx);
    } else if (verdict.setExhausted) {
      renderPlanExhausted(ctx, { ...verdict, target, index });
    } else if (verdict.refused) {
      // TERMINAL. The Pi refused ON the confirmation — the pre-apply
      // accountability veto is the shipped case — and re-raised, so the
      // session is already gone: failure persisted, volume abandoned, relay
      // purged. `renderPlanRefused` is the page's existing terminal for
      // exactly this, and it offers no begin affordance, which is the point:
      // a retry screen here would put a "Try again" button in front of a
      // household whose session no longer exists.
      renderPlanRefused(ctx, { error: verdict.error });
    } else {
      renderPlanAllDone(ctx, { index });
    }
  } catch (err) {
    if (controller && controller.aborted) return;
    // Post-arm in every sense that matters: the captures are all consumed and
    // the Pi owns what happens next, so there is nothing safe to re-tap here.
    renderSweepFailed(ctx, err);
  }
  await endPlanSession(ctx);
}

// Wait for the Pi to close a HELD set. Deliberately not `waitForCaptureResult`:
// the relay's host-event slot still holds this slot's own accepted
// `capture_result` (the one this screen was rendered from), so that function
// would match it immediately and route straight back to the confirm screen.
// Only session-level outcomes count here — plus a NEW terminal verdict, which
// is how a refused group close (the pre-apply accountability veto) reaches the
// household instead of timing out.
async function waitForCaptureSetComplete(client, spec, isAborted) {
  const pollMs = Math.max(100, Math.min(1000, Number(spec.progress_poll_ms) || 250));
  // The group close runs the combine and the fit — the slowest analysis of the
  // session. Same budget the Pi gives the household's own between-step taps,
  // so neither side gives up while the other is still working.
  const deadline = Date.now() + CAPTURE_SET_COMPLETE_TIMEOUT_MS;
  // The session's slowest analysis (the combine plus the fit) behind one static
  // line was the worst dead-air window on the page — D9.
  const tick = waitProgress(CAPTURE_CLOSING_MESSAGE, deadline);
  let reconnecting = null;
  while (Date.now() < deadline) {
    if (isAborted()) return { aborted: true };
    if (!reconnecting) tick();
    let status;
    try {
      status = await pollPhoneStatus(client);
    } catch (err) {
      if (isDeadSessionError(err)) return { deadSession: true };
      if (isRelayConnectivityAbort(err, String((err && err.message) || err))) {
        reconnecting = err;
        setStatus(RELAY_RECONNECTING_RESULT_MESSAGE, "info");
        await delayMs(pollMs);
        continue;
      }
      throw err;
    }
    if (reconnecting) {
      reconnecting = null;
      tick();
    }
    const event = (status && status.host_event) || {};
    const phase = String(event.phase || "");
    if (phase === "capture_set_complete") return { setComplete: true };
    if (phase === "capture_set_exhausted") {
      return {
        setExhausted: true,
        accepted: Number(event.accepted) || 0,
        attempts: Number(event.attempts) || 0,
        // WHICH clock ran out, when the session ended on one (work order D8).
        // Absent from the RUNNER's own exhaustion event — that really is the
        // attempt limit — and absent from an older Pi, both of which keep the
        // attempt-limit copy.
        budget: event.budget ? String(event.budget) : "",
      };
    }
    // TERMINAL, both of them. `capture_refused` is always terminal for the
    // whole session (see waitForCaptureAuthorized's own note) and the held-set
    // refusal is no exception: the Pi publishes it and RE-RAISES, so by the
    // time the phone reads it the failure is persisted, the volume abandoned
    // and the relay session purged. A `capture_result` carrying
    // `accepted: false` this late is the host's own terminal event, addressed
    // to the last armed capture — equally dead. Neither may route to a retry
    // screen: the only thing a "Try again" could do is post into a purged
    // session.
    if (phase === "capture_refused") {
      return {
        refused: true,
        code: event.code ? String(event.code) : "",
        error: event.error ? String(event.error) : String(event.reason || ""),
      };
    }
    if (phase === "capture_result" && event.accepted === false) {
      return {
        refused: true,
        code: event.code ? String(event.code) : "",
        error: event.error ? String(event.error)
          : (event.reason ? String(event.reason) : String(event.banner || "")),
      };
    }
    await delayMs(pollMs);
  }
  if (reconnecting) throw reconnecting;
  const failure = new Error(
    "the speaker did not finish this measurement before the timeout",
  );
  failure.sweepFailed = true;
  throw failure;
}

// ---------------------------------------------------------------------------
// Witness-triggered auto-retake (issue #2557, phase B).
//
// WHAT THIS IS. When a take's OWN pre-upload scan witnessed the render-quantum
// splice (`witnessedZeroFillSplice`) and the host refused that take, the page
// presses its own "Try again" instead of waiting for a thumb. Nothing else
// changes: it is the SAME begin the button posts, on the same budget, and the
// host's residual-desync classifier is still what refused the take. The page
// adds only the fact that it already knows why, at capture time, on evidence it
// measured itself.
//
// WHY IT CANNOT FIRE EARLIER — before the upload, when the scan actually runs.
// See capture-integrity.js's header: an armed capture has exactly two exits,
// upload → verdict or a TERMINAL error, and there is no "this take is dead,
// give me the next attempt" signal. A local abort would not return the spent
// attempt, it would destroy the session. So the witness rides the upload, the
// host judges, and the auto-retake happens exactly where the household's own
// retry does.
//
// THE FOUR BOUNDS, all of them load-bearing:
//
//  - THE BUDGET IS THE HOUSEHOLD'S, and this spends from it rather than
//    minting. It fires only while the position's published extras and the
//    plan's own attempt ceiling both cover another try — and only when the
//    host actually published a count, which is stricter than `armRetakeSlot`'s
//    rule for the tapped control on purpose: a human pressing a button has
//    declared their intent, and a page spending an attempt it cannot count has
//    not.
//  - ONE PER MEASUREMENT. A second witness at the same spot is not a browser
//    hiccup to paper over; it is a pattern the household should see. The
//    ledger below is that bound, and the same entry is what puts the
//    disclosure note on every later screen for that measurement.
//  - GLITCH ONLY, NEVER GEOMETRY. A verdict carrying a `prompt` is the host
//    asking the operator to MOVE (the position-group geometry rung), and
//    re-measuring from the same spot is the one thing that cannot answer it —
//    see waitForCaptureResult's note on the same field. Those wait for a thumb,
//    whatever this page's scan saw.
//
//    THE `prompt` PROXY RESTS ON A HOST-SIDE INVARIANT, named here because it
//    is not this file's to keep: every geometry rejection either CARRIES a
//    prompt (`cloud_geometry_locked`, the wider-spot ask) or is TERMINAL
//    (`geometry_retake_unreachable` — the remote positioner that cannot reach
//    the pose — whose `retry_budget` of 0 puts it in the host's
//    `NON_RETRIABLE_CODES`, so `verdict.terminal` returns above long before
//    this branch). A future geometry reason that is retriable AND prompt-less
//    would fall through to the page as an ordinary rejection and could be
//    auto-retaken from a spot the host just said was wrong. That is why the
//    invariant is pinned host-side rather than assumed:
//    `test_capture_page_auto_retake_never_answers_a_geometry_ask` in
//    tests/test_capture_page_js.py fails the moment a geometry reason is
//    neither prompted nor terminal.
//  - THE REJECTION STAYS HONEST. Every path that does not fire falls through to
//    `renderPlanRetry` unchanged, so a spent budget, a second witness, or a
//    geometry prompt renders the refusal the household would have seen anyway.
// ---------------------------------------------------------------------------

// Which measurements have spent their one automatic retake, and the attempt
// each one was fired FROM. One piece of state, three readers: the one-per-
// measurement bound, the on-screen disclosure, and the wire marker below.
function autoRetakeLedger(ctx) {
  if (!ctx.autoRetakes) ctx.autoRetakes = new Map();
  return ctx.autoRetakes;
}

// The disclosure the WIRE carries, set only on the round this page fired
// itself: the ledger's entry for this measurement names the attempt the
// witness was seen on, so the round after it — and only that one — is the
// automatic take. Returns null everywhere else, and an absent key is the
// ordinary "a household started this" case.
function autoRetakeMarker(ctx, { index, attempt }) {
  const firedFrom = autoRetakeLedger(ctx).get(index);
  if (firedFrom !== attempt - 1) return null;
  return { reason: AUTO_RETAKE_ZERO_FILL_REASON, after_attempt: firedFrom };
}

// Fire the page's own retake for a refused take that witnessed the splice.
// Returns true when it fired — the caller renders the ordinary rejection
// screen when it did not.
function autoRetakeWitnessedSplice(
  ctx, { index, attempt, retake, prompt, attempts, integrity },
) {
  if (!witnessedZeroFillSplice(integrity)) return false;
  if (prompt) return false;
  if (autoRetakeLedger(ctx).has(index)) return false;
  const { maxAttempts } = planTargetAndAttempts(ctx.spec);
  if (!attempts || attempts.left <= 0 || attempt + 1 > maxAttempts) return false;
  autoRetakeLedger(ctx).set(index, attempt);
  // The household is looking at the screen the moment this starts, so SAY it
  // here — the round itself paints no screen of its own, exactly as it does
  // not for a tapped retake (see retakeControl).
  renderRetakeInProgress(ctx, { index, note: AUTO_RETAKE_MESSAGE });
  scheduleAutoBegin(ctx, { index, attempt: attempt + 1, retake });
  return true;
}

function renderPlanRetry(
  ctx,
  {
    index, attempt, target, reason, prompt, retake = false, attempts = null,
    integrity = null,
  },
) {
  // The CURRENT capture's own entry, if any — its progress and (absent
  // server guidance) its instruction carry over; the rejection `reason`
  // always wins the detail slot.
  const current = entryForIndex(ctx.spec, index);
  const screenCopy = (current && current.screen) || {};
  const message = reason || "That measurement didn't pass the speaker's quality check.";
  // A server-supplied `prompt` is an INSTRUCTION, not a restatement: the
  // position-group geometry retake sends "move the microphone 30 in (75 cm)
  // to the LEFT of the mark, at mark height", which the operator has to act on
  // before tapping. Under the §2.4 grammar the instruction IS the headline (that is
  // the slot the household reads first) and the reason sentence becomes the
  // detail underneath — what to do over what happened, without losing either.
  const guidance = prompt ? String(prompt) : "";
  const headline =
    guidance ||
    // The eyebrow already carries "Measurement N of T — extra try 2 of 3", so
    // the entry-less fallback headline must NOT count again (it used to read
    // "Measurement N of T needs another try" directly under it). A plan with
    // no per-entry copy has no instruction to offer, so this is the plainest
    // imperative that is still true.
    String(screenCopy.title || "Take that measurement again");
  const retry = button(guidance ? STEP_PRIMARY_LABEL : "Try again", async () => {
    retry.disabled = true;
    try {
      // A rejected VOLUNTARY retake keeps the marker: without it this begin
      // is (accepted_count, attempts_used + 1) with no retake flag, which the
      // runner refuses as out-of-order — fatal to the session.
      await runPlanCapture(ctx, { index, attempt: attempt + 1, retake });
    } finally {
      retry.disabled = false;
    }
  });
  // Who spent the extras, when it was not all the household (ruling item 4).
  // Its own quiet line rather than folded into the reason sentence: the reason
  // is the Pi's copy about what happened acoustically, and this is about the
  // household's own patience being spent on their behalf.
  const attribution = extraTryAttribution(attempts);
  // The likely cause, but ONLY when this page measured it (#2151). The host's
  // own rejection copy stays a claim about the acoustics; this note is the
  // page reporting something it observed on the take being retried, so it
  // never guesses at a cause it has no evidence for.
  const notes = [];
  if (attribution) notes.push(el("p", { class: "cap-note", text: attribution }));
  if (integrity && integrity.focus_lost) {
    notes.push(el("p", { class: "cap-note", text: INTEGRITY_LOST_FOCUS_NOTE }));
  }
  // …and the automatic try this page already spent at this spot, if it did.
  // Read off the ledger rather than THIS take's report: the household needs to
  // know an attempt went that way whichever take is on screen now, and it is
  // also why the button in front of them is the only try left to that budget.
  if (autoRetakeLedger(ctx).has(index)) {
    notes.push(el("p", { class: "cap-note", text: AUTO_RETAKE_SPENT_NOTE }));
  }
  renderStepScreen(ctx, {
    progress:
      `${String(screenCopy.progress || `Measurement ${index} of ${target}`)}`
      + extraTrySuffix(attempts),
    headline,
    detail: message,
    notes,
    primary: retry,
    // THE ESCAPE FROM A REJECTED VOLUNTARY RETAKE (review blocker B1). This
    // slot is ALREADY accepted — the design's fail-safe is that a rejected
    // retake leaves the original take standing, so the household must be able
    // to keep it and move on. Without this the only control is "Try again",
    // which re-measures a slot that does not need re-measuring and can burn
    // the attempt budget until the session dies with the fit and apply never
    // fired. The forward begin is legal here: a rejected retake leaves
    // `accepted_count` unchanged, `attempts_used` at this attempt, and
    // `next_begin_seen` false, so (accepted_count + 1, attempts_used + 1) —
    // exactly what advanceAfterAccepted posts from these same args — is the
    // pair the runner is waiting for.
    secondary: retake ? keepEarlierTakeControl(ctx, { index, attempt, target }) : null,
  });
  clearStatus();
}

// "Keep the earlier measurement and continue" — the B1 escape. Returning
// forward re-opens the retake offer (the runner's window is still open and the
// budget check still applies), so the screen it lands on is the one the
// household would have seen had they never tapped Retake.
function keepEarlierTakeControl(ctx, { index, attempt, target }) {
  return button("Keep the earlier measurement and continue", () => {
    armRetakeSlot(ctx, { index, attempt });
    if (ctx.retakeAwaitingConfirm) {
      // The rejected retake was of the cloud's FINAL position: returning to
      // the group-close confirm keeps the fit + apply behind the same tap
      // they were behind before.
      renderPlanGroupConfirm(ctx, { index, attempt, target });
      return;
    }
    advanceAfterAccepted(ctx, { index, attempt, target });
  }, true);
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
  renderStepScreen(ctx, {
    progress: String(screenCopy.progress || ""),
    headline: heading,
    detail: message,
    captureHeading: true,
  });
  // A genuine transient state, so this one stays: the household is looking at
  // a screen with nothing to tap and needs to know something is happening.
  setStatus(`Waiting — ${message}`, "info");
}

// The post-apply confirmation (§2.2), the step-11 fix. VERIFY's begin is
// posted IMMEDIATELY, as it always was — each deferred re-post re-arms the
// host's hold clock, and sitting tap-first in `awaiting_begin` would hit
// REVIEW_HOLD_BUDGET_S and kill the session — so the ordering here is
// begin-first, THEN confirm: while the apply runs the hold screen instructs
// the walk back to the mark; once authorization lands this screen asks the
// household to confirm they are standing there, and the tone waits for the
// tap. That wait sits in the runner's `awaiting_arm` phase (DEFAULT_TIMEOUT_S,
// 120 s), comfortably past a minute of walking back.
//
// The copy rides `confirm_title`/`confirm_body` — NEW keys an older cached
// bundle ignores, which is why VERIFY's `title`/`body` were left as the hold
// copy: a phone that predates this build renders exactly today's flow.
function renderPlanConfirm(ctx, { index, onConfirm }) {
  // Only reached for an entry whose `confirm_title` is set
  // (entryConfirmsBeforeArming is the gate), so there is no fallback headline
  // to invent here.
  const entry = entryForIndex(ctx.spec, index);
  const screenCopy = (entry && entry.screen) || {};
  const progress = String(screenCopy.progress || "");
  const headline = String(screenCopy.confirm_title);
  const detail = String(screenCopy.confirm_body || "");
  const confirm = button(STEP_PRIMARY_LABEL, () => {
    confirm.disabled = true;
    // The tap is spent — re-render the same instruction WITHOUT its
    // affordance so nothing tappable survives into the recording window, and
    // hand the heading to advanceDeferredHoldHeading exactly as the hold
    // screen does.
    renderStepScreen(ctx, { progress, headline, detail, captureHeading: true });
    onConfirm();
  });
  renderStepScreen(ctx, { progress, headline, detail, primary: confirm });
  clearStatus();
}

// Whether this entry gates its tone behind a post-apply confirmation. Absent
// keys (every other entry, and any plan built before the redesign) mean "arm
// as soon as the Pi authorizes" — byte-identical to the shipped behavior.
function entryConfirmsBeforeArming(spec, index) {
  const entry = entryForIndex(spec, index);
  const screenCopy = (entry && entry.screen) || {};
  return Boolean(screenCopy.confirm_title);
}

// Park until the household taps (or the session ends under them — Stop and
// the visibility abort both settle the wait through the controller, so a
// stopped session never leaves this promise dangling). No page-side timer
// arms here: the tap budget is the host's, and inventing a second deadline
// would only race it.
function awaitPlanConfirmation(ctx, { index }) {
  return new Promise((resolve) => {
    ctx.pendingConfirm = resolve;
    renderPlanConfirm(ctx, { index, onConfirm: () => resolvePendingConfirm(ctx) });
  });
}

// Idempotent: the tap, Stop, the visibility abort, and the session teardown
// can all reach it, and only the first one settles the wait.
function resolvePendingConfirm(ctx) {
  const pending = ctx && ctx.pendingConfirm;
  if (!pending) return;
  ctx.pendingConfirm = null;
  pending();
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
    setText(ctx.captureRefs.heading, "Verifying…");
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
// liveness), after a countdown elapses, and by the witness-triggered auto
// retake below.
//
// `retake` carries the same marker the tapped controls carry, because the
// runner admits a re-measure of an ALREADY-ACCEPTED slot only when it is
// marked: an auto-fired round that dropped it would be refused as
// `begin_out_of_order`, which ends the whole session.
function scheduleAutoBegin(ctx, { index, attempt, retake = false }) {
  clearAutoAdvance(ctx);
  const controller = ctx.planController;
  ctx.autoAdvanceTimer = setTimeout(() => {
    ctx.autoAdvanceTimer = null;
    if (controller && controller.aborted) return;
    void runPlanCapture(ctx, { index, attempt, retake });
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
    // Blank the counter on the way out, exactly like the retake tap below
    // (#1823 / #1824 D5). Without this the auto-begin path left "Starting
    // in 1…" frozen on screen for the whole capture that had ALREADY started
    // — the countdown screen's notes survive into the round, and the round's
    // own progress goes to #status, so nothing else ever repainted it.
    //
    // NOTE no SHIPPED plan reaches this today: MEASURE was the only countdown
    // entry and #1823 made it a tap. The countdown vocabulary is retained for
    // a future same-spot transition, so the bug is fixed where it lives rather
    // than left to come back with it.
    setText(counter, "");
    void runPlanCapture(ctx, { index: nextIndex, attempt: nextAttempt });
  };
  const cancel = button("Cancel", () => {
    // Cancel drops to a manual begin affordance (the countdown is cancelable
    // per §5.2) — the household starts when ready.
    clearAutoAdvance(ctx);
    renderPlanNext(ctx, { index, attempt, target });
  }, true);
  // A post-capture screen, so the just-finished capture is still retakeable
  // (§2.6) — the control is deliberately NOT registered as the begin
  // affordance below: the countdown still owns the forward path, and its
  // auto-begin shuts the retake window the moment it fires (see
  // retakeControl's re-check).
  renderStepScreen(ctx, {
    progress: String(screenCopy.progress || ""),
    headline: heading,
    detail: body,
    notes: [counter],
    primary: null,
    secondary: cancel,
    extraActions: [
      retakeControl(ctx, {
        index,
        attempt,
        // The counter stops being true the moment the countdown is cancelled
        // behind this tap; blank it rather than freezing "Starting in 3…" on
        // screen for the whole retake.
        onTap: () => { setText(counter, ""); },
      }),
    ],
  });
  // The countdown itself is on-screen; #status stops mirroring it (§2.1).
  clearStatus();
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
    setText(counter, `Starting in ${seconds}…`);
  }, 1000);
}

// After an accepted capture, route to the next screen by the UPCOMING entry's
// auto-advance policy (§5.2). on_apply → the hold owns the screen and the
// deferred loop auto-posts the begin (no tap); countdown → a cancelable
// countdown; tap (or a plan with no policy) → the manual "Next measurement".
function advanceAfterAccepted(ctx, { index, attempt, target, unresolved = null }) {
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
  renderPlanNext(ctx, { index, attempt, target, unresolved });
}

// `index` (the just-completed FINAL wire index, 1-based) is optional — most
// callers of this shared plan-completion screen (room sweep, sync, balance)
// have no per-flow completion copy and get the generic text below. A flow that
// wants its own end copy carries `done_title`/`done_body` on the LAST plan
// entry, so this shared screen needs no flow-specific branch.
//
// For crossover-v2 that entry is STAGE 2's tail since the two-stage split —
// the post-apply group's last position at Full, the anchor at Express (see
// build_v2_verify_capture_plan). Stage 1's plan deliberately carries no done
// copy at all: it is not the end of the journey, and the household's next step
// is a decision on the speaker page.
//
// The fallback therefore states the ONE thing true of every flow that reaches
// it — the measuring is over and the speaker page owns what happens next. It
// used to promise "the speaker continues automatically", which was exactly
// false for a stage-1 session: it deliberately does not continue, it waits for
// a decision. Naming the speaker page instead is also true of the room sweep,
// sync, and balance plans that share this screen, so no flow needs a branch
// here to be told the truth (a flow that wants its own end copy still carries
// `done_title`/`done_body` on its LAST entry, which wins over this).
function renderPlanAllDone(ctx, { index, unresolved = null } = {}) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const entry = typeof index === "number" ? entryForIndex(ctx.spec, index) : null;
  const screenCopy = (entry && entry.screen) || {};
  const heading = unresolved
    ? `Measurement ${index} — left out`
    : String(screenCopy.done_title || "All measurements done");
  const body = unresolved
    ? unresolvedPositionNote(unresolved)
    : String(
      screenCopy.done_body ||
        "All measurements done — the speaker page shows what happens next.",
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

// A capture ran, spent this slot's final available extra and established that
// the set cannot continue. This is not an admission refusal and not a retry:
// the Pi publishes it as the final capture_result, then ends the runner.
function renderPlanTerminal(ctx, verdict) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const message = verdict.error || verdict.reason || verdict.banner
    || "The measurement cannot continue.";
  const children = [
    el("h1", { class: "cap-heading", text: "Measurement cannot continue" }),
    el("p", { class: "cap-note", text: message }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", { class: "cap-note", text: "You can close this tab." }));
  }
  setScreen(ctx.screenEl, children);
  setStatus(message, "error");
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

// WHICH clock ran out, and what an expiry preserved (work order D8, issue
// #1807). The Pi tells the phone this on the session-over event's `budget`
// field; an older Pi sends none and the attempt-limit copy below is unchanged.
//
// The counts are the page's OWN (the capture it was on, out of the plan's
// target) rather than a new wire field: the phone already knows both, and the
// honest claim is about progress, not about a resumable set — a new session
// starts a fresh walk, so "you got N of T in" must not be dressed up as "N are
// saved for next time".
function expiredBudgetCopy(ctx, verdict) {
  const budget = planTimeBudget(ctx.spec);
  const reached =
    Number(verdict.index) > 0 && Number(verdict.target) > 0
      ? ` You got to measurement ${Number(verdict.index)} of ${Number(verdict.target)}.`
      : "";
  const restart =
    " A tune needs the whole set in one session, so the speaker page will" +
    " start you a fresh link.";
  // With no published budget the clock is still NAMED — that is the part the
  // household needs — but no duration is quoted, because the page would be
  // inventing it. Same rule as everywhere else in this layer.
  if (verdict.budget === "step") {
    return {
      heading: "That step timed out",
      body:
        (budget
          ? `The speaker waits ${approxMinutes(budget.stepS)} between taps and` +
            " that window passed, so it ended the session."
          : "The speaker stopped waiting for your next tap, so it ended the" +
            " session.") + `${reached}${restart}`,
    };
  }
  if (verdict.budget === "link") {
    return {
      heading: "This link ran out",
      body:
        (budget
          ? `A measurement link lasts ${approxMinutes(budget.sessionS)} from` +
            " when it is created, and this one is past that."
          : "This measurement link is past the time it lasts.") +
        `${reached}${restart}`,
    };
  }
  // NEITHER clock ran out (issue #2083). The Pi says this explicitly rather
  // than by omission, because omission lands on the attempt-limit copy below —
  // and "the speaker reached its measurement attempt limit" is a third claim
  // that is just as untrue as naming a clock would be. No duration is quoted
  // because no budget is involved: nothing about time went wrong.
  if (verdict.budget === "none") {
    return {
      heading: "The speaker lost its connection",
      body:
        "The speaker lost its connection to the measurement service, so it" +
        ` ended the session.${reached}${restart}`,
    };
  }
  return null;
}

function renderPlanExhausted(ctx, verdict) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const expired = expiredBudgetCopy(ctx, verdict);
  const children = [
    el("h1", {
      class: "cap-heading",
      text: expired ? expired.heading : "Reached the attempt limit",
    }),
    el("p", {
      class: "cap-note",
      text: expired
        ? expired.body
        : `The speaker reached its measurement attempt limit (${verdict.accepted} of ` +
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
    expired
      ? expired.body
      : "Reached the measurement attempt limit. The speaker page shows what happens next.",
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
      status = await pollPhoneStatus(client);
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
  // here. Give the bounded computation enough headroom for anchor analysis,
  // result publication, polling, and loaded-Pi variance.
  const deadline = Date.now() + Math.max(
    resultWaitMs(spec), Number(spec.duration_ms) || 0,
  );
  // 20-40 s of blind analysis per capture, under one static line, was the
  // owner's "I did not know whether to wait or give up" (work order D9).
  const tick = waitProgress(CAPTURE_CHECKING_MESSAGE, deadline);
  // The swallowed abort still in force, or null once the relay answers — same
  // role as waitForSweepComplete's, for the same reason (see its expiry).
  let reconnecting = null;
  while (Date.now() < deadline) {
    if (isAborted()) return { aborted: true };
    if (!reconnecting) tick();
    let status;
    try {
      status = await pollPhoneStatus(client);
    } catch (err) {
      if (isDeadSessionError(err)) return { deadSession: true };
      // Same bound as the sweep wait (#1824 D1): a control request that
      // outlived RELAY_CONTROL_TIMEOUT_MS costs one poll interval, never the
      // session. This wait is squarely POST-arm — the sweep already played —
      // so ending here would discard a capture the Pi is actively analyzing.
      if (isRelayConnectivityAbort(err, String((err && err.message) || err))) {
        reconnecting = err;
        setStatus(RELAY_RECONNECTING_RESULT_MESSAGE, "info");
        await delayMs(pollMs);
        continue;
      }
      throw err;
    }
    if (reconnecting) {
      reconnecting = null;
      tick();
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
        // The relay host slot is last-write-wins: completion may replace the
        // final capture_result before this poll, so the Pi repeats the final
        // unresolved-position disclosure here.
        unresolved: event.unresolved && typeof event.unresolved === "object"
          ? {
              code: String(event.unresolved.code || ""),
              diagnosis: String(event.unresolved.diagnosis || ""),
            }
          : null,
      };
    }
    if (phase === "capture_set_exhausted") {
      return {
        setExhausted: true,
        accepted: Number(event.accepted) || 0,
        target: Number(event.capture_target) || target,
        attempts: Number(event.attempts) || attempt,
        budget: event.budget ? String(event.budget) : "",
      };
    }
    if (
      phase === "capture_result" &&
      Number(event.index) === index &&
      Number(event.attempt) === attempt
    ) {
      // The Pi's rejection verdict is richer than `error` alone: the v2
      // conductor's PhaseVerdict relays `reason` (what happened, in household
      // copy), `banner` (the short form for auto-retrying codes), `code`, and
      // — for a position-group geometry retake — `prompt`, the "move further
      // out" instruction that is the entire point of asking again. Before this
      // extraction only `accepted`/`error` survived, so every one of those
      // reached the phone and was dropped on the floor: the operator saw the
      // generic quality-check line and retook from the SAME spot, which is the
      // one thing that cannot fix a geometry lock.
      //
      // Backwards-compatible both ways: an older Pi sends none of these and
      // the generic fallback still renders; an older page ignores the fields
      // and behaves exactly as it does today.
      return {
        accepted: event.accepted === true,
        error: event.error ? String(event.error) : "",
        reason: event.reason ? String(event.reason) : "",
        banner: event.banner ? String(event.banner) : "",
        prompt: event.prompt ? String(event.prompt) : "",
        code: event.code ? String(event.code) : "",
        // The host decided on THIS capture that the set cannot continue. The
        // runner publishes this result and exits; no next begin exists.
        terminal: event.terminal === true,
        terminalOutcome: event.terminal_outcome
          ? String(event.terminal_outcome)
          : "",
        // §2.6: the pre-apply cloud is walked but its group is NOT closed —
        // the Pi is waiting for a begin PAST the group before it fits and
        // applies. An older Pi never sends the key and the page advances
        // exactly as it does today.
        awaitingConfirm: event.awaiting_confirm === true,
        // THIS position's honest attempt count (owner ruling #2086 item 2).
        // The screen used to say "one more try" on every rejection while the
        // Pi ran a hidden meter to twelve; these are the numbers that stop it.
        attempts: attemptCount(event.attempts),
        // Set when the Pi gave up on this position and moved the group on. The
        // verdict reads `accepted` because that is the relay's only "this slot
        // is done" signal — it is NOT a claim the spot was measured, and the
        // next screen must not imply one.
        unresolved: event.unresolved && typeof event.unresolved === "object"
          ? {
              code: String(event.unresolved.code || ""),
              diagnosis: String(event.unresolved.diagnosis || ""),
            }
          : null,
      };
    }
    await delayMs(pollMs);
  }
  // Still blind when the window ran out: report the outage, not a verdict
  // about a speaker we could not hear from. Mirrors waitForSweepComplete's
  // expiry — the caller's post-arm branch renders the same terminal either
  // way, so the only thing that changes is whether the household is told the
  // truth about WHY. Left unmarked (no `sweepFailed`) exactly like the sweep
  // wait's: `armedPosted` is necessarily true this late, so the routing is
  // identical, and the v1 caller reads it as the retryable connectivity case
  // it is.
  if (reconnecting) throw reconnecting;
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
async function beginAndAwaitAuthorization(ctx, { index, attempt, retake = false }) {
  const { spec, client } = ctx;
  const controller = ctx.planController;
  const { target } = planTargetAndAttempts(spec);
  // The deferred hold is the page's one UNBOUNDED wait: it re-posts the same
  // begin every 1.5 s until the Pi is ready, and before this it sat on one
  // static line until the Pi's own step budget killed the session (work order
  // D9 / issue #1840's page half). The page cannot shorten that budget, but it
  // can say how long it has been waiting and what the budget is — built lazily
  // so the clock starts at the FIRST deferral, which is when the household
  // starts wondering.
  const budget = planTimeBudget(spec);
  let deferredTick = null;
  for (;;) {
    // No counter here any more (§2.1): the screen's eyebrow owns it, and this
    // line used to number the same walk differently.
    setStatus("Asking the speaker to start…", "info");
    // ONE seam for the whole begin exchange (issue #2517). Both halves — the
    // post AND the admission poll — raise the same relay-connectivity abort,
    // and until this fix EITHER of them turned a one-blip outage into the
    // "Tap … to try again" park below runPlanCapture's pre-arm branch. An
    // unattended remote session has no thumb to offer, so a blip at exactly
    // the admission moment became a 120 s `awaiting_arm` expiry and a failed
    // stage. Retrying the exchange as a whole (rather than each call
    // separately) is what makes the budget ONE budget, and what lets a blip on
    // the poll recover by re-posting rather than needing its own path.
    //
    // Re-posting is safe here, and the Pi is the reason — verified against
    // jasper/capture_relay/session.py's `run_capture_plan`:
    //   * it acts on a begin only when the SIGNED sequence advances
    //     (`if sequence > begin_handled_sequence`), and `RelayClient.postEvent`
    //     mints a fresh sequence per post, so a re-post is read exactly once;
    //   * a re-post of a pair already admitted is a documented no-op while the
    //     Pi still holds it — `if pair in processed:` refuses `begin_replayed`
    //     ONLY when `phase == "awaiting_begin" or pair != current`, and this
    //     window is pre-arm, so the Pi is either still `awaiting_begin` for
    //     this pair (an ordinary admission) or `awaiting_arm` on it (the
    //     "nothing to do" branch);
    //   * the deferral `continue` below already re-posts this identical begin
    //     every 1.5 s, unboundedly, for the same reason.
    const admission = await withRelayReconnect(
      async () => {
        await client.postEvent({
          begin_capture: beginCapturePayload({ index, attempt, retake }),
          setup: setupWirePayload(),
        });
        if (controller.aborted) return { aborted: true };
        return waitForCaptureAuthorized(
          client, spec, index, attempt, () => controller.aborted,
        );
      },
      () => controller.aborted,
      {
        backoffMs: RELAY_BEGIN_RECONNECT_BACKOFF_MS,
        // Rungs alone do NOT bound this: one attempt can sit in
        // waitForCaptureAuthorized's fresh 20 s admission window before the
        // blip that ends it. See RELAY_BEGIN_RECONNECT_BUDGET_MS.
        budgetMs: RELAY_BEGIN_RECONNECT_BUDGET_MS,
        message: RELAY_RECONNECTING_BEGIN_MESSAGE,
        tone: "info",
      },
    );
    // `admission` is undefined when withRelayReconnect bailed on the abort
    // check between re-sends.
    if (controller.aborted || !admission || admission.aborted) {
      return { aborted: true };
    }
    if (admission.deferred) {
      renderPlanDeferred(ctx, { index, target, reason: admission.reason });
      if (budget) {
        if (!deferredTick) {
          deferredTick = waitProgress(
            "Waiting for the speaker —", Date.now() + budget.stepS * 1000,
          );
        }
        deferredTick();
      }
      await delayMs(CAPTURE_DEFERRED_RETRY_POLL_MS);
      if (controller.aborted) return { aborted: true };
      continue; // re-post the SAME begin_capture and try again
    }
    return admission;
  }
}

// The wire shape of a `begin_capture` request. The optional `retake` marker
// (§2.6) is OMITTED unless it is true: jasper/capture_relay/session.py's
// parse_begin_capture admits exactly two key sets and requires the literal
// `true` when the key is present, so there is one wire shape per meaning and
// a page that never retakes is byte-identical to the pre-retake page.
function beginCapturePayload({ index, attempt, retake = false }) {
  return retake ? { index, attempt, retake: true } : { index, attempt };
}

// The retry screen's detail line when the repair below has to build that screen
// for a round that never started. `renderPlanRetry`'s own default says the
// measurement "didn't pass the speaker's quality check", which would be a claim
// about a take that was never recorded; the specific cause rides `#status`
// (`captureFailureMessage`), so this says only the part that is certain.
const PRE_ARM_NOT_STARTED_NOTE = "That measurement didn't start.";

// The label of the live begin affordance on the current plan screen —
// "I'm there — play the tone" / "Try again" / the spec's own Start-button
// label — so a pre-arm failure's retry copy names a button that actually
// exists (the plan screens have no button called "Start").
function planRetryAffordance(ctx) {
  const begin = ((ctx.captureRefs && ctx.captureRefs.buttons) || []).find(
    (entry) => entry && entry.action === "begin_capture",
  );
  const label = begin && begin.el ? String(begin.el.textContent || "").trim() : "";
  return label || "the measurement button";
}

// A pre-arm failure leaves the previous screen up — which is only safe when
// that screen's live control re-posts a pair the Pi will still accept. Two
// cases where it does not, both repaired here before the failure copy names a
// button (review S2):
//
//  - THIS ROUND WAS A RETAKE. The visible primary is the FORWARD path; tapping
//    it posts (index + 1, attempt + 1) while the Pi may be sitting in
//    `awaiting_arm` on the retake, which the runner refuses as out-of-order —
//    fatal. Re-arm the retake offer instead: its closure re-posts the
//    IDENTICAL (index, attempt) pair, which the runner tolerates (an
//    unadmitted pair is admitted; the in-flight pair is its own `current` and
//    is a no-op), and name that control.
//
//    …and PUT THAT CONTROL BACK ON SCREEN. Re-arming the slot alone was
//    sufficient only while a retake left the OFFERING screen up; since PR-T4 a
//    retake renders its own affordance-free in-progress screen
//    (`renderRetakeInProgress`, so the household reads the spot being
//    re-measured rather than the next one), and a failure there left a screen
//    with no controls at all under copy naming a button that was not on it —
//    Stop the only remaining tap, mid-walk. Probed: this arm rendered
//    `[]` where the pre-T4 build rendered both buttons. Re-rendering the
//    offering screen restores that state exactly, including its forward
//    primary; the copy still steers to the retake, which is the part that was
//    always doing the safety work.
//  - THE SCREEN HAS NO BEGIN AFFORDANCE AT ALL. A countdown's auto-begin is
//    the forward path, so its screen renders none; the copy would otherwise
//    name a button that does not exist. Drop back to the manual screen the
//    countdown's own Cancel produces, which has one.
//
//    …AND THAT DROP-BACK NEEDS A FLOOR AT INDEX 1 (#2557 phase B, gate B1).
//    `renderPlanNext(index - 1, …)` is how the affordance-free case is repaired,
//    and it has nothing to render for the FIRST measurement — there is no
//    position before it. That was unreachable while every index-1 round was
//    entered from a screen that already had a begin button (round 1 from the
//    spec's own Start, a retry from `renderPlanRetry`'s). The witness-triggered
//    auto-retake is the first affordance-free begin reachable at index 1, so the
//    floor is now load-bearing: without it the household reads
//    "Measuring this spot again", copy naming a button, and no button —
//    #2090's "silence is the only wrong outcome", exactly the state this whole
//    function exists to prevent. The retry screen for that same measurement is
//    the right floor: its primary re-posts the pair that just failed.
function repairPreArmAffordance(ctx, { index, attempt, target, retake }) {
  if (retake) {
    // `attempt - 1` throughout: this round is the retake, so the slot and the
    // screen that offered it both belong to the ACCEPTED attempt before it —
    // the same arithmetic `keepEarlierTakeControl` uses to return there.
    // Arming precedes the render, or `retakeControl`'s own `canRetake` guard
    // returns null and the screen comes back without the control this arm
    // exists to name.
    const restore = { index, attempt: attempt - 1, target };
    armRetakeSlot(ctx, restore);
    if (ctx.retakeAwaitingConfirm) {
      // The retake was of the cloud's FINAL position, so the offering screen
      // was the group-close confirm — the worst place to strand a household,
      // since the whole apply decision sits behind its Continue.
      renderPlanGroupConfirm(ctx, restore);
    } else {
      // `renderPlanNext` rather than `advanceAfterAccepted`: the latter routes
      // by the UPCOMING entry's auto-advance policy, and a countdown there
      // would auto-post the forward begin — precisely the out-of-order pair
      // this arm exists to keep the household away from.
      renderPlanNext(ctx, restore);
    }
    return RETAKE_LABEL;
  }
  const hasBegin = ((ctx.captureRefs && ctx.captureRefs.buttons) || []).some(
    (entry) => entry && entry.action === "begin_capture",
  );
  if (!hasBegin) {
    if (index > 1) {
      renderPlanNext(ctx, { index: index - 1, attempt: attempt - 1, target });
    } else {
      // `attempt - 1` for the same reason the branch above uses it: both
      // screens' primaries post ONE PAST what they are handed, so this is what
      // makes the button re-post the exact pair that just failed. It needs no
      // floor of its own — an index-1 round is only affordance-free after an
      // earlier attempt, so `attempt` is at least 2 here, and even at 1 the
      // resulting begin would be the correct (1, 1).
      renderPlanRetry(ctx, {
        index, attempt: attempt - 1, target, reason: PRE_ARM_NOT_STARTED_NOTE,
      });
    }
  }
  return planRetryAffordance(ctx);
}

// One capture round: begin -> Pi admission -> quiet window + sweep + upload
// (index-aware) -> Pi verdict -> the next screen. Invoked once from
// onPlanStart (index 1, attempt 1) and thereafter from a primary tap ("I'm
// there — play the tone" / "Try again" / "Continue") or a Retake tap
// (`retake: true`, §2.6 — the just-accepted slot on the next attempt).
async function runPlanCapture(ctx, { index, attempt, retake = false }) {
  const { spec, client } = ctx;
  const controller = ctx.planController;
  const { target } = planTargetAndAttempts(spec);
  // A fresh round cancels any pending auto-advance (e.g. a countdown, or a Cancel
  // that dropped to the manual tap then the tap fired) so no stale timer fires.
  clearAutoAdvance(ctx);
  // Posting ANY begin shuts the retake window this page offers — it re-opens
  // only on the next accepted verdict (armRetakeSlot). See retakeControl's
  // header for why an offer must never outlive the runner's own window.
  shutRetakeWindow(ctx);
  let disposeWatch = () => {};
  // Per-take integrity accounting (#2151) — passive bookkeeping alongside
  // `disposeWatch`'s session abort, never a second abort path. See
  // capture-integrity.js for why a focus loss is labelled rather than aborted.
  let integrityWatch = null;
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
    const admission = await beginAndAwaitAuthorization(ctx, { index, attempt, retake });
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

    // Confirm-then-tone (§2.2): an entry that carries `confirm_*` copy waits
    // for the household's tap AFTER the Pi authorizes — the VERIFY sweep must
    // not fire into a walk back to the mark. Every other entry (and every
    // pre-redesign plan) resolves immediately, so this is inert for them.
    if (entryConfirmsBeforeArming(spec, index)) {
      await awaitPlanConfirmation(ctx, { index });
      if (controller.aborted) return;
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
      // The session's mic is now open and reused for every remaining capture
      // (#1658) — the picker cannot change it any more, so it stops taking up
      // room above the instruction (§2.3).
      collapseMicPicker();
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
        `This device can't run a clean measurement (${capture.decision.reason}). ` +
          "Try a different device, or use a calibrated USB mic on the speaker.",
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

    const noise = await captureAmbientNoise(recorder, spec, entryNoiseNote(spec, index));
    if (controller.aborted) return;

    // Armed only for the recording window itself — the idle gap between rounds
    // is a legitimate time to look away, and warning then would be crying wolf.
    integrityWatch = createIntegrityWatch({
      onLoss: () => setStatus(INTEGRITY_LOST_FOCUS_MESSAGE, "error"),
    });
    recorder.start();
    setStatus(
      capture.decision.degraded
        ? `Recording at lower confidence — ${capture.decision.reason}. Waiting for the speaker.`
        : "Recording — waiting for the speaker to start.",
      "recording",
    );

    armedPosted = true;
    // Retried on a relay-connectivity abort (#1824 D2) rather than ending the
    // round: the Pi is sitting in `awaiting_arm` for this exact (index,
    // attempt) with a 120 s budget, the relay's phone-event slot is
    // last-write-wins, and the runner treats a repeat of the in-flight begin
    // as a no-op — so re-posting the IDENTICAL armed event is both safe and
    // the only way to use the slot the speaker is holding open. The recorder
    // is already running, so this recovers in place: no screen teardown, no
    // re-record, same capture.
    const armedEvent = {
      armed: true,
      degraded: capture.decision.degraded,
      device: capture.device,
      noise_floor: { duration_ms: noise.duration_ms, rms_dbfs: noise.rms_dbfs },
      // The armed event re-states its own begin context; carry the retake
      // marker here too, so a Pi poll that lands on THIS event rather than the
      // begin still reads the same (index, attempt, retake) request.
      begin_capture: beginCapturePayload({ index, attempt, retake }),
      setup: setupWirePayload(),
      acknowledgement: ctx.planAcknowledgement,
      ...ambientStatsFieldsFor(spec, noise),
    };
    await withRelayReconnect(
      () => client.postEvent(armedEvent),
      () => controller.aborted,
    );
    if (controller.aborted) return;

    const sweepCompleted = await waitForSweepComplete(
      client, spec, () => controller.aborted, { index, attempt },
    );
    if (sweepCompleted === false) {
      await endPlanSession(ctx);
      return;
    }
    await delayMs(Math.max(0, Number(spec.post_roll_ms) || 700));
    if (controller.aborted) return;
    const samples = await recorder.stop({ timeoutMs: 5000 });
    // The recording window is over: stop watching before the upload leg, whose
    // own focus losses say nothing about the samples already in hand.
    const integrity = summarizeCaptureIntegrity({
      watch: integrityWatch,
      stats: typeof recorder.captureStats === "function" ? recorder.captureStats() : null,
      // What this page will actually encode, counted here rather than at the
      // encoder below so the report describes the SAME array `stop()` returned
      // (#2094). Nothing between here and `float32ToWavBlob` touches it.
      encodedFrames: samples ? samples.length : null,
      // The array itself, scanned for the render-quantum of digital silence a
      // browser capture-FIFO resync leaves behind (#2557). Same array, same
      // moment: the scan describes exactly the bytes this page is about to
      // upload, not a proxy for them.
      samples: samples,
      // …and, when this round is one the PAGE started off the back of that
      // scan, the disclosure that says so (phase B). It rides the retained
      // operator sidecar with the rest of the report, so a session reviewed
      // later shows the automatic try as an automatic try rather than as one
      // more attempt nobody can account for.
      autoRetake: autoRetakeMarker(ctx, { index, attempt }),
    });
    if (integrityWatch) integrityWatch.dispose();
    integrityWatch = null;
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
    // Report this take's capture-side condition BEFORE the blob (#2151). The
    // ordering is the whole guarantee: the relay's phone-event slot is
    // last-write-wins and the Pi reads both from the same status document, so
    // posting first means any poll that sees the blob has already seen the
    // integrity report — no race, no extra handshake. It re-sends the ENTIRE
    // armed payload rather than the new field alone, because overwriting the
    // slot with a partial event would strand a Pi that had not yet polled
    // `armed: true`. Re-posting an identical armed event is already the
    // documented-safe no-op the connectivity retry above relies on.
    //
    // Best-effort by construction: a diagnostic must never cost the household
    // the capture it describes, so a failed post is swallowed and the upload
    // proceeds. The host then simply has no report for this take.
    try {
      await client.postEvent({ ...armedEvent, capture_integrity: integrity });
    } catch {
      /* diagnostics are never worth failing a good capture over */
    }
    if (controller.aborted) return;
    // Each admitted attempt's blob rides its OWN relay key
    // (capture_index = attempt - 1) so a retried slot never clobbers the
    // prior attempt's upload.
    await client.putBlob(blob, plaintextLen, sha256, attempt - 1);
    if (controller.aborted) return;

    setStatus(CAPTURE_CHECKING_MESSAGE, "info");
    const verdict = await waitForCaptureResult(
      client, spec, index, attempt, target, () => controller.aborted,
    );
    if (controller.aborted || verdict.aborted) return;
    if (verdict.deadSession) {
      renderSessionExpired(ctx);
      await endPlanSession(ctx);
      return;
    }
    if (verdict.terminal) {
      renderPlanTerminal(ctx, verdict);
      await endPlanSession(ctx);
      return;
    }
    // ORDER IS LOAD-BEARING (two-stage work order D1). `awaitingConfirm` must
    // beat the completion test, not follow it: in a measure-only plan the
    // final cloud position IS `target`, so `verdict.accepted && index >=
    // target` is true on the very capture whose verdict says the group is
    // still open. Testing completion first would render "All measurements
    // done" over the confirm screen the whole apply decision rests on. (The Pi
    // also withholds `capture_set_complete` while it holds the set, so
    // `setComplete` cannot be true here either — two independent guards,
    // because this one is the page's own and survives an older Pi.)
    if (verdict.accepted && verdict.awaitingConfirm) {
      // The just-accepted capture becomes retakeable — until this page posts
      // its next begin (§2.6) or signals the set complete.
      if (!verdict.unresolved) {
        armRetakeSlot(ctx, { index, attempt, attempts: verdict.attempts });
      }
      ctx.retakeAwaitingConfirm = true;
      // The pre-apply cloud is walked but NOT closed: the Pi is holding the
      // set open for the household's confirmation, which is exactly the
      // window in which retaking this spot still means something.
      renderPlanGroupConfirm(ctx, {
        index, attempt, target, unresolved: verdict.unresolved,
      });
      return;
    }
    if (verdict.setComplete || (verdict.accepted && index >= target)) {
      renderPlanAllDone(ctx, { index, unresolved: verdict.unresolved });
      await endPlanSession(ctx);
      return;
    }
    if (verdict.setExhausted) {
      renderPlanExhausted(ctx, { ...verdict, target, index });
      await endPlanSession(ctx);
      return;
    }
    if (verdict.accepted) {
      // The just-accepted capture becomes retakeable — until this page posts
      // its next begin (§2.6).
      if (!verdict.unresolved) {
        armRetakeSlot(ctx, { index, attempt, attempts: verdict.attempts });
      }
      // …and remember WHICH screen this acceptance belongs on, so a rejected
      // retake of it can return to the same place (B1's escape) rather than
      // guessing. Survives the retake round; overwritten by the next accept.
      ctx.retakeAwaitingConfirm = false;
      // Route by the UPCOMING entry's auto-advance policy (§5.2): a hold that
      // owns the screen (on_apply), a cancelable countdown, or the manual tap.
      // `unresolved` rides along so the next screen SAYS the spot was left out
      // instead of letting the household read the advance as a tick.
      advanceAfterAccepted(ctx, {
        index, attempt, target, unresolved: verdict.unresolved,
      });
    } else {
      // The page's own witness first (#2557 phase B): a take whose pre-upload
      // scan found the render-quantum splice retakes itself, once, inside the
      // budget this screen would otherwise ask the household to spend by hand.
      // Every rejection it declines falls straight through to the screen below,
      // unchanged.
      if (
        autoRetakeWitnessedSplice(ctx, {
          index,
          attempt,
          retake,
          prompt: verdict.prompt,
          attempts: verdict.attempts,
          integrity,
        })
      ) {
        return;
      }
      // `error` first for the legacy shape (kinds whose host sets it), then
      // the v2 conductor's `reason`/`banner`; `prompt` is the separate
      // what-to-do instruction renderPlanRetry headlines.
      renderPlanRetry(ctx, {
        index,
        attempt,
        target,
        reason: verdict.error || verdict.reason || verdict.banner,
        prompt: verdict.prompt,
        retake,
        attempts: verdict.attempts,
        // THIS take's own report (#2151), passed rather than stashed on ctx:
        // the value is a local of the round being rejected, so there is no
        // cross-take staleness question to reason about at all.
        integrity,
      });
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
        //
        // A relay-connectivity abort no longer ARRIVES here in the ordinary
        // case: both post-arm waits absorb one (waitForSweepComplete /
        // waitForCaptureResult) and the armed post itself is re-sent by
        // withRelayReconnect, so reaching this line means the relay stayed
        // unreachable across the whole retry budget. It is still terminal —
        // but renderSweepFailed classifies the copy (sweepFailureMessage), so
        // the household reads a connectivity sentence rather than the raw
        // text of a 3 s fetch budget expiring (#1824 D2).
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
        setStatus(
          captureFailureMessage(
            err, repairPreArmAffordance(ctx, { index, attempt, target, retake }),
          ),
          "error",
        );
      }
    }
  } finally {
    disposeWatch();
    // Every escape from the recording window, including the failure paths that
    // never reach the post-stop dispose above.
    if (integrityWatch) integrityWatch.dispose();
  }
}

// Entry point wired to the spec's own `begin_capture` button (rendered by
// the standard DATA renderer, same as the single-capture `onStart`) for a
// spec carrying a `capture_plan`. Captures the operator's
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
  const wakeLock = await acquireWakeLockWithHint(ctx.spec);
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
          ? "Measurement stopped — this screen must stay on. Tap Start to try again."
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
        `This device can't run a clean measurement (${decision.reason}). ` +
          "Try a different device, or use a calibrated USB mic on the speaker.",
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
    // Signing is unconditional: with one protocol there is no version that
    // exempts a phone event from its authenticated envelope. This used to be
    // gated on the spec's protocol being at least two, which a protocol-1
    // spec — including a version-less one, via the deleted legacy mapping —
    // could turn off entirely.
    client.setTransportIntegrity(verified.integrity, { required: true });
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
          text: "This page no longer has the setup identity from the safe level check. Return to the speaker and run that check again before measuring.",
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
    renderRoomReady(screenEl, ctx);
  } else if (spec.kind === "room_sweep" || spec.kind === "level_ramp") {
    renderIntro(screenEl, ctx);
  } else {
    // Session-spanning capture plans (SPEC W2.3). `capture_plan` presence is
    // the ONLY signal — the protocol version says nothing about plan-ness
    // (there is exactly one protocol; see jasper/capture_relay/spec.py).
    const isPlanSpec = Boolean(spec.capture_plan);
    // W6.12: this branch (crossover_sweep, with or without a plan) has no
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
    // §2.3: the consent screen IS the plan announcement — say how many
    // measurements and how long BEFORE the first tone, above the placement
    // instruction the spec already renders.
    insertPlanAnnouncement(screenEl, spec);
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
  setText(auto, "Automatic (recommended)");
  select.appendChild(auto);
  for (const d of inputs) {
    const opt = document.createElement("option");
    opt.value = d.deviceId;
    setText(opt, d.label); // browser-provided text uses the shared inert-text boundary
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

// Collapse the picker once the session's mic stream exists (§2.3). A v3 plan
// session opens ONE stream and reuses it for every capture (#1658), so from
// that moment the picker cannot change anything — leaving it above the
// instruction is a live-looking control that silently does nothing. Hidden
// rather than removed, so boot()'s re-boot removal still finds it.
function collapseMicPicker() {
  if (micPickerEl) micPickerEl.hidden = true;
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
  renderRoomReady,
  renderPositionCount,
  entryForIndex,
  renderPlanDeferred,
  advanceDeferredHoldHeading,
  planAnnouncementText,
  planEstimatedMinutes,
  beginCapturePayload,
  // The honesty layer's pure derivations (work order D8/D9). Exported for the
  // same reason `entryForIndex` and `beginCapturePayload` are: each turns
  // spec/verdict DATA into one sentence or one decision, and a harness that
  // exercises them directly pins the absence cases — a spec with no budget, a
  // verdict naming no clock — that a full-loop test only reaches by accident.
  planTimeBudget,
  timeBudgetLine,
  entryNoiseNote,
  expiredBudgetCopy,
  // Same layer, same reason: the absence case — a spec from a Pi that
  // publishes no wait — is the one a full-loop test reaches only by accident,
  // and is exactly the case an old speaker depends on.
  resultWaitMs,
  // The link-deadline pair (issue #2083) belongs to the same layer: one
  // records the only evidence the page has about the relay's own clock, the
  // other turns it into the single decision the dead-session screen makes.
  // Exported together because the absence case — never polled — is the one a
  // full-loop test reaches only by accident, and is exactly the case that must
  // keep today's copy.
  notePhoneStatus,
  linkDeadlinePassed,
};
