// Minimal DOM harness for the /sound/ static module. It exercises the
// live-source tab state machine plus active-speaker guards without needing a
// browser or CamillaDSP.
//
//   node tests/js/sound_profile_harness.mjs deploy/assets/sound-profile/js/main.js
import { readFileSync } from "node:fs";

const modulePath = process.argv[2];
const eqMathPath = new URL("../../deploy/assets/sound-profile/js/eq-math.js", import.meta.url);
const eqMathPreamble = readFileSync(eqMathPath, "utf8").replace(/^export\s+/gm, "");
const escapePath = new URL("../../deploy/assets/shared/js/escape.js", import.meta.url);
const escapePreamble = readFileSync(escapePath, "utf8")
  .replace(/^export\s+\{[^}]+\};\s*$/gm, "")
  .replace(/^export\s+/gm, "");
const localModulePaths = [
  "../../deploy/assets/sound-profile/js/api.js",
  "../../deploy/assets/sound-profile/js/store.js",
  "../../deploy/assets/sound-profile/js/active-speaker-views.js",
  "../../deploy/assets/sound-profile/js/active-speaker-actions.js",
];
const localModulePreamble = localModulePaths.map((moduleName) => {
  const moduleUrl = new URL(moduleName, import.meta.url);
  return readFileSync(moduleUrl, "utf8").replace(/^export\s+/gm, "");
}).join("\n");

const rawSource = readFileSync(modulePath, "utf8");
const source = rawSource
  .replace(/^import\s+\{\s*jtsConfirm\s+\}\s+from\s+["'][^"']+["'];\s*/m, "const jtsConfirm = async () => true;\n")
  .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*escape\.js["'];\s*/m, "")
  .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*eq-math\.js["'];\s*/m, "")
  .replace(/^import\s+\{[^}]*\}\s+from\s+["'][.][^"']+["'];\s*/gm, "");
if (/^import\s/m.test(source)) {
  throw new Error("unhandled import in main.js — add a strip rule + preamble to this harness");
}

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
    id, innerHTML: "", textContent: "", className: "", value: "", checked: false,
    attrs: {}, style: {}, _listeners: {}, classList: classList(),
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    addEventListener(ev, fn) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
    },
    click() {
      for (const fn of this._listeners.click || []) {
        fn({ preventDefault() {}, target: this });
      }
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
}

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

function topologyPayload() {
  return {
    status: "valid",
    hardware: { physical_output_count: 2, profile_id: "test-dac" },
    routing: { mono_group_id: "main", main_left_group_id: null, main_right_group_id: null, subwoofer_group_ids: [] },
    evaluation: { status: "valid" },
    speaker_groups: [{
      id: "main",
      label: "Main speaker",
      kind: "mono",
      mode: "full_range_passive",
      position: { x: 0, y: 0, rotation_degrees: 0 },
      channels: [{
        role: "full_range",
        physical_output_index: 0,
        identity_verified: true,
        startup_muted: true,
        protection_required: false,
        protection_status: "not_required",
      }],
    }],
  };
}

function activePayloads() {
  const level = {
    status: "ready",
    test_signal: {
      min_level_dbfs: -80,
      max_level_dbfs: -45,
      step_db: 1,
      default_level_dbfs: -80,
      requested_level_dbfs: -72,
    },
    mic_meter: { status: "usable", recommendation: "hold_level" },
    software_gain_guard: { upward_step_limit_db: 1 },
    issues: [],
  };
  return {
    "./active-speaker/environment": {
      ok_to_load_active_config: true,
      issues: [],
      warning: "",
      status: "ready",
    },
    "./active-speaker/safe-playback": {
      status: "armed",
      issues: [],
      quiet_start: { status: "floor_required", floor_audio_confirmed: false },
    },
    "./active-speaker/staged-config": {
      status: "staged",
      preset: { name: "Protected" },
      config: { basename: "startup.yml", playback_device: "hw:test", playback_channels: 2, validation: { status: "valid" } },
      load: { load_gate: "ready" },
      issues: [],
    },
    "./active-speaker/calibration-level": level,
    "./active-speaker/bringup-preflight": {
      modes: {
        manual_guarded_bringup: { status: "ready", required_gates: [] },
        guided_calibration: { status: "ready", required_gates: [] },
      },
      microphone: { status: "usable" },
      software_guard: { status: "ready" },
      calibration_level: { at_floor: false },
      next_step: "Bring-up ready.",
    },
    "./active-speaker/startup-load": {
      state: { status: "loaded", rollback_available: true, current_config_matches_loaded: true },
      preflight: { status: "ready", load_allowed: true, path_safety: { load_gate: "ready" }, candidate: { basename: "startup.yml" } },
    },
    "./active-speaker/commissioning-rehearsal": {
      status: "ready",
      next_step: "Commissioning rehearsal is still present.",
      steps: [{ id: "path", label: "Path check sentinel", status: "done", message: "sentinel rehearsal evidence" }],
    },
    "./active-speaker/tone-targets": {
      calibration_level: level,
      targets: [{ side: "mono", driver_role: "full_range", label: "Main full range" }],
    },
  };
}

