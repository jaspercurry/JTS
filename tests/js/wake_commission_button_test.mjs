// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The chip-AEC alignment button is an affordance of the array, not of a
// reconciler remedy: it shows whenever the mic view model reports chip-AEC
// capability. Clicking it opens the calibration-tone confirm first, so a
// dismissed dialog must leave the run unstarted.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { element, installFixedDocument } from "./_dom.mjs";

const nodes = new Map();
const node = (id) => {
  if (!nodes.has(id)) nodes.set(id, element(id));
  return nodes.get(id);
};
installFixedDocument([], {
  getElementById: node,
  querySelectorAll: () => [],
});

let now = 1000;
Date.now = () => now;
const timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };

let confirmAnswer = true;
let confirmCalls = 0;
const posts = [];
globalThis.__jtsConfirm = async () => {
  confirmCalls += 1;
  return confirmAnswer;
};
const alerts = [];
globalThis.__jtsAlert = async (message) => { alerts.push(message); };
let getCalls = 0;
globalThis.__getJSON = async (path) => {
  assert.equal(path, "detection.json"); getCalls++; return {};
};
let response = () => ({});
globalThis.__postJSON = async (path, body) => {
  posts.push({ path, body });
  return response();
};

const { applyProfileStatus, applyState, postProfile, postLayer, postUsbMic, pollDetection } = await loadEsm(
  repoPath("deploy/assets/wake/js/main.js"),
  {
    stripImports: true,
    guardNoImports: true,
    prelude: aliasGlobals(["jtsConfirm", "jtsAlert", "getJSON", "postJSON"]),
    truncateBefore: "\n// Model-picker form:",
    exportNames: ["applyProfileStatus", "applyState", "postProfile", "postLayer", "postUsbMic", "pollDetection"],
  },
);

const button = node("echo-commission-button");
const detail = node("echo-commission-detail");

const view = (mic, commission = {}) => ({
  mic_settings: { mic, echo: {} },
  commission,
});

// --- visibility follows the hardware fact, not a recommended remedy --------
applyProfileStatus(view({ chip_aec_capable: true }));
assert.equal(button.hidden, false);
applyProfileStatus(view({ chip_aec_capable: false }));
assert.equal(button.hidden, true);
applyProfileStatus(view({}));
assert.equal(button.hidden, true);

// --- progress + verdict lines ---------------------------------------------
const lineFor = (commission) => {
  applyProfileStatus(view({ chip_aec_capable: true }, commission));
  return detail.hidden ? "" : detail.textContent;
};

// The three states the control layer publishes each say something, and each
// says something different — the copy itself is not the contract.
const lines = [
  lineFor({ running: true }),
  lineFor({ state: "passed" }),
  lineFor({ state: "failed", detail: "no reference" }),
];
assert.equal(lines.filter(Boolean).length, 3);
assert.equal(new Set(lines).size, 3);
assert.equal(button.disabled, false);

// A failure carries the backend's own reason rather than a fixed string.
assert.match(lines[2], /no reference/);

applyProfileStatus(view({ chip_aec_capable: true }, { running: true }));
assert.equal(button.disabled, true);

assert.equal(lineFor({}), "");

// A box with no capable array shows neither the button nor a verdict about
// a run it cannot start.
applyProfileStatus(view({ chip_aec_capable: false }, { state: "passed" }));
assert.equal(detail.hidden, true);

// --- a dismissed confirm starts no run ------------------------------------
confirmAnswer = false;
await button.click();
assert.equal(confirmCalls, 1);
assert.deepEqual(posts, []);

confirmAnswer = true;
await button.click();
assert.equal(confirmCalls, 2);
assert.equal(posts.length, 1);
assert.equal(posts[0].path, "commission");

const cases = [
  ["profile", { profile: "direct_mic" }, () => postProfile("direct_mic"), "profile"],
  ["layer/raw", { enabled: true }, () => postLayer("raw", true), "layer-raw"],
  ["usb-mic", { enabled: true }, () => postUsbMic(true), "usb-mic-toggle"],
  ["sensitivity", { value: 0.7 }, () => node("sensitivity-save").click(), "sensitivity-save"],
];
for (const [path, body, act, id] of cases) {
  for (const fails of [false, true]) {
    let resolve, reject;
    response = () => new Promise((yes, no) => { resolve = yes; reject = no; });
    node("sensitivity-input").value = "0.70";
    node("sensitivity-save").classList.add("is-dirty");
    node(id).checked = true;
    const alertCount = alerts.length;
    const task = act();
    assert.deepEqual(posts.at(-1), { path, body });
    if (path === "usb-mic" || path === "sensitivity") assert.equal(node(id).disabled, true);
    if (path === "profile" || path === "layer/raw") {
      const before = getCalls;
      await pollDetection();
      assert.equal(getCalls, before);
    }
    applyState({});
    if (path === "layer/raw" || path === "usb-mic") assert.equal(node(id).checked, true);
    if (fails) reject(new Error("test failure"));
    else resolve({ mic_settings: { fusion: { toggles: [{ id: "raw", checked: true, enabled: true }] } },
      usb_mic: { enabled: true, toggle_enabled: true } });
    await task;
    assert.equal(alerts.length, alertCount + Number(fails));
    if (path === "layer/raw" || path === "usb-mic") assert.equal(node(id).checked, !fails);
    if (path === "sensitivity") assert.equal(node(id).classList.contains("is-dirty"), fails);
  }
}
now += 3000;
await pollDetection();
assert.equal(getCalls, 1);
assert.ok(timers.some(({ ms }) => ms === 250));
assert.ok(timers.some(({ ms }) => ms === 500));
console.log(JSON.stringify({ ok: true, requests: cases.length * 2 }));
