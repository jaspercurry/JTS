// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pins #3629's page-local units preference. capture_plan.py's
// format_position_distance always emits "NN in (MM cm)" (#1805) -- this
// module only reorders which unit leads; it never hides either, and it
// persists the choice per device (#1941 Q2), not globally.

import assert from "node:assert/strict";

// A minimal localStorage: Map-backed, the same get/set contract the module
// relies on. Node has no global localStorage by default; units.js already
// tolerates that (try/catch around every call), but a real stub lets this
// file pin the PERSISTENCE half of the behavior, not just the in-memory half.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

const units = await import("../../deploy/assets/correction/js/crossover/units.js");

check(units.currentUnits() === units.UNIT_IMPERIAL, "defaults to imperial");
check(
  units.formatDistances("Turn 7 in (18 cm) to the RIGHT.") ===
    "Turn 7 in (18 cm) to the RIGHT.",
  "imperial (the default) leaves format_position_distance's own order alone",
);

units.setUnits(units.UNIT_METRIC);
check(units.currentUnits() === units.UNIT_METRIC, "setUnits switches the in-memory preference");
check(
  units.formatDistances("Turn 7 in (18 cm) to the RIGHT.") ===
    "Turn 18 cm (7 in) to the RIGHT.",
  "metric reorders the SAME two numbers -- neither unit is dropped",
);
check(
  units.formatDistances("Stay on the mark.") === "Stay on the mark.",
  "text without a distance pattern passes through unchanged",
);
check(store.get("jts-crossover-units") === "metric", "the choice is persisted to localStorage");

units.setUnits(units.UNIT_IMPERIAL);
check(
  units.formatDistances("7 in (18 cm)") === "7 in (18 cm)",
  "switching back to imperial restores format_position_distance's own order",
);

// A fresh "page load" (a distinct module instance, forced by the query
// string) reads the persisted choice back rather than defaulting -- the
// actual #1941 Q2 "per-device, remembered" behavior, not just the setter.
store.set("jts-crossover-units", "metric");
const reloaded = await import(
  "../../deploy/assets/correction/js/crossover/units.js?reload=1"
);
check(
  reloaded.currentUnits() === reloaded.UNIT_METRIC,
  "a fresh page load reads the persisted preference back",
);

console.log(JSON.stringify({ ok: true, passed }));
