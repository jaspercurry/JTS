// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Phone-side batched level-event emitter for the level-match ramp (P2, §3.1).
//
// The Pi plays a quiet-start staircase and its RampController (Pi-side) needs a
// DENSE mic-level series to recover the chain gain and settle into the safe
// window. The relay `event` slot is last-write-wins and the Pi polls it at
// ~0.75 s, so singular per-sample events would be decimated to ~1 Hz with every
// intervening post silently lost. This emitter instead accumulates a rolling
// window of client-timestamped samples and posts them as BATCHED arrays at
// <= 2 Hz over the existing `postEvent` envelope — zero relay schema change, a
// richer payload in the same slot.
//
// The batch is a SUPERSET envelope: it carries the phone's own `agc_frozen`,
// `armed`, and `aborted` state on every post, so a Pi ramp-control host event
// that gets clobbered by an interleaving phone post (the read-modify-write race)
// never strands the flow — the phone's state is always visible in its own event.
//
// Pure and testable: inject `now`, `postEvent`, and (for tests) drive `addFrame`
// directly with Float32 chunks. dBFS is computed exactly like the Pi's
// `quality._dbfs` via the shared `rmsToDbfs`.

import { rmsToDbfs } from "./measurement-audio.js?v=20260630-1";

// Level-event schema version — MUST match
// jasper.audio_measurement.ramp.LEVEL_EVENT_SCHEMA_VERSION. Bump both together.
export const LEVEL_EVENT_SCHEMA_VERSION = 1;

// A sample is at digital full scale when |x| reaches this — matches the Pi's
// QualityModel.clip_abs_threshold so clip detection agrees on both ends.
export const CLIP_ABS_THRESHOLD = 0.999;

// Compute one {rms_dbfs, peak_dbfs, clip} verdict for a block of mono samples.
// Exported for direct unit testing.
export function blockLevel(samples) {
  const input =
    samples instanceof Float32Array ? samples : new Float32Array(samples || []);
  if (input.length === 0) {
    return { rms_dbfs: -120, peak_dbfs: -120, clip: false };
  }
  let sumSquares = 0;
  let peak = 0;
  for (let i = 0; i < input.length; i++) {
    const x = input[i];
    sumSquares += x * x;
    const a = Math.abs(x);
    if (a > peak) peak = a;
  }
  const rms = Math.sqrt(sumSquares / input.length);
  return {
    rms_dbfs: rmsToDbfs(rms),
    peak_dbfs: rmsToDbfs(peak),
    clip: peak >= CLIP_ABS_THRESHOLD,
  };
}

export class LevelStreamer {
  // opts:
  //   postEvent(event)  — async; posts the batch over the relay `event` slot.
  //   now()             — ms clock (defaults to Date.now); injected in tests.
  //   blockMs           — mic-block granularity per emitted sample (default 200).
  //   postIntervalMs    — min gap between relay posts (default 500 → <=2 Hz).
  //   windowMs          — rolling window kept in each batch (default 3000).
  //   sampleRate        — mic sample rate (default 48000).
  //   agcFrozen         — realized autoGainControl:false (false = iOS ignored it).
  constructor(opts = {}) {
    this._postEvent = opts.postEvent || (async () => {});
    this._now = opts.now || (() => Date.now());
    this._blockMs = opts.blockMs || 200;
    this._postIntervalMs = opts.postIntervalMs || 500;
    this._windowMs = opts.windowMs || 3000;
    this._sampleRate = opts.sampleRate || 48000;
    this._agcFrozen = opts.agcFrozen !== false;

    this._blockSamples = Math.max(
      1,
      Math.round((this._blockMs / 1000) * this._sampleRate),
    );
    this._pending = []; // Float32 tail not yet aggregated into a block
    this._pendingLen = 0;
    this._window = []; // rolling [{seq,t_client_ms,rms_dbfs,peak_dbfs,clip,agc_frozen}]
    this._seq = 0;
    this._lastPostMs = -Infinity;
    this._armed = false;
    this._aborted = false;
    this._abortReason = "";
    this._closed = false;
  }

  setArmed(armed) {
    this._armed = Boolean(armed);
  }

  // Feed a block of mono samples (Float32Array or array). Aggregates into fixed
  // ~blockMs samples, appends to the rolling window, and posts a batch when the
  // post interval has elapsed. Returns a promise that resolves once any due post
  // settles (so a caller can await backpressure).
  async addFrame(frames) {
    if (this._closed) return;
    const input =
      frames instanceof Float32Array ? frames : new Float32Array(frames || []);
    this._pending.push(input);
    this._pendingLen += input.length;
    while (this._pendingLen >= this._blockSamples) {
      const block = this._takeBlock(this._blockSamples);
      const level = blockLevel(block);
      this._seq += 1;
      this._window.push({
        seq: this._seq,
        t_client_ms: Math.round(this._now()),
        rms_dbfs: round1(level.rms_dbfs),
        peak_dbfs: round1(level.peak_dbfs),
        clip: level.clip,
        agc_frozen: this._agcFrozen,
      });
      this._trimWindow();
    }
    await this._maybePost();
  }

  _takeBlock(n) {
    const out = new Float32Array(n);
    let filled = 0;
    while (filled < n && this._pending.length) {
      const head = this._pending[0];
      const need = n - filled;
      if (head.length <= need) {
        out.set(head, filled);
        filled += head.length;
        this._pending.shift();
      } else {
        out.set(head.subarray(0, need), filled);
        this._pending[0] = head.subarray(need);
        filled += need;
      }
    }
    this._pendingLen -= n;
    return out;
  }

  _trimWindow() {
    const cutoff = this._now() - this._windowMs;
    while (this._window.length && this._window[0].t_client_ms < cutoff) {
      this._window.shift();
    }
  }

  _batchPayload() {
    return {
      level_batch: {
        schema: LEVEL_EVENT_SCHEMA_VERSION,
        samples: this._window.slice(),
        agc_frozen: this._agcFrozen,
        armed: this._armed,
        aborted: this._aborted,
        abort_reason: this._abortReason,
      },
    };
  }

  async _maybePost(force = false) {
    if (this._closed && !force) return;
    const t = this._now();
    if (!force && t - this._lastPostMs < this._postIntervalMs) return;
    if (!force && this._window.length === 0) return;
    this._lastPostMs = t;
    try {
      await this._postEvent(this._batchPayload());
    } catch (err) {
      // The phone superset on the NEXT post is the backstop; a dropped post is
      // not fatal to the ramp. Swallow — never break the capture leg on it.
    }
  }

  // Post the phone's abort state as a superset so a lost one-shot abort event
  // never strands the Pi. Idempotent; safe to call once on teardown.
  async abort(reason = "phone_aborted") {
    this._aborted = true;
    this._abortReason = String(reason || "phone_aborted");
    await this._maybePost(true);
    this._closed = true;
  }

  // Final flush (e.g. when the Pi posts a terminal ramp state). Sends whatever is
  // in the window immediately, then stops.
  async close() {
    await this._maybePost(true);
    this._closed = true;
  }
}

function round1(x) {
  return Math.round(x * 10) / 10;
}
