// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

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
// http.js is the shared CSRF/JSON helper module. Inline its real definitions
// (with `export` stripped) so csrfHeaders()/jsonHeaders() behave exactly as in
// the browser. The lazy `import("/assets/shared/js/dialog.js")` inside
// promptForControlToken parses but never runs — main.js only calls the two
// header helpers, neither of which hits the token-prompt path.
const httpPath = new URL("../../deploy/assets/shared/js/http.js", import.meta.url);
const httpPreamble = readFileSync(httpPath, "utf8")
  .replace(/^export\s+\{[^}]+\};\s*$/gm, "")
  .replace(/^export\s+/gm, "");
const activeSpeakerUiPath = new URL("../../deploy/assets/sound-profile/js/active-speaker-ui.js", import.meta.url);
const activeSpeakerUiPreamble = readFileSync(activeSpeakerUiPath, "utf8")
  .replace(/^export\s+/gm, "");

function stripKnownImports(input) {
  return input
    .replace(/^import\s+\{\s*jtsConfirm\s+\}\s+from\s+["'][^"']+["'];\s*/m,
      "const jtsConfirm = async (...args) => globalThis.__jtsConfirm ? globalThis.__jtsConfirm(...args) : true;\n")
    .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*escape\.js["'];\s*/m, "")
    .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*http\.js["'];\s*/m, "")
    .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*active-speaker-ui\.js["'];\s*/m, "")
    .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*eq-math\.js["'];\s*/m, "");
}

const rawSource = readFileSync(modulePath, "utf8");
const unknownImportProbe = stripKnownImports(
  'import {\n  unknownHarnessDependency,\n} from "/assets/unknown.js";\n' + rawSource
);
if (!/^import\s+\{\s*unknownHarnessDependency/m.test(unknownImportProbe)) {
  throw new Error("known-import stripping swallowed an unknown multiline import");
}
const source = stripKnownImports(rawSource);
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
    attrs: {}, style: {}, _listeners: {}, _listenerCapture: {}, classList: classList(),
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
    },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(ev, fn, options) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
      const capture = options === true || !!(options && options.capture);
      (this._listenerCapture[ev] = this._listenerCapture[ev] || []).push(capture);
    },
    focus() { globalThis.document.activeElement = this; },
    select() {
      this.selectionStart = 0;
      this.selectionEnd = String(this.value || "").length;
    },
    setSelectionRange(start, end) {
      this.selectionStart = start;
      this.selectionEnd = end;
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

function response(payload, ok = true, status = ok ? 200 : 500) {
  return { ok, status, async json() { return payload; } };
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

function activeTwoWayTopologyPayload() {
  return {
    status: "valid",
    hardware: {
      physical_output_count: 2,
      profile_id: "test-dac",
      outputs: [
        { index: 0, human_label: "DAC output 1" },
        { index: 1, human_label: "DAC output 2" },
      ],
    },
    routing: { mono_group_id: "main", main_left_group_id: null, main_right_group_id: null, subwoofer_group_ids: [] },
    evaluation: { status: "valid" },
    speaker_groups: [{
      id: "main",
      label: "Main speaker",
      kind: "mono",
      mode: "active_2_way",
      position: { x: 0, y: 0, rotation_degrees: 0 },
      channels: [
        {
          role: "woofer",
          physical_output_index: 0,
          identity_verified: true,
          startup_muted: true,
          protection_required: false,
          protection_status: "not_required",
        },
        {
          role: "tweeter",
          physical_output_index: 1,
          identity_verified: true,
          startup_muted: true,
          protection_required: true,
          protection_status: "software_guard_requested",
        },
      ],
    }],
  };
}

function activeStereoTwoWayTopologyPayload() {
  const topology = activeTwoWayTopologyPayload();
  topology.hardware.physical_output_count = 4;
  topology.hardware.outputs = [0, 1, 2, 3].map((index) => ({
    index,
    human_label: `DAC output ${index + 1}`,
  }));
  topology.routing = {
    mono_group_id: null,
    main_left_group_id: "left",
    main_right_group_id: "right",
    subwoofer_group_ids: [],
  };
  topology.speaker_groups = [
    { id: "left", label: "Left cabinet", kind: "left", outputBase: 0 },
    { id: "right", label: "Right cabinet", kind: "right", outputBase: 2 },
  ].map((group) => ({
    id: group.id,
    label: group.label,
    kind: group.kind,
    mode: "active_2_way",
    position: { x: group.id === "left" ? -1 : 1, y: 0, rotation_degrees: 0 },
    channels: [
      {
        role: "woofer",
        physical_output_index: group.outputBase,
        identity_verified: true,
        startup_muted: true,
        protection_required: false,
        protection_status: "not_required",
      },
      {
        role: "tweeter",
        physical_output_index: group.outputBase + 1,
        identity_verified: true,
        startup_muted: true,
        protection_required: true,
        protection_status: "software_guard_requested",
      },
    ],
  }));
  return topology;
}

function activeTwoWayWithSubwooferTopologyPayload() {
  const topology = activeTwoWayTopologyPayload();
  topology.hardware.physical_output_count = 3;
  topology.hardware.outputs.push({ index: 2, human_label: "DAC output 3" });
  topology.routing.subwoofer_group_ids = ["sub"];
  topology.speaker_groups.push({
    id: "sub",
    label: "Subwoofer",
    kind: "subwoofer",
    mode: "subwoofer",
    position: { x: 0, y: -0.72, rotation_degrees: 0 },
    channels: [{
      role: "subwoofer",
      physical_output_index: 2,
      identity_verified: true,
      startup_muted: true,
      protection_required: false,
      protection_status: "not_required",
    }],
  });
  return topology;
}

function activeThreeWayTopologyPayload() {
  return {
    status: "valid",
    hardware: {
      physical_output_count: 3,
      profile_id: "test-dac",
      outputs: [
        { index: 0, human_label: "DAC output 1" },
        { index: 1, human_label: "DAC output 2" },
        { index: 2, human_label: "DAC output 3" },
      ],
    },
    routing: { mono_group_id: "main", main_left_group_id: null, main_right_group_id: null, subwoofer_group_ids: [] },
    evaluation: { status: "valid" },
    speaker_groups: [{
      id: "main",
      label: "Main speaker",
      kind: "mono",
      mode: "active_3_way",
      position: { x: 0, y: 0, rotation_degrees: 0 },
      channels: [
        {
          role: "woofer",
          physical_output_index: 0,
          identity_verified: true,
          startup_muted: true,
          protection_required: false,
          protection_status: "not_required",
        },
        {
          role: "mid",
          physical_output_index: 1,
          identity_verified: true,
          startup_muted: true,
          protection_required: false,
          protection_status: "not_required",
        },
        {
          role: "tweeter",
          physical_output_index: 2,
          identity_verified: true,
          startup_muted: true,
          protection_required: true,
          protection_status: "software_guard_requested",
        },
      ],
    }],
  };
}

function emptyTopologyPayload() {
  return {
    artifact_schema_version: 1,
    kind: "jts_output_topology",
    topology_id: "bench",
    name: "Bench output setup",
    status: "draft",
    hardware: {
      device_id: "hifiberry_dac8x",
      device_label: "HiFiBerry DAC8x",
      physical_output_count: 8,
      outputs: [
        { index: 0, human_label: "DAC output 1" },
        { index: 1, human_label: "DAC output 2" },
      ],
    },
    speaker_groups: [],
    routing: {},
    evaluation: {},
  };
}

// Single physical output (the Apple-dongle case) already consumed by a passive
// mono layout: no spare DAC channel for a LOCAL subwoofer, so the subwoofer
// add-on dead-ends and should offer the wireless-sub CTA instead.
function dongleMonoTopologyPayload() {
  return {
    status: "valid",
    hardware: {
      physical_output_count: 1,
      profile_id: "apple-dongle",
      outputs: [{ index: 0, human_label: "Headphone output" }],
    },
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

function activeRoutePayload(overrides = {}) {
  return {
    kind: "jts_active_speaker_playback_route_capability",
    playback_device: "outputd_active_content_playback",
    playback_device_source: "outputd_active_lane",
    transport_channel_count: 4,
    required_active_output_count: 0,
    active_group_count: 0,
    subwoofer_group_count: 0,
    subwoofer_supported: false,
    fits_required_outputs: true,
    ready: true,
    issues: [],
    ...overrides,
  };
}

function activePayloads() {
  const level = {
    status: "ready",
    test_signal: {
      min_level_dbfs: -80,
      max_level_dbfs: 0,
      step_db: 1,
      default_level_dbfs: -80,
      requested_level_dbfs: -72,
    },
    mic_meter: { status: "usable", recommendation: "hold_level" },
    software_gain_guard: { upward_step_limit_db: 1 },
    issues: [],
  };
  return {
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
    "./active-speaker/startup-load": {
      state: { status: "loaded", rollback_available: true, current_config_matches_loaded: true },
      preflight: { status: "ready", load_allowed: true, path_safety: { load_gate: "ready" }, candidate: { basename: "startup.yml" } },
    },
    "./active-speaker/measurements": {
      status: "not_applicable",
      summary: {
        required_driver_count: 0,
        captured_driver_count: 0,
        driver_checks_complete: false,
        driver_measurements_complete: false,
        latest_driver_checks: {},
        required_summed_group_count: 0,
        validated_summed_group_count: 0,
        summed_validation_complete: false,
        latest_driver_measurements: {},
        latest_summed_validations: {},
      },
      issues: [],
    },
    "./active-speaker/baseline-profile": {
      status: "blocked",
      permissions: { may_compile: false, may_apply: false },
      config: {},
      issues: [],
    },
    "./active-speaker/commission-state": {
      commission_load: { status: "idle", target: {}, rollback_available: false },
      ramp: { confirmed_roles: [], pending: null },
      floor: { status: "floor_required", floor_audio_confirmed: false },
    },
  };
}

function levelPayload(value) {
  return {
    status: "ready",
    test_signal: {
      min_level_dbfs: -80,
      max_level_dbfs: 0,
      step_db: 1,
      default_level_dbfs: -80,
      requested_level_dbfs: value,
    },
    mic_meter: { status: "usable", recommendation: "hold_level" },
    software_gain_guard: { upward_step_limit_db: 1 },
    issues: [],
  };
}

function commissioningSteps(currentStep, statuses = {}) {
  const labels = {
    layout: "Choose speaker layout",
    research: "Add driver and crossover values",
    map: "Confirm outputs",
    safety: "Test combined drivers",
    profile: "Validate and apply",
  };
  return ["layout", "research", "map", "safety", "profile"].map((id) => ({
    id,
    label: labels[id],
    status: statuses[id] || (id === currentStep ? "active" : "todo"),
    message: "",
  }));
}

function commissioningViewPayload(overrides = {}) {
  const currentStep = overrides.current_step || "layout";
  const stepStatuses = overrides.stepStatuses || {};
  const steps = overrides.steps || commissioningSteps(currentStep, stepStatuses);
  const payload = {
    artifact_schema_version: 1,
    kind: "jts_active_speaker_commissioning_view",
    status: overrides.status || "needs_layout",
    current_step: currentStep,
    steps,
    driver_values: {
      status: "ready",
      complete: true,
      design_ready: true,
      preview_ready: true,
      missing_driver_info_roles: [],
      missing_crossover_candidate_pairs: [],
      message: "Driver and crossover values are saved.",
    },
    output_identity: { assigned_channel_count: 2, unverified_channel_count: 0, complete: true },
    driver_checks: { complete: true, captured: 2, required: 2 },
    summed_validation: { complete: false, validated: 0, required: 1 },
    revalidation: {},
    test_level: levelPayload(-72).test_signal,
    combined_groups: [],
    next_action: {},
  };
  delete overrides.stepStatuses;
  return { ...payload, ...overrides, steps };
}

function profileCommissioningView(overrides = {}) {
  return commissioningViewPayload({
    current_step: "safety",
    stepStatuses: {
      layout: "done",
      research: "done",
      map: "done",
      safety: "active",
      profile: "todo",
    },
    status: "needs_combined_check",
    driver_target_proof: { complete: true, source: "measurements", captured: 2, required: 2 },
    ...overrides,
  });
}

function setupHarness(fetchHandler, options = {}) {
  const elements = new Map();
  const absent = new Set();
  for (const id of [
    "tab-off", "tab-saved", "tab-draft", "back", "view-body",
    "plot", "plot-summary", "live-label", "status",
  ]) {
    elements.set(id, makeEl(id));
  }
  if (options.follower) {
    // Reproduce the bonded-follower /sound/ DOM: the follower island is present,
    // and the content-EQ chrome (Off/Saved/Draft tabs + now-playing plot) is
    // omitted from the page. Making those ids resolve to null exercises the
    // module's follower-mode guards exactly as the browser would. islandText lets
    // a test inject a malformed island to prove the safe (follower) fallback.
    const island = makeEl("sound-follower-data");
    island.textContent = options.islandText !== undefined
      ? options.islandText
      : JSON.stringify({ follower: true });
    elements.set("sound-follower-data", island);
    for (const id of ["tab-off", "tab-saved", "tab-draft", "plot", "plot-summary", "live-label"]) {
      elements.delete(id);
      absent.add(id);
    }
  }

  globalThis.document = {
    _listeners: {},
    activeElement: null,
    body: {
      children: [],
      appendChild(node) {
        this.children.push(node);
        return node;
      },
      removeChild(node) {
        this.children = this.children.filter((child) => child !== node);
        return node;
      },
    },
    createElement(tagName) {
      const node = makeEl(String(tagName || "").toLowerCase());
      node.tagName = String(tagName || "").toUpperCase();
      return node;
    },
    getElementById(id) {
      if (absent.has(id)) return null;
      if (!elements.has(id)) elements.set(id, makeEl(id));
      return elements.get(id);
    },
    querySelector(sel) {
      return sel === "meta[name=jts-csrf]" ? { content: "csrf-token" } : null;
    },
    addEventListener(ev, fn) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
    },
    removeEventListener(ev, fn) {
      this._listeners[ev] = (this._listeners[ev] || []).filter((listener) => listener !== fn);
    },
  };
  globalThis.window = {
    _listeners: {},
    addEventListener(ev, fn) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
    },
    setTimeout,
    clearTimeout,
    location: { href: "" },
  };
  Object.defineProperty(globalThis, "navigator", {
    value: { clipboard: { async writeText() {} } },
    configurable: true,
  });
  delete globalThis.__jtsConfirm;
  globalThis.btoa = (binary) => Buffer.from(binary, "binary").toString("base64");
  globalThis.fetch = fetchHandler;

  new Function(
    escapePreamble + "\n" + httpPreamble + "\n" + eqMathPreamble + "\n" +
      activeSpeakerUiPreamble + "\n" + source
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
    if (target && target.id) {
      if (!elements.has(target.id)) elements.set(target.id, makeEl(target.id));
      elements.get(target.id).value = target.value || "";
      elements.get(target.id).checked = !!target.checked;
    }
    for (const fn of viewBody._listeners.change || []) {
      fn({ target });
    }
  };
  const dispatchToggle = (attrs) => {
    const target = {
      open: attrs.open !== undefined ? attrs.open : true,
      getAttribute(name) { return attrs[name] || ""; },
      matches(selector) {
        return selector === "[data-active-speaker-setup]" &&
          Object.prototype.hasOwnProperty.call(attrs, "data-active-speaker-setup");
      },
      classList: {
        contains(name) {
          return name === "output-step" &&
            Object.prototype.hasOwnProperty.call(attrs, "data-output-step");
        },
      },
    };
    for (const [index, fn] of (viewBody._listeners.toggle || []).entries()) {
      if (viewBody._listenerCapture.toggle?.[index]) fn({ target });
    }
    return target;
  };
  const dispatchInput = (attrs, value = "") => {
    const target = {
      id: attrs.id || "",
      value,
      getAttribute(name) { return attrs[name] || ""; },
      hasAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name); },
    };
    if (target.id) {
      if (!elements.has(target.id)) elements.set(target.id, makeEl(target.id));
      elements.get(target.id).value = value;
    }
    for (const fn of viewBody._listeners.input || []) {
      fn({ target });
    }
  };
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return { elements, dispatchClick, dispatchChange, dispatchToggle, dispatchInput, flush };
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
    if (path === "./active-speaker/commissioning-view") {
      return Promise.resolve(response(commissioningViewPayload({
        status: "needs_layout",
        current_step: "layout",
        stepStatuses: { layout: "active", research: "todo", map: "todo", safety: "todo", profile: "todo" },
        driver_values: {
          status: "not_saved",
          complete: false,
          design_ready: false,
          preview_ready: false,
          missing_driver_info_roles: [],
          missing_crossover_candidate_pairs: [],
          message: "Save driver and crossover values.",
        },
        output_identity: { assigned_channel_count: 0, unverified_channel_count: 0, complete: false },
        driver_checks: { complete: false, captured: 0, required: 0 },
      })));
    }
    if (path === "./preview") return Promise.resolve(response({ preview: [] }));
    if (active[path] && !options.method) return Promise.resolve(response(active[path]));
    throw new Error(`unexpected fetch: ${path}`);
  };
}

function fail(message, details = {}) {
  throw new Error(`${message}\n${JSON.stringify(details, null, 2)}`);
}

function commissionCardHtml(html) {
  const match = String(html || "").match(
    /<div class="commission-card">[\s\S]*?commission-card__followup[\s\S]*?<\/p><\/div>/
  );
  return match ? match[0] : String(html || "");
}

async function loadAndSetActiveState(harness) {
  await harness.flush();
  await harness.flush();
  await harness.flush();
}

function assertQuietTestSurfaceVisible(harness, label) {
  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of ["Test combined drivers"]) {
    if (!html.includes(expected)) {
      fail(`${label} should keep the combined-test surface visible`, { expected, html });
    }
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

async function testVolumeFloorRequiresExplicitSaveButAuditionsDraft() {
  const settingsPosts = [];
  const auditionPosts = [];
  const statePayload = {
    ...basePayload,
    profile: { ...flatProfile, enabled: false },
    filter_count: 0,
    dsp_write_epoch: "state-0",
    sound_settings: {
      ...basePayload.sound_settings,
      volume_floor_db: -50,
    },
  };
  const fetchHandler = baseFetch({
    "./state": () => Promise.resolve(response(statePayload)),
    "./apply": (_path, options = {}) => Promise.resolve(response({
      ...statePayload,
      profile: JSON.parse(options.body || "{}"),
      dsp_write_epoch: "apply-1",
    })),
    "./settings": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      settingsPosts.push(body);
      return Promise.resolve(response({
        ...statePayload,
        sound_settings: body,
        dsp_write_epoch: "settings-1",
      }));
    },
    "./volume-floor/audition": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      auditionPosts.push(body);
      return Promise.resolve(response({
        ok: true,
        active: true,
        continuous: true,
        status: auditionPosts.length === 1 ? "started" : "updated",
        volume_floor_db: body.volume_floor_db,
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush();
  await harness.flush();

  harness.elements.get("tab-saved").click();
  await harness.flush();
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('data-act="save-volume-floor"') || !html.includes(">Saved</button>")) {
    fail("volume floor should render an explicit saved/save button", { html });
  }

  harness.dispatchInput({ id: "set-volume-floor" }, "-42");
  await harness.flush();
  if (settingsPosts.length !== 0) {
    fail("dragging the volume floor must not persist settings", { settingsPosts });
  }

  harness.dispatchClick({ "data-act": "toggle-volume-floor-tone" });
  await harness.flush(); await harness.flush(); await harness.flush();
  if (auditionPosts.length !== 1 || auditionPosts[0].volume_floor_db !== -42) {
    fail("Start tone should audition the unsaved floor draft", { auditionPosts });
  }
  if (settingsPosts.length !== 0) {
    fail("auditioning the volume floor must not persist settings", { settingsPosts });
  }

  harness.dispatchClick({ "data-act": "save-volume-floor" });
  await harness.flush(); await harness.flush(); await harness.flush();
  if (settingsPosts.length !== 1 || settingsPosts[0].volume_floor_db !== -42) {
    fail("Save floor should persist the selected floor exactly once", { settingsPosts });
  }
  if (!harness.elements.get("status").textContent.includes("Volume floor saved.")) {
    fail("saving the volume floor should provide visible confirmation", {
      status: harness.elements.get("status").textContent,
    });
  }
  return { volumeFloorRequiresExplicitSaveButAuditionsDraft: true };
}

async function testQuietTestSurfaceSurvivesStartupActions() {
  const fetchHandler = baseFetch();
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  assertQuietTestSurfaceVisible(harness, "initial refresh");

  const html = harness.elements.get("view-body").innerHTML;
  for (const forbidden of [
    "data-act=\"check-active-path-safety\"",
    "data-act=\"load-active-startup\"",
    "data-act=\"stage-active-config\"",
    "Check path safety",
    "Continue setup",
  ]) {
    if (html.includes(forbidden)) {
      fail("Driver measurement flow should not expose internal startup actions", { forbidden, html });
    }
  }

  return { quietTestSurfacePreserved: true };
}

async function testPassiveLayoutsDoNotExposeDirectDriverTestFlow() {
  const fetchHandler = baseFetch();
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  for (const forbidden of [
    "data-act=\"check-output-readiness\"",
    "data-act=\"play-output-readiness-tone\"",
    "data-act=\"active-floor-result\"",
    "data-act=\"stop-active-speaker\"",
    "Test volume",
    "active-speaker-level\" min=",
    "I did not hear anything",
    "move this a little louder",
    "Did this driver make the sound?",
  ]) {
    if (html.includes(forbidden)) {
      fail("Passive layouts should not expose the removed direct test flow", { forbidden, html });
    }
  }
  return { passiveLayoutsDoNotExposeDirectDriverTestFlow: true };
}

async function testActiveCrossoverFirstStepRender() {
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(emptyTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "not_saved",
      summary: {},
      operator_inputs: {},
    })),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      status: "not_prepared",
      summary: {},
      groups: [],
      issues: [],
    })),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush();
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  const includes = (needle) => {
    if (!html.includes(needle)) fail(`Rendered active crossover flow should include ${needle}`, { html });
  };
  const excludes = (needle) => {
    if (html.includes(needle)) fail(`Rendered active crossover flow should not include ${needle}`, { html });
  };
  includes("Active crossover setup");
  includes("Choose speaker layout");
  includes("Add driver and crossover values");
  includes("Working setup");
  includes("AI helper");
  includes("2048 characters or fewer");
  includes("do not paste a full research report");
  includes("DAC output assignments");
  includes("Speaker count");
  includes("Speaker type");
  includes('data-output-step="layout" open');
  includes('data-output-step="research"');
  includes('data-output-step="map"');
  includes('data-output-step="safety"');
  includes('data-output-step="profile"');
  excludes('data-output-step="research" open');
  excludes('data-output-step="map" open');
  excludes('data-output-step="safety" open');
  excludes('data-output-step="profile" open');
  excludes("Save output map");
  excludes("Check readiness");
  excludes("Change protection");
  excludes("Hardware protected");
  excludes("Use software guard");
  excludes("Use quiet-start");
  excludes("Check setup");
  excludes("saved drivers");
  excludes("saved crossover settings");
  excludes("Save crossover settings");
  return { activeCrossoverFirstStepRendered: true };
}

async function testActiveSpeakerSetupTogglePersistsAcrossRender() {
  const harness = setupHarness(baseFetch());
  await loadAndSetActiveState(harness);

  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (initialHtml.includes("data-active-speaker-setup open")) {
    fail("settled passive setup should start collapsed", { initialHtml });
  }

  harness.dispatchToggle({ "data-active-speaker-setup": true, open: true });
  harness.dispatchClick({ "data-act": "browse-presets" });
  await harness.flush();
  await harness.flush();

  const rerenderedHtml = harness.elements.get("view-body").innerHTML;
  if (!rerenderedHtml.includes("data-active-speaker-setup open")) {
    fail("opening speaker setup should survive the next render", { rerenderedHtml });
  }
  return { activeSpeakerSetupTogglePersistsAcrossRender: true };
}

async function testActiveRouteLimitsRenderedTemplates() {
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: emptyTopologyPayload(),
      active_playback_route: activeRoutePayload(),
    })),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "not_saved",
      summary: {},
      operator_inputs: {},
    })),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      status: "not_prepared",
      summary: {},
      groups: [],
      issues: [],
    })),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush();
  await harness.flush();
  await harness.flush();

  harness.dispatchClick({
    "data-act": "output-template-axis",
    "data-axis": "layout",
    "data-value": "stereo",
  });
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("This install can test and apply up to 4 active outputs right now")) {
    fail("Stereo active 3-way should explain the active route width limit", { html });
  }
  if (!html.includes('data-value="active_3way" aria-pressed="false" disabled')) {
    fail("Stereo active 3-way should be disabled when the active route is four lanes", { html });
  }
  return { activeRouteLimitsRenderedTemplates: true };
}

