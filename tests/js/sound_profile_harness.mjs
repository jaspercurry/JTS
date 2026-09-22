// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Minimal DOM harness for the /sound/ static module. It exercises the
// live-source tab state machine plus active-speaker guards without needing a
// browser or CamillaDSP.
//
//   node tests/js/sound_profile_harness.mjs deploy/assets/sound-profile/js/main.js
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { buildFunction, repoPath } from "./_loader.mjs";

const driverVocabularyFixture = JSON.parse(execFileSync(process.env.PYTHON || "python3", [
  "-c", "import json; from jasper.active_speaker.driver_safety_prompt import driver_field_vocabulary; print(json.dumps(driver_field_vocabulary()))",
], {cwd: repoPath("."), encoding: "utf8"}));

const subCrossoverBoundsFixture = JSON.parse(execFileSync(process.env.PYTHON || "python3", [
  "-c", "import json; from jasper.active_speaker.profile import DEFAULT_SUB_CROSSOVER_HZ, SUB_CROSSOVER_HZ_LO, SUB_CROSSOVER_HZ_HI; print(json.dumps(dict(default_hz=DEFAULT_SUB_CROSSOVER_HZ, lo_hz=SUB_CROSSOVER_HZ_LO, hi_hz=SUB_CROSSOVER_HZ_HI)))",
], {cwd: repoPath("."), encoding: "utf8"}));

const modulePath = process.argv[2] || "deploy/assets/sound-profile/js/main.js";
const siblingDir = dirname(modulePath);

const JTSCONFIRM_STUB = [
  /^import\s+\{\s*jtsConfirm\s+\}\s+from\s+["'][^"']+["'];\s*/m,
  "const jtsConfirm = async (...args) => globalThis.__jtsConfirm ? globalThis.__jtsConfirm(...args) : true;\n",
];

// Sibling order is load-bearing: state.js reads the JSON island at eval time.
const runner = buildFunction(
  [
    { path: repoPath("deploy/assets/shared/js/escape.js") },
    { path: repoPath("deploy/assets/shared/js/dom.js") },
    { path: repoPath("deploy/assets/shared/js/http.js") },
    { path: repoPath("deploy/assets/shared/js/copy.js") },
    { path: repoPath("deploy/assets/shared/js/frequency-scale.js") },
    ...[
      "eq-math.js", "state.js", "format.js", "eq-curve.js", "cardioid-compare.js",
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

globalThis.Node = class { static [Symbol.hasInstance](value) { return !!value?._listeners; } };

function makeEl(id) {
  return {
    children: [], dataset: {},
    appendChild(node) { this.children.push(node); return node; },
    replaceChildren(...nodes) { this.children = nodes; },
    replaceWith(node) { Object.assign(this, node); },
    disabled: false, hidden: false,
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
        },
        {
          role: "tweeter",
          physical_output_index: 1,
          startup_muted: true,
          protection_required: true,
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
      },
      {
        role: "tweeter",
        physical_output_index: group.outputBase + 1,
        startup_muted: true,
        protection_required: true,
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
        },
        {
          role: "mid",
          physical_output_index: 1,
          startup_muted: true,
          protection_required: false,
        },
        {
          role: "tweeter",
          physical_output_index: 2,
          startup_muted: true,
          protection_required: true,
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
      }],
    }],
  };
}


function activePayloads() {
  return {
    "./active-speaker/measurements": {
      status: "not_applicable",
      summary: {
        required_driver_count: 0,
        captured_driver_count: 0,
        driver_checks_complete: false,
        driver_measurements_complete: false,
        latest_driver_checks: {},
        latest_driver_measurements: {},
      },
      issues: [],
    },
    "./active-speaker/baseline-profile": {
      status: "blocked",
      permissions: { may_compile: false, may_apply: false },
      config: {},
      issues: [],
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
    research: "Driver values",
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
    ...overrides,
  });
}

function setupHarness(fetchHandler, options = {}) {
  const pageMode = options.mode || "speaker";
  const elements = new Map();
  const absent = new Set(options.absentIslands || []);
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
  const driverIsland = makeEl('jts-driver-fields');
  driverIsland.textContent = JSON.stringify(options.driverVocabulary || driverVocabularyFixture);
  elements.set('jts-driver-fields', driverIsland);
  const boundsIsland = makeEl('jts-sub-crossover-bounds');
  boundsIsland.textContent = JSON.stringify(subCrossoverBoundsFixture);
  elements.set('jts-sub-crossover-bounds', boundsIsland);
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

  const nowPlaying = elements.get('now-playing');
  if (nowPlaying) nowPlaying.after = node => elements.set(node.id, node);

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
    createTextNode(text) { return {textContent: text}; },
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
    return target;
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
    if (path === "./cardioid-compare") return Promise.resolve(response({available: false}));
    if (path === "./active-speaker/design-draft") {
      return Promise.resolve(response({ status: "ready_for_review", summary: {}, operator_inputs: {} }));
    }
    if (path === "./active-speaker/crossover-preview") {
      return Promise.resolve(response({ status: "blocked", issues: [] }));
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
  let finishCompare;
  const eqBase = baseFetch({'./cardioid-compare': () => new Promise(resolve => { finishCompare = resolve; })});
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
  assert.equal(eq.elements.get('cardioid-compare-card').hidden, true);
  assert.equal(eqFetched.filter(path => path === './cardioid-compare').length, 1);
  finishCompare(response({available: false}));
  await eq.flush();

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

  return { splitPageModesRenderAndBootOnlyOwnedSurfaces: true };
}


async function testCardioidCompareMountFollowsEqPageMode() {
  for (const mode of ['eq', 'output']) {
    for (const follower of [false, true]) {
      const compare = {available: true, state: 'normal', tune: {label: 'Current tune', layers: [], applied_at: null},
        level_match: {status: 'unavailable', trim_db: null, louder: null}, expires_in_s: null};
      let requests = 0;
      const harness = setupHarness(baseFetch({
        './cardioid-compare': () => { requests++; return Promise.resolve(response(compare)); },
      }), {mode, follower});
      await harness.flush(); await harness.flush();
      const card = harness.elements.get('cardioid-compare-card');
      assert.equal(!!card, mode === 'eq' && !follower);
      assert.equal(requests, card ? 1 : 0);
      if (!card) continue;
      for (const view of ['off', 'saved', 'draft']) {
        harness.elements.get('tab-' + view).click();
        await harness.flush(); await harness.flush();
        assert.equal(harness.elements.get('cardioid-compare-card'), card);
        assert.equal(card.hidden, false);
        assert.ok(!harness.elements.get('view-body').children.includes(card));
      }
    }
  }
  return {cardioidCompareMountFollowsEqPageMode: true};
}

const results = [];
results.push(await testCardioidCompareMountFollowsEqPageMode());
const liveTabResult = await testLiveTabReplay();
results.push(liveTabResult);
results.push(await testEqSliderDragSendsNoLiveAudioUntilRelease());
results.push(await testCancellingTheNameBoxClosesIt());
results.push(await testVolumeFloorRequiresExplicitSaveButAuditionsDraft());
results.push(await testBlockedSettingsSaveRendersOnTheCard());
results.push(await testBlockedEqCarrierIsThePageState());
results.push(await testLeavingAnUnsavedDraftRestoresThePersistedProfile());
results.push(await testSplitPageModesRenderAndBootOnlyOwnedSurfaces());
console.log(JSON.stringify({ok: true, ...liveTabResult, results}));
