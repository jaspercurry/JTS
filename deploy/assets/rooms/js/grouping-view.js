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