async function testMeasuredDriversOpenProfileStep() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  confirmedTopology.channel_identity = {
    kind: "jts_output_channel_identity_report",
    status: "verified",
    assigned_channel_count: 2,
    verified_channel_count: 2,
    unverified_channel_count: 0,
    targets: [
      {
        id: "main:woofer",
        speaker_group_id: "main",
        speaker_label: "Main speaker",
        role: "woofer",
        assigned: true,
        identity_verified: true,
        physical_output_index: 0,
      },
      {
        id: "main:tweeter",
        speaker_group_id: "main",
        speaker_label: "Main speaker",
        role: "tweeter",
        assigned: true,
        identity_verified: true,
        physical_output_index: 1,
      },
    ],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
    })),
    "./active-speaker/measurements": () => Promise.resolve(response({
      status: "needs_summed_validation",
      summary: {
        required_driver_count: 2,
        captured_driver_count: 2,
        driver_measurements_complete: true,
        required_summed_group_count: 1,
        validated_summed_group_count: 0,
        summed_validation_complete: false,
        latest_driver_measurements: {
          "main:woofer": { captured: true, outcome: "heard_correct_driver" },
          "main:tweeter": { captured: true, outcome: "heard_correct_driver" },
        },
        latest_summed_tests: {
          main: {
            captured: false,
            audio_emitted: false,
            issues: [{
              severity: "blocker",
              code: "summed_commission_load_failed",
              message: "could not open the combined active-speaker test path",
            }],
          },
        },
        latest_summed_validations: {},
      },
      permissions: { may_compile_baseline: false },
      issues: [],
    })),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "ready_for_review",
      summary: { missing_driver_info_roles: [], missing_crossover_candidate_pairs: [] },
      operator_inputs: {},
    })),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      kind: "jts_active_speaker_crossover_preview",
      status: "ready_for_protected_staging",
      permissions: { may_prepare_protected_startup_config: true },
      issues: [],
    })),
    "./active-speaker/commissioning-view": () => Promise.resolve(response(profileCommissioningView({
      status: "needs_combined_check",
      test_level: levelPayload(-80).test_signal,
      combined_groups: [{
        group_id: "main",
        label: "Main speaker",
        status: "test_failed",
        status_label: "not tested",
        message: "JTS could not open the quiet combined-test path. Press Play combined test to retry.",
        failure_message: "JTS could not open the quiet combined-test path. Press Play combined test to retry.",
        actions: {
          start_combined_test: {
            id: "start_combined_test",
            label: "Play combined test",
            enabled: true,
            endpoint: "./active-speaker/summed-test",
            body: { speaker_group_id: "main", audio: true, stimulus: "speech", duration_ms: 12000 },
          },
          record_combined_result: {
            id: "record_combined_result",
            label: "Record combined check",
            enabled: false,
            endpoint: "./active-speaker/summed-validation",
            body: { speaker_group_id: "main", summed_test_id: "" },
          },
        },
      }],
    }))),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    'data-output-step="safety" open',
    "Test combined drivers",
    "Combined crossover check",
    "JTS could not open the quiet combined-test path. Press Play combined test to retry.",
    "Sounds right",
    "Back to adjust crossover",
  ]) {
    if (!html.includes(expected)) {
      fail("Completed driver proof should advance to the combined-test card", { expected, html });
    }
  }
  if (html.includes("could not open the combined active-speaker test path")) {
    fail("Combined-test implementation internals should not be the primary recovery copy", { html });
  }
  if (html.includes('data-output-step="profile" open')) {
    fail("Completed driver proof should not skip the combined-test card", { html });
  }
  return { measuredDriversOpenCombinedStep: true };
}

async function testAppliedProfileEditContinueOpensProfileStep() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  confirmedTopology.channel_identity = {
    kind: "jts_output_channel_identity_report",
    status: "verified",
    assigned_channel_count: 2,
    verified_channel_count: 2,
    unverified_channel_count: 0,
    targets: [],
  };
  const measurements = {
    status: "needs_driver_measurements",
    summary: {
      required_driver_count: 2,
      captured_driver_count: 0,
      driver_checks_complete: false,
      driver_measurements_complete: false,
      required_summed_group_count: 1,
      validated_summed_group_count: 0,
      summed_validation_complete: false,
      latest_driver_checks: {
        "main:woofer": { speaker_group_id: "main", role: "woofer", captured: true },
        "main:tweeter": { speaker_group_id: "main", role: "tweeter", captured: true },
      },
      latest_driver_measurements: {},
      latest_summed_tests: {},
      latest_summed_validations: {},
    },
    issues: [],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
    })),
    "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "ready_for_review",
      summary: { missing_driver_info_roles: [], missing_crossover_candidate_pairs: [] },
      operator_inputs: {},
    })),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      kind: "jts_active_speaker_crossover_preview",
      status: "ready_for_protected_staging",
      permissions: { may_prepare_protected_startup_config: true },
      issues: [],
    })),
    "./active-speaker/commissioning-view": () => Promise.resolve(response(profileCommissioningView({
      status: "needs_revalidation",
      driver_target_proof: {
        complete: true,
        source: "applied_profile_revalidation",
        captured: 0,
        required: 2,
      },
      driver_checks: {
        complete: true,
        source: "applied_profile_revalidation",
        captured: 0,
        required: 2,
      },
      revalidation: {
        required: true,
        reason: "applied_profile_superseded",
        next_step: "combined_check",
      },
      combined_groups: [{
        group_id: "main",
        label: "Main speaker",
        status: "ready_to_test",
        status_label: "next",
        message: "Run the combined speaker test.",
        failure_message: "",
        actions: {
          start_combined_test: {
            id: "start_combined_test",
            label: "Play combined test",
            enabled: true,
            endpoint: "./active-speaker/summed-test",
            body: { speaker_group_id: "main", audio: true, stimulus: "speech", duration_ms: 12000 },
          },
          record_combined_result: {
            id: "record_combined_result",
            label: "Record combined check",
            enabled: false,
            endpoint: "./active-speaker/summed-validation",
            body: { speaker_group_id: "main", summed_test_id: "" },
          },
        },
      }],
    }))),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (!initialHtml.includes('data-output-step="safety" open')) {
    fail("applied-profile edit should open the combined-test card", {
      initialHtml,
    });
  }

  harness.dispatchClick({ "data-act": "output-step-next", "data-step": "safety" });
  await harness.flush();
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('data-output-step="safety" open')) {
    fail("combined-test Continue should stay put until validation is saved", {
      html,
      status: harness.elements.get("status").textContent,
    });
  }
  return { appliedProfileEditOpensCombinedStep: true };
}

async function testCombinedTestLevelPostsSelectedBoundedLevel() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  confirmedTopology.channel_identity = {
    kind: "jts_output_channel_identity_report",
    status: "verified",
    assigned_channel_count: 2,
    verified_channel_count: 2,
    unverified_channel_count: 0,
    targets: [],
  };
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
    })),
    "./active-speaker/measurements": () => Promise.resolve(response({
      status: "needs_summed_validation",
      summary: {
        required_driver_count: 2,
        captured_driver_count: 2,
        driver_measurements_complete: true,
        required_summed_group_count: 1,
        validated_summed_group_count: 0,
        summed_validation_complete: false,
        latest_driver_measurements: {
          "main:woofer": { captured: true, outcome: "heard_correct_driver" },
          "main:tweeter": { captured: true, outcome: "heard_correct_driver" },
        },
        latest_summed_tests: {},
        latest_summed_validations: {},
      },
      permissions: { may_compile_baseline: false },
      issues: [],
    })),
    "./active-speaker/commissioning-view": () => Promise.resolve(response({
      status: "needs_combined_check",
      test_level: {
        requested_level_dbfs: -72,
        min_level_dbfs: -80,
        max_level_dbfs: 0,
        step_db: 1,
        upward_step_limit_db: 6,
      },
      combined_groups: [{
        group_id: "main",
        label: "Main speaker",
        status: "ready_to_test",
        status_label: "next",
        message: "Run the combined speaker test.",
        actions: {
          start_combined_test: {
            id: "start_combined_test",
            label: "Play combined test",
            enabled: true,
            endpoint: "./active-speaker/summed-test",
            body: { speaker_group_id: "main", audio: true, stimulus: "speech", duration_ms: 12000 },
          },
          record_combined_result: {
            id: "record_combined_result",
            label: "Record combined check",
            enabled: false,
            endpoint: "./active-speaker/summed-validation",
            body: { speaker_group_id: "main", summed_test_id: "" },
          },
        },
      }],
    })),
    "./active-speaker/summed-test": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      posts.push(body);
      return Promise.resolve(response({
        playback: {
          status: "completed",
          audio_emitted: true,
          confirmable: true,
          tone: { level_dbfs: body.level_dbfs },
        },
        calibration_level: levelPayload(body.level_dbfs),
        measurements: {
          status: "needs_summed_validation",
          summary: {
            driver_measurements_complete: true,
            summed_validation_complete: false,
            latest_summed_tests: {
              main: {
                captured: true,
                audio_emitted: true,
                summed_test_id: "summed-playback-1",
                issues: [],
              },
            },
            latest_summed_validations: {},
          },
        },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  let html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Combined test level") || !html.includes('min="-80"') || !html.includes('max="0"')) {
    fail("Combined card should expose the full commissioning level envelope", { html });
  }
  harness.dispatchInput({ "data-summed-test-level": "main" }, "-40");
  harness.dispatchClick({
    "data-act": "prepare-summed-test",
    "data-group-id": "main",
    "data-label": "Main speaker",
  });
  for (let i = 0; i < 8; i += 1) await harness.flush();

  if (posts.length !== 1) {
    fail("Playing the combined test should POST once", { posts });
  }
  if (posts[0].level_dbfs !== -40) {
    fail("Combined test should POST the selected level inside the envelope", { posts });
  }
  if (posts[0].stimulus !== "speech" || posts[0].duration_ms !== 12000) {
    fail("Combined test should request the looped speech stimulus", { posts });
  }
  return { combinedTestLevelPostsSelectedBoundedLevel: true };
}

async function testCombinedTestButtonStopsActiveRequest() {
  const start = deferred();
  const stopPosts = [];
  const levelPosts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/measurements": () => Promise.resolve(response({
      status: "needs_summed_validation",
      summary: {
        driver_measurements_complete: true,
        validated_summed_group_count: 0,
        summed_validation_complete: false,
        latest_driver_measurements: {
          "main:woofer": { captured: true, outcome: "heard_correct_driver" },
          "main:tweeter": { captured: true, outcome: "heard_correct_driver" },
        },
        latest_summed_tests: {
          main: {
            captured: false,
            audio_emitted: false,
            summed_test_id: "summed-playback-stale",
            playback_id: "summed-playback-stale",
            issues: [{
              severity: "blocker",
              code: "summed_test_playback_incomplete",
              message: "combined test did not complete",
            }],
          },
        },
        latest_summed_validations: {},
      },
      permissions: {},
      issues: [],
    })),
    "./active-speaker/commissioning-view": () => Promise.resolve(response({
      status: "needs_combined_check",
      test_level: {
        requested_level_dbfs: -72,
        min_level_dbfs: -80,
        max_level_dbfs: 0,
        step_db: 1,
      },
      combined_groups: [{
        group_id: "main",
        label: "Main speaker",
        status: "test_failed",
        status_label: "not tested",
        message: "The combined test did not finish. Press Play combined test to retry.",
        failure_message: "The combined test did not finish. Press Play combined test to retry.",
        latest_test_id: "summed-playback-stale",
        has_audible_test: false,
        actions: {
          start_combined_test: {
            id: "start_combined_test",
            label: "Play combined test",
            enabled: true,
            endpoint: "./active-speaker/summed-test",
            body: { speaker_group_id: "main", audio: true, stimulus: "speech", duration_ms: 12000 },
          },
        },
      }],
    })),
    "./active-speaker/summed-test": () => start.promise,
    "./active-speaker/summed-test/level": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      levelPosts.push(body);
      return Promise.resolve(response({
        status: "loaded",
        speaker_group_id: body.speaker_group_id,
        playback_id: "summed-playback-1",
        calibration_level: levelPayload(body.level_dbfs),
        commissioning_load: { load: { status: "loaded" } },
      }));
    },
    "./active-speaker/summed-test/stop": (_path, options = {}) => {
      stopPosts.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({ status: "stopped", reason: "operator_stop" }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const originalSetTimeout = globalThis.window.setTimeout;
  globalThis.window.setTimeout = (fn, ms) => {
    if (ms === 250 || ms === 120) {
      queueMicrotask(fn);
      return 1;
    }
    return originalSetTimeout(fn, ms);
  };
  try {
    harness.dispatchClick({
      "data-act": "prepare-summed-test",
      "data-group-id": "main",
      "data-label": "Main speaker",
    });
    await harness.flush(); await harness.flush(); await harness.flush();
    let html = harness.elements.get("view-body").innerHTML;
    if (!html.includes('data-act="stop-summed-test"') || !html.includes("btn--danger")) {
      fail("combined test should turn into a fixed red Stop action while active", { html });
    }
    const slider = html.match(/<input type="range" data-summed-test-level="main"[^>]*>/);
    if (!slider || slider[0].includes("disabled") ||
        html.includes("Stop and replay the test audio to use a different level.") ||
        !html.includes("Changes apply while the test audio is playing.")) {
      fail("combined level slider should stay live while test audio is playing", { html, slider });
    }
    const soundsRight = html.match(/<button type="button" class="btn btn--primary" [^>]*data-act="record-summed-validation"[^>]*>Sounds right<\/button>/);
    if (!soundsRight || soundsRight[0].includes("disabled")) {
      fail("Sounds right should stay available while test audio is playing", { html, soundsRight });
    }
    harness.dispatchInput({ "data-summed-test-level": "main" }, "-35");
    await harness.flush(); await harness.flush(); await harness.flush();
    if (levelPosts.length !== 1 || levelPosts[0].level_dbfs !== -35) {
      fail("dragging the combined slider while playing should update the active test level", { levelPosts });
    }
    harness.dispatchClick({ "data-act": "stop-summed-test", "data-group-id": "main" });
    await harness.flush(); await harness.flush(); await harness.flush();
  } finally {
    globalThis.window.setTimeout = originalSetTimeout;
  }
  if (stopPosts.length !== 1 || stopPosts[0].reason !== "operator_stop") {
    fail("Stop should post to the combined-test stop endpoint once", { stopPosts });
  }
  start.resolve(response({
    playback: { status: "stopped", audio_emitted: false, confirmable: false },
    calibration_level: levelPayload(-40),
    measurements: { status: "needs_summed_validation", summary: {} },
  }));
  await harness.flush(); await harness.flush();
  return { combinedTestButtonStopsActiveRequest: true };
}

async function testReloadedPageRendersReloadSafeStopForActiveTest() {
  // Regression for the jts3 2026-07-06 incident: a combined test kept looping
  // ("Like and subscribe to Jasper tech") but a reloaded /sound/ page showed
  // "Play combined test" with no way to stop it. The fix surfaces the live
  // session on the commissioning view (summed_test_active) so any page load can
  // render Stop — even a tab that never clicked Play.
  const stopPosts = [];
  let testActive = true;
  const commissioningView = () => response({
    status: "needs_combined_check",
    test_level: {
      requested_level_dbfs: -72,
      min_level_dbfs: -80,
      max_level_dbfs: 0,
      step_db: 1,
    },
    active_summed_test: testActive
      ? {
          active: true,
          speaker_group_id: "main",
          playback_id: "summed-playback-1",
          level_dbfs: -30,
        }
      : { active: false },
    combined_groups: [{
      group_id: "main",
      label: "Main speaker",
      status: "ready_to_test",
      status_label: testActive ? "playing" : "next",
      message: "Run the combined speaker test.",
      summed_test_active: testActive,
      actions: {
        start_combined_test: {
          id: "start_combined_test",
          label: "Play combined test",
          enabled: true,
          endpoint: "./active-speaker/summed-test",
          body: { speaker_group_id: "main", audio: true, stimulus: "speech", duration_ms: 12000 },
        },
      },
    }],
  });
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/measurements": () => Promise.resolve(response({
      status: "needs_summed_validation",
      summary: {
        driver_measurements_complete: true,
        validated_summed_group_count: 0,
        summed_validation_complete: false,
        latest_driver_measurements: {
          "main:woofer": { captured: true, outcome: "heard_correct_driver" },
          "main:tweeter": { captured: true, outcome: "heard_correct_driver" },
        },
        latest_summed_tests: {},
        latest_summed_validations: {},
      },
      permissions: {},
      issues: [],
    })),
    "./active-speaker/commissioning-view": () => Promise.resolve(commissioningView()),
    "./active-speaker/summed-test/stop": (_path, options = {}) => {
      stopPosts.push(JSON.parse(options.body || "{}"));
      testActive = false;
      return Promise.resolve(response({ status: "stopped", reason: "operator_stop" }));
    },
  });
  const harness = setupHarness(fetchHandler);
  // Fresh page load only — this tab NEVER clicked "Play combined test", so the
  // Stop control must come purely from the server's summed_test_active flag.
  await loadAndSetActiveState(harness);

  let html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('data-act="stop-summed-test"') || !html.includes("btn--danger")) {
    fail("a reloaded page with a live combined test should render Stop", { html });
  }
  if (html.includes('data-act="prepare-summed-test"')) {
    fail("a reloaded page with a live combined test must not offer Play", { html });
  }

  harness.dispatchClick({ "data-act": "stop-summed-test", "data-group-id": "main" });
  await harness.flush(); await harness.flush(); await harness.flush();
  if (stopPosts.length !== 1 || stopPosts[0].reason !== "operator_stop") {
    fail("the reload-safe Stop should post to the stop endpoint once", { stopPosts });
  }
  html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('data-act="prepare-summed-test"') ||
      html.includes('data-act="stop-summed-test"')) {
    fail("after Stop the card should return to Play once the server clears the test", { html });
  }
  return { reloadedPageRendersReloadSafeStop: true };
}

