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
const measurementAudioPreamble = `
const DEFAULT_SAMPLE_RATE = 48000;
async function createMonoRecorder() {
  return {
    context: { sampleRate: DEFAULT_SAMPLE_RATE },
    start() {},
    async stop() { return new Float32Array(480); },
    async close() {},
  };
}
function float32ToWavBlob() {
  return {
    async arrayBuffer() {
      return Uint8Array.from([82, 73, 70, 70, 4, 0, 0, 0]).buffer;
    },
  };
}
`;

const rawSource = readFileSync(modulePath, "utf8");
const source = rawSource
  .replace(/^import\s+\{\s*jtsConfirm\s+\}\s+from\s+["'][^"']+["'];\s*/m, "const jtsConfirm = async () => true;\n")
  .replace(/^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']*measurement-audio\.js["'];\s*/m, "")
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
  globalThis.btoa = (binary) => Buffer.from(binary, "binary").toString("base64");
  globalThis.fetch = fetchHandler;

  new Function(
    escapePreamble + "\n" + eqMathPreamble + "\n" +
      activeSpeakerUiPreamble + "\n" + measurementAudioPreamble + "\n" + source
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

async function testPassiveLayoutsRenderNoActiveDriverTestCard() {
  const fetchHandler = baseFetch();
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    "No active driver test",
    "no separate direct-DAC driver test in the product UI",
  ]) {
    if (!html.includes(expected)) {
      fail("Passive layouts should render the non-active product card", { expected, html });
    }
  }
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
  return { passiveLayoutsRenderNoActiveDriverTestCard: true };
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
  includes("Working setup");
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
  excludes("saved drivers");
  excludes("saved crossover settings");
  excludes("Save crossover settings");
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
  });
  const harness = setupHarness(fetchHandler);
  await loadAndSetActiveState(harness);

  const html = harness.elements.get("view-body").innerHTML;
  for (const expected of [
    'data-output-step="profile" open',
    "Validate and apply",
    "Combined crossover check",
    "JTS could not open the quiet combined-test path. Press Play combined test to retry.",
    "Record mic capture",
    "Save active profile",
  ]) {
    if (!html.includes(expected)) {
      fail("Completed driver checks should advance to the profile card", { expected, html });
    }
  }
  if (html.includes("could not open the combined active-speaker test path")) {
    fail("Combined-test implementation internals should not be the primary recovery copy", { html });
  }
  if (html.includes('data-output-step="safety" open')) {
    fail("Completed driver checks should not reopen the driver-test card", { html });
  }
  return { measuredDriversOpenProfileStep: true };
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
        max_level_dbfs: -30,
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
            body: { speaker_group_id: "main", audio: true, duration_ms: 500 },
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
  if (!html.includes("Combined test level") || !html.includes('max="-66"')) {
    fail("Combined card should expose a bounded next-play level control", { html });
  }
  harness.dispatchInput({ "data-summed-test-level": "main" }, "-66");
  harness.dispatchClick({
    "data-act": "prepare-summed-test",
    "data-group-id": "main",
    "data-label": "Main speaker",
  });
  for (let i = 0; i < 8; i += 1) await harness.flush();

  if (posts.length !== 1) {
    fail("Playing the combined test should POST once", { posts });
  }
  if (posts[0].level_dbfs !== -66) {
    fail("Combined test should POST the selected bounded level", { posts });
  }
  return { combinedTestLevelPostsSelectedBoundedLevel: true };
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
    "Crossover preview ready. No filters are active and no sound was played."
  )) {
    fail("Preview completion should keep the no-audio safety copy", {
      status: harness.elements.get("status").textContent,
    });
  }
  return { preparePreviewUpdatesWorkingSetupFirst: true };
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
  if (!html.includes('class="commission-card"')) fail("commission card not rendered", { html });
  if (html.includes('data-act="commission-arm"')) fail("arm button should not be visible", { html });
  if (!html.includes('data-act="commission-step"')) fail("start button missing before arm", { html });
  if (!html.includes(">Start tone</button>")) fail("idle card should expose Start tone", { html });

  // Start tone silently opens the quiet driver setup, then begins the
  // automatic ramp. The card treats the whole ramp as one playing state; "too
  // quiet" is internal, not a visible operator button.
  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  harness.dispatchClick({ "data-act": "commission-step", "data-role": "woofer" });
  await harness.flush(); await harness.flush(); await harness.flush();
  await harness.flush(); await harness.flush(); await harness.flush();
  const loadPosts = posts.filter((x) =>
    x.path === "./active-speaker/commission-load" && x.body.role === "woofer");
  if (loadPosts.length !== 1) {
    fail("rapid Start clicks should open the quiet driver test once", { posts });
  }
  if (!posts.some((x) => x.path === "./active-speaker/commission-ramp-step")) {
    fail("commission-ramp-step not posted on step", { posts });
  }
  html = harness.elements.get("view-body").innerHTML;
  let cardHtml = commissionCardHtml(html);
  for (const expected of ["Stop tone", "I hear the tone", "Wrong driver"]) {
    if (!cardHtml.includes(expected)) fail("playing row should expose stable tone controls", { expected, cardHtml });
  }
  for (const expected of ["Status", "Tone playing", "250 Hz"]) {
    if (!cardHtml.includes(expected)) fail("playing card should expose stable status and tone frequency", { expected, cardHtml });
  }
  if (cardHtml.includes("By-ear") || cardHtml.includes("Not yet made audible")) {
    fail("playing card should not expose the old flickering by-ear state", { cardHtml });
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
  const cardHtml = commissionCardHtml(html);
  if (!cardHtml.includes("Complete")) {
    fail("all confirmed driver roles should render the card complete", { cardHtml });
  }
  if (!cardHtml.includes("All drivers are confirmed")) {
    fail("complete driver test should tell the user to continue", { cardHtml });
  }
  if (!cardHtml.includes("Continue to validate")) {
    fail("complete driver test should provide an explicit next action", { cardHtml });
  }
  if (cardHtml.includes("next: Woofer") || cardHtml.includes('data-act="commission-step"')) {
    fail("complete driver test must not wrap back to woofer", { cardHtml });
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
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

  const html = harness.elements.get("view-body").innerHTML;
  const cardHtml = commissionCardHtml(html);
  if (cardHtml.includes("Complete") || cardHtml.includes("All drivers are confirmed")) {
    fail("stale ramp roles without measurement-backed checks must not complete the card", { cardHtml });
  }
  if (!cardHtml.includes("next: Woofer") || !cardHtml.includes('data-act="commission-step"')) {
    fail("stale ramp roles should restart visible driver checks from woofer", { cardHtml });
  }
  return { staleRampConfirmationsDoNotCompleteDriverChecks: true };
}

async function testDriverCaptureAfterRampConfirmationPostsMicPayload() {
  const confirmedTopology = activeTwoWayTopologyPayload();
  let measurements = {
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
  const capturePosts = [];
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
    "./active-speaker/driver-capture": (_path, options = {}) => {
      const body = JSON.parse(options.body || "{}");
      capturePosts.push(body);
      measurements = {
        ...measurements,
        summary: {
          ...measurements.summary,
          captured_driver_count: 1,
          latest_driver_measurements: {
            "main:woofer": { captured: true, outcome: "heard_correct_driver" },
          },
        },
      };
      return Promise.resolve(response({
        recorded: true,
        verdict: "present",
        outcome: "heard_correct_driver",
        measurement: measurements,
      }));
    },
  });
  const harness = setupHarness(fetchHandler);
  await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();
  const originalSetTimeout = globalThis.window.setTimeout;
  globalThis.window.setTimeout = (fn) => { queueMicrotask(fn); return 0; };
  try {
    let html = harness.elements.get("view-body").innerHTML;
    if (!html.includes('data-act="record-driver-capture"')) {
      fail("confirmed ramp should expose driver mic capture", { html });
    }
    if (html.includes("not yet wired")) {
      fail("driver capture follow-up should not claim measurement is unwired", { html });
    }

    harness.dispatchClick({
      "data-act": "record-driver-capture",
      "data-group-id": "main",
      "data-role": "woofer",
      "data-playback-id": "pb-woofer",
    });
    await harness.flush(); await harness.flush(); await harness.flush(); await harness.flush();

    if (capturePosts.length !== 1) fail("driver capture should POST once", { capturePosts });
    const body = capturePosts[0];
    if (body.speaker_group_id !== "main" || body.role !== "woofer" || body.playback_id !== "pb-woofer") {
      fail("driver capture should bind to the accepted floor result", { body });
    }
    if (!body.capture || !body.capture.wav_base64) {
      fail("driver capture should upload a WAV payload", { body });
    }
  } finally {
    globalThis.window.setTimeout = originalSetTimeout;
  }
  return { driverCaptureAfterRampConfirmationPostsMicPayload: true };
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

async function testSummedOffersByEarAndMicValidation() {
  // The combined crossover check is phone-OPTIONAL: a by-ear "Blend sounds right"
  // (operator_listening_check) and a mic capture are BOTH offered. The by-ear
  // positive is still gated on an audible combined test (you can't certify a
  // blend you didn't hear), and the mic stays available as the more reliable
  // check for a polarity/delay null.
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

  // (2) An audible combined test exists -> by-ear AND mic paths both offered, and
  //     the by-ear positive POSTs an operator listening check (no WAV).
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
      if (!html.includes('data-act="record-summed-capture"')) {
        fail("summed validation should still offer the mic capture", { html });
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
  return { summedOffersByEarAndMicValidation: true };
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
results.push(await testPassiveLayoutsRenderNoActiveDriverTestCard());
results.push(await testActiveCrossoverFirstStepRender());
results.push(await testActiveRouteLimitsRenderedTemplates());
results.push(await testMeasuredDriversOpenProfileStep());
results.push(await testCombinedTestLevelPostsSelectedBoundedLevel());
results.push(await testCompiledProfileApplyBlockStaysUnderstandable());
results.push(await testVisibleCrossoverSettingsWinOverImportedJson());
results.push(await testWorkingSetupSummaryAvoidsStorageCounts());
results.push(await testPreparePreviewUpdatesWorkingSetupFirst());
results.push(await testPreparePreviewWaitsForInFlightWorkingSetupUpdate());
results.push(await testPartialThreeWayWorkingSetupSummaryReadsCleanly());
results.push(await testCommissionCardArmsAndSteps());
results.push(await testCommissionCompleteDoesNotWrapToWoofer());
results.push(await testStaleRampConfirmationsDoNotCompleteDriverChecks());
results.push(await testDriverCaptureAfterRampConfirmationPostsMicPayload());
results.push(await testSummedOffersByEarAndMicValidation());
results.push(await testCommissionPendingStepShowsAckWithoutFloorFlag());
results.push(await testCommissionArmBlockedSurfacesReason());
results.push(await testCommissionActiveGraphBlockSurfacesReason());
results.push(await testCommissionOutputReconcileFailureSurfacesReason());
results.push(await testCommissionToneFailureStopsAutoRamp());
results.push(await testCommissionRampLimitStopsAutoRamp());

console.log(JSON.stringify(Object.assign({ ok: true, results }, liveTabResult)));
