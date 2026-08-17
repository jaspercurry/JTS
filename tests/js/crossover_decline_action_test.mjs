// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// #2641, the client half. The server minted "Keep current sound" as a
// decision, and renderActions() branched on `href` FIRST — so the decision
// rendered as an anchor, the click reloaded the page, and the household
// landed back on the same decision screen. Measured live: five clicks, no
// state-changing request in the network log.
//
// The invariant this pins is the one that makes "every minted action is
// machine-actionable" true for the BROWSER as well as for a driver reading
// the envelope: when an action carries an endpoint, that endpoint is what the
// control performs; `href` is only a presentation hint. An action with an
// href and no endpoint is still a navigation, which is what keeps
// "Continue to Room correction" a link.

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
    toggle(name, force) { if (force) values.add(name); else values.delete(name); },
  };
}

function element(id = "") {
  return {
    id,
    tag: id,
    children: [],
    classList: classList(),
    dataset: {},
    disabled: false,
    textContent: "",
    href: "",
    _listeners: {},
    addEventListener(event, fn) {
      (this._listeners[event] = this._listeners[event] || []).push(fn);
    },
    click() {
      let result;
      for (const fn of this._listeners.click || []) result = fn();
      return result;
    },
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

const posted = [];
let nextEnvelope = {
  verdict_text: "", steps: [], nudges: [], relay: null,
  next_action: null, alternate_actions: [],
};
globalThis.__getJSON = async () => nextEnvelope;
globalThis.__postJSON = async (url, body) => {
  posted.push({ url, body });
  return { status: "ok" };
};
globalThis.__renderRelayQr = () => {};
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

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
  "const renderCloud = globalThis.__renderCloud; " +
  "const redrawCloudChart = globalThis.__redrawCloudChart;\n" + source;
const bootStart = source.lastIndexOf("\nrefresh().catch((error) => {");
if (bootStart < 0) throw new Error("crossover module boot call not found");
source = source.slice(0, bootStart).concat("\nexport { render };\n");
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { render } = await import(dataUrl);

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

function rowChildren() { return elements.get("crossover-action").children; }

// The review screen's real pair, as the server now mints it.
const DECLINE = {
  id: "review_decline",
  label: "Keep current sound",
  endpoint: "/correction/crossover/v2/decline",
  body: { expected_candidate_fingerprint: "fp-1" },
  href: "/correction/crossover/",
};
const ROOM = {
  id: "room",
  label: "Continue to Room correction",
  href: "/correction/room/",
};

render({
  verdict_text: "Review", steps: [], nudges: [], relay: null,
  next_action: null,
  alternate_actions: [DECLINE, ROOM],
});

const [decline, room] = rowChildren();

// --- (a) endpoint wins: the decision is a button, not a link --------------
check(decline.tag === "button", "(a) an action with an endpoint renders a button");
check(
  decline.textContent === "Keep current sound",
  "(a) it is the decline that rendered",
);
check(!decline.href, "(a) the presentation hint is not turned into an anchor href");

// --- (b) it actually posts, with the guard body ---------------------------
posted.length = 0;
await decline.click();
check(posted.length === 1, "(b) clicking the decline performs one request");
check(
  posted[0].url === "/correction/crossover/v2/decline",
  "(b) it posts to the endpoint the envelope named",
);
check(
  posted[0].body.expected_candidate_fingerprint === "fp-1",
  "(b) the candidate guard rides the body",
);

// --- (c) href-only stays a navigation -------------------------------------
// The other half of the rule. A cross-subsystem link has no endpoint that
// could perform it, and turning it into a dead button would be the mirror of
// the bug above.
check(room.tag === "a", "(c) an href-only action still renders an anchor");
check(
  room.href === "/correction/room/",
  "(c) and keeps pointing where the envelope said",
);

console.log(JSON.stringify({ ok: true, passed }));
