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
  ZERO_RUN_QUANTUM,
  ZERO_RUN_RECORD_CAP,
  createIntegrityWatch,
  scanZeroFillRuns,
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

// #2094: the page's half of the end-to-end frame ledger. `frames` is what the
// worklet assembled, `encoded_frames` is what survived the transfer to this
// page and is about to be encoded; the host closes the chain against its own
// count of the decoded capture.
function testSummaryCarriesTheFrameLedgersPageEnd() {
  const summary = summarizeCaptureIntegrity({
    stats: {
      frames: 480000, blocks: 3750, block_gaps: 0,
      block_gap_frames: 0, silent_blocks: 0,
    },
    encodedFrames: 480000,
  });
  assert.equal(summary.frames, 480000);
  assert.equal(summary.encoded_frames, 480000);
  ok();
}

// The two ends of the transfer are reported SEPARATELY on purpose: a ledger
// whose left and right edge came from the same measurement checks nothing, so
// a page that loses samples between the worklet and the encoder must be able
// to say two different numbers.
function testTheTwoTransferEndsAreIndependent() {
  const summary = summarizeCaptureIntegrity({
    stats: { frames: 480000, blocks: 3750, block_gaps: 0, block_gap_frames: 0 },
    encodedFrames: 479872,
  });
  assert.equal(summary.frames, 480000);
  assert.equal(summary.encoded_frames, 479872);
  ok();
}

// Same fail-soft rule as every other counter: not measured is an ABSENT key.
// An older recorder reports no `frames`, and a caller that passes no
// `encodedFrames` must not have a zero invented for it — the host reads a
// missing count as "not reported" and a zero as "this capture was empty".
function testFrameCountsAreOmittedWhenNotMeasured() {
  const oldRecorder = summarizeCaptureIntegrity({
    stats: { blocks: 3750, block_gaps: 0, block_gap_frames: 0, silent_blocks: 0 },
  });
  assert.equal("frames" in oldRecorder, false);
  assert.equal("encoded_frames" in oldRecorder, false);

  for (const bad of [null, undefined, NaN, "480000", Infinity]) {
    const summary = summarizeCaptureIntegrity({ stats: null, encodedFrames: bad });
    assert.equal("encoded_frames" in summary, false);
  }
  ok();
}

// ---------------------------------------------------------------------------
// The zero-run witness for hop A (issue #2557, verdict 2026-08-15).
//
// A browser capture-FIFO resync hands the audio graph one silent render
// quantum. The counters above cannot see it — the worklet gets an ordinary
// input array that happens to be full of zeros — so the evidence is the DATA:
// 128 consecutive exact zeros beginning on the render grid. These tests pin the
// two halves of that claim separately, because it is the CONJUNCTION that is
// diagnostic: a shorter run is not the fingerprint at all, and a run off the
// grid is a different finding rather than a weaker one.

// A capture that looks like a real room: never silent, deterministic so the
// suite cannot flake, and seeded to hit exact zero at roughly the 0.5 % rate
// measured in the campaign's ambient data.
function ambientCapture(length, { zeroRate = 0.005, seed = 1234567 } = {}) {
  const out = new Float32Array(length);
  let state = seed;
  for (let i = 0; i < length; i += 1) {
    // A plain LCG: the point is a fixed sequence, not statistical quality.
    state = (state * 1103515245 + 12345) % 2147483648;
    const unit = state / 2147483648;
    out[i] = unit < zeroRate ? 0 : (unit - 0.5) * 0.02 || 0.001;
  }
  return out;
}

function silence(buffer, offset, length) {
  for (let i = offset; i < offset + length; i += 1) buffer[i] = 0;
  return buffer;
}

// THE QUESTION A CONSUMER MUST ASK of a reported run, derived from the record
// alone: does it cover a whole block-aligned quantum? Not `phase === 0` — one
// natural ambient zero beside a zero-filled quantum merges with it and shifts
// the phase off the grid while the quantum is still in there.
function containsAlignedQuantum(run, quantum) {
  const firstAligned = Math.ceil(run.offset / quantum) * quantum;
  return firstAligned + quantum <= run.offset + run.len;
}

