// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// A fast pre-arm Stop can become terminal in the refresh awaited by
// stopCapture(). The authoritative next action must be re-rendered after `busy`
// clears rather than remaining disabled until a page reload.

import assert from "node:assert/strict";
import { crossoverMainModule, element } from "./_dom.mjs";

globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

const terminalEnvelope = {
  verdict_text: "Stopped safely",
  steps: [],
  nudges: [],
  capture: { status: "stopped", error: "Measurement stopped safely." },
  next_action: {
    id: "retry",
    label: "Try again",
    endpoint: "/sound/speaker/crossover/capture-capture",
    body: {},
    enabled: true,
  },
  alternate_actions: [],
};
let nextEnvelope = terminalEnvelope;
let postResponse = { capture: { status: "stopping" } };
// Lets a test hold a postJSON call pending so it can inspect render() state
// while that request is still in flight, then release it explicitly.
let postGate = null;
// PR-7's before/after visualization (./cloud.js) is out of scope for this
// harness — it only pins the Stop-measurement flow — so a no-op stands in.
const { elements, render, runAction, stopCapture } = await crossoverMainModule({
  extraStubs: {
    getJSON: async () => nextEnvelope,
    postJSON: async () => {
      if (postGate) await postGate;
      return postResponse;
    },
    renderCloud: () => {},
    redrawCloudChart: () => {},
  },
  exportNames: ["render", "runAction", "stopCapture"],
});

render({
  ...terminalEnvelope,
  capture: { status: "awaiting_capture" },
  next_action: null,
});

await stopCapture();

const actions = elements.get("crossover-action").children;
assert.equal(actions.length, 1);
assert.equal(actions[0].textContent, "Try again");
assert.equal(actions[0].disabled, false, "terminal action is enabled after Stop");

// --- busy (unrelated in-flight actions) must not latch Stop disabled -------
// Only stopCapture()'s own in-flight cancel request may disable the Stop
// control; a slow, unrelated action re-rendering mid-flight must not.
let releasePostGate = null;
postGate = new Promise((resolve) => { releasePostGate = resolve; });
postResponse = { status: "ok" };

const stoppableEnvelope = {
  ...terminalEnvelope,
  capture: { status: "awaiting_capture" },
  next_action: null,
};
render(stoppableEnvelope);
assert.equal(
  elements.get("crossover-capture-stop").disabled,
  false,
  "Stop starts enabled while the capture is stoppable",
);

const actionPromise = runAction(
  { endpoint: "/sound/speaker/crossover/v2/session", body: {} },
  element("restart-session-button"),
);
// A poll re-render arrives while the unrelated action's POST is still in
// flight (busy === true). Stop must stay clickable.
render(stoppableEnvelope);
assert.equal(
  elements.get("crossover-capture-stop").disabled,
  false,
  "an unrelated in-flight action must not disable Stop",
);
releasePostGate();
await actionPromise;
postGate = null;

// stopCapture's OWN cancel request in flight must disable Stop.
postGate = new Promise((resolve) => { releasePostGate = resolve; });
postResponse = { capture: { status: "stopping" } };
render(stoppableEnvelope);
const stopPromise = stopCapture();
assert.equal(
  elements.get("crossover-capture-stop").disabled,
  true,
  "Stop disables itself only while its own cancel request is in flight",
);
releasePostGate();
await stopPromise;
postGate = null;

console.log(JSON.stringify({ ok: true, passed: 6 }));
