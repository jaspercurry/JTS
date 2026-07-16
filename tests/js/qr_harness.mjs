// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Exercises deploy/assets/shared/js/qr.js in isolation: the pure
// encodeQrMatrix() encoder (deterministic, structurally valid QR module
// matrices — no DOM needed) and the renderRelayQr() DOM wrapper (desktop vs
// collapsed-<details> narrow-viewport structure, the exact href reaching the
// rendered node, and the no-relay-link/empty-container case). The page
// integration points (correction/js/main.js's renderRelayCapture,
// crossover/js/main.js's renderRelay) are covered separately by
// tests/js/correction_render_harness.mjs and tests/js/crossover_stop_render_test.mjs
// via a spy — this file is the one place the encoder/renderer's own
// correctness is pinned.
//
//   node tests/js/qr_harness.mjs deploy/assets/shared/js/qr.js

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const modulePath = process.argv[2] || join(root, "deploy/assets/shared/js/qr.js");

// ---- minimal DOM stub — only what qr.js's renderRelayQr touches ----
// className and classList share one underlying Set (as real DOM does) so a
// later `el.className = '...'` string assignment fully replaces whatever
// classList.add() calls accumulated earlier — the same fidelity bug class
// tests/js/correction_render_harness.mjs's makeEl already guards against.
function makeClassList(values) {
  return {
    add(...names) { names.forEach((n) => values.add(n)); },
    remove(...names) { names.forEach((n) => values.delete(n)); },
    contains(n) { return values.has(n); },
    toString() { return [...values].join(" "); },
  };
}

function makeEl(tag) {
  const classValues = new Set();
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    _attrs: {},
    _innerHTML: "",
    textContent: "",
    width: 0,
    height: 0,
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
    },
    getContext(kind) {
      if (kind !== "2d") return null;
      return {
        fillStyle: "",
        fillRect() {},
      };
    },
  };
  el.classList = makeClassList(classValues);
  Object.defineProperty(el, "className", {
    get() { return el.classList.toString(); },
    set(v) {
      classValues.clear();
      String(v || "").split(" ").filter(Boolean).forEach((n) => classValues.add(n));
    },
    enumerable: true,
    configurable: true,
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return el._innerHTML; },
    set(v) {
      el._innerHTML = String(v == null ? "" : v);
      if (el._innerHTML === "") el.children = [];
    },
    enumerable: true,
    configurable: true,
  });
  return el;
}

globalThis.document = { createElement: (tag) => makeEl(tag) };

// ---- load the real module (strip `export` so one Function-eval exposes it,
// mirroring dialog_harness.mjs's technique — qr.js's exported surface is
// three plain function declarations, so this is a lossless transform) ----
const src = readFileSync(modulePath, "utf8").replace(/\bexport\s+/g, "");
const { encodeQrMatrix, renderRelayQr, isDesktopViewport } = new Function(
  src + "\nreturn { encodeQrMatrix, renderRelayQr, isDesktopViewport };",
)();

let failures = 0;
function fail(msg, context) {
  failures += 1;
  console.error(`FAIL: ${msg}`, context !== undefined ? JSON.stringify(context) : "");
}
function assert(cond, msg, context) {
  if (!cond) fail(msg, context);
}

const RELAY_URL =
  "https://capture.jasper.tech/#s=cap_AbC123-xyz&u=UPLOADTOKEN&k=BASE64URLKEY_-1234567890abcdefghij&a=MACVALUE";

// ---- encodeQrMatrix: pure function -----------------------------------

// 1. Deterministic: the same text always produces the same module matrix.
{
  const a = encodeQrMatrix(RELAY_URL);
  const b = encodeQrMatrix(RELAY_URL);
  assert(a.size === b.size, "encodeQrMatrix is deterministic in size", {
    a: a.size, b: b.size,
  });
  let identical = true;
  for (let r = 0; r < a.size && identical; r += 1) {
    for (let c = 0; c < a.size && identical; c += 1) {
      if (a.isDark(r, c) !== b.isDark(r, c)) identical = false;
    }
  }
  assert(identical, "encodeQrMatrix is deterministic in module content");
}

// 2. Structural invariant: every valid QR code has a 7x7 finder pattern
//    (solid dark ring, light interior-of-ring, solid dark 3x3 center) at
//    the top-left corner, with a light one-module separator around it —
//    true regardless of the encoded data. This is checkable without an
//    external decoder.
{
  const { size, isDark } = encodeQrMatrix(RELAY_URL);
  assert(size >= 21, "a real payload produces at least a version-1 (21x21) code", { size });
  let ringOk = true;
  for (let i = 0; i < 7; i += 1) {
    if (!isDark(0, i) || !isDark(6, i)) ringOk = false;
    if (!isDark(i, 0) || !isDark(i, 6)) ringOk = false;
  }
  assert(ringOk, "top-left finder pattern's outer ring is solid dark");
  let centerOk = true;
  for (let r = 2; r <= 4; r += 1) {
    for (let c = 2; c <= 4; c += 1) {
      if (!isDark(r, c)) centerOk = false;
    }
  }
  assert(centerOk, "top-left finder pattern's 3x3 center is solid dark");
  assert(!isDark(7, 0) && !isDark(0, 7), "the finder pattern has a light quiet separator", {
    row7col0: isDark(7, 0), row0col7: isDark(0, 7),
  });
  // Valid QR sizes are 21 + 4k for k = 0..39 (versions 1-40).
  assert((size - 21) % 4 === 0 && size >= 21 && size <= 177,
    "module count matches a valid QR version", { size });
}

