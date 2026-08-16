// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Per-take capture-integrity accounting for the capture page (issue #2151).
//
// WHAT WENT WRONG. During the 2026-08-05 R15 hardware checkpoint, clicking out
// of the capture window mid-sweep produced discontinuities of exactly ±128
// samples — ONE Web Audio render quantum — in three consecutive takes, each
// honestly refused by the host as `drift_baselines_disagree`. Keeping the
// window frontmost eliminated it.
//
// WHY wakelock.js DID NOT CATCH IT. `watchVisibilityAbort` fires on
// `visibilitychange` → `hidden`, which is the phone case: backgrounding the tab
// KILLS the mic track, so aborting the session is right. A desktop browser that
// merely loses window focus keeps `visibilityState === "visible"` — the tab is
// still on screen — so no visibility event fires at all and the corrupted take
// uploaded silently. That gap is what this module closes: focus loss is a
// DIFFERENT, milder event than a hide, and it gets a different response.
//
// WHY THIS LABELS RATHER THAN ABORTS. By the time a take is recording, the Pi
// has already admitted the attempt (`authorize_begin` at the `begin_capture`
// post) and is playing the stimulus. The shipped relay protocol has exactly two
// exits from an armed capture: upload → verdict, or a TERMINAL error
// (`CaptureAborted` from an `aborted` event, or a `CaptureTimeout` if the blob
// never arrives). There is no non-terminal "this take is dead, give me the next
// attempt" signal. So a local abort would not return the spent attempt — it
// would destroy the whole multi-position session, which is the #2086 pathology
// ("a household told to measure again must actually be able to measure again").
// Uploading and letting the host judge is therefore the honest path, and this
// module's job is to make the take's condition VISIBLE: to the household
// immediately, and to the retained sidecar for later forensics.

// Bounded so a pathological focus-thrash cannot grow the relay event without
// limit. The first few transitions are what attribution needs; the count is
// reported separately and stays exact past the cap.
export const INTEGRITY_EVENT_CAP = 12;

// Watch one take's window/document lifecycle. Returns a handle whose `dispose()`
// is idempotent and always safe. `onLoss(kind)` fires ONCE, on the first
// integrity-relevant transition, so a caller can say something to the household
// while the sweep is still playing rather than after the verdict.
//
// Every listener is passive bookkeeping: this module never aborts, never posts,
// and never touches the recorder.
export function createIntegrityWatch({ win, doc, now, onLoss } = {}) {
  const window_ = win || (typeof window !== "undefined" ? window : null);
  const document_ = doc || (typeof document !== "undefined" ? document : null);
  const clock =
    now ||
    (typeof performance !== "undefined" && performance.now
      ? () => performance.now()
      : () => Date.now());
  const started = clock();
  const events = [];
  let losses = 0;
  let disposed = false;

  const record = (kind, isLoss) => {
    if (disposed) return;
    if (events.length < INTEGRITY_EVENT_CAP) {
      events.push({ kind, ms: Math.round(clock() - started) });
    }
    if (!isLoss) return;
    losses += 1;
    if (losses === 1 && typeof onLoss === "function") onLoss(kind);
  };

  const onBlur = () => record("blur", true);
  const onFocus = () => record("focus", false);
  const onPageHide = () => record("pagehide", true);
  const onVisibility = () => {
    const hidden = document_ && document_.visibilityState === "hidden";
    record(hidden ? "hidden" : "visible", Boolean(hidden));
  };

  const wired = [];
  if (window_ && typeof window_.addEventListener === "function") {
    window_.addEventListener("blur", onBlur);
    window_.addEventListener("focus", onFocus);
    window_.addEventListener("pagehide", onPageHide);
    wired.push(
      () => window_.removeEventListener("blur", onBlur),
      () => window_.removeEventListener("focus", onFocus),
      () => window_.removeEventListener("pagehide", onPageHide),
    );
  }
  if (document_ && typeof document_.addEventListener === "function") {
    document_.addEventListener("visibilitychange", onVisibility);
    wired.push(() =>
      document_.removeEventListener("visibilitychange", onVisibility),
    );
  }

  return {
    // True once this take saw ANY loss of foreground. Read after the recording
    // window closes; the household-facing warning rides `onLoss` instead.
    get lost() {
      return losses > 0;
    },
    get losses() {
      return losses;
    },
    events: () => events.slice(),
    dispose: () => {
      if (disposed) return;
      disposed = true;
      for (const off of wired) {
        try {
          off();
        } catch {
          /* a detached document — nothing left to remove */
        }
      }
    },
  };
}