// THE FINGERPRINT. One whole quantum of digital silence, block-aligned, in
// otherwise live signal — the shape found in 13 of 13 testable glitch events.
function testAnAlignedQuantumOfSilenceIsFlagged() {
  const capture = ambientCapture(64 * ZERO_RUN_QUANTUM);
  const offset = 20 * ZERO_RUN_QUANTUM;
  silence(capture, offset, ZERO_RUN_QUANTUM);

  const scan = scanZeroFillRuns(capture);
  assert.equal(scan.count, 1);
  assert.equal(scan.quantum, ZERO_RUN_QUANTUM);
  assert.deepEqual(scan.runs, [
    { offset: offset, len: ZERO_RUN_QUANTUM, phase: 0 },
  ]);
  ok();
}

// A run that ends at the last sample must still be closed. The state machine
// only emits a run when it sees a non-zero sample after it, so without the
// end-of-buffer flush a capture whose tail died would report nothing — the one
// failure the household is most likely to hit.
function testARunAtTheEndOfTheCaptureIsClosed() {
  const capture = ambientCapture(64 * ZERO_RUN_QUANTUM);
  const offset = capture.length - ZERO_RUN_QUANTUM;
  silence(capture, offset, ZERO_RUN_QUANTUM);

  const scan = scanZeroFillRuns(capture);
  assert.equal(scan.count, 1);
  assert.equal(scan.runs[0].offset, offset);
  assert.equal(scan.runs[0].len, ZERO_RUN_QUANTUM);
  ok();
}

// ONE SAMPLE SHORT IS NOT THE FINGERPRINT. 127 zeros is not a render quantum,
// and a detector that accepted it would be claiming a mechanism its own
// evidence does not support.
function testAShortRunIsNotTheFingerprint() {
  const capture = ambientCapture(64 * ZERO_RUN_QUANTUM);
  silence(capture, 20 * ZERO_RUN_QUANTUM, ZERO_RUN_QUANTUM - 1);

  const scan = scanZeroFillRuns(capture);
  assert.equal(scan.count, 0);
  assert.deepEqual(scan.runs, []);
  ok();
}

// OFF THE GRID IS A DIFFERENT FINDING. 128 zeros that straddle two render
// boundaries are reported — they are real silence and worth seeing — but they
// cover no whole aligned quantum, and it is that CONTAINMENT test, not the
// phase, that says so.
function testAMisalignedRunIsReportedButNotAsAQuantum() {
  const capture = ambientCapture(64 * ZERO_RUN_QUANTUM);
  const offset = 20 * ZERO_RUN_QUANTUM + 37;
  silence(capture, offset, ZERO_RUN_QUANTUM);

  const scan = scanZeroFillRuns(capture);
  assert.equal(scan.count, 1);
  assert.equal(scan.runs[0].offset, offset);
  assert.equal(scan.runs[0].phase, 37);
  assert.equal(containsAlignedQuantum(scan.runs[0], scan.quantum), false);
  ok();
}

