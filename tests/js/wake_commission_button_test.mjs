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

globalThis.setTimeout = () => 0;

let confirmAnswer = true;
let confirmCalls = 0;
const posts = [];
globalThis.__jtsConfirm = async () => {
  confirmCalls += 1;
  return confirmAnswer;
};
globalThis.__jtsAlert = async () => {};
globalThis.__jsonHeaders = () => ({});
globalThis.__postJSON = async (path, body) => {
  posts.push({ path, body });
  return {};
};

const { applyProfileStatus } = await loadEsm(
  repoPath("deploy/assets/wake/js/main.js"),
  {
    stripImports: true,
    guardNoImports: true,
    prelude: aliasGlobals(["jtsConfirm", "jtsAlert", "jsonHeaders", "postJSON"]),
    truncateBefore: "\n// Model-picker form:",
    exportNames: ["applyProfileStatus"],
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
applyProfileStatus(view({ chip_aec_capable: true }, { running: true }));
assert.equal(button.disabled, true);
assert.equal(detail.hidden, false);
assert.equal(detail.textContent, "Running…");

applyProfileStatus(
  view({ chip_aec_capable: true }, { running: true, phase: "Playing chirps" }),
);
assert.equal(detail.textContent, "Playing chirps");

applyProfileStatus(view({ chip_aec_capable: true }, { state: "passed" }));
assert.equal(detail.textContent, "Aligned.");

applyProfileStatus(
  view({ chip_aec_capable: true }, { state: "passed", sys_delay: 12, k_samples: 768 }),
);
assert.match(detail.textContent, /12/);
assert.match(detail.textContent, /768/);

applyProfileStatus(
  view({ chip_aec_capable: true }, { state: "failed", detail: "no reference" }),
);
assert.match(detail.textContent, /no reference/);

applyProfileStatus(view({ chip_aec_capable: true }, {}));
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
