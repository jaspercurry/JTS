// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pure presentation helpers for the /rooms bond card — dependency-free (no
// DOM, no imports) so node can unit-test the render DECISIONS directly,
// mirroring deploy/assets/sound-profile/js/active-speaker-ui.js. main.js
// consumes these and does the (DOM-only, untestable-without-a-browser)
// assembly via its h() helper. Tested by tests/js/rooms_grouping_view_test.mjs.

export const BALANCE_MIN_DB = -24;
export const BALANCE_MAX_DB = 24;

export function clampBalanceDb(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.max(BALANCE_MIN_DB, Math.min(BALANCE_MAX_DB, n));
}

export function formatBalanceDb(value) {
  return Number(value).toFixed(1) + " dB";
}

export function balanceText(value) {
  const db = clampBalanceDb(value);
  if (Math.abs(db) < 0.05) return "Centered";
  return (db > 0 ? "Right louder by " : "Left louder by ")
    + formatBalanceDb(Math.abs(db));
}

export function trimsForBalance(value) {
  const db = clampBalanceDb(value);
  return {
    left: Math.min(0, -db),
    right: Math.min(0, db),
  };
}

export function balanceTrimRequest(value) {
  return { target: "pair", balance_db: clampBalanceDb(value) };
}

// The bonded-leader "AirPlay lip-sync" row's presentation, or null when no row
// should render. `fit` is /state.grouping.airplay_latency_fit (shape from
// jasper/multiroom/airplay_latency.py): {applicable, tight?, residual_lag_sec?}
// — null on a fail-soft read error, {applicable:false} on solo/follower.
//
// Returns null unless this speaker is an active bonded leader. Otherwise
// {tight, tone, label, note}: a quiet "Synced" (status-ok) when the offset
// fits, an amber "Lagging ~N ms" (status-warn) + an explanatory note when the
// sender's budget can't absorb the bonded round-trip.
// A subwoofer follower's low-pass corner, formatted for display. A "sub"
// member NEVER plays full-range, so a missing/invalid/non-positive corner
// reads as the 80 Hz default the outputd reconciler also falls back to —
// this label can never render blank or "full-range". Mirrors the producer-
// side default so the UI and the DAC agree on the fallback.
export function subCornerLabel(hz) {
  const n = Number(hz);
  const corner = Number.isFinite(n) && n > 0 ? n : 80;
  return `${Math.round(corner)} Hz low-pass`;
}

// Create-face copy for the role the household picked, so the card's title,
// intro, picker label, and BUTTON all match what clicking actually does — a
// button reading "Create stereo pair" must never be how you add a subwoofer.
// Pure (no DOM); main.js applies these strings to its h()-built nodes in
// syncRoleControls. Anything other than "sub" is the stereo-pair default
// (unchanged wording), so a future/unknown role degrades to the safe pair copy.
export function createFaceCopy(role) {
  if (role === "sub") {
    return {
      title: "Optional wireless subwoofer",
      intro:
        "Optional: keep this speaker playing the full range and make the " +
        "speaker you pick play only the low end, low-passed locally on that box.",
      label: "This speaker is the main — add",
      button: "Add subwoofer",
    };
  }
  return {
    title: "Create a stereo pair",
    intro:
      "Create a stereo pair: this speaker plays the left channel and the one " +
      "you pick plays the right. Both are configured automatically — no " +
      "settings files, no per-speaker setup.",
    label: "This speaker is Left — pair with",
    button: "Create stereo pair",
  };
}

