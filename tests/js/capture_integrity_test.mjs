// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the capture page's per-take integrity accounting (issue #2151).
//
// The bug this guards: on a desktop browser, clicking out of the capture window
// does NOT fire `visibilitychange` — the tab stays `visible` — so wakelock.js's
// hide-abort never sees it and a spliced take uploaded silently. These tests pin
// that a plain window `blur` IS caught, that the household is told once (not per
// event), and that the wire summary omits what it has not measured rather than
// inventing zeros. Prints {"ok":true}.
//
//   node tests/js/capture_integrity_test.mjs

import assert from "node:assert/strict";

import {
  INTEGRITY_EVENT_CAP,
  INTEGRITY_LOST_FOCUS_MESSAGE,
  INTEGRITY_LOST_FOCUS_NOTE,
  createIntegrityWatch,
  summarizeCaptureIntegrity,
} from "../../capture-page/js/capture-integrity.js";
import { runTestFunctions } from "./run_test_functions.mjs";

let passed = 0;
function ok() {
  passed += 1;
}

// A minimal event-target stand-in that also counts removals, so a disposer that
// silently fails to unwire is a test failure rather than a leak nobody sees.
function fakeTarget(extra = {}) {
  const handlers = new Map();
  return {
    ...extra,
    handlers,
    addEventListener(kind, fn) {
      if (!handlers.has(kind)) handlers.set(kind, new Set());
      handlers.get(kind).add(fn);
    },
    removeEventListener(kind, fn) {
      if (handlers.has(kind)) handlers.get(kind).delete(fn);
    },
    fire(kind) {
      for (const fn of handlers.get(kind) || []) fn();
    },
    count(kind) {
      return (handlers.get(kind) || new Set()).size;
    },
  };
}

function watchOn(win, doc, onLoss) {
  let t = 0;
  return createIntegrityWatch({ win, doc, onLoss, now: () => (t += 10) });
}

// THE REGRESSION. A desktop window losing focus keeps visibilityState
// "visible", so only the blur listener can see it.
function testBlurWithoutHideIsCaught() {
  const win = fakeTarget();
  const doc = fakeTarget({ visibilityState: "visible" });
  const losses = [];
  const watch = watchOn(win, doc, (kind) => losses.push(kind));

  assert.equal(watch.lost, false);
  win.fire("blur");
  assert.equal(watch.lost, true);
  assert.deepEqual(losses, ["blur"]);
  assert.equal(watch.events()[0].kind, "blur");
  ok();
}

// The household hears about it ONCE. A window that blurs and refocuses
// repeatedly must not redraw the warning on every transition, but the count
// still has to stay exact for the report.
function testOnLossFiresOnceButCountingContinues() {
  const win = fakeTarget();
  const doc = fakeTarget({ visibilityState: "visible" });
  let calls = 0;
  const watch = watchOn(win, doc, () => (calls += 1));

  win.fire("blur");
  win.fire("focus");
  win.fire("blur");
  win.fire("blur");
  assert.equal(calls, 1);
  assert.equal(watch.losses, 3);
  ok();
}

// A visibilitychange to "visible" is a RECOVERY, not a loss — counting it would
// double-report every return from a brief hide.
function testVisibleTransitionIsNotALoss() {
  const win = fakeTarget();
  const doc = fakeTarget({ visibilityState: "visible" });
  const watch = watchOn(win, doc, () => {});

  doc.fire("visibilitychange");
  assert.equal(watch.lost, false);
  assert.equal(watch.events().at(-1).kind, "visible");

  doc.visibilityState = "hidden";
  doc.fire("visibilitychange");
  assert.equal(watch.lost, true);
  assert.equal(watch.events().at(-1).kind, "hidden");
  ok();
}

function testPageHideCounts() {
  const win = fakeTarget();
  const doc = fakeTarget({ visibilityState: "visible" });
  const watch = watchOn(win, doc, () => {});
  win.fire("pagehide");
  assert.equal(watch.lost, true);
  ok();
}

// The log is bounded (a focus-thrash cannot grow the relay event without
// limit) but the COUNT past the cap stays exact.
function testEventLogIsBoundedButCountIsNot() {
  const win = fakeTarget();
  const doc = fakeTarget({ visibilityState: "visible" });
  const watch = watchOn(win, doc, () => {});
  for (let i = 0; i < INTEGRITY_EVENT_CAP + 25; i += 1) win.fire("blur");
  assert.equal(watch.events().length, INTEGRITY_EVENT_CAP);
  assert.equal(watch.losses, INTEGRITY_EVENT_CAP + 25);
  ok();
}

