// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the capture page's fixed DATA renderer (build step 3).
//
// The load-bearing security claim (docs/phone-mic-relay-plan.md §8, §15): the
// page renders the Pi-supplied UI as DATA ONLY. A spec arriving across the
// untrusted relay that contains <script> / onerror= / javascript: / a hostile
// component type / raw CSS is rendered INERT — never executed. This harness
// proves it against a faithful DOM stub (no JSDOM needed). Prints {"ok":true}.
//
//   node tests/js/capture_render_test.mjs

import assert from "node:assert/strict";

import {
  acceptedAcknowledgement,
  renderScreen,
} from "../../capture-page/js/render.js";
import { THEME_ACCENT_VARS, DEFAULT_THEME } from "../../capture-page/js/theme.js";

let passed = 0;
function ok() {
  passed += 1;
}

// ---- Faithful-enough DOM stub -----------------------------------------------
// Builds a real element tree. Critically: setting textContent stores a string
// and never parses markup (mirrors the browser), and any use of innerHTML is
// recorded so the test can assert the renderer never reached for it.

function makeDoc() {
  const innerHTMLWrites = [];
  const created = [];
  function makeEl(tag) {
    const node = {
      tagName: String(tag).toUpperCase(),
      className: "",
      type: "",
      _attrs: {},
      children: [],
      _listeners: {},
      style: {
        _props: {},
        setProperty(k, v) {
          this._props[k] = v;
        },
      },
      get firstChild() {
        return this.children[0] || null;
      },
      get ownerDocument() {
        return doc;
      },
      appendChild(child) {
        this.children.push(child);
        return child;
      },
      removeChild(child) {
        const i = this.children.indexOf(child);
        if (i >= 0) this.children.splice(i, 1);
        return child;
      },
      setAttribute(k, v) {
        this._attrs[String(k)] = String(v);
      },
      getAttribute(k) {
        return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
      },
      addEventListener(ev, fn) {
        (this._listeners[ev] = this._listeners[ev] || []).push(fn);
      },
      dispatch(ev) {
        if (node.disabled && ev === "click") return;
        for (const fn of this._listeners[ev] || []) fn({ preventDefault() {}, target: node });
      },
    };
    let text = "";
    Object.defineProperty(node, "textContent", {
      get() {
        return text;
      },
      set(v) {
        text = String(v);
        node.children.length = 0; // setting text clears children (real DOM)
      },
    });
    Object.defineProperty(node, "innerHTML", {
      get() {
        return "";
      },
      set(v) {
        innerHTMLWrites.push(v);
      },
    });
    created.push(node);
    return node;
  }
  const doc = {
    createElement: (t) => makeEl(t),
    _innerHTMLWrites: innerHTMLWrites,
    _created: created,
  };
  return doc;
}

function walk(node, fn) {
  fn(node);
  for (const child of node.children || []) walk(child, fn);
}

// ============================================================================

function testHostilePayloadIsInert() {
  const doc = makeDoc();
  const root = doc.createElement("div");
  const malicious = {
    ui: {
      theme: { accent: "red; } body { background: url(//evil) }", font: "evil" },
      screen: [
        { type: "heading", text: "<script>globalThis.__pwned = 1</script>" },
        { type: "note", text: "<img src=x onerror=globalThis.__pwned=2>" },
        { type: "steps", items: ["<b>bold</b>", "javascript:alert(2)"] },
        { type: "button", label: "<svg/onload=alert(3)>", action: "begin_capture" },
        // Hostile component types — must render NOTHING:
        { type: "iframe", src: "javascript:alert(4)" },
        { type: "script", text: "globalThis.__pwned = 3" },
        { type: "img", onerror: "globalThis.__pwned = 4" },
        { type: "object", data: "evil" },
      ],
    },
  };

  renderScreen(root, malicious, { doc, handlers: {} });

  // No dangerous element was ever created — the spec cannot choose the tag.
  const banned = new Set([
    "SCRIPT",
    "IFRAME",
    "IMG",
    "OBJECT",
    "EMBED",
    "SVG",
    "LINK",
    "META",
    "BASE",
    "STYLE",
  ]);
  for (const node of doc._created) {
    assert.ok(!banned.has(node.tagName), `must never create <${node.tagName}>`);
  }

  // No event-handler attribute and no javascript: URL reached the DOM.
  walk(root, (node) => {
    for (const [name, value] of Object.entries(node._attrs || {})) {
      assert.ok(!/^on/i.test(name), `no on* attribute (${name})`);
      assert.ok(!/javascript:/i.test(value), `no javascript: in attr ${name}`);
    }
  });

  // innerHTML was never used.
  assert.deepEqual(doc._innerHTMLWrites, [], "innerHTML never assigned");

  // The hostile types rendered nothing; only the 4 known components survived.
  assert.equal(root.children.length, 4, "only known component types rendered");

  // The script-looking text is carried as INERT text, verbatim.
  const heading = root.children[0];
  assert.equal(heading.tagName, "H1");
  assert.equal(
    heading.textContent,
    "<script>globalThis.__pwned = 1</script>",
    "script text is inert textContent, not parsed",
  );

  // The "javascript:" step item is inert text in an <li>.
  const steps = root.children[2];
  assert.equal(steps.tagName, "OL");
  assert.equal(steps.children[1].textContent, "javascript:alert(2)");

  // Nothing executed.
  assert.equal(globalThis.__pwned, undefined, "no payload executed");
  ok();
}

