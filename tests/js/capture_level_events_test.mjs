// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the phone-side batched level-event emitter (level-events.js, P2).
// The module imports `rmsToDbfs` from the shared measurement-audio helper, which
// isn't resolvable from a Node test path; we strip that import and inject an
// identical `rmsToDbfs` (matching the Pi's quality._dbfs math), then import the
// rewritten module from a data: URL — the same strip-and-eval pattern the other
// capture-page harnesses use. Asserts:
//   * batching (<= 2 Hz posts, not one-per-sample);
//   * the superset envelope (armed / aborted / agc_frozen on every batch);
//   * dBFS + clip agree with the Pi;
//   * the rolling window is time-bounded.
//
//   node tests/js/capture_level_events_test.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const modulePath = resolve(here, "../../capture-page/js/level-events.js");

const raw = readFileSync(modulePath, "utf8");
const rewritten = raw.replace(
  /^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']*measurement-audio\.js[^"']*["'];\s*/m,
  "const rmsToDbfs = (rms) => { const v = Number(rms); return v > 0 ? 20 * Math.log10(v) : -120; };\n",
);
if (/^import\s/m.test(rewritten)) {
  throw new Error("unhandled import in level-events.js — add a strip rule");
}

const dataUrl =
  "data:text/javascript;base64," + Buffer.from(rewritten, "utf8").toString("base64");
const { LevelStreamer, blockLevel, LEVEL_EVENT_SCHEMA_VERSION, CLIP_ABS_THRESHOLD } =
  await import(dataUrl);

let passed = 0;
function ok() {
  passed += 1;
}

// A controllable ms clock so posting cadence is deterministic.
function makeClock() {
  const state = { t: 0 };
  return { now: () => state.t, advance: (ms) => (state.t += ms), state };
}

// --- blockLevel agrees with the Pi ------------------------------------------

{
  // Full-scale sample → clip true, ~0 dBFS.
  const level = blockLevel(new Float32Array([1.0, -1.0, 0.5]));
  assert.equal(level.clip, true);
  assert.ok(level.peak_dbfs > -0.1);
  ok();

  // Silence → floor, no clip.
  const silent = blockLevel(new Float32Array([0, 0, 0, 0]));
  assert.equal(silent.clip, false);
  assert.equal(silent.rms_dbfs, -120);
  ok();

  // A -20 dBFS sine-ish level: rms of a constant 0.1 is 0.1 → -20 dBFS.
  const q = blockLevel(new Float32Array([0.1, 0.1, 0.1, 0.1]));
  assert.ok(Math.abs(q.rms_dbfs - -20) < 0.01);
  assert.equal(q.clip, false);
  ok();

  // Just below the clip threshold → no clip.
  const nearly = blockLevel(new Float32Array([CLIP_ABS_THRESHOLD - 0.01]));
  assert.equal(nearly.clip, false);
  ok();
}

// --- schema version is pinned to the Pi -------------------------------------

{
  assert.equal(LEVEL_EVENT_SCHEMA_VERSION, 1);
  ok();
}

// --- batching: many samples, few posts, superset envelope -------------------

{
  const clock = makeClock();
  const posts = [];
  const streamer = new LevelStreamer({
    now: clock.now,
    postEvent: async (e) => {
      posts.push(JSON.parse(JSON.stringify(e)));
    },
    blockMs: 100,
    postIntervalMs: 500,
    windowMs: 2000,
    sampleRate: 48000,
    agcFrozen: true,
  });
  streamer.setArmed(true);

  // Feed 2 s of audio in 100 ms blocks (4800 samples each). Advance the clock as
  // we go so post cadence and window trimming behave like wall-clock.
  const blockSamples = Math.round((100 / 1000) * 48000);
  for (let i = 0; i < 20; i++) {
    const frame = new Float32Array(blockSamples).fill(0.1); // -20 dBFS
    await streamer.addFrame(frame);
    clock.advance(100);
  }

  // 20 blocks over 2 s at <= 2 Hz → at most ~4-5 posts, NOT 20.
  assert.ok(posts.length >= 1, "expected at least one batch post");
  assert.ok(
    posts.length <= 6,
    `expected batched posts (<= ~2 Hz), got ${posts.length}`,
  );
  ok();

  // Every batch carries the superset envelope.
  for (const p of posts) {
    assert.equal(p.level_batch.schema, LEVEL_EVENT_SCHEMA_VERSION);
    assert.equal(p.level_batch.armed, true);
    assert.equal(p.level_batch.aborted, false);
    assert.equal(p.level_batch.agc_frozen, true);
    assert.ok(Array.isArray(p.level_batch.samples));
    for (const s of p.level_batch.samples) {
      assert.ok(Math.abs(s.rms_dbfs - -20) < 0.2);
      assert.equal(typeof s.seq, "number");
      assert.equal(typeof s.t_client_ms, "number");
    }
  }
  ok();

  // The rolling window stays time-bounded: the last batch spans <= windowMs.
  const last = posts[posts.length - 1].level_batch.samples;
  if (last.length >= 2) {
    const span = last[last.length - 1].t_client_ms - last[0].t_client_ms;
    assert.ok(span <= 2000, `window span ${span} exceeds windowMs`);
  }
  ok();
}

// --- agc_frozen=false is carried on every sample + the batch ----------------

{
  const clock = makeClock();
  const posts = [];
  const streamer = new LevelStreamer({
    now: clock.now,
    postEvent: async (e) => posts.push(e),
    blockMs: 100,
    postIntervalMs: 100,
    agcFrozen: false,
  });
  const blockSamples = Math.round((100 / 1000) * 48000);
  await streamer.addFrame(new Float32Array(blockSamples).fill(0.05));
  clock.advance(200);
  await streamer.addFrame(new Float32Array(blockSamples).fill(0.05));
  assert.ok(posts.length >= 1);
  const b = posts[posts.length - 1].level_batch;
  assert.equal(b.agc_frozen, false);
  for (const s of b.samples) assert.equal(s.agc_frozen, false);
  ok();
}

// --- abort posts a superset immediately -------------------------------------

{
  const clock = makeClock();
  const posts = [];
  const streamer = new LevelStreamer({
    now: clock.now,
    postEvent: async (e) => posts.push(e),
    blockMs: 100,
    postIntervalMs: 5000, // large: normal posts wouldn't fire
  });
  await streamer.abort("backgrounded");
  const b = posts[posts.length - 1].level_batch;
  assert.equal(b.aborted, true);
  assert.equal(b.abort_reason, "backgrounded");
  ok();

  // After abort the streamer is closed: further frames don't post.
  const before = posts.length;
  await streamer.addFrame(new Float32Array(4800).fill(0.1));
  assert.equal(posts.length, before);
  ok();
}

// --- a failing postEvent never throws out of addFrame -----------------------

{
  const clock = makeClock();
  const streamer = new LevelStreamer({
    now: clock.now,
    postEvent: async () => {
      throw new Error("relay down");
    },
    blockMs: 100,
    postIntervalMs: 100,
  });
  const blockSamples = Math.round((100 / 1000) * 48000);
  await streamer.addFrame(new Float32Array(blockSamples).fill(0.1)); // must not throw
  ok();
}

console.log(JSON.stringify({ ok: true, passed }));
