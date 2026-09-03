// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The banked-findings line (crossover_envelope_v2._finding_notes — WO-1's read
// half; the first-principles panel's lens C, CC1).
//
// The defect this closes is precisely "the machine writes what it learns and
// nothing ever reads it", so a harness that only pinned the server key would
// reproduce it one layer up: the point of this file is that the sentence
// reaches the DOM. It pins the frontend half — render() must draw one quiet
// `info` row per finding, in the server's exact words, positioned after the
// screen's own nudges and above the collapsed expert numbers, and draw nothing
// at all when the envelope carries none (no heading, no empty state).
//
//   node tests/js/crossover_findings_test.mjs

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, installFixedDocument } from "./_dom.mjs";

// _dom.mjs's element() already sets `tag` to the id/tag it was built with,
// so createElement needs no extra assignment on top of the default factory.
const elements = installFixedDocument(CROSSOVER_IDS);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

globalThis.__getJSON = async () => ({});
globalThis.__postJSON = async () => ({});
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

const { render } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "renderCloud", "redrawCloudChart",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render"],
  },
);

const baseEnvelope = {
  verdict_text: "",
  steps: [],
  nudges: [],
  expert_details: [],
  findings: [],
  relay: null,
  next_action: null,
  alternate_actions: [],
  applied: { state: "none", label: "" },
};
const nudges = elements.get("crossover-nudges");

let passed = 0;
function check(condition, message, context) {
  assert.ok(condition, context ? `${message} — got ${JSON.stringify(context)}` : message);
  passed += 1;
}

const FINDING = {
  text:
    "Two measurements of how this speaker's high and low ranges balance " +
    "disagreed. A further check found the tuning itself lands the two ranges " +
    "level, so it was offered rather than refused, and the disagreement " +
    "recorded.",
};

// --- the acceptance: a banked finding reaches the DOM --------------------
render({ ...baseEnvelope, findings: [FINDING] });
check(nudges.children.length === 1, "a banked finding renders one row", {
  rows: nudges.children.length,
});
check(
  nudges.children[0].textContent === FINDING.text,
  "the row carries the SERVER's exact sentence — the page composes nothing " +
  "and abbreviates nothing",
  { got: nudges.children[0].textContent },
);
check(
  nudges.children[0].className === "wizard-nudge info",
  "quiet `info` register: a finding is something to know, not a problem to " +
  "solve — the same styling the aged-resume history note uses",
  { got: nudges.children[0].className },
);
check(nudges.children[0].tag === "p", "one paragraph per finding");

// --- nothing banked renders nothing at all ------------------------------
render({ ...baseEnvelope });
check(
  nudges.children.length === 0,
  "no findings: no rows, no heading, no 'nothing found' line — a clean " +
  "speaker has nothing to say and says it",
);

// --- an envelope from a build without the key must not crash ------------
const legacy = { ...baseEnvelope };
delete legacy.findings;
render(legacy);
check(
  nudges.children.length === 0,
  "an envelope predating the `findings` key renders nothing, no crash — the " +
  "same tolerance every other optional key on this page has",
);

// --- ordering: screen state first, findings next, numbers folded last ----
render({
  ...baseEnvelope,
  nudges: [{ code: "x", severity: "warn", text: "The predicted result misses." }],
  findings: [FINDING, { text: "A second thing it learned." }],
  expert_details: ["flatness average error 1.20 dB"],
});
check(
  nudges.children.length === 4,
  "one warn nudge + two findings + the expert <details>",
  { rows: nudges.children.map((row) => row.tag) },
);
check(
  nudges.children[0].textContent === "The predicted result misses.",
  "the screen's own state leads — a nudge is about THIS screen, a finding is " +
  "about the speaker",
);
check(
  nudges.children[1].textContent === FINDING.text
    && nudges.children[2].textContent === "A second thing it learned.",
  "both findings render, in the order the envelope carries them — the " +
  "producer's order, never re-sorted and never de-duplicated here",
  { got: nudges.children.slice(1, 3).map((row) => row.textContent) },
);
check(
  nudges.children[3].tag === "details",
  "the expert numbers stay LAST and stay folded away — a finding is household " +
  "copy and must not be collapsed behind an expert toggle",
  { got: nudges.children[3].tag },
);

// --- a malformed row is dropped, never rendered as a blank line ---------
render({
  ...baseEnvelope,
  findings: [{}, { text: "" }, { text: null }, null, FINDING],
});
check(
  nudges.children.length === 1
    && nudges.children[0].textContent === FINDING.text,
  "rows with no readable sentence are dropped rather than drawn empty",
  { rows: nudges.children.map((row) => row.textContent) },
);

console.log(JSON.stringify({ ok: true, passed }));
