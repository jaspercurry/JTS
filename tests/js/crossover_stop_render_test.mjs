// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// A fast pre-arm Stop can become terminal in the refresh awaited by
// stopRelay(). The authoritative next action must be re-rendered after `busy`
// clears rather than remaining disabled until a page reload.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function classList() {
  const values = new Set();
  return {
    add(...names) { names.forEach((name) => values.add(name)); },
    remove(...names) { names.forEach((name) => values.delete(name)); },
    contains(name) { return values.has(name); },
    toggle(name, force) {
      if (force) values.add(name); else values.delete(name);
    },
  };
}

function element(id = "") {
  return {
    id,
    children: [],
    classList: classList(),
    dataset: {},
    disabled: false,
    textContent: "",
    addEventListener() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(key, value) { this[key] = String(value); },
  };
}

const ids = [
  "crossover-verdict",
  "crossover-steps",
  "crossover-nudges",
  "crossover-review",
  "crossover-review-body",
  "crossover-action",
  "crossover-relay",
  "crossover-relay-status",
  "crossover-relay-link",
  "crossover-relay-stop",
  "capture-status",
];
const elements = new Map(ids.map((id) => [id, element(id)]));
globalThis.document = {
  visibilityState: "visible",
  addEventListener() {},
  createElement: (tag) => element(tag),
  getElementById: (id) => elements.get(id),
};
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

const terminalEnvelope = {
  verdict_text: "Stopped safely",
  steps: [],
  nudges: [],
  relay: { status: "stopped", error: "Measurement stopped safely." },
  next_action: {
    id: "retry",
    label: "Try again",
    endpoint: "/correction/crossover/relay-capture",
    body: {},
    enabled: true,
  },
  alternate_actions: [],
};
let nextEnvelope = terminalEnvelope;
let postResponse = { relay: { status: "stopping" } };
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

const here = dirname(fileURLToPath(import.meta.url));
let source = readFileSync(
  resolve(here, "../../deploy/assets/correction/js/crossover/main.js"),
  "utf8",
);
source = source.replace(
  /^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*/m,
  "const getJSON = globalThis.__getJSON; const postJSON = globalThis.__postJSON;\n",
);
const bootStart = source.lastIndexOf("\nrefresh().catch((error) => {");
if (bootStart < 0) throw new Error("crossover module boot call not found");
source = source.slice(0, bootStart).concat(
  "\nexport { render, runAction, stopRelay };\n",
);
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { render, runAction, stopRelay } = await import(dataUrl);

render({
  ...terminalEnvelope,
  relay: { status: "awaiting_phone", tap_link: "https://capture.test/#s=cap" },
  next_action: null,
});
await stopRelay();

const actions = elements.get("crossover-action").children;
assert.equal(actions.length, 1);
assert.equal(actions[0].textContent, "Try again");
assert.equal(actions[0].disabled, false, "terminal action is enabled after Stop");

// --- busy (unrelated in-flight actions) must not latch Stop disabled -------
// Only stopRelay()'s own in-flight cancel request may disable the Stop
// control; a slow, unrelated action re-rendering mid-flight must not.
let releasePostGate = null;
postGate = new Promise((resolve) => { releasePostGate = resolve; });
postError = null;
postResponse = { status: "ok" };

const stoppableEnvelope = {
  ...terminalEnvelope,
  relay: { status: "awaiting_phone", tap_link: "https://capture.test/#s=cap2" },
  next_action: null,
};
render(stoppableEnvelope);
assert.equal(
  elements.get("crossover-relay-stop").disabled,
  false,
  "Stop starts enabled while the relay is stoppable",
);

const actionPromise = runAction(
  { endpoint: "/correction/crossover/level-match", body: {} },
  element("level-match-button"),
);
// A poll re-render arrives while the unrelated action's POST is still in
// flight (busy === true). Stop must stay clickable.
render(stoppableEnvelope);
assert.equal(
  elements.get("crossover-relay-stop").disabled,
  false,
  "an unrelated in-flight action must not disable Stop",
);
releasePostGate();
await actionPromise;
postGate = null;

// stopRelay's OWN cancel request in flight must disable Stop.
postGate = new Promise((resolve) => { releasePostGate = resolve; });
postResponse = { relay: { status: "stopping" } };
render(stoppableEnvelope);
const stopPromise = stopRelay();
assert.equal(
  elements.get("crossover-relay-stop").disabled,
  true,
  "Stop disables itself only while its own cancel request is in flight",
);
releasePostGate();
await stopPromise;
postGate = null;

render({
  ...terminalEnvelope,
  candidate_review: {
    retained_crossover_regions: [{
      lower_role: "woofer",
      upper_role: "tweeter",
      fc_hz: 1600,
      filter_family: "LinkwitzRiley",
      order: 4,
      lower_polarity: "non-inverted",
      upper_polarity: "non-inverted",
    }],
    drivers: [{
      role: "woofer",
      attenuation_db: 0,
      delay_ms: 0.0375,
      polarity: "non-inverted",
    }],
    evidence: {
      isolated_artifact: {fingerprint: "isolated-proof"},
      summed_artifact: {fingerprint: "summed-proof"},
      algorithm_id: "candidate-evaluator",
      algorithm_version: "1",
    },
  },
});
assert.equal(elements.get("crossover-review").classList.contains("hidden"), false);
assert.equal(elements.get("crossover-review-body").children.length, 1);

nextEnvelope = {
  ...terminalEnvelope,
  verdict_text: "Restart the complete measurements.",
  candidate_review: null,
  next_action: {
    id: "level_match",
    label: "Restart driver and alignment measurements",
    endpoint: "/correction/crossover/level-match",
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

console.log(JSON.stringify({ ok: true, passed: 16 }));
