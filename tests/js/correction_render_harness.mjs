// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Render harness for /correction/ rendering (C4a-2 + P3a + P3b).
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
// And the P3b stepped-wizard router (envelope-driven dumb frontend):
//   - screen render per envelope fixture, nudge severity (info|warn, never
//     a block), next_action liveness with warn nudges present, the step
//     indicator, and single-sourcing the chart visibility from envelope
//     curves;
//   - the poll discipline (envelope fetched once per state change on static
//     screens, via a probe fetch counter);
//   - the single-primary-action contract: legacy Apply/Verify subordination
//     when the wizard shows exactly that action, the envelope-down legacy
//     fallback, mid-flow-outage stale-action retirement, and the one
//     bounded retry.
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
    _innerHTML: "",
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
    // Child tracking — the P3b router builds step/nudge rows via
    // document.createElement + appendChild, and clears via innerHTML=''.
    children: [],
    appendChild(child) { this.children.push(child); return child; },
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
  // innerHTML accessor: setting it to '' (the router's clear-before-rebuild
  // idiom) also drops tracked children, so child counts reflect the latest
  // render. A non-empty assignment (legacy innerHTML string builders) is
  // stored verbatim and does not populate `children`.
  Object.defineProperty(el, "innerHTML", {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v == null ? "" : v);
      if (this._innerHTML === "") this.children = [];
    },
    enumerable: true,
    configurable: true,
  });
  return el;
}

