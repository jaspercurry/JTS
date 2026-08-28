// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Executes the transport-park card against a tiny structural DOM seam. Owner
// ruling 2026-08-27 gives parks no banner anywhere, so this card is the ONLY
// surface a browser can learn about one from — which makes "renders nothing
// when nothing is parked" and "renders every park when something is" both
// load-bearing, and makes tolerance of a malformed entry the difference
// between one blank row and a blank card.

import assert from "node:assert/strict";
import { buildFunction } from "./_loader.mjs";

const modulePath = process.argv[2];
if (!modulePath) throw new Error("usage: node system_transport_park_test.mjs <sections.js>");

function flatten(items) {
  return items.flatMap((item) => Array.isArray(item) ? flatten(item) : [item]);
}

function h(tag, props, ...children) {
  return {
    tag,
    props: props || {},
    children: flatten(children).filter((child) => child != null && child !== false),
  };
}
const defList = (rows, modifier = "") => h("deflist", { rows, modifier });

const { transportParkCard, transportParkBody } = buildFunction(modulePath, {
  rewrite: [[/^import[\s\S]*?;\n/gm, ""], [/^export /gm, ""]],
  guardNoImports: true,
  params: ["h", "defList"],
  returns: ["transportParkCard", "transportParkBody"],
})(h, defList);

function rowsOf(card) {
  return Object.fromEntries(card.rows);
}

// --- nothing renders while the ring serves this box -------------------------
for (const quiet of [
  undefined, null, "nonsense", {},
  // Reaches Object.prototype under a bare index, and would read as a truthy
  // headline — the card would render a function on a healthy box.
  { status: "constructor", parks: [] },
  { status: "toString", parks: [] },
  { status: "ok", parked: false, parks: [], unproven_endpoint: false, converge_refused: null },
  // The ADR-0184 seam is deliberately NOT a park and gets no row here.
  { status: "ok", parks: [], unproven_endpoint: true, converge_refused: null },
]) {
  assert.equal(transportParkCard(quiet), null,
    `expected no park card for ${JSON.stringify(quiet)}`);
}

// --- a live park names itself, its issue and its reason ---------------------
const parked = transportParkCard({
  status: "parked",
  parked: true,
  ring_only: true,
  parks: [
    {
      park_class: "mono_full_range",
      issue: "#3117",
      remedy: null,
      detail: "this box declares a 1-channel full-range layout",
    },
    {
      park_class: "roleful_active_endpoint_unconverged",
      issue: null,
      remedy: "sudo jasper-active-speaker baseline-reemit --endpoint ring",
      detail: "its endpoint marker has not converged onto it",
    },
  ],
  unproven_endpoint: false,
  converge_refused: null,
});
assert.ok(parked, "a parked box must produce a card");
assert.match(parked.headline, /emits nothing/);
const parkedRows = rowsOf(parked);
// The class token is the label, underscores read as spaces — one vocabulary
// shared with /state and jasper-doctor, never a second map to drift.
assert.match(parkedRows["mono full range"], /1-channel full-range layout/);
assert.match(parkedRows["mono full range"], /#3117/);
assert.doesNotMatch(parkedRows["mono full range"], /Clear it with/);
assert.match(
  parkedRows["roleful active endpoint unconverged"], /baseline-reemit --endpoint ring/);

// A box that still plays says so instead of claiming silence.
const pending = transportParkCard({
  status: "pending",
  parks: [{ park_class: "mono_full_range", issue: "#3117", detail: "why" }],
});
assert.match(pending.headline, /still plays on the loopback route/);
assert.equal(pending.rows.length, 1);

// --- the fifth shape: ring-eligible, converge refused -----------------------
// `status` reads clean here; without its own row this box is invisible.
const refused = transportParkCard({
  status: "ok",
  parked: false,
  parks: [],
  unproven_endpoint: false,
  converge_refused: "the marker is armed but the loaded graph is elsewhere",
});
assert.ok(refused, "a converge refusal must produce a card on an ok box");
assert.match(refused.headline, /never moved onto the ring/);
assert.equal(
  rowsOf(refused)["ring converge refused"],
  "the marker is armed but the loaded graph is elsewhere",
);

// It rides alongside a park rather than replacing one.
const both = transportParkCard({
  status: "parked",
  parks: [{ park_class: "mono_full_range", issue: "#3117", detail: "why" }],
  converge_refused: "and the graph never moved",
});
assert.equal(both.rows.length, 2);
assert.match(both.headline, /emits nothing/);

// --- the honest-silence statuses --------------------------------------------
const unclassified = transportParkCard({ status: "unclassified", parks: [] });
assert.match(unclassified.headline, /no named park describes it/);
assert.deepEqual(unclassified.rows, []);

const unavailable = transportParkCard({
  status: "unavailable", parks: [], error: "topology unreadable",
});
assert.equal(rowsOf(unavailable)["read failed"], "topology unreadable");

// --- a malformed entry costs its own row, never the card --------------------
const messy = transportParkCard({
  status: "parked",
  parks: [null, "nonsense", {}, { detail: "unnamed but real" },
    { park_class: "mono_full_range", detail: "why" }],
});
assert.deepEqual(messy.rows, [
  ["transport park", "unnamed but real"],
  ["mono full range", "why"],
]);

// --- the body renders the headline plus the rows ----------------------------
// A flat array, never a wrapper node: `.info-card > * + *` owns the spacing
// between a card's blocks, so a wrapper collapses the gap to zero.
const body = transportParkBody(parked);
assert.ok(Array.isArray(body), "the body must be a flat array of card blocks");
assert.equal(body[0].children[0], parked.headline);
assert.equal(body[1].tag, "deflist");
assert.equal(body[1].props.rows.length, 2);
// The responsive hook the long class tokens need on a narrow screen.
assert.equal(body[1].props.modifier, "parks");
// No rows to show: the headline still stands alone rather than an empty list.
assert.equal(transportParkBody(unclassified).length, 1);

process.stdout.write(JSON.stringify({ ok: true }));
