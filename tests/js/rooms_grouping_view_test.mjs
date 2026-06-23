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
  addSubPlan,
  airplayLipSyncRow,
  createFaceCopy,
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

// createFaceCopy: the title/intro/label/BUTTON must match the picked role, so a
// button reading "Create stereo pair" is never how you add a sub. Sub-specific
// for "sub"; everything else degrades to the unchanged stereo-pair copy.
{
  const sub = createFaceCopy("sub");
  assert.equal(sub.title, "Add a wireless subwoofer");
  assert.equal(sub.button, "Add subwoofer");
  assert.ok(/main/.test(sub.label), sub.label); // not "Left"
  assert.ok(/low end/.test(sub.intro), sub.intro);
  assert.ok(!/stereo pair/i.test(sub.button), sub.button);

  const pair = createFaceCopy("right");
  assert.equal(pair.title, "Create a stereo pair");
  assert.equal(pair.button, "Create stereo pair");
  assert.ok(/Left/.test(pair.label), pair.label);

  // Unknown / future role → safe stereo-pair default (never the sub copy).
  const unknown = createFaceCopy("zzz");
  assert.equal(unknown.button, "Create stereo pair");
  assert.deepEqual(unknown, pair);
}

// addSubPlan: decide whether to offer "add a subwoofer" on a bonded leader,
// and (when shown) the existing-members list to re-post as the SAME bond.
{
  // Leader/left with a RIGHT sibling in the roster (no sub) → show, and the
  // members reconstruct leader(left) + follower(right).
  const rosterPair = addSubPlan({
    enabled: true, bond_id: "bond-1", role: "leader", channel: "left",
    self_addr: "192.168.1.5",
    roster: [{ addr: "192.168.1.9", name: "Right", channel: "right" }],
  });
  assert.equal(rosterPair.show, true, JSON.stringify(rosterPair));
  assert.deepEqual(rosterPair.members, [
    { addr: "192.168.1.5", role: "leader", channel: "left" },
    { addr: "192.168.1.9", role: "follower", channel: "right", name: "Right" },
  ]);

  // A group that ALREADY contains a sub → no panel.
  const hasSub = addSubPlan({
    enabled: true, bond_id: "bond-1", role: "leader", channel: "left",
    self_addr: "192.168.1.5",
    roster: [
      { addr: "192.168.1.9", name: "Right", channel: "right" },
      { addr: "192.168.1.8", name: "Sub", channel: "sub" },
    ],
  });
  assert.equal(hasSub.show, false);
  assert.equal(hasSub.reason, "already has a sub");

  // A FOLLOWER (not the leader) → no panel.
  const follower = addSubPlan({
    enabled: true, bond_id: "bond-1", role: "follower", channel: "right",
    self_addr: "192.168.1.9", leader_addr: "jts.local",
  });
  assert.equal(follower.show, false);

  // A MONO leader (not a stereo channel) → no panel.
  const mono = addSubPlan({
    enabled: true, bond_id: "bond-1", role: "leader", channel: "mono",
    self_addr: "192.168.1.5",
  });
  assert.equal(mono.show, false);
  assert.equal(mono.reason, "not a stereo leader");

  // Not bonded → no panel.
  assert.equal(addSubPlan({ enabled: false }).show, false);
  assert.equal(addSubPlan({}).show, false);
  assert.equal(addSubPlan(null).show, false);

  // LEGACY pair: no roster, peer_addr+peer_name only, leader on "left" →
  // reconstruct the sibling on the OPPOSITE channel (right).
  const legacy = addSubPlan({
    enabled: true, bond_id: "bond-1", role: "leader", channel: "left",
    self_addr: "192.168.1.5",
    peer_addr: "192.168.1.9", peer_name: "JTS3",
  });
  assert.equal(legacy.show, true);
  assert.deepEqual(legacy.members, [
    { addr: "192.168.1.5", role: "leader", channel: "left" },
    { addr: "192.168.1.9", role: "follower", channel: "right", name: "JTS3" },
  ]);

  // Legacy pair, leader on "right" → sibling reconstructed as "left".
  const legacyRight = addSubPlan({
    enabled: true, bond_id: "bond-1", role: "leader", channel: "right",
    self_addr: "192.168.1.5",
    peer_addr: "192.168.1.9", peer_name: "JTS3",
  });
  assert.equal(legacyRight.members[1].channel, "left");
}

console.log(JSON.stringify({ ok: true }));
