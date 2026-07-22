// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the phone-side ambient-stats emitter (ambient-stats.js, Wave 2
// W2.1/W2.4). The module imports `rmsToDbfs` from the shared
// measurement-audio helper and `CLIP_ABS_THRESHOLD` from level-events.js,
// neither resolvable from a Node test path; strip both imports and inject
// identical implementations, then import the rewritten module from a
// data: URL — the same strip-and-eval pattern the other capture-page
// harnesses use (see capture_level_events_test.mjs).
//
// Schema conformance is cross-checked against the REAL Python parser in
// tests/test_capture_page_ambient_stats_bridge.py — this harness only proves
// the JS-side computation and wire shape.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const modulePath = resolve(here, "../../capture-page/js/ambient-stats.js");

const raw = readFileSync(modulePath, "utf8");
const rewritten = raw
  .replace(
    /^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']*measurement-audio\.js[^"']*["'];\s*/m,
    "const rmsToDbfs = (rms) => { const v = Number(rms); return v > 0 ? 20 * Math.log10(v) : -120; };\n",
  )
  .replace(
    /^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']*level-events\.js[^"']*["'];\s*/m,
    "const CLIP_ABS_THRESHOLD = 0.999;\n",
  );
if (/^import\s/m.test(rewritten)) {
  throw new Error("unhandled import in ambient-stats.js — add a strip rule");
}

const dataUrl =
  "data:text/javascript;base64," + Buffer.from(rewritten, "utf8").toString("base64");
const {
  AMBIENT_STATS_SCHEMA_VERSION,
  AMBIENT_STATS_MAX_BANDS,
  computeOctaveBandStats,
  buildAmbientStatsEvent,
} = await import(dataUrl);

let passed = 0;
function ok() {
  passed += 1;
}

function toneSamples({ freqHz = 250, amplitude = 0.01, sampleRate = 48000, durationS = 0.8 } = {}) {
  const n = Math.round(sampleRate * durationS);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    out[i] = amplitude * Math.sin((2 * Math.PI * freqHz * i) / sampleRate);
  }
  return out;
}

// 1. A pure tone's loudest reported band contains the tone's frequency.
function testLoudestBandContainsTheTone() {
  const sampleRate = 48000;
  const samples = toneSamples({ freqHz: 250, sampleRate });
  const bands = computeOctaveBandStats(samples, sampleRate);
  const loudest = bands.reduce((a, b) => (b.rms_dbfs > a.rms_dbfs ? b : a));
  assert.ok(loudest.lo_hz <= 250 && 250 <= loudest.hi_hz, `expected 250 Hz inside the loudest band, got ${JSON.stringify(loudest)}`);
  ok();
}

// 2. Band count stays well under the Pi's AMBIENT_STATS_MAX_BANDS ceiling —
//    the fixed octave-center set at 48 kHz.
function testBandCountUnderMax() {
  const bands = computeOctaveBandStats(toneSamples({}), 48000);
  assert.ok(bands.length > 0);
  assert.ok(bands.length <= AMBIENT_STATS_MAX_BANDS);
  ok();
}

// 3. Every band's edges increase and are finite — matches AmbientBand's own
//    Pi-side __post_init__ validation (lo_hz > 0, hi_hz > lo_hz, finite).
function testBandEdgesAreValid() {
  const bands = computeOctaveBandStats(toneSamples({}), 48000);
  for (const band of bands) {
    assert.ok(Number.isFinite(band.lo_hz) && band.lo_hz > 0);
    assert.ok(Number.isFinite(band.hi_hz) && band.hi_hz > band.lo_hz);
    assert.ok(Number.isFinite(band.rms_dbfs));
  }
  ok();
}

// 4. buildAmbientStatsEvent's wire shape matches parse_ambient_stats_event's
//    schema EXACTLY: top-level ambient_stats key, schema=1 (int, not bool),
//    run_token echoed verbatim, duration_s a number, clipped a bool, bands
//    a non-empty array of {lo_hz, hi_hz, rms_dbfs}.
function testEventShapeMatchesPiSchema() {
  const event = buildAmbientStatsEvent(toneSamples({}), 48000, "run-token-abc", 0.8);
  assert.deepEqual(Object.keys(event), ["ambient_stats"]);
  const stats = event.ambient_stats;
  assert.deepEqual(
    new Set(Object.keys(stats)),
    new Set(["schema", "run_token", "duration_s", "clipped", "bands"]),
  );
  assert.equal(stats.schema, AMBIENT_STATS_SCHEMA_VERSION);
  assert.equal(typeof stats.schema, "number");
  assert.equal(stats.run_token, "run-token-abc");
  assert.equal(typeof stats.duration_s, "number");
  assert.equal(stats.duration_s, 0.8);
  assert.equal(typeof stats.clipped, "boolean");
  assert.equal(stats.clipped, false);
  assert.ok(Array.isArray(stats.bands));
  assert.ok(stats.bands.length >= 1 && stats.bands.length <= AMBIENT_STATS_MAX_BANDS);
  for (const band of stats.bands) {
    assert.deepEqual(new Set(Object.keys(band)), new Set(["lo_hz", "hi_hz", "rms_dbfs"]));
  }
  ok();
}

// 5. A clipping capture is reported clipped:true — the Pi's parser treats a
//    clipped ambient as untrustworthy and falls back regardless of bands.
function testClippedCaptureIsReported() {
  const samples = toneSamples({ amplitude: 1.5 }); // clips at the +-1.0 float rail
  const event = buildAmbientStatsEvent(samples, 48000, "run-token-abc", 0.8);
  assert.equal(event.ambient_stats.clipped, true);
  ok();
}

// 6. An empty run_token still produces a valid (if empty) run_token field —
//    never undefined/null on the wire (mirrors level_batch's own run_token
//    echo convention).
function testMissingRunTokenCoercesToEmptyString() {
  const event = buildAmbientStatsEvent(toneSamples({}), 48000, undefined, 0.8);
  assert.equal(event.ambient_stats.run_token, "");
  ok();
}

const tests = [
  testLoudestBandContainsTheTone,
  testBandCountUnderMax,
  testBandEdgesAreValid,
  testEventShapeMatchesPiSchema,
  testClippedCaptureIsReported,
  testMissingRunTokenCoercesToEmptyString,
];

let failure = null;
for (const t of tests) {
  try {
    t();
  } catch (e) {
    failure = { test: t.name, error: String(e && e.stack ? e.stack : e) };
    break;
  }
}

if (failure) {
  console.error(failure.error);
  console.log(JSON.stringify({ ok: false, ...failure }));
  process.exit(1);
} else {
  console.log(JSON.stringify({ ok: true, passed }));
}
