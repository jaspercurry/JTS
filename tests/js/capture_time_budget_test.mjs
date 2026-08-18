// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Behavioral harness for the capture page's HONESTY LAYER — the derivations
// that turn spec/verdict data into the sentences a household reads about time
// (two-stage commission work order D8/D9, issues #1807 / #1835 / #1840).
//
// Why these are worth a harness of their own rather than a line in the plan
// loop: every one of them has an ABSENCE case that is the whole point. A spec
// with no published budget must say nothing about time (that is what makes a
// new page safe against an older Pi); a session-over event that names no clock
// must keep the attempt-limit copy (that is what keeps the runner's genuine
// exhaustion honest); an entry with no `noise_note` must keep the page's own
// default (that is what keeps the two OTHER ambient windows untouched). A
// full-loop test reaches the present cases and only stumbles into the absent
// ones; this file asserts both halves of each.
//
// Loads main.js the same way the other capture-page harnesses do — strip the
// imports, inject the browser globals it reaches for, import as a data: URL.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { runTestFunctions } from "./run_test_functions.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const raw = readFileSync(resolve(here, "../../capture-page/js/main.js"), "utf8");
const withoutImports = raw
  .replace(/^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']+["'];\s*/gm, "")
  .replace(/^import\s+[^;\n]+\s+from\s+["'][^"']+["'];\s*/gm, "")
  .replace(
    /^const PAGE_VERSION_URL = .*;$/m,
    'const PAGE_VERSION_URL = new URL("https://capture.test/version.json");',
  );
if (/^import\s/m.test(withoutImports)) {
  throw new Error("unhandled import in main.js — update the harness strip rule");
}

// The names main.js's stripped imports would have provided. Only the ones its
// module-level code touches need real values; the honesty-layer derivations
// touch none of them.
const injected = `
const renderScreen = () => ({ buttons: [], levelMeters: [], acknowledgement: null });
const acceptedAcknowledgement = () => null;
const setText = (node, text) => { node.textContent = typeof text === "string" ? text : ""; };
const RELAY_BASE = "https://relay.test";
class RelayClient {}
const parseFragment = () => ({});
const verifySpecMac = async () => true;
const signPhoneEvent = async (event) => event;
const isSupportedCaptureProtocol = () => true;
const createMonoRecorder = async () => ({});
const rmsToDbfs = () => -60;
const float32ToWavBlob = () => ({});
const verifyRealizedConstraints = () => ({});
const constraintDecision = () => ({ action: "accept" });
const requestWakeLock = async () => null;
const releaseWakeLock = () => {};
const watchVisibilityAbort = () => () => {};
const buildAmbientStatsEvent = () => ({});
const createLevelEventBatcher = () => ({});
const loadStoredSetup = () => null;
const storeSetup = () => {};
const clearStoredSetup = () => {};
const safeReturnUrl = () => "";
const CALIBRATION_MODELS = [];
const importContentKey = async () => ({});
const encryptWav = async () => ({});
const withinUploadCap = () => true;
`;

async function loadModule() {
  const dataUrl =
    "data:text/javascript;base64," +
    Buffer.from(injected + withoutImports, "utf8").toString("base64");
  return import(dataUrl);
}

const mod = await loadModule();

function specWith(extra = {}) {
  return {
    kind: "crossover_sweep",
    capture_plan: { schema_version: 1, capture_target: 3, max_attempts: 4 },
    ...extra,
  };
}

let passed = 0;
function ok() {
  passed += 1;
}

