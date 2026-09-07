// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Render harness for /wifi/ (deploy/assets/wifi/js/main.js). Loads the real
// module (dom.js/escape.js run for real; only the network-touching
// http.js/dialog.js imports are stubbed), drives its exported
// fetchState()/rescan() with fixture /state and /scan payloads, and asserts
// the resulting DOM *structure* (tags, classes, ids, data-* attrs, child
// counts) — never copy/prose, which changes for reasons unrelated to
// behavior.
//
//   node tests/js/wifi_render_harness.mjs [path/to/main.js]

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { loadEsm, repoPath } from "./_loader.mjs";
import { cssIdSafe } from "../../deploy/assets/shared/js/escape.js";

// ---- minimal DOM stub, just enough for the real dom.js h()/appendChildren
// to build against and for main.js's own classList/checked/hidden/dataset
// reads to work. document.getElementById is a live id->element registry
// (mirrors a real DOM): every element auto-registers when h()'s id
// shorthand assigns el.id, whether it is one of the fixed containers below
// or a row main.js rebuilds on every render.
const BY_ID = new Map();

class ClassList {
  constructor() { this._values = new Set(); }
  add(...names) { for (const n of names) this._values.add(n); }
  remove(...names) { for (const n of names) this._values.delete(n); }
  toggle(name, force) {
    const on = force === undefined ? !this._values.has(name) : !!force;
    if (on) this._values.add(name); else this._values.delete(name);
  }
  contains(name) { return this._values.has(name); }
}

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.classList = new ClassList();
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.type = "";
    this._id = "";
    this._className = "";
    this._text = "";
  }
  get id() { return this._id; }
  set id(v) { this._id = v; if (v) BY_ID.set(v, this); }
  get className() { return this._className; }
  set className(v) {
    this._className = v || "";
    this.classList = new ClassList();
    this.classList.add(...this._className.split(" ").filter(Boolean));
  }
  get textContent() {
    return this.children.length
      ? this.children.map((c) => c.textContent || "").join("")
      : this._text;
  }
  set textContent(v) { this._text = v == null ? "" : String(v); this.children = []; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...nodes) { this.children = nodes.filter((n) => n != null); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) {
    return Object.prototype.hasOwnProperty.call(this.attributes, key)
      ? this.attributes[key] : null;
  }
  addEventListener() {}
}