async function testCombinedSoundsRightStopsAndSavesActiveLoop() {
  const start = deferred();
  const stopPosts = [];
  const validationPosts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/measurements": () => Promise.resolve(response({
      status: "needs_summed_validation",
      summary: {
        driver_measurements_complete: true,
        validated_summed_group_count: 0,
        summed_validation_complete: false,
        latest_driver_measurements: {
          "main:woofer": { captured: true, outcome: "heard_correct_driver" },
          "main:tweeter": { captured: true, outcome: "heard_correct_driver" },
        },
        latest_summed_tests: {},
        latest_summed_validations: {},
      },
      permissions: {},
      issues: [],
    })),
    "./active-speaker/commissioning-view": () => Promise.resolve(response({
      status: "needs_combined_check",
      test_level: {
        requested_level_dbfs: -72,
        min_level_dbfs: -80,
        max_level_dbfs: 0,
        step_db: 1,
      },
      combined_groups: [{
        group_id: "main",
        label: "Main speaker",
        status: "ready_to_test",
        status_label: "next",
        message: "Run the combined speaker test.",
        actions: {
          start_combined_test: {
            id: "start_combined_test",
            label: "Play combined test",
            enabled: true,
            endpoint: "./active-speaker/summed-test",
            body: { speaker_group_id: "main", audio: true, stimulus: "speech", duration_ms: 12000 },
          },
          record_combined_result: {
            id: "record_combined_result",
            label: "Record combined check",
            enabled: false,
            endpoint: "./active-speaker/summed-validation",
            body: { speaker_group_id: "main", summed_test_id: "summed-playback-stale" },
          },
        },
      }],
    })),
    "./active-speaker/summed-test": () => start.promise,
    "./active-speaker/summed-test/stop": (_path, options = {}) => {
      stopPosts.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({
        status: "stopped",
        reason: "operator_confirmed",
        playback_id: "summed-playback-1",
      }));
    },
    "./active-speaker/summed-validation": (_path, options = {}) => {
      validationPosts.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({
        status: "complete",
        summary: {
          driver_measurements_complete: true,
          summed_validation_complete: true,
          latest_summed_tests: {
            main: {
              captured: true,
              audio_emitted: true,
              summed_test_id: "summed-playback-1",
            },
          },
          latest_summed_validations: {
            main: {
              captured: true,
              validated: true,
              summed_test_id: "summed-playback-1",
            },
          },
        },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const originalSetTimeout = globalThis.window.setTimeout;
  globalThis.window.setTimeout = (fn, ms) => {
    if (ms === 250) {
      queueMicrotask(fn);
      return 1;
    }
    return originalSetTimeout(fn, ms);
  };
  try {
    harness.dispatchClick({
      "data-act": "prepare-summed-test",
      "data-group-id": "main",
      "data-label": "Main speaker",
    });
    await harness.flush(); await harness.flush(); await harness.flush();
    const html = harness.elements.get("view-body").innerHTML;
    const soundsRight = html.match(/<button type="button" class="btn btn--primary" [^>]*data-act="record-summed-validation"[^>]*>Sounds right<\/button>/);
    if (!soundsRight || soundsRight[0].includes('data-summed-test-id="summed-playback-stale"')) {
      fail("active Sounds right must not carry a stale summed test id", { html, soundsRight });
    }
    harness.dispatchClick({
      "data-act": "record-summed-validation",
      "data-group-id": "main",
      "data-summed-test-id": "summed-playback-stale",
      "data-outcome": "blend_ok",
    });
    await harness.flush(); await harness.flush();
    if (stopPosts.length !== 1 || stopPosts[0].reason !== "operator_confirmed") {
      fail("Sounds right while playing should stop with a confirmation reason", { stopPosts });
    }
    start.resolve(response({
      playback: {
        status: "completed",
        audio_emitted: true,
        confirmable: true,
        playback_id: "summed-playback-1",
        tone: { level_dbfs: -72 },
      },
      calibration_level: levelPayload(-72),
      measurements: {
        status: "needs_summed_validation",
        summary: {
          driver_measurements_complete: true,
          summed_validation_complete: false,
          latest_summed_tests: {
            main: {
              captured: true,
              audio_emitted: true,
              summed_test_id: "summed-playback-1",
              playback_id: "summed-playback-1",
              issues: [],
            },
          },
          latest_summed_validations: {},
        },
      },
    }));
    for (let i = 0; i < 10; i += 1) await harness.flush();
  } finally {
    globalThis.window.setTimeout = originalSetTimeout;
  }
  if (validationPosts.length !== 1 ||
      validationPosts[0].summed_test_id !== "summed-playback-1" ||
      validationPosts[0].operator_listening_check !== true) {
    fail("Sounds right should save the confirmed active summed test", { validationPosts });
  }
  return { combinedSoundsRightStopsAndSavesActiveLoop: true };
}

async function testStaleSummedValidationDoesNotRenderValidatedGroup() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  const measurements = {
    status: "needs_summed_validation",
    summary: {
      ...summedSummary({
        main: {
          captured: true,
          audio_emitted: true,
          summed_test_id: "sum-2",
          playback_id: "sum-2",
        },
      }),
      latest_summed_validations: {
        main: { validated: true, outcome: "blend_ok", summed_test_id: "sum-1" },
      },
    },
    permissions: {},
    issues: [],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(confirmedTopology)),
    "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
    "./active-speaker/commissioning-view": () => Promise.resolve(response({
      status: "needs_combined_check",
      test_level: {
        requested_level_dbfs: -72,
        min_level_dbfs: -80,
        max_level_dbfs: 0,
        step_db: 1,
      },
      combined_groups: [{
        group_id: "main",
        label: "Main speaker",
        status: "ready_to_record",
        status_label: "ready to record",
        message: "Combined speaker test played. Record what you heard.",
        latest_test_id: "sum-2",
        has_audible_test: true,
        validated: false,
        actions: {
          start_combined_test: {
            id: "start_combined_test",
            label: "Play combined test",
            enabled: true,
            endpoint: "./active-speaker/summed-test",
            body: { speaker_group_id: "main", audio: true, stimulus: "speech", duration_ms: 12000 },
          },
          record_combined_result: {
            id: "record_combined_result",
            label: "Record combined check",
            enabled: true,
            endpoint: "./active-speaker/summed-validation",
            body: { speaker_group_id: "main", summed_test_id: "sum-2", operator_listening_check: true },
          },
        },
      }],
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes(">validated</span>")) {
    fail("stale summed validation should not render as current validated state", { html });
  }
  if (!html.includes(">ready to record</span>") ||
      !html.includes('data-summed-test-id="sum-2"') ||
      html.includes('data-summed-test-id="sum-1"')) {
    fail("combined result should point at the latest audible test", { html });
  }
  return { staleSummedValidationDoesNotRenderValidatedGroup: true };
}

async function testTwoOutputChannelSelectorAutoAssignsPeerOnSave() {
  const topology = activeTwoWayTopologyPayload();
  topology.hardware.physical_output_count = 8;
  topology.hardware.outputs = Array.from({ length: 8 }, (_unused, index) => ({
    index,
    human_label: `DAC output ${index + 1}`,
  }));
  topology.speaker_groups[0].channels[0].human_output_label = "Old woofer label";
  topology.speaker_groups[0].channels[1].human_output_label = "Old tweeter label";
  const saves = [];
  const fetchHandler = baseFetch({
    "./output-topology": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        saves.push(body.output_topology);
        return Promise.resolve(response({
          output_topology: body.output_topology,
          topology_revision: "saved-1",
        }));
      }
      return Promise.resolve(response(topology));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (!initialHtml.includes("swaps with Main speaker · Tweeter")) {
    fail("two-output selector should allow swapping with the peer channel", { initialHtml });
  }

  harness.dispatchChange({
    value: "1",
    getAttribute(name) {
      return { "data-group-id": "main", "data-role": "woofer" }[name] || "";
    },
    hasAttribute(name) { return name === "data-output-channel"; },
  });
  await harness.flush();
  harness.dispatchClick({ "data-act": "save-output-topology" });
  await harness.flush(); await harness.flush(); await harness.flush();

  if (saves.length !== 1) fail("selector save should POST one topology", { saves });
  const channels = saves[0].speaker_groups[0].channels;
  const byRole = Object.fromEntries(channels.map((channel) => [channel.role, channel]));
  if (byRole.woofer.physical_output_index !== 1 ||
      byRole.tweeter.physical_output_index !== 0) {
    fail("two-output selector should auto-assign the peer to the remaining channel", { channels });
  }
  if (byRole.woofer.identity_verified !== false ||
      byRole.tweeter.identity_verified !== false) {
    fail("changing channel assignment should clear identity verification", { channels });
  }
  if ("human_output_label" in byRole.woofer || "human_output_label" in byRole.tweeter) {
    fail("changing channel assignment should clear stale human labels", { channels });
  }
  return { twoOutputChannelSelectorAutoAssignsPeerOnSave: true };
}

async function testChannelSelectorKeepsConfirmOutputsOpenWhenDraftDirty() {
  const topology = activeTwoWayTopologyPayload();
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: topology,
    })),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "ready_for_review",
      summary: { missing_driver_info_roles: [], missing_crossover_candidate_pairs: [] },
      operator_inputs: {},
    })),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      kind: "jts_active_speaker_crossover_preview",
      status: "ready_for_protected_staging",
      permissions: { may_prepare_protected_startup_config: true },
      issues: [],
    })),
    "./active-speaker/commissioning-view": () => Promise.resolve(response(commissioningViewPayload({
      status: "needs_output_confirmation",
      current_step: "map",
      stepStatuses: {
        layout: "done",
        research: "done",
        map: "active",
        safety: "todo",
        profile: "todo",
      },
      output_identity: { assigned_channel_count: 2, unverified_channel_count: 2, complete: false },
      driver_checks: { complete: false, captured: 0, required: 2 },
    }))),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (!initialHtml.includes('data-output-step="map" open')) {
    fail("unconfirmed active outputs should start on the Confirm outputs card", { initialHtml });
  }

  harness.dispatchChange({
    value: "1",
    getAttribute(name) {
      return { "data-group-id": "main", "data-role": "woofer" }[name] || "";
    },
    hasAttribute(name) { return name === "data-output-channel"; },
  });
  await harness.flush();

  const dirtyHtml = harness.elements.get("view-body").innerHTML;
  if (!dirtyHtml.includes('data-output-step="map" open')) {
    fail("changing a DAC assignment should keep Confirm outputs open for saving", { dirtyHtml });
  }
  if (!dirtyHtml.includes('data-act="save-output-topology"') || !dirtyHtml.includes(">Save</button>")) {
    fail("dirty channel assignment should expose the save action in Confirm outputs", { dirtyHtml });
  }
  if (dirtyHtml.includes('data-output-step="layout" open')) {
    fail("changing a DAC assignment should not bounce back to Choose speaker layout", { dirtyHtml });
  }
  const reopened = harness.dispatchToggle({
    "data-output-step": "map",
    open: true,
  });
  if (!reopened.open) {
    fail("dirty Confirm outputs should remain reopenable until the draft is saved", { dirtyHtml });
  }
  return { channelSelectorKeepsConfirmOutputsOpenWhenDraftDirty: true };
}

async function testConfirmOutputsPlayUsesIdentityAuditionMode() {
  const topology = activeTwoWayTopologyPayload();
  topology.speaker_groups[0].channels.forEach((channel) => {
    channel.identity_verified = false;
  });
  let commissionState = {
    commission_load: { status: "idle", target: {}, rollback_available: false },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: topology,
    })),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "ready_for_review",
      summary: { missing_driver_info_roles: [], missing_crossover_candidate_pairs: [] },
      operator_inputs: {},
    })),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      kind: "jts_active_speaker_crossover_preview",
      status: "ready_for_protected_staging",
      permissions: { may_prepare_protected_startup_config: true },
      issues: [],
    })),
    "./active-speaker/commissioning-view": () => Promise.resolve(response(commissioningViewPayload({
      status: "needs_output_confirmation",
      current_step: "map",
      stepStatuses: {
        layout: "done",
        research: "done",
        map: "active",
        safety: "todo",
        profile: "todo",
      },
      output_identity: { assigned_channel_count: 2, unverified_channel_count: 2, complete: false },
      driver_checks: { complete: false, captured: 0, required: 2 },
    }))),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-load": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      commissionState = {
        commission_load: {
          status: "loaded",
          target: { role: body.role, audible_gain_db: -120 },
          rollback_available: true,
        },
        ramp: { confirmed_roles: [], pending: null },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({
        status: "loaded",
        load: { status: "loaded", target: { role: body.role } },
      }));
    },
    "./active-speaker/commission-ramp-step": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      commissionState = {
        commission_load: {
          status: "loaded",
          target: { role: body.role, audible_gain_db: -80 },
          rollback_available: true,
        },
        ramp: {
          confirmed_roles: [],
          pending: { role: body.role, gain_db: -80, frequency_hz: 120 },
        },
        floor: { status: "floor_pending_operator", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ status: "stepped", next_gain_db: -80 }));
    },
    "./active-speaker/commission-ramp-abort": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      commissionState = {
        commission_load: { status: "rolled_back", target: {}, rollback_available: false },
        ramp: { confirmed_roles: [], pending: null },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ status: "rolled_back" }));
    },
    "./active-speaker/channel-identity": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      topology.speaker_groups[0].channels.forEach((channel) => {
        if (channel.role === body.role) {
          channel.identity_verified = !!body.identity_verified;
        }
      });
      return Promise.resolve(response({ output_topology: topology }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('data-output-step="map" open')) {
    fail("unconfirmed active outputs should start on Confirm outputs", { html });
  }
  if (!html.includes('data-identity-audition="true"')) {
    fail("Confirm outputs Play button should be explicitly marked as identity audition", { html });
  }

  harness.dispatchClick({
    "data-act": "commission-step",
    "data-role": "woofer",
    "data-identity-audition": "true",
  });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();

  const load = posts.find((x) => x.path === "./active-speaker/commission-load");
  const step = posts.find((x) => x.path === "./active-speaker/commission-ramp-step");
  if (!load || load.body.identity_audition !== true) {
    fail("Confirm outputs Play should arm using identity-audition mode", { posts });
  }
  if (!step || step.body.identity_audition !== true) {
    fail("Confirm outputs Play should ramp using identity-audition mode", { posts });
  }
  globalThis.__jtsConfirm = async () => {
    posts.push({ path: "dialog-confirm" });
    return true;
  };
  harness.dispatchClick({
    "data-act": "mark-output-identity",
    "data-group-id": "main",
    "data-role": "woofer",
    "data-label": "Main speaker Woofer on DAC output 1",
  });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();
  const abortIndex = posts.findIndex((x) => x.path === "./active-speaker/commission-ramp-abort");
  const confirmIndex = posts.findIndex((x) => x.path === "dialog-confirm");
  const identityIndex = posts.findIndex((x) => x.path === "./active-speaker/channel-identity");
  if (abortIndex < 0 || confirmIndex < 0 || identityIndex < 0 ||
      abortIndex > confirmIndex || confirmIndex > identityIndex) {
    fail("Confirming output during audition should remute before dialog and identity save", { posts });
  }
  const afterConfirmHtml = harness.elements.get("view-body").innerHTML;
  if (afterConfirmHtml.includes('data-role="tweeter" disabled')) {
    fail("Confirming one output should not leave sibling audition controls disabled", { afterConfirmHtml });
  }
  return { confirmOutputsPlayUsesIdentityAuditionMode: true };
}

