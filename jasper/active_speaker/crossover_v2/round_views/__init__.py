# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Round-grading comparison views over banked evidence."""

from ..round_inputs import (
    RoundInputs,
    RoundViewsError,
)
from .agreement import (
    AGREEMENT_DISSENT_MAX,
    AGREEMENT_TESTIFY_MIN,
    AgreementFeature,
    agreement_table,
    default_agreement_lo_hz,
)
from .banked import DEFAULT_PRIMARY_ROLE as DEFAULT_PRIMARY_ROLE
from .banked import (
    BankedRound,
    load_banked_round,
    response_from_banked_curve,
)
from .cloud_binding import (
    BOUND_FLOOR_DB,
    CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE,
    CLOUD_BINDING_ENTRY_INCOMPLETE,
    CLOUD_BINDING_FIT_INPUTS_NOT_BANKED,
    CLOUD_BINDING_NO_CLOUD_EVIDENCE,
    CLOUD_BINDING_NO_FIT,
    CLOUD_BINDING_NOT_A_PAIR,
    CLOUD_BINDING_NOT_FITTED,
    CLOUD_BINDING_REFIT_DRIFTED,
    REFIT_TOLERANCE_DB,
    SEVERED_CLOUD_INPUTS,
    CloudBindingBand,
    CloudBindingRole,
    CloudBindingView,
    cloud_binding_view,
)
from .co_metrics import (
    AudibilityCoMetrics,
    AudibilityMetrics,
    PooledWindowResult,
    audibility_co_metrics,
    directivity_view,
    pooled_window_horizontal,
)
from .entry_grade import (
    ENTRY_STATE_UNREADABLE,
    EntryStateGrade,
    entry_state_grade,
)
from .frozen import (
    FrozenReferenceResult,
    frozen_reference_grade,
)
from .gate_sensitivity import (
    NOT_SWEPT_BAND_NOT_EVALUABLE,
    NOT_SWEPT_BIN_OFF_ANALYSIS_GRID,
    NOT_SWEPT_CAPTURES_UNREADABLE,
    NOT_SWEPT_SINGLE_POSE,
    spec_with_gate_sensitivity,
)
from .repeatability import (
    RepeatabilityMetric,
    RepeatabilityResult,
    repeat_floor_provenance,
    repeatability_spread,
)
from .seats import (
    SeatCurve,
    VerifyPoseResult,
    per_seat_curves,
    verify_pose_curve,
)

__all__ = [
    "AGREEMENT_DISSENT_MAX",
    "AGREEMENT_TESTIFY_MIN",
    "NOT_SWEPT_BAND_NOT_EVALUABLE",
    "NOT_SWEPT_BIN_OFF_ANALYSIS_GRID",
    "NOT_SWEPT_CAPTURES_UNREADABLE",
    "NOT_SWEPT_SINGLE_POSE",
    "AgreementFeature",
    "AudibilityCoMetrics",
    "BOUND_FLOOR_DB",
    "CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE",
    "CLOUD_BINDING_ENTRY_INCOMPLETE",
    "CLOUD_BINDING_FIT_INPUTS_NOT_BANKED",
    "CLOUD_BINDING_NOT_FITTED",
    "CLOUD_BINDING_NOT_A_PAIR",
    "CLOUD_BINDING_NO_CLOUD_EVIDENCE",
    "CLOUD_BINDING_NO_FIT",
    "CLOUD_BINDING_REFIT_DRIFTED",
    "CloudBindingBand",
    "CloudBindingRole",
    "CloudBindingView",
    "REFIT_TOLERANCE_DB",
    "SEVERED_CLOUD_INPUTS",
    "AudibilityMetrics",
    "BankedRound",
    "ENTRY_STATE_UNREADABLE",
    "EntryStateGrade",
    "FrozenReferenceResult",
    "PooledWindowResult",
    "RepeatabilityMetric",
    "RepeatabilityResult",
    "RoundInputs",
    "RoundViewsError",
    "SeatCurve",
    "VerifyPoseResult",
    "agreement_table",
    "audibility_co_metrics",
    "cloud_binding_view",
    "response_from_banked_curve",
    "default_agreement_lo_hz",
    "directivity_view",
    "entry_state_grade",
    "frozen_reference_grade",
    "load_banked_round",
    "per_seat_curves",
    "pooled_window_horizontal",
    "repeat_floor_provenance",
    "repeatability_spread",
    "spec_with_gate_sensitivity",
    "verify_pose_curve",
]
