// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Issue #2292. Undo restores two things through two different writers: the DSP
// graph, and the crossover `/sound` declares. The graph half can succeed while
// the declaration half honestly refuses (somebody edited Sound after the
// crossover was applied), and that combination returns HTTP 200 — so the
// wizard's success branch is the ONLY place the household can learn about it.
// Before this the branch said "Updated." unconditionally and the refusal lived
// in the journal alone, which is the same silence the issue was filed about,
// one layer up.
//
// Drives the real `runAction` against a 200 response and asserts:
//   1. a `sound_declaration_message` is shown verbatim, in the `bad` tone;
//   2. it SURVIVES the refresh that follows. This is the shape the first
//      version of this harness missed: `relay: null` is the one envelope where
//      nothing re-writes the status line, so it cannot see the clobber.
//      renderRelay's terminal branch calls setStatus('Capture complete.') on a
//      completed relay — and after an Undo the post-apply VERIFY's relay is
//      sitting there complete, so that is the DOMINANT case, not an edge one;
//   3. an ordinary success still says "Updated." in the `ok` tone;
//   4. a relay-starting action still says its own copy — the caveat branch
//      does not swallow the sibling message it sits beside.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { elementWithLiveText as element, installFixedDocument } from "./_dom.mjs";

const ids = [
  "crossover-verdict",
  "crossover-applied",
  "crossover-start-over",
  "crossover-steps",
  "crossover-nudges",
  "crossover-review",
  "crossover-review-body",
  "crossover-cloud",
  "crossover-cloud-provenance",
  "crossover-cloud-chart",
  "crossover-cloud-geometry",
  "crossover-cloud-callouts",
  "crossover-cloud-pending",
  "crossover-chart-legend-measure",
  "crossover-chart-legend-verify",
  "crossover-chart-legend-corridor",
  "crossover-chart-legend-excluded",
  "crossover-action",
  "crossover-relay",
  "crossover-walk",
  "crossover-walk-progress",
  "crossover-walk-headline",
  "crossover-walk-detail",
  "crossover-walk-action",
  "crossover-relay-status",
  "crossover-relay-link",
  "crossover-relay-qr",
  "crossover-relay-stop",
  "capture-status",
];
const elements = installFixedDocument(ids, {
  factory: element,
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
});
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.window = { addEventListener() {} };

const baseEnvelope = {
  verdict_text: "",
  steps: [],
  nudges: [],
  relay: null,
  next_action: null,
  alternate_actions: [],
};

// What `refresh()` fetches and renders AFTER the mutation returns. The
// terminal-relay envelope is the post-Undo reality: the deferred VERIFY that
// auto-armed on the apply left a completed relay behind.
let refreshEnvelope = { ...baseEnvelope };
globalThis.__getJSON = async () => refreshEnvelope;
globalThis.__renderRelayQr = () => {};
globalThis.__jtsConfirm = async () => true;
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

let postResponse = { ...baseEnvelope };
globalThis.__postJSON = async () => postResponse;

const { render, runAction } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "renderRelayQr", "jtsConfirm", "renderCloud", "redrawCloudChart",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render", "runAction"],
  },
);

const statusEl = elements.get("capture-status");
render({ ...baseEnvelope });

const UNDO = {
  id: "verify_undo",
  label: "Undo (restore previous sound)",
  endpoint: "/correction/crossover/v2/restore",
  body: {},
};

// The server's own copy for declaration_refused_sound_moved.
const CAVEAT =
  "The previous sound is back, but Sound still declares 2250 Hz: the speaker " +
  "design changed after this crossover was applied, so JTS left it alone. " +
  "Set the crossover back to 2500 Hz in Sound settings if you want it.";

const UNDO_WITH_CAVEAT = {
  ...baseEnvelope,
  status: "restored",
  sound_declaration: "declaration_refused_sound_moved",
  sound_declaration_message: CAVEAT,
};

// --- 1: the graph came back, the declaration did not, and the screen says so
postResponse = UNDO_WITH_CAVEAT;
await runAction(UNDO, element("btn"));

assert.equal(
  statusEl.textContent,
  CAVEAT,
  `the server's caveat must be shown verbatim, got: ${statusEl.textContent}`,
);
assert.equal(
  statusEl.dataset.tone,
  "bad",
  "a restore the household has to act on is not painted as a clean success",
);

// --- 2: and it survives the refresh, on the envelope an Undo actually meets -
refreshEnvelope = {
  ...baseEnvelope,
  relay: { status: "complete", tap_link: null },
};
postResponse = UNDO_WITH_CAVEAT;
await runAction(UNDO, element("btn"));

assert.equal(
  statusEl.textContent,
  CAVEAT,
  "the caveat must outlive the refresh's own render — got " +
    `${statusEl.textContent}`,
);
assert.equal(
  statusEl.dataset.tone,
  "bad",
  "and keep its tone, not the terminal relay's 'ok'",
);
refreshEnvelope = { ...baseEnvelope };

// --- 3: an ordinary success is untouched ------------------------------------
postResponse = {
  ...baseEnvelope,
  status: "restored",
  sound_declaration: "declaration_restored",
};
await runAction(UNDO, element("btn"));

assert.equal(statusEl.textContent, "Updated.");
assert.equal(
  statusEl.dataset.tone,
  "ok",
  "a fully successful Undo keeps the plain success tone",
);

// --- 4: the relay branch beside it still speaks for itself ------------------
postResponse = { ...baseEnvelope, relay: {url: "https://capture.example/x"} };
await runAction(
  {id: "start", label: "Start", endpoint: "/correction/crossover/v2/session", body: {}},
  element("btn"),
);

assert.equal(statusEl.textContent, "The measurement page is ready.");
assert.equal(statusEl.dataset.tone, "ok");

console.log(JSON.stringify({ ok: true, passed: 8 }));