// One Web Audio render quantum. The grid every browser's audio graph runs on,
// and therefore the grid a FIFO resync writes its silence onto.
export const ZERO_RUN_QUANTUM = 128;

// Bounded for the same reason `INTEGRITY_EVENT_CAP` is: a pathological capture
// (a mic that died mid-sweep, a buffer that is mostly silence) must not grow
// the relay event without limit. The FIRST runs are what attribution needs, and
// `zero_run_count` stays exact past the cap.
export const ZERO_RUN_RECORD_CAP = 8;

// Scan an assembled capture for runs of digital silence at least one render
// quantum long. Returns `{count, runs, quantum}`, or `null` when handed
// something that is not an indexable buffer.
//
// WHY THIS EXISTS (issue #2557, verdict 2026-08-15). Every one of 13 testable
// glitch events in the 2026-08-15 measurement campaign contained exactly one run
// of 128 consecutive digital-zero samples starting at an index ≡ 0 mod 128 — one
// whole render quantum, block-aligned, sitting in a live room's noise floor. No
// clean control contained any such run (0/3; their longest natural zero run was
// 13 samples). The zeros are the browser's own capture FIFO handing the graph a
// silent quantum, and they are present in the Float32Array BEFORE this page
// encodes it. That makes the fault a MEASURABLE fact at capture time rather than
// an inference drawn from the analyzer's residual desync days later, which is
// the whole difference between "retake this now" and "these numbers were wrong
// and nobody knew".
//
// EXACT ZEROS ONLY — no epsilon, and the absence of one is load-bearing. A real
// room's samples hit exact zero about 0.5 % of the time, so a tolerance band
// would start matching quiet passages and manufacture false positives on the
// takes it is supposed to clear; 128 consecutive EXACT zeros has a chance
// probability around 10⁻²⁹⁵. That is #1765's banked lesson in one line: a
// capture glitch is a SPLICE, never drift, and an epsilon fitted on a glitch is
// an artefact. `-0 === 0` in JS, which is right — a negative zero float is a
// digital zero; `NaN` compares false and correctly ends a run.
//
// WHAT IT CANNOT SEE, stated so nobody over-reads a zero count. A FIFO that
// DROPS samples rather than inserting silence splices the content without
// writing any zeros, so no scan of the data can witness it. And a run is not
// self-evidently a fault: the offset is reported precisely because a run at
// offset 0 has a benign reading (a graph that had not warmed up) that a boolean
// would have hidden. This module reports; it never judges.
//
// COST. One linear pass with an early state machine and no copies, over the same
// array `float32ToWavBlob` is about to walk sample by sample anyway — so the
// scan is strictly cheaper than the encode that already follows it, and it runs
// on the page thread, never on the audio render thread.
export function scanZeroFillRuns(samples, options = {}) {
  if (!samples || !Number.isFinite(samples.length)) return null;
  const quantum = Number.isFinite(options.quantum) && options.quantum > 0
    ? options.quantum : ZERO_RUN_QUANTUM;
  const cap = Number.isFinite(options.cap) && options.cap >= 0
    ? options.cap : ZERO_RUN_RECORD_CAP;
  const runs = [];
  const total = samples.length;
  let count = 0;
  let run = 0;

  // `phase` is the run's offset on the render grid, computed here beside the
  // offset it comes from so there is one writer for it and nothing downstream
  // has to know which modulus to use. Phase 0 with `len === quantum` is the
  // clean block-aligned fingerprint.
  //
  // A NONZERO PHASE DOES NOT RULE THE QUANTUM OUT, and a consumer that tested
  // `phase === 0` would miss real events. One natural ambient zero sitting
  // immediately beside a zero-filled quantum merges with it into a single run —
  // `{offset: n - 1, len: 129, phase: 127}` — which still CONTAINS a whole
  // block-aligned quantum. At the measured ambient zero density (P ≈ 0.005 per
  // sample, either neighbour) that is roughly 1 % of events, which is why all 13
  // events of the 2026-08-15 campaign happened to read phase 0 and why that
  // must not be read as a rule. So the three fields are reported together and
  // the question to ask of a run is CONTAINMENT — does `offset`/`len` cover an
  // aligned quantum — never phase alone.
  const closeRun = (end) => {
    if (run < quantum) return;
    count += 1;
    if (runs.length < cap) {
      const offset = end - run;
      runs.push({ offset: offset, len: run, phase: offset % quantum });
    }
  };

  for (let i = 0; i < total; i += 1) {
    if (samples[i] === 0) {
      run += 1;
      continue;
    }
    if (run !== 0) {
      closeRun(i);
      run = 0;
    }
  }
  closeRun(total);
  return { count: count, runs: runs, quantum: quantum };
}

