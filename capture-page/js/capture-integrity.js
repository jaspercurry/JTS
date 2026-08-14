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
// stall but NOT a resync inside the browser's upstream microphone FIFO. When
// `focus_lost` is true and `block_gaps` is 0, that asymmetry is itself the
// finding — it places the discontinuity upstream of the worklet.
//
// `encodedFrames` (issue #2094) is the number of frames this page is about to
// hand the WAV encoder — `samples.length` at the call site. It is the page's
// half of an END-TO-END frame ledger the host closes against its own count of
// the decoded capture, so a future loss reports itself with the losing hop
// named instead of being found by WAV forensics weeks later. It is passed in
// rather than read off `stats` on purpose: `stats.frames` is what the WORKLET
// assembled and this is what SURVIVED the transfer to the page, and a ledger
// whose two ends came from the same measurement would check nothing.
export function summarizeCaptureIntegrity({
  watch = null,
  stats = null,
  encodedFrames = null,
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
