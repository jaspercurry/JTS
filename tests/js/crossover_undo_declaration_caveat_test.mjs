// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Issue #2292. Undo restores two things through two different writers: the DSP
// graph, and the crossover `/sound` declares. The graph half can succeed while
// the declaration half honestly refuses (somebody edited Sound after the
// crossover was applied), and that combination returns HTTP 200 — so the
// wizard's success branch is the ONLY place the household can learn about it.
// Before this the branch said "Updated." unconditionally and the refusal lived
// in the journal alone, which is the same silence the issue was filed about,
// one layer up.
//
// Drives the real `runAction` against a 200 response and asserts:
//   1. a `sound_declaration_message` is shown verbatim, in the `bad` tone;
//   2. an ordinary success still says "Updated." in the `ok` tone;
//   3. a relay-starting action still says its own copy — the caveat branch
//      does not swallow the sibling message it sits beside.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function element(id = "") {
  const node = {
    id,
    children: [],
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    dataset: {},
    disabled: false,
    hidden: false,
    className: "",
    _text: "",
    addEventListener() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this._text = ""; },
    setAttribute(key, value) { this[key] = String(value); },
  };
  Object.defineProperty(node, "textContent", {
    get() {
      return this.children.length
        ? this.children.map((child) => child.textContent || "").join("")
        : this._text;
    },
    set(value) {
      this._text = value == null ? "" : String(value);
      this.children = [];
    },
  });
  return node;
}

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
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  getElementById: (id) => elements.get(id),
};
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.window = { addEventListener() {} };

const baseEnvelope = {
  verdict_text: "",
  steps: [],
  nudges: [],
  relay: null,
  next_action: null,
  alternate_actions: [],
};

globalThis.__getJSON = async () => ({ ...baseEnvelope });
globalThis.__renderRelayQr = () => {};
globalThis.__jtsConfirm = async () => true;
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

let postResponse = { ...baseEnvelope };
globalThis.__postJSON = async () => postResponse;

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
  "const jtsConfirm = globalThis.__jtsConfirm; " +
  "const renderCloud = globalThis.__renderCloud; " +
  "const redrawCloudChart = globalThis.__redrawCloudChart;\n" + source;
const bootStart = source.lastIndexOf("\nrefresh().catch((error) => {");
if (bootStart < 0) throw new Error("crossover module boot call not found");
source = source.slice(0, bootStart).concat(
  "\nexport { render, runAction };\n",
);
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { render, runAction } = await import(dataUrl);

const statusEl = elements.get("capture-status");
render({ ...baseEnvelope });

const UNDO = {
  id: "verify_undo",
  label: "Undo (restore previous sound)",
  endpoint: "/correction/crossover/v2/restore",
  body: {},
};

// The server's own copy for declaration_refused_sound_moved.
const CAVEAT =
  "The previous sound is back, but Sound still declares 2250 Hz: the speaker " +
  "design changed after this crossover was applied, so JTS left it alone. " +
  "Set the crossover back to 2500 Hz in Sound settings if you want it.";

// --- 1: the graph came back, the declaration did not, and the screen says so
postResponse = {
  ...baseEnvelope,
  status: "restored",
  sound_declaration: "declaration_refused_sound_moved",
  sound_declaration_message: CAVEAT,
};
await runAction(UNDO, element("btn"));

assert.equal(
  statusEl.textContent,
  CAVEAT,
  `the server's caveat must be shown verbatim, got: ${statusEl.textContent}`,
);
assert.equal(
  statusEl.dataset.tone,
  "bad",
  "a restore the household has to act on is not painted as a clean success",
);

// --- 2: an ordinary success is untouched ------------------------------------
postResponse = {
  ...baseEnvelope,
  status: "restored",
  sound_declaration: "declaration_restored",
};
await runAction(UNDO, element("btn"));

assert.equal(statusEl.textContent, "Updated.");
assert.equal(
  statusEl.dataset.tone,
  "ok",
  "a fully successful Undo keeps the plain success tone",
);

// --- 3: the relay branch beside it still speaks for itself ------------------
postResponse = { ...baseEnvelope, relay: {url: "https://capture.example/x"} };
await runAction(
  {id: "start", label: "Start", endpoint: "/correction/crossover/v2/session", body: {}},
  element("btn"),
);

assert.equal(statusEl.textContent, "The measurement page is ready.");
assert.equal(statusEl.dataset.tone, "ok");

console.log(JSON.stringify({ ok: true, passed: 6 }));
