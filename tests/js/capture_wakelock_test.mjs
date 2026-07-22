// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the capture page's wake-lock + visibility-abort (relay step 7).
//
// iOS kills the mic track when the page backgrounds (plan §10), so we hold a
// Screen Wake Lock during capture and ABORT+cue if the page hides anyway rather
// than upload garbage. Prints {"ok":true}.
//
//   node tests/js/capture_wakelock_test.mjs

import assert from "node:assert/strict";

import {
  acquireWakeLock,
  shouldAbortOnHidden,
  watchVisibilityAbort,
  watchVisibilityReacquire,
} from "../../capture-page/js/wakelock.js";

let passed = 0;
function ok() {
  passed += 1;
}

async function testAcquireAndRelease() {
  let releaseCount = 0;
  const nav = {
    wakeLock: {
      request: async (type) => {
        assert.equal(type, "screen");
        return {
          release: async () => {
            releaseCount += 1;
          },
        };
      },
    },
  };
  const lock = await acquireWakeLock(nav);
  assert.equal(lock.supported, true);
  await lock.release();
  await lock.release(); // idempotent — no double release
  assert.equal(releaseCount, 1);
  ok();
}

async function testDegradesWhenUnsupported() {
  const lock = await acquireWakeLock({}); // no wakeLock API (older iOS)
  assert.equal(lock.supported, false);
  await lock.release(); // safe no-op
  ok();
}

async function testDegradesWhenRequestThrows() {
  const nav = {
    wakeLock: {
      request: async () => {
        throw new Error("NotAllowedError");
      },
    },
  };
  const lock = await acquireWakeLock(nav);
  assert.equal(lock.supported, false);
  await lock.release();
  ok();
}

function testShouldAbortOnHidden() {
  assert.equal(shouldAbortOnHidden("hidden"), true);
  assert.equal(shouldAbortOnHidden("visible"), false);
  assert.equal(shouldAbortOnHidden(undefined), false);
  ok();
}

function testWatchAbortsOnceWhenHidden() {
  // Minimal document stub.
  let handler = null;
  const doc = {
    visibilityState: "visible",
    addEventListener: (ev, fn) => {
      if (ev === "visibilitychange") handler = fn;
    },
    removeEventListener: () => {
      handler = null;
    },
  };
  const reasons = [];
  const dispose = watchVisibilityAbort(doc, (r) => reasons.push(r));

  // Visible -> no abort.
  handler();
  assert.deepEqual(reasons, []);

  // Hidden -> abort once.
  doc.visibilityState = "hidden";
  handler();
  handler(); // second hide must not re-fire
  assert.deepEqual(reasons, ["backgrounded"]);

  dispose();
  assert.equal(handler, null); // listener removed
  ok();
}

function testWatchDegradesWithoutDocument() {
  const dispose = watchVisibilityAbort(null, () => {
    throw new Error("should not fire");
  });
  dispose(); // safe no-op
  ok();
}

// #1658: the v3 plan loop holds one wake lock across a whole multi-capture
// session; the browser auto-releases it on every hide, so this is the piece
// that notices the page coming BACK and re-requests it — but only while the
// session is still active.
function testReacquireFiresOnVisibleWhileActive() {
  let handler = null;
  const doc = {
    visibilityState: "hidden",
    addEventListener: (ev, fn) => {
      if (ev === "visibilitychange") handler = fn;
    },
    removeEventListener: () => {
      handler = null;
    },
  };
  let calls = 0;
  let active = true;
  const dispose = watchVisibilityReacquire(doc, () => { calls += 1; }, () => active);

  // Hidden -> never fires.
  handler();
  assert.equal(calls, 0);

  // Visible while active -> fires.
  doc.visibilityState = "visible";
  handler();
  assert.equal(calls, 1);

  // Visible again while active -> fires again (unlike watchVisibilityAbort,
  // this is not a one-shot — the page can hide/show repeatedly across a
  // multi-minute session).
  handler();
  assert.equal(calls, 2);

  // Visible but the session has since ended -> no re-acquire for a dead
  // session.
  active = false;
  handler();
  assert.equal(calls, 2);

  dispose();
  assert.equal(handler, null);
  ok();
}

function testReacquireDegradesWithoutDocument() {
  const dispose = watchVisibilityReacquire(null, () => {
    throw new Error("should not fire");
  }, () => true);
  dispose(); // safe no-op
  ok();
}

const tests = [
  testAcquireAndRelease,
  testDegradesWhenUnsupported,
  testDegradesWhenRequestThrows,
  testShouldAbortOnHidden,
  testWatchAbortsOnceWhenHidden,
  testWatchDegradesWithoutDocument,
  testReacquireFiresOnVisibleWhileActive,
  testReacquireDegradesWithoutDocument,
];

let failure = null;
for (const t of tests) {
  try {
    await t();
  } catch (e) {
    failure = { test: t.name, error: String(e && e.stack ? e.stack : e) };
    break;
  }
}

if (failure) {
  console.error(failure.error);
  console.log(JSON.stringify({ ok: false, ...failure }));
  process.exit(1);
} else {
  console.log(JSON.stringify({ ok: true, passed }));
}
