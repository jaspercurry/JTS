// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The in-flow "Start over" (scoped measurement-journey reset) must:
//   1. confirm with GROUPING-AWARE copy — a bonded speaker is told it will
//      fall back to solo until re-measured (its group crossover is rebuilt
//      from the cleared measurement evidence); a solo speaker keeps the
//      accurate "what is playing now stays" copy (adversarial-review S1b);
//   2. surface a PARTIAL reset honestly — a reset whose status is not
//      "cleared" must not paint the status line green (adversarial-review N1).

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, installFixedDocument } from "./_dom.mjs";

const elements = installFixedDocument(CROSSOVER_IDS);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

const baseEnvelope = {
  verdict_text: "",
  steps: [],
  nudges: [],
  relay: null,
  next_action: null,
  alternate_actions: [],
};

let confirmMessages = [];
let confirmAnswer = false;
globalThis.__jtsConfirm = async (message) => {
  confirmMessages.push(message);
  return confirmAnswer;
};
let postResponse = { ...baseEnvelope };
globalThis.__getJSON = async () => ({ ...baseEnvelope });
globalThis.__postJSON = async () => postResponse;
// PR-7's before/after visualization (./cloud.js) is out of scope for this
// harness — it only pins Start-over — so a no-op stands in, same shape as
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

const { render, startOver } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "jtsConfirm", "renderCloud", "redrawCloudChart",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render", "startOver"],
  },
);

const statusEl = elements.get("capture-status");

// --- solo speaker: confirm copy states what is playing now is preserved ----
render({ ...baseEnvelope, grouping_member: false });
confirmMessages = [];
confirmAnswer = false; // cancel — we only care about the copy
await startOver();
assert.equal(confirmMessages.length, 1, "solo Start over shows one confirm");
const soloMsg = confirmMessages[0];
assert.ok(
  /playing now stay/i.test(soloMsg),
  `solo copy should reassure current crossover is kept, got: ${soloMsg}`,
);
assert.ok(
  !/grouped/i.test(soloMsg),
  "solo copy must not mention grouping",
);

// --- grouped speaker: confirm copy is honest about the solo fallback -------
render({ ...baseEnvelope, grouping_member: true });
confirmMessages = [];
confirmAnswer = false;
await startOver();
assert.equal(confirmMessages.length, 1, "grouped Start over shows one confirm");
const groupedMsg = confirmMessages[0];
assert.ok(
  /grouped/i.test(groupedMsg),
  `grouped copy should name the grouping, got: ${groupedMsg}`,
);
assert.ok(
  /solo/i.test(groupedMsg) && /measure it again|re-?measure/i.test(groupedMsg),
  `grouped copy should warn about solo fallback until re-measure, got: ${groupedMsg}`,
);
assert.ok(
  !/playing now stay/i.test(groupedMsg),
  "grouped copy must not promise the current crossover is frozen",
);

// --- honest status branch: a full clear paints green -----------------------
render({ ...baseEnvelope, grouping_member: false });
confirmAnswer = true;
postResponse = { ...baseEnvelope, reset: { status: "cleared", errors: [] } };
await startOver();
assert.equal(statusEl.dataset.tone, "ok", "a full clear paints green");
assert.ok(/cleared/i.test(statusEl.textContent), "green message names the clear");

// --- honest status branch: a PARTIAL clear is NOT painted green ------------
render({ ...baseEnvelope, grouping_member: false });
confirmAnswer = true;
postResponse = {
  ...baseEnvelope,
  reset: { status: "partial", errors: ["measurements"] },
};
await startOver();
assert.equal(
  statusEl.dataset.tone,
  "bad",
  "a partial reset must not be painted green",
);
assert.ok(
  /could not be cleared/i.test(statusEl.textContent),
  `partial message should be honest, got: ${statusEl.textContent}`,
);

// --- W6.11: the finally re-renders the action row (sibling parity) --------
// render(response) inside the try runs WHILE busy is still true, so a fresh
// next_action's button bakes `disabled: busy` = true. Only a re-render
// AFTER busy flips back to false (the finally, matching stopRelay/runAction)
// clears it — before this fix "Start measurement" stayed disabled until a
// manual reload.
render({ ...baseEnvelope, grouping_member: false });
confirmAnswer = true;
postResponse = {
  ...baseEnvelope,
  reset: { status: "cleared", errors: [] },
  next_action: { label: "Start measurement", endpoint: "/x" },
};
await startOver();
const actionEl = elements.get("crossover-action");
assert.equal(actionEl.children.length, 1, "the fresh next_action rendered");
assert.equal(
  actionEl.children[0].disabled,
  false,
  "the action button must not stay disabled after Start over completes",
);

console.log(JSON.stringify({ ok: true, passed: 11 }));
