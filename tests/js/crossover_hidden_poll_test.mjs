// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// A hidden tab must not stop polling entirely — the phone-in-hand user may
// look away mid-measurement, but the wizard still needs to auto-advance when
// the phone finishes its side. schedulePoll() should slow to HIDDEN_POLL_MS
// while hidden instead of cancelling the timer, and the visibilitychange
// listener must re-apply (not discard) the caller's last requested cadence.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { installFixedDocument } from "./_dom.mjs";

const ids = [
  "crossover-verdict",
  "crossover-start-over",
  "crossover-steps",
  "crossover-nudges",
  "crossover-review",
  "crossover-review-body",
  "crossover-action",
  "crossover-relay",
  "crossover-relay-status",
  "crossover-relay-stop",
  "capture-status",
];

const visibilityListeners = [];
installFixedDocument(ids, {
  addEventListener(name, fn) {
    if (name === "visibilitychange") visibilityListeners.push(fn);
  },
});

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

globalThis.__getJSON = async () => ({});
globalThis.__postJSON = async () => ({});

const { schedulePoll } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals(["getJSON", "postJSON"]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["schedulePoll"],
  },
);

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