function testDisposeUnwiresAndIsIdempotent() {
  const win = fakeTarget();
  const doc = fakeTarget({ visibilityState: "visible" });
  const watch = watchOn(win, doc, () => {});
  assert.equal(win.count("blur"), 1);
  assert.equal(doc.count("visibilitychange"), 1);

  watch.dispose();
  watch.dispose();
  assert.equal(win.count("blur"), 0);
  assert.equal(win.count("focus"), 0);
  assert.equal(win.count("pagehide"), 0);
  assert.equal(doc.count("visibilitychange"), 0);

  // Events after disposal are not recorded — a disposed watch is inert even if
  // some other reference keeps firing at it.
  const before = watch.losses;
  win.fire("blur");
  assert.equal(watch.losses, before);
  ok();
}

// Absent hosts must degrade, not throw: the module is imported by a page that
// also runs under test harnesses with no DOM.
function testMissingHostsDegrade() {
  const watch = createIntegrityWatch({ win: null, doc: null, now: () => 0 });
  assert.equal(watch.lost, false);
  assert.deepEqual(watch.events(), []);
  watch.dispose();
  ok();
}

// FAIL-SOFT WIRE CONTRACT: a missing counter is an ABSENT KEY, never a zero.
// A zero would read as "measured and clean", which is the one claim an older
// bundle cannot support.
function testSummaryOmitsWhatItDidNotMeasure() {
  assert.deepEqual(summarizeCaptureIntegrity({}), {});
  assert.deepEqual(summarizeCaptureIntegrity(), {});

  const statsOnly = summarizeCaptureIntegrity({
    stats: { blocks: 100, block_gaps: 1, block_gap_frames: 128, silent_blocks: 0 },
  });
  assert.equal("focus_lost" in statsOnly, false);
  assert.equal(statsOnly.block_gap_frames, 128);
  assert.equal(statsOnly.silent_blocks, 0);

  // Non-numeric / absent counters are dropped rather than passed through.
  const junk = summarizeCaptureIntegrity({
    stats: { blocks: "many", block_gaps: null, block_gap_frames: NaN },
  });
  assert.deepEqual(junk, {});
  ok();
}

function testSummaryCarriesFocusEvidence() {
  const win = fakeTarget();
  const doc = fakeTarget({ visibilityState: "visible" });
  const watch = watchOn(win, doc, () => {});
  win.fire("blur");

  const summary = summarizeCaptureIntegrity({
    watch,
    stats: { blocks: 940, block_gaps: 0, block_gap_frames: 0, silent_blocks: 0 },
  });
  assert.equal(summary.focus_lost, true);
  assert.equal(summary.focus_losses, 1);
  assert.equal(summary.focus_events[0].kind, "blur");
  assert.equal(typeof summary.focus_events[0].ms, "number");
  // The diagnostic asymmetry the issue is about: focus lost, render graph
  // continuous — which places the discontinuity upstream of the worklet.
  assert.equal(summary.block_gaps, 0);
  ok();
}

// The two household-facing strings name no browser, platform, or vendor, and
// say what will happen rather than only what went wrong.
function testCopyIsPlainAndProviderAgnostic() {
  for (const copy of [INTEGRITY_LOST_FOCUS_MESSAGE, INTEGRITY_LOST_FOCUS_NOTE]) {
    assert.equal(/chrome|safari|firefox|android|ios|mac|windows|tab\b/i.test(copy), false);
    assert.ok(copy.length > 30);
  }
  assert.ok(/retaken/.test(INTEGRITY_LOST_FOCUS_MESSAGE));
  ok();
}

await runTestFunctions(
  [
    testBlurWithoutHideIsCaught,
    testOnLossFiresOnceButCountingContinues,
    testVisibleTransitionIsNotALoss,
    testPageHideCounts,
    testEventLogIsBoundedButCountIsNot,
    testDisposeUnwiresAndIsIdempotent,
    testMissingHostsDegrade,
    testSummaryOmitsWhatItDidNotMeasure,
    testSummaryCarriesFocusEvidence,
    testCopyIsPlainAndProviderAgnostic,
  ],
  () => passed,
);
