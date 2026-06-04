// Minimal DOM harness for the /sound/ static module. It exercises the
// live-source tab state machine without needing a browser or CamillaDSP.
//
//   node tests/js/sound_profile_harness.mjs deploy/assets/sound-profile/js/main.js
import { readFileSync } from "node:fs";

const elements = new Map();

function classList() {
  const values = new Set();
  return {
    toggle(name, force) {
      if (force) values.add(name);
      else values.delete(name);
    },
    contains(name) { return values.has(name); },
  };
}

function makeEl(id) {
  return {
    id, innerHTML: "", textContent: "", className: "",
    attrs: {}, style: {}, _listeners: {}, classList: classList(),
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    addEventListener(ev, fn) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
    },
    click() {
      for (const fn of this._listeners.click || []) {
        fn({ preventDefault() {}, target: this });
      }
    },
    querySelector() { return null; },
    closest() { return null; },
  };
}

for (const id of [
  "tab-off", "tab-saved", "tab-draft", "back", "view-body",
  "plot", "plot-summary", "live-label", "status",
]) {
  elements.set(id, makeEl(id));
}

globalThis.document = {
  getElementById(id) { return elements.get(id) || null; },
  querySelector(sel) {
    return sel === "meta[name=jts-csrf]" ? { content: "csrf-token" } : null;
  },
};
globalThis.window = {
  setTimeout,
  clearTimeout,
  location: { href: "" },
};

function response(payload, ok = true) {
  return { ok, async json() { return payload; } };
}

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

const flatProfile = {
  enabled: true,
  curve_id: "flat",
  simple_eq: {},
  parametric_bands: [],
  profile_id: "stock:flat",
  profile_name: "Flat",
};
const library = [
  { id: "stock:flat", name: "Flat", kind: "stock", editable: false, profile: flatProfile },
];
const basePayload = {
  limits: {
    simple_gain_db: 12, advanced_gain_db: 12, max_parametric_bands: 8,
    min_freq_hz: 20, max_freq_hz: 20000, min_q: 0.2, max_q: 10,
    simple_bands: [], headroom_trim_max_db: 12,
  },
  curves: [{ id: "flat", filters: [] }],
  profile_library: library,
  sound_settings: { headroom_trim_db: 0, match_loudness: false },
};

const applyRequests = [];
const applyResponses = [];
const liveDraftRequests = [];

globalThis.fetch = (path, options = {}) => {
  if (path === "./state") {
    return Promise.resolve(response({
      ...basePayload,
      profile: { ...flatProfile, enabled: false },
      filter_count: 0,
      dsp_write_epoch: "state-0",
    }));
  }
  if (path === "./apply") {
    const body = JSON.parse(options.body || "{}");
    applyRequests.push(body);
    const d = deferred();
    applyResponses.push(d);
    return d.promise;
  }
  if (path === "./live-draft") {
    liveDraftRequests.push(JSON.parse(options.body || "{}"));
    return Promise.resolve(response({
      ...basePayload,
      profile: flatProfile,
      filter_count: 0,
      dsp_write_epoch: "live-1",
      live_status: "live",
    }));
  }
  if (path === "./preview") {
    return Promise.resolve(response({ preview: [] }));
  }
  throw new Error(`unexpected fetch: ${path}`);
};

const flush = () => new Promise((r) => setTimeout(r, 0));
const fail = (message) => {
  throw new Error(`${message}\napply=${JSON.stringify(applyRequests)}\nlive=${JSON.stringify(liveDraftRequests)}`);
};

// Inline the real eq-math module (pure, DOM-free) so the harness exercises
// the actual graph math. Both module imports are stripped because the source
// is eval'd via new Function(), where ES import statements are illegal.
const modulePath = process.argv[2];
const eqMathPath = new URL("../../deploy/assets/sound-profile/js/eq-math.js", import.meta.url);
const eqMathPreamble = readFileSync(eqMathPath, "utf8").replace(/^export\s+/gm, "");

const source = readFileSync(modulePath, "utf8")
  .replace(/^import\s+\{\s*jtsConfirm\s+\}\s+from\s+["'][^"']+["'];\s*/m, "const jtsConfirm = async () => true;\n")
  .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*eq-math\.js["'];\s*/m, "");
new Function(eqMathPreamble + "\n" + source)();

await flush();
await flush();

elements.get("tab-saved").click();
await flush();
if (applyRequests.length !== 1) fail("Saved tab should start one durable apply");

elements.get("tab-draft").click();
await flush();
if (liveDraftRequests.length !== 0) fail("Draft should wait while durable apply is in flight");

applyResponses[0].resolve(response({
  ...basePayload,
  profile: applyRequests[0],
  filter_count: 0,
  dsp_write_epoch: "apply-1",
}));
await flush();
await flush();
await flush();

if (liveDraftRequests.length !== 1) {
  fail("Draft live update should replay after the stale Saved apply finishes");
}

console.log(JSON.stringify({
  applyProfileIds: applyRequests.map((p) => p.profile_id || ""),
  liveDraftRequests: liveDraftRequests.length,
  liveDraftEpoch: liveDraftRequests[0].dsp_write_epoch,
  liveTabMarked: elements.get("tab-draft").classList.contains("is-live"),
}));
