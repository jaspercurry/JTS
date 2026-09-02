// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Executes the transport-park card against a tiny structural DOM seam, and
// pins where its section lands in the System panel. Owner ruling 2026-08-27
// gives parks no banner anywhere, so this card is the ONLY surface a browser
// can learn about one from — which makes "renders nothing when nothing is
// parked", "renders every park when something is", and "sits above the
// metrics" all load-bearing, and makes tolerance of a malformed entry the
// difference between one blank row and a blank card.
//
// Headlines are asserted by IDENTITY against the module's own exported table,
// never by matching the copy: this pins which status selects which entry,
// which is the behaviour, and leaves the wording free to change.

import assert from "node:assert/strict";
import { buildFunction } from "./_loader.mjs";

const [sectionsPath, viewsPath] = process.argv.slice(2);
if (!sectionsPath || !viewsPath) {
  throw new Error("usage: node system_transport_park_test.mjs <sections.js> <views.js>");
}

function flatten(items) {
  return items.flatMap((item) => Array.isArray(item) ? flatten(item) : [item]);
}

function h(tag, props, ...children) {
  return {
    tag,
    props: props || {},
    children: flatten(children).filter((child) => child != null && child !== false),
    // Enough of an Element for buildSystemPanel's assembly: it appends into
    // card bodies and tags one section with a class.
    append(...nodes) { this.children.push(...flatten(nodes)); },
    classList: { add() {} },
    style: { setProperty() {} },
  };
}
const defList = (rows, modifier = "") => h("deflist", { rows, modifier });

const STRIP = [[/^import[\s\S]*?;\n/gm, ""], [/^export /gm, ""]];

const {
  transportParkCard, transportParkBody, PARK_HEADLINE, CONVERGE_HEADLINE,
} = buildFunction(sectionsPath, {
  rewrite: STRIP,
  guardNoImports: true,
  params: ["h", "defList"],
  returns: [
    "transportParkCard", "transportParkBody", "PARK_HEADLINE", "CONVERGE_HEADLINE",
  ],
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
assert.equal(parked.headline, PARK_HEADLINE.parked);
const parkedRows = rowsOf(parked);
// The class token is the label, underscores read as spaces — one vocabulary
// shared with /state and jasper-doctor, never a second map to drift.
assert.match(parkedRows["mono full range"], /1-channel full-range layout/);
assert.match(parkedRows["mono full range"], /#3117/);
assert.doesNotMatch(parkedRows["mono full range"], /Clear it with/);
assert.match(
  parkedRows["roleful active endpoint unconverged"], /baseline-reemit --endpoint ring/);

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
assert.equal(refused.headline, CONVERGE_HEADLINE);
assert.equal(
  rowsOf(refused)["ring converge refused"],
  "the marker is armed but the loaded graph is elsewhere",
);

// It rides alongside a park rather than replacing one, and the park's own
// status still chooses the headline.
const both = transportParkCard({
  status: "parked",
  parks: [{ park_class: "mono_full_range", issue: "#3117", detail: "why" }],
  converge_refused: "and the graph never moved",
});
assert.equal(both.rows.length, 2);
assert.equal(both.headline, PARK_HEADLINE.parked);

// --- the honest-silence statuses --------------------------------------------
const unclassified = transportParkCard({ status: "unclassified", parks: [] });
assert.equal(unclassified.headline, PARK_HEADLINE.unclassified);
assert.deepEqual(unclassified.rows, []);

const unavailable = transportParkCard({
  status: "unavailable", parks: [], error: "topology unreadable",
});
assert.equal(unavailable.headline, PARK_HEADLINE.unavailable);
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

// --- where the card sits in the System panel --------------------------------
// Built, not grepped: a park is why a box emits nothing, so its card belongs
// with the audio alert ABOVE the metrics grid rather than below the fold.
const titled = (title) => ({ section: h("section", { title }), body: h("div") });
const { buildSystemPanel } = buildFunction([sectionsPath, viewsPath], {
  rewrite: STRIP,
  guardNoImports: true,
  params: [
    "h", "defList", "livePill", "titledCard", "actionButton", "collapsible",
    "buildDebugCard", "buildUsbForensicsCard", "buildEnhancedAecCard",
  ],
  returns: ["buildSystemPanel"],
})(
  h, defList,
  () => ({ el: h("live-pill"), label: { textContent: "" } }),
  titled,
  (label) => h("button", { label }),
  ({ title, body }) => h("collapsible", { title }, body),
  () => h("debug-card"),
  () => ({ card: h("forensics-card"), update() {} }),
  () => h("aec-card"),
);

const { panel, refs } = buildSystemPanel({});
const order = panel.children.map(
  (child) => child.props.title || child.tag,
);
const parksAt = order.indexOf("Audio transport parks");
assert.notEqual(parksAt, -1, "the System panel must carry the park card");
assert.equal(order[parksAt - 1], "Audio",
  "the park card sits beside the audio alert");
assert.ok(parksAt < order.indexOf("section.stat-grid"),
  "the park card must sit ABOVE the vitals grid, not below the metrics");
// Built hidden: a healthy box pays nothing for this card until update() finds
// a park, which is the "nothing renders" half of the ruling at panel level.
assert.equal(refs.parksSection.hidden, true);
assert.equal(refs.audioAlertSection.hidden, true);

process.stdout.write(JSON.stringify({ ok: true }));
