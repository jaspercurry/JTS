// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Source harness for /chat/'s date-filter helpers. Node cannot resolve the
// browser-absolute imports in views.js, so ratchet the exact import/export
// surface, strip it, and evaluate only the three helpers under test plus their
// two local formatting dependencies. Also pins shared/js/chrome.js's
// appHeader() against the element tree jasper/web/_common.py's
// canonical_header() emits (SP-1: one page-header contract, not four).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadEsm, repoPath } from "./_loader.mjs";
import { FakeElement } from "./_dom.mjs";

const modulePath = process.argv[2];
if (!modulePath) throw new Error("usage: node chat_views_test.mjs <views.js>");

const expectedImports = [
  'import { h } from "/assets/shared/js/dom.js";',
  'import { appHeader } from "/assets/shared/js/chrome.js";',
  'import { actionButton, badge, livePill, titledCard } from "./components.js";',
];
const expectedExports = [
  "buildPage",
  "dateValueToSince",
  "sinceToDateValue",
  "normalizeSince",
  "updateError",
  "update",
];

const rawSource = readFileSync(modulePath, "utf8");
const importLines = rawSource.match(/^import .*;$/gm) || [];
assert.deepEqual(importLines, expectedImports, "views.js import surface changed");

const exportNames = Array.from(
  rawSource.matchAll(/^export function ([A-Za-z0-9_]+)\(/gm),
  (match) => match[1],
);
assert.deepEqual(exportNames, expectedExports, "views.js export surface changed");

const strippedSource = rawSource
  .replace(/^import .*;\n/gm, "")
  .replace(/^export (?=function )/gm, "");
assert.doesNotMatch(strippedSource, /^\s*(?:import|export)\s/m);

function extractFunction(source, name, nextName = null) {
  const marker = `function ${name}(`;
  assert.equal(source.split(marker).length - 1, 1, `${name} must exist exactly once`);
  const start = source.indexOf(marker);
  const end = nextName === null
    ? source.length
    : source.indexOf(`\n\nfunction ${nextName}(`, start);
  assert.ok(end > start, `could not find the end of ${name}`);
  return source.slice(start, end).trim();
}

const evaluatedSource = [
  extractFunction(strippedSource, "dateValueToSince", "sinceToDateValue"),
  extractFunction(strippedSource, "sinceToDateValue", "normalizeSince"),
  extractFunction(strippedSource, "normalizeSince", "updateError"),
  extractFunction(strippedSource, "localDateValue", "isoNoMillis"),
  extractFunction(strippedSource, "isoNoMillis"),
  "return { dateValueToSince, sinceToDateValue, normalizeSince };",
].join("\n\n");

const { dateValueToSince, sinceToDateValue, normalizeSince } = Function(
  evaluatedSource,
)();

assert.equal(process.env.TZ, "America/New_York");
assert.equal(
  Intl.DateTimeFormat().resolvedOptions().timeZone,
  "America/New_York",
);

// Local midnight is EST in winter and EDT in summer. Leap day uses the
// winter offset and round-trips through the same local calendar date.
const localDates = new Map([
  ["2024-02-29", "2024-02-29T05:00:00Z"],
  ["2026-01-15", "2026-01-15T05:00:00Z"],
  ["2026-07-15", "2026-07-15T04:00:00Z"],
]);
for (const [dateValue, since] of localDates) {
  assert.equal(dateValueToSince(dateValue), since);
  assert.equal(sinceToDateValue(since), dateValue);
  assert.equal(normalizeSince(dateValue), since);
}

// A date input's exact date-only value is stable rather than being parsed as
// UTC midnight (which would display as the prior local day in New York).
assert.equal(sinceToDateValue("2026-07-15"), "2026-07-15");
assert.equal(sinceToDateValue("2026-07-15T02:30:00Z"), "2026-07-14");

for (const invalid of [
  "2023-02-29",
  "2024-02-30",
  "2026-04-31",
  "2026-00-10",
  "2026-13-01",
]) {
  assert.equal(dateValueToSince(invalid), "");
  assert.equal(normalizeSince(invalid), "");
}

for (const invalid of ["", "not-a-date"]) {
  assert.equal(dateValueToSince(invalid), "");
  assert.equal(sinceToDateValue(invalid), "");
}
assert.equal(dateValueToSince("07/15/2026"), "");
assert.equal(
  sinceToDateValue("2026-07-15-not-a-real-timestamp"),
  "2026-07-15",
);

assert.equal(normalizeSince(""), "");
assert.equal(normalizeSince("   "), "");
assert.equal(
  normalizeSince("  2026-07-15T02:30:00Z  "),
  "2026-07-15T02:30:00Z",
);
assert.equal(normalizeSince("  not-an-iso-filter  "), "not-an-iso-filter");

// appHeader() parity: same element tree canonical_header() emits (a round
// #icon-back button, the centred title, the right-slot placeholder) — a
// class/attribute pin, not a byte-for-byte string compare, since nothing in
// this test suite spawns Python to generate a live fixture.
globalThis.Node = class {};
Object.setPrototypeOf(FakeElement.prototype, globalThis.Node.prototype);
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => ({ __text: String(text) }),
};

const domPath = repoPath("deploy/assets/shared/js/dom.js");
const { appHeader } = await loadEsm(repoPath("deploy/assets/shared/js/chrome.js"), {
  rewrite: [[/from "\/assets\/shared\/js\/dom.js";/, `from "file://${domPath}";`]],
});

const header = appHeader({ title: "EQ", backHref: "/sound/setup/" });
assert.equal(header.tag, "header");
assert.equal(header.className, "app-header");
const [row] = header.children;
assert.equal(row.tag, "div");
assert.equal(row.className, "app-header__row");
const [back, title, right] = row.children;
assert.equal(back.tag, "a");
assert.equal(back.className, "icon-button");
assert.equal(back.attributes.href, "/sound/setup/");
assert.equal(back.attributes["aria-label"], "Home");
const [icon] = back.children;
assert.equal(icon.tag, "svg");
assert.equal(icon.attributes.class, "ico");
assert.equal(icon.attributes["aria-hidden"], "true");
assert.equal(icon.children[0].attributes.href, "#icon-back");
assert.equal(title.tag, "h1");
assert.equal(title.className, "app-header__title");
assert.equal(title.children[0].__text, "EQ");
assert.equal(right.tag, "span");

const withRight = appHeader({ title: "T", right: new FakeElement("button") });
assert.equal(withRight.children[0].children[2].tag, "button");

console.log(JSON.stringify({ ok: true, timezone: process.env.TZ }));
