# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Analysis-side evidence codes; capture refusals belong to refusal_copy."""

from types import MappingProxyType

CANDIDATE_BELOW_MIN_DEPTH = "below_min_depth"
CANDIDATE_DEPTH_EXCEEDS_CEILING = "depth_exceeds_arrival_ceiling"
CANDIDATE_NOT_MEASURABLE = "no_flanking_maxima"
CANDIDATE_NO_MATCHING_RUNG = "no_matching_rung"
CANDIDATE_OUTSIDE_CONTIGUOUS_RUN = "outside_contiguous_run"
CAPTURES_UNREADABLE = "classification_captures_unreadable"
CAPTURE_ADMISSIBLE = "admissible"
CAPTURE_OTHER_SESSION = "other_session"
CAPTURE_PHASE_NOT_ADMISSIBLE = "phase_not_admissible"
CAPTURE_PROGRAM_MISSING = "program_missing"
CAPTURE_PROGRAM_UNIDENTIFIED = "program_unidentified"
CAPTURE_UNREADABLE_SIDECAR = "unreadable_sidecar"
CAPTURE_UNSTAMPED_NAME = "unstamped_name"
CAPTURE_WAV_MISSING = "wav_missing"
CLASSIFICATION_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
CLASSIFICATION_POSITION_DEPENDENT = "position_dependent"
CLASSIFICATION_POSITION_INVARIANT = "position_invariant"
CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE = "cloud_exclusion_evidence_unreadable"
CLOUD_BINDING_ENTRY_INCOMPLETE = "banked_fit_entry_names_no_tier_or_driver_class"
CLOUD_BINDING_FIT_INPUTS_NOT_BANKED = "fit_inputs_not_banked"
CLOUD_BINDING_NOT_A_PAIR = "fit_roles_not_a_pair"
CLOUD_BINDING_NOT_FITTED = "round_linearization_was_prescribed_not_fitted"
CLOUD_BINDING_NO_CLOUD_EVIDENCE = "round_banked_no_cloud_exclusion_evidence"
CLOUD_BINDING_NO_FIT = "round_banked_no_linearization_fit"
CLOUD_BINDING_REFIT_DRIFTED = "refit_does_not_reproduce_the_banked_fit"
NOT_SWEPT_BAND_NOT_EVALUABLE = "not_swept_band_not_evaluable"
NOT_SWEPT_BIN_OFF_ANALYSIS_GRID = "not_swept_bin_outside_analysis_grid"
NOT_SWEPT_CAPTURES_UNREADABLE = "not_swept_captures_unreadable"
NOT_SWEPT_SINGLE_POSE = "not_swept_single_pose"
NO_ADMISSIBLE_CAPTURES = "classification_no_admissible_captures"
NO_FEATURES_DETECTED = "classification_no_features_detected"
PROGRAM_MISSING = "classification_program_missing"
REASON_COVERAGE_SHORT = "coverage_short"
REASON_CROSS_SEAT_SPREAD_OVERFLOW = "cross_seat_spread_overflow"
REASON_EXCLUSION_CAP = "exclusion_cap_exceeded"
REASON_GAP_NOT_CONFIDENT = "gap_not_confident"
REASON_LADDER_ARRIVAL_MISMATCH = "ladder_arrival_mismatch"
REASON_NON_BEARING = "non_bearing_pose"
REASON_NO_CANDIDATE_NULLS = "no_candidate_nulls"
REASON_NO_COMPARISON = "no_candidate_comparison"
REASON_NO_CORROBORATING_ARRIVALS = "no_corroborating_arrivals"
REASON_NO_CURVE_GRID = "no_curve_grid"
REASON_NO_IMPULSE = "no_impulse"
REASON_NO_LADDER = "no_ladder"
REASON_NO_PER_POSITION_CURVES = "no_per_position_curves"
REASON_NO_REFERENCE_TAKE = "no_reference_take"
REASON_NO_REPEATS = "too_few_repeats"
REASON_NO_ROW = "no_row"
REASON_REFUSED = "round_views_refused"
REASON_REPEAT_FLOOR_NOT_BANKED = "repeat_floor_not_banked"
REASON_R_DISAGREEMENT = "r_disagreement"
REASON_SEGMENT_MISSING = "pair_segment_missing"
REASON_TOO_FEW_POSITIONS = "too_few_positions"
REASON_TOO_FEW_SEATS = "too_few_seats"
REASON_UNREADABLE = "round_views_unreadable_round"
REASON_UNWRITABLE = "round_views_unwritable_out"
REFUSAL_ALL_ZERO_IR = "all_zero_ir"
REFUSAL_BAD_BAND_HZ = "bad_band_hz"
REFUSAL_BAD_SAMPLE_RATE = "bad_sample_rate"
REFUSAL_BAD_SEARCH_US = "bad_search_us"
REFUSAL_BAD_SIGNAL_BAND_HZ = "bad_signal_band_hz"
REFUSAL_BAND_BELOW_PASSBAND = "band_below_passband"
REFUSAL_BAND_TOO_NARROW = "analysis_band_too_narrow"
REFUSAL_DETECTOR_ERROR = "detector_error"
REFUSAL_EARLIER_DOMINANT_ARRIVAL = "earlier_dominant_arrival"
REFUSAL_LOW_ARRIVAL_CREST = "low_arrival_crest"
REFUSAL_MALFORMED_IR = "malformed_ir"
REFUSAL_NO_IN_WINDOW_ECHO = "no_in_window_echo"
REFUSAL_RAHMONIC_OF_LOWER_DELAY = "rahmonic_of_lower_delay"
REFUSAL_SEARCH_OUTSIDE_CEPSTRUM = "search_window_outside_cepstrum"
REFUSAL_TAU_AT_WINDOW_LOWER_EDGE = "tau_at_window_lower_edge"
REFUSAL_WINDOW_TOO_SHORT = "analysis_window_too_short"
REFUSE_AT_HZ_OFF_SPEC_TABLE = "close_reference_at_hz_off_spec_table"
REFUSE_GATE_NOT_POSITIVE = "close_reference_gate_not_positive"
REFUSE_NO_BRANCH_DIAGNOSTIC = "rear_pair_branch_diagnostic_missing"
REFUSE_NO_INCUMBENT = "rear_incumbent_set_unavailable"
REFUSE_NO_REAR_TAKES = "rear_no_summed_takes"
REFUSE_RATE_MISMATCH = "close_reference_rate_mismatch"
ROUND_SHAPE_INADMISSIBLE = "classification_round_shape_inadmissible"
UNRESOLVED_LOW_CONFIDENCE = "alignment_confidence_below_floor"
UNRESOLVED_NO_CANCELLATION = "agreement_without_cancellation"
UNRESOLVED_OUTSIDE_VALIDITY = "band_outside_validity"
UNRESOLVED_RESIDUAL_SMALL = "disagreement_without_residual"
VERDICT_AGREEMENT = "agreement"
VERDICT_ROOM_DOMINATED = "room_dominated"
VERDICT_UNRESOLVED = "unresolved"

