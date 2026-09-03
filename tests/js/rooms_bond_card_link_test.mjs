// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Issue #1842: the bond card's balance block used to render an
// `https://<hostname>/balance/` link ("Balance automatically with a
// microphone") — a capture design ADR-0188 parked. On the self-signed origin
// that link fails hard (ERR_CERT_AUTHORITY_INVALID). This pins that the card
// never builds an anchor at all: only the manual slider/reset controls.
//
// Loads main.js as a real ES module (dom.js/grouping-view.js/
// pair-balance-controller.js run for real; only the network-touching
// imports — http.js, dialog.js, local-web-host.js — are stubbed, since
// building the page never calls them) and inspects the actual DOM tree
// buildPage() produces, truncated before the self-scheduling poll() so the
// module never reaches for the network.
//
//   node tests/js/rooms_bond_card_link_test.mjs

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { loadEsm, repoPath } from "./_loader.mjs";

// A DOM stub minimal enough for the real dom.js h()/svg()/appendChildren to
// build against: appendChild, setAttribute/getAttribute, a style object,
// and an el that satisfies `instanceof Node`.
class Style {
  setProperty(name, value) { this[name] = value; }
}
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.style = new Style();
    this.dataset = {};
    this.className = "";
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) {
    return Object.prototype.hasOwnProperty.call(this.attributes, key)
      ? this.attributes[key] : null;
  }
  addEventListener() {}
}

const appRoot = new El("div");
globalThis.Node = El;
globalThis.location = { hostname: "test-speaker.local" };
globalThis.document = {
  createElement: (tag) => new El(tag),
  createElementNS: (_ns, tag) => new El(tag),
  createTextNode: (text) => Object.assign(new El("#text"), { textContent: String(text) }),
  getElementById: (id) => (id === "app" ? appRoot : null),
};

const domUrl = pathToFileURL(repoPath("deploy/assets/shared/js/dom.js")).href;
const pbcUrl = pathToFileURL(repoPath("deploy/assets/rooms/js/pair-balance-controller.js")).href;
const groupingUrl = pathToFileURL(repoPath("deploy/assets/rooms/js/grouping-view.js")).href;

const { refs } = await loadEsm(repoPath("deploy/assets/rooms/js/main.js"), {
  rewrite: [
    [/^import \{ getJSON, postJSON \} from "\/assets\/shared\/js\/http\.js";\n/m, ""],
    [/^import \{ jtsConfirm \} from "\/assets\/shared\/js\/dialog\.js";\n/m, ""],
    [/^import \{ localWebHost \} from "\/assets\/shared\/js\/local-web-host\.js";\n/m, ""],
    [/"\/assets\/shared\/js\/dom\.js"/, `"${domUrl}"`],
    [/"\.\/pair-balance-controller\.js"/, `"${pbcUrl}"`],
    [/"\.\/grouping-view\.js"/, `"${groupingUrl}"`],
  ],
  prelude:
    "const getJSON = async () => ({});\n" +
    "const postJSON = async () => ({});\n" +
    "const jtsConfirm = async () => true;\n" +
    "const localWebHost = () => '';\n",
  // buildPage() (and its refs const) runs at module top level, well before
  // the self-scheduling poll() call at EOF — cut there so the module never
  // touches the network.
  truncateBefore: "\npoll();",
  exportNames: ["refs"],
});

function collectAnchors(node, out) {
  if (!node || !Array.isArray(node.children)) return out;
  if (node.tagName === "a") out.push(node);
  for (const child of node.children) collectAnchors(child, out);
  return out;
}

const anchors = collectAnchors(refs.bondCard.el, []);
assert.deepEqual(anchors, [], "bond card must render no <a> elements");

console.log(JSON.stringify({ ok: true }));
