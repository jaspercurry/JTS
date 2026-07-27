#!/usr/bin/env node

// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// JS↔Python↔CamillaDSP parity check for the PEQ magnitude math.
//
// The browser graph (deploy/assets/sound-profile/js/eq-math.js) and the
// Python preview (jasper/sound/profile.py) compute the same RBJ biquad
// magnitude. tests/fixtures/peq_response_fixture.json is the shared contract:
// the Python test asserts Python matches it; this script asserts the JS
// module matches it. If they ever diverge, one side regressed.
//
// Two implementations agreeing proves nothing about the SPEAKER, though —
// before 2026-07-27 both drew a Butterworth shelf while the emitter wrote
// `slope: 6.0`, whose realised Q is gain-dependent (0.476 at -11 dB), so the
// drawn curve was up to 1.7 dB off what CamillaDSP played. So this script also
// checks every shelf case against CamillaDSP's OWN shelf coefficients
// (v4.1.3 src/filters/biquad.rs) evaluated at fixture.shelf_emission — the
// exact `q` jasper.camilla_stereo_prefix.emit_filter_spec writes.
//
// Usage: node scripts/check-peq-parity.mjs   (exit 0 = parity holds)
// Run it from a maintainer check when touching either implementation.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { magnitudeDb, RESPONSE_SAMPLE_RATE_HZ } from "../deploy/assets/sound-profile/js/eq-math.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const fixture = JSON.parse(
  readFileSync(join(root, "tests/fixtures/peq_response_fixture.json"), "utf8")
);

// CamillaDSP's Highshelf/Lowshelf with ShelfSteepness::Q, transcribed from
// src/filters/biquad.rs. Independent of eq-math.js on purpose.
function camillaShelfDb(type, freq, gainDb, q, at) {
  const omega = 2.0 * Math.PI * freq / RESPONSE_SAMPLE_RATE_HZ;
  const sn = Math.sin(omega), cs = Math.cos(omega);
  const A = Math.pow(10.0, gainDb / 40.0);
  const beta = sn * Math.sqrt(A) / q;
  let b0, b1, b2, a0, a1, a2;
  if (type === "Highshelf") {
    b0 = A * ((A + 1) + (A - 1) * cs + beta);
    b1 = -2 * A * ((A - 1) + (A + 1) * cs);
    b2 = A * ((A + 1) + (A - 1) * cs - beta);
    a0 = (A + 1) - (A - 1) * cs + beta;
    a1 = 2 * ((A - 1) - (A + 1) * cs);
    a2 = (A + 1) - (A - 1) * cs - beta;
  } else {
    b0 = A * ((A + 1) - (A - 1) * cs + beta);
    b1 = 2 * A * ((A - 1) - (A + 1) * cs);
    b2 = A * ((A + 1) - (A - 1) * cs - beta);
    a0 = (A + 1) + (A - 1) * cs + beta;
    a1 = -2 * ((A - 1) + (A + 1) * cs);
    a2 = (A + 1) + (A - 1) * cs - beta;
  }
  const w = 2.0 * Math.PI * at / RESPONSE_SAMPLE_RATE_HZ;
  const cw = Math.cos(w), sw = Math.sin(w), c2 = Math.cos(2 * w), s2 = Math.sin(2 * w);
  const nRe = b0 + b1 * cw + b2 * c2, nIm = -(b1 * sw + b2 * s2);
  const dRe = a0 + a1 * cw + a2 * c2, dIm = -(a1 * sw + a2 * s2);
  return 20.0 * Math.log10(Math.hypot(nRe, nIm) / Math.hypot(dRe, dIm));
}

const TOL = 1e-6; // both sides are float64 doing identical arithmetic.
const SHELF_TYPES = ["Lowshelf", "Highshelf"];
const emission = fixture.shelf_emission;
if (!emission || emission.param !== "q" || typeof emission.value !== "number") {
  console.error("fixture.shelf_emission missing or not a `q` steepness — the CamillaDSP leg cannot run.");
  process.exit(1);
}
let failures = 0;
let shelfChecked = 0;
for (const c of fixture.cases) {
  const shelf = SHELF_TYPES.includes(c.type);
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
    if (!shelf) return;
    const realized = camillaShelfDb(c.type, c.freq, c.gain_db, emission.value, f);
    if (Math.abs(realized - want) > TOL) {
      failures += 1;
      console.error(
        `UNREALIZED ${c.type} f0=${c.freq} g=${c.gain_db} @${f}Hz: ` +
        `camilladsp(q=${emission.value})=${realized} fixture=${want} ` +
        `(Δ=${Math.abs(realized - want)})`
      );
    }
  });
  if (shelf) shelfChecked += 1;
}

if (failures) {
  console.error(`\n${failures} parity mismatch(es). JS eq-math.js drifted from the Python reference, or the emitted shelf steepness no longer realizes the drawn curve.`);
  process.exit(1);
}
console.log(`PEQ parity OK: ${fixture.cases.length} cases × ${fixture.test_freqs.length} freqs match within ${TOL} (${shelfChecked} shelf cases also checked against CamillaDSP at q=${emission.value}).`);
