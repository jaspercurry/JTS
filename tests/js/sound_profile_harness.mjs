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
const activeSpeakerUiPath = new URL("../../deploy/assets/sound-profile/js/active-speaker-ui.js", import.meta.url);
const activeSpeakerUiPreamble = readFileSync(activeSpeakerUiPath, "utf8")
  .replace(/^export\s+/gm, "");

const rawSource = readFileSync(modulePath, "utf8");
const source = rawSource
  .replace(/^import\s+\{\s*jtsConfirm\s+\}\s+from\s+["'][^"']+["'];\s*/m, "const jtsConfirm = async () => true;\n")
  .replace(/^import\s+\{[^}]*\}\s+from\s+["'][^"']*escape\.js["'];\s*/m, "")
  .replace(/^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']*active-speaker-ui\.js["'];\s*/m, "")
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
      max_level_dbfs: -30,
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
      max_level_dbfs: -30,
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
    escapePreamble + "\n" + eqMathPreamble + "\n" + activeSpeakerUiPreamble + "\n" + source
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
        tone_backend: {
          status: "audio_enabled",
          audio_enabled: true,
          tone_playback_implemented: true,
        },
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

function commissionCardHtml(html) {
  const match = String(html || "").match(
    /<div class="commission-card">[\s\S]*?commission-card__followup[\s\S]*?<\/p><\/div>/
  );
  return match ? match[0] : "";
}

async function loadAndSetActiveState(harness) {
  await harness.flush();
  await harness.flush();
  await harness.flush();
}

function assertQuietTestSurfaceVisible(harness, label) {
  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of ["Test each driver"]) {
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

async function testDriverTestUsesAutomaticRampNotSlider() {
  const fetchHandler = baseFetch();
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

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "Listen for this driver",
    "Test progress",
    "Start quiet test",
    "JTS starts very quiet and automatically tries a little louder",
    "Wrong driver",
    "Stop",
  ]) {
    if (!html.includes(expected)) {
      fail("Driver test should use the automatic ramp surface", { expected, html });
    }
  }
  for (const forbidden of [
    "Test volume",
    "active-speaker-level\" min=",
    "I did not hear anything",
    "move this a little louder",
    "Did this driver make the sound?",
  ]) {
    if (html.includes(forbidden)) {
      fail("Driver test should not expose the removed manual slider flow", { forbidden, html });
    }
  }
  return { driverTestUsesAutomaticRampNotSlider: true };
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
      fail("Completed driver checks should advance to the profile card", { expected, html });
    }
  }
  if (html.includes('data-output-step="safety" open')) {
    fail("Completed driver checks should not reopen the driver-test card", { html });
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
  for (const expected of ["Listen for this driver", "Main full range on Output 1", "Start quiet test", "Test progress"]) {
    if (!html.includes(expected)) {
      fail("Chosen driver should render the readiness result without playing sound", { expected, html });
    }
  }
  return { confirmedOutputChoosesFirstQuietDriver: true };
}

