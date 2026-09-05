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
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, installFixedDocument } from "./_dom.mjs";

const elements = installFixedDocument(CROSSOVER_IDS);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

globalThis.__getJSON = async () => ({});
globalThis.__postJSON = async () => ({});
// PR-7's before/after visualization (./cloud.js) is out of scope for this
// harness — it only pins the applied chip — so a no-op stands in, same
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

const { render } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "renderCloud", "redrawCloudChart",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render"],
  },
);

const baseEnvelope = {
  verdict_text: "",
  steps: [],
  nudges: [],
  capture: null,
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
check(chip.className === "badge badge--ok", "manual: chip carries the ok tone modifier");

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

// --- an unrecognised state still renders, on the neutral tone ---------
render({ ...baseEnvelope, applied: { state: "future", label: "Something new" } });
check(chip.hidden === false, "unknown state: chip is unhidden");
check(chip.className === "badge badge--idle",
  "unknown state: chip falls back to the idle tone modifier");

// --- an envelope predating the `applied` field must not crash render() ---
render({ ...baseEnvelope });
check(chip.hidden === true, "missing applied field: chip stays hidden, no crash");
check(chip.dataset.state === "none", "missing applied field: data-state defaults to none");

console.log(JSON.stringify({ ok: true, passed }));