EVIDENCE_REASONS = MappingProxyType({
    CANDIDATE_BELOW_MIN_DEPTH: "The candidate minimum is shallower than the required depth.",
    CANDIDATE_DEPTH_EXCEEDS_CEILING: "The candidate depth exceeds what the measured arrival strength can produce.",
    CANDIDATE_NOT_MEASURABLE: "The candidate minimum has no usable flanking maxima.",
    CANDIDATE_NO_MATCHING_RUNG: "The candidate minimum matches no rung of the fitted ladder.",
    CANDIDATE_OUTSIDE_CONTIGUOUS_RUN: "The candidate is not assigned to a rung in the selected consecutive run.",
    CAPTURES_UNREADABLE: "The round has an admissible capture shape but its stamped audio cannot be read.",
    CAPTURE_ADMISSIBLE: "The capture has an admissible shape, matching session and program, and readable stamped audio.",
    CAPTURE_OTHER_SESSION: "The capture belongs to a different session.",
    CAPTURE_PHASE_NOT_ADMISSIBLE: "The capture phase is not admissible for feature classification.",
    CAPTURE_PROGRAM_MISSING: "No banked program matches this capture stimulus hash.",
    CAPTURE_PROGRAM_UNIDENTIFIED: "The capture banks no stimulus hash to identify the played program.",
    CAPTURE_UNREADABLE_SIDECAR: "The sidecar is not a readable object with a phase string.",
    CAPTURE_UNSTAMPED_NAME: "The capture filename lacks the timestamp required for timing analysis.",
    CAPTURE_WAV_MISSING: "The capture WAV is missing from the ring.",
    CLASSIFICATION_INSUFFICIENT_EVIDENCE: "The measured evidence does not support an interference classification.",
    CLASSIFICATION_POSITION_DEPENDENT: "An identified ladder rung does not meet the required position-presence fraction.",
    CLASSIFICATION_POSITION_INVARIANT: "Every identified ladder rung meets the required position-presence fraction.",
    CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE: "The banked cloud exclusion evidence is malformed.",
    CLOUD_BINDING_ENTRY_INCOMPLETE: "The banked fit entry names no known microphone tier or driver class.",
    CLOUD_BINDING_FIT_INPUTS_NOT_BANKED: "The banked curves cannot reconstruct the original fit inputs.",
    CLOUD_BINDING_NOT_A_PAIR: "The fit roles do not form the branch pair required for the comparison.",
    CLOUD_BINDING_NOT_FITTED: "The round prescribed its linearization instead of fitting it.",
    CLOUD_BINDING_NO_CLOUD_EVIDENCE: "The fit has no banked cloud exclusion evidence.",
    CLOUD_BINDING_NO_FIT: "The round banked no linearization fit to compare.",
    CLOUD_BINDING_REFIT_DRIFTED: "The refit with all inputs does not reproduce the banked fit within tolerance.",
    NOT_SWEPT_BAND_NOT_EVALUABLE: "The band could not be evaluated, so the gate ladder did not run.",
    NOT_SWEPT_BIN_OFF_ANALYSIS_GRID: "The requested frequency bin falls outside the gate analysis grid.",
    NOT_SWEPT_CAPTURES_UNREADABLE: "The gate ladder could not read the required capture curves.",
    NOT_SWEPT_SINGLE_POSE: "The gate ladder could not run because only one pose was available.",
    NO_ADMISSIBLE_CAPTURES: "No readable capture in the ring can be attributed to this round.",
    NO_FEATURES_DETECTED: "No pooled-response feature exceeds the measured capture-to-capture scatter.",
    PROGRAM_MISSING: "No banked program matches the stimulus bytes recorded by the round captures.",
    REASON_COVERAGE_SHORT: "The captured band does not cover the requested figure.",
    REASON_CROSS_SEAT_SPREAD_OVERFLOW: "A member curve carries samples so large that their spread does not fit a float; this artifact cannot be read for a cross-seat spread at all.",
    REASON_EXCLUSION_CAP: "The identified nulls would exclude more than the allowed fraction of the band.",
    REASON_GAP_NOT_CONFIDENT: "The measured arrival gap is below the confidence threshold.",
    REASON_LADDER_ARRIVAL_MISMATCH: "The fitted ladder delay disagrees with the independently measured arrival.",
    REASON_NON_BEARING: "The pose is not a bearing at which the requested figure can be measured.",
    REASON_NO_CANDIDATE_NULLS: "No measured minima qualify as candidate interference nulls.",
    REASON_NO_COMPARISON: "One candidate was played, so there is no candidate comparison or repeat spread for it.",
    REASON_NO_CORROBORATING_ARRIVALS: "No credible time-domain arrivals corroborate an interference ladder.",
    REASON_NO_CURVE_GRID: "The positions block carries no curve grid, so there are no bins to take a spread over.",
    REASON_NO_IMPULSE: "No usable impulse segments are available to measure the arrival gap.",
    REASON_NO_LADDER: "The candidate nulls do not form a sufficient consecutive ladder.",
    REASON_NO_PER_POSITION_CURVES: "No per-position curves are available for the analysis.",
    REASON_NO_REFERENCE_TAKE: "The reference take is missing at this position, so no comparison zero exists.",
    REASON_NO_REPEATS: "Fewer than two usable repeats are available to measure repeat spread.",
    REASON_NO_ROW: "This position has no measured row.",
    REASON_REFUSED: "The requested round view refused the available evidence.",
    REASON_REPEAT_FLOOR_NOT_BANKED: "No repeat floor was banked; bank one before judging a fit against repeat spread.",
    REASON_R_DISAGREEMENT: "The reflection strength inferred from null depths disagrees with the arrival envelope.",
    REASON_SEGMENT_MISSING: "The pair take lacks all three segments on one shared frequency grid.",
    REASON_TOO_FEW_POSITIONS: "Too few usable positions support the requested cross-position statistic.",
    REASON_TOO_FEW_SEATS: "Too few usable seats support the requested comparison; a sample spread needs at least two member curves.",
    REASON_UNREADABLE: "The round view could not read its input round.",
    REASON_UNWRITABLE: "The round view could not write its output artifact.",
    REFUSAL_ALL_ZERO_IR: "The impulse response contains only zeros.",
    REFUSAL_BAD_BAND_HZ: "The analysis frequency band is invalid.",
    REFUSAL_BAD_SAMPLE_RATE: "The sample rate is not finite and positive.",
    REFUSAL_BAD_SEARCH_US: "The delay search bounds are invalid.",
    REFUSAL_BAD_SIGNAL_BAND_HZ: "The declared signal frequency band is invalid.",
    REFUSAL_BAND_BELOW_PASSBAND: "The analysis band is too far below the declared passband level.",
    REFUSAL_BAND_TOO_NARROW: "The analysis band contains too few frequency bins.",
    REFUSAL_DETECTOR_ERROR: "The echo detector failed without a more specific refusal code.",
    REFUSAL_EARLIER_DOMINANT_ARRIVAL: "A dominant earlier arrival prevents attribution to the in-window echo.",
    REFUSAL_LOW_ARRIVAL_CREST: "The direct arrival does not stand far enough above the impulse noise floor.",
    REFUSAL_MALFORMED_IR: "The impulse response is not a usable finite one-dimensional array.",
    REFUSAL_NO_IN_WINDOW_ECHO: "No credible echo was found inside the delay search window.",
    REFUSAL_RAHMONIC_OF_LOWER_DELAY: "A stronger lower-delay cepstral peak makes this estimate a possible rahmonic.",
    REFUSAL_SEARCH_OUTSIDE_CEPSTRUM: "The delay search window falls outside the usable cepstrum.",
    REFUSAL_TAU_AT_WINDOW_LOWER_EDGE: "The estimated delay is too close to the lower search edge to resolve.",
    REFUSAL_WINDOW_TOO_SHORT: "The impulse provides too few samples for the analysis window.",
    REFUSE_AT_HZ_OFF_SPEC_TABLE: "The requested close-reference frequency has no specification tolerance.",
    REFUSE_GATE_NOT_POSITIVE: "The requested close-reference gate is not finite and positive.",
    REFUSE_NO_BRANCH_DIAGNOSTIC: "The rear pair round banked no branch diagnostic segments.",
    REFUSE_NO_INCUMBENT: "The rear comparison has no usable incumbent set.",
    REFUSE_NO_REAR_TAKES: "The round has no usable rear summed takes.",
    REFUSE_RATE_MISMATCH: "The close and far captures have different sample rates.",
    ROUND_SHAPE_INADMISSIBLE: "The round banked no capture shape admissible for feature classification.",
    UNRESOLVED_LOW_CONFIDENCE: "The alignment confidence is below the comparison threshold.",
    UNRESOLVED_NO_CANCELLATION: "The responses agree without enough cancellation to support that agreement.",
    UNRESOLVED_OUTSIDE_VALIDITY: "The band has too few points inside the valid comparison range.",
    UNRESOLVED_RESIDUAL_SMALL: "The responses disagree without a large enough residual to identify the room.",
    VERDICT_AGREEMENT: "The close and far responses agree and the residual supports cancellation.",
    VERDICT_ROOM_DOMINATED: "The close and far responses disagree with a large room residual.",
    VERDICT_UNRESOLVED: "The close-reference comparison cannot resolve speaker response from room response.",
})
