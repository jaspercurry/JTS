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
  "const rmsToDbfs = (rms) => { const v = Number(rms); return v > 0 ? 20 * Math.log10(v) : -120; };\n" +
    "const delayMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));\n",
);
if (/^import\s/m.test(rewritten)) {
  throw new Error("unhandled import in level-events.js — add a strip rule");
}

const dataUrl =
  "data:text/javascript;base64," + Buffer.from(rewritten, "utf8").toString("base64");
const {
  LevelStreamer,
  blockLevel,
  LEVEL_EVENT_SCHEMA_VERSION,
  CLIP_ABS_THRESHOLD,
  ABORT_REPOSTS,
  RAMP_TERMINAL_STATES,
  rampEventFromStatus,
  retryableRelayStatusError,
  runLevelRampProtocol,
} = await import(dataUrl);

let passed = 0;
function ok() {
  passed += 1;
}

// A controllable ms clock so posting cadence is deterministic.
function makeClock() {
  const state = { t: 0 };
  return { now: () => state.t, advance: (ms) => (state.t += ms), state };
}

function setupBinding(id = "flow-123456789012") {
  return {
    schema: 1,
    binding_id: id,
    sha256: "a".repeat(64),
  };
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

// Raw calibration secrets/text must never ride the repeated meter batch.
{
  assert.throws(
    () => new LevelStreamer({
      context: {
        setup: {
          calibration: { mode: "upload", filename: "mic.cal", content: "20 0" },
        },
      },
    }),
    /validated compact setup binding/,
  );
  assert.throws(
    () => new LevelStreamer({
      context: {
        setup: { calibration: { mode: "serial", serial: "secret-serial" } },
      },
    }),
    /validated compact setup binding/,
  );
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
    context: {
      setup: { binding: setupBinding() },
      device: { label: "USB measurement mic", device_id: "mic-1" },
    },
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
    assert.deepEqual(p.level_batch.context, {
      setup: { binding: setupBinding() },
      device: { label: "USB measurement mic", device_id: "mic-1" },
    });
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

// --- run token rides every batch ---------------------------------------------

{
  const clock = makeClock();
  const posts = [];
  const streamer = new LevelStreamer({
    now: clock.now,
    postEvent: async (e) => posts.push(e),
    blockMs: 100,
    postIntervalMs: 100,
    runToken: "run-42",
  });
  const blockSamples = Math.round((100 / 1000) * 48000);
  await streamer.addFrame(new Float32Array(blockSamples).fill(0.1));
  clock.advance(200);
  await streamer.addFrame(new Float32Array(blockSamples).fill(0.1));
  assert.ok(posts.length >= 1);
  for (const p of posts) assert.equal(p.level_batch.run_token, "run-42");
  ok();
}

// --- abort posts a superset and RE-POSTS it (bounded) ------------------------

{
  const clock = makeClock();
  const posts = [];
  const delays = [];
  const streamer = new LevelStreamer({
    now: clock.now,
    postEvent: async (e) => posts.push(JSON.parse(JSON.stringify(e))),
    delay: async (ms) => {
      delays.push(ms);
      clock.advance(ms);
    },
    blockMs: 100,
    postIntervalMs: 5000, // large: normal posts wouldn't fire
    context: {
      setup: { binding: setupBinding() },
      device: { label: "Phone mic", device_id: "default" },
    },
  });
  await streamer.abort("backgrounded");
  // 1 immediate + ABORT_REPOSTS re-posts, all carrying the aborted superset —
  // a single one-shot can be clobbered by an in-flight pre-abort batch or a Pi
  // host-event write into the same read-modify-write slot.
  assert.equal(posts.length, 1 + ABORT_REPOSTS);
  for (const p of posts) {
    assert.equal(p.level_batch.aborted, true);
    assert.equal(p.level_batch.abort_reason, "backgrounded");
    assert.deepEqual(p.level_batch.context.setup.binding, setupBinding());
    assert.equal(p.level_batch.context.device.device_id, "default");
  }
  assert.equal(delays.length, ABORT_REPOSTS);
  ok();

  // After abort the streamer is closed: further frames don't post.
  const before = posts.length;
  await streamer.addFrame(new Float32Array(4800).fill(0.1));
  assert.equal(posts.length, before);
  ok();

  // abort() is idempotent — a second call adds nothing.
  await streamer.abort("again");
  assert.equal(posts.length, before);
  ok();
}

// --- abort is serialized strictly AFTER an in-flight batch post ---------------

{
  const clock = makeClock();
  const resolved = [];
  let releaseFirst;
  const firstGate = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  let postCount = 0;
  const streamer = new LevelStreamer({
    now: clock.now,
    postEvent: async (e) => {
      postCount += 1;
      if (postCount === 1) await firstGate; // the in-flight pre-abort batch
      resolved.push(JSON.parse(JSON.stringify(e)));
    },
    delay: async () => {},
    blockMs: 100,
    postIntervalMs: 100,
  });
  const blockSamples = Math.round((100 / 1000) * 48000);
  // Kick off a batch post that hangs in flight (don't await it yet).
  const framePromise = streamer.addFrame(new Float32Array(blockSamples).fill(0.1));
  // Abort while the batch is in flight; the chain must order it AFTER.
  const abortPromise = streamer.abort("backgrounded");
  releaseFirst();
  await framePromise;
  await abortPromise;
  assert.ok(resolved.length >= 2);
  assert.equal(resolved[0].level_batch.aborted, false); // the pre-abort batch
  const last = resolved[resolved.length - 1].level_batch;
  assert.equal(last.aborted, true); // the abort is the strictly-last write
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

// A display-only meter failure never interrupts the safety feed.
{
  const posts = [];
  const streamer = new LevelStreamer({
    postEvent: async (event) => posts.push(event),
    onLevel: () => {
      throw new Error("meter detached");
    },
    blockMs: 100,
    postIntervalMs: 100,
  });
  await streamer.addFrame(new Float32Array(4800).fill(0.1));
  assert.equal(posts.length, 1);
  ok();
}

// --- terminal host events are token-scoped ----------------------------------

{
  assert.ok(RAMP_TERMINAL_STATES.includes("locked"));
  assert.ok(RAMP_TERMINAL_STATES.includes("error"));
  assert.equal(
    rampEventFromStatus(
      { host_event: { ramp: { state: "locked", run_token: "old" } } },
      "current",
    ),
    null,
  );
  assert.deepEqual(
    rampEventFromStatus(
      { host_event: { ramp: { state: "settling", run_token: "current" } } },
      "current",
    ),
    { state: "settling", terminal: false },
  );
  assert.deepEqual(
    rampEventFromStatus(
      {
        host_event: {
          ramp: {
            state: "error",
            run_token: "current",
            error: "microphone calibration changed",
          },
        },
      },
      "current",
    ),
    {
      state: "error",
      terminal: true,
      error: "microphone calibration changed",
    },
  );
  assert.deepEqual(
    rampEventFromStatus(
      { host_event: { ramp: { state: "locked", run_token: "current" } } },
      "current",
    ),
    { state: "locked", terminal: true },
  );
  ok();
}

// --- level_ramp is meter-only: batches + terminal, never a WAV upload --------

{
  const posts = [];
  let blobUploads = 0;
  let statusReads = 0;
  let starts = 0;
  let stops = 0;
  const progress = [];
  const client = {
    async postEvent(event) {
      posts.push(JSON.parse(JSON.stringify(event)));
    },
    async fetchPhoneStatus() {
      statusReads += 1;
      const state = statusReads < 2 ? "climbing" : "locked";
      return { host_event: { ramp: { state, run_token: "run-current" } } };
    },
    async putBlob() {
      blobUploads += 1;
    },
  };
  const recorder = {
    start() {
      starts += 1;
    },
    async stop() {
      stops += 1;
      return new Float32Array(4800).fill(0.1);
    },
  };
  const ramp = await runLevelRampProtocol({
    client,
    recorder,
    spec: {
      kind: "level_ramp",
      run_token: "run-current",
      duration_ms: 5000,
      sample_rate_hz: 48000,
    },
    blockMs: 100,
    delay: async () => {},
    context: {
      setup: { binding: setupBinding() },
      device: { label: "External mic", device_id: "external-1" },
    },
    onProgress: (event) => progress.push(event.state),
  });
  assert.deepEqual(ramp, { state: "locked", terminal: true });
  assert.equal(starts, 2);
  assert.equal(stops, 2);
  assert.equal(blobUploads, 0, "level ramp must never upload a WAV/blob");
  assert.ok(posts.length >= 2);
  assert.ok(posts.every((post) => post.level_batch));
  assert.ok(posts.every((post) => post.level_batch.run_token === "run-current"));
  assert.ok(posts.every((post) => (
    post.level_batch.context.setup.binding.sha256 === "a".repeat(64) &&
    post.level_batch.context.device.device_id === "external-1"
  )));
  assert.deepEqual(progress, ["climbing", "locked"]);
  ok();
}

// Every Pi terminal state stops streaming, not only the happy-path lock.
{
  for (const state of ["maxed_out", "aborted", "cancelled", "error"]) {
    const client = {
      async postEvent() {},
      async fetchPhoneStatus() {
        return { host_event: { ramp: { state, run_token: "run-terminal" } } };
      },
    };
    const recorder = {
      start() {},
      async stop() {
        return new Float32Array(4800).fill(0.05);
      },
    };
    const ramp = await runLevelRampProtocol({
      client,
      recorder,
      spec: {
        kind: "level_ramp",
        run_token: "run-terminal",
        duration_ms: 5000,
        sample_rate_hz: 48000,
      },
      blockMs: 100,
      delay: async () => {},
    });
    assert.deepEqual(ramp, { state, terminal: true });
  }
  ok();
}

// A transient observational status failure must not abort a safe ramp.  The
// next poll can still observe the Pi's terminal lock; credential/session 4xx
// remains fatal instead of spinning until the outer duration bound.
{
  let statusReads = 0;
  const client = {
    async postEvent() {},
    async fetchPhoneStatus() {
      statusReads += 1;
      if (statusReads === 1) throw new TypeError("Failed to fetch");
      return {
        host_event: { ramp: { state: "locked", run_token: "run-transient" } },
      };
    },
  };
  const recorder = {
    start() {},
    async stop() {
      return new Float32Array(4800).fill(0.05);
    },
  };
  const ramp = await runLevelRampProtocol({
    client,
    recorder,
    spec: {
      kind: "level_ramp",
      run_token: "run-transient",
      duration_ms: 5000,
      sample_rate_hz: 48000,
    },
    blockMs: 100,
    delay: async () => {},
  });
  assert.deepEqual(ramp, { state: "locked", terminal: true });
  assert.equal(statusReads, 2);
  assert.equal(retryableRelayStatusError({ status: 503 }), true);
  assert.equal(retryableRelayStatusError({ status: 429 }), true);
  assert.equal(retryableRelayStatusError({ name: "AbortError" }), true);
  assert.equal(retryableRelayStatusError({ status: 404 }), false);
  ok();
}

// The phone's own hard deadline (spec.duration_ms) elapsing without ever
// observing a Pi terminal ramp state is a genuine phone/relay-side timeout,
// not a failed measurement — the Pi may still be legitimately mid-ramp (the
// 2026-07-15 JTS3 crossover level-ramp incident: the phone declared a false
// timeout failure while the Pi's ramp was still running). The failure copy
// must say so, and the phone must still post an aborted superset so the Pi
// is not left blind-waiting.
{
  const clock = makeClock();
  const posts = [];
  const client = {
    async postEvent(event) {
      posts.push(JSON.parse(JSON.stringify(event)));
    },
    async fetchPhoneStatus() {
      return { host_event: { ramp: { state: "climbing", run_token: "run-deadline" } } };
    },
  };
  const recorder = {
    start() {},
    async stop() {
      return new Float32Array(4800).fill(0.05);
    },
  };
  await assert.rejects(
    runLevelRampProtocol({
      client,
      recorder,
      spec: {
        kind: "level_ramp",
        run_token: "run-deadline",
        duration_ms: 1000,
        sample_rate_hz: 48000,
      },
      blockMs: 100,
      now: clock.now,
      delay: async (ms) => clock.advance(ms),
    }),
    /timed out waiting for the speaker's level-check result/,
  );
  const last = posts[posts.length - 1];
  assert.equal(last.level_batch.aborted, true);
  assert.equal(last.level_batch.abort_reason, "phone_timeout");
  ok();
}

// Pre-ramp server overhead must not burn the ramp's own budget. The deadline
// is re-armed when the Pi's ramp first becomes visible: a run whose arming
// phase plus ramp jointly exceed a single tap-anchored budget — while each
// phase individually fits — must complete, not false-timeout (the 2026-07-15
// JTS3 class: the server's safety timeout anchors at ramp_start, so a
// tap-anchored client deadline silently shrinks the ramp budget by the
// arming overhead).
{
  const clock = makeClock();
  const client = {
    async postEvent() {},
    async fetchPhoneStatus() {
      const t = clock.now();
      if (t < 4000) return {}; // arming: ambient/DSP load, no ramp yet
      if (t < 8000) {
        return { host_event: { ramp: { state: "climbing", run_token: "run-rearm" } } };
      }
      return { host_event: { ramp: { state: "locked", run_token: "run-rearm" } } };
    },
  };
  const recorder = {
    start() {},
    async stop() {
      return new Float32Array(4800).fill(0.05);
    },
  };
  // Budget 5000 ms: arming ends at 4000 (< 5000), ramp locks 4000 ms after it
  // becomes visible (< 5000), but total 8000 > 5000 — only the re-arm passes.
  const ramp = await runLevelRampProtocol({
    client,
    recorder,
    spec: {
      kind: "level_ramp",
      run_token: "run-rearm",
      duration_ms: 5000,
      sample_rate_hz: 48000,
    },
    blockMs: 100,
    now: clock.now,
    delay: async (ms) => clock.advance(ms),
  });
  assert.deepEqual(ramp, { state: "locked", terminal: true });
  ok();
}

console.log(JSON.stringify({ ok: true, passed }));