// Decide whether the bonded-leader DISSOLVE face should offer "add a
// subwoofer", and (when it should) the existing-members list to re-post as
// part of the SAME bond. PURE (no DOM) so node can unit-test the decision —
// main.js appends only the new sub + bond_id to `members` and does the DOM.
//
// `g` is /state.grouping (the read_grouping_state snapshot). Show the panel
// ONLY when: this speaker is a bonded LEADER on a stereo channel (left/right)
// and the group has no sub yet. "No sub yet" = roster absent/empty OR no
// roster member has channel "sub".
//
// `members` is the EXISTING members (without the new sub): self as leader on
// its current channel, plus each follower. With a roster we read followers
// from it directly; for a legacy 2-member pair (no roster, peer_addr/_name
// only) we reconstruct the single sibling on the OPPOSITE stereo channel so
// re-posting keeps the pair intact.
export function addSubPlan(g) {
  const grouping = g && typeof g === "object" ? g : {};
  const bonded = !!(grouping.enabled && grouping.bond_id && !grouping.error);
  const channel = grouping.channel || "";
  const isStereoLeader =
    grouping.role === "leader" && (channel === "left" || channel === "right");
  const roster = Array.isArray(grouping.roster) ? grouping.roster : [];
  const hasSub = roster.some((m) => m && m.channel === "sub");
  if (!bonded || !isStereoLeader || hasSub) {
    return {
      show: false,
      members: [],
      reason: !bonded
        ? "not bonded"
        : !isStereoLeader
          ? "not a stereo leader"
          : "already has a sub",
    };
  }

  const selfAddr = grouping.self_addr || "";
  const members = [{ addr: selfAddr, role: "leader", channel }];
  if (roster.length) {
    for (const m of roster) {
      if (!m || !m.addr) continue;
      members.push({
        addr: m.addr,
        role: "follower",
        channel: m.channel || "",
        name: m.name || "",
      });
    }
  } else if (grouping.peer_addr) {
    // Legacy 2-member pair: reconstruct the sibling on the OPPOSITE channel.
    const opposite = channel === "left" ? "right" : "left";
    members.push({
      addr: grouping.peer_addr,
      role: "follower",
      channel: opposite,
      name: grouping.peer_name || "",
    });
  }
  return { show: true, members, reason: "ok" };
}

export function airplayLipSyncRow(fit) {
  if (!fit || typeof fit !== "object" || !fit.applicable) return null;
  const tight = fit.tight === true;
  // Number(...) || 0 hardens against a missing / non-numeric residual (the
  // producer always sends a rounded float, but a NaN must never reach the UI).
  const lagMs = Math.round((Number(fit.residual_lag_sec) || 0) * 1000);
  return {
    tight,
    tone: tight ? "var(--status-warn)" : "var(--status-ok)",
    label: tight ? `Lagging ~${lagMs} ms` : "Synced",
    note: tight
      ? `AirPlay audio plays ~${lagMs} ms after video: the sender's latency `
        + "budget is too short for the bonded round-trip. The sender's budget "
        + "can't be changed locally; if the Snapcast buffer was raised above "
        + "its default, lowering it reduces the lag."
      : null,
  };
}

// Snapcast provisioning notice. `g` is /state.grouping. While the reconciler
// installs the snapcast binaries on the grouping opt-in (the household's
// "set up multi-room" click — provision.state === "installing"), show a quiet
// "Installing Snapcast…" progress notice; on a failed install, show the error
// + the apt remediation. Returns {tone, label, note} or null when there is
// nothing to show (already present / installed / no status). PURE (no DOM);
// main.js renders it. Mirrors airplayLipSyncRow's shape.
export function snapcastProvisionRow(g) {
  const grouping = g && typeof g === "object" ? g : {};
  const prov =
    grouping.provision && typeof grouping.provision === "object"
      ? grouping.provision
      : null;
  if (!prov) return null;
  if (prov.state === "installing") {
    return {
      tone: "var(--status-warn)",
      label: "Installing Snapcast…",
      note:
        "Multi-room needs Snapcast. Installing it now — this takes about a "
        + "minute or two, then the group finishes setting up automatically.",
    };
  }
  if (prov.state === "failed") {
    return {
      tone: "var(--status-danger)",
      label: "Snapcast install failed",
      note:
        "Couldn't install Snapcast — check this speaker's internet connection. "
        + "It retries on the next change; or install it from a terminal with: "
        + "sudo apt install snapserver snapclient.",
    };
  }
  return null; // present / installed / unknown → nothing to show
}
