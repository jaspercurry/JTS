// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Screen Wake Lock + visibility-abort helpers for the capture page (step 7).
//
// iOS lifecycle (plan §10): backgrounding the tab or letting the screen lock
// KILLS the mic track, so a capture that continues would record garbage. We hold
// a Screen Wake Lock during capture to keep the screen on, and we still listen
// for `visibilitychange` — if the page is hidden anyway (the user switched apps),
// we ABORT and cue rather than upload garbage (the Pi plays the audible cue when
// it sees the aborted event).
//
// Pure where it can be: `acquireWakeLock` degrades gracefully when the API is
// absent (older iOS), and `shouldAbortOnHidden` is a trivial pure predicate.
// Unit-tested in tests/js/capture_wakelock_test.mjs.
//
// A session that spans more than one capture (the v3 plan loop, #1658) holds
// the lock across the WHOLE session rather than re-acquiring it per capture;
// `watchVisibilityReacquire` is the piece that re-requests it once the phone
// returns from a brief hide (a Control Center swipe, a notification banner)
// without tearing the session down — that class of hide is not the same as
// the capture-time "the mic track just died" case `watchVisibilityAbort`
// guards. See capture-page/js/main.js's onPlanStart/runPlanCapture.

// Acquire a screen wake lock. Returns a handle whose release() is always safe to
// call (idempotent, never throws), and `supported` says whether a real lock was
// taken. A re-acquire helper handles the documented case where iOS drops the
// lock when the page is briefly hidden and visible again.
export async function acquireWakeLock(nav) {
  const navigator_ = nav || (typeof navigator !== "undefined" ? navigator : null);
  const api = navigator_ && navigator_.wakeLock;
  if (!api || typeof api.request !== "function") {
    return { sentinel: null, supported: false, release: async () => {} };
  }
  try {
    const sentinel = await api.request("screen");
    let released = false;
    return {
      sentinel,
      supported: true,
      release: async () => {
        if (released) return;
        released = true;
        try {
          await sentinel.release();
        } catch {
          /* already released by the browser */
        }
      },
    };
  } catch {
    // Permission denied / not allowed (e.g. not a user gesture) — degrade.
    return { sentinel: null, supported: false, release: async () => {} };
  }
}

// Pure: should an in-progress capture abort given the document visibility state?
// We abort the moment the page is hidden — a backgrounded mic yields garbage.
export function shouldAbortOnHidden(visibilityState) {
  return visibilityState === "hidden";
}

// Wire visibility-abort onto a document for the duration of a capture. Calls
// `onAbort(reason)` once when the page is hidden, and returns a disposer that
// removes the listener (call it when the capture finishes). Browser-only glue;
// the decision it defers to (`shouldAbortOnHidden`) is unit-tested.
export function watchVisibilityAbort(doc, onAbort) {
  const document_ = doc || (typeof document !== "undefined" ? document : null);
  if (!document_ || typeof document_.addEventListener !== "function") {
    return () => {};
  }
  let fired = false;
  const handler = () => {
    if (fired) return;
    if (shouldAbortOnHidden(document_.visibilityState)) {
      fired = true;
      onAbort("backgrounded");
    }
  };
  document_.addEventListener("visibilitychange", handler);
  return () => document_.removeEventListener("visibilitychange", handler);
}

// The symmetric case (#1658): a wake lock is auto-released by the browser the
// moment the document goes hidden — documented Wake Lock API behavior, not a
// failure — so a session that spans more than one capture (the v3 plan loop's
// idle gaps between rounds) needs to notice when the page comes BACK to
// visible and re-request the lock. Calls `onVisible()` each time the page
// returns to visible while `isActive()` still reports the session as live
// (never after the session has ended — there is nothing left to re-acquire
// for). Returns a disposer, mirroring `watchVisibilityAbort`.
export function watchVisibilityReacquire(doc, onVisible, isActive) {
  const document_ = doc || (typeof document !== "undefined" ? document : null);
  if (!document_ || typeof document_.addEventListener !== "function") {
    return () => {};
  }
  const handler = () => {
    if (document_.visibilityState === "visible" && isActive()) onVisible();
  };
  document_.addEventListener("visibilitychange", handler);
  return () => document_.removeEventListener("visibilitychange", handler);
}
