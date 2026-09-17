// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Minimal DOM harness for the /sound/ static module. It exercises the
// live-source tab state machine plus active-speaker guards without needing a
// browser or CamillaDSP.
//
//   node tests/js/sound_profile_harness.mjs deploy/assets/sound-profile/js/main.js
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { buildFunction, repoPath } from "./_loader.mjs";

const modulePath = process.argv[2] || "deploy/assets/sound-profile/js/main.js";
const siblingDir = dirname(modulePath);

// http.js's promptForControlToken (behind a lazy dialog.js import) parses but
// never runs — main.js only calls the two header helpers, neither of which
// hits the token-prompt path.
const JTSCONFIRM_STUB = [
  /^import\s+\{\s*jtsConfirm\s+\}\s+from\s+["'][^"']+["'];\s*/m,
  "const jtsConfirm = async (...args) => globalThis.__jtsConfirm ? globalThis.__jtsConfirm(...args) : true;\n",
];

// Sibling order is load-bearing: state.js reads the JSON island at eval time.
const runner = buildFunction(
  [
    { path: repoPath("deploy/assets/shared/js/escape.js") },
    { path: repoPath("deploy/assets/shared/js/http.js") },
    { path: repoPath("deploy/assets/shared/js/copy.js") },
    { path: repoPath("deploy/assets/shared/js/frequency-scale.js") },
    ...[
      "eq-math.js", "active-speaker-ui.js", "state.js", "format.js",
      "eq-curve.js", "topology.js", "driver-model.js", "installation.js", "driver-fields.js",
      "seat-level.js", "rear-calibration.js",
    ].map((name) => ({ path: join(siblingDir, name) })),
    { path: modulePath, rewrite: [JTSCONFIRM_STUB] },
  ],
  {
    stripImports: true,
    stripExports: true,
    strictImports: true,
    guardNoImports: true,
    // fetch stays on globalThis, not a runner param: setupHarness() reassigns
    // it every scenario, and a per-runner param would pin a stale mock for any
    // continuation outliving its scenario (main.js's commission auto-ramp loop).
    params: ["document", "window", "globalThis", "console", "setTimeout", "clearTimeout"],
  },
);

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
          startup_muted: true,
          protection_required: false,
          protection_status: "not_required",
        },
        {
          role: "tweeter",
          physical_output_index: 1,
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
        startup_muted: true,
        protection_required: false,
        protection_status: "not_required",
      },
      {
        role: "tweeter",
        physical_output_index: group.outputBase + 1,
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
      startup_muted: true,
      protection_required: false,
      protection_status: "not_required",
    }],
  });
  return topology;
}

// ADR-0318: a cardioid mono pair with a rear woofer output alongside the
// front woofer/tweeter — the shape the rear-calibration panel gates on.
function cardioidTwoWayTopologyPayload() {
  const topology = activeTwoWayTopologyPayload();
  topology.hardware.physical_output_count = 3;
  topology.hardware.outputs.push({ index: 2, human_label: "DAC output 3" });
  topology.speaker_groups[0].channels.push({
    role: "woofer",
    output_variant: "rear",
    physical_output_index: 2,
    identity_verified: false,
    startup_muted: true,
    protection_required: false,
    protection_status: "not_required",
  });
  return topology;
}

