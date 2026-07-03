// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Render harness for /correction/ rendering (C4a-2 + P3a).
//
// Exercises renderCurrentCorrection (deploy/assets/correction/js/main.js,
// ~line 726) with representative backend payloads and asserts the correct
// CSS class, reset-button visibility, and label copy for each correction kind.
// Also pins the P3a honest measured before/after surfaces:
//   - verifyHeadlineHtml: verb/colour choice from the server delta, the
//     ±0.1 dB display deadband, the neutral bucket, band text from the
//     server payload, and ''-on-missing-fields.
//   - drawBeforeAfterFill (via drawChart + a recording canvas context):
//     per-segment polygon vertex counts, improved→green / regressed→amber
//     fill colours, and that server-classified tones are consumed
//     verbatim (the client never re-derives improvement from the curves).
//
// The functions are IIFE-local; the harness injects a probe hook
// (`globalThis.__testProbe`) just before the IIFE closes so the test can
// call them directly.  DOM elements are lightweight stubs — no browser, no
// JSDOM needed.
//
//   node tests/js/correction_render_harness.mjs deploy/assets/correction/js/main.js

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
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
    // Layout stub — drawChart bails on a 0×0 canvas, so report real
    // dimensions for the chart-rendering tests.
    getBoundingClientRect() {
      return { width: 600, height: 200, top: 0, left: 0, right: 600, bottom: 200 };
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
  `  globalThis.__testProbe = {
    renderCurrentCorrection,
    correctionBannerClass,
    verifyHeadlineHtml,
    drawChart,
    // lastVerify is IIFE-local state read by drawChart's before/after
    // fill; expose a setter that shares the closure binding.
    setLastVerify: function (v) { lastVerify = v; },
  };
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
  isSecureContext: true,
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

const {
  renderCurrentCorrection,
  correctionBannerClass,
  verifyHeadlineHtml,
  drawChart,
  setLastVerify,
} = globalThis.__testProbe;
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

// ---- P3a: honest measured before/after pins --------------------------------

// Helper: a complete server-shaped verify_before_after payload.
function makeBA(deltaRms, extra) {
  return Object.assign({
    band_hz: [50, 350],
    before: { rms_db: 6.2, max_db: 11.0, n_points: 120 },
    after: { rms_db: 2.1, max_db: 4.0, n_points: 120 },
    delta: { rms_db: deltaRms, max_db: 7.0 },
    fill_segments: [],
  }, extra || {});
}

// 12. verifyHeadlineHtml: honest "better" verb + measured numbers + band
//     text derived from the SERVER payload's band_hz (never hard-coded).
{
  const html = verifyHeadlineHtml(makeBA(4.1));
  assert(html.includes('verify-headline improved'),
    "positive measured delta must use the improved tone class", { got: html });
  assert(html.includes('Bass evened out'),
    "positive measured delta must use the 'evened out' verb", { got: html });
  assert(html.includes('±6.2 dB → ±2.1 dB'),
    "headline must show the server before → after RMS values", { got: html });
  assert(html.includes('50–350 Hz'),
    "band text must come from the payload band_hz", { got: html });

  const wideBand = verifyHeadlineHtml(makeBA(4.1, { band_hz: [50, 500] }));
  assert(wideBand.includes('50–500 Hz'),
    "a non-default server band_hz must flow into the band text",
    { got: wideBand });
}

// 13. verifyHeadlineHtml: honest "worse" verb — a regression is named,
//     never dressed up as improvement.
{
  const html = verifyHeadlineHtml(makeBA(-3.0));
  assert(html.includes('verify-headline regressed'),
    "negative measured delta must use the regressed tone class", { got: html });
  assert(html.includes('Bass deviation grew'),
    "negative measured delta must use the 'deviation grew' verb", { got: html });
  assert(!html.includes('evened out'),
    "a regression must not claim the bass evened out", { got: html });
}

// 14. verifyHeadlineHtml: ±0.1 dB display deadband, both directions.
//     |delta| <= 0.1 reads neutral ("held about the same"); just past the
//     deadband flips to the directional verb. Strict comparison: exactly
//     ±0.1 is still neutral.
{
  for (const delta of [0.0, 0.1, -0.1, 0.05, -0.05]) {
    const html = verifyHeadlineHtml(makeBA(delta));
    assert(html.includes('verify-headline neutral'),
      `delta ${delta} is inside the ±0.1 dB deadband → neutral class`,
      { got: html });
    assert(html.includes('Bass held about the same'),
      `delta ${delta} must use the neutral verb`, { got: html });
  }
  const justBetter = verifyHeadlineHtml(makeBA(0.11));
  assert(justBetter.includes('verify-headline improved'),
    "delta just above +0.1 dB must read improved", { got: justBetter });
  const justWorse = verifyHeadlineHtml(makeBA(-0.11));
  assert(justWorse.includes('verify-headline regressed'),
    "delta just below -0.1 dB must read regressed", { got: justWorse });
}

// 15. verifyHeadlineHtml: missing/partial server payloads render nothing —
//     no headline without a real measured before/after.
{
  assert(verifyHeadlineHtml(null) === '', "null payload → ''");
  assert(verifyHeadlineHtml(undefined) === '', "undefined payload → ''");
  assert(verifyHeadlineHtml({}) === '', "empty payload → ''");
  assert(verifyHeadlineHtml({ before: {}, after: {} }) === '',
    "payload without delta → ''");
  assert(verifyHeadlineHtml(makeBA(undefined)) === '',
    "non-numeric delta.rms_db → ''");
  const noBeforeRms = makeBA(4.1);
  delete noBeforeRms.before.rms_db;
  assert(verifyHeadlineHtml(noBeforeRms) === '',
    "missing before.rms_db → ''");
}

// ---- drawBeforeAfterFill (via drawChart + recording canvas context) --------

const FILL_GREEN = 'rgba(29, 185, 84, 0.22)';
const FILL_AMBER = 'rgba(214, 130, 0, 0.22)';

function makeRecordingContext() {
  const ops = [];
  const ctx = {
    ops,
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    font: '',
    scale() {}, clearRect() {}, fillRect() {},
    beginPath() { ops.push({ op: 'beginPath' }); },
    moveTo() { ops.push({ op: 'moveTo' }); },
    lineTo() { ops.push({ op: 'lineTo' }); },
    closePath() { ops.push({ op: 'closePath' }); },
    fill() { ops.push({ op: 'fill', fillStyle: ctx.fillStyle }); },
    stroke() {}, fillText() {}, setLineDash() {},
    measureText() { return { width: 0 }; },
    save() {}, restore() {},
  };
  return ctx;
}

// Drive drawChart with a recording context on the chart canvas stub and
// return the recorded ops. drawChart reads IIFE-local `lastVerify` for the
// verify overlay + fill, so callers set it via the probe first.
function recordDrawChart(measured, target, predicted, payload) {
  const ctx = makeRecordingContext();
  getOrMake('chart').getContext = () => ctx;
  drawChart(measured, target, predicted, payload);
  return ctx.ops;
}

function beforeAfterFills(ops) {
  return ops
    .map((o, i) => ({ op: o.op, fillStyle: o.fillStyle, i }))
    .filter((o) => o.op === 'fill' &&
      (o.fillStyle === FILL_GREEN || o.fillStyle === FILL_AMBER));
}

// Count moveTo/lineTo/closePath between a fill and its beginPath.
function polygonShape(ops, fillIndex) {
  let moveTo = 0, lineTo = 0, closePath = 0;
  for (let i = fillIndex - 1; i >= 0; i--) {
    const op = ops[i].op;
    if (op === 'beginPath') break;
    if (op === 'moveTo') moveTo += 1;
    else if (op === 'lineTo') lineTo += 1;
    else if (op === 'closePath') closePath += 1;
  }
  return { moveTo, lineTo, closePath };
}

const N_GRID = 480;
const gridFreqs = Array.from(
  { length: N_GRID },
  (_, i) => 20 * Math.pow(20000 / 20, i / (N_GRID - 1)),
);
function curveOf(fn) {
  return {
    freqs_hz: gridFreqs.slice(),
    magnitude_db: gridFreqs.map(fn),
  };
}

// 16. drawBeforeAfterFill: tone→colour mapping + per-segment polygon
//     vertex counts. A segment spanning n grid points draws 1 moveTo +
//     (2n − 1) lineTo (forward along `after`, back along `before`) and
//     closes the path — mirroring drawSpread's polygon technique.
{
  // Physically consistent data: 100..150 improved (6→0 dB), 151..200
  // regressed (0→5 dB) — but the client must take the TONE from the
  // server segment, not from the data (pinned separately in 17).
  const measured = curveOf((_, ) => 0);
  measured.magnitude_db = measured.magnitude_db.map((v, i) =>
    (i >= 100 && i <= 150) ? 6.0 : 0.0);
  const verify = curveOf(() => 0);
  verify.magnitude_db = verify.magnitude_db.map((v, i) =>
    (i >= 151 && i <= 200) ? 5.0 : 0.0);
  setLastVerify(verify);
  const payload = {
    verify_before_after: {
      band_hz: [50, 350],
      fill_segments: [
        { tone: 'improved', i_lo: 100, i_hi: 150, f_lo_hz: gridFreqs[100], f_hi_hz: gridFreqs[150] },
        { tone: 'regressed', i_lo: 151, i_hi: 200, f_lo_hz: gridFreqs[151], f_hi_hz: gridFreqs[200] },
      ],
    },
  };
  const ops = recordDrawChart(measured, null, null, payload);
  const fills = beforeAfterFills(ops);
  assert(fills.length === 2,
    "one fill per server segment", { got: fills.length });
  assert(fills[0] && fills[0].fillStyle === FILL_GREEN,
    "improved tone must fill green", { got: fills[0] && fills[0].fillStyle });
  assert(fills[1] && fills[1].fillStyle === FILL_AMBER,
    "regressed tone must fill amber", { got: fills[1] && fills[1].fillStyle });

  // Segment 1 spans 51 points → 1 moveTo + 101 lineTo; segment 2 spans
  // 50 points → 1 moveTo + 99 lineTo. Each closes its polygon.
  const shape1 = polygonShape(ops, fills[0].i);
  assert(shape1.moveTo === 1 && shape1.lineTo === 101 && shape1.closePath === 1,
    "improved segment polygon must walk its index span forward and back",
    shape1);
  const shape2 = polygonShape(ops, fills[1].i);
  assert(shape2.moveTo === 1 && shape2.lineTo === 99 && shape2.closePath === 1,
    "regressed segment polygon must walk its index span forward and back",
    shape2);
}

// 17. Server-classified tones are consumed VERBATIM. Curve data that
//     visibly regressed (after moved away from target) but is tagged
//     'improved' by the server must still fill green — the client never
//     re-derives improvement from the curves. (The server is the single
//     honest classifier; a client re-derivation could disagree with the
//     Pi's raw-grid verdict once display smoothing is on.)
{
  const measured = curveOf(() => 0);
  const verify = curveOf(() => 0);
  verify.magnitude_db = verify.magnitude_db.map((v, i) =>
    (i >= 100 && i <= 120) ? 6.0 : 0.0);  // clearly worse than before
  setLastVerify(verify);
  const payload = {
    verify_before_after: {
      fill_segments: [
        { tone: 'improved', i_lo: 100, i_hi: 120 },  // server says improved
      ],
    },
  };
  const ops = recordDrawChart(measured, null, null, payload);
  const fills = beforeAfterFills(ops);
  assert(fills.length === 1, "segment must render", { got: fills.length });
  assert(fills[0] && fills[0].fillStyle === FILL_GREEN,
    "client must trust the server tone verbatim, not re-derive from curves",
    { got: fills[0] && fills[0].fillStyle });
}

// 18. No before/after fill without a verify measurement or without the
//     server payload — the chart never invents a before/after story.
{
  const measured = curveOf(() => 0);
  const payload = {
    verify_before_after: {
      fill_segments: [{ tone: 'improved', i_lo: 100, i_hi: 120 }],
    },
  };
  setLastVerify(null);
  let ops = recordDrawChart(measured, null, null, payload);
  assert(beforeAfterFills(ops).length === 0,
    "no verify measurement → no before/after fill");

  const verify = curveOf(() => 0);
  setLastVerify(verify);
  ops = recordDrawChart(measured, null, null, {});
  assert(beforeAfterFills(ops).length === 0,
    "no verify_before_after payload → no before/after fill");
  setLastVerify(null);
}

if (failures) {
  console.error(`\n${failures} correction render test failure(s).`);
  process.exit(1);
}
console.log(JSON.stringify({ ok: true, tests: 18 }));
