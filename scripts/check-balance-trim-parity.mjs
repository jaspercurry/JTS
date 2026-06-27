#!/usr/bin/env node

// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// JS↔Python parity check for the multiroom balance-trim mapping.
//
// The browser rooms card (deploy/assets/rooms/js/grouping-view.js) and the
// Python backend (jasper/web/rooms_setup._balance_trims_from_db) compute the
// same signed-dB → {left_trim, right_trim} mapping. This script runs the JS
// mapping over a representative sweep of balance_db values and compares it
// against a fixture generated from the Python implementation.
//
// tests/fixtures/balance_trim_parity_fixture.json is the shared contract:
// tests/test_balance_trim_parity.py asserts Python matches it; this script
// asserts the JS module matches it. If either side drifts, one test fails.
//
// Usage: node scripts/check-balance-trim-parity.mjs   (exit 0 = parity holds)

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { trimsForBalance, BALANCE_MIN_DB, BALANCE_MAX_DB } from "../deploy/assets/rooms/js/grouping-view.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const fixture = JSON.parse(
  readFileSync(join(root, "tests/fixtures/balance_trim_parity_fixture.json"), "utf8"),
);

// Sanity: JS range constants must match fixture metadata.
if (BALANCE_MIN_DB !== fixture.balance_min_db) {
  console.error(
    `RANGE MISMATCH: JS BALANCE_MIN_DB=${BALANCE_MIN_DB} but fixture expects ${fixture.balance_min_db}`,
  );
  process.exit(1);
}
if (BALANCE_MAX_DB !== fixture.balance_max_db) {
  console.error(
    `RANGE MISMATCH: JS BALANCE_MAX_DB=${BALANCE_MAX_DB} but fixture expects ${fixture.balance_max_db}`,
  );
  process.exit(1);
}

const TOL = 1e-9; // both sides are integer or one decimal place — should be exact
let failures = 0;
for (const c of fixture.cases) {
  const got = trimsForBalance(c.balance_db);
  if (Math.abs(got.left - c.left) > TOL || Math.abs(got.right - c.right) > TOL) {
    failures += 1;
    console.error(
      `MISMATCH balance_db=${c.balance_db}: ` +
        `js={left:${got.left}, right:${got.right}} ` +
        `fixture={left:${c.left}, right:${c.right}}`,
    );
  }
}

if (failures) {
  console.error(
    `\n${failures} parity mismatch(es). ` +
      "JS trimsForBalance drifted from the Python reference (_balance_trims_from_db).",
  );
  process.exit(1);
}
console.log(
  `Balance-trim parity OK: ${fixture.cases.length} cases match within ${TOL} ` +
    `(range ±${BALANCE_MAX_DB} dB).`,
);
