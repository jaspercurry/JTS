// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pins SYS-2 on the /system/ diagnostics disclosure (actions.js): the
// verdict line leads the panel, above the table, and rows sort worst-first
// (fail, warn, skipped, ok) regardless of the fetch order.
//
//   node tests/js/system_diagnostics_render_test.mjs <actions.js>

import assert from "node:assert/strict";
import { buildFunction } from "./_loader.mjs";

const modulePath = process.argv[2];
if (!modulePath) {
  throw new Error("usage: node system_diagnostics_render_test.mjs <actions.js>");
}

// A structural double for dom.js's h(): a plain {tag, props, children} tree,
// cheap to inspect without a browser or a real DOM.
function flatten(items) {
  return items.flatMap((item) => (Array.isArray(item) ? flatten(item) : [item]));
}
function h(tag, props, ...children) {
  return { tag, props: props || {}, children: flatten(children).filter((c) => c != null && c !== false) };
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

// Fetch order is deliberately NOT sorted — the module must reorder it.
globalThis.fetch = async () => ({
  json: async () => ({
    results: [
      { name: "alpha", status: "ok", detail: "up" },
      { name: "bravo", status: "fail", detail: "down", reason: "bravo_down" },
      { name: "charlie", status: "warn", detail: "flaky", reason: "charlie_flaky" },
    ],
    fails: 1,
    warns: 1,
    speaker_silent: false,
  }),
});
const realConsoleError = console.error;
console.error = () => {};

await runDiagnostics(btn, out);
console.error = realConsoleError;

const [first, second] = out.lastChildren;
assert.equal(first.tag, "p.info-card__note", "the verdict leads the diagnostics panel");
assert.equal(second.tag, "div.table-wrap", "the row table follows the verdict");

const table = second.children[0];
const tbody = table.children[0];
const names = tbody.children.map((row) => row.children[1].children[0]);
assert.deepEqual(names, ["bravo", "charlie", "alpha"], "rows sort fail, warn, ok");

console.log(JSON.stringify({ ok: true }));