async function testConfirmOutputAbortsPendingAuditionWithoutAutoRamp() {
  const topology = activeTwoWayTopologyPayload();
  topology.speaker_groups[0].channels.forEach((channel) => {
    channel.identity_verified = false;
  });
  let commissionState = {
    commission_load: {
      status: "loaded",
      target: { speaker_group_id: "main", role: "tweeter", audible_gain_db: -80 },
      rollback_available: true,
    },
    ramp: {
      confirmed_roles: [],
      pending: { role: "tweeter", gain_db: -80, frequency_hz: 120 },
    },
    floor: { status: "floor_pending_operator", floor_audio_confirmed: false },
  };
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: topology,
    })),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-ramp-abort": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      commissionState = {
        commission_load: {
          status: "rolled_back",
          target: {},
          rollback_available: false,
        },
        ramp: { confirmed_roles: [], pending: null },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ status: "rolled_back" }));
    },
    "./active-speaker/channel-identity": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      topology.speaker_groups[0].channels.forEach((channel) => {
        if (channel.role === body.role) {
          channel.identity_verified = !!body.identity_verified;
        }
      });
      return Promise.resolve(response({ output_topology: topology }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes(">Stop</button>") || !html.includes('data-role="woofer" disabled')) {
    fail("Fixture should start with tweeter audition pending and woofer play disabled", { html });
  }
  globalThis.__jtsConfirm = async () => {
    posts.push({ path: "dialog-confirm" });
    return true;
  };

  harness.dispatchClick({
    "data-act": "mark-output-identity",
    "data-group-id": "main",
    "data-role": "tweeter",
    "data-label": "Main speaker Tweeter on DAC output 2",
  });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();

  const abortIndex = posts.findIndex((x) => x.path === "./active-speaker/commission-ramp-abort");
  const confirmIndex = posts.findIndex((x) => x.path === "dialog-confirm");
  const identityIndex = posts.findIndex((x) => x.path === "./active-speaker/channel-identity");
  if (abortIndex < 0 || confirmIndex < 0 || identityIndex < 0 ||
      abortIndex > confirmIndex || confirmIndex > identityIndex) {
    fail("Confirming output with a pending audition should remute before dialog and identity save", { posts });
  }
  const afterConfirmHtml = harness.elements.get("view-body").innerHTML;
  if (afterConfirmHtml.includes(">Stop</button>") ||
      afterConfirmHtml.includes('data-role="woofer" disabled')) {
    fail("Confirming output should clear the pending audition and re-enable siblings", {
      afterConfirmHtml,
    });
  }
  return { confirmOutputAbortsPendingAuditionWithoutAutoRamp: true };
}

async function testThreeOutputChannelSelectorDoesNotAutoAssignPeers() {
  const topology = activeThreeWayTopologyPayload();
  topology.speaker_groups[0].channels[0].physical_output_index = null;
  const saves = [];
  const fetchHandler = baseFetch({
    "./output-topology": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        saves.push(body.output_topology);
        return Promise.resolve(response({
          output_topology: body.output_topology,
          topology_revision: "saved-1",
        }));
      }
      return Promise.resolve(response(topology));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchChange({
    value: "0",
    getAttribute(name) {
      return { "data-group-id": "main", "data-role": "woofer" }[name] || "";
    },
    hasAttribute(name) { return name === "data-output-channel"; },
  });
  await harness.flush();
  harness.dispatchClick({ "data-act": "save-output-topology" });
  await harness.flush(); await harness.flush(); await harness.flush();

  if (saves.length !== 1) fail("three-output selector save should POST one topology", { saves });
  const channels = saves[0].speaker_groups[0].channels;
  const byRole = Object.fromEntries(channels.map((channel) => [channel.role, channel]));
  if (byRole.woofer.physical_output_index !== 0 ||
      byRole.mid.physical_output_index !== 1 ||
      byRole.tweeter.physical_output_index !== 2) {
    fail("three-output selector should only update the selected driver", { channels });
  }
  if (byRole.woofer.identity_verified !== false ||
      byRole.mid.identity_verified !== true ||
      byRole.tweeter.identity_verified !== true) {
    fail("three-output selector should not clear peer identity verification", { channels });
  }
  return { threeOutputChannelSelectorDoesNotAutoAssignPeers: true };
}

async function testCompiledProfileApplyBlockStaysUnderstandable() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  confirmedTopology.channel_identity = {
    kind: "jts_output_channel_identity_report",
    status: "verified",
    assigned_channel_count: 2,
    verified_channel_count: 2,
    unverified_channel_count: 0,
    targets: [
      {
        id: "main:woofer",
        speaker_group_id: "main",
        speaker_label: "Main speaker",
        role: "woofer",
        assigned: true,
        identity_verified: true,
        physical_output_index: 0,
      },
      {
        id: "main:tweeter",
        speaker_group_id: "main",
        speaker_label: "Main speaker",
        role: "tweeter",
        assigned: true,
        identity_verified: true,
        physical_output_index: 1,
      },
    ],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
    })),
    "./active-speaker/measurements": () => Promise.resolve(response({
      status: "ready_for_baseline",
      summary: {
        required_driver_count: 2,
        captured_driver_count: 2,
        driver_measurements_complete: true,
        required_summed_group_count: 1,
        validated_summed_group_count: 1,
        summed_validation_complete: true,
        latest_driver_measurements: {
          "main:woofer": { captured: true, outcome: "heard_correct_driver" },
          "main:tweeter": { captured: true, outcome: "heard_correct_driver" },
        },
        latest_summed_validations: {
          main: { validated: true, outcome: "blend_ok" },
        },
      },
      permissions: { may_compile_baseline: true },
      issues: [],
    })),
    "./active-speaker/baseline-profile": () => Promise.resolve(response({
      status: "compiled_apply_blocked",
      permissions: { may_compile: true, may_apply: false },
      config: { basename: "active_speaker_baseline.yml" },
      issues: [{
        severity: "blocker",
        code: "baseline_output_handoff_not_supported",
        message: "active profile YAML can be compiled, but applying it is disabled until outputd owns this DAC handoff",
      }],
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "blocked",
    "cannot be made active from this page yet",
    "cannot switch normal playback to it from here yet",
  ]) {
    if (!html.includes(expected)) {
      fail("Apply-blocked profiles should explain the limitation in user terms", { expected, html });
    }
  }
  for (const forbidden of ["outputd owns", "handoff", "Save profile"]) {
    if (html.includes(forbidden)) {
      fail("Apply-blocked profiles should not leak backend ownership vocabulary", { forbidden, html });
    }
  }
  return { compiledProfileApplyBlockStaysUnderstandable: true };
}

async function testVisibleCrossoverSettingsWinOverImportedJson() {
  const designSaves = [];
  const importedResearch = {
    artifact_schema_version: 1,
    kind: "jts_active_crossover_driver_research",
    drivers: [
      { role: "woofer", model: "Imported Woofer" },
      { role: "tweeter", model: "Imported Tweeter", gain_offset_db: -18 },
    ],
    crossover_candidates: [{
      between_roles: ["woofer", "tweeter"],
      frequency_hz: 4000,
      filter_type: "Butterworth",
      slope_db_per_octave: 12,
      confidence: "medium",
    }],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({
          status: "ready_for_review",
          summary: { manual_driver_count: 2, manual_crossover_candidate_count: 1 },
          manual_settings: body.manual_settings,
          driver_research: body.driver_research,
          operator_inputs: body.operator_inputs || {},
        }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2100");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "slope_db_per_octave",
  }, "24");
  harness.dispatchInput({
    "data-driver-import": "",
  }, JSON.stringify(importedResearch));
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 1) fail("Updating the working setup should POST once", { designSaves });
  const saved = designSaves[0];
  const manualCandidate = saved.manual_settings.crossover_candidates[0];
  const importedCandidate = saved.driver_research.crossover_candidates[0];
  if (manualCandidate.frequency_hz !== 2100 || manualCandidate.slope_db_per_octave !== 24) {
    fail("Visible manual crossover fields should win when updating", { saved });
  }
  if (importedCandidate.frequency_hz !== 4000 || importedCandidate.slope_db_per_octave !== 12) {
    fail("Imported research should still be preserved as research evidence", { saved });
  }
  return { visibleCrossoverSettingsWinOverImportedJson: true };
}

// The manual /sound/ crossover-editor polarity/delay authoring surface
// (P2a). manualSettingsPayload() must omit lower_polarity/upper_polarity/
// delay_ms/delay_target_role when the operator never touched them from their
// defaults (absent-in -> absent-out), so an untouched draft stays
// byte-minimal and round-trips cleanly through design_draft.py's own
// {key: value for ... if value not in (None, [])} filter.
async function testManualCrossoverPayloadOmitsPolarityAndDelayWhenDefault() {
  const designSaves = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({
          status: "ready_for_review",
          summary: {},
          manual_settings: body.manual_settings,
          operator_inputs: body.operator_inputs || {},
        }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2000");
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 1) fail("Updating the working setup should POST once", { designSaves });
  const candidate = designSaves[0].manual_settings.crossover_candidates[0];
  if ("lower_polarity" in candidate || "upper_polarity" in candidate ||
      "delay_ms" in candidate || "delay_target_role" in candidate) {
    fail("Untouched polarity/delay defaults must stay absent from the saved candidate", { candidate });
  }
  return { manualCrossoverPayloadOmitsPolarityAndDelayWhenDefault: true };
}

// A set polarity/delay must be emitted, and a 0 ms delay is a legitimate,
// deliberate value -- it must survive alongside its target role rather than
// being dropped by a `if (delayMs)` truthiness check (0 is falsy).
async function testManualCrossoverPayloadEmitsPolarityAndZeroDelay() {
  const designSaves = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({
          status: "ready_for_review",
          summary: {},
          manual_settings: body.manual_settings,
          operator_inputs: body.operator_inputs || {},
        }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2000");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "upper_polarity",
  }, "inverted");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "delay_ms",
  }, "0");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "delay_target_role",
  }, "tweeter");
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 1) fail("Updating the working setup should POST once", { designSaves });
  const candidate = designSaves[0].manual_settings.crossover_candidates[0];
  if (candidate.upper_polarity !== "inverted") {
    fail("upper_polarity=inverted should be sent", { candidate });
  }
  if ("lower_polarity" in candidate) {
    fail("Untouched lower_polarity must stay absent", { candidate });
  }
  if (candidate.delay_ms !== 0) {
    fail("0 ms delay is a legitimate value and must not be dropped by truthiness", { candidate });
  }
  if (candidate.delay_target_role !== "tweeter") {
    fail("delay_target_role should be sent alongside a set delay_ms", { candidate });
  }
  return { manualCrossoverPayloadEmitsPolarityAndZeroDelay: true };
}

// A delay entered without picking which driver it applies to must block the
// save client-side (not silently drop the delay, not silently POST a
// mis-shaped candidate) and surface an inline hint.
async function testManualCrossoverDelayWithoutTargetBlocksSaveClientSide() {
  const designSaves = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        designSaves.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response({ status: "ready_for_review", summary: {}, operator_inputs: {} }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2000");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "delay_ms",
  }, "0.15");
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 0) {
    fail("A delay without a target driver must block the save client-side", { designSaves });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Pick which driver is delayed")) {
    fail("The blocked save should surface an inline hint", { html });
  }
  return { manualCrossoverDelayWithoutTargetBlocksSaveClientSide: true };
}

// Reload round-trip: a saved candidate carrying an inverted polarity or a
// delay must reopen the collapsed "Alignment (advanced)" section and show
// the saved values, without needing any user action first.
async function testManualCrossoverAlignmentAdvancedAutoOpensOnSavedDelay() {
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "ready_for_review",
      summary: {},
      operator_inputs: { woofer: "Manual Woofer", tweeter: "Manual Tweeter" },
      manual_settings: {
        drivers: [],
        crossover_candidates: [{
          between_roles: ["woofer", "tweeter"],
          frequency_hz: 2000,
          filter_type: "Linkwitz-Riley",
          slope_db_per_octave: 24,
          confidence: "medium",
          upper_polarity: "inverted",
          delay_ms: 0.2,
          delay_target_role: "tweeter",
        }],
      },
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('driver-research__advanced" open')) {
    fail("A saved polarity/delay value should auto-open the Alignment (advanced) section on reload", { html });
  }
  if (!html.includes('data-manual-field="upper_polarity"') || !html.includes('value="0.2"')) {
    fail("Reloaded form fields should reflect the saved polarity/delay", { html });
  }
  return { manualCrossoverAlignmentAdvancedAutoOpensOnSavedDelay: true };
}

// "Use values" (applyDriverResearchToManualSettings) must copy an imported
// research candidate's polarity/delay into the working form, mirroring the
// existing filter_type/slope copy, so a subsequent save persists them.
async function testDriverResearchImportCopiesPolarityAndDelayIntoManualSettings() {
  const designSaves = [];
  const importedResearch = {
    artifact_schema_version: 1,
    kind: "jts_active_crossover_driver_research",
    drivers: [
      { role: "woofer", model: "Imported Woofer" },
      { role: "tweeter", model: "Imported Tweeter" },
    ],
    crossover_candidates: [{
      between_roles: ["woofer", "tweeter"],
      frequency_hz: 1800,
      filter_type: "Linkwitz-Riley",
      slope_db_per_octave: 24,
      confidence: "high",
      upper_polarity: "inverted",
      delay_ms: 0.1,
      delay_target_role: "tweeter",
    }],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({
          status: "ready_for_review",
          summary: {},
          manual_settings: body.manual_settings,
          driver_research: body.driver_research,
          operator_inputs: body.operator_inputs || {},
        }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-import": "" }, JSON.stringify(importedResearch));
  harness.dispatchClick({ "data-act": "parse-driver-research" });
  await harness.flush();
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 1) fail("Applying imported research should still allow exactly one save", { designSaves });
  const candidate = designSaves[0].manual_settings.crossover_candidates[0];
  if (candidate.upper_polarity !== "inverted" || candidate.delay_ms !== 0.1 ||
      candidate.delay_target_role !== "tweeter") {
    fail("Imported research polarity/delay should be copied into the manual working setting", { candidate });
  }
  return { driverResearchImportCopiesPolarityAndDelayIntoManualSettings: true };
}

// The crossover-preview candidate echo (renderCrossoverPreviewRows) must show
// an inverted/delayed region as a read-only annotation once a preview exists
// -- kept distinct from the applied-profile corrections card (never merged).
async function testCrossoverPreviewRowsShowInversionAndDelay() {
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "ready_for_review",
      summary: {},
      operator_inputs: { woofer: "Manual Woofer", tweeter: "Manual Tweeter" },
    })),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      kind: "jts_active_speaker_crossover_preview",
      status: "ready_for_protected_staging",
      summary: { ready_crossover_count: 1, blocker_count: 0 },
      groups: [{
        group_id: "main",
        label: "Main speaker",
        crossovers: [{
          status: "ready_for_review",
          between_roles: ["woofer", "tweeter"],
          proposed_frequency_hz: 2000,
          filters: [{ filter_type: "Linkwitz-Riley", slope_db_per_octave: 24 }],
          upper_polarity: "inverted",
          delay_ms: 0.2,
          delay_target_role: "tweeter",
          issues: [],
        }],
      }],
      issues: [],
      permissions: { may_prepare_protected_startup_config: true },
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Tweeter inverted")) {
    fail("An inverted region should be echoed on the preview row", { html });
  }
  if (!html.includes("Tweeter delayed 0.2 ms")) {
    fail("A delayed region should be echoed on the preview row", { html });
  }
  return { crossoverPreviewRowsShowInversionAndDelay: true };
}