function passiveWithSubwooferTopologyPayload() {
  const topology = topologyPayload();
  topology.hardware.physical_output_count = 2;
  topology.hardware.outputs = [
    { index: 0, human_label: "DAC output 1" },
    { index: 1, human_label: "DAC output 2" },
  ];
  topology.routing.subwoofer_group_ids = ["sub"];
  topology.speaker_groups.push({
    id: "sub",
    label: "Subwoofer",
    kind: "subwoofer",
    mode: "subwoofer",
    position: { x: 0, y: -0.72, rotation_degrees: 0 },
    channels: [{
      role: "subwoofer",
      physical_output_index: 1,
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
          startup_muted: true,
          protection_required: false,
          protection_status: "not_required",
        },
        {
          role: "mid",
          physical_output_index: 1,
          startup_muted: true,
          protection_required: false,
          protection_status: "not_required",
        },
        {
          role: "tweeter",
          physical_output_index: 2,
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
// add-on dead-ends.
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
    research: "Confirm driver safety profile",
    experiment: "First speaker experiment",
    profile: "Apply speaker profile",
  };
  return ["layout", "research", "experiment", "profile"].map((id) => ({
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
    current_step: "experiment",
    stepStatuses: {
      layout: "done",
      research: "done",
      experiment: "active",
      profile: "todo",
    },
    status: "needs_combined_check",
    driver_target_proof: { complete: true, source: "measurements", captured: 2, required: 2 },
    ...overrides,
  });
}

function summedSummary(latestSummedTests, overrides = {}) {
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
    ...overrides,
  };
}

function setupHarness(fetchHandler, options = {}) {
  const pageMode = options.mode || "speaker";
  const elements = new Map();
  const absent = new Set();
  for (const id of [
    "tab-off", "tab-saved", "tab-draft", "eq-tabs", "back", "view-body",
    "now-playing", "plot", "plot-summary", "live-label", "status",
    "copy-driver-research-prompt-control",
  ]) {
    elements.set(id, makeEl(id));
  }
  const island = makeEl("sound-page-data");
  // Mirrors jasper/web/sound_setup.py:_sound_page_island. The crossover
  // vocabulary the editor may offer is SERVED, not hardcoded in the page, so
  // the harness has to serve it too. That the served lists are the compiler's
  // own is pinned on the Python side (tests/test_sound_setup.py); scenarios
  // narrow or drop this fixture to exercise the page's own refusals.
  island.textContent = options.islandText !== undefined
    ? options.islandText
    : JSON.stringify({
      mode: pageMode,
      follower: !!options.follower,
      crossover_vocabulary: options.crossoverVocabulary !== undefined
        ? options.crossoverVocabulary
        : {
          filter_types: ["Linkwitz-Riley"],
          slopes_db_per_octave: [12, 24, 48],
          default_filter_type: "Linkwitz-Riley",
          default_slope_db_per_octave: 24,
        },
    });
  elements.set("sound-page-data", island);
  if (pageMode !== "eq" || options.follower) {
    // The hardware and follower pages omit the content-EQ chrome. Making those
    // ids resolve to null exercises the module's mode guards as the browser does.
    // islandText lets a test inject malformed renderer data.
    for (const id of [
      "tab-off", "tab-saved", "tab-draft", "eq-tabs", "now-playing",
      "plot", "plot-summary", "live-label",
    ]) {
      elements.delete(id);
      absent.add(id);
    }
  }

  let bodyHtml = '';
  Object.defineProperty(elements.get('view-body'), 'innerHTML', {
    get() { return bodyHtml; },
    set(html) {
      bodyHtml = html;
      const seatIds = ['card', 'target', 'start', 'stop', 'status'].map(id => 'seat-level-' + id);
      for (const id of seatIds) {
        if (html.includes('id="' + id + '"')) {
          elements.set(id, makeEl(id));
          absent.delete(id);
        } else {
          elements.delete(id);
          absent.add(id);
        }
      }
      const card = elements.get('seat-level-card');
      if (card) {
        card.descendants = seatIds.map(id => elements.get(id));
        card.replaceWith = saved => saved.descendants.forEach(node => elements.set(node.id, node));
      }
    },
  });
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
      if (sel === "meta[name=jts-csrf]") return { content: "csrf-token" };
      if (sel === '[data-act="copy-driver-research-prompt"]') {
        return elements.get("copy-driver-research-prompt-control");
      }
      return null;
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
    // `hash` is read by the /sound/ deep-link entry point
    // (applyConfirmSafetyDeepLink); a real browser always has it, so the
    // harness does too — empty means "no fragment", the ordinary page load.
    location: { href: "", hash: options.hash || "" },
  };
  const clipboard = [];
  Object.defineProperty(globalThis, "navigator", {
    value: { clipboard: { async writeText(text) { clipboard.push(text); } } },
    configurable: true,
  });
  delete globalThis.__jtsConfirm;
  globalThis.fetch = fetchHandler;

  runner(globalThis.document, globalThis.window, globalThis, console, setTimeout, clearTimeout);

  const viewBody = elements.get("view-body");
  const driverProposal = makeEl("driver-proposal-control");
  const driverResearchFooter = makeEl("driver-research-footer-control");
  // The #2195 echo-back panel's targeted-refresh container. Same shape as the
  // two above: a manual driver edit repaints it without a full render, so the
  // panel cannot go on describing a value the operator has already changed.
  const driverEcho = makeEl("driver-echo-control");
  elements.set(driverProposal.id, driverProposal);
  elements.set(driverResearchFooter.id, driverResearchFooter);
  elements.set(driverEcho.id, driverEcho);
  viewBody.querySelector = (selector) => {
    if (selector === "[data-driver-proposal]") return driverProposal;
    if (selector === "[data-driver-research-footer]") return driverResearchFooter;
    if (selector === "[data-driver-echo]") return driverEcho;
    if (selector === '[data-output-step="layout"]') return {
      scrollIntoView(options) { elements.get('view-body').scrolledTo = {step: 'layout', ...options}; },
    };
    return null;
  };
  const dispatchClick = (attrs) => {
    const target = {
      getAttribute(name) { return attrs[name] || ""; },
      dataset: Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith('data-'))
        .map(([key, value]) => [key.slice(5), value])),
      closest(selector) {
        return (selector === "[data-act]" && 'data-act' in attrs) ||
          (selector === "[data-copy], [data-copy-target]" && 'data-copy' in attrs) ? this : null;
      },
    };
    for (const fn of viewBody._listeners.click || []) {
      fn({ target, preventDefault() {} });
    }
  };
  const dispatchChange = (target) => {
    if (!target.getAttribute) target.getAttribute = () => "";
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
        return selector === "[data-driver-advanced]" &&
          Object.prototype.hasOwnProperty.call(attrs, "data-driver-advanced");
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
  return { elements, clipboard, dispatchClick, dispatchChange, dispatchToggle, dispatchInput, flush };
}

function baseFetch(overrides = {}) {
  const active = activePayloads();
  return (path, options = {}) => {
    const override = overrides[path] || overrides[path.split("?")[0]];
    if (override) return override(path, options);
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
        stepStatuses: { layout: "active", research: "todo", experiment: "todo", profile: "todo" },
        driver_values: {
          status: "not_saved",
          complete: false,
          design_ready: false,
          preview_ready: false,
          missing_driver_info_roles: [],
          missing_crossover_candidate_pairs: [],
          message: "Save driver and crossover values.",
        },
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

// The rendered BODY of one step card, so an assertion cannot be satisfied by
// the step's summary hint or by a different step's card. Slices from the step's
// `output-step__body` to the next step marker; safe for the safety/profile
// steps, which contain no nested <details>.
function outputStepBodyHtml(html, step) {
  const at = String(html || "").indexOf('data-output-step="' + step + '"');
  if (at < 0) return null;
  const open = '<div class="output-step__body">';
  const bodyAt = html.indexOf(open, at);
  if (bodyAt < 0) return null;
  const start = bodyAt + open.length;
  const next = html.indexOf('data-output-step="', start);
  return html.slice(start, next < 0 ? html.length : next);
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
  const harness = setupHarness(fetchHandler, { mode: "eq" });
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

// #3309 rejected skipping the swap duck itself (a full Camilla graph replace
// cannot prove gain continuity, so it stays). The accepted fix is
// event-wiring: a continuous EQ slider drag ('input', one event per tick)
// must update the draft and the local graph only; the live-draft send that
// causes the duck fires exactly once, on 'change' (release).
async function testEqSliderDragSendsNoLiveAudioUntilRelease() {
  const liveDraftRequests = [];
  const fetchHandler = baseFetch({
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
  const harness = setupHarness(fetchHandler, { mode: "eq" });
  await harness.flush();
  await harness.flush();

  harness.elements.get("tab-draft").click();
  await harness.flush();
  await harness.flush();
  await harness.flush();
  liveDraftRequests.length = 0; // discard the tab switch's own (unrelated) immediate live-draft

  // Collapse the 180ms live-draft debounce to a microtask for the whole
  // gesture below, standing in for a drag slow enough that the debounce
  // window elapses between ticks — the real-world case #3309 was filed
  // against. If any 'input' tick still schedules a live-draft, this makes it
  // actually land instead of merely being outrun by the assertion.
  const originalSetTimeout = globalThis.window.setTimeout;
  globalThis.window.setTimeout = (fn, ms) => {
    if (ms === 180) { queueMicrotask(fn); return 1; }
    return originalSetTimeout(fn, ms);
  };
  try {
    // A drag is a stream of 'input' events, one per tick.
    harness.dispatchInput({ "data-field": "bass_db" }, "2");
    await harness.flush(); await harness.flush(); await harness.flush();
    harness.dispatchInput({ "data-field": "bass_db" }, "4");
    await harness.flush(); await harness.flush(); await harness.flush();
    harness.dispatchInput({ "data-field": "bass_db" }, "6");
    await harness.flush(); await harness.flush(); await harness.flush();
    if (liveDraftRequests.length !== 0) {
      fail("a slider 'input' stream (drag) must send no live-draft", { liveDraftRequests });
    }

    harness.dispatchChange({
      value: "6",
      getAttribute(name) { return name === "data-field" ? "bass_db" : ""; },
    });
    await harness.flush(); await harness.flush(); await harness.flush();
  } finally {
    globalThis.window.setTimeout = originalSetTimeout;
  }
  if (liveDraftRequests.length !== 1) {
    fail("releasing the slider ('change') should send exactly one live-draft", { liveDraftRequests });
  }
  return { eqSliderDragSendsNoLiveAudioUntilRelease: true };
}

// Every way INTO the name box re-seeds the whole naming record, so every way
// out has to clear the same record: a cancel that dropped only part of it
// leaves the box sitting over the footer's real actions.
async function testCancellingTheNameBoxClosesIt() {
  const harness = setupHarness(baseFetch(), { mode: "eq" });
  await harness.flush(); await harness.flush();
  harness.elements.get("tab-draft").click();
  await harness.flush(); await harness.flush();

  harness.dispatchClick({ "data-act": "begin-name" });
  await harness.flush();
  if (!harness.elements.get("view-body").innerHTML.includes('id="name-input"')) {
    fail("saving a profile should open the name box", {
      html: harness.elements.get("view-body").innerHTML,
    });
  }

  harness.dispatchClick({ "data-act": "cancel-name" });
  await harness.flush();
  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes('id="name-input"')) {
    fail("cancelling should close the name box", { html });
  }
  if (!html.includes('data-act="begin-name"')) {
    fail("cancelling should restore the draft footer actions", { html });
  }
  return { cancellingTheNameBoxClosesIt: true };
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
  // ./settings takes a patch and answers with the whole SoundSettings.to_dict()
  // (jasper/sound/settings.py:to_dict), so the mock keeps the saved record.
  const savedSettings = { ...statePayload.sound_settings };
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
      Object.assign(savedSettings, body);
      return Promise.resolve(response({
        ...statePayload,
        sound_settings: { ...savedSettings },
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
  const harness = setupHarness(fetchHandler, { mode: "output" });
  await harness.flush();
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
  if (settingsPosts.length !== 1 ||
      JSON.stringify(settingsPosts[0]) !== JSON.stringify({ volume_floor_db: -42 })) {
    fail("Save floor should persist the selected floor exactly once", { settingsPosts });
  }
  if (!harness.elements.get("status").textContent.includes("Volume floor saved.")) {
    fail("saving the volume floor should provide visible confirmation", {
      status: harness.elements.get("status").textContent,
    });
  }
  return { volumeFloorRequiresExplicitSaveButAuditionsDraft: true };
}

// A carrier that refuses to host EQ is page state on the settings card, not a
// line of prose below the fold: the save succeeded, the sound did not change.
// The re-render it forces is also the only place this harness sees the card
// AFTER a settings save, so the patch-save survivors are pinned here too.
async function testBlockedSettingsSaveRendersOnTheCard() {
  const statePayload = {
    ...basePayload,
    profile: { ...flatProfile, enabled: false },
    filter_count: 0,
    dsp_write_epoch: "state-0",
    sound_settings: {
      ...basePayload.sound_settings,
      headroom_trim_db: 3,
      volume_floor_db: -50,
    },
  };
  // ./settings takes a patch and answers with the whole SoundSettings.to_dict()
  // (jasper/sound/settings.py:to_dict), so the mock keeps the saved record.
  const savedSettings = { ...statePayload.sound_settings };
  const harness = setupHarness(baseFetch({
    "./settings": (_path, options = {}) => {
      Object.assign(savedSettings, JSON.parse(options.body || "{}"));
      return Promise.resolve(response({
        ...statePayload,
        sound_settings: { ...savedSettings },
        status: "blocked",
        reason_code: "active_baseline_recompose_unavailable",
        message: "This speaker runs an active crossover.",
        volume_warning: "Saved, but the volume floor lands on the next change.",
      }));
    },
  }), { mode: "output" });
  await harness.flush(); await harness.flush(); await harness.flush();

  harness.dispatchChange({ id: "set-match-loudness", checked: true });
  await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of ["info-card", "not audible until this speaker"]) {
    if (!html.includes(expected)) {
      fail("a blocked settings save should render the refusal on the card", {
        expected, html,
      });
    }
  }
  // The card carries the refusal, so the status line is free for the warning
  // the card does NOT carry instead of repeating it.
  if (!harness.elements.get("status").textContent.includes("volume floor")) {
    fail("a blocked save should leave the status line to its other warning", {
      status: harness.elements.get("status").textContent,
    });
  }
  // The re-rendered card describes the SAVED settings: the toggle the operator
  // moved, and the extra headroom the patch never mentioned.
  if (!/id="set-match-loudness" checked/.test(html)) {
    fail("the re-rendered card should keep Match loudness on", { html });
  }
  if (!html.includes('id="set-headroom-readout">\u2212' + "3.0 dB<")) {
    fail("a patch save must not blank the settings it did not carry", { html });
  }
  return { blockedSettingsSaveRendersOnTheCard: true };
}

// A graph that cannot host EQ is the PAGE's state, not a status line after the
// user edits something the save will refuse: no tab strip, no now-playing plot,
// no editor body — the reason and the page that can change it. It clears as
// soon as a payload stops refusing, and a mid-session refusal from ./apply
// puts the page into that state without a reload.
async function testBlockedEqCarrierIsThePageState() {
  const editorState = (eqCarrier) => ({
    ...basePayload,
    profile: { ...flatProfile, enabled: false },
    filter_count: 0,
    dsp_write_epoch: "state-0",
    eq_carrier: eqCarrier,
  });
  const eqChrome = ["eq-tabs", "now-playing"];

  const blocked = setupHarness(baseFetch({
    "./state": () => Promise.resolve(response(editorState({
      status: "blocked",
      reason_code: "unknown_config",
      message: "CamillaDSP is running a configuration JTS did not generate.",
    }))),
    // A successful apply carries eq_carrier too, so a graph that stopped
    // refusing drops the block at the next ingest instead of at a reload.
    "./apply": () => Promise.resolve(response(editorState({ status: "unknown" }))),
  }), { mode: "eq" });
  await blocked.flush(); await blocked.flush();

  const html = blocked.elements.get("view-body").innerHTML;
  for (const expected of [
    "info-card", "a configuration JTS did not generate", 'href="/sound/speaker/"',
  ]) {
    if (!html.includes(expected)) {
      fail("a blocked EQ carrier should render the refusal as the page", {
        expected, html,
      });
    }
  }
  if (html.includes("off-card") || html.includes("profile-row")) {
    fail("a blocked EQ carrier must not render the editor body", { html });
  }
  for (const id of eqChrome) {
    if (blocked.elements.get(id).hidden !== true) {
      fail("a blocked EQ carrier should hide the EQ-only chrome", { id });
    }
  }

  blocked.elements.get("tab-saved").click();
  await blocked.flush(); await blocked.flush(); await blocked.flush();
  const cleared = blocked.elements.get("view-body").innerHTML;
  if (cleared.includes("JTS did not generate") || !cleared.includes("Presets")) {
    fail("a carrier that stopped refusing should give the editor back", {
      html: cleared,
    });
  }
  for (const id of eqChrome) {
    if (blocked.elements.get(id).hidden) {
      fail("a cleared EQ carrier should bring the EQ-only chrome back", { id });
    }
  }

  // Fail-open half: "unknown" is what /state puts on the wire when nothing
  // probed the graph, and it keeps the editor...
  const open = setupHarness(baseFetch({
    "./state": () => Promise.resolve(response(editorState({ status: "unknown" }))),
    "./apply": () => Promise.resolve(response({
      status: "blocked",
      reason_code: "active_baseline_recompose_unavailable",
      message: "This speaker runs an active crossover.",
    })),
  }), { mode: "eq" });
  await open.flush(); await open.flush();
  if (!open.elements.get("view-body").innerHTML.includes("off-card")) {
    fail("an unprobed EQ carrier should keep the editor", {
      html: open.elements.get("view-body").innerHTML,
    });
  }
  if (open.elements.get("eq-tabs").hidden) {
    fail("an unprobed EQ carrier should keep the tab strip", {});
  }

  // ...until a save is refused, which is the same page state, mid-session.
  open.elements.get("tab-saved").click();
  await open.flush(); await open.flush(); await open.flush();
  const refused = open.elements.get("view-body").innerHTML;
  if (!refused.includes("info-card") || !refused.includes("active crossover")) {
    fail("a 200 refusal from ./apply should flip the page into the block", {
      html: refused,
    });
  }
  for (const id of eqChrome) {
    if (open.elements.get(id).hidden !== true) {
      fail("a refused save should hide the EQ-only chrome with the editor", { id });
    }
  }
  return { blockedEqCarrierIsThePageState: true };
}

// An unsaved Draft is live in CamillaDSP and persisted nowhere, so leaving the
// page must put the persisted profile back or the speaker keeps playing an EQ
// no surface will ever show again.
async function testLeavingAnUnsavedDraftRestoresThePersistedProfile() {
  const appliedProfile = { ...flatProfile, curve_id: "harman" };
  const applyPosts = [];
  const harness = setupHarness(baseFetch({
    "./state": () => Promise.resolve(response({
      ...basePayload, profile: appliedProfile, filter_count: 1, dsp_write_epoch: "state-0",
    })),
    "./apply": (_path, options = {}) => {
      applyPosts.push(options);
      return Promise.resolve(response({
        ...basePayload, profile: appliedProfile, dsp_write_epoch: "apply-1",
      }));
    },
    "./live-draft": () => Promise.resolve(response({
      live_status: "live", dsp_write_epoch: "live-1",
    })),
  }), { mode: "eq" });
  await harness.flush(); await harness.flush();

  harness.elements.get("tab-draft").click();
  await harness.flush(); await harness.flush(); await harness.flush();
  applyPosts.length = 0;

  for (const listener of globalThis.window._listeners.pagehide || []) listener();
  await harness.flush(); await harness.flush();

  if (applyPosts.length !== 1) {
    fail("leaving a live Draft should re-apply the persisted profile once", { applyPosts });
  }
  if (applyPosts[0].keepalive !== true) {
    fail("the pagehide restore must outlive the page", { options: applyPosts[0] });
  }
  if (JSON.parse(applyPosts[0].body).curve_id !== appliedProfile.curve_id) {
    fail("the restore should post the persisted profile, not the draft", {
      body: applyPosts[0].body,
    });
  }

  // The negative half: nothing is live-but-unpersisted from Saved or Off, so
  // leaving from either must not re-post anything at all.
  for (const tab of ["tab-saved", "tab-off"]) {
    harness.elements.get(tab).click();
    await harness.flush(); await harness.flush(); await harness.flush();
    applyPosts.length = 0;
    for (const listener of globalThis.window._listeners.pagehide || []) listener();
    await harness.flush(); await harness.flush();
    if (applyPosts.length !== 0) {
      fail("leaving a persisted view should post nothing", { tab, applyPosts });
    }
  }
  return { leavingAnUnsavedDraftRestoresThePersistedProfile: true };
}

async function testSplitPageModesRenderAndBootOnlyOwnedSurfaces() {
  const eqFetched = [];
  const eqBase = baseFetch();
  const eq = setupHarness((path, options = {}) => {
    eqFetched.push(path);
    return eqBase(path, options);
  }, { mode: "eq" });
  await eq.flush(); await eq.flush();
  eq.elements.get("tab-saved").click();
  await eq.flush(); await eq.flush();
  const eqHtml = eq.elements.get("view-body").innerHTML;
  for (const expected of ["Your profiles"]) {
    if (!eqHtml.includes(expected)) {
      fail("EQ mode omitted an owned Saved control", { expected, eqHtml });
    }
  }
  for (const forbidden of ["Volume floor", "Extra headroom", "I²S audio HAT", "Match loudness"]) {
    if (eqHtml.includes(forbidden)) {
      fail("EQ mode rendered a hardware-page control", { forbidden, eqHtml });
    }
  }
  if (eqFetched.includes("./output-topology") ||
      eqFetched.some((path) => path.indexOf("./active-speaker/") === 0)) {
    fail("EQ mode must not boot topology or commissioning", { eqFetched });
  }
  eq.elements.get("tab-draft").click();
  await eq.flush(); await eq.flush();
  const draftHtml = eq.elements.get("view-body").innerHTML;
  for (const expected of [">Simple</button>", ">PEQ</button>"]) {
    if (!draftHtml.includes(expected)) {
      fail("EQ mode omitted an owned editor mode", { expected, draftHtml });
    }
  }

  const outputFetched = [], hatPosts = [];
  const hat = {
    visibility: "visible", available: true, reason: "", intent_error: "",
    profiles: [{ id: "innomaker_hifi_amp_pro", label: "InnoMaker HiFi AMP Pro" }],
    desired_profile_id: null, detected_profile_id: null, detected_label: "",
    warnings: [], restart_required: false,
  };
  const hardwareBase = baseFetch({
    "./output-topology": () => response({
      output_topology: topologyPayload(), i2s_hat: hat,
    }),
    "./i2s-hat": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      hatPosts.push(body);
      const failed = hatPosts.length > 1;
      return response({
        ...hat, desired_profile_id: body.profile_id,
        restart_required: !failed, error: failed ? "apply failed" : "",
      }, !failed, failed ? 502 : 200);
    },
  });
  const output = setupHarness((path, options = {}) => {
    outputFetched.push(path);
    return hardwareBase(path, options);
  }, { mode: "output" });
  await output.flush(); await output.flush(); await output.flush();
  const outputHtml = output.elements.get("view-body").innerHTML;
  for (const expected of [
    "Volume floor", "Extra headroom", "I²S audio HAT", "Match loudness",
  ]) {
    if (!outputHtml.includes(expected)) {
      fail("Output mode omitted an owned control", { expected, outputHtml });
    }
  }
  for (const forbidden of ["Your profiles", "Simple", "PEQ", "Active crossover setup"]) {
    if (outputHtml.includes(forbidden)) {
      fail("Output mode rendered another page's control", { forbidden, outputHtml });
    }
  }
  // The HAT reading rides the topology payload, so Output loads it too --
  // and nothing else: the crossover/commissioning reads paint no control on
  // this page, so booting them is six round trips of nothing.
  if (!outputFetched.includes("./output-topology")) {
    fail("Output mode should load the I2S HAT reading", { outputFetched });
  }
  const outputExtras = outputFetched.filter(
    (path) => path.indexOf("./active-speaker/") === 0);
  if (outputExtras.length) {
    fail("Output mode must boot no commissioning reads", { outputExtras });
  }
  // Nothing detected: the picker is rendered, on the saved value, offering
  // only the HATs that cannot identify themselves.
  if (!outputHtml.includes('<option value="" selected>None / unmanaged</option>') ||
      !outputHtml.includes('<option value="innomaker_hifi_amp_pro">')) {
    fail("the select must offer the undetectable HATs on the saved value",
      { outputHtml });
  }
  output.dispatchChange({ id: "set-i2s-hat", value: "innomaker_hifi_amp_pro" });
  await loadAndSetActiveState(output);
  let hatHtml = output.elements.get("view-body").innerHTML;
  if (hatPosts[0].profile_id !== "innomaker_hifi_amp_pro")
    fail("the HAT control must POST the selected profile id", { hatPosts });
  if (!hatHtml.includes("Restart required."))
    fail("the HAT response must add its restart callout", { hatHtml });
  output.dispatchChange({ id: "set-i2s-hat", value: "" });
  await loadAndSetActiveState(output);
  hatHtml = output.elements.get("view-body").innerHTML;
  const message = output.elements.get("status").textContent;
  if (hatPosts[1].profile_id !== null ||
      message !== "Setting saved, but the boot change could not be applied. Try again; if it still fails, open System and run diagnostics." ||
      hatHtml.includes("Restart required.")) {
    fail("partial HAT apply must adopt state and distinguish persistence", { message, hatHtml });
  }
  // A detected HAT is reported, not offered: no control to change it.
  hat.detected_profile_id = "hifiberry_dac8x_studio";
  hat.detected_label = "HiFiBerry DAC8x Studio";
  const detectedOutput = setupHarness(hardwareBase, { mode: "output" });
  await detectedOutput.flush(); await detectedOutput.flush(); await detectedOutput.flush();
  const detectedHtml = detectedOutput.elements.get("view-body").innerHTML;
  if (!detectedHtml.includes("HiFiBerry DAC8x Studio") ||
      detectedHtml.includes('id="set-i2s-hat"')) {
    fail("a detected HAT must be reported without a picker", { detectedHtml });
  }

  const speakerFetched = [];
  const speaker = setupHarness((path, options = {}) => {
    speakerFetched.push(path);
    return hardwareBase(path, options);
  }, { mode: "speaker" });
  await speaker.flush(); await speaker.flush(); await speaker.flush();
  const speakerHtml = speaker.elements.get("view-body").innerHTML;
  if (!speakerHtml.includes("Active crossover setup")) {
    fail("Speaker mode omitted the layout/driver surface", { speakerHtml });
  }
  for (const forbidden of [
    "Volume floor", "Extra headroom", "I²S audio HAT", "Match loudness", "Your profiles",
  ]) {
    if (speakerHtml.includes(forbidden)) {
      fail("Speaker mode rendered another page's control", { forbidden, speakerHtml });
    }
  }
  // The leader owns the program domain on this page too: no content-EQ /state.
  if (speakerFetched.includes("./state")) {
    fail("Speaker mode must not fetch the content-EQ /state", { speakerFetched });
  }
  return { splitPageModesRenderAndBootOnlyOwnedSurfaces: true };
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
  includes("Confirm driver safety profile");
  includes("Component setup");
  includes("Research your components");
  includes("Load information");
  includes("Proposed starting crossover");
  includes("Advanced");
  includes("Build notes");
  includes('maxlength="1000"');
  includes("Additional build information");
  includes("DAC output assignments");
  includes("Speaker count");
  includes("Speaker type");
  includes('data-output-step="layout" open');
  includes('data-output-step="research"');
  excludes('data-output-step="map"');
  includes('data-output-step="experiment"');
  includes('data-output-step="profile"');
  excludes('data-output-step="research" open');
  excludes("Installed configuration");
  excludes('data-output-step="experiment" open');
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

async function testComponentFirstResearchFlowIsOrderedAndAdvancedIsFlat() {
  const topologySaves = [];
  const fetchHandler = baseFetch({
    "./output-topology": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        topologySaves.push(body.output_topology);
        return Promise.resolve(response({
          output_topology: body.output_topology,
          topology_revision: "component-style-1",
        }));
      }
      return Promise.resolve(response(activeTwoWayTopologyPayload()));
    },
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "not_saved",
      summary: {},
      operator_inputs: {},
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  let html = harness.elements.get("view-body").innerHTML;
  const componentAt = html.indexOf("Your components");
  const buildNotesAt = html.indexOf("Build notes");
  const promptAt = html.indexOf("1. Copy the prompt");
  const loadAt = html.indexOf("Load information");
  const proposalAt = html.indexOf("Proposed starting crossover");
  const advancedAt = html.indexOf('data-driver-advanced');
  if (!(componentAt >= 0 && componentAt < buildNotesAt &&
      buildNotesAt < promptAt && promptAt < loadAt &&
      loadAt < proposalAt && proposalAt < advancedAt)) {
    fail("components, build notes, research, proposal, and Advanced should render in the intended order", {
      componentAt, buildNotesAt, promptAt, loadAt, proposalAt, advancedAt, html,
    });
  }

  const basicHtml = html.slice(componentAt, advancedAt);
  for (const expected of [
    'data-driver-target="main:woofer"',
    'data-driver-target="main:tweeter"',
    'data-manual-driver="main:woofer" data-manual-field="enclosure_kind"',
    'data-driver-style data-save-driver-style data-group-id="main" data-role="tweeter"',
    'data-driver-field="notes"',
    'Additional build information',
    // Ticket 1.6: the one free-text field prompts with guided bullets rather
    // than a sentence, because it is now read by the tuning assistant as well
    // as the research one. `&#10;` is the newline that makes the placeholder a
    // list; asserting on it is asserting the bullets actually render as bullets.
    'placeholder="For example:&#10;- Horn or waveguide: kind, size, nominal coverage angle&#10;',
    '&#10;- Enclosure: sealed or ported, volume, port tuning&#10;',
    '&#10;- Why you built it this way"',
    // …and it says what neither assistant will do with what it reads.
    'never as an instruction.',
    "Choose enclosure / loading",
    "Choose tweeter type",
    "Ported / vented enclosure",
    "Passive-radiator enclosure",
    "Compression driver (horn-loaded)",
    "In-line attenuation",
  ]) {
    if (!basicHtml.includes(expected)) {
      fail("basic component cards should capture every installed driver choice", {
        expected, basicHtml,
      });
    }
  }
  for (const removed of [
    "Installed configuration",
    'data-manual-driver="main:woofer" data-manual-field="notes"',
    'data-manual-driver="main:tweeter" data-manual-field="notes"',
    "Alignment (advanced)",
    '<details class="driver-research__evidence"',
  ]) {
    if (html.includes(removed)) {
      fail("the simplified flow should not render per-driver notes or nested Advanced disclosures", {
        removed, html,
      });
    }
  }
  for (const advancedSection of [
    "Driver values",
    "Protection and measurement limits",
    "Cabinet geometry",
    "Research evidence",
    "Crossover points",
    "Alignment",
  ]) {
    if (!html.includes(advancedSection)) {
      fail("Advanced should render clear, always-visible sections", {
        advancedSection, html,
      });
    }
  }
  if (/data-driver-advanced open/.test(html)) {
    fail("Advanced should be collapsed on first render", { html });
  }

  harness.dispatchChange({
    value: "compression_driver",
    getAttribute(name) {
      return { "data-group-id": "main", "data-role": "tweeter" }[name] || "";
    },
    hasAttribute(name) {
      return name === "data-driver-style" || name === "data-save-driver-style";
    },
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();
  if (topologySaves.length !== 1 ||
      topologySaves[0].speaker_groups[0].channels[1].driver_style !==
        "compression_driver") {
    fail("the pre-prompt tweeter choice should auto-save through the topology owner", {
      topologySaves,
    });
  }

  harness.dispatchToggle({ "data-driver-advanced": true, open: true });
  harness.dispatchInput({
    "data-manual-driver": "main:tweeter",
    "data-manual-field": "driver_class",
  }, "compression_horn");
  html = harness.elements.get("view-body").innerHTML;
  if (!/data-driver-advanced open/.test(html) ||
      !html.includes('value="compression_horn" selected')) {
    fail("a conditional component edit should not collapse the open Advanced editor", { html });
  }

  // #2603 decision 8: the low limit is entered ONCE, and this is the input it
  // is entered in. Without it the operator's only routes were pasting research
  // or typing the high-pass cutoff — which the derivation overwrites, so a
  // deliberate edit could vanish with nowhere to express it. jts3's own remedy
  // is typing B&C's published 1600 here.
  if (!html.includes('data-manual-field="recommended_highpass_hz"') ||
      !html.includes('data-manual-field="recommended_highpass_slope_db_per_octave"')) {
    fail("the low limit's owner must be editable, not only pasteable", { html });
  }
  // …and the fields it derives say so, rather than inviting an edit that the
  // next save silently replaces.
  if (!html.includes("Required high-pass cutoff (derived)")) {
    fail("a derived protection field must be labelled derived", { html });
  }
  // Typing is deliberately not a repaint here (only driver_class/pad_kind
  // re-render), so the round trip is asserted where it is observable: the
  // field reaches the same payload the echo-back contract already requires it
  // to render back, pinned in tests/test_sound_profile_echo_back_contract.py.
  return { componentFirstResearchFlowIsOrderedAndAdvancedIsFlat: true };
}

async function testOneDriverComponentCanPrepareResearchPrompt() {
  const researchPosts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(topologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "not_saved",
      summary: {},
      operator_inputs: {},
    })),
    "./active-speaker/driver-research-request": (_path, options = {}) => {
      researchPosts.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({
        prompt: "Research the Example FR8 full-range driver",
        request: { request_fingerprint: "f".repeat(64) },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  const copyButton = harness.elements.get("copy-driver-research-prompt-control");
  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (!/<button[^>]*data-act="copy-driver-research-prompt"[^>]*disabled/.test(
    initialHtml
  )) {
    fail("a one-driver prompt should start disabled without component details");
  }

  harness.dispatchInput(
    { "data-driver-target": "main:full_range" },
    "Example FR8"
  );
  if (!copyButton.disabled) {
    fail("the prompt should still require the installed enclosure choice");
  }
  harness.dispatchInput({
    "data-manual-driver": "main:full_range",
    "data-manual-field": "enclosure_kind",
  }, "sealed");
  harness.dispatchInput({ "data-driver-field": "notes" },
    "Passive radiator on the rear baffle");
  if (copyButton.disabled) {
    fail("choosing the enclosure should visibly enable Copy prompt");
  }
  harness.dispatchClick({ "data-act": "copy-driver-research-prompt" });
  await harness.flush();
  await harness.flush();

  if (researchPosts.length !== 1) {
    fail("a one-driver layout should prepare one research request", { researchPosts });
  }
  const body = researchPosts[0];
  const driver = body.manual_settings.drivers[0];
  if (body.operator_inputs.target_models["main:full_range"] !== "Example FR8" ||
      body.operator_inputs.notes !== "Passive radiator on the rear baffle" ||
      driver.target_id !== "main:full_range" ||
      driver.cabinet.enclosure_kind !== "sealed") {
    fail("one-driver prompt should carry the physical component and enclosure", {
      body,
    });
  }
  return { oneDriverComponentCanPrepareResearchPrompt: true };
}

// A subless passive speaker: the backend terminates the ladder (safety and
// profile are `not_required`) and the page must render that termination
// instead of a titled-but-empty combined-test card, a "0/1 heard" pill styled
// ready, and a profile card asking for a combined check that cannot run.
async function testSublessPassiveLayoutRendersATerminatedLadder() {
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(dongleMonoTopologyPayload())),
    "./active-speaker/commissioning-view": () => Promise.resolve(response({
      status: "not_required", current_step: "layout",
      steps: ["layout", "research", "experiment", "profile"].map(id => ({
        id, status: id === "layout" ? "done" : "not_required",
      })),
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  const html = harness.elements.get("view-body").innerHTML;
  for (const id of ["map", "safety"]) {
    if (html.includes('data-output-step="' + id + '"')) fail("retired step", {id});
  }
  if (html.includes('data-act="prepare-summed-test"') ||
      html.includes('data-act="save-apply-baseline-profile"') ||
      html.includes('href="/sound/speaker/crossover/"')) fail("passive experiment action", {});
  return { sublessPassiveLayoutRendersATerminatedLadder: true };
}

async function testPassiveMainWithSubUsesResearchableMainTargetOnly() {
  const researchPosts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(
      passiveWithSubwooferTopologyPayload()
    )),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "not_saved",
      summary: {},
      operator_inputs: {},
    })),
    "./active-speaker/driver-research-request": (_path, options = {}) => {
      researchPosts.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({
        prompt: "Research the passive main component",
        request: { request_fingerprint: "e".repeat(64) },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  const componentHtml = html.slice(
    html.indexOf("<h3 class=\"setting-row__title\">Your components</h3>"),
    html.indexOf("<h3 class=\"setting-row__title\">Build notes</h3>")
  );
  if (!componentHtml.includes('data-driver-target="main:full_range"') ||
      componentHtml.includes('data-driver-target="sub:subwoofer"')) {
    fail("passive-main research targets must match the backend policy", {
      componentHtml,
    });
  }

  harness.dispatchInput(
    { "data-driver-target": "main:full_range" },
    "Example FR8"
  );
  harness.dispatchInput({
    "data-manual-driver": "main:full_range",
    "data-manual-field": "enclosure_kind",
  }, "sealed");
  harness.dispatchClick({ "data-act": "copy-driver-research-prompt" });
  await harness.flush();
  await harness.flush();

  const body = researchPosts[0];
  if (!body ||
      Object.keys(body.operator_inputs.target_models || {}).join(",") !==
        "main:full_range" ||
      (body.manual_settings.drivers || []).some(
        (driver) => driver.target_id === "sub:subwoofer"
      )) {
    fail("passive-main prompt must not send a backend-rejected sub target", {
      researchPosts,
    });
  }
  return { passiveMainWithSubUsesResearchableMainTargetOnly: true };
}

async function testPartialSavePreservesUnchosenEnclosure() {
  const designPosts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(topologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designPosts.push(body);
        return Promise.resolve(response({
          status: "ready_for_review",
          revision: 1,
          summary: { manual_driver_count: 1 },
          operator_inputs: body.operator_inputs || {},
          manual_settings: body.manual_settings,
          driver_research: null,
          driver_research_request: null,
        }));
      }
      return Promise.resolve(response({
        status: "not_saved",
        revision: 0,
        summary: {},
        operator_inputs: {},
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput(
    { "data-driver-target": "main:full_range" },
    "Example FR8"
  );
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const savedDriver = designPosts[0]?.manual_settings?.drivers?.[0];
  if (!savedDriver || Object.prototype.hasOwnProperty.call(savedDriver, "cabinet")) {
    fail("saving a partial component must not fabricate Not sure", {
      designPosts,
    });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes(
    '<option value="" disabled selected>Choose enclosure / loading</option>'
  ) || !/<button[^>]*data-act="copy-driver-research-prompt"[^>]*disabled/.test(
    html
  )) {
    fail("reload after a partial save must still require an explicit choice", {
      html,
    });
  }
  return { partialSavePreservesUnchosenEnclosure: true };
}

async function testDirectCrossoverEditRefreshesProposalAndFooter() {
  const draft = {
    status: "ready_for_review",
    revision: 2,
    summary: {
      manual_driver_count: 2,
      manual_crossover_candidate_count: 1,
    },
    operator_inputs: {
      target_models: {
        "main:woofer": "Example Woofer",
        "main:tweeter": "Example Tweeter",
      },
    },
    manual_settings: {
      drivers: [
        { target_id: "main:woofer", role: "woofer", model: "Example Woofer" },
        { target_id: "main:tweeter", role: "tweeter", model: "Example Tweeter" },
      ],
      crossover_candidates: [{
        between_roles: ["woofer", "tweeter"],
        frequency_hz: 1800,
        filter_type: "Linkwitz-Riley",
        slope_db_per_octave: 24,
      }],
    },
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(
      activeTwoWayTopologyPayload()
    )),
    "./active-speaker/design-draft": () => Promise.resolve(response(draft)),
    "./active-speaker/crossover-preview": () => Promise.resolve(response({
      status: "ready_for_protected_staging",
      summary: { ready_crossover_count: 1, blocker_count: 0 },
      groups: [{
        group_id: "main",
        label: "Main speaker",
        crossovers: [{
          status: "ready_for_review",
          between_roles: ["woofer", "tweeter"],
          proposed_frequency_hz: 1800,
          filters: [{
            filter_type: "Linkwitz-Riley",
            slope_db_per_octave: 24,
          }],
        }],
      }],
      issues: [],
      permissions: { may_prepare_protected_startup_config: true },
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({
    "data-manual-crossover": "woofer:tweeter",
    "data-manual-field": "frequency_hz",
  }, "2400");

  const proposal = harness.elements.get("driver-proposal-control").innerHTML;
  const footer = harness.elements.get("driver-research-footer-control").innerHTML;
  if (!proposal.includes("2.4 kHz") ||
      !proposal.includes("working proposal") ||
      proposal.includes("1.8 kHz") ||
      proposal.includes("preview ready") ||
      !footer.includes("Save values")) {
    fail("direct edits must immediately replace the stale prepared proposal", {
      proposal,
      footer,
    });
  }
  return { directCrossoverEditRefreshesProposalAndFooter: true };
}

async function testTweeterTypeChangeInvalidatesCopiedResearchBinding() {
  const topology = activeTwoWayTopologyPayload();
  topology.speaker_groups[0].channels[1].driver_style = "soft_dome";
  const oldRequest = {
    kind: "jts_active_crossover_driver_research_request",
    request_fingerprint: "d".repeat(64),
  };
  const designPosts = [];
  const topologyPosts = [];
  const draft = {
    status: "ready_for_review",
    revision: 3,
    summary: { manual_driver_count: 2 },
    operator_inputs: {
      target_models: {
        "main:woofer": "Example Woofer",
        "main:tweeter": "Example Tweeter",
      },
    },
    manual_settings: {
      drivers: [
        {
          target_id: "main:woofer",
          role: "woofer",
          model: "Example Woofer",
          cabinet: { enclosure_kind: "sealed" },
        },
        {
          target_id: "main:tweeter",
          role: "tweeter",
          model: "Example Tweeter",
        },
      ],
      crossover_candidates: [],
    },
    driver_research_request: oldRequest,
    driver_research: null,
  };
  const fetchHandler = baseFetch({
    "./output-topology": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        topologyPosts.push(body);
        return Promise.resolve(response({
          output_topology: body.output_topology,
          topology_revision: "topology-2",
        }));
      }
      return Promise.resolve(response(topology));
    },
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designPosts.push(body);
        return Promise.resolve(response({
          ...draft,
          revision: 4,
          driver_research_request: body.driver_research_request,
          driver_research: body.driver_research,
          manual_settings: body.manual_settings,
          operator_inputs: body.operator_inputs,
        }));
      }
      // Deliberately return the pre-topology-change binding. The client must
      // invalidate it after the topology save rather than restoring it here.
      return Promise.resolve(response(draft));
    },
    "./active-speaker/driver-research-request": () => Promise.resolve(response({
      prompt: "Research the saved soft-dome setup",
      request: oldRequest,
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchClick({ "data-act": "copy-driver-research-prompt" });
  await harness.flush();
  await harness.flush();
  harness.dispatchChange({
    value: "compression_driver",
    getAttribute(name) {
      return { "data-group-id": "main", "data-role": "tweeter" }[name] || "";
    },
    hasAttribute(name) {
      return name === "data-driver-style" || name === "data-save-driver-style";
    },
  });
  for (let i = 0; i < 6; i += 1) await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (topologyPosts.length !== 1 ||
      !html.includes(">Copy prompt</button>") ||
      html.includes(">Copied</button>")) {
    fail("changing tweeter type must visibly invalidate the copied prompt", {
      topologyPosts,
      html,
    });
  }
  harness.dispatchClick({ "data-act": "save-driver-design" });
  for (let i = 0; i < 4; i += 1) await harness.flush();
  if (designPosts.length !== 1 ||
      designPosts[0].driver_research_request !== null) {
    fail("the stale topology-bound request must not reach the next save", {
      designPosts,
    });
  }
  return { tweeterTypeChangeInvalidatesCopiedResearchBinding: true };
}

async function testThreeWayRendersEveryPhysicalComponentChoice() {
  const topology = activeTwoWayTopologyPayload();
  topology.hardware.physical_output_count = 3;
  topology.hardware.outputs.push({ index: 2, human_label: "DAC output 3" });
  topology.speaker_groups[0].mode = "active_3_way";
  topology.speaker_groups[0].channels = [
    {
      role: "woofer",
      physical_output_index: 0,
      protection_required: false,
      protection_status: "not_required",
    },
    {
      role: "mid",
      physical_output_index: 1,
      protection_required: false,
      protection_status: "not_required",
    },
    {
      role: "tweeter",
      physical_output_index: 2,
      protection_required: true,
      protection_status: "software_guard_requested",
    },
  ];
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(topology)),
  }));
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  const componentHtml = html.slice(
    html.indexOf("Your components"),
    html.indexOf("Research your components")
  );
  const componentCount = (componentHtml.match(/<section class="component-card">/g) || []).length;
  if (componentCount !== 3 ||
      !componentHtml.includes('data-driver-target="main:woofer"') ||
      !componentHtml.includes('data-driver-target="main:mid"') ||
      !componentHtml.includes('data-driver-target="main:tweeter"') ||
      !componentHtml.includes(
        'data-manual-driver="main:mid" data-manual-field="enclosure_kind"'
      ) ||
      !componentHtml.includes("Choose tweeter type")) {
    fail("active three-way should render one installed-choice card per physical driver", {
      componentCount,
      componentHtml,
    });
  }
  return { threeWayRendersEveryPhysicalComponentChoice: true };
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


async function testSpeakerLayoutMatrixAndRearAssignment() {
  for (const layout of ['mono', 'stereo']) {
    for (const speakerMode of ['passive', 'active_2way', 'active_3way', 'cardioid']) {
      const topology = speakerMode === 'cardioid'
        ? (layout === 'mono' ? activeTwoWayTopologyPayload() : activeStereoTwoWayTopologyPayload())
        : emptyTopologyPayload();
      topology.hardware.physical_output_count = 8;
      const originalChannels = structuredClone(topology.speaker_groups);
      topology.hardware.outputs = Array.from({length: 8}, (_, index) => ({index}));
      const saves = [];
      const harness = setupHarness(baseFetch({
        './output-topology': (_path, options = {}) => {
          if (options.method === 'POST') saves.push(JSON.parse(options.body).output_topology);
          return Promise.resolve(response({
            output_topology: saves.at(-1) || topology,
            topology_revision: 'saved',
            active_playback_route: activeRoutePayload({transport_channel_count: 8}),
          }));
        },
      }));
      await harness.flush(); await harness.flush(); await harness.flush();
      for (const [axis, value] of [['layout', layout], ['speaker-mode', speakerMode === 'cardioid' ? 'active_3way' : speakerMode]]) {
        harness.dispatchClick({'data-act': 'output-template-axis', 'data-axis': axis, 'data-value': value});
        await harness.flush(); await harness.flush();
      }
      if (speakerMode === 'cardioid') {
        harness.dispatchChange({checked: true, hasAttribute: name => name === 'data-output-cardioid'});
        await harness.flush(); await harness.flush();
        const html = harness.elements.get('view-body').innerHTML;
        assert.ok(html.includes('Front woofer') && html.includes('Rear woofer'));
      }
      harness.dispatchClick({'data-act': 'save-output-topology'});
      await harness.flush(); await harness.flush(); await harness.flush();
      const saved = saves.at(-1);
      const count = layout === 'mono' ? 1 : 2;
      const perSpeaker = speakerMode === 'passive' ? 1 : speakerMode === 'active_2way' ? 2 : 3;
      assert.equal(saved.speaker_groups.length, count);
      assert.equal(new Set(saved.speaker_groups.flatMap(g => g.channels.map(c => c.physical_output_index))).size, count * perSpeaker);
      assert.equal(saved.artifact_schema_version, speakerMode === 'cardioid' ? 2 : 1);
      for (const group of saved.speaker_groups) {
        assert.equal(group.channels.length, perSpeaker);
        if (speakerMode === 'cardioid') {
          assert.deepEqual(group.channels.slice(0, 2), originalChannels.find(g => g.id === group.id).channels);
          assert.equal(group.mode, 'active_2_way');
          assert.deepEqual(group.channels.map(c => c.role), ['woofer', 'tweeter', 'woofer']);
          assert.equal(group.channels.at(-1).output_variant, 'rear');
          assert.equal(group.channels.at(-1).startup_muted, true);
        }
      }
      if (speakerMode !== 'cardioid') continue;
      const group = saved.speaker_groups[0];
      const frontIndex = group.channels[0].physical_output_index;
      harness.dispatchChange({value: '7', getAttribute: name => ({
        'data-group-id': group.id, 'data-role': 'woofer', 'data-output-variant': 'rear',
      })[name], hasAttribute: name => name === 'data-output-channel'});
      harness.dispatchClick({'data-act': 'save-output-topology'});
      await harness.flush(); await harness.flush(); await harness.flush();
      assert.equal(saves.at(-1).speaker_groups[0].channels[0].physical_output_index, frontIndex);
      assert.equal(saves.at(-1).speaker_groups[0].channels.at(-1).physical_output_index, 7);

    }
  }
  return {speakerLayoutMatrixAndRearAssignment: true};
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
  if (/<option[^>]* disabled/.test(initialHtml.match(/<select data-output-channel[^]*?<\/select>/g)?.join("") || "")) {
    fail("no DAC output option should ever be disabled", { initialHtml });
  }
  if (!initialHtml.includes(" — used by Main speaker · Tweeter")) {
    fail("the used-by hint should still appear for an output taken by another driver", { initialHtml });
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

  if ("human_output_label" in byRole.woofer || "human_output_label" in byRole.tweeter) {
    fail("changing channel assignment should clear stale human labels", { channels });
  }
  return { twoOutputChannelSelectorAutoAssignsPeerOnSave: true };
}

// JTS3 hardware punch: a compression-driver tweeter commissioned with a
// ~2 kHz crossover point was permanently blocked because driver_style had no
// UI surface anywhere, so the conservative 5000 Hz "unknown style" floor
// could never be lowered to the driver's real 2000 Hz floor. This pins the
// fix: the component card offers a tweeter-only style selector before Copy
// prompt, writes onto the topology channel (the existing single writer), and
// auto-saves that choice before research can proceed.
async function testTweeterDriverStyleSelectorSetsTopologyAndAppearsInReview() {
  const topology = activeTwoWayTopologyPayload();
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
  if (!initialHtml.includes(
      'data-driver-style data-save-driver-style data-group-id="main" data-role="tweeter"'
  )) {
    fail("component card must offer the topology-owned tweeter style before the prompt", {
      initialHtml,
    });
  }
  if (initialHtml.includes('data-driver-style data-group-id="main" data-role="woofer"')) {
    fail("a low-frequency role must not get a driver-style selector (unused by the floor policy)", { initialHtml });
  }
  if (!initialHtml.includes("Choose tweeter type") ||
      !initialHtml.includes("Not sure (conservative default)")) {
    fail("undeclared style should require an explicit type or Not sure choice", { initialHtml });
  }
  if (!initialHtml.includes("Tweeter style not set")) {
    fail("component card must explain why the exact style is required", { initialHtml });
  }

  harness.dispatchChange({
    value: "compression_driver",
    getAttribute(name) {
      return { "data-group-id": "main", "data-role": "tweeter" }[name] || "";
    },
    hasAttribute(name) {
      return name === "data-driver-style" || name === "data-save-driver-style";
    },
  });
  await harness.flush(); await harness.flush(); await harness.flush();

  const afterSelectHtml = harness.elements.get("view-body").innerHTML;
  if (!afterSelectHtml.includes('value="compression_driver" selected')) {
    fail("selecting a style must reflect back as the selected option", { afterSelectHtml });
  }
  // #2603 re-baselined this copy: the figure is the DEFAULT minimum crossover
  // used when the datasheet publishes none, not a floor the declaration must
  // clear. The assertion still pins that the declared style and its number
  // reach the review card.
  if (!afterSelectHtml.includes("Tweeter style: Compression driver (horn-loaded)") ||
      !afterSelectHtml.includes("default minimum crossover 2000 Hz")) {
    fail("declared style must be visible on the review card with its figure", { afterSelectHtml });
  }
  if (afterSelectHtml.includes("protective high-pass floor")) {
    fail("the retired floor vocabulary must not return to this hint", { afterSelectHtml });
  }

  if (saves.length !== 1) fail("style change should auto-save through the existing topology writer", { saves });
  const tweeter = saves[0].speaker_groups[0].channels.find((c) => c.role === "tweeter");
  if (!tweeter || tweeter.driver_style !== "compression_driver") {
    fail("saved topology must carry the declared driver_style on the channel", { tweeter });
  }
  return { tweeterDriverStyleSelectorSetsTopologyAndAppearsInReview: true };
}

// A stored driver_style the picker doesn't know (set via API or a newer
// build, e.g. horn_compression_driver) must render label-only — never a
// guessed floor number — and the picker must not misreport it as "Not sure".
// The server's safety evaluation stays the floor authority.
async function testUnknownDriverStyleRendersWithoutGuessedFloor() {
  const topology = activeTwoWayTopologyPayload();
  topology.speaker_groups[0].channels[1].driver_style = "horn_compression_driver";
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(topology)),
  }));
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Tweeter style: horn compression driver.")) {
    fail("an unknown-to-the-picker style must render its label without a floor", { html });
  }
  if (html.includes("horn compression driver — default minimum crossover") ||
      html.includes("horn compression driver — protective high-pass floor") ||
      /horn compression driver[^<]*5000/.test(html)) {
    fail("an unknown-to-the-picker style must never show a guessed figure", { html });
  }
  if (!html.includes('value="horn_compression_driver" selected')) {
    fail("the picker must show the stored unknown style as selected, not 'Not sure'", { html });
  }
  return { unknownDriverStyleRendersWithoutGuessedFloor: true };
}

// Punch #13 (MEDIUM): a save refusal must surface the server's real error, not
// a false "saved" toast.
async function testDesignDraftSaveRefusalShowsServerErrorNotSavedToast() {
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        posts.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response(
          { error: "speaker design changed in another session" },
          false,
          400,
        ));
      }
      return Promise.resolve(response({ status: "ready_for_review", revision: 3, summary: {}, operator_inputs: {} }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush(); await harness.flush(); await harness.flush();

  if (posts.length !== 1) fail("save action should POST once", { posts });
  const statusNode = harness.elements.get("status");
  if (!statusNode.textContent.includes("speaker design changed in another session")) {
    fail("a 400 refusal must surface the server's real error text", { text: statusNode.textContent });
  }
  if (statusNode.textContent.toLowerCase().includes("updated")) {
    fail("a 400 refusal must not show a success/saved toast", { text: statusNode.textContent });
  }
  if (!statusNode.className.includes("err")) {
    fail("a 400 refusal must render with the error status style", { className: statusNode.className });
  }
  return { designDraftSaveRefusalShowsServerErrorNotSavedToast: true };
}

async function testChannelSelectorKeepsLayoutOpenWhenDraftDirty() {
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
      status: "needs_layout",
      current_step: "layout",
      stepStatuses: {
        layout: "active",
        research: "done",

        experiment: "todo",
        profile: "todo",
      },
      driver_checks: { complete: false, captured: 0, required: 2 },
    }))),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (!initialHtml.includes('data-output-step="layout" open')) {
    fail("active outputs should start on the Speaker layout card", { initialHtml });
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
  if (!dirtyHtml.includes('data-output-step="layout" open')) {
    fail("changing a DAC assignment should keep Speaker layout open for saving", { dirtyHtml });
  }
  if (!dirtyHtml.includes('data-act="save-output-topology"') || !dirtyHtml.includes(">Save</button>")) {
    fail("dirty channel assignment should expose the save action in Speaker layout", { dirtyHtml });
  }
  const reopened = harness.dispatchToggle({
    "data-output-step": "layout",
    open: true,
  });
  if (!reopened.open) {
    fail("dirty Speaker layout should remain reopenable until the draft is saved", { dirtyHtml });
  }
  return { channelSelectorKeepsLayoutOpenWhenDraftDirty: true };
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
  const rowHints = Array.from(
    harness.elements.get("view-body").innerHTML.matchAll(/<div class="output-role__text">([^]*?)<\/div>/g),
    (match) => match[1].match(/<small>([^]*?)<\/small>/)?.[1] || "",
  );
  if (JSON.stringify(rowHints) !== JSON.stringify(["Assign a DAC output.", "", ""])) {
    fail("only an unassigned DAC row should show an assignment hint", { rowHints });
  }

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

  return { threeOutputChannelSelectorDoesNotAutoAssignPeers: true };
}

async function testCompiledProfileApplyBlockStaysUnderstandable() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: confirmedTopology,
    })),
    "./active-speaker/measurements": () => Promise.resolve(response({
      status: "ready_for_baseline",
      summary: summedSummary({}, {
        validated_summed_group_count: 1,
        summed_validation_complete: true,
        latest_summed_validations: {
          main: { validated: true, outcome: "blend_ok" },
        },
      }),
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

async function testAppliedProfileCardUsesCommissioningRecord() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  const applied = {
    exists: true, stands: true, candidate_fingerprint: "0123456789abcdef".repeat(4), applied_at: "2026-09-13T12:00:00Z",
    config_path: "/var/lib/camilladsp/applied.yml", record: "7edfa758981e", disclosures: [],
  };
  const reviewPath = "/var/lib/camilladsp/review.yml";
  const blocker = {severity: "blocker", code: "baseline_profile_apply_failed", message: "Restore failed at the DSP load."};
  for (const [stands, hasPrevious, restoreStatus, alignment, timing] of [
    [true, true, "applied", {status: "measured", reason: null}, "Saved timing: delay 22 µs; polarity normal; provenance measured."],
    [true, false, undefined, {status: "declared", reason: null}, "Saved timing: delay 18 µs; polarity normal; provenance authored_by_model."],
    [false, true, "applied", {status: "alignment_unmeasured", reason: "delay_out_of_bounds"},
      "Saved timing: delay 18 µs; polarity inverted; provenance set_by_user."],
    [true, true, "apply_failed"], [true, true, "unexpected"],
  ]) {
    applied.stands = stands;
    const restores = [];
    let profileReads = 0;
    const fetchHandler = baseFetch({
      "./active-speaker/baseline-profile/restore": (_path, options) => {
        restores.push(options);
        return Promise.resolve(response({status: restoreStatus, issues: restoreStatus === "apply_failed" ? [blocker] : []}));
      },
      "./output-topology": () => Promise.resolve(response({
        output_topology: confirmedTopology,
      })),
      "./active-speaker/commissioning-view": () => Promise.resolve(response(
        profileCommissioningView({
          status: "applied", current_step: "profile",
          applied_profile: applied, first_experiment: {alignment}, timing: timing ? {saved: timing} : undefined,
          stepStatuses: {layout: "done", research: "done", experiment: "done", profile: "done"},
        })
      )),
      "./active-speaker/baseline-profile": () => {
        profileReads += 1;
        return Promise.resolve(response({
        status: "ready_to_compile", candidate_fingerprint: "review-fp",
        permissions: { may_compile: true, may_apply: false },
        config: { path: reviewPath }, issues: [],
        corrections: { woofer: { gain_db: 0 }, tweeter: { gain_db: -7.5 } },
        corrections_source: { woofer: "measured", tweeter: "measured" },
        previous_candidate_fingerprint: hasPrevious ? "previous" : null,
      })); },
    });
    const harness = setupHarness(fetchHandler);
    await loadAndSetActiveState(harness);

    const html = harness.elements.get("view-body").innerHTML;
    if (timing && !html.includes(timing)) fail("Apply card timing is missing", {alignment, timing});
    if (!timing && html.includes("Saved timing")) fail("Missing evidence invented timing");
    if (html.includes('<p class="setting-row__hint"></p>')) fail("Empty timing left a blank hint");
    if (!html.includes(applied.config_path) || html.includes(reviewPath)) {
      fail("Applied profile card used the wrong config path", {applied, reviewPath});
    }
    for (const expected of ["Driver levels", "0 dB (reference)", "-7.5 dB", "Measured",
      applied.candidate_fingerprint.slice(0, 12), applied.record, applied.applied_at]) {
      if (!html.includes(expected)) fail("Applied profile details are missing", {expected, stands, hasPrevious});
    }
    if (html.includes(applied.candidate_fingerprint)) fail("The card should abbreviate the fingerprint");
    if (html.includes('>active</span>') !== stands) fail("Active badge disagrees with displacement", {stands});
    if (html.includes('data-act="restore-baseline-profile"') !== hasPrevious) {
      fail("Restore action disagrees with the previous candidate record", {hasPrevious});
    }
    if (hasPrevious) {
      const readsBeforeRestore = profileReads;
      harness.dispatchClick({"data-act": "restore-baseline-profile"});
      for (let i = 0; i < 8; i += 1) await harness.flush();
      if (restores.length !== 1 || restores[0].method !== "POST") fail("Restore must post once", {restores});
      if (profileReads !== readsBeforeRestore + (restoreStatus === "applied" ? 1 : 0)) {
        fail("Only a successful restore may refresh the composed profile", {profileReads, readsBeforeRestore, restoreStatus});
      }
      const banner = harness.elements.get("status");
      if (restoreStatus === "apply_failed" && banner.textContent !== blocker.message) {
        fail("Restore failure must show its blocker", {banner: banner.textContent});
      }
      if (banner.className.split(" ").includes("status-line--err") !== (restoreStatus !== "applied")) {
        fail("Restore banner must reflect the apply result", {restoreStatus, banner: banner.className});
      }
    }
    if (html.includes('>Save and apply</button>') === stands) {
      fail("The primary apply door disagrees with displacement", {stands});
    }
  }
  return { appliedProfileCardUsesCommissioningRecord: true };
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

// The crossover pickers offer exactly what the compiler builds, and offer it
// from the island rather than from a list in the page. Before this the filter
// select offered Butterworth (which no supported target_type compiles to) and
// the slope was a free `step="6"` number field, so 18 dB/oct was one keystroke
// away — both only refused later, by staging's
// crossover_preview_filter_unsupported blocker.
async function testCrossoverPickersOfferOnlyTheServedVocabulary() {
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response({
      status: "not_saved", summary: {}, operator_inputs: {},
    })),
  });
  const harness = setupHarness(fetchHandler, {
    crossoverVocabulary: {
      filter_types: ["Linkwitz-Riley", "Bessel"],
      slopes_db_per_octave: [12, 24],
      default_filter_type: "Linkwitz-Riley",
      default_slope_db_per_octave: 24,
    },
  });
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes("Butterworth")) {
    fail("the filter picker must not offer a filter the compiler does not build", { html });
  }
  if (!html.includes('<select data-manual-crossover="woofer:tweeter" data-manual-field="slope_db_per_octave">')) {
    fail("slope must be picked from the served set, not typed freely", { html });
  }
  // The served set, whatever it is — a widened SUPPORTED_CROSSOVER_TYPES
  // reaches the page with no edit in main.js.
  for (const expected of [
    '<option value="Bessel">Bessel</option>',
    '<option value="Linkwitz-Riley" selected>Linkwitz-Riley</option>',
    '<option value="12">12 dB/oct</option>',
    '<option value="24" selected>24 dB/oct</option>',
  ]) {
    if (!html.includes(expected)) {
      fail("the pickers must render exactly the served vocabulary", { expected, html });
    }
  }
  if (html.includes('value="48"')) {
    fail("a slope outside the served set must not be offered", { html });
  }
  return { crossoverPickersOfferOnlyTheServedVocabulary: true };
}

// A value the pickers can no longer produce can still arrive from a draft
// saved earlier. The control must SHOW it — a picker that silently displayed a
// neighbouring offered value would put a number on screen that neither the
// model nor the refusal is talking about, and re-picking the displayed value
// fires no change event, so there would be no way to clear it. It must also
// not reach the server as a save that design_draft.py will refuse: the page
// names the pair and the offer first.
async function testStoredUnsupportedCrossoverSlopeBlocksSaveClientSide() {
  const designSaves = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        designSaves.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response({ status: "ready_for_review", summary: {}, operator_inputs: {} }));
      }
      return Promise.resolve(response({
        status: "ready_for_review",
        summary: {},
        operator_inputs: { woofer: "Manual Woofer", tweeter: "Manual Tweeter" },
        manual_settings: {
          drivers: [],
          crossover_candidates: [{
            between_roles: ["woofer", "tweeter"],
            frequency_hz: 2000,
            filter_type: "Linkwitz-Riley",
            slope_db_per_octave: 18,
            confidence: "medium",
          }],
        },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('<option value="18" selected>18 dB/oct (not supported)</option>')) {
    fail("the stored value must be visible and selected, labelled unsupported", { html });
  }
  if (html.includes('<option value="24" selected>')) {
    fail("an unsupported stored slope must not be coerced onto a neighbour", { html });
  }

  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 0) {
    fail("a crossover the compiler cannot build must block the save client-side", { designSaves });
  }
  const blockedHtml = harness.elements.get("view-body").innerHTML;
  if (!blockedHtml.includes("JTS cannot build a 18 dB/oct crossover") ||
      !blockedHtml.includes("12, 24, 48 dB/oct")) {
    fail("the blocked save should name the refused slope and the offer", { blockedHtml });
  }
  return { storedUnsupportedCrossoverSlopeBlocksSaveClientSide: true };
}

// The vocabulary guard is scoped to layouts that HAVE a crossover to author.
// A passive layout never renders the pickers, so a damaged island must not
// stop its save over a vocabulary it does not use.
async function testAPassiveLayoutSavesWithNoCrossoverVocabularyServed() {
  const designSaves = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(
      passiveWithSubwooferTopologyPayload()
    )),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        designSaves.push(JSON.parse(options.body || "{}"));
        return Promise.resolve(response({ status: "ready_for_review", summary: {}, operator_inputs: {} }));
      }
      return Promise.resolve(response({ status: "not_saved", summary: {}, operator_inputs: {} }));
    },
  });
  const harness = setupHarness(fetchHandler, { crossoverVocabulary: {} });
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-target": "main:full_range" }, "Example FR8");
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 1) {
    fail("a passive layout must save without a crossover vocabulary", { designSaves });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes("crossover filter and slope options could not be read")) {
    fail("a layout with no crossover must not be told to reload for one", { html });
  }
  return { aPassiveLayoutSavesWithNoCrossoverVocabularyServed: true };
}

