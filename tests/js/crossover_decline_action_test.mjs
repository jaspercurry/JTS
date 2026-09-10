// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, installFixedDocument } from "./_dom.mjs";

const elements = installFixedDocument(CROSSOVER_IDS);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

const posted = [];
let nextEnvelope = {
  verdict_text: "", steps: [], nudges: [], capture: { status: "complete" },
  next_action: { label: "Start", endpoint: "/sound/speaker/crossover/v2/session" },
  alternate_actions: [{ label: "Start full", endpoint: "/sound/speaker/crossover/v2/session" }],
};
globalThis.__getJSON = async () => nextEnvelope;
globalThis.__postJSON = async (url, body) => {
  posted.push({ url, body });
  return nextEnvelope;
};
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

// #3629: the walk's per-position picture and units toggle -- pinned in
// their own tests/js/crossover_position_diagram_test.mjs and
// tests/js/crossover_units_test.mjs, so this file stubs them through.
globalThis.__positionDiagram = () => ({tag: "svg"});
globalThis.__positionCaption = () => "";
globalThis.__UNIT_IMPERIAL = "imperial";
globalThis.__UNIT_METRIC = "metric";
globalThis.__currentUnits = () => "imperial";
globalThis.__setUnits = () => {};
globalThis.__formatDistances = (text) => text;

const { render } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "renderCloud", "redrawCloudChart",
      "positionDiagram", "positionCaption", "UNIT_IMPERIAL", "UNIT_METRIC", "currentUnits", "setUnits", "formatDistances",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render"],
  },
);

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

function rowChildren() { return elements.get("crossover-action").children; }

// The review screen's real pair, as the server now mints it.
const DECLINE = {
  id: "review_decline",
  label: "Keep current sound",
  endpoint: "/sound/speaker/crossover/v2/decline",
  body: { expected_candidate_fingerprint: "fp-1" },
  href: "/sound/speaker/crossover/",
};
// An href-only navigation out of this flow — no endpoint could perform it.
const HUB = {
  id: "sound_hub",
  label: "Back to Sound",
  href: "/sound/",
};

render({
  verdict_text: "Review", steps: [], nudges: [], capture: null,
  next_action: null,
  alternate_actions: [DECLINE, HUB],
});

const [decline, hub] = rowChildren();

// --- (a) endpoint wins: the decision is a button, not a link --------------
check(decline.tag === "button", "(a) an action with an endpoint renders a button");
check(
  decline.textContent === "Keep current sound",
  "(a) it is the decline that rendered",
);
check(!decline.href, "(a) the presentation hint is not turned into an anchor href");

// --- (b) it actually posts, with the guard body ---------------------------
posted.length = 0;
await decline.click();
check(posted.length === 1, "(b) clicking the decline performs one request");
check(
  posted[0].url === "/sound/speaker/crossover/v2/decline",
  "(b) it posts to the endpoint the envelope named",
);
check(
  posted[0].body.expected_candidate_fingerprint === "fp-1",
  "(b) the candidate guard rides the body",
);
check(rowChildren().length === 2, "both start options return after declining");
check(
  rowChildren().every((button) => button.disabled === false),
  "the completed capture does not leave start disabled after declining",
);

// --- (c) href-only stays a navigation -------------------------------------
// The other half of the rule. A cross-subsystem link has no endpoint that
// could perform it, and turning it into a dead button would be the mirror of
// the bug above.
check(hub.tag === "a", "(c) an href-only action still renders an anchor");
check(
  hub.href === "/sound/",
  "(c) and keeps pointing where the envelope said",
);

console.log(JSON.stringify({ ok: true, passed }));