globalThis.Node = El;
globalThis.document = {
  visibilityState: "visible",
  createElement: (tag) => new El(tag),
  createElementNS: (_ns, tag) => new El(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  getElementById: (id) => BY_ID.get(id) || null,
  addEventListener() {},
};

function fixed(id, tag = "div") {
  const el = new El(tag);
  el.id = id;
  return el;
}

const elements = {
  current: fixed("current"),
  savedList: fixed("saved-list"),
  savedCount: fixed("saved-count", "span"),
  scanHealth: fixed("scan-health"),
  scanBtn: fixed("scan-btn", "button"),
  availList: fixed("avail-list"),
};

function findAll(node, pred, out = []) {
  if (!node || !Array.isArray(node.children)) return out;
  if (pred(node)) out.push(node);
  for (const child of node.children) findAll(child, pred, out);
  return out;
}
function hasClass(node, cls) { return !!(node && node.classList && node.classList.contains(cls)); }

// ---- load the real module ----
// jsonHeaders/jtsConfirm/jtsAlert are network/dialog-touching; every path
// this harness exercises (fetchState, rescan with radioOn already true)
// either never reaches them or only needs a harmless stand-in.
let fixtureState = null;
let fixtureScan = null;
// Set by the connect-failure and state-fetch-failure cases below; both
// reset their flag once done so they don't leak into a later case.
let connectResponse = null;
let stateFetchShouldThrow = false;
globalThis.fetch = async (url) => {
  if (url === "./state") {
    if (stateFetchShouldThrow) throw new Error("network down");
    return { ok: true, json: async () => fixtureState };
  }
  if (url === "./scan") return { ok: true, json: async () => fixtureScan };
  if (url === "./connect") return { ok: true, json: async () => connectResponse || {} };
  return { ok: true, json: async () => ({}) };
};

const domUrl = pathToFileURL(repoPath("deploy/assets/shared/js/dom.js")).href;
const escapeUrl = pathToFileURL(repoPath("deploy/assets/shared/js/escape.js")).href;
const modulePath = process.argv[2] || repoPath("deploy/assets/wifi/js/main.js");

const { fetchState, rescan, openConnect, submitConnect } = await loadEsm(modulePath, {
  rewrite: [
    [/^import \{ jsonHeaders, startPolling \} from "\/assets\/shared\/js\/http\.js";\n/m, ""],
    [/^import \{ jtsConfirm, jtsAlert \} from "\/assets\/shared\/js\/dialog\.js";\n/m, ""],
    [/"\/assets\/shared\/js\/escape\.js"/, `"${escapeUrl}"`],
    [/"\/assets\/shared\/js\/dom\.js"/, `"${domUrl}"`],
  ],
  prelude:
    "const jsonHeaders = () => ({});\n" +
    "const jtsConfirm = async () => true;\n" +
    "const jtsAlert = async () => {};\n",
  // The delegated click handler + startPolling(fetchState, ...) boot call —
  // this harness calls fetchState()/rescan() directly and never wants the
  // module reaching for a real network timer.
  truncateBefore: "\n// Bootstrap",
  exportNames: ["fetchState", "rescan", "openConnect", "submitConnect"],
});

// ---- current-network card + radio toggle, parametrized over states ----
const CURRENT_CASES = [
  {
    name: "no-adapter",
    state: {
      adapterPresent: false, radioOn: false, hasEthernet: false,
      lockoutRisk: "low", current: null, saved: [],
    },
    disconnected: true, radioToggle: false,
  },
  {
    name: "radio-off-not-connected",
    state: {
      adapterPresent: true, radioOn: false, hasEthernet: true,
      lockoutRisk: "low", current: null, saved: [],
    },
    disconnected: true, radioToggle: true, checked: false,
  },
  {
    name: "connected",
    state: {
      adapterPresent: true, radioOn: true, hasEthernet: false,
      lockoutRisk: "high",
      current: {
        ssid: "HomeNet", ip: "192.168.1.50", security: "WPA2",
        signal: 80, profileName: "home-profile",
      },
      saved: [],
    },
    disconnected: false, radioToggle: true, checked: true, metaRows: 3,
  },
];

for (const c of CURRENT_CASES) {
  fixtureState = c.state;
  await fetchState();
  const card = elements.current.children[0];
  assert.ok(card, `${c.name}: current card rendered`);
  assert.equal(hasClass(card, "current-card"), true, `${c.name}: .current-card`);
  assert.equal(hasClass(card, "disconnected"), c.disconnected, `${c.name}: .disconnected`);

  const radioToggle = document.getElementById("radio-toggle");
  if (c.radioToggle) {
    assert.ok(radioToggle, `${c.name}: radio toggle exists`);
    assert.equal(radioToggle.type, "checkbox", `${c.name}: radio toggle is a checkbox`);
    assert.equal(radioToggle.checked, c.checked, `${c.name}: radio toggle checked state`);
  } else {
    assert.equal(radioToggle, null, `${c.name}: no radio toggle without an adapter`);
  }

  if (c.metaRows != null) {
    const [meta] = findAll(card, (n) => hasClass(n, "meta"));
    assert.ok(meta, `${c.name}: .meta present`);
    const rows = meta.children.filter((n) => hasClass(n, "row"));
    assert.equal(rows.length, c.metaRows, `${c.name}: meta row count`);
    for (const row of rows) {
      assert.equal(findAll(row, (n) => hasClass(n, "key")).length, 1, `${c.name}: row has a .key`);
      assert.equal(findAll(row, (n) => hasClass(n, "val")).length, 1, `${c.name}: row has a .val`);
    }
  }
}

// ---- saved networks list ----
const SAVED_CASES = [
  { name: "no-saved-networks", saved: [], current: null, rowCount: 0, empty: true },
  {
    name: "current-plus-other",
    saved: [
      { name: "home-profile", ssid: "HomeNet" },
      { name: "guest-profile", ssid: "GuestNet" },
    ],
    current: { profileName: "home-profile" },
    rowCount: 2,
  },
];

for (const c of SAVED_CASES) {
  fixtureState = {
    adapterPresent: true, radioOn: true, hasEthernet: true,
    lockoutRisk: "low", current: c.current, saved: c.saved,
  };
  await fetchState();
  const rows = elements.savedList.children.filter((n) => hasClass(n, "net-row"));
  assert.equal(rows.length, c.rowCount, `${c.name}: saved row count`);
  if (c.empty) {
    assert.equal(elements.savedList.children.length, 1, `${c.name}: one empty placeholder`);
    assert.ok(hasClass(elements.savedList.children[0], "empty"), `${c.name}: placeholder is .empty`);
  }
  for (const p of c.saved) {
    const row = rows.find((r) => r.id === `sv-${cssIdSafe(p.name)}`);
    assert.ok(row, `${c.name}: row for ${p.name} exists`);
    const isCurrentRow = !!(c.current && c.current.profileName === p.name);
    const badges = findAll(row, (n) => hasClass(n, "badge") && hasClass(n, "badge--ok"));
    assert.equal(badges.length > 0, isCurrentRow, `${c.name}: ${p.name} "in use" badge matches current-ness`);
    const [forgetBtn] = findAll(row, (n) => n.tagName === "button" && hasClass(n, "btn--danger"));
    assert.ok(forgetBtn, `${c.name}: ${p.name} has a Forget button`);
    assert.equal(forgetBtn.getAttribute("data-action"), "open-forget", `${c.name}: Forget data-action`);
    assert.equal(forgetBtn.getAttribute("data-name"), p.name, `${c.name}: Forget data-name`);
  }
}

// ---- scan health notice + the Scan button's own hide/show ----
const SCAN_HEALTH_CASES = [
  { name: "clean", scan: { degraded: false, suspect: false, hideScanButton: false, debug: {} }, note: null },
  {
    name: "degraded",
    scan: { degraded: true, reason: "x", suspect: false, hideScanButton: false, debug: {} },
    note: ["scan-note", "warn"],
  },
  {
    name: "suspect-only",
    scan: { degraded: false, suspect: true, hideScanButton: false, debug: {} },
    note: ["scan-note"],
  },
  {
    name: "hides-scan-button",
    scan: { degraded: false, suspect: false, hideScanButton: true, debug: {} },
    note: null, buttonHidden: true,
  },
];

// rescan() no-ops unless the module's internal state already has radioOn —
// set that once via fetchState before driving the scan-only cases below.
fixtureState = {
  adapterPresent: true, radioOn: true, hasEthernet: true,
  lockoutRisk: "low", current: null, saved: [],
};
await fetchState();

for (const c of SCAN_HEALTH_CASES) {
  fixtureScan = {
    networks: [{ ssid: "N", secured: false, channel: 1, signal: 50, inUse: false }],
    scan: c.scan,
  };
  await rescan();
  const notes = elements.scanHealth.children;
  if (c.note) {
    assert.equal(notes.length, 1, `${c.name}: exactly one health note`);
    for (const cls of c.note) {
      assert.ok(hasClass(notes[0], cls), `${c.name}: note carries .${cls}`);
    }
  } else {
    assert.equal(notes.length, 0, `${c.name}: no health note`);
  }
  assert.equal(elements.scanBtn.hidden, !!c.buttonHidden, `${c.name}: scan button hidden state`);
  assert.equal(elements.scanBtn.children.length, 0, `${c.name}: scan button spinner cleared after scan`);
}

// ---- available-networks list ----
const AVAIL_CASES = [
  { name: "no-results", networks: [], rowCount: 0, emptyClass: "empty" },
  {
    name: "open-and-secured",
    networks: [
      { ssid: "OpenNet", secured: false, channel: 6, signal: 40, inUse: false },
      { ssid: "HomeNet", secured: true, channel: 11, signal: 90, inUse: true },
    ],
    rowCount: 2,
  },
];

for (const c of AVAIL_CASES) {
  fixtureScan = {
    networks: c.networks,
    scan: { degraded: false, suspect: false, hideScanButton: false, debug: {} },
  };
  await rescan();
  const rows = elements.availList.children.filter((n) => hasClass(n, "net-row"));
  assert.equal(rows.length, c.rowCount, `${c.name}: avail row count`);
  if (c.rowCount === 0) {
    assert.equal(elements.availList.children.length, 1, `${c.name}: one empty placeholder`);
    assert.ok(hasClass(elements.availList.children[0], c.emptyClass), `${c.name}: placeholder class`);
  }
  for (const n of c.networks) {
    const row = rows.find((r) => r.id === `av-${cssIdSafe(n.ssid)}`);
    assert.ok(row, `${c.name}: row for ${n.ssid} exists`);
    const [head] = findAll(row, (r) => hasClass(r, "head"));
    assert.ok(head, `${c.name}: ${n.ssid} has a .head`);
    assert.equal(head.getAttribute("data-action"), "open-connect", `${c.name}: head data-action`);
    assert.equal(head.getAttribute("data-ssid"), n.ssid, `${c.name}: head data-ssid`);
    const badges = findAll(row, (r) => hasClass(r, "badge") && hasClass(r, "badge--ok"));
    assert.equal(badges.length > 0, !!n.inUse, `${c.name}: ${n.ssid} "Connected" badge matches inUse`);
  }
}

// ---- connect failure renders errorPanel() ----
const CONNECT_FAILURE_CASES = [
  { name: "server-rejects-connect", response: { ok: false, message: "nope" } },
];

for (const c of CONNECT_FAILURE_CASES) {
  fixtureState = {
    adapterPresent: true, radioOn: true, hasEthernet: true,
    lockoutRisk: "low", current: null, saved: [],
  };
  await fetchState();
  fixtureScan = {
    networks: [{ ssid: "OpenNet", secured: false, channel: 6, signal: 40, inUse: false }],
    scan: { degraded: false, suspect: false, hideScanButton: false, debug: {} },
  };
  await rescan();
  openConnect("OpenNet");
  connectResponse = c.response;
  await submitConnect("OpenNet", false);
  connectResponse = null;

  const slot = document.getElementById(`av-panel-${cssIdSafe("OpenNet")}`);
  const [panel] = slot.children;
  assert.ok(panel, `${c.name}: a panel is rendered`);
  assert.equal(hasClass(panel, "panel"), true, `${c.name}: .panel wrapper`);
  const [errDiv] = findAll(panel, (n) => hasClass(n, "result") && hasClass(n, "err"));
  assert.ok(errDiv, `${c.name}: .result.err present`);
  const [dismissBtn] = findAll(panel, (n) => n.tagName === "button" && hasClass(n, "btn--ghost"));
  assert.ok(dismissBtn, `${c.name}: Dismiss button present`);
  assert.equal(dismissBtn.getAttribute("data-action"), "dismiss-connect", `${c.name}: Dismiss data-action`);
  assert.equal(dismissBtn.getAttribute("data-ssid"), "OpenNet", `${c.name}: Dismiss data-ssid`);
}

// ---- fetchState's network-error card ----
{
  stateFetchShouldThrow = true;
  await fetchState();
  stateFetchShouldThrow = false;

  const card = elements.current.children[0];
  assert.ok(card, "state-fetch-failure: a card is rendered");
  assert.equal(hasClass(card, "current-card"), true, "state-fetch-failure: .current-card");
  assert.equal(hasClass(card, "disconnected"), true, "state-fetch-failure: .disconnected");
  assert.equal(findAll(card, (n) => hasClass(n, "ssid")).length, 1, "state-fetch-failure: .ssid present");
  assert.equal(findAll(card, (n) => hasClass(n, "meta")).length, 1, "state-fetch-failure: .meta present");
}

console.log(JSON.stringify({ ok: true }));