async function testDriverResearchPromptCopyUsesHttpFallback() {
  let copiedText = "";
  let asyncClipboardCalled = false;
  const researchRequests = [];
  const draft = {
    status: "ready_for_review",
    operator_inputs: {
      woofer: "Manual Woofer",
      tweeter: "Manual Tweeter",
    },
    manual_settings: {
      drivers: [
        { role: "woofer", model: "Manual Woofer" },
        { role: "tweeter", model: "Manual Tweeter" },
      ],
      crossover_candidates: [{
        between_roles: ["woofer", "tweeter"],
        frequency_hz: 1800,
        filter_type: "Linkwitz-Riley",
        slope_db_per_octave: 24,
        confidence: "medium",
      }],
    },
    summary: {},
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response(draft)),
    "./active-speaker/driver-research-request": (_path, options = {}) => {
      researchRequests.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({
        prompt: "Target-bound prompt for Manual Woofer and Manual Tweeter",
        request: { request_fingerprint: "a".repeat(64) },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  Object.defineProperty(globalThis, "navigator", {
    value: {
      clipboard: {
        async writeText() {
          asyncClipboardCalled = true;
          throw new Error("not allowed on local HTTP");
        },
      },
    },
    configurable: true,
  });
  const promptEl = harness.elements.get("driver-research-prompt");
  promptEl.style.opacity = "0";
  promptEl.style.pointerEvents = "none";
  globalThis.document.execCommand = (command) => {
    if (command !== "copy") return false;
    const active = globalThis.document.activeElement;
    if (!active || active.style.opacity === "0" || active.style.pointerEvents === "none") {
      return false;
    }
    copiedText = active ? String(active.value || "") : "";
    return Boolean(copiedText);
  };

  harness.dispatchClick({ "data-act": "copy-driver-research-prompt" });
  await harness.flush();

  if (!copiedText.includes("Manual Woofer") || !copiedText.includes("Manual Tweeter")) {
    fail("driver research prompt should copy through the HTTP fallback", { copiedText });
  }
  if (researchRequests.length !== 1 ||
      researchRequests[0].operator_inputs.woofer !== "Manual Woofer" ||
      researchRequests[0].operator_inputs.tweeter !== "Manual Tweeter") {
    fail("driver research prompt should be prepared from the visible current values", {
      researchRequests,
    });
  }
  if (asyncClipboardCalled) {
    fail("local HTTP fallback should not await async clipboard before selection copy", {
      asyncClipboardCalled,
    });
  }
  const statusText = harness.elements.get("status").textContent;
  if (!statusText.includes("Copied driver research prompt.")) {
    fail("successful fallback copy should report success", { statusText });
  }
  return { driverResearchPromptCopyUsesHttpFallback: true };
}

async function testDriverResearchPromptCopyBlockedSelectsPrompt() {
  const draft = {
    artifact_schema_version: 1,
    kind: "jts_active_speaker_design_draft",
    status: "ready_for_review",
    topology_id: "default",
    driver_research: {
      drivers: [
        { role: "woofer", model: "Manual Woofer" },
        { role: "tweeter", model: "Manual Tweeter" },
      ],
      crossover_candidates: [{
        between_roles: ["woofer", "tweeter"],
        frequency_hz: 1800,
        filter_type: "Linkwitz-Riley",
        slope_db_per_octave: 24,
        confidence: "medium",
      }],
    },
    summary: {},
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response(draft)),
    "./active-speaker/driver-research-request": () => Promise.resolve(response({
      prompt: "Target-bound prompt for Manual Woofer and Manual Tweeter",
      request: { request_fingerprint: "b".repeat(64) },
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  Object.defineProperty(globalThis, "navigator", {
    value: {
      clipboard: {
        async writeText() {
          throw new Error("not allowed on local HTTP");
        },
      },
    },
    configurable: true,
  });
  globalThis.document.execCommand = (command) => command === "copy" ? false : false;

  harness.dispatchClick({ "data-act": "copy-driver-research-prompt" });
  await harness.flush();

  const statusText = harness.elements.get("status").textContent;
  if (!statusText.includes("Prompt text is selected")) {
    fail("blocked copy should leave the user with selected prompt text", { statusText });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes(">Selected</button>")) {
    fail("blocked copy should update the CTA to Selected", { html });
  }
  if (!html.includes('id="driver-research-prompt" class="driver-research__textarea driver-research__textarea--compact"')) {
    fail("blocked copy should render the prompt visibly instead of keeping it hidden", { html });
  }
  return { driverResearchPromptCopyBlockedSelectsPrompt: true };
}

async function testDriverResearchNotesCapExplainsBeforePost() {
  const designSaves = [];
  const importedResearch = {
    artifact_schema_version: 1,
    kind: "jts_active_crossover_driver_research",
    drivers: [
      { role: "woofer", model: "Imported Woofer", notes: "x".repeat(2049) },
    ],
    crossover_candidates: [],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({ status: "ready_for_review" }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-import": "" }, JSON.stringify(importedResearch));
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();

  if (designSaves.length) {
    fail("Overlong imported driver notes should fail before posting", { designSaves });
  }
  const statusText = harness.elements.get("status").textContent;
  if (!statusText.includes("Driver research notes for woofer must be <= 2048 chars")) {
    fail("Overlong imported driver notes should explain the 2048 char cap", { statusText });
  }
  return { driverResearchNotesCapExplainsBeforePost: true };
}

async function testWorkingSetupSummaryAvoidsStorageCounts() {
  const draft = {
    status: "ready_for_review",
    operator_inputs: {
      woofer: "Manual Woofer",
      tweeter: "Manual Tweeter",
    },
    manual_settings: {
      drivers: [
        { role: "woofer", model: "Manual Woofer", recommended_lowpass_hz: 2100 },
        { role: "tweeter", model: "Manual Tweeter", do_not_test_below_hz: 1800 },
      ],
      crossover_candidates: [{
        between_roles: ["woofer", "tweeter"],
        frequency_hz: 2100,
        filter_type: "Linkwitz-Riley",
        slope_db_per_octave: 24,
        confidence: "medium",
      }],
    },
    summary: {
      manual_driver_count: 2,
      manual_crossover_candidate_count: 1,
    },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response(draft)),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      status: "not_prepared",
      summary: {},
      groups: [],
      issues: [],
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "ready to preview",
    "Working setup: woofer + tweeter, crossover 2.1 kHz. No filters are active yet.",
    "Driver safety notes captured for woofer and tweeter.",
  ]) {
    if (!html.includes(expected)) {
      fail("Working setup summary should use product language", { expected, html });
    }
  }
  for (const forbidden of [
    "saved drivers",
    "saved driver",
    "saved crossover settings",
    "saved settings",
    "Save crossover settings",
    "No filters are applied",
  ]) {
    if (html.includes(forbidden)) {
      fail("Working setup summary should not expose storage/count language", { forbidden, html });
    }
  }
  return { workingSetupSummaryAvoidsStorageCounts: true };
}

async function testPreparePreviewUpdatesWorkingSetupFirst() {
  const designSaves = [];
  const previewSaves = [];
  let commissioningViewFetches = 0;
  const fetchHandler = baseFetch({
    "./active-speaker/commissioning-view": () => {
      commissioningViewFetches += 1;
      return Promise.resolve(response(commissioningViewPayload(
        commissioningViewFetches > 1
          ? {
              status: "needs_output_confirmation",
              current_step: "map",
              stepStatuses: {
                layout: "done",
                research: "done",
                map: "active",
                safety: "todo",
                profile: "todo",
              },
            }
          : {
              status: "needs_driver_values",
              current_step: "research",
              stepStatuses: {
                layout: "done",
                research: "active",
                map: "todo",
                safety: "todo",
                profile: "todo",
              },
            }
      )));
    },
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({
          status: "ready_for_review",
          summary: { manual_driver_count: 2, manual_crossover_candidate_count: 1 },
          manual_settings: body.manual_settings,
          driver_research: body.driver_research,
          operator_inputs: body.operator_inputs || {},
        }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
    "./active-speaker/crossover-preview": (_path, options = {}) => {
      if (options.method === "POST") {
        previewSaves.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response({
          status: "ready_for_protected_staging",
          summary: { ready_crossover_count: 1, blocker_count: 0 },
          groups: [{
            group_id: "main",
            label: "Main speaker",
            crossovers: [{
              status: "ready_for_review",
              between_roles: ["woofer", "tweeter"],
              proposed_frequency_hz: 2100,
              filters: [{ filter_type: "Linkwitz-Riley", slope_db_per_octave: 24 }],
              issues: [],
            }],
          }],
          issues: [],
          permissions: { may_prepare_protected_startup_config: true },
        }));
      }
      return Promise.resolve(response({ status: "not_prepared", summary: {}, groups: [], issues: [] }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2100");
  harness.dispatchClick({ "data-act": "prepare-crossover-preview" });
  for (let i = 0; i < 8; i += 1) await harness.flush();

  if (designSaves.length !== 1) {
    fail("Preparing the preview should update the working setup first", { designSaves, previewSaves });
  }
  if (previewSaves.length !== 1) {
    fail("Preparing the preview should build the preview after updating", { designSaves, previewSaves });
  }
  const saved = designSaves[0];
  const manualCandidate = saved.manual_settings.crossover_candidates[0];
  if (manualCandidate.frequency_hz !== 2100) {
    fail("Preview auto-update should persist the visible crossover point", { saved });
  }
  if (!harness.elements.get("status").textContent.includes(
    "Crossover preview ready. No sound was played. Confirm the outputs next."
  )) {
    fail("Preview completion should point to the next setup step", {
      status: harness.elements.get("status").textContent,
    });
  }
  if (commissioningViewFetches < 2) {
    fail("Preview completion should refresh the backend-owned commissioning step", {
      commissioningViewFetches,
    });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!/data-output-step="map"[^>]* open/.test(html)) {
    fail("Preview completion should open Confirm outputs without a page reload", {
      html,
    });
  }
  return { preparePreviewUpdatesWorkingSetupFirst: true };
}

async function testPreparePreviewIgnoresOptionalSubwooferDriverInfo() {
  const designSaves = [];
  const previewSaves = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayWithSubwooferTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({
          status: "ready_for_review",
          summary: { manual_driver_count: 2, manual_crossover_candidate_count: 1 },
          manual_settings: body.manual_settings,
          driver_research: body.driver_research,
          operator_inputs: body.operator_inputs || {},
        }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
    "./active-speaker/crossover-preview": (_path, options = {}) => {
      if (options.method === "POST") {
        previewSaves.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response({
          status: "ready_for_protected_staging",
          summary: { ready_crossover_count: 1, blocker_count: 0 },
          groups: [],
          issues: [],
          permissions: { may_prepare_protected_startup_config: true },
        }));
      }
      return Promise.resolve(response({ status: "not_prepared", summary: {}, groups: [], issues: [] }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2100");
  harness.dispatchClick({ "data-act": "prepare-crossover-preview" });
  for (let i = 0; i < 8; i += 1) await harness.flush();

  if (designSaves.length !== 1 || previewSaves.length !== 1) {
    fail("Optional local subwoofer should not block active-main crossover preview", {
      designSaves,
      previewSaves,
      status: harness.elements.get("status").textContent,
    });
  }
  const roles = (designSaves[0].manual_settings.drivers || []).map((driver) => driver.role);
  if (roles.includes("subwoofer")) {
    fail("Active-main driver research payload should not require the optional subwoofer", {
      roles,
      saved: designSaves[0],
    });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes("- subwoofer:")) {
    fail("AI helper prompt should not ask for optional subwoofer model details", { html });
  }
  return { preparePreviewIgnoresOptionalSubwooferDriverInfo: true };
}

async function testPreparePreviewWaitsForInFlightWorkingSetupUpdate() {
  const designSaves = [];
  const previewSaves = [];
  const pendingSave = deferred();
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return pendingSave.promise.then(() => response({
          status: "ready_for_review",
          summary: { manual_driver_count: 2, manual_crossover_candidate_count: 1 },
          manual_settings: body.manual_settings,
          driver_research: body.driver_research,
          operator_inputs: body.operator_inputs || {},
        }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
    "./active-speaker/crossover-preview": (_path, options = {}) => {
      if (options.method === "POST") {
        previewSaves.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response({ status: "ready_for_protected_staging", issues: [] }));
      }
      return Promise.resolve(response({ status: "not_prepared", summary: {}, groups: [], issues: [] }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2100");
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();

  let html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Working setup is updating before the preview.") ||
      !/data-act="prepare-crossover-preview" disabled/.test(html)) {
    fail("Preview should be disabled while the working setup update is in flight", { html });
  }

  harness.dispatchClick({ "data-act": "prepare-crossover-preview" });
  await harness.flush();
  if (designSaves.length !== 1 || previewSaves.length !== 0) {
    fail("Preview click during an in-flight update should not double-save or prepare", {
      designSaves,
      previewSaves,
      status: harness.elements.get("status").textContent,
    });
  }

  pendingSave.resolve();
  await harness.flush();
  await harness.flush();
  return { preparePreviewWaitsForInFlightWorkingSetupUpdate: true };
}

async function testPartialThreeWayWorkingSetupSummaryReadsCleanly() {
  const draft = {
    status: "needs_research",
    operator_inputs: {
      woofer: "Manual Woofer",
      mid: "Manual Mid",
      tweeter: "Manual Tweeter",
    },
    manual_settings: {
      drivers: [
        { role: "woofer", model: "Manual Woofer" },
        { role: "mid", model: "Manual Mid" },
        { role: "tweeter", model: "Manual Tweeter", do_not_test_below_hz: 1800 },
      ],
      crossover_candidates: [{
        between_roles: ["woofer", "mid"],
        frequency_hz: 350,
        filter_type: "Linkwitz-Riley",
        slope_db_per_octave: 24,
        confidence: "medium",
      }],
    },
    summary: {
      manual_driver_count: 3,
      manual_crossover_candidate_count: 1,
      missing_crossover_candidate_pairs: [["mid", "tweeter"]],
    },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeThreeWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response(draft)),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      status: "not_prepared",
      summary: {},
      groups: [],
      issues: [],
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  const expected = "Working setup: woofer, midrange, tweeter. Crossovers: woofer/midrange 350 Hz. Add the remaining crossover point before previewing the active crossover.";
  if (!html.includes(expected)) {
    fail("Partial 3-way working setup summary should read as polished sentences", { expected, html });
  }
  if (html.includes(". crossovers:") || html.includes(", Crossovers:")) {
    fail("Partial 3-way working setup summary should not contain awkward crossover punctuation", { html });
  }
  return { partialThreeWayWorkingSetupSummaryReadsCleanly: true };
}

async function testCommissionCardArmsAndSteps() {
  let commissionState = {
    commission_load: { status: "idle", target: {}, rollback_available: false },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const posts = [];
  let stepCount = 0;
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-load": (p, o) => {
      posts.push({ path: p, body: JSON.parse(o.body || "{}") });
      commissionState = {
        commission_load: { status: "loaded", target: { role: "woofer", audible_gain_db: -120 }, rollback_available: true },
        ramp: { confirmed_roles: [], pending: null },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ load: { status: "loaded", target: { role: "woofer" } } }));
    },
    "./active-speaker/commission-ramp-step": (p, o) => {
      posts.push({ path: p, body: JSON.parse(o.body || "{}") });
      stepCount += 1;
      const gain = -80 + ((stepCount - 1) * 5);
      commissionState = {
        commission_load: { status: "loaded", target: { role: "woofer", audible_gain_db: gain }, rollback_available: true },
        ramp: { confirmed_roles: [], pending: { role: "woofer", gain_db: gain, frequency_hz: 250 } },
        floor: { status: "floor_pending_operator", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ status: "stepped", next_gain_db: gain }));
    },
    "./active-speaker/commission-ramp-ack": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      if (body.outcome === "heard_correct_driver") {
        commissionState = {
          commission_load: { status: "loaded", target: { role: "woofer", audible_gain_db: -80 }, rollback_available: true },
          ramp: { confirmed_roles: ["woofer"], pending: null },
          floor: { status: "floor_confirmed", floor_audio_confirmed: true },
        };
        return Promise.resolve(response({ status: "confirmed", outcome: body.outcome }));
      }
      commissionState = {
        commission_load: { status: "loaded", target: { role: "woofer", audible_gain_db: -80 }, rollback_available: true },
        ramp: { confirmed_roles: [], pending: null },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ status: "retry", outcome: body.outcome }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  let html = harness.elements.get("view-body").innerHTML;
  if (html.includes('data-act="commission-arm"')) fail("arm button should not be visible", { html });
  if (!html.includes('data-act="commission-step"')) fail("start button missing before arm", { html });
  if (!html.includes(">Play</button>")) fail("idle output row should expose Play", { html });

  // Play silently opens the quiet driver setup, then begins the
  // automatic ramp. The card treats the whole ramp as one playing state; "too
  // quiet" is internal, not a visible operator button.
  harness.dispatchClick({
    "data-act": "commission-step",
    "data-role": "woofer",
    "data-identity-audition": "true",
  });
  harness.dispatchClick({
    "data-act": "commission-step",
    "data-role": "woofer",
    "data-identity-audition": "true",
  });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();
  const loadPosts = posts.filter((x) =>
    x.path === "./active-speaker/commission-load" && x.body.role === "woofer");
  if (loadPosts.length !== 1) {
    fail("rapid Start clicks should open the quiet driver test once", { posts });
  }
  if (!loadPosts[0].body.identity_audition) {
    fail("Confirm outputs arm must use identity-audition mode", { posts });
  }
  if (!posts.some((x) => x.path === "./active-speaker/commission-ramp-step")) {
    fail("commission-ramp-step not posted on step", { posts });
  }
  if (posts.some((x) =>
      x.path === "./active-speaker/commission-ramp-step" &&
      !x.body.identity_audition)) {
    fail("Confirm outputs ramp must use identity-audition mode", { posts });
  }
  html = harness.elements.get("view-body").innerHTML;
  let cardHtml = commissionCardHtml(html);
  for (const expected of ["Stop", "I hear woofer"]) {
    if (!cardHtml.includes(expected)) fail("playing row should expose stable tone controls", { expected, cardHtml });
  }
  if (cardHtml.includes("By-ear") || cardHtml.includes("Not yet made audible")) {
    fail("playing card should not expose the old flickering by-ear state", { cardHtml });
  }
  if (cardHtml.includes("commission-card__message")) {
    fail("automatic ramp should not render a changing progress line", { cardHtml });
  }
  if (cardHtml.includes('data-act="commission-abort" disabled')) {
    fail("Stop must stay enabled while the automatic ramp is active", { cardHtml });
  }
  for (const flappy of ["Raising", "Starting Woofer tone", "Recording…"]) {
    if (cardHtml.includes(flappy)) fail("automatic ramp should not surface transient busy copy", { flappy, cardHtml });
  }
  for (const hidden of ["Too quiet", "Too loud"]) {
    if (cardHtml.includes(hidden)) fail("automatic ramp should not expose legacy manual loudness buttons", { hidden, cardHtml });
  }

  harness.dispatchClick({
    "data-act": "commission-ack",
    "data-outcome": "heard_correct_driver",
    "data-confirm-output-identity": "true",
  });
  await harness.flush(); await harness.flush(); await harness.flush();
  if (!posts.some((x) =>
      x.path === "./active-speaker/commission-ramp-ack" &&
      x.body.outcome === "heard_correct_driver" &&
      x.body.confirm_output_identity === true)) {
    fail("heard verdict should be posted when the user hears the tone", { posts });
  }
  const visibleSilentAcks = posts.filter((x) =>
    x.path === "./active-speaker/commission-ramp-ack" && x.body.outcome === "silent");
  if (visibleSilentAcks.length) {
    fail("user-visible controls should not post manual silent retries", { posts });
  }
  return { commissionCardArmsAndSteps: true };
}

async function testCommissionCompleteDoesNotWrapToWoofer() {
  const commissionState = {
    commission_load: { status: "rolled_back", target: {}, rollback_available: false },
    ramp: { confirmed_roles: ["tweeter", "woofer"], pending: null },
    floor: { status: "floor_confirmed", floor_audio_confirmed: true },
  };
  const measurements = {
    status: "ready",
    summary: {
      required_driver_count: 2,
      captured_driver_count: 2,
      driver_checks_complete: true,
      driver_measurements_complete: true,
      latest_driver_checks: {
        "main:woofer": { speaker_group_id: "main", role: "woofer", captured: true },
        "main:tweeter": { speaker_group_id: "main", role: "tweeter", captured: true },
      },
      latest_driver_measurements: {
        "main:woofer": { speaker_group_id: "main", role: "woofer", captured: true },
        "main:tweeter": { speaker_group_id: "main", role: "tweeter", captured: true },
      },
    },
    issues: [],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("2/2 heard")) {
    fail("complete driver proof should show heard progress", { html });
  }
  return { commissionCompleteDoesNotWrapToWoofer: true };
}

async function testStaleRampConfirmationsDoNotCompleteDriverChecks() {
  const commissionState = {
    commission_load: { status: "rolled_back", target: {}, rollback_available: false },
    ramp: { confirmed_roles: ["tweeter", "woofer"], pending: null },
    floor: { status: "floor_confirmed", floor_audio_confirmed: true },
  };
  const measurements = {
    status: "ready",
    summary: {
      required_driver_count: 2,
      captured_driver_count: 0,
      driver_checks_complete: false,
      driver_measurements_complete: false,
      latest_driver_checks: {},
      latest_driver_measurements: {},
    },
    issues: [],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commissioning-view": () => Promise.resolve(response(commissioningViewPayload({
      status: "needs_driver_target_proof",
      current_step: "map",
      stepStatuses: {
        layout: "done",
        research: "done",
        map: "active",
        safety: "todo",
        profile: "todo",
      },
      driver_target_proof: {
        complete: false,
        source: "measurements",
        captured: 0,
        required: 2,
      },
    }))),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  const cardHtml = commissionCardHtml(html);
  if (cardHtml.includes("Complete") || cardHtml.includes("All drivers are confirmed")) {
    fail("stale ramp roles without measurement-backed checks must not complete the card", { cardHtml });
  }
  if (!html.includes('data-output-step="map" open') ||
      !html.includes('data-act="commission-step"')) {
    fail("stale ramp roles should restart visible output confirmation from the map", { html });
  }
  return { staleRampConfirmationsDoNotCompleteDriverChecks: true };
}

async function testDriverMicCaptureIsRemovedFromSoundFlow() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  const measurements = {
    status: "needs_driver_measurements",
    summary: {
      required_driver_count: 2,
      captured_driver_count: 0,
      driver_measurements_complete: false,
      required_summed_group_count: 1,
      validated_summed_group_count: 0,
      summed_validation_complete: false,
      latest_driver_measurements: {},
      latest_summed_validations: {},
    },
    permissions: { may_compile_baseline: false },
    issues: [],
  };
  const commissionState = {
    commission_load: { status: "rolled_back", target: {}, rollback_available: false },
    ramp: { confirmed_roles: ["woofer"], pending: null },
    floor: {
      status: "floor_confirmed",
      floor_audio_confirmed: true,
      last_operator_result: {
        accepted: true,
        playback_id: "pb-woofer",
        target: { speaker_group_id: "main", role: "woofer", driver_role: "woofer", output_index: 0 },
      },
    },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(confirmedTopology)),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();
  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes('data-act="record-driver-capture"')) {
    fail("driver mic capture should not be part of the /sound flow", { html });
  }
  if (!html.includes("Mic-based level matching is a separate HTTPS measurement step")) {
    fail("driver follow-up should point mic work to the separate HTTPS flow", { html });
  }
  return { driverMicCaptureIsRemovedFromSoundFlow: true };
}

function summedSummary(latestSummedTests) {
  return {
    required_driver_count: 2,
    captured_driver_count: 2,
    driver_measurements_complete: true,
    required_summed_group_count: 1,
    validated_summed_group_count: 0,
    summed_validation_complete: false,
    latest_driver_measurements: {
      "main:woofer": { captured: true, outcome: "heard_correct_driver" },
      "main:tweeter": { captured: true, outcome: "heard_correct_driver" },
    },
    latest_summed_tests: latestSummedTests,
    latest_summed_validations: {},
  };
}

async function testSummedByEarValidationExcludesMicCapture() {
  // The combined crossover check is phone-optional in the product sense: the
  // core /sound flow offers a by-ear "Sounds right" path and keeps microphone
  // capture out of this HTTP page. The by-ear positive is still gated on an
  // audible combined test (you can't certify a blend you didn't hear).
  const confirmedTopology = activeTwoWayTopologyPayload();

  // (1) No audible combined test yet -> the by-ear positive must be DISABLED
  //     (no certify-without-hearing bypass).
  {
    const measurements = {
      status: "needs_summed_validation",
      summary: summedSummary({}),
      permissions: {},
      issues: [],
    };
    const harness = setupHarness(baseFetch({
      "./output-topology": () => Promise.resolve(response(confirmedTopology)),
      "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
    }));
    await loadAndSetActiveState(harness);
    const html = harness.elements.get("view-body").innerHTML;
    if (!/data-outcome="blend_ok"[^>]*\sdisabled/.test(html)) {
      fail("by-ear blend confirmation must be disabled before an audible test", { html });
    }
  }

  // (2) An audible combined test exists -> the by-ear path is offered, the mic
  //     path is absent from /sound, and the positive POSTs an operator listening
  //     check (no WAV).
  {
    const measurements = {
      status: "needs_summed_validation",
      summary: summedSummary({
        main: { captured: true, audio_emitted: true, summed_test_id: "sum-1", playback_id: "sum-1" },
      }),
      permissions: {},
      issues: [],
    };
    const validationPosts = [];
    const harness = setupHarness(baseFetch({
      "./output-topology": () => Promise.resolve(response(confirmedTopology)),
      "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
      "./active-speaker/summed-validation": (_path, options = {}) => {
        validationPosts.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response(measurements));
      },
    }));
    await loadAndSetActiveState(harness);
    const originalSetTimeout = globalThis.window.setTimeout;
    globalThis.window.setTimeout = (fn) => { queueMicrotask(fn); return 0; };
    try {
      const html = harness.elements.get("view-body").innerHTML;
      if (html.includes('data-act="record-summed-capture"')) {
        fail("summed validation should keep mic capture out of the /sound flow", { html });
      }
      if (!/data-outcome="blend_ok"(?![^>]*\sdisabled)/.test(html)) {
        fail("by-ear blend confirmation should be enabled after an audible test", { html });
      }
      harness.dispatchClick({
        "data-act": "record-summed-validation",
        "data-group-id": "main",
        "data-summed-test-id": "sum-1",
        "data-outcome": "blend_ok",
      });
      await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();
      if (validationPosts.length !== 1) {
        fail("by-ear blend confirmation should POST once", { validationPosts });
      }
      const body = validationPosts[0];
      if (body.outcome !== "blend_ok" || body.operator_listening_check !== true) {
        fail("by-ear blend confirmation must post operator_listening_check", { body });
      }
      if (body.capture) fail("by-ear path must not upload a WAV", { body });
    } finally {
      globalThis.window.setTimeout = originalSetTimeout;
    }
  }
  return { summedByEarValidationExcludesMicCapture: true };
}

async function testSummedValidationRefreshesBaselineProfileState() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  const initialMeasurements = {
    status: "needs_summed_validation",
    summary: summedSummary({
      main: {
        captured: true,
        audio_emitted: true,
        summed_test_id: "sum-1",
        playback_id: "sum-1",
      },
    }),
    permissions: {},
    issues: [],
  };
  const validatedMeasurements = {
    status: "ready_for_baseline",
    summary: {
      ...summedSummary({
        main: {
          captured: true,
          audio_emitted: true,
          summed_test_id: "sum-1",
          playback_id: "sum-1",
        },
      }),
      validated_summed_group_count: 1,
      summed_validation_complete: true,
      latest_summed_validations: {
        main: { validated: true, outcome: "blend_ok", summed_test_id: "sum-1" },
      },
    },
    permissions: { may_compile_baseline: true },
    issues: [],
  };
  let measurements = initialMeasurements;
  let viewStatus = "needs_combined_check";
  let baselineFetches = 0;
  const baselineApplied = {
    status: "applied",
    permissions: { may_compile: false, may_apply: false },
    config: { basename: "active_speaker_baseline.yml" },
    issues: [],
  };
  const baselineReadyToCompile = {
    status: "ready_to_compile",
    permissions: { may_compile: true, may_apply: false },
    config: { basename: "active_speaker_baseline.yml" },
    issues: [],
  };
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(confirmedTopology)),
    "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
    "./active-speaker/baseline-profile": () => {
      baselineFetches += 1;
      return Promise.resolve(response(
        baselineFetches === 1 ? baselineApplied : baselineReadyToCompile
      ));
    },
    "./active-speaker/commissioning-view": () => Promise.resolve(response({
      status: viewStatus,
      test_level: levelPayload(-72).test_signal,
      combined_groups: [{
        group_id: "main",
        label: "Main speaker",
        status: viewStatus,
        status_label: viewStatus === "ready_to_save_profile" ? "ready" : "next",
        has_audible_test: true,
        validated: viewStatus === "ready_to_save_profile",
        actions: {
          record_combined_result: {
            id: "record_combined_result",
            enabled: true,
            endpoint: "./active-speaker/summed-validation",
            body: { speaker_group_id: "main", summed_test_id: "sum-1" },
          },
        },
      }],
    })),
    "./active-speaker/summed-validation": () => {
      measurements = validatedMeasurements;
      viewStatus = "ready_to_save_profile";
      return Promise.resolve(response(validatedMeasurements));
    },
  }));
  await loadAndSetActiveState(harness);

  harness.dispatchClick({
    "data-act": "record-summed-validation",
    "data-group-id": "main",
    "data-summed-test-id": "sum-1",
    "data-outcome": "blend_ok",
  });
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('data-act="save-apply-baseline-profile"')) {
    fail("saving the combined check must refresh stale applied profile state and show the save action", {
      baselineFetches,
      html,
    });
  }
  if (!html.includes("Save and apply")) {
    fail("ready-to-compile baseline profile should invite save/apply after combined validation", { html });
  }
  return { summedValidationRefreshesBaselineProfileState: true };
}

async function testSaveAndApplyUsesSingleFinishEndpoint() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  const measurements = {
    status: "ready_for_baseline",
    summary: {
      ...summedSummary({
        main: {
          captured: true,
          audio_emitted: true,
          summed_test_id: "sum-1",
          playback_id: "sum-1",
        },
      }),
      validated_summed_group_count: 1,
      summed_validation_complete: true,
      latest_summed_validations: {
        main: { validated: true, outcome: "blend_ok", summed_test_id: "sum-1" },
      },
    },
    permissions: { may_compile_baseline: true },
    issues: [],
  };
  const baselineReady = {
    status: "ready_to_compile",
    permissions: { may_compile: true, may_apply: false },
    config: { basename: "active_speaker_baseline.yml" },
    issues: [],
  };
  const baselineApplied = {
    status: "applied",
    permissions: { may_compile: false, may_apply: false },
    config: { basename: "active_speaker_baseline.yml" },
    issues: [],
  };
  const finishPosts = [];
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(confirmedTopology)),
    "./active-speaker/measurements": () => Promise.resolve(response(measurements)),
    "./active-speaker/baseline-profile": (_path, options = {}) => {
      if (options.method === "POST") {
        fail("final active profile CTA must not post the compile-only endpoint");
      }
      return Promise.resolve(response(baselineReady));
    },
    "./active-speaker/baseline-profile/apply": () => {
      fail("final active profile CTA must not call the old apply endpoint");
    },
    "./active-speaker/baseline-profile/save-and-apply": (_path, options = {}) => {
      finishPosts.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({
        status: "applied",
        profile: baselineApplied,
        apply: { result: "success" },
        output_safety: {
          safety_muted: false,
          active_config_path: "/var/lib/camilladsp/configs/active_speaker_baseline.yml",
        },
        issues: [],
      }));
    },
    "./active-speaker/commissioning-view": () => Promise.resolve(response({
      status: "ready_to_save_profile",
      test_level: levelPayload(-72).test_signal,
      combined_groups: [{
        group_id: "main",
        label: "Main speaker",
        status: "validated",
        status_label: "ready",
        has_audible_test: true,
        validated: true,
        actions: {
          record_combined_result: {
            id: "record_combined_result",
            enabled: false,
            endpoint: "./active-speaker/summed-validation",
            body: { speaker_group_id: "main", summed_test_id: "sum-1" },
          },
        },
      }],
    })),
  }));
  await loadAndSetActiveState(harness);

  harness.dispatchClick({ "data-act": "save-apply-baseline-profile" });
  for (let i = 0; i < 8; i += 1) await harness.flush();

  if (finishPosts.length !== 1) {
    fail("save/apply should be a single backend-owned mutation", { finishPosts });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("This is now your active speaker profile")) {
    fail("successful finish should render the applied active profile", { html });
  }
  if (!harness.elements.get("status").textContent.includes("saved and applied")) {
    fail("successful finish should provide one clear success message", {
      status: harness.elements.get("status").textContent,
    });
  }
  return { saveAndApplyUsesSingleFinishEndpoint: true };
}

