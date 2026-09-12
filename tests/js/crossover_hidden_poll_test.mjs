// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0


import assert from "node:assert/strict";
import { crossoverMainModule } from "./_dom.mjs";

const visibilityListeners = [];

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

const { schedulePoll, render } = await crossoverMainModule({
  documentOptions: {
    addEventListener(name, fn) {
      if (name === "visibilitychange") visibilityListeners.push(fn);
    },
  },
  extraStubs: {
    getJSON: async () => ({}),
    renderCloud: () => {},
    redrawCloudChart: () => {},
    postJSON: async () => ({}),
  },
  exportNames: ["schedulePoll", "render"],
});

schedulePoll(1500);
assert.equal(timers.length, 1, "exactly one timer scheduled");
assert.equal(timers[0].delay, 1500, "visible tab polls at the requested cadence");

document.visibilityState = "hidden";
schedulePoll(1500);
assert.equal(timers.length, 1, "old timer cleared, exactly one new one scheduled");
assert.ok(
  timers[0].delay >= 8000,
  `hidden tab should poll far less often than 1500ms, got ${timers[0].delay}`,
);

document.visibilityState = "visible";
schedulePoll(1500);
assert.equal(timers[0].delay, 1500, "cadence returns to normal once visible");

document.visibilityState = "hidden";
assert.equal(visibilityListeners.length, 1, "one visibilitychange listener registered");
visibilityListeners[0]();
assert.ok(
  timers[0].delay >= 8000,
  "going hidden mid-poll re-schedules slower instead of stopping",
);

document.visibilityState = "visible";
schedulePoll(null);
assert.equal(timers.length, 0, "null intent means no polling regardless of visibility");

for (const screen of ["awaiting_plan", "finished"]) {
  render({screen});
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 1500);
}

console.log(JSON.stringify({ ok: true, passed: 12 }));
