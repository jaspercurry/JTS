// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// #2641, the client half. The server minted "Keep current sound" as a
// decision, and renderActions() branched on `href` FIRST — so the decision
// rendered as an anchor, the click reloaded the page, and the household
// landed back on the same decision screen. Measured live: five clicks, no
// state-changing request in the network log.
//
// The invariant this pins is the one that makes "every minted action is
// machine-actionable" true for the BROWSER as well as for a driver reading
// the envelope: when an action carries an endpoint, that endpoint is what the
// control performs; `href` is only a presentation hint. An action with an
// href and no endpoint is still a navigation, which is what keeps
// "Continue to Room correction" a link.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, installFixedDocument } from "./_dom.mjs";

const elements = installFixedDocument(CROSSOVER_IDS);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

const posted = [];
let nextEnvelope = {
  verdict_text: "", steps: [], nudges: [], capture: null,
  next_action: null, alternate_actions: [],
};
globalThis.__getJSON = async () => nextEnvelope;
globalThis.__postJSON = async (url, body) => {
  posted.push({ url, body });
  return { status: "ok" };
};
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
  endpoint: "/correction/crossover/v2/decline",
  body: { expected_candidate_fingerprint: "fp-1" },
  href: "/correction/crossover/",
};
const ROOM = {
  id: "room",
  label: "Continue to Room correction",
  href: "/correction/room/",
};

render({
  verdict_text: "Review", steps: [], nudges: [], capture: null,
  next_action: null,
  alternate_actions: [DECLINE, ROOM],
});

const [decline, room] = rowChildren();

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
  posted[0].url === "/correction/crossover/v2/decline",
  "(b) it posts to the endpoint the envelope named",
);
check(
  posted[0].body.expected_candidate_fingerprint === "fp-1",
  "(b) the candidate guard rides the body",
);

// --- (c) href-only stays a navigation -------------------------------------
// The other half of the rule. A cross-subsystem link has no endpoint that
// could perform it, and turning it into a dead button would be the mirror of
// the bug above.
check(room.tag === "a", "(c) an href-only action still renders an anchor");
check(
  room.href === "/correction/room/",
  "(c) and keeps pointing where the envelope said",
);

console.log(JSON.stringify({ ok: true, passed }));