async function testCommissionPendingStepShowsAckWithoutFloorFlag() {
  const commissionState = {
    commission_load: {
      status: "loaded",
      target: { role: "woofer", audible_gain_db: -45 },
      rollback_available: true,
    },
    ramp: {
      confirmed_roles: [],
      pending: { role: "woofer", gain_db: -45, playback_id: "old-step" },
    },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  const cardHtml = commissionCardHtml(html);
  if (!html.includes('data-act="commission-ack"')) {
    fail("pending ramp step must expose acknowledgement buttons even with a stale floor flag", { html });
  }
  for (const expected of ["Stop", "I hear woofer"]) {
    if (!cardHtml.includes(expected)) fail("pending ramp step should reuse the stable playing row", { expected, cardHtml });
  }
  for (const hidden of ["Too quiet", "Too loud"]) {
    if (cardHtml.includes(hidden)) fail("pending ramp step should not expose legacy manual loudness buttons", { hidden, cardHtml });
  }
  const enabledStep = cardHtml.match(/<button\b(?=[^>]*data-act="commission-step")(?![^>]*\bdisabled\b)[^>]*>/);
  if (enabledStep) {
    fail("pending ramp step must block another enabled step until it is acknowledged", {
      button: enabledStep[0],
      cardHtml,
    });
  }
  return { commissionPendingStepShowsAckWithoutFloorFlag: true };
}

async function testCommissionArmBlockedSurfacesReason() {
  // The gate can refuse an arm with HTTP 200 + a blocked body (e.g. the speaker
  // isn't staged). The card must surface a calm reason — not the "flicker then
  // nothing" silent failure, and never a raw snake_case code.
  const commissionState = {
    commission_load: { status: "idle", target: {}, rollback_available: false },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-load": () => Promise.resolve(response({
      preflight: {
        required_gates: [
          {
            id: "speaker_ready_for_active_load",
            passed: false,
            message: "Resolve protected startup-load blockers before commissioning",
          },
        ],
      },
      load: {
        status: "blocked",
        issues: [
          { code: "route_verified_not_verified", message: "Music renderers: route_verified is not verified" },
        ],
      },
    })),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("fully set up for driver tests yet")) {
    fail("blocked arm must surface a calm reason, not flicker silently", { html });
  }
  for (const leak of ["route_verified", "speaker_ready_for_active_load", "Music renderers:"]) {
    if (html.includes(leak)) fail("blocked arm reason must not leak backend codes", { leak, html });
  }
  return { commissionArmBlockedSurfacesReason: true };
}

async function testCommissionActiveGraphBlockSurfacesReason() {
  const commissionState = {
    commission_load: { status: "idle", target: {}, rollback_available: false },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-load": () => Promise.resolve(response({
      load: {
        status: "blocked",
        issues: [{
          code: "commission_active_graph_not_staged",
          message: "current persisted config is /etc/camilladsp/outputd-cutover.yml",
        }],
      },
    })),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("silent active-speaker setup")) {
    fail("active-graph-not-staged block should surface specific setup copy", { html });
  }
  for (const leak of ["commission_active_graph_not_staged", "outputd-cutover"]) {
    if (html.includes(leak)) fail("active graph block reason must not leak raw codes", { leak, html });
  }
  return { commissionActiveGraphBlockSurfacesReason: true };
}

async function testCommissionOutputReconcileFailureSurfacesReason() {
  const commissionState = {
    commission_load: { status: "idle", target: {}, rollback_available: false },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-load": () => Promise.resolve(response({
      load: {
        status: "failed",
        issues: [{
          code: "commission_output_hardware_reconcile_failed",
          message: "could not switch outputd to the active driver lane before tone playback",
        }],
      },
    })),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("could not switch the speaker output path")) {
    fail("output reconcile failure should surface specific output-path copy", { html });
  }
  for (const leak of ["commission_output_hardware_reconcile_failed", "outputd"]) {
    if (html.includes(leak)) fail("output reconcile failure must not leak raw backend detail", { leak, html });
  }
  return { commissionOutputReconcileFailureSurfacesReason: true };
}