// Reload round-trip: polarity and delay live directly in the single Advanced
// disclosure, so saved values must be visible without another nested accordion.
async function testManualCrossoverAlignmentIsAlwaysVisibleOnSavedDelay() {
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
  if (html.includes("Alignment (advanced)") ||
      html.includes('<details class="advanced driver-research__advanced"')) {
    fail("Alignment should not introduce another disclosure inside Advanced", { html });
  }
  if (!html.includes('<h5 class="setting-row__title">Alignment</h5>') ||
      !html.includes('data-manual-field="upper_polarity"') ||
      !html.includes('value="0.2"')) {
    fail("Reloaded form fields should reflect the saved polarity/delay", { html });
  }
  return { manualCrossoverAlignmentIsAlwaysVisibleOnSavedDelay: true };
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

// The prompt asks for one ```json fenced block, but an operator pastes what the
// chat window gave them: fence markers, a "Here's what I found" preamble, a
// trailing offer to explain. A raw JSON.parse on that failed with a V8 character
// offset, which reads as "your research is broken" rather than "paste the block".
async function testDriverResearchImportToleratesFencesAndProse() {
  const payload = {
    artifact_schema_version: 1,
    kind: "jts_active_crossover_driver_research",
    drivers: [
      { role: "woofer", model: "Imported Woofer" },
      { role: "tweeter", model: "Imported Tweeter" },
    ],
    crossover_candidates: [],
  };
  const body = JSON.stringify(payload, null, 2);
  const accepted = [
    ["bare object", body],
    ["fenced block", "```json\n" + body + "\n```"],
    ["unlabelled fence", "```\n" + body + "\n```"],
    [
      "fence with prose on both sides",
      "Here is what I found for your drivers:\n\n```json\n" + body +
        "\n```\n\nLet me know if you want the measurements too.",
    ],
    [
      "prose-wrapped object with no fence",
      "Sure! " + body + "\n\nHappy to dig deeper on the tweeter.",
    ],
  ];
  for (const [label, text] of accepted) {
    const harness = setupHarness(baseFetch({
      "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    }));
    await loadAndSetActiveState(harness);
    harness.dispatchInput({ "data-driver-import": "" }, text);
    harness.dispatchClick({ "data-act": "parse-driver-research" });
    await harness.flush();
    const statusText = harness.elements.get("status").textContent;
    if (!statusText.includes("Imported driver research.")) {
      fail(`paste-back should accept a ${label}`, { label, statusText });
    }
    const html = harness.elements.get("view-body").innerHTML;
    if (!html.includes("import ready")) {
      fail(`paste-back should summarize a ${label}`, { label });
    }
  }

  // Junk after a value inside the object — a unit suffix or a comment — is the
  // shape the operator actually hit. No recovery can rescue it, so the message
  // has to lead with the action, not the offset.
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
  }));
  await loadAndSetActiveState(harness);
  harness.dispatchInput({ "data-driver-import": "" },
    '```json\n{"kind": "jts_active_crossover_driver_research", "sensitivity_db_2v83_1m": 90 dB}\n```');
  harness.dispatchClick({ "data-act": "parse-driver-research" });
  await harness.flush();
  const statusText = harness.elements.get("status").textContent;
  if (!statusText.includes(
    "Couldn't read that as JSON — paste the complete code block the assistant returned."
  )) {
    fail("unparseable paste should lead with the action, not the parser offset", { statusText });
  }
  if (!/\([\s\S]+\)$/.test(statusText)) {
    fail("unparseable paste should still carry the parser detail in parentheses", { statusText });
  }
  // The detail must come from the recovered block, not from the outer fence:
  // "Unexpected token '`'" would send the operator after the thing they were
  // told to paste instead of the junk inside their JSON.
  if (statusText.includes("```") || statusText.includes("'`'")) {
    fail("parser detail should describe the object, not the fence", { statusText });
  }
  return { driverResearchImportToleratesFencesAndProse: true };
}

async function testDriverResearchImportPreservesOperatorInstalledConfiguration() {
  const designSaves = [];
  const draft = {
    status: "ready_for_review",
    summary: {},
    operator_inputs: {
      target_models: {
        "main:woofer": "Manual Woofer",
        "main:tweeter": "Manual Tweeter",
      },
    },
    manual_settings: {
      drivers: [
        {
          target_id: "main:woofer",
          role: "woofer",
          model: "Manual Woofer",
          cabinet: { enclosure_kind: "sealed" },
        },
        {
          target_id: "main:tweeter",
          role: "tweeter",
          model: "Manual Tweeter",
          driver_class: "compression_horn",
          pad: { kind: "l_pad", series_ohm: 2.2, shunt_ohm: 12 },
        },
      ],
      crossover_candidates: [],
    },
  };
  const importedResearch = {
    artifact_schema_version: 1,
    kind: "jts_active_crossover_driver_research",
    drivers: [
      {
        target_id: "main:woofer",
        role: "woofer",
        model: "Manual Woofer",
        sensitivity_db_2v83_1m: 88,
        cabinet: { enclosure_kind: "vented", baffle_width_mm: 200 },
      },
      {
        target_id: "main:tweeter",
        role: "tweeter",
        model: "Manual Tweeter",
        sensitivity_db_2v83_1m: 95,
        driver_class: "soft_dome",
        pad: { kind: "direct_db", attenuation_db: -9 },
      },
    ],
    crossover_candidates: [],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        return Promise.resolve(response({
          ...draft,
          manual_settings: body.manual_settings,
          operator_inputs: body.operator_inputs,
        }));
      }
      return Promise.resolve(response(draft));
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

  if (designSaves.length !== 1) {
    fail("loaded research should remain saveable", { designSaves });
  }
  const savedDrivers = designSaves[0].manual_settings.drivers;
  const woofer = savedDrivers.find((driver) => driver.target_id === "main:woofer");
  const tweeter = savedDrivers.find((driver) => driver.target_id === "main:tweeter");
  if (woofer.cabinet.enclosure_kind !== "sealed" ||
      tweeter.driver_class !== "compression_horn" ||
      tweeter.pad.kind !== "l_pad") {
    fail("AI import must not replace operator-owned installed configuration", {
      woofer, tweeter,
    });
  }
  if (woofer.sensitivity_db_2v83_1m !== 88 ||
      tweeter.sensitivity_db_2v83_1m !== 95 ||
      woofer.cabinet.baffle_width_mm !== 200) {
    fail("research should still fill non-physical product values", { woofer, tweeter });
  }
  return { driverResearchImportPreservesOperatorInstalledConfiguration: true };
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

async function testLoadedResearchHidesStalePreparedPreview() {
  const topology = activeTwoWayTopologyPayload();
  topology.speaker_groups[0].channels[1].driver_style = "compression_driver";
  const draft = {
    status: "ready_for_review",
    summary: {},
    operator_inputs: {
      target_models: {
        "main:woofer": "Manual Woofer",
        "main:tweeter": "Manual Tweeter",
      },
    },
    manual_settings: {
      drivers: [
        {
          target_id: "main:woofer",
          role: "woofer",
          model: "Manual Woofer",
          cabinet: { enclosure_kind: "sealed" },
        },
        {
          target_id: "main:tweeter",
          role: "tweeter",
          model: "Manual Tweeter",
        },
      ],
      crossover_candidates: [{
        between_roles: ["woofer", "tweeter"],
        frequency_hz: 1800,
        filter_type: "Linkwitz-Riley",
        slope_db_per_octave: 24,
      }],
    },
  };
  const oldPreview = {
    kind: "jts_active_speaker_crossover_preview",
    status: "ready_for_protected_staging",
    summary: { ready_crossover_count: 1, blocker_count: 0 },
    groups: [{
      group_id: "main",
      label: "Main speaker",
      crossovers: [{
        status: "ready_for_review",
        between_roles: ["woofer", "tweeter"],
        proposed_frequency_hz: 1800,
        filters: [{ filter_type: "Linkwitz-Riley", slope_db_per_octave: 24 }],
      }],
    }],
    issues: [],
  };
  const importedResearch = {
    artifact_schema_version: 1,
    kind: "jts_active_crossover_driver_research",
    drivers: [
      { target_id: "main:woofer", role: "woofer", model: "Manual Woofer" },
      { target_id: "main:tweeter", role: "tweeter", model: "Manual Tweeter" },
    ],
    crossover_candidates: [{
      between_roles: ["woofer", "tweeter"],
      frequency_hz: 2400,
      filter_type: "Linkwitz-Riley",
      slope_db_per_octave: 24,
      confidence: "high",
    }],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(topology)),
    "./active-speaker/design-draft": () => Promise.resolve(response(draft)),
    "./active-speaker/crossover-preview": () => Promise.resolve(response(oldPreview)),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-import": "" }, JSON.stringify(importedResearch));
  harness.dispatchClick({ "data-act": "parse-driver-research" });
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  const proposal = html.slice(
    html.indexOf("Proposed starting crossover"),
    html.indexOf("data-driver-advanced")
  );
  if (!proposal.includes("2.4 kHz") ||
      !proposal.includes("working proposal") ||
      proposal.includes("1.8 kHz") ||
      proposal.includes("preview ready")) {
    fail("loaded working values should replace a stale prepared preview immediately", {
      proposal,
    });
  }
  return { loadedResearchHidesStalePreparedPreview: true };
}

// #2186 field case (jts5, Dayton CX120-8): the old prompt called null "a
// correct answer", so an honest researcher returned the tweeter's required
// high-pass as kind-only with null cutoff and slope. applyDriverSafetyToSetting
// wrote the halves it had, protectionFiltersFromSetting then dropped the whole
// requirement out of the POST, and the operator got a cheerful success toast
// with the protection silently gone. Before this guard the paste was ACCEPTED
// ("Imported driver research." + an "import ready" pill), which is what makes
// this a fail-first test: run the harness against the pre-change main.js and
// the two assertions below both fail.
async function testDriverResearchNullProtectionNumbersAreRefusedNotDropped() {
  const honestNullPacket = {
    artifact_schema_version: 2,
    kind: "jts_active_crossover_driver_research",
    request_fingerprint: "a".repeat(64),
    drivers: [
      { target_id: "main:woofer", role: "woofer", model: "Manual Woofer" },
      {
        target_id: "main:tweeter",
        role: "tweeter",
        model: "Dayton CX120-8",
        usable_frequency_range_hz: [4500, 20000],
        sensitivity_db_2v83_1m: 89.2,
        // The exact shape the retired "null is a correct answer" rule invited.
        required_protection_filters: [{
          kind: "highpass",
          cutoff_hz: null,
          minimum_slope_db_per_octave: null,
          family_or_equivalent: "equivalent_or_steeper",
        }],
        unknowns: ["required high-pass cutoff and slope are not published"],
      },
    ],
    crossover_candidates: [],
  };
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
  }));
  await loadAndSetActiveState(harness);
  const pasted = JSON.stringify(honestNullPacket, null, 2);
  harness.dispatchInput({ "data-driver-import": "" }, pasted);
  harness.dispatchClick({ "data-act": "parse-driver-research" });
  await harness.flush();

  const statusText = harness.elements.get("status").textContent;
  if (statusText.includes("Imported driver research.")) {
    fail("a filter with null cutoff/slope must not import as if it were storable",
      { statusText });
  }
  if (!statusText.includes("without both a cutoff and a minimum slope") ||
      !statusText.includes("tweeter")) {
    fail("the refusal must name the driver and the missing numbers", { statusText });
  }
  if (!statusText.includes("conservative estimate, not null")) {
    fail("the refusal must say what the right answer looks like", { statusText });
  }
  // An explanation the operator cannot act on is only half a fix: the paste has
  // to survive so they can re-ask or hand-type the two numbers.
  const importBox = harness.elements.get("view-body").innerHTML;
  if (!importBox.includes("Dayton CX120-8")) {
    fail("a refused paste must stay in the box for the operator to act on", {
      importBox: importBox.slice(0, 400),
    });
  }
  if (importBox.includes("import ready")) {
    fail("a refused paste must not render as a ready import", {
      importBox: importBox.slice(0, 400),
    });
  }

  // The guard mirrors its server twin (_positive_float), which refuses '' and
  // 0 as well as null -- a bare null check would let both through to a server
  // 400 or, worse, to protectionFiltersFromSetting's drop. It must NOT be
  // tighter than the server either: a numeric string is accepted by float()
  // server-side, so it is accepted here.
  const refused = [["empty string", ""], ["zero", 0], ["negative", -1]];
  for (const [label, value] of refused) {
    const packet = _honestNullPacket();
    packet.drivers[1].required_protection_filters = [{
      kind: "highpass",
      cutoff_hz: value,
      minimum_slope_db_per_octave: 24,
      family_or_equivalent: "equivalent_or_steeper",
    }];
    const box = setupHarness(baseFetch({
      "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    }));
    await loadAndSetActiveState(box);
    box.dispatchInput({ "data-driver-import": "" }, JSON.stringify(packet));
    box.dispatchClick({ "data-act": "parse-driver-research" });
    await box.flush();
    const text = box.elements.get("status").textContent;
    if (!text.includes("without both a cutoff and a minimum slope")) {
      fail(`a ${label} cutoff must be refused like a null one`, { label, text });
    }
  }

  // ... and the subset property: what the server accepts, this accepts.
  const numericString = _honestNullPacket();
  numericString.drivers[1].required_protection_filters = [{
    kind: "highpass",
    cutoff_hz: "4500",
    minimum_slope_db_per_octave: "24",
    family_or_equivalent: "equivalent_or_steeper",
  }];
  const ok = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
  }));
  await loadAndSetActiveState(ok);
  ok.dispatchInput({ "data-driver-import": "" }, JSON.stringify(numericString));
  ok.dispatchClick({ "data-act": "parse-driver-research" });
  await ok.flush();
  const okText = ok.elements.get("status").textContent;
  if (okText.includes("without both a cutoff and a minimum slope")) {
    fail("the browser guard must not be tighter than its server twin", { okText });
  }
  return { driverResearchNullProtectionNumbersAreRefusedNotDropped: true };
}

