#!/usr/bin/env node
// JS↔Python parity check for the PEQ magnitude math.
//
// The browser graph (deploy/assets/sound-profile/js/eq-math.js) and the
// Python preview (jasper/sound/profile.py) compute the same RBJ biquad
// magnitude. tests/fixtures/peq_response_fixture.json is the shared contract:
// the Python test asserts Python matches it; this script asserts the JS
// module matches it. If they ever diverge, one side regressed.
//
// Usage: node scripts/check-peq-parity.mjs   (exit 0 = parity holds)
// Run it from a maintainer check when touching either implementation.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { magnitudeDb } from "../deploy/assets/sound-profile/js/eq-math.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const fixture = JSON.parse(
  readFileSync(join(root, "tests/fixtures/peq_response_fixture.json"), "utf8")
);

const TOL = 1e-6; // both sides are float64 doing identical arithmetic.
let failures = 0;
for (const c of fixture.cases) {
  fixture.test_freqs.forEach((f, i) => {
    const got = magnitudeDb(c.type, c.freq, c.gain_db, c.q, f);
    const want = c.db[i];
    if (Math.abs(got - want) > TOL) {
      failures += 1;
      console.error(
        `MISMATCH ${c.type} f0=${c.freq} g=${c.gain_db} q=${c.q} @${f}Hz: ` +
        `js=${got} fixture=${want} (Δ=${Math.abs(got - want)})`
      );
    }
  });
}

if (failures) {
  console.error(`\n${failures} parity mismatch(es). JS eq-math.js drifted from the Python reference.`);
  process.exit(1);
}
console.log(`PEQ parity OK: ${fixture.cases.length} cases × ${fixture.test_freqs.length} freqs match within ${TOL}.`);