function levelPayload(value) {
  return {
    status: "ready",
    test_signal: {
      min_level_dbfs: -80,
      max_level_dbfs: -45,
      step_db: 1,
      default_level_dbfs: -80,
      requested_level_dbfs: value,
    },
    mic_meter: { status: "usable", recommendation: "hold_level" },
    software_gain_guard: { upward_step_limit_db: 1 },
    issues: [],
  };
}

function setupHarness(fetchHandler) {
  const elements = new Map();
  for (const id of [
    "tab-off", "tab-saved", "tab-draft", "back", "view-body",
    "plot", "plot-summary", "live-label", "status",
  ]) {
    elements.set(id, makeEl(id));
  }

  globalThis.document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeEl(id));
      return elements.get(id);
    },
    querySelector(sel) {
      return sel === "meta[name=jts-csrf]" ? { content: "csrf-token" } : null;
    },
  };
  globalThis.window = {
    setTimeout,
    clearTimeout,
    location: { href: "" },
  };
  Object.defineProperty(globalThis, "navigator", {
    value: { clipboard: { async writeText() {} } },
    configurable: true,
  });
  globalThis.fetch = fetchHandler;

  new Function(
    escapePreamble + "\n" + eqMathPreamble + "\n" + localModulePreamble + "\n" + source
  )();

  const viewBody = elements.get("view-body");
  const dispatchClick = (attrs) => {
    const target = {
      getAttribute(name) { return attrs[name] || ""; },
      closest(selector) { return selector === "[data-act]" ? this : null; },
    };
    for (const fn of viewBody._listeners.click || []) {
      fn({ target, preventDefault() {} });
    }
  };
  const dispatchChange = (target) => {
    for (const fn of viewBody._listeners.change || []) {
      fn({ target });
    }
  };
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return { elements, dispatchClick, dispatchChange, flush };
}

function baseFetch(overrides = {}) {
  const active = activePayloads();
  return (path, options = {}) => {
    if (overrides[path]) return overrides[path](path, options);
    if (path === "./state") {
      return Promise.resolve(response({
        ...basePayload,
        profile: { ...flatProfile, enabled: false },
        filter_count: 0,
        dsp_write_epoch: "state-0",
      }));
    }
    if (path === "./output-topology") return Promise.resolve(response(topologyPayload()));
    if (path === "./active-speaker/design-draft") {
      return Promise.resolve(response({ status: "ready_for_review", summary: {}, operator_inputs: {} }));
    }
    if (path === "./active-speaker/crossover-preview") {
      return Promise.resolve(response({ status: "not_prepared", issues: [] }));
    }
    if (path === "./preview") return Promise.resolve(response({ preview: [] }));
    if (active[path] && !options.method) return Promise.resolve(response(active[path]));
    throw new Error(`unexpected fetch: ${path}`);
  };
}

function fail(message, details = {}) {
  throw new Error(`${message}\n${JSON.stringify(details, null, 2)}`);
}

async function loadAndSetActiveState(harness) {
  await harness.flush();
  await harness.flush();
  harness.dispatchClick({ "data-act": "refresh-active-speaker" });
  await harness.flush();
  await harness.flush();
  await harness.flush();
}

function assertRehearsalVisible(harness, label) {
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Path check sentinel") || !html.includes("sentinel rehearsal evidence")) {
    fail(`${label} should preserve commissioning rehearsal evidence`, { html });
  }
}