// #2186 resilience follow-up. The import-boundary guard above is only one of
// the three places the silent drop had to die, and the other two live on the
// SAVE path -- which nothing in this harness exercised. Both of these tests
// must go RED against a revert of their half; the status line alone is not
// enough, because the operator's next ordinary click overwrites it and the
// panel is then the only surviving account of what happened.
function _honestNullPacket(fingerprint) {
  return {
    artifact_schema_version: 2,
    kind: "jts_active_crossover_driver_research",
    request_fingerprint: fingerprint || "a".repeat(64),
    drivers: [
      { target_id: "main:woofer", role: "woofer", model: "Manual Woofer" },
      {
        target_id: "main:tweeter",
        role: "tweeter",
        model: "Dayton CX120-8",
        required_protection_filters: [{
          kind: "highpass",
          cutoff_hz: null,
          minimum_slope_db_per_octave: null,
          family_or_equivalent: "equivalent_or_steeper",
        }],
      },
    ],
    crossover_candidates: [],
  };
}

async function _harnessWithNamedDrivers(designSaves) {
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        designSaves.push(body);
        // The server saves the visible values and NOT the dropped packet --
        // which is exactly why ingestDesignDraft would blank the paste box.
        return Promise.resolve(response({
          status: "ready_for_review",
          summary: { manual_driver_count: 2 },
          manual_settings: body.manual_settings,
          driver_research: body.driver_research,
          operator_inputs: body.operator_inputs || {},
        }));
      }
      return Promise.resolve(response({
        status: "not_saved", summary: {}, operator_inputs: {},
      }));
    },
  }));
  await loadAndSetActiveState(harness);
  harness.dispatchInput({ "data-driver-field": "woofer" }, "Manual Woofer");
  harness.dispatchInput({ "data-driver-field": "tweeter" }, "Manual Tweeter");
  return harness;
}

