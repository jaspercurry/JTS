// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Behaviour pins for startPolling() in deploy/assets/shared/js/http.js, the
// one poller every live page schedules through. What must hold: it runs once
// at start and then on the interval, a hidden tab slows to hiddenIntervalMs
// instead of hammering a 1 GB Pi (and instead of stopping outright), it comes
// current the instant the tab is looked at again, a rejected fn is retried on
// the next tick rather than ending the poll, ticks never overlap (a tick
// swallowed mid-run is re-run as soon as that run settles), and stop() is
// final.
//
// Run via tests/test_web_http_helper.py. A virtual clock stands in for real
// timers so the pins are deterministic and instant.
import assert from "node:assert/strict";

let now = 0;
let nextTimerId = 1;
const timers = new Map();
globalThis.setTimeout = (fn, delay) => {
  const id = nextTimerId++;
  timers.set(id, { fn, due: now + (delay || 0) });
  return id;
};
globalThis.clearTimeout = (id) => { timers.delete(id); };

const visibilityListeners = [];
globalThis.document = {
  visibilityState: "visible",
  addEventListener(name, fn) {
    if (name === "visibilitychange") visibilityListeners.push(fn);
  },
  removeEventListener(name, fn) {
    const idx = visibilityListeners.indexOf(fn);
    if (idx !== -1) visibilityListeners.splice(idx, 1);
  },
};

const logged = [];
console.error = (...args) => { logged.push(args.join(" ")); };

// Run every timer that has come due, in due order. Awaits each callback, so a
// tick's own rescheduling has happened before the next one is considered, and
// drains pending microtasks first so a run that settled between calls has
// scheduled its successor.
async function advance(ms) {
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
  now += ms;
  for (;;) {
    const due = [...timers.entries()]
      .filter(([, t]) => t.due <= now)
      .sort((a, b) => a[1].due - b[1].due)[0];
    if (!due) break;
    timers.delete(due[0]);
    await due[1].fn();
  }
}

function setVisibility(value) {
  document.visibilityState = value;
  visibilityListeners.forEach((fn) => fn());
}

const { startPolling } = await import("../../deploy/assets/shared/js/http.js");

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

// --- runs at once, then on the interval ------------------------------------
{
  let calls = 0;
  const stop = startPolling(() => { calls += 1; }, { intervalMs: 20 });
  check(calls === 1, "startPolling runs its first tick at start — no page repeats the call");
  await advance(20);
  check(calls === 2, "a visible tab then ticks once per interval");
  await advance(20);
  check(calls === 3, "and keeps ticking");

  // --- hidden: slows to hiddenIntervalMs, does not stop --------------------
  setVisibility("hidden");
  await advance(20 * 5);
  check(calls === 3, "a hidden tab does not tick on the visible cadence");
  await advance(60000);
  check(calls === 4, "a hidden tab still ticks on the hidden cadence, it does not stop");

  // --- visible again: comes current immediately ---------------------------
  setVisibility("visible");
  await advance(0);
  check(calls === 5, "becoming visible ticks immediately");

  // --- stop() is final, including against a later visibility change -------
  stop();
  await advance(60000);
  setVisibility("hidden");
  setVisibility("visible");
  await advance(60000);
  check(calls === 5, "stop() ends the poll for good");
}

// --- a rejected fn is retried on the next tick -----------------------------
{
  let calls = 0;
  logged.length = 0;
  const stop = startPolling(async () => {
    calls += 1;
    throw new Error("backend down");
  }, { intervalMs: 20 });
  check(calls === 1, "the first tick ran");
  await advance(20);
  check(calls === 2, "a failed fn does not end the poll — the next tick still fires");
  check(logged.length === 2, "each failure is reported rather than swallowed");
  stop();
}

// --- ticks never overlap ---------------------------------------------------
{
  let calls = 0;
  let release = null;
  const stop = startPolling(() => {
    calls += 1;
    if (calls > 1) return Promise.resolve();
    return new Promise((resolve) => { release = resolve; });
  }, { intervalMs: 20 });
  check(calls === 1, "the slow first tick started and is still in flight");
  setVisibility("visible");
  await advance(0);
  check(calls === 1, "a visibility wake does not re-enter an in-flight tick");
  release();
  await advance(0);
  check(calls === 2, "the swallowed wake runs the moment the slow tick settles");
  await advance(20);
  check(calls === 3, "and the normal cadence resumes after it");
  stop();
}

console.log(JSON.stringify({ ok: true, passed }));