// THE OVERCLAIM THIS INOCULATES AGAINST. A zero-filled quantum that happens to
// abut ONE natural ambient zero merges into a single 129-sample run at phase
// 127 — the quantum arrived whole, and a consumer testing `phase === 0` would
// throw the event away. At the measured ambient zero density (P ≈ 0.005 per
// sample, either neighbour) that is roughly 1 in 100 events, which is why all 13
// events of the 2026-08-15 campaign reading phase 0 is a coincidence of the
// sample and not a rule to build on.
function testAQuantumMergedWithOneAmbientZeroIsStillAWholeQuantum() {
  const capture = ambientCapture(64 * ZERO_RUN_QUANTUM);
  const aligned = 20 * ZERO_RUN_QUANTUM;
  // The fixture must isolate the ONE prepended zero, or this is measuring a
  // longer accidental run instead of the merge it means to.
  assert.notEqual(capture[aligned - 2], 0);
  silence(capture, aligned, ZERO_RUN_QUANTUM);
  capture[aligned - 1] = 0;

  const scan = scanZeroFillRuns(capture);
  assert.equal(scan.count, 1);
  assert.deepEqual(scan.runs, [
    { offset: aligned - 1, len: ZERO_RUN_QUANTUM + 1, phase: ZERO_RUN_QUANTUM - 1 },
  ]);
  // The record still carries the whole aligned quantum, by offset/len alone...
  assert.equal(containsAlignedQuantum(scan.runs[0], scan.quantum), true);
  // ...and the phase test a reader might reach for first would have missed it.
  assert.notEqual(scan.runs[0].phase, 0);
  ok();
}

// THE FALSE-POSITIVE CONTROL. Real rooms produce exact zeros constantly — about
// one sample in 200 — and 0 of 3 clean campaign controls contained a run this
// long; their longest was 13 samples. An epsilon tolerance is what would break
// this test, which is exactly why the scan compares against exact zero.
//
// The control PROVES ITS OWN INSTRUMENT first: a fixture that happened to
// contain no exact zeros would pass this test while checking nothing, so the
// zero density is asserted before the verdict is.
function testAmbientExactZerosDoNotTripIt() {
  const capture = ambientCapture(400_000);
  let zeros = 0;
  for (let i = 0; i < capture.length; i += 1) if (capture[i] === 0) zeros += 1;
  assert.ok(zeros > 1500, `fixture is not room-like: ${zeros} exact zeros`);

  // Plus the worst clean run the campaign actually measured, planted on the
  // render grid where it would be most tempting to call it a quantum.
  silence(capture, 100 * ZERO_RUN_QUANTUM, 13);

  // Plus a QUIET PASSAGE: four whole quanta of real but very small signal, the
  // hush between two notes in a genuinely quiet room. This is the sample the
  // scan must keep calling audio, and it is the reason the comparison is
  // against exact zero — any tolerance wide enough to swallow this level turns
  // a quiet room into a rejected measurement.
  for (let i = 0; i < 4 * ZERO_RUN_QUANTUM; i += 1) {
    capture[200 * ZERO_RUN_QUANTUM + i] = i % 2 === 0 ? 3e-6 : -3e-6;
  }

  const scan = scanZeroFillRuns(capture);
  assert.equal(scan.count, 0);
  ok();
}

// Two adjacent lost quanta are ONE run of 256, and `len / quantum` recovers the
// block count — which is why no second counter lives in the worklet.
function testAdjacentQuantaMergeIntoOneRunWhoseLengthSaysHowMany() {
  const capture = ambientCapture(64 * ZERO_RUN_QUANTUM);
  const offset = 8 * ZERO_RUN_QUANTUM;
  silence(capture, offset, 2 * ZERO_RUN_QUANTUM);

  const scan = scanZeroFillRuns(capture);
  assert.equal(scan.count, 1);
  assert.equal(scan.runs[0].len / scan.quantum, 2);
  assert.equal(scan.runs[0].phase, 0);
  ok();
}

// Same bounded-log / exact-count contract the focus events keep: a capture that
// is mostly silence cannot grow the relay event, and the FIRST runs are the
// ones attribution needs.
function testTheRunListIsBoundedButTheCountIsNot() {
  const runs = ZERO_RUN_RECORD_CAP + 5;
  const capture = ambientCapture((2 * runs + 4) * ZERO_RUN_QUANTUM);
  for (let i = 0; i < runs; i += 1) {
    silence(capture, (2 * i + 1) * ZERO_RUN_QUANTUM, ZERO_RUN_QUANTUM);
  }

  const scan = scanZeroFillRuns(capture);
  assert.equal(scan.count, runs);
  assert.equal(scan.runs.length, ZERO_RUN_RECORD_CAP);
  assert.equal(scan.runs[0].offset, ZERO_RUN_QUANTUM);
  ok();
}