async function testRejectedImportReasonSurvivesTheSaveInThePanel() {
  // The paste -> "Update working setup" path WITHOUT pressing Parse first.
  // Nothing else guards it: the reason is produced inside saveDriverResearchDraft
  // and was then cleared before the first render.
  const designSaves = [];
  const harness = await _harnessWithNamedDrivers(designSaves);
  harness.dispatchInput(
    { "data-driver-import": "" },
    JSON.stringify(_honestNullPacket(), null, 2)
  );
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  if (designSaves.length !== 1) {
    fail("the visible values should still save when the packet is dropped", {
      designSaves,
    });
  }
  if (designSaves[0].driver_research !== null) {
    fail("an unstorable packet must not be sent as research evidence", {
      sent: designSaves[0].driver_research,
    });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("driver-research__error")) {
    fail("the drop reason must reach the panel, not only the status line", { html });
  }
  if (!html.includes("without both a cutoff and a minimum slope")) {
    fail("the panel must name WHY the packet was dropped", {
      panel: html.slice(html.indexOf("driver-research__error") - 200, 600),
    });
  }
  const statusText = harness.elements.get("status").textContent;
  if (!statusText.includes("Imported JSON was not saved")) {
    fail("the toast should still name the outcome", { statusText });
  }
  return { rejectedImportReasonSurvivesTheSaveInThePanel: true };
}

async function testRejectedPasteAndReasonSurviveDraftIngest() {
  // ingestDesignDraft blanks importText whenever the saved draft carries no
  // driver_research. Handing back an explanation with nothing to act on is
  // half a fix, so the paste has to survive the round trip too.
  const designSaves = [];
  const harness = await _harnessWithNamedDrivers(designSaves);
  const pasted = JSON.stringify(_honestNullPacket(), null, 2);
  harness.dispatchInput({ "data-driver-import": "" }, pasted);
  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Dayton CX120-8")) {
    fail("a refused paste must survive the draft ingest so it can be fixed", {
      html: html.slice(0, 600),
    });
  }
  if (!html.includes("driver-research__error")) {
    fail("the reason must survive the draft ingest alongside the paste", { html });
  }

  // The v2-invalidation drop path is the OTHER way a packet gets dropped
  // here (valid JSON, but no longer bound to the current request). It must
  // reach the panel too, not just the ephemeral status line.
  const invalidationSaves = [];
  const second = await _harnessWithNamedDrivers(invalidationSaves);
  const boundPacket = _honestNullPacket();
  boundPacket.drivers[1].required_protection_filters = [{
    kind: "highpass",
    cutoff_hz: 4500,
    minimum_slope_db_per_octave: 24,
    family_or_equivalent: "equivalent_or_steeper",
  }];
  second.dispatchInput(
    { "data-driver-import": "" }, JSON.stringify(boundPacket, null, 2)
  );
  second.dispatchClick({ "data-act": "save-driver-design" });
  await second.flush();
  await second.flush();
  await second.flush();
  const secondHtml = second.elements.get("view-body").innerHTML;
  if (!secondHtml.includes("invalidated by a visible edit")) {
    fail("the v2-invalidation drop must also explain itself in the panel", {
      secondHtml: secondHtml.slice(0, 800),
    });
  }
  if (!secondHtml.includes("Dayton CX120-8")) {
    fail("the v2-invalidation drop must keep the paste too", {
      secondHtml: secondHtml.slice(0, 600),
    });
  }
  return { rejectedPasteAndReasonSurviveDraftIngest: true };
}

// --- #2195: the best-estimate provenance echo-back --------------------------
//
// The ask now wants the researcher's BEST number rather than a timid one, so
// the operator has to be able to arbitrate. That is this panel: every consumed
// value, its published/estimated badge, and its one citation. It REPLACED a
// bare tally ("2 of these limits came from the research reply as estimates"),
// which named a number to distrust without naming which one.
//
// The view carried an `hf_measurement_abs_ceiling_dbfs` until 2026-08-20, and
// these fixtures deliberately SET it to a number that was not the real -35, so
// a page hardcoding the constant would have failed here. That constant is
// retired and the field no longer goes on the wire; the delegation test pins
// instead that the page quotes no dBFS bound at all, which no fixture value
// can fake.
const ECHO_TWEETER_CLASS_CEILING_DBFS = -65;
// The low-frequency class ceiling, which LIMITS permits a woofer to declare
// exactly. Delegation is a high-frequency-only rule, so landing on this number
// must NOT produce it -- see the woofer half of the delegation test.
const ECHO_WOOFER_CLASS_CEILING_DBFS = 0;

// Shaped like driver_protection_policy_view: target_id + role_class +
// max_auto_level_dbfs + the resolved low limit with its provenance. No `role`
// -- the view stopped emitting one, because role_class answers every question
// the page asks. The raw class-table `min_highpass_hz` is gone too (#2874): it
// sat unlabelled beside a declared figure, and what replaced it says which of
// the two bounds the corner.
function echoProtectionPolicy(overrides = {}) {
  return {
    policy_version: "driver_protection_auto_level_v1",
    targets: [
      {
        target_id: "main:woofer",
        role_class: "low_frequency",
        max_auto_level_dbfs: ECHO_WOOFER_CLASS_CEILING_DBFS,
        low_limit_hz: null,
        low_limit_provenance: null,
        low_limit_summary: null,
      },
      {
        target_id: "main:tweeter",
        role_class: "high_frequency",
        max_auto_level_dbfs: ECHO_TWEETER_CLASS_CEILING_DBFS,
        low_limit_hz: 3000,
        low_limit_provenance: "style_default",
        low_limit_summary: "3000 Hz (class fallback; nothing declared)",
      },
    ],
    ...overrides,
  };
}

// One reply, four provenance shapes on purpose: a URL citation, a plain-text
// citation (a datasheet is usually a NAME), an entry with no citation, and a
// consumed value with no provenance entry at all.
//
// It also carries the fields the FIRST cut of this panel left unechoed. Two of
// the three it originally missed survive — measurement_band_hz and cabinet, both
// frozen into the confirmed safety profile by _profile_core, so a panel that
// claims completeness at the confirmation gate has to name them. The third,
// crossover_search_band_hz, was deleted outright by #2870.
function echoResearchPacket(tweeterPeakDbfs = ECHO_TWEETER_CLASS_CEILING_DBFS) {
  return {
    artifact_schema_version: 2,
    kind: "jts_active_crossover_driver_research",
    request_fingerprint: "b".repeat(64),
    drivers: [
      {
        target_id: "main:woofer",
        target_fingerprint: "c".repeat(64),
        role: "woofer",
        model: "Manual Woofer",
        hard_excitation_band_hz: [30, 5000],
        measurement_band_hz: [40, 3000],
        // Carries an enclosure_kind on purpose. It is an operator-declared
        // installation choice, so the panel must echo the GEOMETRY and never
        // the enclosure -- the "no enclosure" assertion in the echo test has
        // something to catch only because this value is here to leak.
        cabinet: {
          enclosure_kind: "sealed",
          radiator_count: 1,
          effective_radiating_diameter_mm: 116,
          baffle_width_mm: 200,
        },
        field_provenance: {
          hard_excitation_band_hz: {
            confidence: "high",
            basis: "datasheet usable range",
            source: "https://example.test/w6-datasheet.pdf",
            sources: ["https://example.test/w6-datasheet.pdf"],
          },
          measurement_band_hz: {
            confidence: "high",
            basis: "datasheet piston band",
            source: "https://example.test/w6-datasheet.pdf",
            sources: [],
          },
          // cabinet: consumed and FROZEN, asserted nothing about. Silence is
          // not a publication claim, so this must badge `estimated`.
        },
      },
      {
        target_id: "main:tweeter",
        target_fingerprint: "d".repeat(64),
        role: "tweeter",
        model: "Dayton CX120-8",
        hard_excitation_band_hz: [2500, 20000],
        required_protection_filters: [{
          kind: "highpass",
          cutoff_hz: 3000,
          minimum_slope_db_per_octave: 24,
          family_or_equivalent: "equivalent_or_steeper",
        }],
        level_duration_limits: {
          // `null` omits the key -- the ordinary reply since the 2026-08-23
          // ruling made this a published-fact-or-omit field.
          ...(tweeterPeakDbfs === null
            ? {}
            : { max_effective_peak_dbfs: tweeterPeakDbfs }),
          max_sweep_duration_s: 4,
          max_repeat_count: 3,
          minimum_cooldown_s: 2,
        },
        // Consumed, but the reply asserted nothing about it.
        sensitivity_db_2v83_1m: 89.2,
        field_provenance: {
          hard_excitation_band_hz: {
            confidence: "medium",
            basis: "independent measurement",
            source: "Dayton CX120-8 datasheet, p.2",
            sources: [],
          },
          required_protection_filters: {
            confidence: "low",
            basis: "estimated: 25 mm soft dome, Fs unpublished",
            sources: [],
          },
          level_duration_limits: {
            confidence: "low",
            basis: "estimated: protocol default, no published limit",
            source: "measurement protocol, no published limit",
            sources: [],
          },
        },
      },
    ],
    crossover_candidates: [],
  };
}

function echoManualSettings(research) {
  return {
    drivers: research.drivers.map((driver) => ({
      target_id: driver.target_id,
      role: driver.role,
      model: driver.model,
      hard_excitation_band_hz: driver.hard_excitation_band_hz,
      measurement_band_hz: driver.measurement_band_hz,
      required_protection_filters: driver.required_protection_filters,
      level_duration_limits: driver.level_duration_limits,
      sensitivity_db_2v83_1m: driver.sensitivity_db_2v83_1m,
      cabinet: driver.cabinet,
    })),
    crossover_candidates: [],
  };
}

function echoDraft({ research, policy } = {}) {
  const packet = research || echoResearchPacket();
  const draft = designDraftWithSafety({
    status: "confirmed",
    confirmed_and_current: true,
    reasons: [],
  });
  draft.driver_research_request = {
    request_fingerprint: packet.request_fingerprint,
    targets: [],
  };
  draft.driver_research = packet;
  draft.manual_settings = echoManualSettings(packet);
  // `driver_protection_policy_view`, not `driver_protection_policy`: the
  // latter is taken by a different shape inside excitation_safety_plan's
  // protection-requirement fingerprint, and one name for two shapes is how a
  // reader ends up consuming the wrong one.
  if (policy !== null) {
    draft.driver_protection_policy_view = policy || echoProtectionPolicy();
  }
  return draft;
}

async function echoHarness(draft) {
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response(draft)),
  }));
  await loadAndSetActiveState(harness);
  return harness.elements.get("view-body").innerHTML;
}

function echoPanel(html) {
  const at = html.indexOf("driver-research__panel driver-echo");
  if (at < 0) fail("expected the echo-back panel to render", { html });
  return html.slice(at, html.indexOf("driver-research__proposal"));
}

async function testResearchEchoBackNamesEveryValueWithBadgeAndSource() {
  const panel = echoPanel(await echoHarness(echoDraft()));

  // The superseded tally must be gone, not merely moved.
  if (panel.includes("came from the research reply as estimate")) {
    fail("the estimate tally must not survive alongside the panel", { panel });
  }

  // What we're RUNNING WITH, per value, read out of the working setting.
  // The last two are what survives of the completeness fix: both are frozen
  // into the confirmed safety profile by _profile_core, and the first cut of
  // this panel echoed neither while claiming to echo everything. Its third
  // field, the crossover-search band, was deleted by #2870.
  for (const expected of [
    "Never test outside",
    "30 Hz to 5.0 kHz",
    "2.5 kHz to 20 kHz",
    "Protection filter",
    "high-pass 3.0 kHz, 24 dB/oct or steeper",
    "Test level and duration",
    "-65.0 dBFS peak, sweeps up to 4 s, 3 repeats, 2 s cooldown",
    "Sensitivity",
    "+89.2 dB",
    "Measure inside",
    "40 Hz to 3.0 kHz",
    "Cabinet geometry",
    "1 radiator, 116 mm effective diameter, 200 mm baffle",
  ]) {
    if (!panel.includes(expected)) {
      fail("the echo-back must name the value JTS is running with", {
        expected, panel,
      });
    }
  }

  // The headline's completeness claim is scoped to exactly what renders. It
  // may not drift back into claiming the whole reply.
  if (!panel.includes(
    "Every value the research reply gave us that JTS asked it to source, or " +
    "that gets frozen into this speaker&rsquo;s safety limits."
  )) {
    fail("the panel must claim only the completeness it actually delivers", {
      panel,
    });
  }

  // The woofer's cabinet carries enclosure_kind "sealed". It is an
  // operator-declared installation choice the ask is forbidden to infer, so
  // the geometry is echoed and the enclosure is not.
  if (panel.includes("sealed")) {
    fail("an operator-declared enclosure must not be echoed as research", {
      panel,
    });
  }

  // Badges are DERIVED from confidence: high and medium assert a published
  // figure, low does not, and neither does silence. `cabinet` is the silence
  // case among the newly-echoed pair.
  const confirmed = (panel.match(/>confirmed</g) || []).length;
  const estimated = (panel.match(/>estimated</g) || []).length;
  if (confirmed !== 3 || estimated !== 4) {
    fail("badge derivation must follow confidence (high/medium -> confirmed)", {
      confirmed, estimated, panel,
    });
  }

  // A URL citation is a link; a datasheet NAME is not, and neither is absent.
  // target="_blank" because the panel renders BEFORE the save: following a
  // citation in this tab would discard the pasted JSON being checked.
  if (!panel.includes(
    '<a class="driver-echo__source" href="https://example.test/w6-datasheet.pdf"' +
    ' target="_blank" rel="noreferrer noopener">' +
    'https://example.test/w6-datasheet.pdf</a>'
  )) {
    fail("an http(s) source must linkify and open in a new tab", { panel });
  }
  if (!panel.includes(
    '<span class="driver-echo__source">Dayton CX120-8 datasheet, p.2</span>'
  )) {
    fail("a non-URL source must render as plain escaped text", { panel });
  }
  if (!panel.includes("no source given")) {
    fail("a citation-less assertion must say so rather than look sourced", {
      panel,
    });
  }
  return { researchEchoBackNamesEveryValueWithBadgeAndSource: true };
}

async function testResearchEchoBackDisclosesTheDelegation() {
  // A tweeter that declares NO level limit has DELEGATED the level, and
  // protection derives it. Saying nothing would leave the household with a
  // level row that never mentions the loudest fact about it (#2192).
  //
  // Absence is the ordinary shape since the 2026-08-23 ruling made the field
  // published-fact-or-omit. A profile saved before that carries the class
  // ceiling itself, which said the same thing, so both must disclose.
  const undeclared = echoPanel(await echoHarness(
    echoDraft({ research: echoResearchPacket(null) })
  ));
  if (!undeclared.includes("Test level here is left to JTS")) {
    fail("an undeclared peak must disclose the delegation", { undeclared });
  }
  const onStoredSeed = echoPanel(await echoHarness(echoDraft()));
  if (!onStoredSeed.includes("Test level here is left to JTS")) {
    fail("a declared peak on the class ceiling must disclose the delegation", {
      onStoredSeed,
    });
  }
  // The sentence names WHAT sets the level, never a dBFS number. The absolute
  // ceiling it used to quote was retired on 2026-08-20 (the provisional -35
  // dBFS constant); the real bound is the per-driver sensitivity derivation,
  // which this topology-only policy view cannot compute. A number here would
  // have to be invented, so any dBFS in this sentence is a regression.
  if (!onStoredSeed.includes(
    "from this driver’s declared sensitivity against the low-frequency " +
    "driver’s own limit"
  )) {
    fail("the disclosure must name what sets the level", { onStoredSeed });
  }
  const disclosedSentence = onStoredSeed.slice(
    onStoredSeed.indexOf("Test level here is left to JTS")
  ).split("</p>")[0];
  if (/dBFS/.test(disclosedSentence)) {
    fail("the disclosure must quote no dBFS bound — the -35 constant is retired", {
      disclosedSentence,
    });
  }

  // One decibel off the ceiling is a deliberate quieter choice, honoured
  // literally -- no delegation, so no sentence.
  const deliberate = echoPanel(await echoHarness(
    echoDraft({ research: echoResearchPacket(ECHO_TWEETER_CLASS_CEILING_DBFS - 1) })
  ));
  if (deliberate.includes("Test level here is left to JTS")) {
    fail("a deliberate quieter peak must not claim delegation", { deliberate });
  }

  // No policy from the server means no bound to state. Say nothing rather than
  // invent a number.
  const noPolicy = echoPanel(await echoHarness(echoDraft({ policy: null })));
  if (noPolicy.includes("Test level here is left to JTS")) {
    fail("without a server policy the disclosure must stay silent", { noPolicy });
  }
  if (!noPolicy.includes("Test level and duration")) {
    fail("a missing policy must not take the rest of the panel with it", {
      noPolicy,
    });
  }

  // The inverse of the pin this replaced. Until 2026-08-20 the sentence
  // required an absolute ceiling from the server and stayed silent without one
  // (so that a dropped bound could not render a fabricated "+0.0 dBFS"). The
  // server no longer sends any such field, so that guard would now silence the
  // delegation disclosure on every real box. Pinned in the new direction: a
  // policy view carrying NO ceiling field still discloses, and still quotes no
  // number.
  const noBound = echoPanel(await echoHarness(echoDraft({
    policy: echoProtectionPolicy({ hf_measurement_abs_ceiling_dbfs: null }),
  })));
  if (!noBound.includes("Test level here is left to JTS")) {
    fail("the disclosure must not depend on a server-sent absolute bound", {
      noBound,
    });
  }
  if (noBound.includes("dBFS.")) {
    fail("a ceiling field on the wire must not resurrect a quoted bound", {
      noBound,
    });
  }

  // The role_class gate, pinned. Delegation is a HIGH-FREQUENCY rule --
  // resolve_driver_excitation_ceilings only supersedes a declared peak for an
  // HF role -- but LIMITS lets a woofer declare exactly 0.0 dBFS, which is its
  // own class ceiling. Without the gate that woofer is told its level is "left
  // to JTS", picked from a sensitivity derivation, directly under a row where
  // the household typed the number itself: a delegation claim the server never
  // performs, on the biggest driver.
  const lfOnCeiling = echoResearchPacket();
  lfOnCeiling.drivers[0].level_duration_limits = {
    max_effective_peak_dbfs: ECHO_WOOFER_CLASS_CEILING_DBFS,
    max_sweep_duration_s: 6,
    max_repeat_count: 3,
    minimum_cooldown_s: 2,
  };
  const lfPanel = echoPanel(await echoHarness(
    echoDraft({ research: lfOnCeiling })
  ));
  // Positive control: the woofer's own peak row is on screen, so the absence
  // asserted below is the gate holding rather than a row that never rendered.
  if (!lfPanel.includes("0.0 dBFS peak, sweeps up to 6 s")) {
    fail("expected the woofer to declare its class ceiling in this fixture", {
      lfPanel,
    });
  }
  const disclosures = (lfPanel.match(/Test level here is left to JTS/g) || []).length;
  if (disclosures !== 1) {
    fail("only the high-frequency target may disclose a delegation", {
      disclosures, lfPanel,
    });
  }
  const wooferBlock = lfPanel.split('<section class="driver-echo__driver">')[1] || "";
  if (!wooferBlock.includes("Woofer / midbass")) {
    fail("expected the first echo block to be the woofer", { wooferBlock });
  }
  if (wooferBlock.includes("Test level here is left to JTS")) {
    fail("a low-frequency target on its class ceiling delegates nothing", {
      wooferBlock,
    });
  }
  return { researchEchoBackDisclosesTheDelegation: true };
}

async function testResearchEchoBackEscapesUntrustedSources() {
  // `source` is free text pasted from an LLM reply: untrusted input reaching
  // innerHTML. Neither branch may emit live markup, and a javascript: URL must
  // not satisfy the linkify test.
  //
  // Three payloads, one per branch, because the first two both FAIL the
  // linkify regex and route to the text branch -- leaving the anchor branch's
  // two escapeHtml calls (href slot and text slot) unpinned. The third is
  // URL-shaped on purpose: it starts `https://` and contains no whitespace, so
  // it PASSES the regex and is the only payload that reaches them.
  const research = echoResearchPacket();
  const urlShaped = 'https://a.test/x"><script>alert(1)</script>';
  const urlShapedEscaped =
    "https://a.test/x&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;";
  research.drivers[0].field_provenance.hard_excitation_band_hz.source =
    "javascript:alert(1)";
  research.drivers[0].field_provenance.hard_excitation_band_hz.sources = [];
  research.drivers[0].field_provenance.measurement_band_hz.source = urlShaped;
  research.drivers[1].field_provenance.level_duration_limits.source =
    '<img src=x onerror="alert(1)">';
  const panel = echoPanel(await echoHarness(echoDraft({ research })));

  if (panel.includes("<img src=x") || panel.includes("href=\"javascript:")) {
    fail("an untrusted source must never reach innerHTML as markup", { panel });
  }
  if (!panel.includes("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;")) {
    fail("a markup-shaped source must render escaped, not dropped", { panel });
  }
  if (!panel.includes(
    '<span class="driver-echo__source">javascript:alert(1)</span>'
  )) {
    fail("a non-http(s) scheme must render as text, never as a link", { panel });
  }

  // The anchor branch: both slots escaped, in one exact string, so dropping
  // escapeHtml from EITHER the href or the link text fails here.
  if (!panel.includes(
    '<a class="driver-echo__source" href="' + urlShapedEscaped +
    '" target="_blank" rel="noreferrer noopener">' + urlShapedEscaped + '</a>'
  )) {
    fail("a URL-shaped payload must be escaped in both anchor slots", { panel });
  }
  if (panel.includes("<script>") || panel.includes('x">')) {
    fail("a URL-shaped payload must not break out of the anchor", { panel });
  }
  return { researchEchoBackEscapesUntrustedSources: true };
}

