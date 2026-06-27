// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Render harness for /correction/ banner rendering (C4a-2).
//
// Exercises renderCurrentCorrection (deploy/assets/correction/js/main.js,
// ~line 726) with representative backend payloads and asserts the correct
// CSS class, reset-button visibility, and label copy for each correction kind.
//
// The function is an IIFE-local var; the harness injects a probe hook
// (`globalThis.__probe`) just before the IIFE runs so the test can call the
// function directly.  DOM elements are lightweight stubs — no browser, no
// JSDOM needed.
//
//   node tests/js/correction_render_harness.mjs deploy/assets/correction/js/main.js

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const modulePath = process.argv[2] || join(root, "deploy/assets/correction/js/main.js");
let rawSource = readFileSync(modulePath, "utf8");

// ---- classList stub ----
function makeClassList(initial) {
  const values = new Set(initial ? initial.split(" ").filter(Boolean) : []);
  return {
    _values: values,
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
    contains(name) { return values.has(name); },
    toggle(name, force) {
      if (force === undefined) {
        if (values.has(name)) values.delete(name); else values.add(name);
      } else if (force) { values.add(name); } else { values.delete(name); }
    },
    get size() { return values.size; },
    toString() { return [...values].join(" "); },
  };
}

// ---- DOM element stub ----
function makeEl(id) {
  const el = {
    id,
    textContent: "",
    innerHTML: "",
    className: "",
    value: "",
    checked: false,
    disabled: false,
    attrs: {},
    style: {},
    _listeners: {},
    classList: makeClassList(),
    options: [],
    selectedIndex: 0,
    addEventListener(ev, fn) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
    },
    removeEventListener() {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs.hasOwnProperty(k) ? this.attrs[k] : null; },
    hasAttribute(k) { return this.attrs.hasOwnProperty(k); },
    removeAttribute(k) { delete this.attrs[k]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    focus() {},
    click() {
      for (const fn of this._listeners.click || []) fn({ preventDefault() {}, target: this });
    },
    // canvas stub (used by the chart functions)
    getContext() {
      return {
        clearRect() {}, fillRect() {}, strokeRect() {},
        beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
        fillText() {}, measureText() { return { width: 0 }; },
        save() {}, restore() {}, scale() {}, translate() {},
        arc() {}, fill() {}, clip() {}, setLineDash() {},
        createLinearGradient() {
          return { addColorStop() {} };
        },
        canvas: { width: 600, height: 200 },
      };
    },
    // form element
    submit() {},
  };
  el.classList = makeClassList();
  return el;
}

// Registry of DOM elements the module looks up
const elements = new Map();
function getOrMake(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

// ---- Minimal globals the IIFE needs ----
const globalFetch = (_url) => Promise.resolve({
  ok: true,
  status: 200,
  async json() { return {}; },
  async text() { return ""; },
});

// AudioContext stub (mic capture path — not exercised by render tests)
class FakeAudioContext {
  constructor() { this.state = "running"; this.sampleRate = 48000; }
  createMediaStreamSource() { return { connect() {} }; }
  createAnalyser() { return { fftSize: 0, frequencyBinCount: 0, getByteTimeDomainData() {} }; }
  createGain() { return { gain: { value: 1 }, connect() {}, disconnect() {} }; }
  createMediaStreamDestination() { return { stream: {} }; }
  close() { return Promise.resolve(); }
  async resume() {}
}

// ---- Strip imports, stub calls that would fail in Node ----
let source = rawSource
  // Strip ES module imports (the IIFE body uses them via injected closures below)
  .replace(/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n/gm, "")
  // Stub getUserMedia / navigator / MediaRecorder — not exercised in render tests
  .replace(
    /navigator\.mediaDevices\.getUserMedia\b/g,
    "(() => Promise.reject(new Error('no media in harness')))",
  )
  // Stub AudioWorklet loading
  .replace(
    /audioCtx\.audioWorklet\.addModule\b/g,
    "(() => Promise.resolve())",
  )
  // Suppress console.error during boot (network calls fire and fail)
  ;

// Inject a probe hook just before the IIFE closes so tests can call the function
// directly.  The hook is a function expression assigned to a global, set from
// inside the IIFE closure so it shares the DOM-variable bindings.
source = source.replace(
  /\}\)\(\);\s*$/,
  `  globalThis.__testProbe = { renderCurrentCorrection, correctionBannerClass };
})();`,
);

