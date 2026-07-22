// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// A hidden tab must not stop polling entirely — the phone-in-hand user may
// look away mid-measurement, but the wizard still needs to auto-advance when
// the phone finishes its side. schedulePoll() should slow to HIDDEN_POLL_MS
// while hidden instead of cancelling the timer, and the visibilitychange
// listener must re-apply (not discard) the caller's last requested cadence.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function element(id = "") {
  return {
    id,
    children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    dataset: {},
    disabled: false,
    textContent: "",
    href: "",
    addEventListener() {},
    append() {},
    replaceChildren() {},
    setAttribute() {},
  };
}

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
  "crossover-relay-link",
  "crossover-relay-qr",
  "crossover-relay-stop",
  "capture-status",
];
const elements = new Map(ids.map((id) => [id, element(id)]));

const visibilityListeners = [];
globalThis.document = {
  visibilityState: "visible",
  addEventListener(name, fn) {
    if (name === "visibilitychange") visibilityListeners.push(fn);
  },
  createElement: (tag) => element(tag),
  getElementById: (id) => elements.get(id),
};

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
// deploy/assets/shared/js/qr.js's renderRelayQr is exercised elsewhere
// (tests/js/qr_harness.mjs for the encoder/DOM, crossover_stop_render_test.mjs
// for the wiring) — this file only drives schedulePoll(), so a no-op stands
// in purely so the module (which imports it) loads without a real DOM.
globalThis.__renderRelayQr = () => {};

const here = dirname(fileURLToPath(import.meta.url));
let source = readFileSync(
  resolve(here, "../../deploy/assets/correction/js/crossover/main.js"),
  "utf8",
);
source = source.replace(
  /^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm,
  "",
);
source =
  "const getJSON = globalThis.__getJSON; const postJSON = globalThis.__postJSON; " +
  "const renderRelayQr = globalThis.__renderRelayQr;\n" + source;
const bootStart = source.lastIndexOf("\nrefresh().catch((error) => {");
if (bootStart < 0) throw new Error("crossover module boot call not found");
source = source.slice(0, bootStart).concat(
  "\nexport { schedulePoll };\n",
);
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { schedulePoll } = await import(dataUrl);

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