// Same degrade-never-throw rule as the watch: this module is imported by a page
// that also runs under harnesses with no capture in hand.
function testANonBufferDegradesRatherThanThrowing() {
  for (const bad of [null, undefined, {}, "", 0, { length: NaN }]) {
    assert.equal(scanZeroFillRuns(bad), null);
  }
  assert.deepEqual(scanZeroFillRuns(new Float32Array(0)), {
    count: 0, runs: [], quantum: ZERO_RUN_QUANTUM,
  });
  ok();
}

// The wire half: a scanned capture says so even when it found nothing, and an
// unscanned one omits the keys rather than claiming a clean take.
function testSummaryCarriesTheZeroRunWitness() {
  const capture = ambientCapture(64 * ZERO_RUN_QUANTUM);
  silence(capture, 20 * ZERO_RUN_QUANTUM, ZERO_RUN_QUANTUM);

  const flagged = summarizeCaptureIntegrity({ samples: capture });
  assert.equal(flagged.zero_run_count, 1);
  assert.equal(flagged.zero_run_quantum, ZERO_RUN_QUANTUM);
  assert.equal(flagged.zero_runs[0].phase, 0);
  assert.equal(flagged.zero_runs[0].len, ZERO_RUN_QUANTUM);

  const clean = summarizeCaptureIntegrity({
    samples: ambientCapture(64 * ZERO_RUN_QUANTUM),
  });
  assert.equal(clean.zero_run_count, 0);
  assert.deepEqual(clean.zero_runs, []);

  const unscanned = summarizeCaptureIntegrity({
    stats: { frames: 8192, blocks: 64, block_gaps: 0, block_gap_frames: 0 },
    encodedFrames: 8192,
  });
  assert.equal("zero_run_count" in unscanned, false);
  assert.equal("zero_runs" in unscanned, false);
  assert.equal("zero_run_quantum" in unscanned, false);
  ok();
}

// The counter this scan exists to compensate for: a zero-FILLED input array is
// an ordinary block to the worklet, so `silent_blocks` stays 0 through the very
// event the capture is being refused for. Both facts ride the same report.
function testSilentBlocksStaysZeroThroughAZeroFilledQuantum() {
  const capture = ambientCapture(64 * ZERO_RUN_QUANTUM);
  silence(capture, 20 * ZERO_RUN_QUANTUM, ZERO_RUN_QUANTUM);

  const summary = summarizeCaptureIntegrity({
    stats: {
      frames: capture.length, blocks: 64,
      block_gaps: 0, block_gap_frames: 0, silent_blocks: 0,
    },
    encodedFrames: capture.length,
    samples: capture,
  });
  assert.equal(summary.silent_blocks, 0);
  assert.equal(summary.block_gaps, 0);
  assert.equal(summary.zero_run_count, 1);
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
    testSummaryCarriesTheFrameLedgersPageEnd,
    testTheTwoTransferEndsAreIndependent,
    testFrameCountsAreOmittedWhenNotMeasured,
    testAnAlignedQuantumOfSilenceIsFlagged,
    testARunAtTheEndOfTheCaptureIsClosed,
    testAShortRunIsNotTheFingerprint,
    testAMisalignedRunIsReportedButNotAsAQuantum,
    testAQuantumMergedWithOneAmbientZeroIsStillAWholeQuantum,
    testAmbientExactZerosDoNotTripIt,
    testAdjacentQuantaMergeIntoOneRunWhoseLengthSaysHowMany,
    testTheRunListIsBoundedButTheCountIsNot,
    testANonBufferDegradesRatherThanThrowing,
    testSummaryCarriesTheZeroRunWitness,
    testSilentBlocksStaysZeroThroughAZeroFilledQuantum,
    testCopyIsPlainAndProviderAgnostic,
  ],
  () => passed,
);