async function testBlockedAudibleDriverTestDoesNotClaimSoundPlayed() {
  const confirmedTopology = topologyPayload();
  confirmedTopology.channel_identity = {
    kind: "jts_output_channel_identity_report",
    status: "verified",
    assigned_channel_count: 1,
    verified_channel_count: 1,
    unverified_channel_count: 0,
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
    "./active-speaker/play-tone": (_path, options = {}) => {
      if (!options.method) throw new Error("play-tone should be POST");
      return Promise.resolve(response({
        playback: {
          status: "blocked",
          audio_emitted: false,
          backend: null,
          target: {
            speaker_group_id: "main",
            role: "full_range",
            label: "Main full range on Output 1",
          },
          tone: { frequency_hz: 500, level_dbfs: -80, duration_ms: 500 },
          issues: [{
            code: "audio_backend_not_enabled",
            message: "audible channel tests require explicit lab backend enablement",
          }],
        },
        session: activePayloads()["./active-speaker/safe-playback"],
      }));
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

  let html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("ready to test") ||
      !html.includes("Start quiet test")) {
    fail("Audio-enabled readiness should land on the audible-test action", { html });
  }
  if (html.includes("preview ready")) {
    fail("Audio-enabled readiness should not be framed as preview-only", { html });
  }

  harness.dispatchClick({
    "data-act": "play-output-readiness-tone",
    "data-group-id": "main",
    "data-role": "full_range",
    "data-label": "Main full range on Output 1",
    "data-audio": "true",
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const statusText = harness.elements.get("status").textContent;
  html = harness.elements.get("view-body").innerHTML;
  if (statusText.includes("Played quiet channel test")) {
    fail("Blocked audible playback must not be reported as played", { statusText, html });
  }
  if (!statusText.includes("No sound played") ||
      !statusText.includes("Driver tests are not available")) {
    fail("Blocked audible playback should explain that no sound played", { statusText, html });
  }
  if (!html.includes("No sound played")) {
    fail("Blocked audible playback should render no-audio evidence", { html });
  }
  for (const forbidden of ["Playback: audio_backend_not_enabled", "tone_plan_not_ready"]) {
    if (html.includes(forbidden) || statusText.includes(forbidden)) {
      fail("Blocked audible playback should not expose backend issue codes", { forbidden, statusText, html });
    }
  }
  return { blockedAudibleDriverTestDoesNotClaimSoundPlayed: true };
}

async function testAudibleRampRecordsSilenceAndAsksBackendForNextStep() {
  const confirmedTopology = topologyPayload();
  confirmedTopology.channel_identity = {
    kind: "jts_output_channel_identity_report",
    status: "verified",
    assigned_channel_count: 1,
    verified_channel_count: 1,
    unverified_channel_count: 0,
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
  const target = {
    speaker_group_id: "main",
    speaker_label: "Main speaker",
    role: "full_range",
    output_index: 0,
    label: "Main full range on Output 1",
  };
  const persistedTarget = {
    speaker_group_id: "main",
    driver_role: "full_range",
    physical_output_index: 0,
  };
  let session = activePayloads()["./active-speaker/safe-playback"];
  let level = levelPayload(-80);
  const playToneCalls = [];
  const levelPosts = [];
  const floorResults = [];
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
    })),
    "./active-speaker/prepare-driver-test": () => Promise.resolve(response({
      status: "ready",
      ready: true,
      message: "Ready to start at the quietest test level.",
      target,
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
      session,
      calibration_level: level,
      tone_backend: {
        status: "audio_enabled",
        audio_enabled: true,
        tone_playback_implemented: true,
      },
      startup_load: activePayloads()["./active-speaker/startup-load"],
      staged_config: activePayloads()["./active-speaker/staged-config"],
    })),
    "./active-speaker/play-tone": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      playToneCalls.push(body);
      const playbackId = `play-${playToneCalls.length}`;
      session = {
        status: "armed",
        issues: [],
        quiet_start: {
          status: "floor_pending_operator",
          floor_audio_confirmed: false,
          pending_playback_id: playbackId,
          current_target: target,
        },
      };
      return Promise.resolve(response({
        playback: {
          playback_id: playbackId,
          status: "played",
          audio_emitted: true,
          backend: "unit-test",
          target,
          tone: {
            frequency_hz: 500,
            level_dbfs: level.test_signal.requested_level_dbfs,
            duration_ms: 300,
          },
          issues: [],
        },
        session,
      }));
    },
    "./active-speaker/floor-audio-result": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      floorResults.push(body);
      session = {
        status: "armed",
        issues: [],
        quiet_start: {
          status: "floor_required",
          floor_audio_confirmed: false,
          current_target: persistedTarget,
          last_operator_result: {
            outcome: body.outcome,
            playback_id: body.playback_id,
            target: persistedTarget,
          },
        },
      };
      return Promise.resolve(response(session));
    },
    "./active-speaker/driver-measurement": () => Promise.resolve(response({
      status: "needs_driver_measurements",
      summary: {
        required_driver_count: 1,
        captured_driver_count: 0,
        driver_measurements_complete: false,
        latest_driver_measurements: {
          "main:full_range": { captured: false, outcome: "silent" },
        },
      },
      issues: [],
    })),
    "./active-speaker/calibration-level": (_path, options = {}) => {
      if (!options.method) return Promise.resolve(response(level));
      const body = JSON.parse(options.body || "{}");
      levelPosts.push(body);
      if (body.action === "auto_step") {
        level = levelPayload(-80);
        level.auto_level = { action: "hold_at_cap", status: "maxed", next_level_dbfs: -80 };
        level.applied_delta_db = 0;
      }
      return Promise.resolve(response(level));
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

  let html = harness.elements.get("view-body").innerHTML;
  if (!html.includes('data-act="stop-active-speaker" disabled')) {
    fail("Stop should be visible but inactive before a tone is playing", { html });
  }

  harness.dispatchClick({
    "data-act": "play-output-readiness-tone",
    "data-group-id": "main",
    "data-role": "full_range",
    "data-label": "Main full range on Output 1",
    "data-audio": "true",
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "I hear this driver",
    "Wrong driver",
    "Stop",
    "active-speaker-actions--driver-test",
  ]) {
    if (!html.includes(expected)) {
      fail("After playback, the stable driver-test row should stay visible", { expected, html });
    }
  }
  if (html.includes("Did this driver make the sound?") ||
      html.includes("If you hear nothing, wait") ||
      html.includes("Play full range tone")) {
    fail("After playback, the UI should not swap in the old identity block", { html });
  }
  await new Promise((resolve) => setTimeout(resolve, 1300));
  await harness.flush();
  await harness.flush();
  await harness.flush();

  html = harness.elements.get("view-body").innerHTML;
  if (!/data-outcome="heard_correct_driver" data-playback-id="[^"]+">I hear this driver<\/button>/.test(html) ||
      !/data-outcome="heard_wrong_driver" data-playback-id="[^"]+">Wrong driver<\/button>/.test(html)) {
    fail("Response buttons should stay enabled after the first pulse while the ramp waits", { html });
  }
  if (!floorResults.some((body) => body.outcome === "silent")) {
    fail("Automatic ramp should record an internal not-heard result before raising", { floorResults });
  }
  if (!levelPosts.some((body) => body.action === "auto_step" && body.speaker_group_id === "main")) {
    fail("Automatic ramp should ask the backend for the next safe level", { levelPosts });
  }
  return { audibleRampRecordsSilenceAndAsksBackendForNextStep: true };
}