async function testResearchEchoBackFollowsTheSameCurrencyRulesAsTheEvidence() {
  // Two rules already govern the Advanced evidence block, and the panel must
  // not become a second surface that keeps showing stale authority:
  //   * a v2 packet with no bound request is describing some other request;
  //   * an edited target's values are no longer the reply's.
  const unbound = echoDraft();
  delete unbound.driver_research_request;
  const unboundHtml = await echoHarness(unbound);
  if (unboundHtml.includes("driver-research__panel driver-echo")) {
    fail("an unbound v2 packet must not render an echo-back", {
      unboundHtml: unboundHtml.slice(0, 600),
    });
  }

  // A manual driver edit invalidates the v2 binding (setManualDriverField ->
  // invalidateDriverResearchBinding), so the whole panel goes with it. Typing
  // in a number input deliberately does NOT full-render (focus loss), so this
  // only holds because the panel has its own targeted refresh.
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response(echoDraft())),
  }));
  await loadAndSetActiveState(harness);
  if (!harness.elements.get("view-body").innerHTML.includes("driver-echo__rows")) {
    fail("expected the bound packet to echo before the edit", {});
  }
  harness.dispatchInput({
    "data-manual-driver": "main:tweeter",
    "data-manual-field": "hard_excitation_min_hz",
  }, "2600");
  const afterEdit = harness.elements.get("driver-echo-control").innerHTML;
  if (afterEdit !== "") {
    fail("an edit that unbinds the packet must clear the echo, not go stale", {
      afterEdit,
    });
  }

  // A legacy v1 packet has no binding to invalidate, so the per-target rule is
  // what stops its badges from outliving an edit.
  const legacy = echoResearchPacket();
  legacy.artifact_schema_version = 1;
  delete legacy.request_fingerprint;
  const legacyDraft = echoDraft({ research: legacy });
  delete legacyDraft.driver_research_request;
  const legacyHarness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response(legacyDraft)),
  }));
  await loadAndSetActiveState(legacyHarness);
  legacyHarness.dispatchInput({
    "data-manual-driver": "main:tweeter",
    "data-manual-field": "hard_excitation_min_hz",
  }, "2600");
  const edited = legacyHarness.elements.get("driver-echo-control").innerHTML;
  if (!edited.includes("You changed these values, so the research reply no longer describes them.")) {
    fail("an edited target must say the badges no longer apply", { edited });
  }
  if (edited.includes("Dayton CX120-8 datasheet, p.2")) {
    fail("an edited target must not keep showing the reply's citations", {
      edited,
    });
  }
  // The untouched woofer keeps its evidence.
  if (!edited.includes("https://example.test/w6-datasheet.pdf")) {
    fail("editing one target must not blank the others", { edited });
  }
  return { researchEchoBackFollowsTheSameCurrencyRulesAsTheEvidence: true };
}

async function testResearchEchoBackRendersRightAfterAPaste() {
  // The ingest path above is the reload half. This is the one the ruling names:
  // the operator pastes, and the answer to "what did we just take from that?"
  // is on screen without saving anything.
  const draft = echoDraft();
  const packet = draft.driver_research;
  delete draft.driver_research;
  delete draft.manual_settings;
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(response(draft)),
  }));
  await loadAndSetActiveState(harness);
  if (harness.elements.get("view-body").innerHTML.includes("driver-echo__rows")) {
    fail("nothing pasted yet means nothing to echo back", {});
  }

  harness.dispatchInput({ "data-driver-import": "" }, JSON.stringify(packet));
  harness.dispatchClick({ "data-act": "parse-driver-research" });
  await harness.flush();

  const panel = echoPanel(harness.elements.get("view-body").innerHTML);
  for (const expected of [
    "3. What JTS is running with",
    "high-pass 3.0 kHz, 24 dB/oct or steeper",
    "Dayton CX120-8 datasheet, p.2",
    "Test level here is left to JTS",
  ]) {
    if (!panel.includes(expected)) {
      fail("a fresh paste must echo straight back", { expected, panel });
    }
  }
  return { researchEchoBackRendersRightAfterAPaste: true };
}

