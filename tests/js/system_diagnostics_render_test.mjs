// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pins SYS-2 on the /system/ diagnostics disclosure (actions.js): the
// verdict node leads the table node. Worst-first row order is the doctor's
// own sort (jasper/cli/doctor/_cli.py, pinned in tests/test_doctor_core.py)
// — the payload arrives pre-sorted, so this harness does not re-pin it.
//
//   node tests/js/system_diagnostics_render_test.mjs <actions.js>

import assert from "node:assert/strict";
import { buildFunction, h } from "./_loader.mjs";

const modulePath = process.argv[2];
if (!modulePath) {
  throw new Error("usage: node system_diagnostics_render_test.mjs <actions.js>");
}

const { runDiagnostics } = buildFunction(modulePath, {
  stripImports: true,
  stripExports: true,
  guardNoImports: true,
  params: ["h"],
  returns: ["runDiagnostics"],
})(h);

function makeOut() {
  return {
    style: {},
    lastChildren: null,
    replaceChildren(...children) { this.lastChildren = children; },
  };
}

const btn = { disabled: false };
const out = makeOut();

globalThis.fetch = async () => ({
  json: async () => ({
    results: [{ name: "bravo", status: "fail", detail: "down", reason: "bravo_down" }],
    fails: 1,
    warns: 0,
    speaker_silent: false,
  }),
});
await runDiagnostics(btn, out);

const [first, second] = out.lastChildren;
assert.equal(first.tag, "p.info-card__note", "the verdict leads the diagnostics panel");
assert.equal(second.tag, "div.table-wrap", "the row table follows the verdict");

console.log(JSON.stringify({ ok: true }));
