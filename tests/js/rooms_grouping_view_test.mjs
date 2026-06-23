// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Unit tests for the pure /rooms bond-card presentation helpers.
//
// grouping-view.js is a dependency-free ES module (no DOM, no imports), so
// node can import it directly — no harness/DOM stubbing needed (mirrors
// active_speaker_ui_test.mjs). Run via tests/test_web_rooms_setup.py.
import assert from "node:assert/strict";

import {
  airplayLipSyncRow,
  subCornerLabel,
} from "../../deploy/assets/rooms/js/grouping-view.js";

// No row unless this speaker is an active bonded leader: read error (null),
// solo/follower ({applicable:false}), or a malformed payload.
assert.equal(airplayLipSyncRow(null), null);
assert.equal(airplayLipSyncRow(undefined), null);
assert.equal(airplayLipSyncRow({ applicable: false }), null);
assert.equal(airplayLipSyncRow("nope"), null);
assert.equal(airplayLipSyncRow([1, 2]), null); // array: no .applicable

// Active leader, fits: quiet "Synced", ok tone, no note.
{
  const r = airplayLipSyncRow({ applicable: true, tight: false, residual_lag_sec: 0 });
  assert.equal(r.label, "Synced");
  assert.equal(r.tone, "var(--status-ok)");
  assert.equal(r.note, null);
}

// Active leader, tight: amber "Lagging ~550 ms", warn tone, note names the lag.
{
  const r = airplayLipSyncRow({ applicable: true, tight: true, residual_lag_sec: 0.55 });
  assert.equal(r.label, "Lagging ~550 ms");
  assert.equal(r.tone, "var(--status-warn)");
  assert.ok(r.note.includes("550 ms"), r.note);
  assert.ok(/can't be changed locally/.test(r.note), r.note);
}

// tight undefined (malformed) => treated as not tight, never crashes.
{
  const r = airplayLipSyncRow({ applicable: true, residual_lag_sec: 0.55 });
  assert.equal(r.label, "Synced");
  assert.equal(r.note, null);
}

// ms formatting is robust: missing / non-numeric residual => 0, never NaN;
// float artifacts are rounded.
assert.equal(
  airplayLipSyncRow({ applicable: true, tight: true }).label, "Lagging ~0 ms");
assert.equal(
  airplayLipSyncRow({ applicable: true, tight: true, residual_lag_sec: "x" }).label,
  "Lagging ~0 ms");
assert.equal(
  airplayLipSyncRow({ applicable: true, tight: true, residual_lag_sec: 0.5500003 }).label,
  "Lagging ~550 ms");

// subCornerLabel: a "sub" NEVER plays full-range, so a missing / invalid /
// non-positive corner falls back to 80 Hz — never blank, never "full-range".
assert.equal(subCornerLabel(80), "80 Hz low-pass");
assert.equal(subCornerLabel(120), "120 Hz low-pass");
assert.equal(subCornerLabel(40), "40 Hz low-pass");
assert.equal(subCornerLabel(99.6), "100 Hz low-pass"); // rounds for display
assert.equal(subCornerLabel(undefined), "80 Hz low-pass"); // fail-safe default
assert.equal(subCornerLabel(null), "80 Hz low-pass");
assert.equal(subCornerLabel("x"), "80 Hz low-pass"); // non-numeric
assert.equal(subCornerLabel(0), "80 Hz low-pass"); // non-positive
assert.equal(subCornerLabel(-50), "80 Hz low-pass");

console.log(JSON.stringify({ ok: true }));