// Does a reported run cover a whole BLOCK-ALIGNED render quantum?
//
// This is the question a consumer of `scanZeroFillRuns` must ask, and it lives
// here beside the scan that produces the record so there is one of it. It is
// deliberately NOT `phase === 0`: as `scanZeroFillRuns`'s own comment explains,
// one natural ambient zero abutting a zero-filled quantum merges into a single
// run whose phase is off the grid while the whole quantum is still inside it,
// so containment by `offset`/`len` is the only test that does not throw those
// events away.
export function containsAlignedQuantum(run, quantum = ZERO_RUN_QUANTUM) {
  if (!run || !Number.isFinite(run.offset) || !Number.isFinite(run.len)) return false;
  if (!Number.isFinite(quantum) || quantum <= 0) return false;
  const firstAligned = Math.ceil(run.offset / quantum) * quantum;
  return firstAligned + quantum <= run.offset + run.len;
}

// Did THIS take witness the render-quantum SPLICE — the fault worth spending an
// attempt on, as opposed to any run of zeros (issue #2557 phase B)?
//
// Reads a report built by `summarizeCaptureIntegrity` below, so a caller asks
// one question of the object it already has rather than re-deriving the rule.
//
// THREE CONDITIONS, and each one narrows a benign reading OUT rather than
// widening the trigger in:
//
//  1. The run covers a whole block-aligned quantum (above). That shape — 128
//     zeros on the render grid — is what 13 of 13 testable glitch events of the
//     2026-08-15 campaign carried and 0 of 3 clean controls did.
//  2. It starts PAST sample 0. `scanZeroFillRuns` says so itself: a run at
//     offset 0 has a benign reading (a graph that had not warmed up), and it
//     sits in the pre-sweep window in any case. A capture that is silent from
//     its first sample is a dead microphone, which is a different finding with
//     a different answer — never a splice this page should spend an attempt on.
//  3. It ends before the last sample. The witnessed fault is zeros SURROUNDED
//     by a live room's noise floor; a run that reaches the end of the buffer
//     cannot be told apart from a recorder that stopped early or padded its
//     tail, and the tail is past the sweep in any case.
//
// FAIL-CLOSED EVERYWHERE ELSE. A report with no scan (an older recorder, or a
// take that was never handed its samples) and a report with no
// `encoded_frames` to place a run inside both answer false: "not scanned" must
// never read as "scanned and found", and this answer spends a household's
// attempt.
export function witnessedZeroFillSplice(report) {
  if (!report || typeof report !== "object") return false;
  const quantum = Number(report.zero_run_quantum);
  const frames = Number(report.encoded_frames);
  if (!Number.isFinite(quantum) || quantum <= 0) return false;
  if (!Number.isFinite(frames) || frames <= 0) return false;
  const runs = Array.isArray(report.zero_runs) ? report.zero_runs : [];
  return runs.some(
    (run) =>
      containsAlignedQuantum(run, quantum) &&
      run.offset > 0 &&
      run.offset + run.len < frames,
  );
}

// Why an automatic retake fired, as one stable token on the wire. The page's
// only trigger today, and named for the mechanism (a spliced-in quantum of
// digital zeros) rather than for the issue number, so a sidecar reader a year
// from now can tell what the page believed it saw.
export const AUTO_RETAKE_ZERO_FILL_REASON = "zero_fill_splice";

// What the household reads while the page retakes a measurement on its own.
// Says what happened and what is being done about it, in that order, and names
// no browser or platform — the same sentence is true wherever the gap came
// from. Kept here beside the witness for the same reason the focus-loss copy
// is: the sentence and the mechanism it describes drift apart when they live
// in different files.
export const AUTO_RETAKE_MESSAGE =
  "That recording had a gap in it — JTS is taking this measurement again.";

// The same fact, afterwards: shown on a later screen for a measurement whose
// ONE automatic retake has already been spent, so a household looking at a
// rejection knows the page already tried once on its own and is not silently
// sitting on a spare.
export const AUTO_RETAKE_SPENT_NOTE =
  "JTS already retook this measurement once on its own after a gap in the recording.";

