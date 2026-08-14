// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Cross-language frame-ledger proof emitter (issue #2094).
//
// The capture page (Cloudflare Pages) and the Pi ship as independent releases,
// so the frame counters' key spellings and meanings are a WIRE CONTRACT. A key
// the host reads and the page never sends is a check that silently never
// evaluates — the exact "nobody looked" failure #2094 exists to remove,
// reintroduced one release later and just as quiet.
//
// This runs the PAGE's real `summarizeCaptureIntegrity` over the stats shape the
// recorder worklet actually posts, and prints the wire objects as JSON so
// `tests/test_capture_frame_ledger.py` can feed them to the host's real
// `reconcile_capture_frames` and check the ledger reads them the way the page
// meant them. Nothing here restates a key by hand.
//
// Mirrors tests/js/capture_crypto_emit.mjs, which does the same for the E2E
// crypto round trip. `node --check` is syntax-only and does not resolve the
// import, so the syntax sweep stays green either way.
//
//   node tests/js/capture_frame_report_emit.mjs

import { summarizeCaptureIntegrity } from "../../capture-page/js/capture-integrity.js";

// One Web Audio render quantum — the size of every 2026-08-03 loss.
const QUANTUM = 128;
const BLOCKS = 3750;
const FRAMES = BLOCKS * QUANTUM;

// What the worklet posts on a take it saw no discontinuity in. The field names
// are the recorder's own (deploy/assets/shared/js/measurement-audio.js).
const cleanStats = {
  frames: FRAMES,
  blocks: BLOCKS,
  block_gaps: 0,
  block_gap_frames: 0,
  silent_blocks: 0,
};

// The same take with one quantum the render graph never delivered. Note the
// frame counts do NOT drop: the missing frames were never handed to anyone, so
// the worklet assembled and the page encoded exactly what it received.
const gapStats = {
  ...cleanStats,
  blocks: BLOCKS - 1,
  block_gaps: 1,
  block_gap_frames: QUANTUM,
  frames: FRAMES - QUANTUM,
};

const emit = (stats) =>
  summarizeCaptureIntegrity({
    watch: null,
    stats,
    // main.js passes `samples.length` — the array it is about to hand the WAV
    // encoder, which is what the worklet transferred.
    encodedFrames: stats.frames,
  });

console.log(
  JSON.stringify({
    quantum: QUANTUM,
    clean: emit(cleanStats),
    render_gap: emit(gapStats),
    // A page whose recorder reported nothing at all (an older bundle): the
    // summary must OMIT the keys rather than send zeros, or the host cannot
    // tell "counted, and it was zero" from "nobody counted".
    unreported: summarizeCaptureIntegrity({ watch: null, stats: null }),
  }),
);