// ============================================================================
// 1. The budget the Pi published is the budget the page quotes — and a spec
//    that published none makes the page silent, not inventive.
//
//    THE ABSENCE CASE IS THE POINT. The page must never become one more copy
//    of the 900. Publishing the budget collapsed the two PYTHON copies onto
//    `session.DEFAULT_TTL_S` (pinned by
//    test_the_adapters_ttl_default_is_the_sessions_own_constant, which
//    asserts the two Python copies collapsed, not that the duplication is
//    gone entirely) — relay/src/worker.js keeps its own literal deliberately,
//    as a separate release that bounds whatever a Pi asks for. Silence when
//    the Pi said nothing is what keeps the page out of that set.
// ============================================================================
function testTheBudgetLineOnlyEverQuotesWhatThePiPublished() {
  assert.equal(mod.planTimeBudget(specWith()), null);
  assert.equal(mod.timeBudgetLine(specWith()), "");

  // A malformed or zero budget is treated as absent, not as "0 minutes".
  for (const bad of [
    { step_s: 0, session_s: 900 },
    { step_s: 120, session_s: 0 },
    { step_s: "two", session_s: 900 },
    {},
  ]) {
    assert.equal(
      mod.planTimeBudget(specWith({ time_budget: bad })), null,
      `a malformed budget must read as absent: ${JSON.stringify(bad)}`,
    );
  }

  const spec = specWith({ time_budget: { step_s: 120, session_s: 900 } });
  assert.deepEqual(mod.planTimeBudget(spec), { stepS: 120, sessionS: 900 });
  const line = mod.timeBudgetLine(spec);
  // Both clocks, in whole minutes, and no number the Pi did not send.
  assert.match(line, /about 2 minutes between taps/);
  assert.match(line, /lasts about 15 minutes/);

  // The numbers TRACK the spec — this is a derivation, not a fixed sentence.
  const longer = specWith({ time_budget: { step_s: 300, session_s: 1800 } });
  assert.match(mod.timeBudgetLine(longer), /about 5 minutes between taps/);
  assert.match(mod.timeBudgetLine(longer), /lasts about 30 minutes/);
  ok();
}

// ============================================================================
// 2. An expiry names WHICH clock ran out (D8) — and only when the Pi said.
//
//    Before this, both a step timeout and a dead link surfaced as "the speaker
//    reached its measurement attempt limit", which is a third thing entirely:
//    no attempt limit was reached. The runner's GENUINE exhaustion still gets
//    that copy, which is why the absent case has to keep working.
// ============================================================================
function testAnExpiryNamesTheClockAndOnlyWhenTheSpeakerSaid() {
  const spec = specWith({ time_budget: { step_s: 120, session_s: 900 } });
  const ctx = { spec };

  // No budget named -> null, so renderPlanExhausted keeps the attempt-limit
  // copy the runner's own exhaustion event genuinely means.
  assert.equal(mod.expiredBudgetCopy(ctx, { budget: "", index: 4, target: 10 }), null);
  assert.equal(mod.expiredBudgetCopy(ctx, {}), null);
  // An unknown budget name is treated as unnamed rather than rendered raw.
  assert.equal(mod.expiredBudgetCopy(ctx, { budget: "wall_clock" }), null);

  const step = mod.expiredBudgetCopy(ctx, { budget: "step", index: 4, target: 10 });
  assert.match(step.heading, /step/i);
  assert.match(step.body, /about 2 minutes between taps/);
  // What the expiry preserved, honestly: progress reached, NOT a resumable set.
  assert.match(step.body, /measurement 4 of 10/);
  assert.match(step.body, /whole set in one session/);

  const link = mod.expiredBudgetCopy(ctx, { budget: "link", index: 9, target: 10 });
  assert.match(link.heading, /link/i);
  assert.match(link.body, /lasts about 15 minutes/);
  assert.match(link.body, /measurement 9 of 10/);

  // …and each names ITS OWN clock, never the other's — advice about the wrong
  // budget sends a household back to fail the same way.
  assert.ok(!step.body.includes("15 minutes"), "the step expiry must not quote the link's life");
  assert.ok(
    !link.body.includes("between taps"),
    "the link expiry must not quote the step window",
  );

  // With no published budget the page still NAMES the clock — that is the part
  // the household needs — but quotes no duration, because it would be
  // inventing one. Same rule as everywhere else in this layer.
  for (const [named, pattern] of [["step", /step/i], ["link", /link/i]]) {
    const bare = mod.expiredBudgetCopy({ spec: specWith() }, { budget: named });
    assert.match(bare.heading, pattern);
    assert.ok(!/\d/.test(bare.body), `no invented duration: ${bare.body}`);
    // …and with no progress to report it simply does not report progress.
    assert.ok(!bare.body.includes("You got to measurement"), bare.body);
  }
  ok();
}