if (/^import\s/m.test(source)) {
  throw new Error(
    "unhandled import in main.js — add a strip rule to the correction_render_harness",
  );
}

// ---- Build the eval context ----
const docEl = makeEl("document");
const fakeDocument = {
  getElementById(id) { return getOrMake(id); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  removeEventListener() {},
  hidden: false,
  activeElement: null,
  body: makeEl("body"),
  createElement() { return makeEl("anon"); },
};
const fakeWindow = {
  addEventListener() {},
  removeEventListener() {},
  location: { href: "http://jts.local/correction/" },
  navigator: { mediaDevices: { getUserMedia() { return Promise.reject(new Error("no media")); }, enumerateDevices() { return Promise.resolve([]); } } },
  AudioContext: FakeAudioContext,
  MediaRecorder: undefined,
  URL: { createObjectURL() { return "blob:fake"; } },
  requestAnimationFrame(fn) { setTimeout(fn, 0); return 1; },
  cancelAnimationFrame() {},
};

// Inject stubs for the named imports (csrfHeaders, jsonHeaders, etc.)
const preamble = `
const csrfHeaders = () => ({ 'X-CSRF-Token': 'harness', 'Content-Type': 'application/json' });
const jsonHeaders = () => ({ 'X-CSRF-Token': 'harness', 'Content-Type': 'application/json' });
async function jtsConfirm() { return true; }
async function jtsAlert() {}
function escapeText(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
`;

// Evaluate using the Function constructor so DOM globals are in scope
const runner = new Function(
  "document", "window", "fetch", "globalThis", "console",
  "setTimeout", "clearTimeout", "setInterval", "clearInterval",
  "AudioContext", "URL",
  `${preamble}\n${source}`,
);

const safeConsole = {
  log() {},
  warn() {},
  error() {},
  info() {},
};

runner(
  fakeDocument,
  fakeWindow,
  globalFetch,
  globalThis,
  safeConsole,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  FakeAudioContext,
  { createObjectURL() { return "blob:fake"; } },
);

const { renderCurrentCorrection, correctionBannerClass } = globalThis.__testProbe;
delete globalThis.__testProbe;

// ---- Test helpers ----
let failures = 0;
function fail(msg, context) {
  failures += 1;
  console.error(`FAIL: ${msg}`, context ? JSON.stringify(context) : "");
}
function assert(cond, msg, context) {
  if (!cond) fail(msg, context);
}

// Convenience accessors into our stub DOM elements
function banner() { return elements.get("current-correction"); }
function label() { return elements.get("current-correction-label"); }
function resetBtn() { return elements.get("current-correction-reset"); }

// ---- Tests ----

// 1. Applied correction (cc has applied_at_epoch)
// Expect: className='applied', label contains PEQ count, reset button visible
{
  renderCurrentCorrection(
    { applied_at_epoch: 1718000000, peq_count: 5 },
    null,
  );
  assert(banner().className === "applied",
    "applied correction must set banner class to 'applied'",
    { got: banner().className });
  assert(label().textContent.includes("5 PEQ filters"),
    "applied label must mention PEQ filter count",
    { got: label().textContent });
  assert(!resetBtn().classList.contains("hidden"),
    "applied correction must show reset button");
}

// 2. Applied with 1 filter (singular noun)
{
  renderCurrentCorrection({ applied_at_epoch: 1718000001, peq_count: 1 }, null);
  assert(label().textContent.includes("1 PEQ filter"),
    "single filter should use singular 'filter'",
    { got: label().textContent });
  assert(!label().textContent.includes("filters"),
    "single filter must not use plural 'filters'",
    { got: label().textContent });
}

// 3. Applied with 0 filters
{
  renderCurrentCorrection({ applied_at_epoch: 1718000002, peq_count: 0 }, null);
  assert(label().textContent.includes("0 PEQ filters"),
    "zero filters should use plural 'filters'",
    { got: label().textContent });
}

// 4. flat kind (no correction applied)
// Expect: banner class 'flat', no reset button, fallback copy
{
  renderCurrentCorrection(null, { kind: "flat", message: "Flat — no correction.", label: null });
  assert(banner().className === "flat",
    "flat config must set banner class to 'flat'",
    { got: banner().className });
  assert(label().textContent === "Flat — no correction.",
    "flat config must render backend message verbatim",
    { got: label().textContent });
  assert(resetBtn().classList.contains("hidden"),
    "flat config must hide reset button");
}

// 5. custom kind
// Expect: banner class 'custom', reset button visible
{
  renderCurrentCorrection(null, { kind: "custom", message: "Custom DSP loaded.", label: null });
  assert(banner().className === "custom",
    "custom kind must set banner class to 'custom'",
    { got: banner().className });
  assert(label().textContent === "Custom DSP loaded.",
    "custom kind must render backend message",
    { got: label().textContent });
  assert(!resetBtn().classList.contains("hidden"),
    "custom kind must show reset button (offers reset to flat baseline)");
}

// 6. unknown kind (treated same as custom for the banner class, no reset offered for
//    kinds the UI doesn't recognise beyond 'custom' — correctionBannerClass returns
//    'custom' but the reset logic only shows the button for kind === 'custom')
{
  renderCurrentCorrection(null, { kind: "unknown", message: "Unknown state.", label: null });
  assert(banner().className === "custom",
    "unknown kind must resolve to banner class 'custom' via correctionBannerClass",
    { got: banner().className });
  assert(resetBtn().classList.contains("hidden"),
    "unknown kind must hide reset button (only 'custom' offers reset)");
}

// 7. preference kind (managed — no reset offered)
{
  renderCurrentCorrection(null, { kind: "preference", message: "Preference EQ active.", label: null });
  assert(banner().className === "flat",
    "preference kind maps to banner class 'flat'",
    { got: banner().className });
  assert(resetBtn().classList.contains("hidden"),
    "preference kind must hide reset button");
}

// 8. measurement kind (managed)
{
  renderCurrentCorrection(null, { kind: "measurement", label: "Room measurement applied" });
  assert(banner().className === "flat",
    "measurement kind maps to banner class 'flat'",
    { got: banner().className });
  assert(label().textContent === "Room measurement applied",
    "measurement kind falls back to label field when message is absent",
    { got: label().textContent });
}

// 9. no config at all (null/null) → fallback copy
{
  renderCurrentCorrection(null, null);
  assert(banner().className === "flat",
    "null config must set banner class to 'flat'",
    { got: banner().className });
  assert(
    label().textContent === "No correction applied — speaker is flat.",
    "null config must show fallback copy",
    { got: label().textContent });
  assert(resetBtn().classList.contains("hidden"),
    "null config must hide reset button");
}

// 10. cc supplied but no applied_at_epoch (e.g. in-progress session descriptor)
//     → falls through to config path
{
  renderCurrentCorrection(
    { peq_count: 3 },  // no applied_at_epoch
    { kind: "flat", message: "Not yet applied." },
  );
  assert(banner().className === "flat",
    "cc without applied_at_epoch must fall through to config path",
    { got: banner().className });
  assert(label().textContent === "Not yet applied.",
    "cc without applied_at_epoch must render config message",
    { got: label().textContent });
}

// 11. correctionBannerClass: verify the pure mapping independently
{
  assert(correctionBannerClass("custom") === "custom",   "custom → 'custom'");
  assert(correctionBannerClass("unknown") === "custom",  "unknown → 'custom'");
  assert(correctionBannerClass("flat") === "flat",       "flat → 'flat'");
  assert(correctionBannerClass("preference") === "flat", "preference → 'flat'");
  assert(correctionBannerClass("measurement") === "flat","measurement → 'flat'");
  assert(correctionBannerClass("active_speaker") === "flat", "active_speaker → 'flat'");
  assert(correctionBannerClass("") === "flat",           "empty → 'flat'");
}

if (failures) {
  console.error(`\n${failures} correction render test failure(s).`);
  process.exit(1);
}
console.log(JSON.stringify({ ok: true, tests: 11 }));