// Registry of DOM elements the module looks up
const elements = new Map();
function getOrMake(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

// ---- Minimal globals the IIFE needs ----
// Programmable fetch: tests can set the JSON returned per-endpoint substring
// and read a per-endpoint call count. Defaults to an empty 200 so boot's
// fire-and-forget network calls (status/envelope/current-correction) resolve
// harmlessly. The router's fetch-once discipline test drives /status and
// /envelope through this and asserts the /envelope call count.
const fetchRoutes = new Map();       // substring -> () => bodyObject
const fetchCounts = new Map();       // substring -> integer
function setFetchRoute(substr, bodyFn) { fetchRoutes.set(substr, bodyFn); }
function fetchCountFor(substr) { return fetchCounts.get(substr) || 0; }
function resetFetchCounts() { fetchCounts.clear(); }
const globalFetch = (url) => {
  const u = String(url || "");
  let body = {};
  let ok = true;
  let status = 200;
  for (const [substr, bodyFn] of fetchRoutes) {
    if (u.indexOf(substr) !== -1) {
      fetchCounts.set(substr, (fetchCounts.get(substr) || 0) + 1);
      // A bodyFn that throws simulates a DOWN endpoint: the response comes
      // back non-ok so the caller's `if (!resp.ok) throw` fail-soft path
      // runs (mirrors a 5xx / network stall on the Pi).
      try {
        body = bodyFn();
        // A route can return an explicit non-2xx response that still
        // carries a JSON body (e.g. the paid-call 409 with {"error": ...}):
        // { __status: 409, __body: {...} }. Distinct from a thrown/DOWN
        // route, which is a bodyless failure.
        if (body && typeof body === "object" && "__status" in body) {
          status = body.__status;
          ok = status >= 200 && status < 300;
          body = body.__body || {};
        }
      } catch (_e) { ok = false; status = 503; body = {}; }
      break;
    }
  }
  return Promise.resolve({
    ok,
    status,
    async json() { return body; },
    async text() { return ""; },
  });
};

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
    // P3b stepped-wizard router surfaces (all IIFE-local).
    renderEnvelope,
    renderNudges,
    renderPrimaryAction,
    renderProgress,
    showScreenSections,
    pollState,
    // P6 tuning-assistant surfaces (IIFE-local).
    renderTuning,
    renderTuningProposals,
    onTuningInterpret,
    onTuningPropose,
    // The tuning status line text, for the fetch-error-framing tests.
    getTuningStatusText: function () { return tuningStatus.textContent; },
    // Client step-label lexicon, pinned against the server progress spine.
    wizardStepLabels: WIZARD_STEP_LABELS,
    // Probe seams for the fetch-once poll-discipline test.
    getEnvelopeFetchCount: function () { return envelopeFetchCount; },
    resetEnvelopeBookkeeping: function () {
      envelopeFetchCount = 0;
      lastEnvelopeState = null;
      lastAutolevelStatus = null;
      envelopeRetryArmed = false;
      if (envelopeTimer) { clearTimeout(envelopeTimer); envelopeTimer = null; }
    },
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

// Default fetch routes so boot's fire-and-forget /status, /envelope, and
// /sessions calls resolve to a benign idle payload (tests override below).
setFetchRoute("/status", () => ({ state: "idle" }));
setFetchRoute("/envelope", () => ({
  schema_version: 1, screen: "idle", state: "idle",
  curves: {}, fill_segments: [], headline: null,
  verdict_text: "Ready to measure your room.", nudges: [],
  next_action: { label: "Start measuring", endpoint: "/start" },
  progress: { position: 1, total: 6 },
}));

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
  renderEnvelope,
  renderNudges,
  renderPrimaryAction,
  renderProgress,
  showScreenSections,
  pollState,
  renderTuning,
  renderTuningProposals,
  onTuningInterpret,
  onTuningPropose,
  getTuningStatusText,
  wizardStepLabels,
  getEnvelopeFetchCount,
  resetEnvelopeBookkeeping,
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

// Wizard-chrome accessors (getOrMake so they exist even before the router runs).
function wizChrome() { return getOrMake("wizard-chrome"); }
function wizVerdict() { return getOrMake("wizard-verdict"); }
function wizNudges() { return getOrMake("wizard-nudges"); }
function wizNext() { return getOrMake("wizard-next"); }
function wizSteps() { return getOrMake("wizard-steps"); }
function measureSectionEl() { return getOrMake("measure-section"); }
function resultSectionEl() { return getOrMake("result-section"); }
function canvasEl() { return getOrMake("chart"); }

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

// ---- P3b: stepped-wizard router (envelope-driven) --------------------------
//
// The router renders one server screen envelope verbatim: step indicator,
// verdict sentence, homeowner nudges (severity, never a block), and a single
// primary action that stays live regardless of nudges. These pins assert the
// dumb-frontend contract: the browser draws what the server says.

// A complete envelope with sensible defaults; override per test.
function makeEnvelope(over) {
  return Object.assign({
    schema_version: 1,
    screen: "idle",
    state: "idle",
    curves: {},
    fill_segments: [],
    headline: null,
    verdict_text: "Ready to measure your room.",
    nudges: [],
    next_action: { label: "Start measuring", endpoint: "/start" },
    progress: { position: 1, total: 6 },
  }, over || {});
}

function nudgeRows() {
  return wizNudges().children.filter(
    (c) => c.className && c.className.indexOf("wizard-nudge") === 0,
  );
}

// 19. renderEnvelope paints the verdict sentence + reveals the chrome.
{
  renderEnvelope(makeEnvelope({
    screen: "sweep", state: "sweeping",
    verdict_text: "Playing a test sweep. Keep the room quiet.",
    next_action: null,
    progress: { position: 2, total: 6 },
  }));
  assert(wizVerdict().textContent === "Playing a test sweep. Keep the room quiet.",
    "verdict_text is rendered verbatim", { got: wizVerdict().textContent });
  assert(!wizChrome().classList.contains("hidden"),
    "wizard chrome is revealed once an envelope renders");
}

// 20. Step indicator: one row per progress.total, current step marked.
{
  renderEnvelope(makeEnvelope({
    screen: "review", state: "ready",
    progress: { position: 3, total: 6 },
    next_action: { label: "Apply correction", endpoint: "/apply" },
  }));
  const steps = wizSteps().children;
  assert(steps.length === 6, "one step per progress.total", { got: steps.length });
  assert(steps[2].className.indexOf("current") !== -1,
    "progress.position marks the current step (3rd)", { got: steps[2].className });
  assert(steps[0].className.indexOf("done") !== -1,
    "earlier steps are marked done", { got: steps[0].className });
  assert(steps[4].className.indexOf("current") === -1 &&
    steps[4].className.indexOf("done") === -1,
    "later steps are neither current nor done", { got: steps[4].className });
  assert(wizSteps().getAttribute("aria-label") === "Step 3 of 6",
    "step indicator exposes an aria-label", { got: wizSteps().getAttribute("aria-label") });
}

// 21. Nudge severity rendering: info -> info class, warn -> warn class,
//     text rendered as a sentence (verbatim, via textContent).
{
  renderNudges([
    { code: "uncalibrated_mic", severity: "info",
      text: "Results will be approximate without a calibrated mic — you can continue." },
    { code: "high_position_variance", severity: "warn",
      text: "Your measured spots differ a lot — re-measuring can help, but you can continue." },
  ]);
  const rows = nudgeRows();
  assert(rows.length === 2, "one row per nudge", { got: rows.length });
  assert(rows[0].className.indexOf("info") !== -1,
    "info nudge gets the info tone class", { got: rows[0].className });
  assert(rows[1].className.indexOf("warn") !== -1,
    "warn nudge gets the warn tone class", { got: rows[1].className });
  // The sentence lives in the __text child, set via textContent (inert).
  const t0 = rows[0].children.find((c) => c.className === "wizard-nudge__text");
  assert(t0 && t0.textContent.indexOf("approximate") !== -1,
    "nudge text is rendered as a sentence", { got: t0 && t0.textContent });
  assert(!wizNudges().classList.contains("hidden"),
    "nudge container is visible when nudges exist");
}

// 22. No nudges -> container hidden, no rows.
{
  renderNudges([]);
  assert(wizNudges().classList.contains("hidden"),
    "empty nudges hide the container");
  assert(nudgeRows().length === 0, "no nudge rows when empty");
}

// 23. Unknown/blank severity clamps to info (never an unstyled or block row).
{
  renderNudges([{ code: "x", severity: "danger", text: "some future nudge" }]);
  const rows = nudgeRows();
  assert(rows.length === 1 && rows[0].className.indexOf("info") !== -1,
    "a non-info/non-warn severity renders as info, never a block tone",
    { got: rows[0] && rows[0].className });
}

// 24. Primary action: label + endpoint from the server, button live + shown.
{
  renderPrimaryAction({ label: "Apply correction", endpoint: "/apply" });
  assert(wizNext().textContent === "Apply correction",
    "primary action label comes from the server", { got: wizNext().textContent });
  assert(wizNext().getAttribute("data-endpoint") === "/apply",
    "endpoint is stashed on a data-* attribute (no inline handler interp)",
    { got: wizNext().getAttribute("data-endpoint") });
  assert(wizNext().disabled === false, "primary action is live");
  assert(!wizNext().classList.contains("hidden"), "primary action is shown");
}

// 25. next_action === null -> button hidden (browser-driven / terminal step).
{
  renderPrimaryAction(null);
  assert(wizNext().classList.contains("hidden"),
    "null next_action hides the primary action");
  assert(wizNext().getAttribute("data-endpoint") === null,
    "null next_action clears the stashed endpoint");
}

// 26. LIVENESS: a warn nudge present does NOT disable the primary action —
//     measurement-quality nudges inform, they never gate. This is the core
//     "nothing disabled" contract, exercised through the full renderEnvelope.
{
  renderEnvelope(makeEnvelope({
    screen: "review", state: "ready",
    next_action: { label: "Apply correction", endpoint: "/apply" },
    nudges: [
      { code: "high_position_variance", severity: "warn",
        text: "Your measured spots differ a lot — you can continue." },
      { code: "capture_snr_low", severity: "warn",
        text: "The room was a little noisy — you can still continue." },
    ],
  }));
  assert(wizNext().disabled === false,
    "primary action stays LIVE even with warn nudges present (never gated)");
  assert(!wizNext().classList.contains("hidden"),
    "primary action stays visible with warn nudges present");
  assert(nudgeRows().length === 2,
    "both warn nudges are shown alongside the live action", { got: nudgeRows().length });
  assert(nudgeRows().every((r) => r.className.indexOf("warn") !== -1),
    "warn nudges render with the warn tone, not a block tone");
}

// 27. showScreenSections: idle hides the measure workflow; a sweep screen
//     shows it. The review/result chart visibility is SINGLE-SOURCED from
//     the envelope's own curves (never client state like lastResult, which
//     is empty after a mid-flow page reload): no measured curve in the
//     envelope -> chart hidden; envelope measured curve -> chart shown.
{
  showScreenSections("idle", {});
  assert(measureSectionEl().classList.contains("hidden"),
    "idle screen hides the measure workflow");
  showScreenSections("sweep", {});
  assert(!measureSectionEl().classList.contains("hidden"),
    "sweep screen shows the measure workflow");
  // review screen but the envelope carries no curves -> chart stays hidden.
  showScreenSections("review", {});
  assert(resultSectionEl().classList.contains("hidden"),
    "review with no envelope curves keeps the chart hidden (no blank frame)");
  // review with an envelope measured curve -> chart shown (works even when
  // client-side lastResult is empty, e.g. after a mid-flow page reload).
  showScreenSections("review", {
    measured: { freqs_hz: [20, 200], magnitude_db: [0, 0] },
  });
  assert(!resultSectionEl().classList.contains("hidden"),
    "review with an envelope measured curve shows the chart");
  showScreenSections("idle", {});
}

// 28. FETCH-ONCE DISCIPLINE (P3b-1 reviewer advisory): on a STATIC screen the
//     envelope is fetched once per state CHANGE, not on every /status tick.
//     Drive pollState repeatedly with the SAME static state and assert only
//     one envelope fetch fires; then flip the state and assert exactly one more.
await (async () => {
  resetEnvelopeBookkeeping();
  resetFetchCounts();
  // Static 'ready' screen. /status returns 'ready' (no reschedule in
  // pollState for ready), /envelope returns the review screen.
  setFetchRoute("/status", () => ({ state: "ready", autolevel: { status: "idle" } }));
  setFetchRoute("/envelope", () => makeEnvelope({ screen: "review", state: "ready" }));

  await pollState();          // first observation of 'ready' -> one env fetch
  await pollState();          // same state -> NO new env fetch
  await pollState();          // same state -> NO new env fetch
  const afterStatic = getEnvelopeFetchCount();
  assert(afterStatic === 1,
    "static screen fetches the envelope ONCE per state change, not per tick",
    { got: afterStatic });

  // Now transition to 'applied' -> exactly one more envelope fetch.
  setFetchRoute("/status", () => ({ state: "applied", autolevel: { status: "idle" } }));
  setFetchRoute("/envelope", () => makeEnvelope({ screen: "apply", state: "applied" }));
  await pollState();
  const afterTransition = getEnvelopeFetchCount();
  assert(afterTransition === 2,
    "a state transition triggers exactly one more envelope fetch",
    { got: afterTransition });

  // Restore benign defaults for any trailing async boot work.
  setFetchRoute("/status", () => ({ state: "idle" }));
  setFetchRoute("/envelope", () => makeEnvelope());
  resetEnvelopeBookkeeping();
})();

// Drain fire-and-forget async chains (pollState launches refreshEnvelope
// without awaiting: fetch -> json -> render). A few macrotask ticks settle it.
async function settle() {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
}

// 29a. SINGLE PRIMARY ACTION: when the wizard is up and owns the forward
//      action, the duplicate legacy in-section Apply button is subordinated
//      (hidden) while non-duplicated controls (Reset) stay. Drive the full
//      pollState -> applyButtonPolicy -> envelope-render path for 'ready'.
await (async () => {
  resetEnvelopeBookkeeping();
  resetFetchCounts();
  const applyCorrectionBtn = getOrMake("apply-correction");
  const resetCorrectionBtn = getOrMake("reset-correction");
  applyCorrectionBtn.classList.remove("hidden");   // pretend a stale showing

  setFetchRoute("/status", () => ({ state: "ready", autolevel: { status: "idle" } }));
  setFetchRoute("/envelope", () => makeEnvelope({
    screen: "review", state: "ready",
    next_action: { label: "Apply correction", endpoint: "/apply" },
  }));
  await pollState();
  await settle();   // let the fire-and-forget envelope render + reconcile run

  assert(wizNext().getAttribute("data-endpoint") === "/apply",
    "wizard primary action owns Apply on the review screen",
    { got: wizNext().getAttribute("data-endpoint") });
  assert(applyCorrectionBtn.classList.contains("hidden"),
    "the duplicate legacy Apply button is subordinated to the wizard action");
  assert(!resetCorrectionBtn.classList.contains("hidden"),
    "Reset (no wizard equivalent) stays available in the section");
})();

// 29b. RESILIENCE: if the envelope path is DOWN (chrome hidden, /envelope
//      failing), the legacy Apply button remains the fallback so the user is
//      never stranded without a way to apply. Fully settle 29a first so no
//      stale success-render can un-hide the chrome mid-assert.
await (async () => {
  await settle();
  const applyCorrectionBtn = getOrMake("apply-correction");
  resetEnvelopeBookkeeping();
  resetFetchCounts();
  setFetchRoute("/status", () => ({ state: "ready", autolevel: { status: "idle" } }));
  setFetchRoute("/envelope", () => { throw new Error("envelope down"); });
  wizChrome().classList.add("hidden");
  applyCorrectionBtn.classList.remove("hidden");
  await pollState();
  await settle();
  assert(wizChrome().classList.contains("hidden"),
    "a failing /envelope leaves the chrome hidden (fail-soft)");
  assert(!applyCorrectionBtn.classList.contains("hidden"),
    "with the envelope path down, the legacy Apply button stays as fallback");

  setFetchRoute("/status", () => ({ state: "idle" }));
  setFetchRoute("/envelope", () => makeEnvelope());
  resetEnvelopeBookkeeping();
  await settle();
})();

// 30. MID-FLOW ENVELOPE-ONLY OUTAGE (the review's SHOULD-FIX cell): the
//     envelope was healthy at ready (wizard shows Apply), the session then
//     advances to applied, and the envelope fetch FAILS. The stale Apply
//     action must be retired (it points at the wrong endpoint for the new
//     state) and the legacy Verify button must be live — the single visible
//     action is always correct. Then the one bounded retry credit recovers
//     the chrome once the envelope endpoint comes back.
await (async () => {
  resetEnvelopeBookkeeping();
  resetFetchCounts();
  const applyCorrectionBtn = getOrMake("apply-correction");
  const verifyCorrectionBtn = getOrMake("verify-correction");

  // Phase 1: healthy at ready — wizard owns Apply, legacy Apply subordinated.
  setFetchRoute("/status", () => ({ state: "ready", autolevel: { status: "idle" } }));
  setFetchRoute("/envelope", () => makeEnvelope({
    screen: "review", state: "ready",
    next_action: { label: "Apply correction", endpoint: "/apply" },
  }));
  await pollState();
  await settle();
  assert(wizNext().getAttribute("data-endpoint") === "/apply",
    "phase 1: wizard shows Apply at ready",
    { got: wizNext().getAttribute("data-endpoint") });
  assert(applyCorrectionBtn.classList.contains("hidden"),
    "phase 1: legacy Apply subordinated while the wizard owns it");

  // Phase 2: the session advances to applied; the envelope endpoint is DOWN.
  setFetchRoute("/status", () => ({ state: "applied", autolevel: { status: "idle" } }));
  setFetchRoute("/envelope", () => { throw new Error("envelope down"); });
  await pollState();
  await settle();
  assert(wizNext().classList.contains("hidden"),
    "phase 2: the stale Apply action is retired on the failed refresh " +
    "(it no longer matches the applied state)");
  assert(!verifyCorrectionBtn.classList.contains("hidden"),
    "phase 2: the legacy Verify button is live as the single correct action");
  assert(applyCorrectionBtn.classList.contains("hidden"),
    "phase 2: the legacy Apply button stays hidden (wrong for applied)");

  // Phase 3: the endpoint recovers; the one bounded retry credit (scheduled
  // by the phase-2 failure) re-fetches and restores the wizard action.
  setFetchRoute("/envelope", () => makeEnvelope({
    screen: "apply", state: "applied",
    next_action: { label: "Verify the result", endpoint: "/verify" },
  }));
  let recovered = false;
  for (let i = 0; i < 30 && !recovered; i++) {
    await new Promise((r) => setTimeout(r, 100));
    recovered = wizNext().getAttribute("data-endpoint") === "/verify" &&
      !wizNext().classList.contains("hidden");
  }
  assert(recovered,
    "phase 3: the one bounded retry recovers the wizard action after a " +
    "transient envelope blip");
  assert(verifyCorrectionBtn.classList.contains("hidden"),
    "phase 3: the recovered wizard action re-subordinates the legacy Verify");

  setFetchRoute("/status", () => ({ state: "idle" }));
  setFetchRoute("/envelope", () => makeEnvelope());
  resetEnvelopeBookkeeping();   // clears any pending retry timer
  await settle();
})();

// 31. WIZARD_STEP_LABELS stays in lockstep with the server's progress spine:
//     envelope._PROGRESS_SPINE (jasper/correction/envelope.py) has exactly 6
//     entries (idle, sweep, review, apply, verify, result) and progress.total
//     comes from its length. If the server spine grows/shrinks, this fails
//     loudly instead of the indicator silently degrading to "Step N" labels.
{
  assert(wizardStepLabels.length === 6,
    "WIZARD_STEP_LABELS must match envelope._PROGRESS_SPINE's 6 entries — " +
    "update the client lexicon with the server spine",
    { got: wizardStepLabels.length });
  assert(wizardStepLabels.every((l) => typeof l === "string" && l.trim()),
    "every wizard step label is non-empty homeowner copy");
}

// 32. P6 tuning affordance: the panel is hidden until offered, shows the
//     nudge when offered-but-unavailable (no key), and shows the two
//     per-tap actions when available.
function tuningPanelEl() { return getOrMake("tuning-panel"); }
function tuningNudgeEl() { return getOrMake("tuning-nudge"); }
function tuningActionsEl() { return getOrMake("tuning-actions"); }
function tuningProposalsEl() { return getOrMake("tuning-proposals"); }
{
  renderTuning(null);
  assert(tuningPanelEl().classList.contains("hidden"),
    "tuning: a missing block keeps the panel hidden");

  renderTuning({ offered: false, available: true, provider: "openai" });
  assert(tuningPanelEl().classList.contains("hidden"),
    "tuning: not offered (pre-measurement screen) keeps the panel hidden");

  renderTuning({ offered: true, available: false, provider: "openai", nudge: "Add an OpenAI key at /voice" });
  assert(!tuningPanelEl().classList.contains("hidden"),
    "tuning: offered-but-unavailable reveals the panel");
  assert(!tuningNudgeEl().classList.contains("hidden"),
    "tuning: offered-but-unavailable shows the nudge");
  assert(tuningActionsEl().classList.contains("hidden"),
    "tuning: offered-but-unavailable hides the action buttons");
  assert(tuningNudgeEl().textContent.indexOf("/voice") >= 0,
    "tuning: the no-key nudge points at /voice");

  renderTuning({ offered: true, available: true, provider: "openai", model: "gpt-5.4" });
  assert(!tuningActionsEl().classList.contains("hidden"),
    "tuning: available shows the two per-tap actions");
  assert(tuningNudgeEl().classList.contains("hidden"),
    "tuning: available hides the nudge");
}

// 33. A simulate-accepted room-correction proposal renders an applicable
//     card; a rejected one renders its reason and no Apply button; a
//     target move renders as a suggestion with plain-text guidance to the
//     flow's Target curve picker (no apply path, and no dead #target-select
//     anchor — that picker is hidden in relay mode, so a link would
//     silently scroll nowhere).
{
  renderTuningProposals([
    {
      kind: "room_correction", applicable: true,
      correction_peqs: [{ freq_hz: 62, q: 3, gain_db: -7 }],
      rationale: "deeper cut at the 62 Hz mode",
      simulation: { accepted: true, issues: [], acceptance: { verdict: "accept", overall_rms_delta_db: 2.4 } },
    },
    {
      kind: "room_correction", applicable: false,
      correction_peqs: [{ freq_hz: 62, q: 6, gain_db: 6 }],
      rationale: "boost the dip",
      simulation: { accepted: false, issues: [{ code: "boost_would_ring", message: "would ring" }], acceptance: null },
    },
    {
      // Honest server payload shape: suggestion-only, never applicable.
      kind: "preference_question", applicable: false, suggestion_only: true,
      target_id: "warm", warmth: null, rationale: "you asked for warmer",
    },
  ]);
  const cards = tuningProposalsEl().children;
  assert(cards.length === 3, "tuning: three proposal cards render", { got: cards.length });
  // The rejected card carries the rejection modifier class.
  assert(cards[1].className.indexOf("tuning-proposal--rejected") >= 0,
    "tuning: the ring-rejected proposal card is styled as rejected");
  // The target-move card's guidance is plain text — an honest affordance,
  // NOT a dead #target-select link (that anchor no-ops on the review
  // screen when the picker's container is hidden in relay mode).
  const targetCard = cards[2];
  const question = targetCard.children.find(
    (c) => c.className === "tuning-question");
  assert(question, "tuning: the target-move card renders its question line");
  assert(question.textContent.indexOf("Pick it under Target curve") >= 0,
    "tuning: the target-move card carries the Target curve instruction as text");
  const pickerLink = (question.children || []).find(
    (c) => c && c.href === "#target-select");
  assert(!pickerLink,
    "tuning: the target-move card has no dead #target-select link");

  // Empty proposals clears the container.
  renderTuningProposals([]);
  assert(tuningProposalsEl().children.length === 0,
    "tuning: empty proposals clears the cards");
}

// 34. The paid-call min-interval gate returns an honest 409 whose JSON body
//     carries the server's message. The tuning panel shows that message
//     AS-IS (the assistant WAS reached), NOT under the "Could not reach"
//     network-failure prefix. A genuine down endpoint still keeps that
//     prefix.
await (async () => {
  const tuningStatusEl = () => getOrMake("tuning-status");

  // 34a. A 409 carrying the server's honest reason is shown verbatim.
  const gate409 = "the tuning assistant just made a paid call — wait a moment and tap again";
  setFetchRoute("/interpret", () => ({ __status: 409, __body: { error: gate409 } }));
  await onTuningInterpret();
  const status409 = getTuningStatusText();
  assert(status409 === gate409,
    "tuning: a 409 gate refusal shows the server message verbatim",
    { got: status409 });
  assert(status409.indexOf("Could not reach") < 0,
    "tuning: a 409 refusal is NOT framed as 'Could not reach'",
    { got: status409 });

  // 34b. Same honest surfacing on /propose.
  setFetchRoute("/propose", () => ({ __status: 409, __body: { error: gate409 } }));
  await onTuningPropose();
  assert(getTuningStatusText() === gate409,
    "tuning: /propose 409 refusal also shows the server message verbatim",
    { got: getTuningStatusText() });

  // 34c. A genuine down endpoint (no JSON error body) keeps the
  //      "Could not reach" network-failure framing.
  setFetchRoute("/interpret", () => { throw new Error("down"); });
  await onTuningInterpret();
  const statusDown = getTuningStatusText();
  assert(statusDown.indexOf("Could not reach the tuning assistant") === 0,
    "tuning: a true network failure keeps the 'Could not reach' framing",
    { got: statusDown });

  // Restore benign defaults.
  setFetchRoute("/interpret", () => ({}));
  setFetchRoute("/propose", () => ({}));
  tuningStatusEl().textContent = "";
})();

if (failures) {
  console.error(`\n${failures} correction render test failure(s).`);
  process.exit(1);
}
console.log(JSON.stringify({ ok: true, tests: 34 }));