// Build the wire object the page posts alongside its capture.
//
// FAIL-SOFT BY CONSTRUCTION: every field is omitted when its source is absent,
// so an older recorder with no block accounting, or a call with no watch,
// yields a smaller object rather than nulls or an error. The host treats a
// missing key as "not reported", never as zero.
//
// `stats` is `recorder.captureStats()` from measurement-audio.js. Read its
// comment for what the counters can and cannot see: they measure the AUDIO
// RENDER GRAPH (quanta the worklet was handed), so they catch a render-thread
// stall but NOT a resync inside the browser's upstream microphone FIFO — and
// `silent_blocks` does not close that gap either, because it counts only the
// `process()` calls handed NO input array at all. A FIFO resync hands the
// worklet a perfectly ordinary input array that happens to be full of zeros, so
// `silent_blocks` read 0 on every one of the #2557 events. `samples` is what
// closes it: the counters describe the graph, the scan describes the DATA. When
// `focus_lost` is true and `block_gaps` is 0, that asymmetry is itself the
// finding — it places the discontinuity upstream of the worklet.
//
// `samples` is the assembled capture — the same Float32Array `encodedFrames`
// counts and the encoder is about to read. Passing it scans it (see
// `scanZeroFillRuns`); passing nothing omits the zero-run keys entirely, so an
// absent scan never reads as a clean one. Nothing on the host branches on these
// keys: they are DISCLOSURE, and the host's own residual-desync classifier is
// what refuses such a take.
//
// `encodedFrames` (issue #2094) is the number of frames this page is about to
// hand the WAV encoder — `samples.length` at the call site. It is the page's
// half of an END-TO-END frame ledger the host closes against its own count of
// the decoded capture, so a future loss reports itself with the losing hop
// named instead of being found by WAV forensics weeks later. It is passed in
// rather than read off `stats` on purpose: `stats.frames` is what the WORKLET
// assembled and this is what SURVIVED the transfer to the page, and a ledger
// whose two ends came from the same measurement would check nothing.
//
// `autoRetake` is set only on a take the PAGE started by itself, after the
// previous attempt at this measurement witnessed the splice above
// (`{reason, after_attempt}`). It is disclosure, not a request: no host reads
// it, and an older Pi records it in the operator sidecar with the rest of this
// object simply because the whole report is retained verbatim. Its absence is
// the ordinary case — a take a household started — never "unknown".
export function summarizeCaptureIntegrity({
  watch = null,
  stats = null,
  encodedFrames = null,
  samples = null,
  autoRetake = null,
} = {}) {
  const summary = {};
  if (watch) {
    summary.focus_lost = Boolean(watch.lost);
    summary.focus_losses = watch.losses;
    summary.focus_events = watch.events();
  }
  if (stats && typeof stats === "object") {
    for (const key of [
      "frames", "blocks", "block_gaps", "block_gap_frames", "silent_blocks",
    ]) {
      if (Number.isFinite(stats[key])) summary[key] = stats[key];
    }
  }
  if (Number.isFinite(encodedFrames)) summary.encoded_frames = encodedFrames;
  const zeroRuns = scanZeroFillRuns(samples);
  if (zeroRuns) {
    // A scanned capture reports its count even when that count is zero: "looked,
    // found none" is the finding a household-facing retake decision would rest
    // on, and it is exactly what an absent key must not be mistaken for.
    summary.zero_run_quantum = zeroRuns.quantum;
    summary.zero_run_count = zeroRuns.count;
    summary.zero_runs = zeroRuns.runs;
  }
  if (autoRetake && typeof autoRetake === "object") summary.auto_retake = autoRetake;
  return summary;
}

// The one household-facing sentence, kept here next to the mechanism it
// describes. Plain words, no browser or platform named — the same line is true
// for a phone that got a notification and a laptop whose window went behind
// another. It states what will happen so the retake is not a surprise.
export const INTEGRITY_LOST_FOCUS_MESSAGE =
  "Keep this page in front while the speaker plays — this measurement will be retaken.";

// The same fact, phrased for the screen that appears AFTER the host refuses the
// take. Only ever shown when this page actually observed the focus loss, so it
// is a report of something measured rather than a guess at a cause.
export const INTEGRITY_LOST_FOCUS_NOTE =
  "This page lost focus while that measurement was recording, which can break the recording.";
