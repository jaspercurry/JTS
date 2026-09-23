# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A measured candidate's analysis evidence and the applied profile's timing record."""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any, Mapping

from jasper.audio_measurement.program_analysis import ProgramAnalysis, polarity_label
from jasper.audio_measurement.program_analysis.model import AppliedAlignment, TIMING_NEEDS_MEASUREMENT
from jasper.active_speaker.baseline_profile import PROVENANCE_MEASURED, PROVENANCE_AUTHORED_BY_MODEL, PROVENANCE_SET_BY_USER

__all__ = [
    "analysis_json",
    "applied_profile_timing",
]


def applied_profile_timing(applied_profile: Mapping[str, Any] | None) -> AppliedAlignment | None:
    """Absence of the profile's timing record is the only reset. See ADR-0319."""
    record = (applied_profile or {}).get("timing")
    required = {"delay_us", "polarity", "provenance"}
    if not isinstance(record, Mapping) or not required <= record.keys() or record.keys() - required - {"measured"}:
        return None
    delay = record["delay_us"]
    if (not isinstance(delay, (int, float)) or isinstance(delay, bool) or not math.isfinite(delay)
            or record["polarity"] not in ("normal", "inverted")
            or record["provenance"] not in (PROVENANCE_MEASURED, PROVENANCE_AUTHORED_BY_MODEL, PROVENANCE_SET_BY_USER)):
        return None
    return AppliedAlignment(float(delay), record["polarity"], record["provenance"], record.get("measured"))


def analysis_json(analysis: ProgramAnalysis) -> dict[str, Any]:
    """Scalar evidence for the measured candidate fingerprint (ADR-0237)."""
    drift = analysis.drift
    align = analysis.alignment
    cand = analysis.candidate
    seed = cand.alignment_seed_delay_us if cand else None
    return {
        "schema_version": 1,
        "kind": "jts_program_analysis_evidence",
        "program_id": analysis.program_id,
        "epsilon_ppm": round(float(drift.epsilon_ppm), 3) if drift else None,
        "repeat_level_delta_db": drift.repeat_level_delta_db if drift else None,
        "glitch_detected": bool(analysis.glitch_detected),
        "delay_us": round(float(align.delay_us), 3) if align and not (cand and cand.timing_verdict == TIMING_NEEDS_MEASUREMENT) else None,
        "alignment_status": align.status if align else None,
        "alignment_pair_count": align.alignment_pair_count if align else None,
        "alignment_pair_spread_us": (
            round(align.alignment_pair_spread_us, 3)
            if align and align.alignment_pair_spread_us is not None else None
        ),
        "alignment_drift_residual_us": (
            round(align.alignment_drift_residual_us, 3)
            if align and align.alignment_drift_residual_us is not None else None
        ),
        "alignment_seed_delay_us": round(float(seed), 3) if seed is not None else None,
        "gcc_delay_us": (round(align.seed_delay_us if align.seed_delay_us is not None else align.delay_us, 3)
                         if align else None),
        "refinement_delta_us": round(align.delay_us - seed, 3) if align and seed is not None else None,
        "trim_decision": None,
        "polarity": align.polarity if align else None,
        "alignment_objective": cand.alignment_objective if cand else None,
        **{key: getattr(cand, key, None) for key in (
            "residual_rms_db", "margin_db", "repeat_spread_db", "repeat_spread_us", "repeat_count", "timing_verdict", "timing_graph_fingerprint")},
        "timing_saved": ({key: value for key, value in asdict(cand.timing_saved).items() if value is not None}
                         if cand and cand.timing_saved else None),
        "timing_verification": cand.timing_verification if cand else None,
        "seed_polarity": (
            None if cand is None or cand.seed_polarity_sign is None
            else polarity_label(int(cand.seed_polarity_sign))
        ),
        "polarity_agrees_with_sum": (
            align.polarity_agrees_with_sum if align else None
        ),
        "polarity_pinned": bool(cand.polarity_pinned) if cand else False,
        "left_anchor_lobe": bool(cand.left_anchor_lobe) if cand else None,
        "alignment_confidence": round(float(align.confidence), 4) if align else None,
        "parallax_us": align.parallax_us if align else None,
        "alignment_confidence_source": align.confidence_source if align else None,
        "trim_db": (
            {k: round(float(v), 4) for k, v in cand.trim_db.items()} if cand else None
        ),
        # #1667: the band-average seed the ripple-optimal solve started from.
        # Evidence only, and kept even when it coincides with ``trim_db``.
        "trim_band_average_db": (
            {k: round(float(v), 4) for k, v in cand.trim_band_average_db.items()}
            if cand and cand.trim_band_average_db is not None else None
        ),
        # Why the two maps above coincide, when they do: the only field that
        # says a flatness answer was computed and discarded, and by how much.
        # ``None`` on the two paths that discarded nothing.
        "ripple_polish_rejected_delta_db": (
            round(float(cand.ripple_polish_rejected_delta_db), 4)
            if cand and cand.ripple_polish_rejected_delta_db is not None else None
        ),
        "predicted_ripple_db": (
            round(float(cand.predicted_ripple_db), 4) if cand else None
        ),
        "alignment_seed_ripple_db": (
            round(float(cand.alignment_seed_ripple_db), 4)
            if cand and cand.alignment_seed_ripple_db is not None else None
        ),
        "flatness_improvement_db": (
            round(float(cand.flatness_improvement_db), 4)
            if cand and cand.flatness_improvement_db is not None else None
        ),
        "anchor_delay_us": (
            round(float(cand.anchor_delay_us), 3)
            if cand and cand.anchor_delay_us is not None else None
        ),
        "snap_delta_us": (
            round(float(cand.snap_delta_us), 3)
            if cand and cand.snap_delta_us is not None else None
        ),
        "snap_found": bool(cand.snap_found) if cand else None,
    }