// 3. Size scales with payload length (a longer relay URL needs a bigger
//    code / more error-correction capacity) — proves the input actually
//    drives the encoding, not a fixed-size stub.
{
  const short = encodeQrMatrix("https://capture.jasper.tech/#s=a");
  const long = encodeQrMatrix(RELAY_URL + "&extra=" + "x".repeat(150));
  assert(long.size > short.size,
    "a longer payload encodes to a larger module matrix",
    { shortSize: short.size, longSize: long.size });
}

// ---- renderRelayQr: DOM structure -------------------------------------

// 4. Desktop viewport (matchMedia matches: true): the QR renders directly —
//    canvas + caption — with the exact href stashed on the canvas.
{
  globalThis.window = { matchMedia: () => ({ matches: true }) };
  const container = makeEl("div");
  renderRelayQr(container, RELAY_URL);
  assert(container.children.length === 2 &&
      container.children[0].tagName === "CANVAS" &&
      container.children[1].tagName === "P",
    "desktop viewport renders canvas + caption directly",
    { tags: container.children.map((c) => c.tagName) });
  assert(container.children[0].getAttribute("data-qr-text") === RELAY_URL,
    "the rendered canvas is stamped with the EXACT href, fragment included",
    { got: container.children[0].getAttribute("data-qr-text") });
  assert(container.classList.contains("relay-qr") &&
      container.classList.contains("relay-qr--open"),
    "desktop container carries the open modifier class",
    { className: container.className });
  assert(container.children[1].textContent === "Scan with your phone's camera",
    "the caption uses the specified plain-language copy",
    { got: container.children[1].textContent });
}

// 5. Narrow (phone) viewport (matchMedia matches: false): the QR is tucked
//    inside a collapsed <details> — a structural difference from desktop,
//    not just a CSS one — since the tap link is already primary there.
{
  globalThis.window = { matchMedia: () => ({ matches: false }) };
  const container = makeEl("div");
  renderRelayQr(container, RELAY_URL);
  assert(container.children.length === 1 && container.children[0].tagName === "DETAILS",
    "narrow viewport wraps the QR in a single <details> element",
    { tags: container.children.map((c) => c.tagName) });
  const details = container.children[0];
  assert(details.children.length === 3 &&
      details.children[0].tagName === "SUMMARY" &&
      details.children[1].tagName === "CANVAS" &&
      details.children[2].tagName === "P",
    "the <details> holds summary, canvas, then caption in order",
    { tags: details.children.map((c) => c.tagName) });
  assert(details.children[0].textContent === "Show QR code",
    "the collapsed summary uses the specified label",
    { got: details.children[0].textContent });
  assert(details.children[1].getAttribute("data-qr-text") === RELAY_URL,
    "the collapsed canvas still encodes the exact href",
    { got: details.children[1].getAttribute("data-qr-text") });
  assert(container.classList.contains("relay-qr--collapsed"),
    "narrow container carries the collapsed modifier class",
    { className: container.className });
}

// 6. opts.desktop overrides matchMedia entirely — callers that already know
//    the viewport (or want to force a layout in a test) are not at the
//    mercy of window.matchMedia.
{
  globalThis.window = { matchMedia: () => ({ matches: false }) }; // narrow
  const container = makeEl("div");
  renderRelayQr(container, RELAY_URL, { desktop: true });
  assert(container.children[0] && container.children[0].tagName === "CANVAS",
    "opts.desktop: true forces the open layout regardless of matchMedia");

  globalThis.window = { matchMedia: () => ({ matches: true }) }; // desktop
  const container2 = makeEl("div");
  renderRelayQr(container2, RELAY_URL, { desktop: false });
  assert(container2.children[0] && container2.children[0].tagName === "DETAILS",
    "opts.desktop: false forces the collapsed layout regardless of matchMedia");
}

// 7. No relay link (falsy text): the container is cleared and left empty —
//    no QR node at all, matching the pre-link and post-relay states.
{
  globalThis.window = { matchMedia: () => ({ matches: true }) };
  const container = makeEl("div");
  renderRelayQr(container, RELAY_URL);
  assert(container.children.length > 0, "sanity: a real href does render something first");

  for (const empty of [null, undefined, ""]) {
    renderRelayQr(container, empty);
    assert(container.children.length === 0,
      "a falsy href clears the container to no QR node", { empty, got: container.children.length });
    assert(container.className === "relay-qr",
      "a cleared container keeps only the base class, no open/collapsed modifier",
      { got: container.className });
  }
}

// 8. isDesktopViewport(): no window (or no matchMedia) defaults to the
//    prominent desktop layout — the safer default when the viewport signal
//    is unavailable — and otherwise reflects window.matchMedia verbatim.
{
  globalThis.window = { matchMedia: () => ({ matches: false }) };
  assert(isDesktopViewport() === false, "isDesktopViewport reflects matches: false");
  globalThis.window = { matchMedia: () => ({ matches: true }) };
  assert(isDesktopViewport() === true, "isDesktopViewport reflects matches: true");
  globalThis.window = {};
  assert(isDesktopViewport() === true,
    "no matchMedia function defaults to desktop (the safer unknown-viewport default)");
}

if (failures) {
  console.error(`\n${failures} qr.js test failure(s).`);
  process.exit(1);
}
console.log(JSON.stringify({ ok: true, tests: 8 }));
