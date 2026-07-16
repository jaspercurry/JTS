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
// `armed`, `aborted`, and `run_token` state on every post, so a Pi ramp-control
// host event clobbered by an interleaving phone post (the read-modify-write
// race) never strands the flow — the phone's state is always visible in its own
// event, and the Pi's feed can scope events to THIS run via the token (a stale
// previous-run slot must never cancel or feed a retry).
//
// Posts are SERIALIZED through a single-flight chain: the relay slot is
// last-write-wins, so an abort posted while a level batch is still in flight
// must land strictly AFTER it — otherwise the stale batch's aborted:false
// overwrites the abort and the Pi blind-waits. abort() additionally RE-POSTS
// the aborted superset a bounded number of times (the Pi may also be racing its
// own host events into the same slot).
//
// Pure and testable: inject `now`, `postEvent`, `delay`, and (for tests) drive
// `addFrame` directly with Float32 chunks. dBFS is computed exactly like the
// Pi's `quality._dbfs` via the shared `rmsToDbfs`.

import { rmsToDbfs, delayMs } from "./measurement-audio.js?v=20260630-1";

// Level-event schema version — MUST match
// jasper.audio_measurement.ramp.LEVEL_EVENT_SCHEMA_VERSION. Bump both together.
export const LEVEL_EVENT_SCHEMA_VERSION = 1;

// A sample is at digital full scale when |x| reaches this — matches the Pi's
// QualityModel.clip_abs_threshold so clip detection agrees on both ends.
export const CLIP_ABS_THRESHOLD = 0.999;

// How many extra times abort() re-posts the aborted superset (spaced by
// postIntervalMs) after the first post. Bounded — no timers leak past abort().
export const ABORT_REPOSTS = 4;

// Pi-side RampState values that end one level-ramp run. Keep this intentionally
// small and local to the wire protocol: in-progress states are still surfaced
// to the UI, but only these states stop the phone microphone.
export const RAMP_TERMINAL_STATES = Object.freeze([
  "locked",
  "maxed_out",
  "aborted",
  "cancelled",
  "error",
]);
const RAMP_TERMINAL_STATE_SET = new Set(RAMP_TERMINAL_STATES);