async function testLiveTabReplay() {
  const applyRequests = [];
  const applyResponses = [];
  const liveDraftRequests = [];
  const fetchHandler = baseFetch({
    "./apply": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      applyRequests.push(body);
      const d = deferred();
      applyResponses.push(d);
      return d.promise;
    },
    "./live-draft": (_path, options = {}) => {
      liveDraftRequests.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({
        ...basePayload,
        profile: flatProfile,
        filter_count: 0,
        dsp_write_epoch: "live-1",
        live_status: "live",
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush();
  await harness.flush();

  harness.elements.get("tab-saved").click();
  await harness.flush();
  if (applyRequests.length !== 1) fail("Saved tab should start one durable apply", { applyRequests });

  harness.elements.get("tab-draft").click();
  await harness.flush();
  if (liveDraftRequests.length !== 0) fail("Draft should wait while durable apply is in flight", { liveDraftRequests });

  applyResponses[0].resolve(response({
    ...basePayload,
    profile: applyRequests[0],
    filter_count: 0,
    dsp_write_epoch: "apply-1",
  }));
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (liveDraftRequests.length !== 1) {
    fail("Draft live update should replay after the stale Saved apply finishes", { applyRequests, liveDraftRequests });
  }
  return {
    applyProfileIds: applyRequests.map((p) => p.profile_id || ""),
    liveDraftRequests: liveDraftRequests.length,
    liveDraftEpoch: liveDraftRequests[0].dsp_write_epoch,
    liveTabMarked: harness.elements.get("tab-draft").classList.contains("is-live"),
  };
}

async function testRehearsalPreservedAcrossStartupActions() {
  const active = activePayloads();
  const fetchHandler = baseFetch({
    "./active-speaker/check-path-safety": () => Promise.resolve(response({
      report: { ok_to_load_active_config: true },
      startup_load: active["./active-speaker/startup-load"],
    })),
    "./active-speaker/load-startup-config": () => Promise.resolve(response({
      load: { status: "loaded", rollback_available: true, current_config_matches_loaded: true },
      preflight: active["./active-speaker/startup-load"].preflight,
    })),
    "./active-speaker/rollback-startup-config": () => Promise.resolve(response({
      rollback: { status: "rolled_back" },
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  assertRehearsalVisible(harness, "initial refresh");

  harness.dispatchClick({ "data-act": "check-active-path-safety" });
  await harness.flush();
  await harness.flush();
  await harness.flush();
  assertRehearsalVisible(harness, "path safety check");

  harness.dispatchClick({ "data-act": "load-active-startup" });
  await harness.flush();
  await harness.flush();
  await harness.flush();
  assertRehearsalVisible(harness, "startup load");

  harness.dispatchClick({ "data-act": "rollback-active-startup" });
  await harness.flush();
  await harness.flush();
  await harness.flush();
  assertRehearsalVisible(harness, "startup rollback");

  return { rehearsalPreserved: true };
}

async function testStaleLevelResponseDiscarded() {
  const levelPosts = [];
  const levelResponses = [];
  const fetchHandler = baseFetch({
    "./active-speaker/calibration-level": (path, options = {}) => {
      if (!options.method) return Promise.resolve(response(activePayloads()[path]));
      levelPosts.push(JSON.parse(options.body || "{}"));
      const d = deferred();
      levelResponses.push(d);
      return d.promise;
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchChange({
    id: "active-speaker-level",
    value: "-70",
    getAttribute() { return null; },
  });
  harness.dispatchChange({
    id: "active-speaker-level",
    value: "-60",
    getAttribute() { return null; },
  });
  await harness.flush();
  if (levelPosts.length !== 2) fail("Two calibration POSTs should be in flight", { levelPosts });

  levelResponses[1].resolve(response(levelPayload(-60)));
  await harness.flush();
  await harness.flush();
  await harness.flush();
  levelResponses[0].resolve(response(levelPayload(-70)));
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("-60.0 dBFS") || html.includes("-70.0 dBFS")) {
    fail("Stale calibration-level response should not overwrite the latest level", { html, levelPosts });
  }
  return { levelPosts: levelPosts.map((p) => p.level_dbfs), finalLevel: "-60.0 dBFS" };
}

async function testPartialRefreshKeepsSuccessfulSections() {
  const active = activePayloads();
  let failEnvironment = false;
  const fetchHandler = baseFetch({
    "./active-speaker/environment": () => {
      if (failEnvironment) return Promise.resolve(response({ error: "boom" }, false));
      return Promise.resolve(response(active["./active-speaker/environment"]));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  failEnvironment = true;

  harness.dispatchClick({ "data-act": "refresh-active-speaker" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "Partial refresh: boom",
    "Safety preflight",
    "Safety session",
    "Protected startup config",
    "Calibration level",
    "Commissioning rehearsal",
    "Path check sentinel",
  ]) {
    if (!html.includes(expected)) {
      fail("Partial active-speaker refresh should keep successful or previously known sections", { expected, html });
    }
  }
  return { partialRefreshPreservedSections: true };
}

const results = [];
const liveTabResult = await testLiveTabReplay();
results.push(liveTabResult);
results.push(await testRehearsalPreservedAcrossStartupActions());
results.push(await testStaleLevelResponseDiscarded());
results.push(await testPartialRefreshKeepsSuccessfulSections());

console.log(JSON.stringify(Object.assign({ ok: true, results }, liveTabResult)));
