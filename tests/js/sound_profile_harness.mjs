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

const rawSource = readFileSync(modulePath, "utf8");
const source = rawSource
  .replace(/^import\s+\{\s*jtsConfirm\s+\}\s+from\s+["'][^"']+["'];\s*/m, "const jtsConfirm = async () => true;\n")
  .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*escape\.js["'];\s*/m, "")
  .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*eq-math\.js["'];\s*/m, "");
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
    "./active-speaker/measurements": {
      status: "not_applicable",
      summary: {
        required_driver_count: 0,
        captured_driver_count: 0,
        driver_measurements_complete: false,
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

  new Function(escapePreamble + "\n" + eqMathPreamble + "\n" + source)();

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
  const dispatchInput = (attrs, value = "") => {
    const target = {
      value,
      getAttribute(name) { return attrs[name] || ""; },
      hasAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name); },
    };
    for (const fn of viewBody._listeners.input || []) {
      fn({ target });
    }
  };
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return { elements, dispatchClick, dispatchChange, dispatchInput, flush };
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
    if (path === "./active-speaker/prepare-driver-test") {
      const body = JSON.parse(options.body || "{}");
      const topology = topologyPayload();
      const role = body.role || "full_range";
      const outputIndex = role === "tweeter" ? 1 : 0;
      return Promise.resolve(response({
        status: "ready",
        ready: true,
        message: "Ready to start at the quietest test level.",
        target: {
          speaker_group_id: body.speaker_group_id || "main",
          speaker_label: "Main speaker",
          role,
          physical_output_index: outputIndex,
          label: "Main " + role.replace(/_/g, " ") + " on Output " + (outputIndex + 1),
        },
        output_topology: topology,
        channel_identity: topology.channel_identity,
        clock_domain: topology.clock_domain,
        session: active["./active-speaker/safe-playback"],
        calibration_level: active["./active-speaker/calibration-level"],
        startup_load: active["./active-speaker/startup-load"],
        staged_config: active["./active-speaker/staged-config"],
      }));
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

function assertQuietTestSurfaceVisible(harness, label) {
  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of ["Measure drivers"]) {
    if (!html.includes(expected)) {
      fail(`${label} should keep the driver-test surface visible`, { expected, html });
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
  harness.dispatchClick({
    "data-act": "check-output-readiness",
    "data-group-id": "main",
    "data-role": "full_range",
    "data-output-index": "0",
    "data-speaker-label": "Main speaker",
    "data-label": "Main full range on Output 1",
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();

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
  for (const expected of ["Measure drivers", "Choose first driver"]) {
    if (!html.includes(expected)) {
      fail("Partial active-speaker refresh should keep the known driver-test surface", { expected, html });
    }
  }
  for (const forbidden of [
    "Partial refresh: environment probe failed",
    "What needs attention",
    "route_verified",
  ]) {
    if (html.includes(forbidden)) {
      fail("Background active-speaker probes should not leak implementation errors into the setup flow", { forbidden, html });
    }
  }
  return { partialRefreshPreservedSections: true };
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
  includes("Add driver and crossover info");
  includes("Crossover settings");
  includes("Use AI to fill these settings");
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
  return { activeCrossoverFirstStepRendered: true };
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
        latest_summed_validations: {},
      },
      permissions: { may_compile_baseline: false },
      issues: [],
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    'data-output-step="profile" open',
    "Validate and apply",
    "Combined crossover check",
    "Blend sounds right",
    "Save active profile",
  ]) {
    if (!html.includes(expected)) {
      fail("Completed driver measurements should advance to the profile card", { expected, html });
    }
  }
  if (html.includes('data-output-step="safety" open')) {
    fail("Completed driver measurements should not reopen the driver measurement card", { html });
  }
  return { measuredDriversOpenProfileStep: true };
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
    "saved for review",
    "cannot switch normal playback to it from here yet",
    "Rebuild profile",
    "Apply active profile",
  ]) {
    if (!html.includes(expected)) {
      fail("Apply-blocked profiles should explain the limitation in user terms", { expected, html });
    }
  }
  for (const forbidden of ["outputd owns", "handoff"]) {
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

  if (designSaves.length !== 1) fail("Saving crossover settings should POST once", { designSaves });
  const saved = designSaves[0];
  const manualCandidate = saved.manual_settings.crossover_candidates[0];
  const importedCandidate = saved.driver_research.crossover_candidates[0];
  if (manualCandidate.frequency_hz !== 2100 || manualCandidate.slope_db_per_octave !== 24) {
    fail("Visible manual crossover fields should win when saving", { saved });
  }
  if (importedCandidate.frequency_hz !== 4000 || importedCandidate.slope_db_per_octave !== 12) {
    fail("Imported research should still be preserved as research evidence", { saved });
  }
  return { visibleCrossoverSettingsWinOverImportedJson: true };
}

async function testConfirmedOutputChoosesFirstQuietDriver() {
  const confirmedTopology = topologyPayload();
  confirmedTopology.channel_identity = {
    kind: "jts_output_channel_identity_report",
    status: "verified",
    assigned_channel_count: 1,
    verified_channel_count: 1,
    unverified_channel_count: 0,
    next_step: "Outputs are confirmed. Continue when you are ready.",
    targets: [{
      id: "main:full_range",
      speaker_group_id: "main",
      speaker_label: "Main speaker",
      role: "full_range",
      assigned: true,
      identity_verified: true,
      physical_output_index: 0,
    }],
  };
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  let html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "Choose first driver",
    "Test Full range",
    "Output 1",
  ]) {
    if (!html.includes(expected)) {
      fail("Confirmed output should render first quiet-driver choices", { expected, html });
    }
  }
  if (html.includes("Test this driver") || html.includes("Check readiness")) {
    fail("Confirmed output choices should not expose old readiness language", { html });
  }
  if (html.includes("Test volume")) {
    fail("Volume controls should wait until a test driver is selected", { html });
  }
  const readinessActionCount = (html.match(/data-act="check-output-readiness"/g) || []).length;
  if (readinessActionCount !== 1) {
    fail("First driver-test choice should appear only in the driver-test card", {
      readinessActionCount,
      html,
    });
  }

  harness.dispatchClick({
    "data-act": "check-output-readiness",
    "data-group-id": "main",
    "data-role": "full_range",
    "data-output-index": "0",
    "data-speaker-label": "Main speaker",
    "data-label": "Main full range on Output 1",
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  html = harness.elements.get("view-body").innerHTML;
  for (const expected of ["Selected driver", "Main full range on Output 1", "Preview test signal", "Test volume"]) {
    if (!html.includes(expected)) {
      fail("Chosen driver should render the readiness result without playing sound", { expected, html });
    }
  }
  return { confirmedOutputChoosesFirstQuietDriver: true };
}

async function testQuietTestPathIssuesStayActionable() {
  const requestOrder = [];
  const confirmedTopology = activeTwoWayTopologyPayload();
  confirmedTopology.speaker_groups[0].channels[1].protection_status = "required_missing";
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
    "./active-speaker/design-draft": (_path, options = {}) => {
      if (options.method) requestOrder.push("draft");
      return Promise.resolve(response({
        status: "ready_for_review",
        summary: {
          driver_count: 2,
          crossover_candidate_count: 1,
          manual_driver_count: 0,
          manual_crossover_candidate_count: 0,
        },
        operator_inputs: {},
      }));
    },
    "./active-speaker/crossover-preview": (_path, options = {}) => {
      if (!options.method) {
        return Promise.resolve(response({
          kind: "jts_active_speaker_crossover_preview",
          status: "stale",
          permissions: { may_prepare_protected_startup_config: false },
          summary: { ready_crossover_count: 0 },
          issues: [{ code: "crossover_preview_stale_design_draft", severity: "warning" }],
        }));
      }
      requestOrder.push("preview");
      return Promise.resolve(response({
        kind: "jts_active_speaker_crossover_preview",
        status: "ready_for_protected_staging",
        permissions: { may_prepare_protected_startup_config: true },
        summary: { ready_crossover_count: 1 },
        issues: [],
      }));
    },
    "./active-speaker/environment": () => Promise.resolve(response({
      status: "blocked",
      ok_to_load_active_config: false,
      issues: [
        { code: "route_verified_not_verified", message: "Music renderers: route_verified is not verified" },
        { code: "protected_by_active_baseline_not_verified", message: "Music renderers: protected_by_active_baseline is not verified" },
        { code: "bypass_disabled_not_verified", message: "Music renderers: bypass_disabled is not verified" },
      ],
      warning: "This probe does not play tones, reload CamillaDSP, or authorize active-speaker audio output.",
    })),
    "./active-speaker/safe-playback": () => Promise.resolve(response({
      status: "inactive",
      issues: [],
      quiet_start: { status: "not_started", floor_audio_confirmed: false },
    })),
    "./active-speaker/staged-config": () => Promise.resolve(response({ status: "not_staged", issues: [] })),
    "./active-speaker/startup-load": () => Promise.resolve(response({
      state: {
        status: "not_loaded",
        rollback_available: false,
        current_config_matches_loaded: false,
      },
      preflight: { status: "blocked", load_allowed: false, issues: [] },
    })),
    "./active-speaker/prepare-driver-test": (_path, options = {}) => {
      requestOrder.push("prepare");
      if (!options.method) throw new Error("prepare-driver-test should be POST");
      const body = JSON.parse(options.body || "{}");
      return Promise.resolve(response({
        status: "ready",
        ready: true,
        message: "Ready to start at the quietest test level.",
        target: {
          speaker_group_id: body.speaker_group_id,
          speaker_label: "Main speaker",
          role: body.role,
          physical_output_index: body.role === "tweeter" ? 1 : 0,
          label: "Main " + body.role + " on Output " + (body.role === "tweeter" ? "2" : "1"),
        },
        output_topology: confirmedTopology,
        channel_identity: confirmedTopology.channel_identity,
        session: {
          status: "armed",
          issues: [],
          calibration_level: levelPayload(-80),
          quiet_start: { status: "floor_required", floor_audio_confirmed: false },
        },
        calibration_level: levelPayload(-80),
        startup_load: {
          load: { status: "loaded", rollback_available: true },
          preflight: { status: "ready", load_allowed: true, issues: [] },
        },
        staged_config: { status: "staged", issues: [] },
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Choose the driver you want to hear first.")) {
    fail("Renderer path evidence should be hidden behind the driver choice", { html });
  }
  if (!html.includes("Test Woofer") || !html.includes("Test Tweeter")) {
    fail("Driver choices should remain available while internal setup is pending", { html });
  }
  for (const forbidden of [
    "What needs attention",
    "Music renderers",
    "route_verified",
    "protected_by_active_baseline",
    "bypass_disabled",
    "Check again",
    "Set up quiet test mode",
  ]) {
    if (html.includes(forbidden)) {
      fail("Quiet-test setup issues should not expose backend evidence labels", { forbidden, html });
    }
  }
  harness.dispatchClick({
    "data-act": "check-output-readiness",
    "data-group-id": "main",
    "data-role": "woofer",
    "data-output-index": "0",
    "data-speaker-label": "Main speaker",
    "data-label": "Main woofer on Output 1",
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();
  await harness.flush();
  await harness.flush();
  const expectedOrder = ["prepare"];
  if (JSON.stringify(requestOrder) !== JSON.stringify(expectedOrder)) {
    fail("Driver test should ask the backend to prepare the selected driver in one product-level operation", { requestOrder });
  }
  const nextHtml = harness.elements.get("view-body").innerHTML;
  if (!nextHtml.includes("Selected driver") || !nextHtml.includes("Start very quiet woofer tone")) {
    fail("Driver test should land on the actual sound-test action", { html: nextHtml });
  }
  return { quietTestPathIssuesStayActionable: true };
}

async function testProtectionSetupFailureDoesNotAskForDriverAgain() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  confirmedTopology.speaker_groups[0].channels[1].protection_status = "software_guard_requested";
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
    "./active-speaker/safe-playback": () => Promise.resolve(response({
      status: "inactive",
      issues: [],
      quiet_start: { status: "not_started", floor_audio_confirmed: false },
    })),
    "./active-speaker/staged-config": () => Promise.resolve(response({ status: "not_staged", issues: [] })),
    "./active-speaker/prepare-driver-test": () => Promise.resolve(response({
      status: "needs_action",
      ready: false,
      message: "JTS needs to save the quiet limit for that high-frequency driver before any tone can play. Save the crossover settings, then choose the driver again.",
      target: {
        speaker_group_id: "main",
        speaker_label: "Main speaker",
        role: "woofer",
        physical_output_index: 0,
        label: "Main woofer on Output 1",
      },
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
    })),
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchClick({
    "data-act": "check-output-readiness",
    "data-group-id": "main",
    "data-role": "woofer",
    "data-output-index": "0",
    "data-speaker-label": "Main speaker",
    "data-label": "Main woofer on Output 1",
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("save the quiet limit for that high-frequency driver")) {
    fail("Protection setup failures should explain the hidden setup action", { html });
  }
  if (html.includes("Choose the driver you want to test first")) {
    fail("Protection setup failures should not pretend the user failed to choose a driver", { html });
  }
  if (html.includes("needs attention")) {
    fail("Protection setup failures should not use an alarming red status pill", { html });
  }
  return { protectionSetupFailureDoesNotAskForDriverAgain: true };
}

const results = [];
const liveTabResult = await testLiveTabReplay();
results.push(liveTabResult);
results.push(await testQuietTestSurfaceSurvivesStartupActions());
results.push(await testStaleLevelResponseDiscarded());
results.push(await testPartialRefreshKeepsSuccessfulSections());
results.push(await testActiveCrossoverFirstStepRender());
results.push(await testMeasuredDriversOpenProfileStep());
results.push(await testCompiledProfileApplyBlockStaysUnderstandable());
results.push(await testVisibleCrossoverSettingsWinOverImportedJson());
results.push(await testConfirmedOutputChoosesFirstQuietDriver());
results.push(await testQuietTestPathIssuesStayActionable());
results.push(await testProtectionSetupFailureDoesNotAskForDriverAgain());

console.log(JSON.stringify(Object.assign({ ok: true, results }, liveTabResult)));