function testRawCssThemeRejected() {
  const doc = makeDoc();
  const root = doc.createElement("div");
  renderScreen(
    root,
    { ui: { theme: { accent: "red; } body{}", font: "../../evil" }, screen: [] } },
    { doc },
  );
  // A non-allowlisted accent token falls back to the DEFAULT fixed CSS value —
  // never the attacker's raw CSS.
  assert.equal(root.style._props["--cap-accent"], THEME_ACCENT_VARS[DEFAULT_THEME.accent]);
  assert.ok(!/red;|body/.test(root.style._props["--cap-accent"]), "no raw CSS injected");
  ok();
}

function testHappyPathStructure() {
  const doc = makeDoc();
  const root = doc.createElement("div");
  let begun = 0;
  const spec = {
    ui: {
      theme: { accent: "sage", font: "figtree" },
      screen: [
        { type: "heading", text: "Room measurement — position 2 of 5" },
        { type: "steps", items: ["Stand at the couch", "Hold it up", "Stay quiet"] },
        { type: "level_meter", source: "mic" },
        { type: "button", label: "Start", action: "begin_capture" },
        { type: "note", text: "Keep the screen on." },
      ],
    },
  };
  const refs = renderScreen(root, spec, {
    doc,
    handlers: { begin_capture: () => (begun += 1) },
  });

  assert.equal(root.children.map((c) => c.tagName).join(","), "H1,OL,DIV,BUTTON,P");
  assert.equal(root.children[1].children.length, 3, "three step items");
  assert.equal(refs.levelMeters.length, 1, "one level meter ref");
  assert.equal(refs.buttons.length, 1);
  const btn = refs.buttons[0];
  assert.equal(btn.action, "begin_capture");
  assert.equal(btn.el.getAttribute("data-action"), "begin_capture");
  // Host-mediated: the spec selected the action; the page bound the function.
  btn.el.dispatch("click");
  assert.equal(begun, 1, "host-provided handler fired");
  ok();
}

function testUnknownButtonActionNotWired() {
  const doc = makeDoc();
  const root = doc.createElement("div");
  let called = 0;
  renderScreen(
    root,
    { ui: { screen: [{ type: "button", label: "Go", action: "exfiltrate" }] } },
    { doc, handlers: { exfiltrate: () => (called += 1), begin_capture: () => {} } },
  );
  const btn = root.children[0];
  assert.equal(btn.getAttribute("data-action"), null, "non-allowlisted action not set");
  btn.dispatch("click");
  assert.equal(called, 0, "non-allowlisted action never wired to a handler");
  ok();
}

function testAcknowledgementGatesStartAndRendersInertText() {
  const doc = makeDoc();
  const root = doc.createElement("div");
  let begun = 0;
  const spec = {
    acknowledgement: {
      schema_version: 1,
      id: "driver_same_distance_v1",
      binding_id: "binding_abcdefghijklmnop",
      label: "<img onerror=alert(1)> Mic is 3 cm from the woofer",
    },
    ui: {
      screen: [
        { type: "heading", text: "Measure the woofer" },
        { type: "steps", items: ["Position the microphone", "Keep it still"] },
        { type: "level_meter", id: "capture_level" },
        { type: "button", label: "Start", action: "begin_capture" },
      ],
    },
  };
  const refs = renderScreen(root, spec, {
    doc,
    handlers: { begin_capture: () => (begun += 1) },
  });

  assert.ok(refs.acknowledgement, "acknowledgement ref returned");
  assert.equal(
    root.children.map((child) => child.tagName).join(","),
    "H1,OL,DIV,LABEL,BUTTON",
    "placement acknowledgement is the final instruction immediately before Start",
  );
  const label = root.children[3];
  assert.equal(label.tagName, "LABEL");
  assert.equal(
    label.children[1].textContent,
    "<img onerror=alert(1)> Mic is 3 cm from the woofer",
  );
  assert.deepEqual(doc._innerHTMLWrites, [], "ack label remains inert text");
  const start = refs.buttons[0].el;
  assert.equal(start.disabled, true, "Start is initially disabled");
  assert.throws(
    () => acceptedAcknowledgement(spec, refs),
    /confirm the microphone placement/,
  );
  start.dispatch("click");
  assert.equal(begun, 0, "disabled Start does not arm in a real browser");
  refs.acknowledgement.el.checked = true;
  refs.acknowledgement.el.dispatch("change");
  assert.equal(start.disabled, false, "checking acknowledgement enables Start");
  assert.deepEqual(acceptedAcknowledgement(spec, refs), {
    schema_version: 1,
    id: "driver_same_distance_v1",
    binding_id: "binding_abcdefghijklmnop",
    accepted: true,
  });
  start.dispatch("click");
  assert.equal(begun, 1);
  ok();
}

const tests = [
  testHostilePayloadIsInert,
  testRawCssThemeRejected,
  testHappyPathStructure,
  testUnknownButtonActionNotWired,
  testAcknowledgementGatesStartAndRendersInertText,
];

let failure = null;
for (const t of tests) {
  try {
    t();
  } catch (e) {
    failure = { test: t.name, error: String(e && e.stack ? e.stack : e) };
    break;
  }
}

if (failure) {
  console.error(failure.error);
  console.log(JSON.stringify({ ok: false, ...failure }));
  process.exit(1);
} else {
  console.log(JSON.stringify({ ok: true, passed }));
}
