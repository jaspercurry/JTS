# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Analysis-side evidence codes; capture refusals belong to refusal_copy."""

from types import MappingProxyType

CAPTURES_UNREADABLE = "classification_captures_unreadable"
CAPTURE_ADMISSIBLE = "admissible"
CAPTURE_OTHER_SESSION = "other_session"
CAPTURE_PHASE_NOT_ADMISSIBLE = "phase_not_admissible"
CAPTURE_PROGRAM_MISSING = "program_missing"
CAPTURE_PROGRAM_UNIDENTIFIED = "program_unidentified"
CAPTURE_UNREADABLE_SIDECAR = "unreadable_sidecar"
CAPTURE_UNSTAMPED_NAME = "unstamped_name"
CAPTURE_WAV_MISSING = "wav_missing"
NO_ADMISSIBLE_CAPTURES = "classification_no_admissible_captures"
NO_FEATURES_DETECTED = "classification_no_features_detected"
PROGRAM_MISSING = "classification_program_missing"
REASON_COVERAGE_SHORT = "coverage_short"
REASON_FIT_BAND_UNAVAILABLE = "fit_band_unavailable"
REASON_FIT_NOT_FINITE = "fit_not_finite"
REASON_GAP_NOT_CONFIDENT = "gap_not_confident"
REASON_GRAPH_MISMATCH = "graph_mismatch"
REASON_MARK_FIT_BAND_UNAVAILABLE = "mark_fit_band_unavailable"
REASON_MARK_RESPONSE_UNAVAILABLE = "mark_response_unavailable"
REASON_NON_BEARING = "non_bearing_pose"
REASON_NO_COMPARISON = "no_candidate_comparison"
REASON_NO_IMPULSE = "no_impulse"
REASON_NO_MARK_PAIRS = "no_mark_pairs"
REASON_NO_REFERENCE_TAKE = "no_reference_take"
REASON_NO_REPEATS = "too_few_repeats"
REASON_NO_ROW = "no_row"
REASON_NO_SHARED_MARK_TAKES = "no_shared_mark_takes"
REASON_REFUSED = "round_views_refused"
REASON_SEGMENT_MISSING = "pair_segment_missing"
REASON_SNR_SHORT = "snr_short"
REASON_TOO_FEW_POSITIONS = "too_few_positions"
REASON_UNREADABLE = "round_views_unreadable_round"
REASON_UNWRITABLE = "round_views_unwritable_out"
REFUSE_NO_BRANCH_DIAGNOSTIC = "rear_pair_branch_diagnostic_missing"
REFUSE_NO_INCUMBENT = "rear_incumbent_set_unavailable"
REFUSE_NO_NEAR_FIELD_TAKES = "nearfield_no_kept_takes"
REFUSE_NO_REAR_TAKES = "rear_no_summed_takes"
ROOM_NOT_BANKED = "room_not_banked"
ROUND_SHAPE_INADMISSIBLE = "classification_round_shape_inadmissible"

EVIDENCE_REASONS = MappingProxyType({
    CAPTURES_UNREADABLE: "The round has an admissible capture shape but its stamped audio cannot be read.",
    CAPTURE_ADMISSIBLE: "The capture has an admissible shape, matching session and program, and readable stamped audio.",
    CAPTURE_OTHER_SESSION: "The capture belongs to a different session.",
    CAPTURE_PHASE_NOT_ADMISSIBLE: "The capture phase is not admissible for feature classification.",
    CAPTURE_PROGRAM_MISSING: "No banked program matches this capture stimulus hash.",
    CAPTURE_PROGRAM_UNIDENTIFIED: "The capture banks no stimulus hash to identify the played program.",
    CAPTURE_UNREADABLE_SIDECAR: "The sidecar is not a readable object with a phase string.",
    CAPTURE_UNSTAMPED_NAME: "The capture filename lacks the timestamp required for timing analysis.",
    CAPTURE_WAV_MISSING: "The capture WAV is missing from the ring.",
    NO_ADMISSIBLE_CAPTURES: "No readable capture in the ring can be attributed to this round.",
    NO_FEATURES_DETECTED: "No pooled-response feature exceeds the measured capture-to-capture scatter.",
    PROGRAM_MISSING: "No banked program matches the stimulus bytes recorded by the round captures.",
    REASON_COVERAGE_SHORT: "The captured band does not cover the requested figure.",
    REASON_FIT_BAND_UNAVAILABLE: "The fit reports no band to compare the mark pairs over.",
    REASON_FIT_NOT_FINITE: "A fitted filter term is NaN or infinite, so the fit is published without numbers.",
    REASON_GAP_NOT_CONFIDENT: "The measured arrival gap is below the confidence threshold.",
    REASON_GRAPH_MISMATCH: "The summed take played an output the driver-take prediction does not model, so the two sums are not comparable.",
    REASON_MARK_FIT_BAND_UNAVAILABLE: "A mark take does not cover the fit band above its trusted floor.",
    REASON_MARK_RESPONSE_UNAVAILABLE: "A mark take's curve cannot be read for the repeat-spread comparison.",
    REASON_NON_BEARING: "The pose is not a bearing at which the requested figure can be measured.",
    REASON_NO_COMPARISON: "One candidate was played, so there is no candidate comparison or repeat spread for it.",
    REASON_NO_IMPULSE: "No usable impulse segments are available to measure the arrival gap.",
    REASON_NO_MARK_PAIRS: "The round has fewer than two takes of this driver at one placement, so no mark pair exists for a repeat spread.",
    REASON_NO_REFERENCE_TAKE: "The reference take is missing at this position, so no comparison zero exists.",
    REASON_NO_REPEATS: "Fewer than two usable repeats are available to measure repeat spread.",
    REASON_NO_ROW: "This position has no measured row.",
    REASON_NO_SHARED_MARK_TAKES: "No driver has mark takes in two of the compared rounds, so nothing compares between rounds.",
    REASON_REFUSED: "The requested round view refused the available evidence.",
    REASON_SEGMENT_MISSING: "The pair take lacks all three segments on one shared frequency grid.",
    REASON_SNR_SHORT: "A driver take is below the alignment signal-to-noise floor, so its predicted sum is not comparable with the measured sum.",
    REASON_TOO_FEW_POSITIONS: "Too few usable positions support the requested cross-position statistic.",
    REASON_UNREADABLE: "The round view could not read its input round.",
    REASON_UNWRITABLE: "The round view could not write its output artifact.",
    REFUSE_NO_BRANCH_DIAGNOSTIC: "The rear pair round banked no branch diagnostic segments.",
    REFUSE_NO_INCUMBENT: "The rear comparison has no usable incumbent set.",
    REFUSE_NO_NEAR_FIELD_TAKES: "The round has no kept near-field driver takes.",
    REFUSE_NO_REAR_TAKES: "The round has no usable rear summed takes.",
    ROOM_NOT_BANKED: "The banked round holds no room view by the name asked; a room view run after the bank is not its evidence.",
    ROUND_SHAPE_INADMISSIBLE: "The round banked no capture shape admissible for feature classification.",
})
