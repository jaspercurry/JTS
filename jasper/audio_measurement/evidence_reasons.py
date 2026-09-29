# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Analysis-side evidence codes, and the one exception an evidence reader refuses with.

Each code's household sentence and next action live in
``refusal_copy.REASON_REGISTRY`` (ADR-0300).
"""

import json
from collections.abc import Mapping
from typing import Any

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
NO_KEPT_TAKES = "classification_no_kept_takes"
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
TAKE_CURVES_NOT_BANKED = "take_curves_not_banked"


class EvidenceUnavailable(Exception):
    """The evidence cannot answer: ``reason`` is a code, ``detail`` the facts behind it."""

    def __init__(self, reason: str, detail: Mapping[str, Any]) -> None:
        super().__init__(f"{reason}: {json.dumps(detail, sort_keys=True, default=str)}")
        self.reason = reason
        self.detail = dict(detail)


def unavailable(reason: str, detail: Any = None) -> dict[str, Any]:
    """A gap inside a document; a present value reads ``{"status": "available"}`` (#5928)."""
    return {"status": "unavailable", "reason": reason, **({} if detail is None else {"detail": detail})}