async function testCommissionToneFailureStopsAutoRamp() {
  let commissionState = {
    commission_load: {
      status: "loaded",
      target: { role: "woofer", audible_gain_db: -120 },
      rollback_available: true,
    },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-ramp-step": (p, o) => {
      posts.push({ path: p, body: JSON.parse(o.body || "{}") });
      commissionState = {
        commission_load: { status: "rolled_back", target: {}, rollback_available: false },
        ramp: { confirmed_roles: [], pending: null },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({
        status: "tone_failed",
        next_gain_db: -80,
        tone_playback: {
          status: "failed",
          issues: [{
            code: "commission_tone_backend_failed",
            message: "could not play commissioning tone: Permission denied",
          }],
        },
        issues: [{
          code: "commission_tone_playback_failed",
            message: "JTS loaded the quiet driver setup but could not play the test tone.",
        }],
      }));
    },
    "./active-speaker/commission-ramp-abort": (p, o) => {
      posts.push({ path: p, body: JSON.parse(o.body || "{}") });
      return Promise.resolve(response({ status: "aborted" }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();

  const rampSteps = posts.filter((x) => x.path === "./active-speaker/commission-ramp-step");
  if (rampSteps.length !== 1) {
    fail("tone_failed must stop the automatic ramp instead of retrying after rollback", { posts });
  }
  if (!posts.some((x) => x.path === "./active-speaker/commission-ramp-abort")) {
    fail("automatic ramp failure should also call hard Stop to close any continuous tone", { posts });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("could not play the test tone")) {
    fail("tone failure should surface the real playback failure", { html });
  }
  if (html.includes("Press Arm for this driver first")) {
    fail("tone failure should not be overwritten by the follow-up not-armed copy", { html });
  }
  return { commissionToneFailureStopsAutoRamp: true };
}

async function testCommissionRampLimitKeepsConfirmationOpen() {
  let commissionState = {
    commission_load: {
      status: "loaded",
      target: { role: "woofer", audible_gain_db: 0 },
      rollback_available: true,
    },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-ramp-step": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      if (body.role === "tweeter") {
        fail("safe-limit stop must not advance into the tweeter", { posts });
      }
      commissionState = {
        commission_load: {
          status: "loaded",
          target: { role: "woofer", audible_gain_db: 0 },
          rollback_available: true,
        },
        ramp: {
          confirmed_roles: [],
          pending: { role: "woofer", gain_db: 0, frequency_hz: 80 },
        },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({
        status: "blocked",
        role: "woofer",
        speaker_group_id: "main",
        current_gain_db: 0,
        next_gain_db: 0,
        max_gain_db: 0,
        issues: [{
          severity: "blocker",
          code: "commission_ramp_at_limit",
          message: "the driver test is already at the maximum bounded level",
        }],
      }));
    },
    "./active-speaker/commission-ramp-ack": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      posts.push({ path: p, body });
      commissionState = {
        commission_load: {
          status: "rolled_back",
          target: { role: "woofer", audible_gain_db: 0 },
          rollback_available: false,
        },
        ramp: { confirmed_roles: ["woofer"], pending: null },
        floor: { status: "floor_confirmed", floor_audio_confirmed: true },
      };
      return Promise.resolve(response({
        status: "confirmed",
        outcome: body.outcome,
        measurements: {
          status: "needs_driver_measurements",
          summary: {
            driver_checks_complete: false,
            driver_measurements_complete: false,
          },
        },
      }));
    },
    "./active-speaker/commission-ramp-abort": (p, o) => {
      posts.push({ path: p, body: JSON.parse(o.body || "{}") });
      fail("safe-limit response must not abort the pending confirmation", { posts });
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();

  const rampSteps = posts.filter((x) => x.path === "./active-speaker/commission-ramp-step");
  if (rampSteps.length !== 1) {
    fail("safe-limit response must stop the automatic ramp after one blocked step", { posts });
  }
  if (posts.some((x) => x.path === "./active-speaker/commission-ramp-abort")) {
    fail("safe-limit response must leave the pending tone confirmable", { posts });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Reached the safe test limit")) {
    fail("safe-limit response should surface the room-facing action", { html });
  }
  if (!html.includes("I hear woofer")) {
    fail("safe-limit response should keep the heard-confirmation CTA visible", { html });
  }
  harness.dispatchClick({
    "data-act": "commission-ack",
    "data-outcome": "heard_correct_driver",
    "data-confirm-output-identity": "true",
  });
  await harness.flush(); await harness.flush(); await harness.flush();
  const statusText = harness.elements.get("status").textContent;
  if (statusText.includes("Reached the safe test limit")) {
    fail("successful driver confirmation should clear stale ramp-limit status", { statusText });
  }
  if (!statusText.includes("Driver confirmation saved. Continue with the next output.")) {
    fail("partial driver confirmation should give the next-step status", { statusText });
  }
  return { commissionRampLimitKeepsConfirmationOpen: true };
}

// C3a-7: startCommissionAutoRamp single-flight guard must always release.
// If an unexpected throw occurs inside the guarded body (after running is set to
// true but before runCommissionAutoRamp is handed off), commissionAutoRamp.running
// must be reset to false — otherwise the card wedges permanently until reload.
//
// We inject the throw via the status element: status('...') assigns to
// node.className / node.textContent. By replacing the 'status' element with one
// whose className setter throws — AFTER a successful arm response — we produce a
// throw that escapes postCommission's try/catch (which only wraps the fetch call)
// and propagates up into startCommissionAutoRamp's try/finally.
//
// Mutation check: removing the try/finally from startCommissionAutoRamp in
// main.js makes this test fail because after the throw commissionAutoRamp.running
// stays true, the "Play Woofer" button is replaced by a disabled "Preparing"
// button, and the second dispatchClick produces no new arm request.
async function testCommissionAutoRampResetsRunningFlagOnThrow() {
  let commissionState = {
    commission_load: { status: "idle", target: {}, rollback_available: false },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const armRequests = [];
  const stepRequests = [];
  let injectThrowViaStatus = true;
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-load": (p, o) => {
      armRequests.push({ path: p, body: JSON.parse(o.body || "{}") });
      commissionState = {
        commission_load: {
          status: "loaded",
          target: { role: "woofer", audible_gain_db: -120 },
          rollback_available: true,
        },
        ramp: { confirmed_roles: [], pending: null },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ load: { status: "loaded", target: { role: "woofer" } } }));
    },
    "./active-speaker/commission-ramp-step": (p, o) => {
      const body = JSON.parse(o.body || "{}");
      stepRequests.push({ path: p, body });
      commissionState = {
        commission_load: {
          status: "loaded",
          target: { role: "woofer", audible_gain_db: -80 },
          rollback_available: true,
        },
        ramp: {
          confirmed_roles: [],
          pending: { role: body.role, gain_db: -80, frequency_hz: 250 },
        },
        floor: { status: "floor_pending_operator", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ status: "stepped", next_gain_db: -80 }));
    },
    "./active-speaker/commission-ramp-abort": () =>
      Promise.resolve(response({ status: "rolled_back" })),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  // Replace the 'status' DOM element with one whose className setter throws once,
  // but ONLY when the textContent has been set to the "Starting quiet continuous"
  // message — i.e. the status() call inside startCommissionAutoRamp itself, after
  // ensureCommissionArmed has returned {ok:true}.  This escapes postCommission's
  // own try/catch (which only wraps the fetch path) and reaches
  // startCommissionAutoRamp's try/finally (the fix in C3a-7).
  const realStatus = harness.elements.get("status");
  const throwingStatus = Object.create(realStatus);
  Object.defineProperty(throwingStatus, "className", {
    get() { return realStatus.className; },
    set(v) {
      if (injectThrowViaStatus &&
          String(throwingStatus.textContent || "").includes("Starting quiet continuous")) {
        injectThrowViaStatus = false;
        throw new TypeError(
          "simulated unexpected throw in startCommissionAutoRamp body (C3a-7 test)"
        );
      }
      realStatus.className = v;
    },
    configurable: true,
  });
  Object.defineProperty(throwingStatus, "textContent", {
    get() { return realStatus.textContent; },
    set(v) { realStatus.textContent = v; },
    configurable: true,
  });
  harness.elements.set("status", throwingStatus);
  globalThis.document.getElementById = (id) => {
    if (!harness.elements.has(id)) harness.elements.set(id, makeEl(id));
    return harness.elements.get(id);
  };

  // Capture the unhandled rejection that startCommissionAutoRamp emits when the
  // injected throw propagates out of the async function.  In production (browser)
  // this is just a console warning; in Node.js it would crash the test harness.
  // The try/finally resets commissionAutoRamp.running before the rejection fires.
  let capturedRejection = null;
  const rejHandler = (reason) => { capturedRejection = reason; };
  process.on("unhandledRejection", rejHandler);

  // First click: the throw fires in status() after a successful arm → the
  // try/finally must reset commissionAutoRamp.running.
  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();

  process.removeListener("unhandledRejection", rejHandler);

  if (!capturedRejection) {
    fail("expected an unhandledRejection from the injected throw — check injection setup", {});
  }
  if (!String(capturedRejection).includes("C3a-7")) {
    fail("unhandled rejection was not from our injected throw", { capturedRejection: String(capturedRejection) });
  }

  if (armRequests.length !== 1) {
    fail("first commission-step should attempt one arm request", { armRequests });
  }

  // Restore normal status element so render() works cleanly for the assertion.
  harness.elements.set("status", realStatus);
  injectThrowViaStatus = false;

  // Trigger a re-render by re-dispatching to get a clean view.
  // (The throw in status() leaves the element in an indeterminate state; we need
  // a clean render to read the card state.  Click something benign.)
  // Simplest: dispatch a non-commission action and flush so the current card HTML
  // is re-rendered. We read it from the last successful render inside runCommission.
  // Actually the throw happened inside render(), so view-body innerHTML may be stale.
  // Force a fresh render by invoking a harmless action:
  harness.dispatchToggle({ "data-active-speaker-setup": true, open: true });
  await harness.flush(); await harness.flush();

  // After the throw the card must show the Play button again, NOT a disabled
  // "Preparing" button.  If commissionAutoRamp.running stayed true the card
  // would render the rampPreparing branch (disabled Preparing button) instead.
  let html = harness.elements.get("view-body").innerHTML;
  const cardHtml = commissionCardHtml(html);
  if (cardHtml.includes(">Preparing<") || (cardHtml.includes("disabled") && cardHtml.includes("Preparing"))) {
    fail(
      "after a throw in startCommissionAutoRamp the card must not stay in the disabled " +
      "Preparing state — commissionAutoRamp.running was not reset (try/finally missing?)",
      { cardHtml }
    );
  }
  if (!cardHtml.includes('data-act="commission-step"')) {
    fail("after a throw the Play button must be re-enabled so the flow is re-runnable", { cardHtml });
  }

  // Second click: commission is still armed from the first arm request, so
  // ensureCommissionArmed returns {ok:true} immediately without a network call.
  // runCommissionAutoRamp will therefore fire and call commission-ramp-step —
  // that proves the single-flight guard was cleared and the flow re-runs.
  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();

  if (stepRequests.length < 1) {
    fail(
      "a second commission-step click after the throw must kick off the ramp (commission-ramp-step) — " +
      "the single-flight guard must have been cleared by the try/finally",
      { stepRequests, armRequests }
    );
  }

  return { commissionAutoRampResetsRunningFlagOnThrow: true };
}

// C3a-7 (symmetric half): the fire-and-forget runCommissionAutoRamp loop must
// ALSO release the single-flight guard on every exit. A render() throw on a
// happy-path step inside the loop body (line ~4285, OUTSIDE postCommission's
// try/catch) rejects the un-awaited loop promise. Without the loop's try/finally
// commissionAutoRamp.running stays true and the card wedges in the disabled
// "Preparing" state forever.
//
// We inject the throw via the status className setter, gated to fire on the
// SECOND render where the merged Confirm outputs row shows the playing controls
// ("Stop" + "I hear woofer") — the first is postCommission's post-success render
// (now inside its try/catch after fix #2), the second is the loop's own render()
// at line ~4285. Targeting the second isolates the loop finally: with the loop
// finally removed, the card stays wedged and the test fails; with it present,
// running resets and a fresh click re-runs the flow.
async function testCommissionAutoRampLoopResetsRunningFlagOnRenderThrow() {
  let commissionState = {
    commission_load: {
      status: "loaded",
      target: { role: "woofer", audible_gain_db: -120 },
      rollback_available: true,
    },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };
  const stepRequests = [];
  let injectThrow = true;
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
    "./active-speaker/commission-ramp-step": (p, o) => {
      stepRequests.push({ path: p, body: JSON.parse(o.body || "{}") });
      // Successful step that produces a pending tone (canAck) so the card renders
      // the "Tone is playing for" state.
      commissionState = {
        commission_load: {
          status: "loaded",
          target: { role: "woofer", audible_gain_db: -80 },
          rollback_available: true,
        },
        ramp: {
          confirmed_roles: [],
          pending: { role: "woofer", gain_db: -80, frequency_hz: 250 },
        },
        floor: { status: "floor_pending_operator", floor_audio_confirmed: false },
      };
      return Promise.resolve(response({ status: "stepped", next_gain_db: -80 }));
    },
    "./active-speaker/commission-ramp-abort": () =>
      Promise.resolve(response({ status: "rolled_back" })),
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  // Replace the 'status' element with one whose className setter throws on the
  // SECOND render where the card shows the playing controls — i.e. the loop's
  // own render() at line ~4285, not postCommission's post-success render.
  const realStatus = harness.elements.get("status");
  let tonePlayingRenderCount = 0;
  const throwingStatus = Object.create(realStatus);
  Object.defineProperty(throwingStatus, "className", {
    get() { return realStatus.className; },
    set(v) {
      if (injectThrow) {
        const viewBody = harness.elements.get("view-body");
        const body = viewBody ? String(viewBody.innerHTML || "") : "";
        if (body.includes("I hear woofer") && body.includes("Stop")) {
          tonePlayingRenderCount += 1;
          if (tonePlayingRenderCount === 2) {
            injectThrow = false;
            throw new TypeError(
              "simulated render() throw in runCommissionAutoRamp loop body (C3a-7 test)"
            );
          }
        }
      }
      realStatus.className = v;
    },
    configurable: true,
  });
  Object.defineProperty(throwingStatus, "textContent", {
    get() { return realStatus.textContent; },
    set(v) { realStatus.textContent = v; },
    configurable: true,
  });
  harness.elements.set("status", throwingStatus);
  globalThis.document.getElementById = (id) => {
    if (!harness.elements.has(id)) harness.elements.set(id, makeEl(id));
    return harness.elements.get(id);
  };

  // Capture the unhandled rejection the loop emits when the injected throw
  // propagates out of the (un-awaited) loop promise.  The loop's try/finally
  // resets commissionAutoRamp.running before the rejection fires.
  let capturedRejection = null;
  const rejHandler = (reason) => { capturedRejection = reason; };
  process.on("unhandledRejection", rejHandler);

  harness.dispatchClick({
    "data-act": "commission-step",
    "data-role": "woofer",
    "data-identity-audition": "true",
  });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();

  process.removeListener("unhandledRejection", rejHandler);

  // Restore a clean status element so the assertion render works.
  harness.elements.set("status", realStatus);
  injectThrow = false;

  if (!capturedRejection || !String(capturedRejection).includes("C3a-7")) {
    fail("expected the injected render throw to reject the loop promise", {
      capturedRejection: capturedRejection ? String(capturedRejection) : null,
    });
  }
  if (stepRequests.length < 1) {
    fail("the loop should have taken at least one ramp step before the render throw", { stepRequests });
  }

  // Reset the pending state so a recovered card would offer Play again and a
  // stuck-running card would render the disabled "Preparing" branch
  // (rampPreparing = running && !toneActive).
  commissionState = {
    commission_load: {
      status: "loaded",
      target: { role: "woofer", audible_gain_db: -120 },
      rollback_available: true,
    },
    ramp: { confirmed_roles: [], pending: null },
    floor: { status: "floor_required", floor_audio_confirmed: false },
  };

  // The flow must be re-runnable: a fresh click kicks off another ramp step.
  // (With the loop finally missing, commissionAutoRamp.running is stuck true, so
  // startCommissionAutoRamp short-circuits at its "already running" guard and no
  // new ramp step is sent.)
  const stepsBefore = stepRequests.length;
  harness.dispatchClick({
    "data-act": "commission-step",
    "data-role": "woofer",
    "data-identity-audition": "true",
  });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();
  if (stepRequests.length <= stepsBefore) {
    fail(
      "a fresh commission-step click after the loop render throw must kick off the " +
      "ramp again — the loop's single-flight guard was not cleared",
      { stepRequests }
    );
  }

  return { commissionAutoRampLoopResetsRunningFlagOnRenderThrow: true };
}

