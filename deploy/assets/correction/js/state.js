// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Room correction — the mic-calibration record, same contract as the sound
// page's state.js (deploy/assets/sound-profile/js/state.js:8-10): callers
// mutate micCalibration's properties, never the binding itself.

export var micCalibration = {
  selectedCalibrationId: null,
  selectedCalibrationMeta: null,
  householdMicPrefillPending: false,
  calibrationMismatchAlerted: false,
};
