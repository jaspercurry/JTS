// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// A hidden tab must not stop polling entirely — the phone-in-hand user may
// look away mid-measurement, but the wizard still needs to auto-advance when
// the phone finishes its side. schedulePoll() should slow to HIDDEN_POLL_MS
// while hidden instead of cancelling the timer, and the visibilitychange
// listener must re-apply (not discard) the caller's last requested cadence.

import assert from "node:assert/strict";
import { crossoverMainModule } from "./_dom.mjs";

const ids = [
  "crossover-verdict",
  "crossover-start-over",
  "crossover-steps",
  "crossover-nudges",
  "crossover-review",
  "crossover-review-body",
  "crossover-action",
  "crossover-capture",
  // #3629: the units toggle is wired at module load time (alongside Start
  // Over / Stop below), unconditionally -- these two must exist even though
  // this file never exercises renderWalk() itself.
  "crossover-units-imperial",
  "crossover-units-metric",
  "crossover-capture-status",
  "crossover-capture-stop",
  "capture-status",
];

const visibilityListeners = [];

// A real timer registry (not a no-op stub) so delay values are observable.
const timers = [];
let nextTimerId = 1;
globalThis.setTimeout = (fn, delay) => {
  const id = nextTimerId++;
  timers.push({ id, fn, delay });
  return id;
};
globalThis.clearTimeout = (id) => {
  const idx = timers.findIndex((t) => t.id === id);
  if (idx !== -1) timers.splice(idx, 1);
};

const { schedulePoll } = await crossoverMainModule({
  ids,
  documentOptions: {
    addEventListener(name, fn) {
      if (name === "visibilitychange") visibilityListeners.push(fn);
    },
  },
  extraStubs: {
    getJSON: async () => ({}),
    postJSON: async () => ({}),
  },
  exportNames: ["schedulePoll"],
});

// --- visible: schedules at the caller's requested cadence -------------------
schedulePoll(1500);
assert.equal(timers.length, 1, "exactly one timer scheduled");
assert.equal(timers[0].delay, 1500, "visible tab polls at the requested cadence");

// --- hidden: the same request is stretched, never cancelled outright -------
document.visibilityState = "hidden";
schedulePoll(1500);
assert.equal(timers.length, 1, "old timer cleared, exactly one new one scheduled");
assert.ok(
  timers[0].delay >= 8000,
  `hidden tab should poll far less often than 1500ms, got ${timers[0].delay}`,
);

// --- visible again: normal cadence resumes on the next schedule call -------
document.visibilityState = "visible";
schedulePoll(1500);
assert.equal(timers[0].delay, 1500, "cadence returns to normal once visible");

// --- the visibilitychange listener re-applies (not discards) the last cadence
document.visibilityState = "hidden";
assert.equal(visibilityListeners.length, 1, "one visibilitychange listener registered");
visibilityListeners[0]();
assert.ok(
  timers[0].delay >= 8000,
  "going hidden mid-poll re-schedules slower instead of stopping",
);

// --- null (no active polling reason) still means no timer, regardless -----
document.visibilityState = "visible";
schedulePoll(null);
assert.equal(timers.length, 0, "null intent means no polling regardless of visibility");

console.log(JSON.stringify({ ok: true, passed: 6 }));
