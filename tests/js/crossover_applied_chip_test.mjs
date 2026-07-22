// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The durable "a crossover is applied" chip (crossover_envelope.py's
// `_applied_chip` / the `applied` envelope field) is a separate signal from
// the per-run step spine — a manual or automatic crossover can be applied
// while the CURRENT measurement run is still mid-way, or hasn't started at
// all. This harness pins the FRONTEND half of that split: render() must show
// the chip with the server's exact label and `data-state` whenever
// `env.applied.state` is not "none", and hide it (via the native `hidden`
// attribute, never a CSS class) otherwise — including when `applied` is
// missing entirely, which must not throw.

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
    hidden: false,
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

globalThis.__getJSON = async () => ({});
globalThis.__postJSON = async () => ({});
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
  "\nexport { render };\n",
);
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { render } = await import(dataUrl);

const baseEnvelope = {
  verdict_text: "",
  steps: [],
  nudges: [],
  relay: null,
  next_action: null,
  alternate_actions: [],
};
const chip = elements.get("crossover-applied");

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

// --- manual crossover applied: chip shows the server's exact label/state --
render({
  ...baseEnvelope,
  applied: { state: "manual", label: "Manual crossover applied" },
});
check(chip.hidden === false, "manual: chip is unhidden");
check(chip.textContent === "Manual crossover applied", "manual: chip text is the server label");
check(chip.dataset.state === "manual", "manual: chip data-state is manual");

// --- automatic crossover applied: same shape, different state/label ------
render({
  ...baseEnvelope,
  applied: { state: "automatic", label: "Automatic crossover applied" },
});
check(chip.hidden === false, "automatic: chip is unhidden");
check(chip.textContent === "Automatic crossover applied", "automatic: chip text is the server label");
check(chip.dataset.state === "automatic", "automatic: chip data-state is automatic");

// --- no crossover applied: chip is hidden and cleared, not just re-labeled -
render({
  ...baseEnvelope,
  applied: { state: "none", label: "No speaker profile applied" },
});
check(chip.hidden === true, "none: chip is hidden");
check(chip.textContent === "", "none: chip text is cleared");
check(chip.dataset.state === "none", "none: chip data-state is none");

// --- an envelope predating the `applied` field must not crash render() ---
render({ ...baseEnvelope });
check(chip.hidden === true, "missing applied field: chip stays hidden, no crash");
check(chip.dataset.state === "none", "missing applied field: data-state defaults to none");

console.log(JSON.stringify({ ok: true, passed }));
