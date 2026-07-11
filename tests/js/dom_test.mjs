// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Unit test for the shared text-node DOM builder's appendChildren export.
//
// appendChildren is the flattening node-appender the /rooms bond-card summary
// depends on (main.js sync(): `appendChildren(currentSummary, summarize(g))`).
// summarize()'s unrecognised-role fallback returns a NESTED array
// (["Paired", [" — ", <strong>, " channel."]]); native Element.append()
// stringifies a nested array rather than descending into it, so the helper
// must recurse. This test pins (a) that appendChildren stays exported from
// dom.js — un-exporting it reintroduces the ReferenceError that threw on every
// bonded poll — and (b) that it flattens nested arrays into text/nodes.
//
// dom.js has no import-time side effects; appendChildren touches document/Node
// only at call time, so a tiny global stub before the call is enough — no
// browser, no JSDOM. Run via tests/test_web_rooms_setup.py.
import assert from "node:assert/strict";

// Minimal DOM stubs: a Node class for the `instanceof Node` branch, a
// createTextNode that tags its text, and a parent that records appends.
globalThis.Node = class Node {};
globalThis.document = {
  createTextNode(s) {
    return { __text: String(s) };
  },
};

const { appendChildren, h } = await import(
  "../../deploy/assets/shared/js/dom.js"
);

assert.equal(typeof appendChildren, "function", "dom.js must export appendChildren");

function makeParent() {
  return {
    kids: [],
    appendChild(c) {
      this.kids.push(c);
    },
  };
}

// A real child node (what h("strong", ...) returns): survives verbatim.
const strong = Object.assign(new globalThis.Node(), { tag: "strong" });

// The summarize() fallback shape: a nested array. Every leaf lands in order,
// strings become text nodes, the Node child passes through unwrapped.
{
  const el = makeParent();
  appendChildren(el, ["Paired", [" — ", strong, " channel."]]);
  assert.deepEqual(
    el.kids,
    [{ __text: "Paired" }, { __text: " — " }, strong, { __text: " channel." }],
    JSON.stringify(el.kids),
  );
}

// null / undefined / false leaves are skipped, not stringified.
{
  const el = makeParent();
  appendChildren(el, ["a", null, undefined, false, ["b"]]);
  assert.deepEqual(el.kids, [{ __text: "a" }, { __text: "b" }]);
}

// h() itself still composes children through the same flattening path.
assert.equal(typeof h, "function", "dom.js must export h");

console.log(JSON.stringify({ ok: true }));
