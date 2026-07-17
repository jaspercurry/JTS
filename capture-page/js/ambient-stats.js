// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Per-octave-band ambient-noise stats for the driver-sweep quiet window (Wave
// 2, phone-mic-relay-plan.md's "Dormant until the page PR" §, W2.1/W2.4
// closed-loop SNR level solve).
//
// jasper/audio_measurement/level_solver.py's `solve_level` picks the
// quietest safe (main_volume_db, commissioning_gain_db) for a driver sweep
// from the room's PER-BAND ambient noise; absent a real measurement it
// synthesizes a conservative broadband guess. `parse_ambient_stats_event`
// (same module) already parses this event on the Pi side — this module is
// the matching phone-side EMITTER. The parser validates schema, run_token,
// clipped, and bands field for field; `duration_s` is an emitted SUPERSET
// field the parser currently ignores (it matches the Pi test vectors and
// rides for observability / future ambient-drift checks, W2.4):
//
//   { ambient_stats: { schema, run_token, duration_s, clipped, bands } }
//   bands: [{ lo_hz, hi_hz, rms_dbfs }, ...]   (1..AMBIENT_STATS_MAX_BANDS)
//
// Pure and dependency-free (no DOM/browser globals) so it is unit-testable in
// Node (tests/js/capture_ambient_stats_test.mjs) and cross-checked against
// the real Python parser (tests/test_capture_page_ambient_stats_bridge.py).

import { rmsToDbfs } from "./measurement-audio.js?v=20260711-4";
import { CLIP_ABS_THRESHOLD } from "./level-events.js?v=20260716-1";

// MUST match jasper.audio_measurement.level_solver.AMBIENT_STATS_SCHEMA_VERSION.
export const AMBIENT_STATS_SCHEMA_VERSION = 1;
// MUST match jasper.audio_measurement.level_solver.AMBIENT_STATS_MAX_BANDS.
export const AMBIENT_STATS_MAX_BANDS = 64;

// ISO-ish octave-band centers, 31.5 Hz - 16 kHz (10 bands). A driver sweep's
// admitted band is Pi-owned and not carried on the wire spec (only the Pi
// knows the driver-safety-confirmed excitation band), so the phone reports a
// fixed set spanning the sweep's own audible range; the Pi's solver clips
// whatever it receives to the admitted band before use
// (`_clip_ambient_bands`), so a broader phone-side set is harmless.
const OCTAVE_BAND_CENTERS_HZ = Object.freeze([
  31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000,
]);
// 2**0.5 - band edges are +/- half an octave from the center.
const OCTAVE_EDGE_FACTOR = Math.SQRT2;
const DEFAULT_Q = 0.67; // matches measurement-audio.js's createBandpassRmsMeter

function round1(x) {
  return Math.round(x * 10) / 10;
}

// RBJ bandpass (constant skirt gain) biquad coefficients — identical formula
// to measurement-audio.js's createBandpassRmsMeter, just evaluated in plain
// JS over an already-captured buffer instead of an AudioWorklet.
function bandpassCoeffs(centerHz, sampleRate, q) {
  const w0 = (2 * Math.PI * centerHz) / sampleRate;
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  return {
    b0: alpha / a0,
    b1: 0,
    b2: -alpha / a0,
    a1: (-2 * Math.cos(w0)) / a0,
    a2: (1 - alpha) / a0,
  };
}

function bandRmsDbfs(samples, coeffs) {
  let x1 = 0;
  let x2 = 0;
  let y1 = 0;
  let y2 = 0;
  let sumSquares = 0;
  for (let i = 0; i < samples.length; i++) {
    const x = samples[i];
    const y = coeffs.b0 * x + coeffs.b1 * x1 + coeffs.b2 * x2
      - coeffs.a1 * y1 - coeffs.a2 * y2;
    x2 = x1;
    x1 = x;
    y2 = y1;
    y1 = y;
    sumSquares += y * y;
  }
  const rms = samples.length ? Math.sqrt(sumSquares / samples.length) : 0;
  return rmsToDbfs(rms);
}

// Per-octave-band RMS dBFS over a captured ambient buffer. Bands whose upper
// edge would exceed Nyquist are dropped (never happens at the required 48 kHz
// capture rate with this fixed 31.5 Hz-16 kHz center set, but keeps the
// function correct for any sampleRate). Always well under
// AMBIENT_STATS_MAX_BANDS (10 centers).
export function computeOctaveBandStats(samples, sampleRate, { q = DEFAULT_Q } = {}) {
  const input = samples instanceof Float32Array ? samples : new Float32Array(samples || []);
  const nyquist = sampleRate / 2;
  const bands = [];
  for (const center of OCTAVE_BAND_CENTERS_HZ) {
    const hi = center * OCTAVE_EDGE_FACTOR;
    if (hi >= nyquist) continue;
    bands.push({
      lo_hz: round1(center / OCTAVE_EDGE_FACTOR),
      hi_hz: round1(hi),
      rms_dbfs: round1(bandRmsDbfs(input, bandpassCoeffs(center, sampleRate, q))),
    });
  }
  return bands;
}

function samplesClipped(samples) {
  for (let i = 0; i < samples.length; i++) {
    if (Math.abs(samples[i]) >= CLIP_ABS_THRESHOLD) return true;
  }
  return false;
}

// Build the `{ambient_stats: {...}}` event payload for one quiet-window
// capture. `durationS` is the actual recorded window length in seconds.
// `runToken` echoes the spec's run_token exactly like level_batch does, so a
// stale event from a previous attempt can never feed the wrong solve
// (parse_ambient_stats_event rejects a run_token mismatch).
export function buildAmbientStatsEvent(samples, sampleRate, runToken, durationS) {
  const input = samples instanceof Float32Array ? samples : new Float32Array(samples || []);
  const bands = computeOctaveBandStats(input, sampleRate).slice(0, AMBIENT_STATS_MAX_BANDS);
  return {
    ambient_stats: {
      schema: AMBIENT_STATS_SCHEMA_VERSION,
      run_token: String(runToken || ""),
      duration_s: Number(durationS) || 0,
      clipped: samplesClipped(input),
      bands,
    },
  };
}