// A status read is observational and the whole ramp is already bounded by both
// the Pi safety timeout and this page's duration_ms.  Keep the mic stream alive
// across an ordinary network fetch failure, relay throttling, or relay 5xx; a
// credential/session 4xx remains immediately fatal.
export function retryableRelayStatusError(error) {
  const status = Number(error && error.status);
  if (Number.isFinite(status)) return status === 429 || status >= 500;
  return error instanceof TypeError || (error && error.name === "AbortError");
}

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
  //   delay(ms)         — async delay (defaults to the shared delayMs); tests
  //                       inject an instant resolver.
  //   runToken          — the per-run nonce from the spec's `run_token`; echoed
  //                       on every batch so the Pi scopes events to this run.
  //   blockMs           — mic-block granularity per emitted sample (default 200).
  //   postIntervalMs    — min gap between relay posts (default 500 → <=2 Hz).
  //   windowMs          — rolling window kept in each batch (default 3000).
  //   sampleRate        — mic sample rate (default 48000).
  //   agcFrozen         — realized autoGainControl:false (false = the browser
  //                       either reported AGC on, or never reports the
  //                       setting at all — see agcUnattested).
  //   agcUnattested     — true when the browser could not attest autoGainControl
  //                       either way (undefined/null — every WebKit build
  //                       today). Rides alongside agcFrozen:false (never
  //                       agcFrozen:true) so an older Pi that does not know
  //                       this field still falls back to its pre-existing
  //                       "never trust" handling instead of silently trusting
  //                       an unproven chain. A new-Pi server verifies chain
  //                       linearity from the ramp's own staircase instead of
  //                       requiring the browser flag — see ramp.py.
  //   context           — optional compact JSON metadata repeated in every
  //                       batch (validated setup identity + realized device).
  //                       Raw serials/calibration text are forbidden here.
  //   onLevel(level)    — optional display-only callback for each aggregated
  //                       block; never participates in the control decision.
  constructor(opts = {}) {
    this._postEvent = opts.postEvent || (async () => {});
    this._now = opts.now || (() => Date.now());
    this._delay = opts.delay || delayMs;
    this._runToken = String(opts.runToken || "");
    this._blockMs = opts.blockMs || 200;
    this._postIntervalMs = opts.postIntervalMs || 500;
    this._windowMs = opts.windowMs || 3000;
    this._sampleRate = opts.sampleRate || 48000;
    this._agcFrozen = opts.agcFrozen !== false;
    this._agcUnattested = Boolean(opts.agcUnattested);
    this._context = compactLevelContext(opts.context);
    this._onLevel = typeof opts.onLevel === "function" ? opts.onLevel : () => {};

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
    // Single-flight post chain: every relay write is appended here so an abort
    // is strictly ordered after any in-flight batch post.
    this._postChain = Promise.resolve();
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
      try {
        this._onLevel(level);
      } catch {
        // Display-only callback: a broken meter must never stop the safety feed.
      }
      this._seq += 1;
      this._window.push({
        seq: this._seq,
        t_client_ms: Math.round(this._now()),
        rms_dbfs: round1(level.rms_dbfs),
        peak_dbfs: round1(level.peak_dbfs),
        clip: level.clip,
        agc_frozen: this._agcFrozen,
        agc_unattested: this._agcUnattested,
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
    const batch = {
      schema: LEVEL_EVENT_SCHEMA_VERSION,
      run_token: this._runToken,
      samples: this._window.slice(),
      agc_frozen: this._agcFrozen,
      agc_unattested: this._agcUnattested,
      armed: this._armed,
      aborted: this._aborted,
      abort_reason: this._abortReason,
    };
    if (this._context) batch.context = this._context;
    return {
      level_batch: batch,
    };
  }

  // Append one relay write to the single-flight chain. The chain never rejects
  // (a dropped post is not fatal — the superset on the NEXT post is the
  // backstop), so ordering is preserved even across failed posts.
  _enqueuePost() {
    const payload = this._batchPayload();
    this._postChain = this._postChain.then(async () => {
      try {
        await this._postEvent(payload);
      } catch (err) {
        // Swallow — never break the capture leg on a dropped relay post.
      }
    });
    return this._postChain;
  }

  async _maybePost(force = false) {
    if (this._closed && !force) return;
    const t = this._now();
    if (!force && t - this._lastPostMs < this._postIntervalMs) return;
    if (!force && this._window.length === 0) return;
    this._lastPostMs = t;
    await this._enqueuePost();
  }

  // Post the phone's abort state as a superset so a lost one-shot abort event
  // never strands the Pi. Serialized after any in-flight batch post (an
  // interleaved pre-abort batch must not be the last write), then RE-POSTED a
  // bounded number of times spaced by postIntervalMs — the Pi's own host-event
  // writes race the same read-modify-write slot. Safe to call once on teardown;
  // idempotent.
  async abort(reason = "phone_aborted") {
    if (this._aborted) return;
    this._aborted = true;
    this._abortReason = String(reason || "phone_aborted");
    this._closed = true; // no further level batches; abort posts only
    await this._enqueuePost();
    for (let i = 0; i < ABORT_REPOSTS; i++) {
      await this._delay(this._postIntervalMs);
      await this._enqueuePost();
    }
  }

  // Final flush (e.g. when the Pi posts a terminal ramp state). Sends whatever is
  // in the window immediately, then stops.
  async close() {
    if (this._closed) return;
    this._closed = true;
    await this._enqueuePost();
  }
}

// Extract the Pi's ramp progress from phone-safe relay status. A run token is
// the stale-event fence: a terminal state from a previous tap must never stop a
// new microphone stream. Returns null for unrelated/malformed host events.
export function rampEventFromStatus(status, runToken = "") {
  const hostEvent = status && typeof status === "object" ? status.host_event : null;
  const ramp = hostEvent && typeof hostEvent === "object" ? hostEvent.ramp : null;
  if (!ramp || typeof ramp !== "object") return null;
  const expectedToken = String(runToken || "");
  const actualToken = String(ramp.run_token || "");
  if (expectedToken && actualToken !== expectedToken) return null;
  const state = String(ramp.state || "");
  if (!state) return null;
  const event = {
    state,
    terminal: RAMP_TERMINAL_STATE_SET.has(state),
  };
  if (typeof ramp.error === "string" && ramp.error.trim()) {
    event.error = ramp.error.trim().slice(0, 300);
  }
  return event;
}

// Run the meter-only phone leg for one level_ramp spec. This is deliberately a
// separate protocol from the WAV capture path: it repeatedly samples short
// mono blocks, feeds LevelStreamer, and stops only on the Pi's token-scoped
// terminal ramp event. It has no content key and no blob-upload dependency, so
// a level stage cannot accidentally turn into a recording upload.
//
// opts:
//   client             — RelayClient-like {postEvent, fetchPhoneStatus}.
//   recorder           — createMonoRecorder() result.
//   spec               — kind=level_ramp capture spec.
//   agcFrozen          — realized autoGainControl:false.
//   agcUnattested      — true when the browser could not attest AGC either way.
//   context            — selected setup + realized capture device metadata.
//   onLevel(level)     — optional display callback.
//   onProgress(event)  — optional Pi progress callback.
//   onStreamer(value)  — exposes the streamer so visibility teardown can abort.
//   isAborted()        — true after an external teardown.
//   now/delay          — injectable clock for tests.
export async function runLevelRampProtocol(opts = {}) {
  const client = opts.client;
  const recorder = opts.recorder;
  const spec = opts.spec || {};
  if (!client || typeof client.postEvent !== "function") {
    throw new Error("level ramp relay client is unavailable");
  }
  if (!client.fetchPhoneStatus || typeof client.fetchPhoneStatus !== "function") {
    throw new Error("level ramp status reader is unavailable");
  }
  if (!recorder || typeof recorder.start !== "function" || typeof recorder.stop !== "function") {
    throw new Error("level ramp microphone is unavailable");
  }
  if (spec.kind && spec.kind !== "level_ramp") {
    throw new Error("level ramp protocol received the wrong capture kind");
  }

  const now = typeof opts.now === "function" ? opts.now : () => Date.now();
  const delay = typeof opts.delay === "function" ? opts.delay : delayMs;
  const isAborted = typeof opts.isAborted === "function" ? opts.isAborted : () => false;
  const blockMs = Math.max(100, Math.min(500, Number(opts.blockMs) || 200));
  const durationMs = Math.max(1000, Number(spec.duration_ms) || 75000);
  // The deadline is a per-PHASE budget, not a single tap-anchored one. It is
  // armed at Start for the pre-ramp phase (setup echo, the Pi's ambient
  // baseline and DSP commissioning load) and RE-ARMED once when the Pi's ramp
  // first becomes visible for this run token. The server's own safety timeout
  // (MeasurementRamp.safety_timeout) is anchored at ramp start, so a deadline
  // anchored at the Start tap silently shrinks the ramp's budget by however
  // long the pre-ramp phase took — the 2026-07-15 JTS3 false-timeout class.
  let deadline = now() + durationMs;
  let rampSeen = false;
  const streamer = opts.streamer || new LevelStreamer({
    postEvent: (event) => client.postEvent(event),
    runToken: spec.run_token || "",
    blockMs,
    postIntervalMs: Math.max(250, Number(spec.progress_poll_ms) || 500),
    sampleRate: Number(spec.sample_rate_hz) || 48000,
    agcFrozen: opts.agcFrozen !== false,
    agcUnattested: Boolean(opts.agcUnattested),
    context: opts.context,
    onLevel: opts.onLevel,
    now,
    delay,
  });
  if (typeof opts.onStreamer === "function") opts.onStreamer(streamer);
  streamer.setArmed(true);

  try {
    while (now() < deadline) {
      if (isAborted()) return { state: "aborted", terminal: true };
      recorder.start();
      await delay(blockMs);
      if (isAborted()) return { state: "aborted", terminal: true };
      const frames = await recorder.stop({ timeoutMs: 5000 });
      await streamer.addFrame(frames);

      let phoneStatus;
      try {
        phoneStatus = await client.fetchPhoneStatus();
      } catch (err) {
        if (!retryableRelayStatusError(err)) throw err;
        continue;
      }
      const ramp = rampEventFromStatus(phoneStatus, spec.run_token || "");
      if (ramp && !rampSeen) {
        rampSeen = true;
        deadline = now() + durationMs;
      }
      if (ramp && typeof opts.onProgress === "function") opts.onProgress(ramp);
      if (ramp && ramp.terminal) {
        await streamer.close();
        return ramp;
      }
    }
  } catch (err) {
    if (!isAborted()) await streamer.abort("phone_error");
    throw err;
  }

  await streamer.abort("phone_timeout");
  throw new Error(
    "timed out waiting for the speaker's level-check result — this is a " +
      "phone/relay timeout, not a failed measurement",
  );
}

function cloneJsonObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  try {
    const copy = JSON.parse(JSON.stringify(value));
    return copy && typeof copy === "object" && !Array.isArray(copy) ? copy : null;
  } catch {
    return null;
  }
}

function compactLevelContext(value) {
  const context = cloneJsonObject(value);
  if (!context) return null;
  const setup = context.setup;
  if (setup && typeof setup === "object") {
    const binding = setup.binding;
    const calibration = setup.calibration;
    if (
      calibration &&
      typeof calibration === "object" &&
      (Object.prototype.hasOwnProperty.call(calibration, "content") ||
        Object.prototype.hasOwnProperty.call(calibration, "serial"))
    ) {
      throw new Error(
        "level batches require a validated compact setup binding, not raw calibration data",
      );
    }
    if (
      !binding ||
      typeof binding !== "object" ||
      binding.schema !== 1 ||
      typeof binding.binding_id !== "string" ||
      !binding.binding_id ||
      typeof binding.sha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(binding.sha256)
    ) {
      throw new Error("level batches require a validated compact setup binding");
    }
  }
  return context;
}

function round1(x) {
  return Math.round(x * 10) / 10;
}
