// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// A fast pre-arm Stop can become terminal in the refresh awaited by
// stopCapture(). The authoritative next action must be re-rendered after `busy`
// clears rather than remaining disabled until a page reload.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, element, installFixedDocument } from "./_dom.mjs";

const elements = installFixedDocument(CROSSOVER_IDS);
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
    endpoint: "/correction/crossover/capture-capture",
    body: {},
    enabled: true,
  },
  alternate_actions: [],
};
let nextEnvelope = terminalEnvelope;
let postResponse = { capture: { status: "stopping" } };
let postError = null;
// Lets a test hold a postJSON call pending so it can inspect render() state
// while that request is still in flight, then release it explicitly.
let postGate = null;
globalThis.__getJSON = async () => nextEnvelope;
globalThis.__postJSON = async () => {
  if (postGate) await postGate;
  if (postError) throw postError;
  return postResponse;
};

// PR-7's before/after visualization (./cloud.js) is out of scope for this
// harness — it only pins the Stop-measurement flow — so a no-op stands in.
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

const { render, runAction, stopCapture } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "renderCloud", "redrawCloudChart",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render", "runAction", "stopCapture"],
  },
);

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
postError = null;
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
  { endpoint: "/correction/crossover/v2/session", body: {} },
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

render({
  ...terminalEnvelope,
  candidate_review: {
    trims: [
      {role: "woofer", attenuation_db: -2.5},
      {role: "tweeter", attenuation_db: 0},
    ],
    delay: {role: "woofer", delay_ms: 0.0375},
    polarity: "keep",
    confidence: 0.71,
    fingerprint: "cand-proof",
    program_id: "prog-1",
  },
});
assert.equal(elements.get("crossover-review").hidden, false);
assert.equal(elements.get("crossover-review-body").children.length, 1);

nextEnvelope = {
  ...terminalEnvelope,
  verdict_text: "Restart the complete measurements.",
  candidate_review: null,
  next_action: {
    id: "restart_session",
    label: "Restart driver and alignment measurements",
    endpoint: "/correction/crossover/v2/session",
    body: {},
  },
};
postResponse = { status: "candidate_refused" };
await runAction(
  {
    endpoint: "/correction/crossover/candidate",
    body: {},
  },
  element("prepare-candidate"),
);
assert.equal(
  elements.get("crossover-verdict").textContent,
  "Restart the complete measurements.",
);
assert.equal(
  elements.get("crossover-action").children[0].textContent,
  "Restart driver and alignment measurements",
);

nextEnvelope = {
  ...terminalEnvelope,
  verdict_text: "The previous crossover was restored exactly.",
  next_action: {
    id: "retry_measured_candidate_apply",
    label: "Retry reviewed crossover",
    endpoint: "/correction/crossover/apply",
    body: {},
  },
};
postError = Object.assign(
  new Error("Apply failed; the previous crossover was restored."),
  { status: 409, body: { status: "rolled_back" } },
);
await runAction(
  { endpoint: "/correction/crossover/apply", body: {} },
  element("apply-candidate"),
);
assert.equal(
  elements.get("crossover-verdict").textContent,
  "The previous crossover was restored exactly.",
);
assert.equal(
  elements.get("crossover-action").children[0].textContent,
  "Retry reviewed crossover",
);
assert.equal(
  elements.get("capture-status").textContent,
  "Apply failed; the previous crossover was restored.",
);

nextEnvelope = {
  ...terminalEnvelope,
  verdict_text: "The graph is applied; finish its durable state.",
  next_action: {
    id: "finish_measured_candidate_apply",
    label: "Finish apply",
    endpoint: "/correction/crossover/apply",
    body: {},
  },
};
postError = Object.assign(
  new Error("Candidate apply needs durable finalization."),
  { status: 500, body: { code: "candidate_apply_finalization_required" } },
);
await runAction(
  { endpoint: "/correction/crossover/apply", body: {} },
  element("finish-candidate"),
);
assert.equal(
  elements.get("crossover-verdict").textContent,
  "The graph is applied; finish its durable state.",
);
assert.equal(
  elements.get("crossover-action").children[0].textContent,
  "Finish apply",
);
assert.equal(
  elements.get("capture-status").textContent,
  "Candidate apply needs durable finalization.",
);

console.log(JSON.stringify({ ok: true, passed: 18 }));
