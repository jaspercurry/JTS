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
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function element(id = "") {
  return {
    id,
    children: [],
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
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
  "crossover-applied",
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
globalThis.document = {
  visibilityState: "visible",
  addEventListener() {},
  createElement: (tag) => element(tag),
  getElementById: (id) => elements.get(id),
};
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
  "const renderRelayQr = globalThis.__renderRelayQr; " +
  "const jtsConfirm = globalThis.__jtsConfirm;\n" + source;
const bootStart = source.lastIndexOf("\nrefresh().catch((error) => {");
if (bootStart < 0) throw new Error("crossover module boot call not found");
source = source.slice(0, bootStart).concat(
  "\nexport { render, startOver };\n",
);
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { render, startOver } = await import(dataUrl);

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

console.log(JSON.stringify({ ok: true, passed: 9 }));