// ============================================================================
// 3. CHECK's pre-arm floor window gets its own request; every other entry
//    keeps the page's default (issue #1835, and the work order's "two ambient
//    windows, two purposes" trap — this changes ONE of them).
// ============================================================================
function testOnlyAnEntryThatSuppliesOneOverridesTheFloorWindowCopy() {
  const entries = [
    {
      index: 0,
      kind_label: "check",
      duration_ms: 23000,
      screen: { noise_note: "Listening to the room as it normally is." },
    },
    { index: 1, kind_label: "measure", duration_ms: 40000, screen: { title: "Hold still" } },
    { index: 2, kind_label: "cloud_measure", duration_ms: 17000 },
  ];
  const spec = specWith({
    capture_plan: {
      schema_version: 2, capture_target: 3, max_attempts: 6, entries,
    },
  });

  // 1-based wire index -> 0-based entry.
  assert.equal(mod.entryNoiseNote(spec, 1), "Listening to the room as it normally is.");
  assert.equal(mod.entryNoiseNote(spec, 2), "");
  assert.equal(mod.entryNoiseNote(spec, 3), "");
  // A plan with no entries at all (the legacy "N repeats of one spec" shape)
  // keeps the default too.
  assert.equal(mod.entryNoiseNote(specWith(), 1), "");
  ok();
}

// ============================================================================
// 4. The THIRD outcome: no clock ran out at all (issue #2083).
//
//    A session killed by the transport ran out of neither budget. The Pi used
//    to say so by omitting the field, which lands on the attempt-limit copy —
//    a claim about a third thing that also did not happen. An explicit "none"
//    gets its own honest sentence, and — the part that keeps this safe — an
//    unknown name still falls back, so an older Pi and a newer page agree.
// ============================================================================
function testATransportDeathNamesNoClockAndSaysSoHonestly() {
  const spec = specWith({ time_budget: { step_s: 120, session_s: 900 } });
  const ctx = { spec };

  const none = mod.expiredBudgetCopy(ctx, { budget: "none", index: 2, target: 10 });
  assert.ok(none, "an explicit 'none' must render its own copy, not fall back");
  assert.match(none.heading, /lost its connection/i);
  assert.match(none.body, /lost its connection to the measurement service/i);
  // It reports progress and the restart, exactly like its two siblings…
  assert.match(none.body, /measurement 2 of 10/);
  assert.match(none.body, /whole set in one session/);
  // …but it must NOT blame a clock, in either direction, or quote a duration:
  // nothing about time went wrong here.
  assert.ok(!none.body.includes("between taps"), none.body);
  assert.ok(!none.body.includes("15 minutes"), none.body);
  assert.ok(!/attempt limit/i.test(none.body), none.body);

  // The compatibility floor this rests on: a name the page does not know is
  // still treated as unnamed, so a future Pi vocabulary cannot render raw.
  assert.equal(mod.expiredBudgetCopy(ctx, { budget: "nonesuch" }), null);
  ok();
}