async function testDriverResearchPromptCopyUsesHttpFallback() {
  let copiedText = "";
  let asyncClipboardCalled = false;
  const researchRequests = [];
  const topology = activeTwoWayTopologyPayload();
  topology.speaker_groups[0].channels[1].driver_style = "compression_driver";
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
    "./output-topology": () => Promise.resolve(response(topology)),
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
  harness.dispatchInput({
    "data-manual-driver": "main:woofer",
    "data-manual-field": "enclosure_kind",
  }, "sealed");
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
  const topology = activeTwoWayTopologyPayload();
  topology.speaker_groups[0].channels[1].driver_style = "compression_driver";
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
    "./output-topology": () => Promise.resolve(response(topology)),
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
  harness.dispatchInput({
    "data-manual-driver": "main:woofer",
    "data-manual-field": "enclosure_kind",
  }, "sealed");
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
              status: "needs_layout",
              current_step: "layout",
              stepStatuses: {
                layout: "active",
                research: "done",

                experiment: "todo",
                profile: "todo",
              },
            }
          : {
              status: "needs_driver_values",
              current_step: "research",
              stepStatuses: {
                layout: "done",
                research: "active",
                experiment: "todo",
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
  if (commissioningViewFetches < 2) {
    fail("Preview completion should refresh the backend-owned commissioning step", {
      commissioningViewFetches,
    });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!/data-output-step="layout"[^>]* open/.test(html)) {
    fail("Preview completion should open Speaker layout without a page reload", {
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
  if (!/data-act="save-driver-design" disabled/.test(html)) {
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
  return { driverMicCaptureIsRemovedFromSoundFlow: true };
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
    status: "applied", candidate_fingerprint: "applied-fp", applied_at: "2026-09-13T12:00:00Z",
    permissions: { may_compile: false, may_apply: false },
    config: { path: "/var/lib/camilladsp/configs/active_speaker_baseline.yml" },
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
          active_config_path: baselineApplied.config.path,
        },
        issues: [],
      }));
    },
    "./active-speaker/commissioning-view": () => Promise.resolve(response({
      status: finishPosts.length ? "applied" : "ready_to_save_profile",
      review: {ready: true, may_apply: true},
      applied_profile: {exists: finishPosts.length > 0, stands: finishPosts.length > 0,
        candidate_fingerprint: baselineApplied.candidate_fingerprint,
        applied_at: baselineApplied.applied_at, config_path: baselineApplied.config.path},
      test_level: levelPayload(-72).test_signal,
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

// C3a-7 (symmetric half): the fire-and-forget runCommissionAutoRamp loop must
// ALSO release the single-flight guard on every exit. A render() throw on a
// happy-path step inside the loop body (line ~4285, OUTSIDE postCommission's
// try/catch) rejects the un-awaited loop promise. Without the loop's try/finally
// commissionAutoRamp.running stays true and the card wedges in the disabled
// "Preparing" state forever.
//
// We inject the throw via the status className setter, gated to fire on the
// SECOND render where the merged Speaker layout row shows the playing controls
// ("Stop" + "I hear woofer") — the first is postCommission's post-success render
// (now inside its try/catch after fix #2), the second is the loop's own render()
// at line ~4285. Targeting the second isolates the loop finally: with the loop
// finally removed, the card stays wedged and the test fails; with it present,
// running resets and a fresh click re-runs the flow.

// --- #2814: the same-shape composite re-pin offer ---------------------------

// The mismatch card only renders when the page sees a hardware-mismatch, and a
// swapped dongle on a declared composite surfaces as an observed-serial clock
// blocker (not an id/count mismatch) — so the fixture reproduces that shape
// rather than a shortcut the product never produces.
const REPIN_PLAN = {
  child_count: 2,
  replaced_child_count: 1,
};

// #2812 B1: the mismatch card (and everything nested inside it, including
// the re-pin offer below) now renders only when the server-published
// `hardware_mismatch` says so (jasper.output_topology.declared_hardware_mismatch,
// jasper/web/sound_setup.py) -- the page no longer recomputes the rule
// locally from `output_topology`/`output_hardware`/`clock_domain`. This
// mirrors the shape that function produces for "declared composite, a
// serial-mismatch clock blocker, no fresh hardware observation in this
// response".
const SWAPPED_DONGLE_MISMATCH = {
  saved_label: "Dual Apple USB-C DAC 4-channel pair",
  current_label: "Attached hardware",
  saved_count: 4,
  current_count: 0,
  clock_blockers: [{
    severity: "blocker",
    code: "dual_apple_observed_serial_mismatch",
    message: "current dual-Apple DAC serials do not match the saved topology",
  }],
  message: "Saved topology expects Dual Apple USB-C DAC 4-channel pair " +
    "(4 physical outputs), but current output hardware has not been " +
    "observed. current dual-Apple DAC serials do not match the saved topology.",
};

function swappedDonglePayload(overrides = {}) {
  return {
    output_topology: activeTwoWayTopologyPayload(),
    topology_revision: "sha256:saved",
    hardware_adoption: { allowed: true, identity: "sha256:hardware-swapped" },
    hardware_mismatch: SWAPPED_DONGLE_MISMATCH,
    hardware_repin: REPIN_PLAN,
    clock_domain: {
      status: "dual_apple_composite_clock_blocked",
      issues: [{
        severity: "blocker",
        code: "dual_apple_observed_serial_mismatch",
        message: "current dual-Apple DAC serials do not match the saved topology",
      }],
    },
    ...overrides,
  };
}

async function testRepinOfferDisclosesWhatIsKeptAndWhatMustBeRedone() {
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(swappedDonglePayload())),
    "./output-topology/repin": (path, options = {}) => {
      posts.push({ path, body: JSON.parse(options.body || "{}") });
      return Promise.resolve(response({
        output_topology: activeTwoWayTopologyPayload(),
        topology_revision: "sha256:repinned",
        hardware_adoption: { allowed: true, identity: "sha256:hardware-swapped" },
        hardware_repin: null,
        repin: {
          status: "repinned",
          message: "Pinned the new DAC and kept your speaker setup. Re-run the drift measurement, then Apply the baseline to resume audio.",
        },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  // The disclosure itself is the contract: a household decides between this and
  // the destructive reset on these sentences alone, so they are pinned here the
  // way the reset's dialog copy is.
  const offer = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "Same speakers, one new DAC",
    "keep your speaker layout, driver roles, output assignment and tuning",
    "Apply the baseline to resume audio",
    "Re-run the 15-minute drift measurement",
    "Keep setup, pin the new DAC",
  ]) {
    if (!offer.includes(expected)) {
      fail("the re-pin offer must disclose what is kept and what must be redone", {
        expected, offer,
      });
    }
  }
  // The destructive sibling must stop being the primary action beside it.
  if (!offer.includes('class="btn btn--ghost" data-act="reset-output-topology"')) {
    fail("the full reset should de-emphasise while a re-pin is offered", { offer });
  }

  let confirmation = null;
  globalThis.__jtsConfirm = async (message, options) => {
    confirmation = { message, options };
    return true;
  };
  harness.dispatchClick({ "data-act": "repin-output-topology" });
  await harness.flush(); await harness.flush(); await harness.flush();

  if (!confirmation ||
      !confirmation.message.includes("Apply the baseline to resume audio") ||
      confirmation.options.confirmLabel !== "Pin the new DAC" ||
      confirmation.options.danger !== true) {
    fail("the re-pin confirm must be danger-styled and explain how to resume audio", {
      confirmation,
    });
  }
  if (posts.length !== 1 ||
      posts[0].path !== "./output-topology/repin" ||
      posts[0].body.topology_revision !== "sha256:saved" ||
      posts[0].body.detected_hardware_identity !== "sha256:hardware-swapped") {
    fail("the re-pin must post the preconditions it was offered against", { posts });
  }

  const status = harness.elements.get("status").textContent;
  if (!status.includes("kept your speaker setup") ||
      !status.includes("Apply the baseline to resume audio")) {
    fail("a successful re-pin should explain how to resume audio", { status });
  }
  const after = harness.elements.get("view-body").innerHTML;
  if (after.includes("Keep setup, pin the new DAC") || after.includes(">Pinning<")) {
    fail("a spent re-pin offer must clear, and the busy flag with it", { after });
  }
  return { repinOfferDisclosesWhatIsKeptAndWhatMustBeRedone: true };
}

async function testRepinDeclinedOrFailedClearsTheBusyFlag() {
  // Every exit from the action must leave the button clickable again: a wedged
  // "Pinning" needs a page reload to escape, which on a silenced speaker is the
  // worst moment to require one.
  const cases = [
    { name: "declined", confirm: false },
    { name: "conflict", confirm: true, status: 409, body: {
      error: "Speaker setup or detected hardware changed. Review it and try again.",
      output_topology: activeTwoWayTopologyPayload(),
      topology_revision: "sha256:moved",
      hardware_adoption: { allowed: true, identity: "sha256:hardware-moved" },
      hardware_mismatch: swappedDonglePayload().hardware_mismatch,
      hardware_repin: REPIN_PLAN,
      clock_domain: swappedDonglePayload().clock_domain,
      conflict: "detected_hardware_changed",
    } },
    { name: "server error", confirm: true, status: 502, body: {
      error: "JTS could not confirm whether the new DAC was pinned.",
    } },
  ];
  for (const scenario of cases) {
    let posted = 0;
    const fetchHandler = baseFetch({
      "./output-topology": () => Promise.resolve(response(swappedDonglePayload())),
      "./output-topology/repin": () => {
        posted += 1;
        return Promise.resolve(response(scenario.body, false, scenario.status));
      },
    });
    const harness = setupHarness(fetchHandler);
    await loadAndSetActiveState(harness);
    globalThis.__jtsConfirm = async () => scenario.confirm;

    harness.dispatchClick({ "data-act": "repin-output-topology" });
    await harness.flush(); await harness.flush(); await harness.flush();

    if (scenario.confirm === false && posted !== 0) {
      fail("declining the confirm must not post a re-pin", { scenario: scenario.name });
    }
    const html = harness.elements.get("view-body").innerHTML;
    if (html.includes(">Pinning<")) {
      fail("the re-pin busy flag must clear on every exit path", {
        scenario: scenario.name, html,
      });
    }
    if (!html.includes("Keep setup, pin the new DAC")) {
      fail("the offer must remain clickable after a declined or failed re-pin", {
        scenario: scenario.name, html,
      });
    }
    if (scenario.name === "conflict") {
      const status = harness.elements.get("status").textContent;
      if (!status.includes("Review it and try again")) {
        fail("a 409 must surface the conflict rather than a generic failure", {
          status,
        });
      }
    }
  }
  return { repinDeclinedOrFailedClearsTheBusyFlag: true };
}

async function testResetPartialCleanupSurfacesWarning() {
  const posts = [];
  const fetchHandler = baseFetch({
    "./output-topology/reset": (path, options = {}) => {
      posts.push({ path, body: JSON.parse(options.body || "{}") });
      return Promise.resolve(response({
        output_topology: topologyPayload(),
        reset: {
          status: "needs_attention",
          message: "Speaker setup was reset and audio is off. JTS could not finish setup cleanup; open Status before continuing.",
        },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush();
  let confirmation = null;
  globalThis.__jtsConfirm = async (message, options) => {
    confirmation = { message, options };
    return true;
  };

  harness.dispatchClick({ "data-act": "reset-output-topology" });
  await harness.flush(); await harness.flush(); await harness.flush();

  if (posts.length !== 1 || posts[0].path !== "./output-topology/reset") {
    fail("reset button should post to the topology reset endpoint", { posts });
  }
  if (!confirmation ||
      !confirmation.message.includes("usable hardware is detected") ||
      confirmation.options.confirmLabel !== "Reset speaker setup") {
    fail("both reset controls should use one truthful hardware-agnostic dialog", {
      confirmation,
    });
  }
  const status = harness.elements.get("status").textContent;
  if (!status.includes("could not finish setup cleanup")) {
    fail("partial active-speaker cleanup should be visible to the operator", { status });
  }
  for (const leak of ["/var/lib", "PermissionError", "staged_config"]) {
    if (status.includes(leak)) {
      fail("partial cleanup warning should not leak backend path/error details", {
        leak,
        status,
      });
    }
  }
  return { resetPartialCleanupSurfacesWarning: true };
}

// #driver-research-reset-stale: the server deletes the design draft on
// reset, but a dirty in-memory driverResearch form used to survive the
// reset untouched (ingestDesignDraft refuses to overwrite a dirty form
// without {force: true}). The next save then shipped the old driver
// bands/level limits/high-pass right back to the server, re-creating the
// draft the reset just deleted. Dirty the form with an edit that never
// touches driver state (driver spacing) so a stale form -- not a fresh
// edit -- is what's under test.
async function testResetReloadsDesignDraftPastAStaleDirtyForm() {
  const savedDraft = {
    status: "ready_for_review",
    revision: 3,
    summary: { manual_driver_count: 1 },
    operator_inputs: {
      target_models: { "main:woofer": "Old Woofer" },
    },
    manual_settings: {
      drivers: [{
        target_id: "main:woofer",
        role: "woofer",
        model: "Old Woofer",
        hard_excitation_band_hz: [30, 5000],
        recommended_highpass_hz: 60,
      }],
      crossover_candidates: [],
    },
    driver_research_request: null,
    driver_research: null,
  };
  const emptyDraft = {
    status: "not_saved",
    revision: 0,
    manual_settings: null,
    operator_inputs: {},
    driver_research: null,
    driver_research_request: null,
    summary: {},
    issues: [],
  };
  let draftDeleted = false;
  const designPosts = [];
  const resetPosts = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method === "POST") {
        designPosts.push(JSON.parse(options.body || "{}"));
      }
      return Promise.resolve(response(draftDeleted ? emptyDraft : savedDraft));
    },
    "./output-topology/reset": (_path, options = {}) => {
      resetPosts.push(JSON.parse(options.body || "{}"));
      draftDeleted = true;
      return Promise.resolve(response({
        output_topology: activeTwoWayTopologyPayload(),
        topology_revision: "sha256:reset",
        reset: { status: "reset", message: "Speaker setup was reset." },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchInput({ "data-driver-spacing": "" }, "180");
  await harness.flush();

  globalThis.__jtsConfirm = async () => true;
  harness.dispatchClick({ "data-act": "reset-output-topology" });
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  if (resetPosts.length !== 1) fail("reset should post to the topology reset endpoint once", { resetPosts });

  harness.dispatchClick({ "data-act": "save-driver-design" });
  await harness.flush(); await harness.flush(); await harness.flush();

  if (designPosts.length !== 1) fail("save should post to the design draft once after reset", { designPosts });
  const saved = designPosts[0];
  const staleDrivers = (saved.manual_settings && saved.manual_settings.drivers) || [];
  const staleTargetModels = (saved.operator_inputs && saved.operator_inputs.target_models) || {};
  if (staleDrivers.length || Object.keys(staleTargetModels).length) {
    fail("a reset must clear the stale driver form before the next save ships it back", {
      saved,
    });
  }
  return { resetReloadsDesignDraftPastAStaleDirtyForm: true };
}

async function testFailedResetPreservesCommissioningPanels() {
  for (const failureStatus of [409, 502]) {
    const topology = activeTwoWayTopologyPayload();
    const commissionState = {
      commission_load: {
        status: "loaded",
        target: { speaker_group_id: "main", role: "woofer", audible_gain_db: -80 },
        rollback_available: true,
      },
      ramp: {
        confirmed_roles: [],
        pending: { role: "woofer", gain_db: -80, frequency_hz: 250 },
      },
      floor: { status: "floor_pending_operator", floor_audio_confirmed: false },
    };
    const fetchHandler = baseFetch({
      "./output-topology": () => Promise.resolve(response({
        output_topology: topology,
        topology_revision: "sha256:current",
      })),
      "./active-speaker/commission-state": () => Promise.resolve(response(commissionState)),
      "./active-speaker/commissioning-view": () => Promise.resolve(response(
        commissioningViewPayload({
          status: "needs_layout",
          current_step: "layout",
          stepStatuses: {
            layout: "active", research: "done",
            experiment: "todo", profile: "todo",
          },
        })
      )),
      "./output-topology/reset": () => Promise.resolve(response({
        error: "reset refused",
        output_topology: topology,
        topology_revision: "sha256:current",
      }, false, failureStatus)),
    });
    const harness = setupHarness(fetchHandler);
    await loadAndSetActiveState(harness);
    const before = harness.elements.get("view-body").innerHTML;
    if (!before.includes('data-output-step="experiment"')) {
      fail("fixture must start with the commissioning controls visible", {
        failureStatus, before,
      });
    }
    globalThis.__jtsConfirm = async () => true;
    harness.dispatchClick({ "data-act": "reset-output-topology" });
    await harness.flush(); await harness.flush(); await harness.flush();
    const after = harness.elements.get("view-body").innerHTML;
    if (!after.includes('data-output-step="experiment"')) {
      fail("a failed reset must preserve commissioning panels", {
        failureStatus, after,
      });
    }
  }
  return { failedResetPreservesCommissioningPanels: true };
}

async function testSavedTopologyReconcileFailureNeedsAttention() {
  const fetchHandler = baseFetch({
    "./output-topology": (path, options = {}) => {
      if ((options.method || "GET") === "POST") {
        return Promise.resolve(response({
          output_topology: topologyPayload(),
          topology_revision: "sha256:saved",
          save: {
            status: "needs_attention",
            message: "Speaker layout was saved, but audio remains off. Open Status before continuing.",
          },
          reconcile: { ok: false },
        }));
      }
      return Promise.resolve(response(topologyPayload()));
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush();

  harness.dispatchClick({ "data-act": "save-output-topology" });
  await harness.flush(); await harness.flush(); await harness.flush();

  const status = harness.elements.get("status").textContent;
  if (!status.includes("layout was saved, but audio remains off")) {
    fail("a failed post-save reconcile must not announce a clean save", { status });
  }
  return { savedTopologyReconcileFailureNeedsAttention: true };
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
  // The local driver/crossover/commissioning surface renders as primary content.
  for (const expected of ["Active crossover setup", "First speaker experiment"]) {
    if (!html.includes(expected)) {
      fail("follower /sound/ should render the local active-speaker UI", { expected, html });
    }
  }
  for (const forbidden of [
    "Create custom profile",
    "Try a stock profile",
    "data-act=\"new-draft\"",
    "Match loudness",
    "Volume floor",
    "Extra headroom",
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
  if (!initialHtml.includes("Safety profile: add the missing limits.")) {
    fail("Preview readiness must not imply a usable per-target declaration", { initialHtml });
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
  if (!previewHtml.includes("Safety profile: add the missing limits.") ||
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
        // A save lands the declaration current in the same step -- there is no
        // separate confirm any more.
        driver_safety_profile: { status: "confirmed", targets: [] },
        driver_safety_profile_evaluation: {
          status: "confirmed",
          confirmed_and_current: true,
        },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  const initialHtml = harness.elements.get("view-body").innerHTML;
  if (!initialHtml.includes("declared for the current outputs") ||
      !initialHtml.includes("old saved evidence")) {
    fail("A clean current draft must show its declaration and provenance", {
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
    "save your current edits to update it",
  ]) {
    if (!conflictHtml.includes(expected)) {
      fail("Conflict UI must retain and truthfully label unsaved safety edits", {
        expected,
        conflictHtml,
      });
    }
  }
  if (conflictHtml.includes("Fresh T2") || conflictHtml.includes("old saved evidence") ||
      conflictHtml.includes("declared for the current outputs")) {
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

// One cabinet, one driver per dongle: woofer on child A, tweeter on child B.
function crossChildTopology(warnings = []) {
  const topology = activeStereoTwoWayTopologyPayload();
  topology.speaker_groups = [topology.speaker_groups[0]];
  topology.speaker_groups[0].channels[1].physical_output_index = 2;
  topology.routing = {
    mono_group_id: null,
    main_left_group_id: "left",
    main_right_group_id: null,
    subwoofer_group_ids: [],
  };
  topology.hardware.child_devices = [
    { child_id: "left_dac", physical_output_indexes: [0, 1] },
    { child_id: "right_dac", physical_output_indexes: [2, 3] },
  ];
  topology.evaluation = { status: "valid", warnings };
  return topology;
}

function crossChildVerdict(message, groupLabel = "Left cabinet") {
  return [{
    severity: "warning",
    code: "speaker_group_spans_child_devices",
    message,
    group_id: "left",
    group_label: groupLabel,
    child_ids: ["left_dac", "right_dac"],
  }];
}

async function crossChildMapStepHtml(topology) {
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(topology)),
  }));
  await loadAndSetActiveState(harness);
  return outputStepBodyHtml(harness.elements.get("view-body").innerHTML, "layout");
}

// A speaker whose drivers land on two child DACs of a composite output device.
// output_topology.cross_child_group_verdicts names this as a WARNING and the
// save is accepted, so the ONLY thing that tells the household is this notice —
// if it stops rendering, the disclosure silently disappears while every gate
// still reports green. Both halves matter: it must appear when the backend
// sends the verdict, and it must stay away when it does not.
async function testCrossChildSpeakerGroupIsDisclosedInTheMapStep() {
  const disclosed = await crossChildMapStepHtml(crossChildTopology(
    crossChildVerdict(
      "Left cabinet is split across DACs left_dac, right_dac; keep every " +
      "driver of one speaker on one DAC so its crossover does not straddle " +
      "two uncorrected clocks"
    )
  ));
  if (!disclosed || !disclosed.includes("One speaker is split across two DACs")) {
    fail("cross-child verdict should be disclosed in the Speaker layout step",
      { disclosed });
  }
  if (!disclosed.includes("Left cabinet is split across DACs left_dac")) {
    fail("cross-child notice should name the group and the child DACs",
      { disclosed });
  }
  // Disclose, never block: the save control stays live.
  if (disclosed.includes("status-pill--blocked")) {
    fail("cross-child disclosure must not present as a blocked layout",
      { disclosed });
  }

  const quiet = await crossChildMapStepHtml(crossChildTopology());
  if (!quiet) fail("control render should still produce a map step", { quiet });
  if (quiet.includes("One speaker is split across two DACs")) {
    fail("cross-child notice must not render without the backend verdict",
      { quiet });
  }
  return { crossChildSpeakerGroupIsDisclosedInTheMapStep: true };
}

// renderIssueList is the shared innerHTML sink for every verdict message the
// backend sends, and the cross-child notice above quotes a household-TYPED
// speaker-group name inside that message. Free text reaching innerHTML: the
// list item must carry entities, never live markup, or renaming a speaker
// group turns into stored XSS on the Confirm-outputs step.
async function testIssueListEscapesUntrustedVerdictMessages() {
  const hostile = '</li></ul><script>alert("1" & \'x\')</script>';
  const escaped = "&lt;/li&gt;&lt;/ul&gt;&lt;script&gt;" +
    "alert(&quot;1&quot; &amp; &#39;x&#39;)&lt;/script&gt;";
  const disclosed = await crossChildMapStepHtml(
    crossChildTopology(crossChildVerdict(hostile, hostile))
  );
  if (!disclosed) {
    fail("a hostile verdict must still render the map step", { disclosed });
  }
  // One exact string, so dropping escapeHtml from the message slot fails here
  // even if the surrounding <li> is rebuilt.
  const item = '<li class="active-speaker-issue active-speaker-issue--warning">' +
    escaped + "</li>";
  if (!disclosed.includes(item)) {
    fail("an untrusted verdict message must render as an escaped list item",
      { disclosed });
  }
  if (disclosed.includes(hostile) || disclosed.includes("<script>")) {
    fail("an untrusted verdict message must never reach innerHTML as markup",
      { disclosed });
  }
  return { issueListEscapesUntrustedVerdictMessages: true };
}

async function testNextActionOwnsTheSpeakerPage() {
  const programs = ['speaker', 'room', 'bass'].map(id => ({id, title: id, description: id}));
  for (const [status, action, step] of [
    ['needs_layout', {id: 'declare_speaker'}, 'layout'],
    ['needs_driver_values', {id: 'save_driver_values'}, 'research'],
    ['needs_driver_values', {id: 'preview_crossover'}, 'research'],
    ['ready_to_save_profile', {id: 'apply_candidate'}, 'profile'],
    ['applied', {id: 'copy_prompt', program: 'room'}, ''],
    ['applied', {id: 'run_program', program: 'speaker'}, 'experiment'],
    ['needs_first_experiment', {id: 'run_speaker_program'}, 'experiment'],
    ['blocked', {id: 'apply_candidate', enabled: false, reason: '<script>blocked</script>'}, 'profile'],
  ]) {
    const mints = [];
    const harness = setupHarness(baseFetch({
      './output-topology': () => Promise.resolve(response(activeTwoWayTopologyPayload())),
      './active-speaker/baseline-profile': () => Promise.resolve(response({tuning_programs: programs})),
      './active-speaker/commissioning-view': () => Promise.resolve(response(commissioningViewPayload({
        status, next_action: {label: action.id, enabled: true, ...action},
        applied_profile: {exists: status === 'applied', stands: status === 'applied'},
      }))),
      './active-speaker/tuning-handoff': path => {
        mints.push(path);
        return Promise.resolve(response({status: 'ready', prompt: 'packet-room', binding: {design_draft_revision: 1}}));
      },
    }));
    await loadAndSetActiveState(harness);
    const html = harness.elements.get('view-body').innerHTML;
    const firstControl = html.match(/<(button|input|select|textarea|summary|a)\b[^>]*>/)[0];
    assert.ok(firstControl.includes('data-next-action="' + action.id + '"'));
    assert.equal(/\sdisabled(?=[\s>])/.test(firstControl), action.enabled === false);
    assert.deepEqual([...html.matchAll(/data-output-step="([^" ]+)" open/g)].map(m => m[1]), step ? [step] : []);
    const experiment = outputStepBodyHtml(html, 'experiment');
    assert.equal((experiment.match(/id="seat-level-card"/g) || []).length, 1);
    assert.equal((html.match(/id="seat-level-card"/g) || []).length, 1);
    if (action.reason) {
      assert.ok(html.includes('&lt;script&gt;blocked&lt;/script&gt;'));
      assert.ok(!html.includes('<script>'));
    }
    const buttonAttrs = Object.fromEntries([...firstControl.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
    if (action.id === 'declare_speaker') {
      harness.dispatchToggle({'data-output-step': 'profile', open: true});
      harness.dispatchClick(buttonAttrs);
      assert.deepEqual(harness.elements.get('view-body').scrolledTo, {step: 'layout', block: 'start'});
      assert.ok(harness.elements.get('view-body').innerHTML.includes('data-output-step="layout" open'));
    }
    if (action.id === 'copy_prompt') {
      const disclosure = html.match(/<details[^>]* data-other-prompts>[\s\S]*?<\/details>/)[0];
      assert.equal((disclosure.match(/data-act="copy-tuning-handoff"/g) || []).length, 2);
      const visible = html.replace(disclosure, '');
      assert.equal((visible.match(/data-act="copy-tuning-handoff"/g) || []).length, 1);
      assert.equal(buttonAttrs['data-program'], 'room');
      harness.dispatchClick(buttonAttrs);
      await harness.flush();
      await harness.flush();
      assert.deepEqual(mints, ['./active-speaker/tuning-handoff?program=room']);
      assert.deepEqual(harness.clipboard, ['packet-room']);
    }
    if (action.id.startsWith('run_')) {
      const command = html.match(/<textarea id="tuning-run-command"[^>]*>([^<]*)<\/textarea>/)[1];
      assert.equal(command, 'sudo /opt/jasper/.venv/bin/jasper-round run --program speaker');
      document.getElementById('tuning-run-command').value = command;
      harness.dispatchClick(buttonAttrs);
      await harness.flush();
      assert.deepEqual(harness.clipboard, [command]);
    }
  }
  return {nextActionOwnsTheSpeakerPage: true};
}

async function testSeatLevelSurvivesStepRenders() {
  const requests = [];
  const pendingStart = deferred();
  let payload = {state: 'idle', default_target_db_spl: 68, mic: {available: true}};
  const harness = setupHarness(baseFetch({
    './active-speaker/seat-level/status': () => Promise.resolve(response(payload)),
    './active-speaker/seat-level/start': (_path, options) => {
      requests.push(JSON.parse(options.body));
      payload = {...payload, state: 'running', target_db_spl: requests.at(-1).target_db_spl};
      return pendingStart.promise.then(() => response({status: 'started'}));
    },
    './active-speaker/seat-level/stop': () => {
      requests.push({stop: true});
      payload = {...payload, state: 'idle'};
      return Promise.resolve(response({status: 'stopped'}));
    },
  }));
  await loadAndSetActiveState(harness);
  const target = harness.elements.get('seat-level-target');
  assert.equal(Number(target.value), 68);
  target.value = '71.5';
  harness.elements.get('seat-level-start').click();
  await harness.flush();
  assert.deepEqual(requests, [{target_db_spl: 71.5}]);
  harness.dispatchClick({'data-act': 'open-output-layout'});
  assert.equal(harness.elements.get('seat-level-start').disabled, true);
  assert.equal(harness.elements.get('seat-level-target'), target);
  pendingStart.resolve();
  await harness.flush();
  harness.dispatchClick({'data-act': 'open-output-layout'});
  assert.equal(harness.elements.get('seat-level-target').value, '71.5');
  assert.equal(harness.elements.get('seat-level-target').disabled, true);
  assert.equal(harness.elements.get('seat-level-start').hidden, true);
  assert.equal(harness.elements.get('seat-level-stop').hidden, false);
  harness.elements.get('seat-level-stop').click();
  await harness.flush();
  assert.equal(harness.elements.get('seat-level-target').disabled, false);
  assert.equal(harness.elements.get('seat-level-start').hidden, false);
  assert.equal(harness.elements.get('seat-level-stop').hidden, true);
  assert.deepEqual(requests, [{target_db_spl: 71.5}, {stop: true}]);
  return {seatLevelSurvivesStepRenders: true};
}

// ADR-0318 / #5161: the panel is authored data (Seed / Validate / Bank), not
// a form, and it only makes sense once the saved topology actually declares a
// rear output — a primary-only speaker has nothing to calibrate.
async function testCardioidRearCalibrationPanelGatedOnSavedRearOutput() {
  const primaryOnly = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
  }));
  await loadAndSetActiveState(primaryOnly);
  const primaryHtml = primaryOnly.elements.get("view-body").innerHTML;
  if (primaryHtml.includes("data-rear-calibration")) {
    fail("a primary-only topology must not render the rear-calibration panel", { primaryHtml });
  }

  const cardioid = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(cardioidTwoWayTopologyPayload())),
  }));
  await loadAndSetActiveState(cardioid);
  const cardioidHtml = cardioid.elements.get("view-body").innerHTML;
  if (!cardioidHtml.includes("data-rear-calibration") ||
      !cardioidHtml.includes("data-rear-calibration-text")) {
    fail("a saved rear output must render the rear-calibration document panel", { cardioidHtml });
  }
  return { cardioidRearCalibrationPanelGatedOnSavedRearOutput: true };
}

// #2883: the handoff card appears only once a baseline is playing, mints its
// prompt server-side on the copy, and the copy goes STALE — visibly — when the
// declarations move past the revision it was minted against.
async function testTuningHandoffCardMintsAndGoesStale() {
  const programs = ["speaker", "room", "bass"].map(id => ({id, title: id, description: id}));
  async function run(pageRevision, mintRevision, programId = "speaker") {
    const mints = [];
    const harness = setupHarness(baseFetch({
      "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
      "./active-speaker/design-draft": () => Promise.resolve(response({
        status: "ready_for_review", revision: pageRevision, summary: {}, operator_inputs: {},
      })),
      "./active-speaker/baseline-profile": () => Promise.resolve(response({
        status: "ready_to_compile", tuning_programs: programs,
        permissions: { may_compile: false, may_apply: false },
        config: { basename: "active_speaker_baseline.yml" },
        issues: [],
      })),
      "./active-speaker/commissioning-view": () => Promise.resolve(response(commissioningViewPayload({
        status: "applied", applied_profile: {exists: true, stands: true, candidate_fingerprint: "applied-fp",
          applied_at: "2026-09-13T12:00:00Z", config_path: "/var/lib/camilladsp/applied.yml"},
      }))),
      "./active-speaker/tuning-handoff": (path) => {
        mints.push(path);
        const selectedId = new URL(path, "http://jts.local").searchParams.get("program");
        return Promise.resolve(response({
          kind: "jts_tuning_handoff",
          status: "ready",
          reason: null,
          binding: { hostname: "jts3.local", design_draft_revision: mintRevision },
          programs,
          program: selectedId,
          prompt: "MINTED HANDOFF PROMPT " + selectedId,
        }));
      },
    }));
    await loadAndSetActiveState(harness);
    harness.dispatchClick({ "data-act": "copy-tuning-handoff", "data-program": programId });
    await harness.flush();
    await harness.flush();
    return { mints, harness };
  }

  for (const program of programs) {
    const copied = await run(3, 3, program.id);
    const html = copied.harness.elements.get("view-body").innerHTML;
    if (!html.includes("MINTED HANDOFF PROMPT " + program.id)) fail("copy selected the wrong program", {html});
    if ((html.match(/data-act="copy-tuning-handoff"/g) || []).length !== 3) fail("expected three program actions", {html});
  }
  const fresh = await run(3, 3);
  if (fresh.mints.length !== 1) {
    fail("the copy must mint the prompt from the box, once", { mints: fresh.mints });
  }
  const freshHtml = fresh.harness.elements.get("view-body").innerHTML;
  if (!freshHtml.includes("MINTED HANDOFF PROMPT")) {
    fail("the card must show the minted prompt, not a client-built one", { freshHtml });
  }
  if (freshHtml.includes("data-tuning-handoff-stale")) {
    fail("a copy minted against the live revision is not stale", { freshHtml });
  }

  // The mint re-reads the draft, so it can be AHEAD of the page's cached copy.
  // That is a lagging cache, not a declaration the operator changed.
  const behind = await run(2, 3);
  const behindHtml = behind.harness.elements.get("view-body").innerHTML;
  if (behindHtml.includes("data-tuning-handoff-stale")) {
    fail("a page cache behind the mint must not read as a stale copy", { behindHtml });
  }

  const drifted = await run(4, 3);
  const driftedHtml = drifted.harness.elements.get("view-body").innerHTML;
  if (!driftedHtml.includes("data-tuning-handoff-stale")) {
    fail("a declaration edit after the copy must be disclosed as stale", { driftedHtml });
  }

  const noBaseline = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/tuning-handoff": () => fail("the card must not mint before a baseline plays"),
  }));
  await loadAndSetActiveState(noBaseline);
  if (noBaseline.elements.get("view-body").innerHTML.includes('data-act="copy-tuning-handoff"')) {
    fail("the handoff card must not render before the speaker plays a baseline");
  }
  return { tuningHandoffCardMintsAndGoesStale: true };
}


async function testDriverSpacingUsesTheExistingDraftSaveAndReload() {
  const posts = [];
  let draft = {status: 'ready_for_review', revision: 1, summary: {},
    manual_settings: {drivers: [{role: 'woofer', target_id: 'main:woofer', model: 'Test woofer'}]}};
  const fetchHandler = baseFetch({
    './output-topology': () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    './active-speaker/design-draft': (_path, options = {}) => {
      if (options.method === 'POST') {
        const body = JSON.parse(options.body);
        posts.push(body);
        draft = {...draft, ...body, revision: draft.revision + 1};
      }
      return Promise.resolve(response(draft));
    },
  });
  let harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);
  for (const value of ['', '200', '']) {
    const inputs = harness.elements.get('view-body').innerHTML.match(/<input[^>]*data-driver-spacing[^>]*>/g) || [];
    if (inputs.length !== 1 || /\srequired(?:\s|>)/.test(inputs[0])) {
      fail('Driver spacing must have one optional input', {inputs});
    }
    harness.dispatchInput({'data-driver-spacing': ''}, value);
    harness.dispatchClick({'data-act': 'save-driver-design'});
    for (let i = 0; i < 5; i++) await harness.flush();
    if (posts.at(-1)?.manual_settings?.driver_spacing_mm !== (value ? Number(value) : null)) {
      fail('Driver spacing must save or clear in manual settings', {posts});
    }
    harness = setupHarness(fetchHandler);
    await loadAndSetActiveState(harness);
    const input = harness.elements.get('view-body').innerHTML.match(/<input[^>]*data-driver-spacing[^>]*>/)?.[0];
    if (!input?.includes('value="' + value + '"')) {
      fail('Driver spacing must reload from the saved draft', {input, value});
    }
  }
  return {driverSpacingUsesTheExistingDraftSaveAndReload: true};
}


async function testInstallationUsesTheExistingDraftSaveAndReload() {
  const posts = [];
  const installation = {fields: {
    amplifier_model: {label: 'Amplifier model', type: 'text'},
    supply_voltage_v: {label: 'Power supply (V)', type: 'number'},
    passive_radiator_added_mass_g: {label: 'Added weight (g)', type: 'number', allow_zero: true, enclosure: 'passive_radiator'},
  }, drivers: []};
  const harness = setupHarness(baseFetch({
    './active-speaker/design-draft': (_path, options = {}) => {
      const body = options.method === 'POST' ? JSON.parse(options.body) : {};
      if (options.method === 'POST') posts.push(body);
      return Promise.resolve(response({status: 'ready_for_review', revision: 1, summary: {},
        operator_inputs: body.operator_inputs || {}, manual_settings: body.manual_settings || {driver_spacing_mm: 150},
        installation}));
    },
  }));
  await loadAndSetActiveState(harness);
  harness.dispatchInput({'data-driver-target': 'main:full_range'}, 'Example FR8');
  harness.dispatchChange({'data-manual-driver': 'main:full_range', 'data-manual-field': 'enclosure_kind'}, 'passive_radiator');
  for (const [key, value] of Object.entries({amplifier_model: '<TPA3255>', supply_voltage_v: '36', passive_radiator_added_mass_g: '0'})) {
    harness.dispatchInput({'data-manual-driver': 'main:full_range', 'data-manual-field': 'installation_' + key}, value);
  }
  harness.dispatchClick({'data-act': 'save-driver-design'});
  for (let i = 0; i < 5; i++) await harness.flush();
  const saved = posts[0]?.manual_settings?.drivers?.[0]?.installation;
  if (posts[0]?.manual_settings?.driver_spacing_mm !== 150) {
    fail('Saving installation inputs must preserve declared driver spacing', {posts});
  }
  if (JSON.stringify(saved) !== JSON.stringify({amplifier_model: '<TPA3255>', supply_voltage_v: 36, passive_radiator_added_mass_g: 0})) {
    fail('Optional installation inputs must save through the draft, including explicit zero mass', {posts});
  }
  const html = harness.elements.get('view-body').innerHTML;
  if (!html.includes('value="&lt;TPA3255&gt;"') || !html.includes('data-manual-field="installation_supply_voltage_v" value="36"')) {
    fail('Saved installation inputs must reload and escape text', {html});
  }
  return {installationUsesTheExistingDraftSaveAndReload: true};
}

const results = [];
// Dead-end: a layout is drafted but no spare physical output exists for a LOCAL
// subwoofer (the single-output Apple-dongle case). The card must keep the
// disabled "Add subwoofer" affordance — a "sub" must never be a silent dead-end.
async function testSubwooferDeadEndKeepsAddAffordance() {
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
  if (!html.includes('data-act="toggle-output-subwoofer"')) {
    fail("the local-subwoofer add affordance must remain in the dead-end branch", { html });
  }
  return { subwooferDeadEndKeepsAddAffordance: true };
}

// Base case: a spare output exists for a LOCAL subwoofer, so the card offers
// the normal add affordance.
async function testSubwooferWithSpareOutputRendersAddOn() {
  const harness = setupHarness(baseFetch());
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Subwoofer add-on")) {
    fail("default layout should render the subwoofer add-on card", { html });
  }
  return { subwooferWithSpareOutputRendersAddOn: true };
}

// Issue #1820 defect 3, as it stands after the confirm step was retired.
// Saving the declaration IS declaring it, so an ordinary edit can no longer
// leave the profile unusable — but 'incomplete', 'stale', and 'malformed' still
// refuse EVERY crossover measurement, and each needs a different edit before a
// save can succeed. These render the real card and assert the review callout is
// hoisted ahead of the Advanced disclosure in exactly those states, names the
// right remedy, and is gone once the declaration is usable.
function designDraftWithSafety(evaluation, issues = []) {
  return {
    status: "ready_for_review",
    revision: 3,
    summary: {},
    operator_inputs: {},
    driver_safety_profile: {
      status: evaluation.status === "confirmed" ? "confirmed" : "incomplete",
      issues,
    },
    driver_safety_profile_evaluation: evaluation,
    permissions: {},
  };
}

async function harnessWithSafetyEvaluation(evaluation, options = {}, issues = []) {
  const harness = setupHarness(baseFetch({
    "./output-topology": () => Promise.resolve(response(activeTwoWayTopologyPayload())),
    "./active-speaker/design-draft": () => Promise.resolve(
      response(designDraftWithSafety(evaluation, issues))
    ),
  }), options);
  await loadAndSetActiveState(harness);
  return harness;
}

// The nanny loop, pinned shut on the browser side: a declaration whose values
// are usable renders NO callout and NO confirm control, whatever the operator
// last edited. Before the ruling this state existed and blocked every
// measurement behind a button.
async function testUsableSafetyProfileRendersNoCalloutAndNoConfirmControl() {
  const harness = await harnessWithSafetyEvaluation({
    status: "confirmed",
    confirmed_and_current: true,
    reasons: [],
  });
  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes('id="confirm-safety-limits"')) {
    fail("a usable profile must not nag with the hoisted callout", { html });
  }
  if (html.includes('data-act="confirm-driver-safety"')) {
    fail("the confirm control was retired and must not render anywhere", { html });
  }
  return { usableSafetyProfileRendersNoCalloutAndNoConfirmControl: true };
}

// #2874. An implausible low limit the household TYPED saves, and the page has
// to say so — the review callout above stays quiet on a confirmed profile,
// which is exactly the state a warning describes. The copy is server-phrased
// because it names the household's own numbers, so this proves it is rendered
// and escaped rather than dropped or trusted as markup.
async function testATypedDeclarationWarningIsShownOnAConfirmedProfile() {
  const harness = await harnessWithSafetyEvaluation(
    { status: "confirmed", confirmed_and_current: true, reasons: [] },
    {},
    [{
      severity: "warning",
      code: "tweeter:low_limit_implausible_for_style",
      message: "tweeter: declared 700 Hz is more than 4x below the "
        + "compression_driver class band of 500-8000 Hz <img src=x onerror=1>",
    }],
  );
  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes('id="confirm-safety-limits"')) {
    fail("a warning is not a refusal and must not hoist the review callout", {
      html,
    });
  }
  if (!html.includes("JTS is trusting your declaration")) {
    fail("a saved declaration JTS is warning about must say so", { html });
  }
  if (!html.includes("declared 700 Hz is more than 4x below")) {
    fail("the server-phrased warning text must reach the page", { html });
  }
  if (html.includes("<img src=x onerror=1>")) {
    fail("warning text is server data and must be escaped, never markup", {
      html,
    });
  }
  if (!html.includes("&lt;img src=x onerror=1&gt;")) {
    fail("the escaped form of the warning text is missing", { html });
  }
  return { typedDeclarationWarningIsShownOnAConfirmedProfile: true };
}

// ...and a profile with no warnings renders no such block at all.
async function testNoWarningsRenderNoTrustBlock() {
  const harness = await harnessWithSafetyEvaluation({
    status: "confirmed",
    confirmed_and_current: true,
    reasons: [],
  });
  const html = harness.elements.get("view-body").innerHTML;
  if (html.includes("JTS is trusting your declaration")) {
    fail("an unwarned declaration must not grow a warning block", { html });
  }
  return { noWarningsRenderNoTrustBlock: true };
}

async function testIncompleteSafetyProfileHoistsTheReviewCallout() {
  const harness = await harnessWithSafetyEvaluation({
    status: "incomplete",
    confirmed_and_current: false,
    reasons: ["driver_safety_missing_values"],
  });
  const html = harness.elements.get("view-body").innerHTML;
  const calloutAt = html.indexOf('id="confirm-safety-limits"');
  const advancedAt = html.indexOf("data-driver-advanced");
  if (calloutAt < 0) {
    fail("an incomplete profile still needs the explanation", { html });
  }
  if (!(calloutAt < advancedAt)) {
    fail("the review callout must render before the Advanced disclosure", {
      calloutAt, advancedAt, html,
    });
  }
  const callout = html.slice(calloutAt, advancedAt);
  if (!callout.includes("Some safety limits are still missing")) {
    fail("an incomplete profile must name the add-the-values action", { callout });
  }
  if (callout.includes('data-act="confirm-driver-safety"')) {
    fail("the callout must explain, never offer a retired confirm action", {
      callout,
    });
  }
  return { incompleteSafetyProfileHoistsTheReviewCallout: true };
}

// #2603. A profile written before a driver's low limit had one declared owner
// evaluates 'malformed', NOT 'incomplete'. jts3's own stored artifact is this
// shape, and the copy has to name both the cause and the ONE fix a save still
// needs first — otherwise the operator saves, nothing changes, and the loop
// stays shut with no explanation.
async function testStaleLowLimitWithABlockerNamesTheCauseAndTheFix() {
  const harness = await harnessWithSafetyEvaluation({
    status: "malformed",
    confirmed_and_current: false,
    reasons: [
      "driver_safety_profile_low_limit_stale",
      "tweeter:measurement_band_outside_hard_band",
    ],
  });
  const html = harness.elements.get("view-body").innerHTML;
  const calloutAt = html.indexOf('id="confirm-safety-limits"');
  if (calloutAt < 0) {
    fail("a stale-low-limit profile still needs the explanation", { html });
  }
  const callout = html.slice(calloutAt, html.indexOf("data-driver-advanced"));
  if (!callout.includes("one declared minimum crossover per driver")) {
    fail("the copy must name WHY the declaration went unusable", { callout });
  }
  if (!callout.includes(
    "the tweeter&#39;s measurement band reaches outside its hard excitation band"
  )) {
    fail("the copy must name the blocker a save has to clear first", { callout });
  }
  if (!callout.includes("the datasheet")) {
    fail("the copy must name the remedy, not just the conflict", { callout });
  }
  if (html.includes('data-act="confirm-driver-safety"')) {
    fail("the confirm control was retired and must not render anywhere", { html });
  }
  return { staleLowLimitWithABlockerNamesTheCauseAndTheFix: true };
}

// The control. Without it the copy above reads as "malformed is always
// blocked", which would strand every box in the compat class instead of
// telling it that one ordinary save is the whole remedy.
async function testStaleLowLimitWithoutABlockerNamesTheSaveAsTheRemedy() {
  const harness = await harnessWithSafetyEvaluation({
    status: "malformed",
    confirmed_and_current: false,
    reasons: ["driver_safety_profile_low_limit_stale"],
  });
  const html = harness.elements.get("view-body").innerHTML;
  const calloutAt = html.indexOf('id="confirm-safety-limits"');
  const callout = html.slice(calloutAt, html.indexOf("data-driver-advanced"));
  if (!callout.includes("one declared minimum crossover per driver")) {
    fail("the copy must still name why the declaration went unusable", { callout });
  }
  if (!callout.includes("save them again")) {
    fail("a profile that rebuilds cleanly must name the save as the remedy", {
      callout,
    });
  }
  return { staleLowLimitWithoutABlockerNamesTheSaveAsTheRemedy: true };
}

// #2870. A profile written before a field was RETIRED is not corrupt, and the
// generic malformed copy ("JTS could not read these limits") reads as damage
// and names no remedy. Every box confirmed before that ruling lands here, so
// the copy is the whole migration story the household ever sees — pinned on the
// RENDERED DOM, mirroring the #2603 pair above, because a reason the server
// names and the page cannot phrase buys nothing.
async function testRetiredFieldNamesTheSaveAsTheRemedyAndNotCorruption() {
  const harness = await harnessWithSafetyEvaluation({
    status: "malformed",
    confirmed_and_current: false,
    reasons: ["driver_safety_profile_retired_field"],
  });
  const html = harness.elements.get("view-body").innerHTML;
  const calloutAt = html.indexOf('id="confirm-safety-limits"');
  if (calloutAt < 0) {
    fail("a retired-field profile still needs the explanation", { html });
  }
  const callout = html.slice(calloutAt, html.indexOf("data-driver-advanced"));
  if (!callout.includes("no longer uses")) {
    fail("the copy must name WHY the declaration stopped reading", { callout });
  }
  if (!callout.includes("save them again")) {
    fail("the copy must name the save as the remedy", { callout });
  }
  // The load-bearing half: it must NOT fall through to the generic unreadable
  // sentence, which is what the reason exists to replace.
  if (callout.includes("could not read these limits")) {
    fail("a retired field must not be reported as unreadable", { callout });
  }
  return { retiredFieldNamesTheSaveAsTheRemedyAndNotCorruption: true };
}

async function testIncompleteFromABandRelationshipNamesTheRelationship() {
  // Issue #2191. 'incomplete' is also reached with every value present — the
  // owner's tweeter repair hit exactly this — and "add the missing limits"
  // then sends the operator hunting for a blank field that does not exist.
  const harness = await harnessWithSafetyEvaluation({
    status: "incomplete",
    confirmed_and_current: false,
    reasons: ["tweeter:measurement_band_outside_hard_band"],
  });
  const html = harness.elements.get("view-body").innerHTML;
  const calloutAt = html.indexOf('id="confirm-safety-limits"');
  if (calloutAt < 0) {
    fail("an incomplete profile still needs the explanation", { html });
  }
  const callout = html.slice(calloutAt, html.indexOf("data-driver-advanced"));
  if (callout.includes("still missing")) {
    fail("nothing is missing here — the copy must not say it is", { callout });
  }
  if (!callout.includes("Nothing is missing")) {
    fail("the copy must contradict the missing-values reading", { callout });
  }
  if (!callout.includes(
    "the tweeter&#39;s measurement band reaches outside its hard excitation band"
  )) {
    fail("the copy must name which relationship does not line up", { callout });
  }
  // The saved-summary line is the second place that read 'missing'.
  if (!html.includes(
    "Safety profile: resolve the limits that do not line up"
  )) {
    fail("the saved summary must not send the operator after a blank field", {
      html,
    });
  }

  // Both causes at once names both actions rather than picking one.
  const mixed = await harnessWithSafetyEvaluation({
    status: "incomplete",
    confirmed_and_current: false,
    reasons: [
      "tweeter:measurement_band_outside_hard_band",
      "woofer:max_sweep_duration_s_missing",
    ],
  });
  const mixedHtml = mixed.elements.get("view-body").innerHTML;
  const mixedCallout = mixedHtml.slice(
    mixedHtml.indexOf('id="confirm-safety-limits"'),
    mixedHtml.indexOf("data-driver-advanced"),
  );
  if (!mixedCallout.includes("still missing, and some do not line up")) {
    fail("a mixed incomplete state must name both causes", { mixedCallout });
  }
  return { incompleteFromABandRelationshipNamesTheRelationship: true };
}

async function testSafetyLimitsDeepLinkOpensTheComponentStep() {
  const unusable = {
    status: "stale",
    confirmed_and_current: false,
    reasons: ["driver_safety_profile_target_mismatch"],
  };
  const harness = await harnessWithSafetyEvaluation(
    unusable, { hash: "#confirm-safety-limits" },
  );
  await harness.flush();
  const html = harness.elements.get("view-body").innerHTML;
  // The component step's <details> is OPEN, so the deep-linked explanation is
  // actually on screen rather than behind a collapsed summary — the whole
  // point of not relying on bare fragment behaviour.
  const stepAt = html.indexOf('data-output-step="research"');
  if (stepAt < 0) fail("the component step must render", { html });
  if (!html.slice(stepAt, stepAt + 60).includes(" open>")) {
    fail("the deep link must open the component step", {
      step: html.slice(stepAt, stepAt + 200),
    });
  }
  if (html.indexOf('id="confirm-safety-limits"') < 0) {
    fail("the deep-linked callout must be rendered", { html });
  }

  // And a page opened at the same fragment with nothing to review must NOT be
  // yanked into the component step by a stale bookmark.
  const usable = await harnessWithSafetyEvaluation(
    { status: "confirmed", confirmed_and_current: true, reasons: [] },
    { hash: "#confirm-safety-limits" },
  );
  await usable.flush();
  const usableHtml = usable.elements.get("view-body").innerHTML;
  const usableStepAt = usableHtml.indexOf('data-output-step="research"');
  if (usableStepAt >= 0 &&
      usableHtml.slice(usableStepAt, usableStepAt + 60).includes(" open>")) {
    fail("a stale fragment must not open the component step", { usableHtml });
  }
  return { safetyLimitsDeepLinkOpensTheComponentStep: true };
}

const liveTabResult = await testLiveTabReplay();
results.push(liveTabResult);
results.push(await testEqSliderDragSendsNoLiveAudioUntilRelease());
results.push(await testCancellingTheNameBoxClosesIt());
results.push(await testVolumeFloorRequiresExplicitSaveButAuditionsDraft());
results.push(await testBlockedSettingsSaveRendersOnTheCard());
results.push(await testBlockedEqCarrierIsThePageState());
results.push(await testLeavingAnUnsavedDraftRestoresThePersistedProfile());
results.push(await testSplitPageModesRenderAndBootOnlyOwnedSurfaces());
results.push(await testPassiveLayoutsDoNotExposeDirectDriverTestFlow());
results.push(await testSublessPassiveLayoutRendersATerminatedLadder());
results.push(await testActiveCrossoverFirstStepRender());
results.push(await testComponentFirstResearchFlowIsOrderedAndAdvancedIsFlat());
results.push(await testOneDriverComponentCanPrepareResearchPrompt());
results.push(await testPassiveMainWithSubUsesResearchableMainTargetOnly());
results.push(await testPartialSavePreservesUnchosenEnclosure());
results.push(await testDirectCrossoverEditRefreshesProposalAndFooter());
results.push(await testTweeterTypeChangeInvalidatesCopiedResearchBinding());
results.push(await testThreeWayRendersEveryPhysicalComponentChoice());
results.push(await testActiveRouteLimitsRenderedTemplates());
results.push(await testSpeakerLayoutMatrixAndRearAssignment());
results.push(await testTwoOutputChannelSelectorAutoAssignsPeerOnSave());
results.push(await testTweeterDriverStyleSelectorSetsTopologyAndAppearsInReview());
results.push(await testUnknownDriverStyleRendersWithoutGuessedFloor());
results.push(await testDesignDraftSaveRefusalShowsServerErrorNotSavedToast());
results.push(await testChannelSelectorKeepsLayoutOpenWhenDraftDirty());
results.push(await testThreeOutputChannelSelectorDoesNotAutoAssignPeers());
results.push(await testCompiledProfileApplyBlockStaysUnderstandable());
results.push(await testAppliedProfileCardUsesCommissioningRecord());
results.push(await testLegacyStereoDraftCanPreparePreviewWithoutTargetCopy());
results.push(await testStereoDriverValuesStayTargetSpecific());
results.push(await testDesignConflictRefreshesWithoutBlindRetryAndBooleanNumbersDrop());
results.push(await testDesignConflictPreservesUnsavedSafetyEdits());
results.push(await testVisibleCrossoverSettingsWinOverImportedJson());
results.push(await testManualCrossoverPayloadOmitsPolarityAndDelayWhenDefault());
results.push(await testManualCrossoverPayloadEmitsPolarityAndZeroDelay());
results.push(await testManualCrossoverDelayWithoutTargetBlocksSaveClientSide());
results.push(await testCrossoverPickersOfferOnlyTheServedVocabulary());
results.push(await testStoredUnsupportedCrossoverSlopeBlocksSaveClientSide());
results.push(await testAPassiveLayoutSavesWithNoCrossoverVocabularyServed());
results.push(await testManualCrossoverAlignmentIsAlwaysVisibleOnSavedDelay());
results.push(await testDriverResearchImportCopiesPolarityAndDelayIntoManualSettings());
results.push(await testDriverResearchImportToleratesFencesAndProse());
results.push(await testDriverResearchImportPreservesOperatorInstalledConfiguration());
results.push(await testCrossoverPreviewRowsShowInversionAndDelay());
results.push(await testLoadedResearchHidesStalePreparedPreview());
results.push(await testDriverResearchNullProtectionNumbersAreRefusedNotDropped());
results.push(await testRejectedImportReasonSurvivesTheSaveInThePanel());
results.push(await testRejectedPasteAndReasonSurviveDraftIngest());
results.push(await testResearchEchoBackNamesEveryValueWithBadgeAndSource());
results.push(await testResearchEchoBackDisclosesTheDelegation());
results.push(await testResearchEchoBackEscapesUntrustedSources());
results.push(await testResearchEchoBackFollowsTheSameCurrencyRulesAsTheEvidence());
results.push(await testResearchEchoBackRendersRightAfterAPaste());
results.push(await testDriverResearchPromptCopyUsesHttpFallback());
results.push(await testDriverResearchPromptCopyBlockedSelectsPrompt());
results.push(await testDriverResearchNotesCapExplainsBeforePost());
results.push(await testWorkingSetupSummaryAvoidsStorageCounts());
results.push(await testPreparePreviewUpdatesWorkingSetupFirst());
results.push(await testPreparePreviewIgnoresOptionalSubwooferDriverInfo());
results.push(await testPreparePreviewWaitsForInFlightWorkingSetupUpdate());
results.push(await testPartialThreeWayWorkingSetupSummaryReadsCleanly());
results.push(await testDriverMicCaptureIsRemovedFromSoundFlow());
results.push(await testSaveAndApplyUsesSingleFinishEndpoint());
results.push(await testRepinOfferDisclosesWhatIsKeptAndWhatMustBeRedone());
results.push(await testRepinDeclinedOrFailedClearsTheBusyFlag());
results.push(await testResetPartialCleanupSurfacesWarning());
results.push(await testResetReloadsDesignDraftPastAStaleDirtyForm());
results.push(await testFailedResetPreservesCommissioningPanels());
results.push(await testSavedTopologyReconcileFailureNeedsAttention());
results.push(await testFollowerModeRendersLocalDriverUi());
results.push(await testFollowerModeSafeFallbackOnMalformedIsland());
results.push(await testSubwooferDeadEndKeepsAddAffordance());
results.push(await testSubwooferWithSpareOutputRendersAddOn());
results.push(await testUsableSafetyProfileRendersNoCalloutAndNoConfirmControl());
results.push(await testATypedDeclarationWarningIsShownOnAConfirmedProfile());
results.push(await testNoWarningsRenderNoTrustBlock());
results.push(await testIncompleteSafetyProfileHoistsTheReviewCallout());
results.push(await testStaleLowLimitWithABlockerNamesTheCauseAndTheFix());
results.push(await testStaleLowLimitWithoutABlockerNamesTheSaveAsTheRemedy());
results.push(await testRetiredFieldNamesTheSaveAsTheRemedyAndNotCorruption());
results.push(await testIncompleteFromABandRelationshipNamesTheRelationship());
results.push(await testSafetyLimitsDeepLinkOpensTheComponentStep());
results.push(await testCrossChildSpeakerGroupIsDisclosedInTheMapStep());
results.push(await testIssueListEscapesUntrustedVerdictMessages());
results.push(await testNextActionOwnsTheSpeakerPage());
results.push(await testSeatLevelSurvivesStepRenders());
results.push(await testCardioidRearCalibrationPanelGatedOnSavedRearOutput());
results.push(await testTuningHandoffCardMintsAndGoesStale());
results.push(await testDriverSpacingUsesTheExistingDraftSaveAndReload());
results.push(await testInstallationUsesTheExistingDraftSaveAndReload());

console.log(JSON.stringify(Object.assign({ results }, liveTabResult)));
