// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pure presentation helpers for the /rooms bond card — dependency-free (no
// DOM, no imports) so node can unit-test the render DECISIONS directly,
// mirroring deploy/assets/sound-profile/js/active-speaker-ui.js. main.js
// consumes these and does the (DOM-only, untestable-without-a-browser)
// assembly via its h() helper. Tested by tests/js/rooms_grouping_view_test.mjs.

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
      title: "Add a wireless subwoofer",
      intro:
        "This speaker keeps playing the full range; the speaker you pick " +
        "plays only the low end (low-passed locally on that box). Both are " +
        "configured automatically — no settings files, no per-speaker setup.",
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
