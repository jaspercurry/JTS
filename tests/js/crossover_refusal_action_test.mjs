// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Issues #1820 / #1821, review item S1. The session-open pre-flight is the
// PRIMARY path for a profile-not-confirmed refusal, and a pre-flight refusal
// can never reach the envelope's hard-stop screen: that screen renders from a
// PERSISTED failure, and the pre-flight refuses before any state is written, on
// purpose (no link minted, no session burned). So the reason's own resolution
// control has to arrive with the 400 and render beside the message — otherwise
// the household reads the exact remedy as flat text and has to go find the
// control themselves.
//
// This drives the real `runAction` against a rejected POST carrying the 400
// body, and asserts:
//   1. the refusal message is shown, `bad` tone;
//   2. the server's next_action renders as a link with its label + href;
//   3. the link SURVIVES the refresh + action-row re-render that follow;
//   4. a refusal with no next_action still renders plain text (no invented
//      button), and a later plain setStatus clears the stale link.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { elementWithLiveText as element, installFixedDocument } from "./_dom.mjs";

const ids = [
  "crossover-verdict",
  "crossover-applied",
  "crossover-start-over",
  "crossover-steps",
  "crossover-nudges",
  "crossover-review",
  "crossover-review-body",
  "crossover-cloud",
  "crossover-cloud-provenance",
  "crossover-cloud-chart",
  "crossover-cloud-geometry",
  "crossover-cloud-callouts",
  "crossover-cloud-pending",
  "crossover-chart-legend-measure",
  "crossover-chart-legend-verify",
  "crossover-chart-legend-corridor",
  "crossover-chart-legend-excluded",
  "crossover-action",
  "crossover-capture",
  "crossover-walk",
  "crossover-walk-progress",
  "crossover-walk-headline",
  "crossover-walk-detail",
  "crossover-walk-action",
  "crossover-capture-status",
  "crossover-capture-stop",
  "capture-status",
];
const elements = installFixedDocument(ids, {
  factory: element,
  // setStatus builds `[textNode, anchor]`, so the harness needs real text
  // nodes rather than the element stub.
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
});
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.window = { addEventListener() {} };

const baseEnvelope = {
  verdict_text: "",
  steps: [],
  nudges: [],
  capture: null,
  next_action: null,
  alternate_actions: [],
};

globalThis.__getJSON = async () => ({ ...baseEnvelope });
globalThis.__jtsConfirm = async () => true;
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

let postRejection = null;
globalThis.__postJSON = async () => {
  if (postRejection) throw postRejection;
  return { ...baseEnvelope };
};

const { render, runAction, setStatus } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "jtsConfirm", "renderCloud", "redrawCloudChart",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render", "runAction", "setStatus"],
  },
);

const statusEl = elements.get("capture-status");

function statusText() {
  return statusEl.textContent;
}

function statusLink() {
  return statusEl.children.find((child) => child && child.href) || null;
}

function refusal(message, body) {
  const err = new Error(message);
  err.status = 400;
  err.body = body;
  return err;
}

// The real copy + action the server sends for this refusal.
const CONFIRM_COPY =
  "JTS could not use this speaker's saved safety limits, so it did not play " +
  "the measurement signal. Review the limits in speaker setup and save them " +
  "again, then measure.";
const CONFIRM_ACTION = {
  id: "review_safety_limits",
  label: "Review safety limits",
  href: "/sound/#confirm-safety-limits",
};

const START_SESSION = {
  id: "start_session",
  label: "Start",
  endpoint: "/correction/crossover/v2/session",
  body: {},
};

// --- 1/2/3: a coded refusal renders its control, and it survives the refresh
render({ ...baseEnvelope });
postRejection = refusal(CONFIRM_COPY, {
  ok: false, error: CONFIRM_COPY, next_action: CONFIRM_ACTION,
});
await runAction(START_SESSION, element("btn"));

assert.equal(statusEl.dataset.tone, "bad", "a refusal paints the bad tone");
assert.ok(
  statusText().includes("Review the limits in speaker setup"),
  `the refusal copy must be shown, got: ${statusText()}`,
);
const link = statusLink();
assert.ok(link, "the refusal's resolution control must render as a link");
assert.equal(link.href, CONFIRM_ACTION.href, "the link points at the control");
assert.equal(link.textContent, CONFIRM_ACTION.label, "the link is labelled");
assert.ok(
  /btn/.test(link.className),
  `the control should look like a button, got class: ${link.className}`,
);
// runAction's catch calls refresh() and then re-renders the action row; the
// status control must not be wiped by either.
assert.ok(statusLink(), "the control must survive the post-failure refresh");

// --- 4a: an UNCODED refusal renders plain text, never an invented button ----
postRejection = refusal("the woofer and tweeter targets are not both active", {
  ok: false, error: "the woofer and tweeter targets are not both active",
});
await runAction(START_SESSION, element("btn"));
assert.equal(
  statusLink(),
  null,
  "a refusal with no server-named action must not grow one",
);
assert.ok(
  statusText().includes("not both active"),
  `the plain refusal copy must still show, got: ${statusText()}`,
);

// --- 4b: a later plain status clears a stale control ------------------------
postRejection = refusal(CONFIRM_COPY, {
  ok: false, error: CONFIRM_COPY, next_action: CONFIRM_ACTION,
});
await runAction(START_SESSION, element("btn"));
assert.ok(statusLink(), "control is present before the plain status");
setStatus("Working…");
assert.equal(
  statusLink(),
  null,
  "a plain setStatus must clear the previous refusal's control",
);
assert.equal(statusText(), "Working…");

console.log(JSON.stringify({ ok: true, passed: 11 }));
