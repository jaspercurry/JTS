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
globalThis.__getJSON = async () => terminalEnvelope;
globalThis.__postJSON = async () => ({ relay: { status: "stopping" } });

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
source = source.slice(0, bootStart).concat("\nexport { render, stopRelay };\n");
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { render, stopRelay } = await import(dataUrl);

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

console.log(JSON.stringify({ ok: true, passed: 3 }));
