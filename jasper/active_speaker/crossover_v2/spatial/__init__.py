# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a capture-consuming phase DECIDES about one take: is it evidence, what
does it record, and does the group want another one.

Serves the phases that consume a take without prescribing anything from it —
the two position clouds, the lateral walk, and the entry baseline. Each ladder's
content is its ORDER and the gates it deliberately DROPS, both stated at the
ladder.

Three rules this module keeps: inputs are STATED, never reached for (the caller
evaluates the shared predicates into a :class:`CaptureScreens`); no household
vocabulary — a refusal leaves as a kind from :data:`SCREEN_KINDS` and
:mod:`.refusal_copy` maps it; and side-effect-free, so
:func:`boost_excluded_bands_hz` returns its log fields as data
(:attr:`BoostExclusion.diagnostics`) rather than journalling them. No
``jasper.web`` import and nothing from :mod:`..crossover_v2_flow`.
"""

from ..contracts import (
    POSITION_AXES as POSITION_AXES,
    POSITION_AXIS_HORIZONTAL as POSITION_AXIS_HORIZONTAL,
    POSITION_AXIS_VERTICAL as POSITION_AXIS_VERTICAL,
)
from ..pose_curve import (
    LATERAL_EVIDENCE_BAND_HZ as LATERAL_EVIDENCE_BAND_HZ,
    LATERAL_EVIDENCE_POINTS_PER_OCTAVE as LATERAL_EVIDENCE_POINTS_PER_OCTAVE,
    LateralPoseCurve as LateralPoseCurve,
    lateral_evidence_grid_hz as lateral_evidence_grid_hz,
    pose_curve_record as pose_curve_record,
)
from .boost_exclusion import (
    BoostExclusion as BoostExclusion,
    boost_excluded_bands_hz as boost_excluded_bands_hz,
)
from .carve_out_copy import (
    CARVE_OUT_SOURCE_IDENTIFIED_NULL as CARVE_OUT_SOURCE_IDENTIFIED_NULL,
    CARVE_OUT_SOURCE_POSITION_SCREEN as CARVE_OUT_SOURCE_POSITION_SCREEN,
    _geometry_guidance_copy as _geometry_guidance_copy,
    _null_classification_copy as _null_classification_copy,
    carve_outs_by_band as carve_outs_by_band,
)
from .cloud_group import (
    CLOUD_CURVE_MAX_JSON_POINTS as CLOUD_CURVE_MAX_JSON_POINTS,
    CloudCombine as CloudCombine,
    CloudGroupResult as CloudGroupResult,
    CloudVerdict as CloudVerdict,
    _CloudEchoBand as _CloudEchoBand,
    _CloudPosition as _CloudPosition,
    _decimate_curve_for_json as _decimate_curve_for_json,
    _derive_cloud_echo_band_hz as _derive_cloud_echo_band_hz,
    _geometry_verdict_from_combined as _geometry_verdict_from_combined,
    _min_clamped_echo_band_width_hz as _min_clamped_echo_band_width_hz,
    assemble_cloud_group_result as assemble_cloud_group_result,
    cloud_entanglement_floor_hz as cloud_entanglement_floor_hz,
    cloud_geometry_verdict as cloud_geometry_verdict,
    cloud_position_capture as cloud_position_capture,
    cloud_validity_floor_hz as cloud_validity_floor_hz,
    combine_cloud_positions as combine_cloud_positions,
)
from .group_floor import (
    GEOMETRY_RETRY_POSITIONS as GEOMETRY_RETRY_POSITIONS,
    GeometryRetake as GeometryRetake,
    MIN_RESOLVED_CLOUD_POSITIONS as MIN_RESOLVED_CLOUD_POSITIONS,
    geometry_retake as geometry_retake,
    group_position_floor as group_position_floor,
)
from .records import (
    LATERAL_POSE_REGIME as LATERAL_POSE_REGIME,
    LateralPose as LateralPose,
    MARK_DISTANCE_M as MARK_DISTANCE_M,
    POSITION_ROLES as POSITION_ROLES,
    POSITION_ROLE_OFFAX as POSITION_ROLE_OFFAX,
    POSITION_ROLE_ONAX as POSITION_ROLE_ONAX,
    POSITION_ROLE_XOVR as POSITION_ROLE_XOVR,
    PositionGeometry as PositionGeometry,
    TakeClaim as TakeClaim,
    _DESIGN_AXIS_GEOMETRY as _DESIGN_AXIS_GEOMETRY,
    _primary_sweep_bands as _primary_sweep_bands,
    _summed_sweep_band_hz as _summed_sweep_band_hz,
    analysis_curve_records as analysis_curve_records,
    cloud_position_record as cloud_position_record,
    entry_baseline_record as entry_baseline_record,
    lateral_pose_record as lateral_pose_record,
    phase_capture_record as phase_capture_record,
    phase_composition as phase_composition,
    pose_kind_fields as pose_kind_fields,
    take_id_for as take_id_for,
    take_stop_id as take_stop_id,
)
from .screens import (
    CaptureScreens as CaptureScreens,
    EntryBaselineScreen as EntryBaselineScreen,
    SCREEN_CAPTURE_GLITCH as SCREEN_CAPTURE_GLITCH,
    SCREEN_CLIPPED as SCREEN_CLIPPED,
    SCREEN_KINDS as SCREEN_KINDS,
    SCREEN_LINEARITY_FAILED as SCREEN_LINEARITY_FAILED,
    SCREEN_LOCATE_FAILED as SCREEN_LOCATE_FAILED,
    SCREEN_PILOT_LEVEL_COLLAPSE as SCREEN_PILOT_LEVEL_COLLAPSE,
    cloud_position_screens as cloud_position_screens,
    entry_baseline_screens as entry_baseline_screens,
    lateral_curves_sufficient as lateral_curves_sufficient,
    lateral_pose_screens as lateral_pose_screens,
)

__all__ = [
    "CARVE_OUT_SOURCE_IDENTIFIED_NULL",
    "CARVE_OUT_SOURCE_POSITION_SCREEN",
    "CLOUD_CURVE_MAX_JSON_POINTS",
    "GEOMETRY_RETRY_POSITIONS",
    "LATERAL_EVIDENCE_BAND_HZ",
    "LATERAL_EVIDENCE_POINTS_PER_OCTAVE",
    "LATERAL_POSE_REGIME",
    "MARK_DISTANCE_M",
    "POSITION_AXES",
    "POSITION_AXIS_HORIZONTAL",
    "POSITION_AXIS_VERTICAL",
    "POSITION_ROLES",
    "POSITION_ROLE_OFFAX",
    "POSITION_ROLE_ONAX",
    "POSITION_ROLE_XOVR",
    "PositionGeometry",
    "SCREEN_LOCATE_FAILED",
    "SCREEN_PILOT_LEVEL_COLLAPSE",
    "SCREEN_LINEARITY_FAILED",
    "SCREEN_CAPTURE_GLITCH",
    "SCREEN_CLIPPED",
    "SCREEN_KINDS",
    "CaptureScreens",
    "EntryBaselineScreen",
    "GeometryRetake",
    "BoostExclusion",
    "CloudCombine",
    "CloudGroupResult",
    "CloudVerdict",
    "LateralPose",
    "assemble_cloud_group_result",
    "carve_outs_by_band",
    "cloud_entanglement_floor_hz",
    "cloud_position_capture",
    "cloud_validity_floor_hz",
    "combine_cloud_positions",
    "cloud_geometry_verdict",
    "cloud_position_screens",
    "lateral_pose_screens",
    "lateral_curves_sufficient",
    "lateral_evidence_grid_hz",
    "entry_baseline_screens",
    "MIN_RESOLVED_CLOUD_POSITIONS",
    "group_position_floor",
    "geometry_retake",
    "take_id_for",
    "TakeClaim",
    "phase_composition",
    "cloud_position_record",
    "analysis_curve_records",
    "lateral_pose_record",
    "entry_baseline_record",
    "phase_capture_record",
    "boost_excluded_bands_hz",
]