async function testResetPartialCleanupSurfacesWarning() {
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology/reset": (path, options = {}) => {
      posts.push({ path, body: JSON.parse(options.body || "{}") });
      return Promise.resolve(response({
        output_topology: topologyPayload(),
        active_speaker_reset: {
          status: "partial",
          errors: [{
            id: "staged_config",
            path: "/var/lib/jasper/active-speaker-staged.json",
            error: "PermissionError: no access",
          }],
        },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush();

  harness.dispatchClick({ "data-act": "reset-output-topology" });
  await harness.flush(); await harness.flush(); await harness.flush();

  if (posts.length !== 1 || posts[0].path !== "./output-topology/reset") {
    fail("reset button should post to the topology reset endpoint", { posts });
  }
  const status = harness.elements.get("status").textContent;
  if (!status.includes("could not clear 1 active-speaker setup artifact")) {
    fail("partial active-speaker cleanup should be visible to the operator", { status });
  }
  if (!status.includes("staged_config")) {
    fail("partial cleanup warning should name the failed artifact id", { status });
  }
  for (const leak of ["/var/lib", "PermissionError"]) {
    if (status.includes(leak)) {
      fail("partial cleanup warning should not leak backend path/error details", {
        leak,
        status,
      });
    }
  }
  return { resetPartialCleanupSurfacesWarning: true };
}

// Distributed-active Slice 4: a bonded active follower's /sound/ renders the
// LOCAL driver/crossover/commissioning surface (the leader owns content EQ), and
// the module must boot cleanly even though the Off/Saved/Draft tabs + plot are
// absent from the follower page.
async function testFollowerModeRendersLocalDriverUi() {
  const fetched = [];
  const fallback = baseFetch();
  const harness = setupHarness((path, options = {}) => {
    fetched.push(path);
    if (path === "./output-topology") {
      return Promise.resolve(response(activeTwoWayTopologyPayload()));
    }
    return fallback(path, options);
  }, { follower: true });
  await harness.flush();
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  // The local driver/crossover/commissioning surface renders as primary content,
  // expanded (not tucked behind the solo box's "Speaker setup" disclosure).
  for (const expected of ["Active crossover setup", "Test combined drivers"]) {
    if (!html.includes(expected)) {
      fail("follower /sound/ should render the local active-speaker UI", { expected, html });
    }
  }
  for (const forbidden of [
    "Create custom profile",
    "Try a stock profile",
    "data-act=\"new-draft\"",
    "Speaker setup",
  ]) {
    if (html.includes(forbidden)) {
      fail("follower /sound/ should not render the content-EQ editor", { forbidden, html });
    }
  }
  // The leader owns the program domain: a follower must not fetch content-EQ /state,
  // but must load the local active-speaker hardware surface.
  if (fetched.includes("./state")) {
    fail("follower mode must not fetch the content-EQ /state", { fetched });
  }
  if (!fetched.includes("./output-topology")) {
    fail("follower mode should load the local active-speaker topology", { fetched });
  }
  return { followerModeRendersLocalDriverUi: true };
}

// A malformed island must fall to the SAFE side (follower), never solo: the
// follower page has no Off/Saved/Draft tabs or plot, so a solo fallback would
// dereference absent elements and blank the page. (json_island always emits
// valid JSON; this guards the fallback direction, not a real server output.)
async function testFollowerModeSafeFallbackOnMalformedIsland() {
  const fallback = baseFetch();
  const harness = setupHarness((path, options = {}) => {
    if (path === "./output-topology") {
      return Promise.resolve(response(activeTwoWayTopologyPayload()));
    }
    return fallback(path, options);
  }, { follower: true, islandText: "{not valid json" });
  // Reaching here means the module booted without throwing on the absent tabs —
  // i.e. it resolved to follower mode and skipped the solo tab/plot wiring.
  await harness.flush();
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Active crossover setup")) {
    fail("a malformed island must still render the local active-speaker UI", { html });
  }
  return { followerModeSafeFallbackOnMalformedIsland: true };
}

async function testLegacyStereoDraftCanPreparePreviewWithoutTargetCopy() {
  let designPosts = 0;
  let previewPosts = 0;
  const targetIds = ["left:woofer", "left:tweeter", "right:woofer", "right:tweeter"];
  const legacyDraft = {
    status: "ready_for_review",
    revision: 7,
    summary: {
      missing_driver_info_target_ids: [],
      missing_crossover_candidate_pairs: [],
    },
    operator_inputs: { woofer: "Legacy shared woofer", tweeter: "Legacy shared tweeter" },
    manual_settings: {
      drivers: [
        { role: "woofer", model: "Legacy shared woofer" },
        { role: "tweeter", model: "Legacy shared tweeter" },
      ],
      crossover_candidates: [{
        between_roles: ["woofer", "tweeter"],
        frequency_hz: 2500,
        filter_type: "Linkwitz-Riley",
        slope_db_per_octave: 24,
      }],
    },
    driver_safety_profile: {
      status: "incomplete",
      confirmation: null,
      targets: targetIds.map((targetId) => ({
        target_id: targetId,
        target_values_binding: "missing",
      })),
    },
    driver_safety_profile_evaluation: {
      status: "incomplete",
      confirmed_and_current: false,
    },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeStereoTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") designPosts += 1;
      return Promise.resolve(response(legacyDraft));
    },
    "./active-speaker/crossover-preview": (_path, options = {}) => {
      if (options.method === "POST") {
        previewPosts += 1;
        return Promise.resolve(response({
          kind: "jts_active_speaker_crossover_preview",
          status: "ready_for_protected_staging",
          summary: { ready_crossover_count: 2, blocker_count: 0 },
          groups: [],
          issues: [],
          permissions: { may_prepare_protected_startup_config: true },
        }));
      }
      return Promise.resolve(response({ status: "not_prepared", summary: {}, issues: [] }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (initialHtml.includes("Legacy shared woofer") || initialHtml.includes("Legacy shared tweeter")) {
    fail("Legacy role-only values must not copy into stereo target edit rows", { initialHtml });
  }
  for (const targetId of targetIds) {
    if (!initialHtml.includes(`data-driver-target="${targetId}"`)) {
      fail("Legacy stereo draft must retain one editable row per physical target", {
        targetId,
        initialHtml,
      });
    }
  }
  if (!initialHtml.includes("Safety profile: add the missing limits before confirmation.")) {
    fail("Preview readiness must not imply per-target safety confirmation", { initialHtml });
  }
  if (/data-act="prepare-crossover-preview" disabled/.test(initialHtml)) {
    fail("A clean server-ready legacy draft must allow crossover preview", { initialHtml });
  }

  harness.dispatchClick({ "data-act": "prepare-crossover-preview" });
  for (let i = 0; i < 6; i += 1) await harness.flush();

  if (previewPosts !== 1 || designPosts !== 0) {
    fail("Legacy stereo preview should POST directly without rewriting ambiguous target values", {
      previewPosts,
      designPosts,
      status: harness.elements.get("status").textContent,
    });
  }
  const previewHtml = harness.elements.get("view-body").innerHTML;
  if (!previewHtml.includes("Safety profile: add the missing limits before confirmation.") ||
      previewHtml.includes("Legacy shared woofer") || previewHtml.includes("Legacy shared tweeter")) {
    fail("Preparing a preview must not promote or copy legacy role-only safety values", {
      previewHtml,
    });
  }
  if (legacyDraft.driver_safety_profile.confirmation !== null ||
      legacyDraft.driver_safety_profile.targets.some((target) =>
        target.target_values_binding !== "missing")) {
    fail("Legacy preview must leave physical-target safety confirmation incomplete", { legacyDraft });
  }
  return { legacyStereoDraftCanPreparePreviewWithoutTargetCopy: true };
}

async function testStereoDriverValuesStayTargetSpecific() {
  const designSaves = [];
  const legacyDraft = {
    status: "ready_for_review",
    revision: 7,
    summary: {},
    operator_inputs: { woofer: "Legacy shared woofer", tweeter: "Legacy shared tweeter" },
    manual_settings: {
      drivers: [
        { role: "woofer", model: "Legacy shared woofer" },
        { role: "tweeter", model: "Legacy shared tweeter" },
      ],
      crossover_candidates: [],
    },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeStereoTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({
          ...legacyDraft,
          revision: 8,
          operator_inputs: body.operator_inputs,
          manual_settings: body.manual_settings,
        }));
      }
      return Promise.resolve(response(legacyDraft));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const initialHtml = harness.elements.get("view-body").innerHTML;
  const targetRows = initialHtml.match(/data-driver-target=/g) || [];
  if (targetRows.length !== 4) {
    fail("Stereo active speakers must render one driver model row per physical target", { initialHtml });
  }
  if (initialHtml.includes("Legacy shared woofer") || initialHtml.includes("Legacy shared tweeter")) {
    fail("Ambiguous legacy role values must not copy into both stereo cabinets", { initialHtml });
  }

  const models = {
    "left:woofer": "Left W6",
    "left:tweeter": "Left T1",
    "right:woofer": "Right W8",
    "right:tweeter": "Right T2",
  };
  Object.entries(models).forEach(([targetId, model]) => {
    harness.dispatchInput({ "data-driver-target": targetId }, model);
  });
  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2500");
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 1) fail("Target-specific stereo save should POST once", { designSaves });
  const saved = designSaves[0];
  if (saved.expected_revision !== 7) {
    fail("Design save must carry the loaded optimistic revision", { saved });
  }
  if (JSON.stringify(saved.operator_inputs.target_models) !== JSON.stringify(models)) {
    fail("Each stereo model must stay keyed by physical target", { saved });
  }
  const savedModels = Object.fromEntries(
    saved.manual_settings.drivers.map((driver) => [driver.target_id, driver.model])
  );
  if (JSON.stringify(savedModels) !== JSON.stringify(models)) {
    fail("Manual driver rows must preserve asymmetric target models", { savedModels, saved });
  }
  return { stereoDriverValuesStayTargetSpecific: true };
}

async function testDesignConflictRefreshesWithoutBlindRetryAndBooleanNumbersDrop() {
  const posts = [];
  const request = { request_fingerprint: "a".repeat(64), targets: [] };
  const research = {
    artifact_schema_version: 2,
    kind: "jts_active_crossover_driver_research",
    request_fingerprint: "a".repeat(64),
    drivers: [{
      target_id: "main:woofer",
      target_fingerprint: "b".repeat(64),
      role: "woofer",
      model: "Original W6",
      unknowns: ["thermal limit unknown"],
      field_provenance: {
        cabinet: {
          confidence: "medium",
          basis: "manufacturer drawing",
          sources: ["https://example.test/w6"],
        },
      },
    }],
    crossover_candidates: [],
  };
  const initial = {
    status: "ready_for_review",
    revision: 4,
    summary: {},
    operator_inputs: {
      target_models: { "main:woofer": "Original W6", "main:tweeter": "Original T1" },
    },
    driver_research_request: request,
    driver_research: research,
    driver_safety_profile: {
      targets: [{
        target_id: "main:woofer",
        unknowns: ["thermal limit unknown"],
        field_provenance: research.drivers[0].field_provenance,
      }],
    },
    manual_settings: {
      drivers: [
        {
          target_id: "main:woofer",
          role: "woofer",
          model: "Original W6",
          nominal_impedance_ohm: true,
        },
        { target_id: "main:tweeter", role: "tweeter", model: "Original T1" },
      ],
      crossover_candidates: [{
        between_roles: ["woofer", "tweeter"],
        frequency_hz: 2500,
        filter_type: "Linkwitz-Riley",
        slope_db_per_octave: 24,
      }],
    },
  };
  const fresh = {
    status: "ready_for_review",
    revision: 5,
    error: "Speaker design changed in another session",
    summary: {},
    operator_inputs: {
      target_models: { "main:woofer": "Fresh W8", "main:tweeter": "Fresh T2" },
    },
    manual_settings: { drivers: [], crossover_candidates: [] },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        posts.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response(fresh, false, 409));
      }
      return Promise.resolve(response(initial));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  const initialHtml = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "Explicit unknowns",
    "manufacturer drawing",
    "https://example.test/w6",
    "High-pass family / equivalent",
  ]) {
    if (!initialHtml.includes(expected)) {
      fail("Authority-bearing safety evidence must render after reload", { expected, initialHtml });
    }
  }

  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (posts.length !== 1) fail("A 409 must refresh without a blind retry", { posts });
  const saved = posts[0];
  if (saved.expected_revision !== 4) fail("Save must use the loaded revision", { saved });
  if (saved.driver_research_request.request_fingerprint !== request.request_fingerprint ||
      saved.driver_research.request_fingerprint !== research.request_fingerprint) {
    fail("Reload must preserve the bound v2 request and research packet", { saved });
  }
  const woofer = saved.manual_settings.drivers.find((driver) => driver.target_id === "main:woofer");
  if (woofer && Object.prototype.hasOwnProperty.call(woofer, "nominal_impedance_ohm")) {
    fail("Boolean values must not pass through Number(true) into a numeric field", { woofer });
  }
  const refreshedHtml = harness.elements.get("view-body").innerHTML;
  if (!refreshedHtml.includes("Fresh W8") || !refreshedHtml.includes("Fresh T2") ||
      !refreshedHtml.includes("another session")) {
    fail("Conflict response must replace stale values and explain the refresh", { refreshedHtml });
  }
  return { designConflictRefreshesWithoutBlindRetryAndBooleanNumbersDrop: true };
}

async function testDesignConflictPreservesUnsavedSafetyEdits() {
  const posts = [];
  const initial = {
    status: "ready_for_review",
    revision: 4,
    summary: {},
    operator_inputs: {
      target_models: { "main:woofer": "Original W6", "main:tweeter": "Original T1" },
    },
    manual_settings: {
      drivers: [
        { target_id: "main:woofer", role: "woofer", model: "Original W6" },
        {
          target_id: "main:tweeter",
          role: "tweeter",
          model: "Original T1",
          hard_excitation_band_hz: [5000, 22000],
        },
      ],
      crossover_candidates: [],
    },
    driver_safety_profile: {
      status: "confirmed",
      targets: [{
        target_id: "main:tweeter",
        hard_excitation_band_hz: [5000, 22000],
        field_provenance: {
          hard_excitation_band_hz: {
            confidence: "medium",
            basis: "old saved evidence",
            sources: ["https://example.test/old-tweeter"],
          },
        },
      }],
    },
    driver_safety_profile_evaluation: {
      status: "confirmed",
      confirmed_and_current: true,
    },
  };
  const fresh = {
    status: "ready_for_review",
    revision: 5,
    error: "Speaker design changed in another session.",
    summary: {},
    operator_inputs: {
      target_models: { "main:woofer": "Fresh W8", "main:tweeter": "Fresh T2" },
    },
    manual_settings: { drivers: [], crossover_candidates: [] },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method !== "POST") return Promise.resolve(response(initial));
      const body = JSON.parse(options.body || "{}");
      posts.push(body);
      if (posts.length === 1) return Promise.resolve(response(fresh, false, 409));
      return Promise.resolve(response({
        ...fresh,
        revision: 6,
        error: "",
        operator_inputs: body.operator_inputs,
        manual_settings: body.manual_settings,
        driver_safety_profile: { status: "unconfirmed", targets: [] },
        driver_safety_profile_evaluation: {
          status: "unconfirmed",
          confirmed_and_current: false,
        },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (!initialHtml.includes("confirmed for the current outputs") ||
      !initialHtml.includes("old saved evidence")) {
    fail("A clean confirmed draft must show its current confirmation and provenance", {
      initialHtml,
    });
  }

  harness.dispatchInput({
    "data-manual-driver": "main:tweeter",
    "data-manual-field": "hard_excitation_min_hz",
  }, "5500");
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (posts.length !== 1) fail("A conflict must not retry without user action", { posts });
  const conflictHtml = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    'data-manual-field="hard_excitation_min_hz" value="5500"',
    "Your unsaved edits were kept",
    "needs confirmation after saving current edits",
  ]) {
    if (!conflictHtml.includes(expected)) {
      fail("Conflict UI must retain and truthfully label unsaved safety edits", {
        expected,
        conflictHtml,
      });
    }
  }
  if (conflictHtml.includes("Fresh T2") || conflictHtml.includes("old saved evidence") ||
      conflictHtml.includes("confirmed for the current outputs")) {
    fail("Conflict UI must not replace edits or show stale authority", { conflictHtml });
  }

  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();
  if (posts.length !== 2 || posts[1].expected_revision !== 5) {
    fail("An explicit retry must reconcile against the fresh server revision", { posts });
  }
  const tweeter = posts[1].manual_settings.drivers.find(
    (driver) => driver.target_id === "main:tweeter"
  );
  if (!tweeter || tweeter.hard_excitation_band_hz[0] !== 5500) {
    fail("Explicit conflict retry must keep the local safety edit", { posts, tweeter });
  }
  return { designConflictPreservesUnsavedSafetyEdits: true };
}

const results = [];
// Dead-end: a layout is drafted but no spare physical output exists for a LOCAL
// subwoofer (the single-output Apple-dongle case). The card must keep the
// disabled "Add subwoofer" affordance AND additionally point the household at a
// wireless sub on the Speakers page — a "sub" must never be a silent dead-end.
async function testSubwooferDeadEndOffersWirelessCta() {
  const fallback = baseFetch();
  const harness = setupHarness((path, options = {}) => {
    if (path === "./output-topology") {
      return Promise.resolve(response(dongleMonoTopologyPayload()));
    }
    return fallback(path, options);
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("No unused physical output is available for a subwoofer")) {
    fail("dongle layout should still explain why a local sub cannot be added", { html });
  }
  // The existing disabled add affordance stays, with wireless-sub guidance
  // demoted to a secondary option.
  if (!html.includes('data-act="toggle-output-subwoofer"')) {
    fail("the local-subwoofer add affordance must remain in the dead-end branch", { html });
  }
  if (!html.includes('href="/rooms/"')) {
    fail("dead-end subwoofer card should link to the Speakers page", { html });
  }
  if (!html.includes("Wireless sub options")) {
    fail("dead-end subwoofer card should offer secondary wireless-sub guidance", { html });
  }
  return { subwooferDeadEndOffersWirelessCta: true };
}

// Negative: when a spare output exists for a LOCAL subwoofer, the card offers the
// normal add affordance and must NOT show the wireless-sub CTA (it would confuse
// a household that can simply add one locally).
async function testSubwooferWithSpareOutputHidesWirelessCta() {
  const harness = setupHarness(baseFetch());
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Subwoofer add-on")) {
    fail("default layout should render the subwoofer add-on card", { html });
  }
  if (html.includes('href="/rooms/"') || html.includes("Wireless sub options")) {
    fail("a layout with a spare output must not show the wireless-sub CTA", { html });
  }
  return { subwooferWithSpareOutputHidesWirelessCta: true };
}

const liveTabResult = await testLiveTabReplay();
results.push(liveTabResult);
results.push(await testVolumeFloorRequiresExplicitSaveButAuditionsDraft());
results.push(await testQuietTestSurfaceSurvivesStartupActions());
results.push(await testPassiveLayoutsDoNotExposeDirectDriverTestFlow());
results.push(await testActiveCrossoverFirstStepRender());
results.push(await testActiveSpeakerSetupTogglePersistsAcrossRender());
results.push(await testActiveRouteLimitsRenderedTemplates());
results.push(await testMeasuredDriversOpenProfileStep());
results.push(await testAppliedProfileEditContinueOpensProfileStep());
results.push(await testCombinedTestLevelPostsSelectedBoundedLevel());
results.push(await testCombinedTestButtonStopsActiveRequest());
results.push(await testReloadedPageRendersReloadSafeStopForActiveTest());
results.push(await testCombinedSoundsRightStopsAndSavesActiveLoop());
results.push(await testStaleSummedValidationDoesNotRenderValidatedGroup());
results.push(await testTwoOutputChannelSelectorAutoAssignsPeerOnSave());
results.push(await testChannelSelectorKeepsConfirmOutputsOpenWhenDraftDirty());
results.push(await testConfirmOutputsPlayUsesIdentityAuditionMode());
results.push(await testConfirmOutputAbortsPendingAuditionWithoutAutoRamp());
results.push(await testThreeOutputChannelSelectorDoesNotAutoAssignPeers());
results.push(await testCompiledProfileApplyBlockStaysUnderstandable());
results.push(await testLegacyStereoDraftCanPreparePreviewWithoutTargetCopy());
results.push(await testStereoDriverValuesStayTargetSpecific());
results.push(await testDesignConflictRefreshesWithoutBlindRetryAndBooleanNumbersDrop());
results.push(await testDesignConflictPreservesUnsavedSafetyEdits());
results.push(await testVisibleCrossoverSettingsWinOverImportedJson());
results.push(await testManualCrossoverPayloadOmitsPolarityAndDelayWhenDefault());
results.push(await testManualCrossoverPayloadEmitsPolarityAndZeroDelay());
results.push(await testManualCrossoverDelayWithoutTargetBlocksSaveClientSide());
results.push(await testManualCrossoverAlignmentAdvancedAutoOpensOnSavedDelay());
results.push(await testDriverResearchImportCopiesPolarityAndDelayIntoManualSettings());
results.push(await testCrossoverPreviewRowsShowInversionAndDelay());
results.push(await testDriverResearchPromptCopyUsesHttpFallback());
results.push(await testDriverResearchPromptCopyBlockedSelectsPrompt());
results.push(await testDriverResearchNotesCapExplainsBeforePost());
results.push(await testWorkingSetupSummaryAvoidsStorageCounts());
results.push(await testPreparePreviewUpdatesWorkingSetupFirst());
results.push(await testPreparePreviewIgnoresOptionalSubwooferDriverInfo());
results.push(await testPreparePreviewWaitsForInFlightWorkingSetupUpdate());
results.push(await testPartialThreeWayWorkingSetupSummaryReadsCleanly());
results.push(await testCommissionCardArmsAndSteps());
results.push(await testCommissionCompleteDoesNotWrapToWoofer());
results.push(await testStaleRampConfirmationsDoNotCompleteDriverChecks());
results.push(await testDriverMicCaptureIsRemovedFromSoundFlow());
results.push(await testSummedByEarValidationExcludesMicCapture());
results.push(await testSummedValidationRefreshesBaselineProfileState());
results.push(await testSaveAndApplyUsesSingleFinishEndpoint());
results.push(await testCommissionPendingStepShowsAckWithoutFloorFlag());
results.push(await testCommissionArmBlockedSurfacesReason());
results.push(await testCommissionActiveGraphBlockSurfacesReason());
results.push(await testCommissionOutputReconcileFailureSurfacesReason());
results.push(await testCommissionToneFailureStopsAutoRamp());
results.push(await testCommissionRampLimitKeepsConfirmationOpen());
results.push(await testCommissionAutoRampResetsRunningFlagOnThrow());
results.push(await testCommissionAutoRampLoopResetsRunningFlagOnRenderThrow());
results.push(await testResetPartialCleanupSurfacesWarning());
results.push(await testFollowerModeRendersLocalDriverUi());
results.push(await testFollowerModeSafeFallbackOnMalformedIsland());
results.push(await testSubwooferDeadEndOffersWirelessCta());
results.push(await testSubwooferWithSpareOutputHidesWirelessCta());

console.log(JSON.stringify(Object.assign({ results }, liveTabResult)));
