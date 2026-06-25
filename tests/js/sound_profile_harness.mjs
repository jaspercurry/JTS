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
    getElementById(id) {
      if (absent.has(id)) return null;
      if (!elements.has(id)) elements.set(id, makeEl(id));
      return elements.get(id);
    },
    querySelector(sel) {
      return sel === "meta[name=jts-csrf]" ? { content: "csrf-token" } : null;
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
  const dispatchToggle = (attrs) => {
    const target = {
      open: attrs.open !== undefined ? attrs.open : true,
      getAttribute(name) { return attrs[name] || ""; },
      matches() { return false; },
      classList: { contains(name) { return name === "output-step"; } },
    };
    for (const fn of viewBody._listeners.toggle || []) {
      fn({ target });
    }
    return target;
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
    "Sounds right",
    "Back to adjust crossover",
    "Save and apply",
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
  return { combinedTestLevelPostsSelectedBoundedLevel: true };
}

async function testCombinedTestButtonStopsActiveRequest() {
  const start = deferred();
  const stopPosts = [];
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
            body: { speaker_group_id: "main", audio: true, duration_ms: 500 },
          },
        },
      }],
    })),
    "./active-speaker/summed-test": () => start.promise,
    "./active-speaker/summed-test/stop": (_path, options = {}) => {
      stopPosts.push(JSON.parse(options.body || "{}"));
      return Promise.resolve(response({ status: "stopped", reason: "operator_stop" }));
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
    let html = harness.elements.get("view-body").innerHTML;
    if (!html.includes('data-act="stop-summed-test"') || !html.includes("btn--danger")) {
      fail("combined test should turn into a fixed red Stop action while active", { html });
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

async function testTwoOutputChannelSelectorAutoAssignsPeerOnSave() {
  const topology = activeTwoWayTopologyPayload();
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
  if (!dirtyHtml.includes("Save channel assignments")) {
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
    "saved for review",
    "cannot switch normal playback to it from here yet",
    "Save profile",
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
  if (!html.includes(">Play Woofer</button>")) fail("idle card should expose Play Woofer", { html });

  // Play silently opens the quiet driver setup, then begins the
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
  for (const expected of ["Stop", "I hear the woofer", "Back to configuration"]) {
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
    fail("Stop must stay enabled while the automatic ramp is active", { cardHtml });
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
  for (const expected of ["Stop", "I hear the woofer", "Back to configuration"]) {
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
  // expanded (not tucked behind the solo box's "Advanced speaker setup" disclosure).
  for (const expected of ["Active crossover setup", "Test each driver"]) {
    if (!html.includes(expected)) {
      fail("follower /sound/ should render the local active-speaker UI", { expected, html });
    }
  }
  for (const forbidden of [
    "Create custom profile",
    "Try a stock profile",
    "data-act=\"new-draft\"",
    "Advanced speaker setup",
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
  // The existing disabled add affordance stays — the CTA is additive guidance.
  if (!html.includes('data-act="toggle-output-subwoofer"')) {
    fail("the local-subwoofer add affordance must remain in the dead-end branch", { html });
  }
  if (!html.includes('href="/rooms/"')) {
    fail("dead-end subwoofer card should link to the Speakers page", { html });
  }
  if (!html.includes("add a wireless subwoofer on the Speakers page")) {
    fail("dead-end subwoofer card should offer the wireless-sub CTA copy", { html });
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
  if (html.includes('href="/rooms/"') || html.includes("add a wireless subwoofer on the Speakers page")) {
    fail("a layout with a spare output must not show the wireless-sub CTA", { html });
  }
  return { subwooferWithSpareOutputHidesWirelessCta: true };
}

const liveTabResult = await testLiveTabReplay();
results.push(liveTabResult);
results.push(await testQuietTestSurfaceSurvivesStartupActions());
results.push(await testPassiveLayoutsRenderNoActiveDriverTestCard());
results.push(await testActiveCrossoverFirstStepRender());
results.push(await testActiveRouteLimitsRenderedTemplates());
results.push(await testMeasuredDriversOpenProfileStep());
results.push(await testCombinedTestLevelPostsSelectedBoundedLevel());
results.push(await testCombinedTestButtonStopsActiveRequest());
results.push(await testTwoOutputChannelSelectorAutoAssignsPeerOnSave());
results.push(await testChannelSelectorKeepsConfirmOutputsOpenWhenDraftDirty());
results.push(await testThreeOutputChannelSelectorDoesNotAutoAssignPeers());
results.push(await testCompiledProfileApplyBlockStaysUnderstandable());
results.push(await testVisibleCrossoverSettingsWinOverImportedJson());
results.push(await testDriverResearchNotesCapExplainsBeforePost());
results.push(await testWorkingSetupSummaryAvoidsStorageCounts());
results.push(await testPreparePreviewUpdatesWorkingSetupFirst());
results.push(await testPreparePreviewWaitsForInFlightWorkingSetupUpdate());
results.push(await testPartialThreeWayWorkingSetupSummaryReadsCleanly());
results.push(await testCommissionCardArmsAndSteps());
results.push(await testCommissionCompleteDoesNotWrapToWoofer());
results.push(await testStaleRampConfirmationsDoNotCompleteDriverChecks());
results.push(await testDriverMicCaptureIsRemovedFromSoundFlow());
results.push(await testSummedByEarValidationExcludesMicCapture());
results.push(await testSummedValidationRefreshesBaselineProfileState());
results.push(await testCommissionPendingStepShowsAckWithoutFloorFlag());
results.push(await testCommissionArmBlockedSurfacesReason());
results.push(await testCommissionActiveGraphBlockSurfacesReason());
results.push(await testCommissionOutputReconcileFailureSurfacesReason());
results.push(await testCommissionToneFailureStopsAutoRamp());
results.push(await testCommissionRampLimitStopsAutoRamp());
results.push(await testResetPartialCleanupSurfacesWarning());
results.push(await testFollowerModeRendersLocalDriverUi());
results.push(await testFollowerModeSafeFallbackOnMalformedIsland());
results.push(await testSubwooferDeadEndOffersWirelessCta());
results.push(await testSubwooferWithSpareOutputHidesWirelessCta());

console.log(JSON.stringify(Object.assign({ ok: true, results }, liveTabResult)));
