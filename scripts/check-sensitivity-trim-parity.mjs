#!/usr/bin/env node

// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// JS↔Python parity check for the sensitivity→level-trim derivation.
//
// The /sound/ active-crossover form pre-fills a starting per-driver gain trim
// from the driver sensitivity gap (optimistic UI:
// deploy/assets/sound-profile/js/active-speaker-ui.js::sensitivityTrimsFromGap),
// and the server re-derives the same fail-safe authoritatively on save
// (jasper/active_speaker/baseline_profile.py::_derive_corrections). The two MUST
// agree. tests/fixtures/sensitivity_trim_fixture.json is the shared contract:
// the Python test (tests/test_active_speaker_baseline_profile.py) asserts the
// source matches it; this script asserts the JS module matches it. If they ever
// diverge, one side regressed. Same model as scripts/check-peq-parity.mjs.
//
// Usage: node scripts/check-sensitivity-trim-parity.mjs   (exit 0 = parity holds)
// Run it from a maintainer check when touching either implementation.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { sensitivityTrimsFromGap } from "../deploy/assets/sound-profile/js/active-speaker-ui.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const fixture = JSON.parse(
  readFileSync(join(root, "tests/fixtures/sensitivity_trim_fixture.json"), "utf8")
);

const TOL = 1e-9; // both sides round to 0.1 dB doing identical arithmetic.

function trimsEqual(got, want) {
  const gotKeys = Object.keys(got).sort();
  const wantKeys = Object.keys(want).sort();
  if (gotKeys.length !== wantKeys.length) return false;
  for (let i = 0; i < gotKeys.length; i += 1) {
    if (gotKeys[i] !== wantKeys[i]) return false;
    if (Math.abs(got[gotKeys[i]] - want[wantKeys[i]]) > TOL) return false;
  }
  return true;
}

let failures = 0;
for (const c of fixture.cases) {
  const got = sensitivityTrimsFromGap(c.sensitivities);
  if (!trimsEqual(got, c.expected_trims)) {
    failures += 1;
    console.error(
      `MISMATCH ${c.name}: js=${JSON.stringify(got)} ` +
      `fixture=${JSON.stringify(c.expected_trims)}`
    );
  }
}

if (failures) {
  console.error(
    `\n${failures} parity mismatch(es). active-speaker-ui.js sensitivityTrimsFromGap ` +
    "drifted from the Python reference (_derive_corrections)."
  );
  process.exit(1);
}
console.log(
  `Sensitivity-trim parity OK: ${fixture.cases.length} cases match within ${TOL}.`
);