// ============================================================================
// 5. The page must not claim a lapse it cannot observe (issue #2083).
//
//    A dead-session 401/403/404 has two causes that are identical on the wire:
//    the link's TTL ran out, or the speaker ended the session and purged it.
//    The relay's published `expires_at` is the ONLY evidence separating them,
//    and the page had been ignoring it while asserting the first cause — which
//    is how one transient stall produced a false "Link expired" in front of a
//    household whose link was still live.
//
//    THE ABSENCE CASE IS AGAIN THE POINT: never having polled is not evidence,
//    so it keeps the unchanged copy.
// ============================================================================
function testTheLinkDeadlineIsOnlyClaimedWhenItWasActuallyObservedToPass() {
  // Never observed -> the page keeps today's link-expired copy. (This is also
  // a relay that publishes no `expires_at` at all.)
  assert.equal(mod.linkDeadlinePassed(), true);

  // A deadline still in the future PROVES the link had not lapsed — the one
  // direction this evidence is used in.
  mod.notePhoneStatus({ expires_at: Date.now() + 10 * 60 * 1000 });
  assert.equal(mod.linkDeadlinePassed(), false);

  // Junk never overwrites a good deadline: a poll that omits the field, or
  // carries a malformed one, leaves the last real answer standing rather than
  // silently reverting the page to its guess.
  for (const bad of [{}, { expires_at: null }, { expires_at: "soon" }, { expires_at: 0 }]) {
    mod.notePhoneStatus(bad);
    assert.equal(mod.linkDeadlinePassed(), false, JSON.stringify(bad));
  }
  assert.equal(mod.linkDeadlinePassed(), false); // …and a null status too
  mod.notePhoneStatus(null);
  assert.equal(mod.linkDeadlinePassed(), false);

  // A deadline that has passed is a real lapse, and gets named as one.
  mod.notePhoneStatus({ expires_at: Date.now() - 1000 });
  assert.equal(mod.linkDeadlinePassed(), true);
  ok();
}

// ============================================================================
// The result wait belongs to the Pi, and the fallback is what makes the page
// safe against one that never learned to publish it (build 20260818.1).
//
// This is the tolerance the release order rests on. `capture-page/README.md`
// justifies shipping this page BEFORE the Pi on the grounds that a spec with no
// `result_wait_s` behaves exactly as before — so that branch is a requirement,
// not an implementation detail, and an old speaker is the thing it protects.
// ============================================================================
function testTheResultWaitIsThePisWhenItSentOneAndTheFallbackWhenItDidNot() {
  const FALLBACK_MS = 90000;

  // No field at all: an old Pi, and the page must not read NaN off it.
  assert.equal(mod.resultWaitMs(specWith()), FALLBACK_MS);
  assert.equal(mod.resultWaitMs(null), FALLBACK_MS);
  assert.equal(mod.resultWaitMs(undefined), FALLBACK_MS);

  // Malformed or non-positive reads as absent, never as "wait no time".
  for (const bad of [0, -1, "soon", null, {}, NaN]) {
    assert.equal(
      mod.resultWaitMs(specWith({ result_wait_s: bad })), FALLBACK_MS,
      `a malformed wait must fall back: ${JSON.stringify(bad)}`,
    );
  }

  // A published wait wins, and TRACKS the spec — a derivation, not a constant.
  assert.equal(mod.resultWaitMs(specWith({ result_wait_s: 109 })), 109000);
  assert.equal(mod.resultWaitMs(specWith({ result_wait_s: 45 })), 45000);
  // …including one SHORTER than the fallback. The Pi is the owner, so a future
  // Pi that genuinely speeds up must be able to say so; max()-ing the two here
  // would quietly make this page the owner again.
  assert.ok(mod.resultWaitMs(specWith({ result_wait_s: 45 })) < FALLBACK_MS);
  ok();
}

await runTestFunctions(
  [
    testTheBudgetLineOnlyEverQuotesWhatThePiPublished,
    testTheResultWaitIsThePisWhenItSentOneAndTheFallbackWhenItDidNot,
    testAnExpiryNamesTheClockAndOnlyWhenTheSpeakerSaid,
    testOnlyAnEntryThatSuppliesOneOverridesTheFloorWindowCopy,
    testATransportDeathNamesNoClockAndSaysSoHonestly,
    testTheLinkDeadlineIsOnlyClaimedWhenItWasActuallyObservedToPass,
  ],
  () => passed,
);
