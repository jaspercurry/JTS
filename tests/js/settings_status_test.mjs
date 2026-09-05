// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Behaviour pins for deploy/assets/shared/js/settings-status.js — the module
// the landing page and (docs/web-ia.md §3) the area hubs share. What must
// hold: gating runs off the baked capability map synchronously — the layout
// owes the network nothing — and fails closed on a cap the map does not
// grant; one /system/data.json snapshot then fills the status-* sublabels
// without ever re-driving that layout.
//
// startPolling()'s own scheduling — the cadence, the hidden-tab backoff,
// stop() — is polling_test.mjs's subject, so this only pins the interval this
// module asks for. Run via tests/test_web_http_helper.py.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { FakeElement, installFixedDocument } from "./_dom.mjs";
import { loadEsm, repoPath } from "./_loader.mjs";
import { runTestFunctions } from "./run_test_functions.mjs";

// Enough of a timer to let startPolling schedule; the delay it asks for is
// the only scheduling fact this module owns.
const delays = [];
globalThis.setTimeout = (fn, delay) => { delays.push(delay); return 0; };
globalThis.clearTimeout = () => {};

function gatedRow(cap) {
  const row = new FakeElement("a");
  if (cap) row.setAttribute("data-requires", cap);
  return row;
}

const rows = [gatedRow("pair_management"), gatedRow("local_sources"), gatedRow(null)];
const [pairRow, sourcesRow, ungatedRow] = rows;

const STATUS_IDS = [
  "status-speaker-name",
  "status-voice",
  "status-ha",
  "status-software",
  "status-system",
  "system-summary",
];

let snapshot = {};
let fetched = [];
globalThis.fetch = async (url, options) => {
  fetched.push({ url, cache: options && options.cache });
  return { ok: true, json: async () => snapshot };
};

const status = installFixedDocument(STATUS_IDS, {
  title: "JTS",
  removeEventListener() {},
  querySelectorAll: () => rows,
});
const text = (id) => status.get(id).textContent;

// The real http.js is loaded (real getJSON, real startPolling) by pointing
// the browser-absolute specifier at the file on disk.
const { initSettingsStatus } = await loadEsm(
  repoPath("deploy/assets/shared/js/settings-status.js"),
  {
    rewrite: [[
      '"/assets/shared/js/http.js"',
      JSON.stringify(
        pathToFileURL(repoPath("deploy/assets/shared/js/http.js")).href,
      ),
    ]],
  },
);

// Let the immediate first tick settle (getJSON + render are async).
async function settle() {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

function start(caps, extra = {}) {
  fetched = [];
  delays.length = 0;
  rows.forEach((row) => { row.hidden = false; });
  return initSettingsStatus(caps === undefined ? extra : { caps, ...extra });
}

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

async function gating_is_applied_synchronously_on_return() {
  snapshot = {};
  const stop = start({ pair_management: true });

  // Asserted before the in-flight snapshot resolves: the layout owes the
  // network nothing.
  check(pairRow.hidden === false, "a granted row is revealed");
  check(sourcesRow.hidden === true, "an ungranted row stays hidden");
  check(ungatedRow.hidden === false, "an element with no cap name is untouched");
  check(typeof stop === "function", "the caller gets a stop()");
  await settle();
  stop();
}

async function gating_fails_closed_without_a_capability_map() {
  snapshot = {};
  const stop = start(undefined);

  check(pairRow.hidden === true, "no caps means a gated row stays hidden");
  check(sourcesRow.hidden === true, "...for every gated row");
  await settle();
  stop();
}

async function a_snapshot_fills_the_status_sublabels() {
  snapshot = {
    speaker_name: { name: "Kitchen" },
    voice_provider: "openai",
    home_assistant: { configured: true, connected: true, instance_name: "Home" },
    build: { JASPER_GIT_SHA: "abcdef1234567", JASPER_GIT_BRANCH: "main" },
    metrics: {
      current: { per_core_cpu_pct: [10, 30], temp_c: 44.4, disk_used_pct: 12.6 },
    },
  };
  const stop = start(
    { pair_management: true }, { titleFollowsSpeakerName: true },
  );
  await settle();

  check(fetched.length === 1, "one snapshot request per tick");
  check(fetched[0].url === "/system/data.json", "the shared snapshot endpoint");
  check(fetched[0].cache === "no-store", "never served from cache");
  check(delays[0] === 20000, "and scheduled on the 20 s settings cadence");
  check(text("status-speaker-name") === "Kitchen", "speaker name");
  check(document.title === "Kitchen", "the tab takes the speaker's name");
  check(text("status-voice") === "OpenAI", "provider is title-cased");
  check(text("status-ha") === "Home", "a connected HA shows its instance");
  check(text("status-software") === "abcdef1 · main", "short sha + branch");
  check(text("status-system") === "20% CPU · 44 C · 13% disk", "metrics summary");
  check(text("system-summary") === "20% CPU · 44 C · 13% disk", "footer summary");
  check(pairRow.hidden === false, "the snapshot refreshes values, never layout");
  stop();
}

async function a_thin_snapshot_leaves_the_rendered_sublabels_alone() {
  snapshot = { home_assistant: { configured: false }, build: {}, metrics: {} };
  const stop = start({});
  await settle();

  check(text("status-ha") === "Not connected", "an unconfigured HA says so");
  check(text("status-speaker-name") === "Kitchen", "an absent field keeps its value");
  check(text("status-software") === "abcdef1 · main", "...including an empty build");
  stop();
}

// A hub's <title> is its own name (docs/web-ia.md §2), so the speaker name
// must not claim the tab unless the page opted in.
async function the_tab_title_is_left_alone_unless_the_page_asks() {
  snapshot = { speaker_name: { name: "Hallway" } };
  document.title = "Sound";
  const stop = start({});
  await settle();

  check(document.title === "Sound", "the page keeps its own title by default");
  check(text("status-speaker-name") === "Hallway", "the sublabel still updates");
  stop();
}

await runTestFunctions(
  [
    gating_is_applied_synchronously_on_return,
    gating_fails_closed_without_a_capability_map,
    a_snapshot_fills_the_status_sublabels,
    a_thin_snapshot_leaves_the_rendered_sublabels_alone,
    the_tab_title_is_left_alone_unless_the_page_asks,
  ],
  () => passed,
);
