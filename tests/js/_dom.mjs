// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Shared DOM stubs for tests/js. The crossover_*_test.mjs harnesses each
// hand-rolled the same fixed-id element map + document shim; this is that
// shape, generalized enough to cover every variant seen across them (an
// `addEventListener` that tracks listeners so `.click()` can replay them,
// `href`/`tag` fields some but not all of them touch). A harness that never
// calls `.click()` or reads `.href` is unaffected by their presence — only
// ELEMENT ABSENCE would change behavior here, and nothing is removed.
//
// Two call sites need a genuinely different element shape rather than a
// superset of this one (making `textContent` a live view over `children`
// instead of a plain field would change what already-passing assertions
// observe) — `elementWithLiveText` and `FakeElement` below serve those.
//
// Not a test suite: see _loader.mjs's header for why the underscore prefix
// is sufficient on its own here, without also needing this file's name to
// dodge any glob.

// The 19 ids every crossover_*_test.mjs harness looks up via
// document.getElementById, in the crossover screen's own render order. Not
// every consumer needs the same set — some add cloud/legend ids, one drops
// "crossover-applied" — so this is a starting point files splice, not a
// contract every file must use verbatim.
export const CROSSOVER_IDS = [
  "crossover-verdict",
  "crossover-applied",
  "crossover-start-over",
  "crossover-steps",
  "crossover-nudges",
  "crossover-review",
  "crossover-review-body",
  "crossover-action",
  "crossover-capture",
  "crossover-walk",
  "crossover-walk-progress",
  "crossover-walk-headline",
  "crossover-walk-detail",
  "crossover-walk-action",
  "crossover-capture-status",
  "crossover-capture-stop",
  "capture-status",
];

// The plain-field element stub: classList (real add/remove/contains/toggle
// semantics over a Set), a tracked-listener addEventListener + click() that
// replays them, and a textContent that is just a data property (setting it
// does NOT clear `children` — see elementWithLiveText for the harnesses
// that need that real-DOM behavior instead).
export function element(id = "") {
  const listeners = {};
  const values = new Set();
  return {
    id,
    tag: id,
    children: [],
    classList: {
      add(...names) { names.forEach((name) => values.add(name)); },
      remove(...names) { names.forEach((name) => values.delete(name)); },
      contains(name) { return values.has(name); },
      toggle(name, force) { if (force) values.add(name); else values.delete(name); },
    },
    dataset: {},
    disabled: false,
    hidden: false,
    className: "",
    textContent: "",
    href: "",
    addEventListener(event, fn) {
      (listeners[event] = listeners[event] || []).push(fn);
    },
    click() {
      let result;
      for (const fn of listeners.click || []) result = fn();
      return result;
    },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(key, value) { this[key] = String(value); },
  };
}

// A `textContent` that behaves like the real DOM: reading it joins the
// children's own textContent when there are any, otherwise returns the last
// explicitly-set string; setting it clears `children`. Load-bearing for the
// two harnesses that pin a stale control being cleared by a later plain
// setStatus() call — a stub that merely stored a string would let that
// stale control pass unnoticed.
export function elementWithLiveText(id = "") {
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

// Installs `globalThis.document` as a fixed-id lookup table built from
// `factory` (default: `element`) and returns the backing Map so the caller
// can `.get(id)` directly. `extra` is spread last, so it can override
// `addEventListener`/`createElement`/add `createTextNode` etc. per harness.
export function installFixedDocument(ids, { factory = element, ...extra } = {}) {
  const elements = new Map(ids.map((id) => [id, factory(id)]));
  globalThis.document = {
    visibilityState: "visible",
    addEventListener() {},
    createElement: (tag) => factory(tag),
    getElementById: (id) => elements.get(id),
    ...extra,
  };
  return elements;
}

// A createElement-per-call double (no fixed id map): every call returns a
// FRESH node, matching modules that build markup via repeated
// document.createElement + append rather than looking up static ids. Real
// DOM semantics for textContent (setting it clears children, as above).
export class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.className = "";
    this._textContent = "";
    this.children = [];
    this.hidden = false;
    this.dataset = {};
    this.attributes = {};
  }
  get textContent() { return this._textContent; }
  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener() {}
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name] : null;
  }
}
