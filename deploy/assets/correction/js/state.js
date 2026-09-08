// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Room correction — the mic-calibration record. Shared by reference:
// callers mutate properties, never the binding (same contract as
// sound-profile/js/state.js).

export var calibrationSelection = {
  // The selected calibration's calibration_id, or null when none is
  // selected.
  id: null,
  // The selected calibration's full payload (label, provider, point_count,
  // orientation, file_sha256, ...), or null when none is selected.
  meta: null,
  // True after a server-rendered household-mic prefill
  // (applyHouseholdMicPrefill) until reconciled by a model change, the
  // Change button, or maybeInferCalibrationModel overriding a stale prefill
  // on a mismatched connected mic (issue #1656).
  householdPrefillPending: false,
  // One-shot per measurement run: whether the household has already been
  // told their selected calibration didn't end up bound (see
  // checkCalibrationHonesty).
  mismatchAlerted: false,
};

export function clearCalibrationSelection() {
  calibrationSelection.id = null;
  calibrationSelection.meta = null;
  calibrationSelection.householdPrefillPending = false;
}