async function testUnavailableAudioBackendDoesNotPostAudibleTone() {
  const confirmedTopology = topologyPayload();
  let playToneCalls = 0;
  const fetchHandler = baseFetch({
    "./output-topology": () => Promise.resolve(response({
      output_topology: confirmedTopology,
      channel_identity: confirmedTopology.channel_identity,
    })),
    "./active-speaker/prepare-driver-test": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      const topology = topologyPayload();
      return Promise.resolve(response({
        status: "ready",
        ready: true,
        message: "Ready to start at the quietest test level.",
        target: {
          speaker_group_id: body.speaker_group_id || "main",
          speaker_label: "Main speaker",
          role: body.role || "woofer",
          physical_output_index: 0,
          label: "Main speaker woofer (Output 1)",
        },
        output_topology: topology,
        channel_identity: topology.channel_identity,
        clock_domain: topology.clock_domain,
        session: activePayloads()["./active-speaker/safe-playback"],
        calibration_level: activePayloads()["./active-speaker/calibration-level"],
        tone_backend: {
          status: "artifact_only",
          audio_enabled: false,
          tone_playback_implemented: false,
          issues: [{
            code: "audio_backend_not_enabled",
            message: "audible channel tests require explicit lab backend enablement",
          }],
        },
        startup_load: activePayloads()["./active-speaker/startup-load"],
        staged_config: activePayloads()["./active-speaker/staged-config"],
      }));
    },
    "./active-speaker/play-tone": () => {
      playToneCalls += 1;
      return Promise.resolve(response({ playback: { status: "should_not_happen" } }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  harness.dispatchClick({
    "data-act": "check-output-readiness",
    "data-group-id": "main",
    "data-role": "woofer",
    "data-output-index": "0",
    "data-speaker-label": "Main speaker",
    "data-label": "Main speaker woofer (Output 1)",
  });
  await harness.flush();
  await harness.flush();
  await harness.flush();

  const statusText = harness.elements.get("status").textContent;
  const html = harness.elements.get("view-body").innerHTML;
  if (playToneCalls !== 0) {
    fail("Unavailable audio backend should not POST an audible tone request", { playToneCalls, statusText, html });
  }
  if (statusText.includes("No sound played")) {
    fail("Unavailable audio backend should not create a failed test after preparation", { statusText, html });
  }
  for (const expected of ["not available", "Driver tests are not available on this install yet."]) {
    if (!html.includes(expected)) {
      fail("Unavailable audio backend should render a clear no-test state", { expected, html });
    }
  }
  if (html.includes("Generate WAV") || html.includes("data-audio=\"false\"")) {
    fail("Unavailable audio backend should not expose diagnostic WAV generation", { html });
  }
  for (const hidden of [
    "Play woofer tone",
    "Test volume",
    "Return test volume",
    "data-act=\"stop-active-speaker\"",
  ]) {
    if (html.includes(hidden)) {
      fail("Preview-only driver state should not expose audible-test controls", { hidden, html });
    }
  }
  for (const forbidden of ["Playback: audio_backend_not_enabled", "tone_plan_not_ready"]) {
    if (html.includes(forbidden) || statusText.includes(forbidden)) {
      fail("Unavailable audio backend should not leak backend issue codes", { forbidden, statusText, html });
    }
  }
  return { unavailableAudioBackendDoesNotPostAudibleTone: true };
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
        ramp: { confirmed_roles: [], pending: { role: "woofer", gain_db: gain } },
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
  if (!html.includes('class="commission-card"')) fail("commission card not rendered", { html });
  if (html.includes('data-act="commission-arm"')) fail("arm button should not be visible", { html });
  if (!html.includes('data-act="commission-step"')) fail("start button missing before arm", { html });
  if (!html.includes(">Start tone</button>")) fail("idle card should expose Start tone", { html });

  // Start tone silently opens the quiet driver setup, then begins the
  // automatic ramp. The card treats the whole ramp as one playing state; "too
  // quiet" is internal, not a visible operator button.
  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();
  if (!posts.some((x) => x.path === "./active-speaker/commission-load" && x.body.role === "woofer")) {
    fail("commission-load not posted before ramp start", { posts });
  }
  if (!posts.some((x) => x.path === "./active-speaker/commission-ramp-step")) {
    fail("commission-ramp-step not posted on step", { posts });
  }
  html = harness.elements.get("view-body").innerHTML;
  let cardHtml = commissionCardHtml(html);
  for (const expected of ["Stop tone", "I hear the tone", "Wrong driver"]) {
    if (!cardHtml.includes(expected)) fail("playing row should expose stable tone controls", { expected, cardHtml });
  }
  if (cardHtml.includes("commission-card__message")) {
    fail("automatic ramp should not render a changing progress line", { cardHtml });
  }
  if (cardHtml.includes('data-act="commission-abort" disabled')) {
    fail("Stop tone must stay enabled while the automatic ramp is active", { cardHtml });
  }
  for (const flappy of ["Raising", "Starting Woofer tone", "Recording…"]) {
    if (cardHtml.includes(flappy)) fail("automatic ramp should not surface transient busy copy", { flappy, cardHtml });
  }
  for (const hidden of ["Too quiet", "Too loud"]) {
    if (cardHtml.includes(hidden)) fail("automatic ramp should not expose legacy manual loudness buttons", { hidden, cardHtml });
  }

  harness.dispatchClick({ "data-act": "commission-ack", "data-outcome": "heard_correct_driver" });
  await harness.flush(); await harness.flush(); await harness.flush();
  if (!posts.some((x) =>
      x.path === "./active-speaker/commission-ramp-ack" &&
      x.body.outcome === "heard_correct_driver")) {
    fail("heard verdict should be posted when the user hears the tone", { posts });
  }
  const visibleSilentAcks = posts.filter((x) =>
    x.path === "./active-speaker/commission-ramp-ack" && x.body.outcome === "silent");
  if (visibleSilentAcks.length) {
    fail("user-visible controls should not post manual silent retries", { posts });
  }
  return { commissionCardArmsAndSteps: true };
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
  for (const expected of ["Stop tone", "I hear the tone", "Wrong driver"]) {
    if (!cardHtml.includes(expected)) fail("pending ramp step should reuse the stable playing row", { expected, cardHtml });
  }
  for (const hidden of ["Too quiet", "Too loud"]) {
    if (cardHtml.includes(hidden)) fail("pending ramp step should not expose legacy manual loudness buttons", { hidden, cardHtml });
  }
  if (html.includes('data-act="commission-step"')) {
    fail("pending ramp step must block another step until it is acknowledged", { html });
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

async function testCommissionRampLimitStopsAutoRamp() {
  let commissionState = {
    commission_load: {
      status: "loaded",
      target: { role: "woofer", audible_gain_db: -30 },
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
      return Promise.resolve(response({
        status: "blocked",
        role: "woofer",
        speaker_group_id: "main",
        current_gain_db: -30,
        next_gain_db: -30,
        max_gain_db: -30,
        issues: [{
          severity: "blocker",
          code: "commission_ramp_at_limit",
          message: "the driver test is already at the maximum bounded level",
        }],
      }));
    },
    "./active-speaker/commission-ramp-abort": (p, o) => {
      posts.push({ path: p, body: JSON.parse(o.body || "{}") });
      commissionState = {
        commission_load: { status: "rolled_back", target: {}, rollback_available: false },
        ramp: { confirmed_roles: [], pending: null },
        floor: { status: "floor_required", floor_audio_confirmed: false },
      };
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
    fail("safe-limit response must stop the automatic ramp after one blocked step", { posts });
  }
  if (!posts.some((x) => x.path === "./active-speaker/commission-ramp-abort")) {
    fail("safe-limit response should hard Stop the continuous tone", { posts });
  }
  const html = harness.elements.get("view-body").innerHTML;
  if (!html.includes("Reached the safe test limit")) {
    fail("safe-limit response should surface the room-facing action", { html });
  }
  return { commissionRampLimitStopsAutoRamp: true };
}

const results = [];
const liveTabResult = await testLiveTabReplay();
results.push(liveTabResult);
results.push(await testQuietTestSurfaceSurvivesStartupActions());
results.push(await testDriverTestUsesAutomaticRampNotSlider());
results.push(await testActiveCrossoverFirstStepRender());
results.push(await testActiveRouteLimitsRenderedTemplates());
results.push(await testMeasuredDriversOpenProfileStep());
results.push(await testCompiledProfileApplyBlockStaysUnderstandable());
results.push(await testVisibleCrossoverSettingsWinOverImportedJson());
results.push(await testConfirmedOutputChoosesFirstQuietDriver());
results.push(await testBlockedAudibleDriverTestDoesNotClaimSoundPlayed());
results.push(await testAudibleRampRecordsSilenceAndAsksBackendForNextStep());
results.push(await testUnavailableAudioBackendDoesNotPostAudibleTone());
results.push(await testCommissionCardArmsAndSteps());
results.push(await testCommissionPendingStepShowsAckWithoutFloorFlag());
results.push(await testCommissionArmBlockedSurfacesReason());
results.push(await testCommissionActiveGraphBlockSurfacesReason());
results.push(await testCommissionOutputReconcileFailureSurfacesReason());
results.push(await testCommissionToneFailureStopsAutoRamp());
results.push(await testCommissionRampLimitStopsAutoRamp());

console.log(JSON.stringify(Object.assign({ ok: true, results }, liveTabResult)));
