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
