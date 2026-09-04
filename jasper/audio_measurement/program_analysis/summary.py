# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The operator-facing diagnostic summary of a finished analysis."""

from __future__ import annotations

import math
from typing import Any, Mapping

from jasper.audio_measurement import gate_disclosure
from .check import channel_map_isolation_db
from .model import DRIVER_SNR_ALIGNMENT_KEY, DriverResponse
from .response import polarity_label


def _gate_window_ms_of(response: "DriverResponse | None") -> float | None:
    if response is None:
        return None
    window = response.gating.get("window_ms") if response.gating else None
    return float(window) if isinstance(window, (int, float)) else None


def driver_snr_verdict(response: "DriverResponse | None") -> str | None:
    """This branch's worst relevant MAGNITUDE-class capture-SNR verdict
    (``ok``/``reduced``/``insufficient``/``unknown``). ``None`` when no
    verdict was computed (a CHECK with no ambient window). The alignment
    refusal reads the OTHER verdict — see :func:`driver_alignment_snr_verdict`.
    """
    return _snr_verdict_of(response.snr if response is not None else None)


def driver_alignment_snr_verdict(response: "DriverResponse | None") -> str | None:
    """This branch's worst relevant ALIGNMENT-class capture-SNR verdict: 35 dB
    (``DRIVER.alignment_snr_ok_db``), ok-or-insufficient with no reduced
    rung. ``None`` when no verdict was computed — never guessed, so a
    capture with no ambient window keeps the flat-sum selector.
    """
    snr = response.snr if response is not None else None
    if not snr:
        return None
    return _snr_verdict_of(snr.get(DRIVER_SNR_ALIGNMENT_KEY))


def _snr_verdict_of(block: Mapping[str, Any] | None) -> str | None:
    """``block["worst_relevant"]["verdict"]`` when it is a string, else None."""
    if not block:
        return None
    worst = block.get("worst_relevant") or {}
    verdict = worst.get("verdict")
    return verdict if isinstance(verdict, str) else None


def _gate_floor_source_of(response: "DriverResponse | None") -> str | None:
    """WHY this capture's gate window is what it is.

    ``window_ms`` alone can't distinguish the two states, which print
    identically: ``FLOOR_MEASURED`` (a reflection onset was found) vs
    ``FLOOR_SEARCH_BOUND`` (the search ran to ``SEARCH_T_MAX_MS`` without
    finding one and was CAPPED). ``None`` for an ungateable capture.
    """
    if response is None:
        return None
    source = response.gating.get("floor_source") if response.gating else None
    return str(source) if isinstance(source, str) else None


def _gate_disclosure_of(response: "DriverResponse | None") -> str | None:
    """The gate's provenance as a SENTENCE, for the retained sidecar
    (rendered by the one writer, :func:`gate_disclosure.describe_gate`).
    """
    if response is None or not response.gating:
        return None
    return gate_disclosure.describe_gate(response.gating)


def analysis_diagnostic_summary(analysis: Any) -> dict[str, Any]:
    """Flat, JSON-safe numeric diagnostics from one :class:`ProgramAnalysis`,
    compared against a banked corpus by the distortion replays. Reads only
    existing fields, nothing recomputed. Deliberately duck-typed and
    defensive (``getattr(..., None)`` throughout) so a malformed or foreign
    ``analysis`` degrades to an emptier summary rather than raising.
    """
    out: dict[str, Any] = {"phase": getattr(analysis, "phase", None)}

    drift = getattr(analysis, "drift", None)
    if drift is not None:
        out["epsilon_ppm"] = round(float(drift.epsilon_ppm), 3)
        out["max_residual_samples"] = round(float(drift.max_residual_samples), 3)
        out["repeat_level_delta_db"] = round(
            float(getattr(drift, "repeat_level_delta_db", 0.0)), 3
        )
        out["glitch_detected"] = bool(drift.glitch_detected)
        out["glitch_inputs"] = ",".join(getattr(drift, "glitch_inputs", ()) or ())
        # May be DISCONTINUITY_UNRESOLVED (a str); float() would raise.
        discontinuity = getattr(drift, "discontinuity_samples", 0.0)
        out["discontinuity_samples"] = (
            round(float(discontinuity), 3)
            if isinstance(discontinuity, (int, float))
            else discontinuity
        )
        out["discontinuity_after_segment"] = getattr(
            drift, "discontinuity_after_segment", "",
        )
        # Diagnostic-only, never gated.
        for role, eps in (getattr(drift, "per_role_epsilon_ppm", None) or {}).items():
            out[f"{role}_repeat_epsilon_ppm"] = round(float(eps), 3)

    alignment = getattr(analysis, "alignment", None)
    if alignment is not None:
        out["alignment_confidence"] = round(float(alignment.confidence), 4)
        out["alignment_confidence_source"] = getattr(
            alignment, "confidence_source", "gcc_phat",
        )
        out["alignment_status"] = alignment.status
        out["delay_us"] = round(float(alignment.delay_us), 3)
        seed_delay_us = getattr(alignment, "seed_delay_us", None)
        if seed_delay_us is not None:
            out["alignment_seed_delay_us"] = round(float(seed_delay_us), 3)
            out["alignment_refinement_delta_us"] = round(
                float(alignment.delay_us) - float(seed_delay_us), 3,
            )
        out["polarity"] = alignment.polarity
        # The polarity half of the same seed-vs-committed pair the two lines
        # above report for the delay. ``None`` on an estimate nothing
        # cross-checked; the sidecar carries the tri-state rather than
        # flattening "correlation agreed" and "nobody asked" together.
        out["polarity_agrees_with_sum"] = getattr(
            alignment, "polarity_agrees_with_sum", None,
        )

    candidate = getattr(analysis, "candidate", None)
    if candidate is not None:
        out["predicted_ripple_db"] = round(float(candidate.predicted_ripple_db), 4)
        out["alignment_objective"] = getattr(candidate, "alignment_objective", "")
        seed_polarity_sign = getattr(candidate, "seed_polarity_sign", None)
        out["seed_polarity"] = (
            None if seed_polarity_sign is None
            else polarity_label(int(seed_polarity_sign))
        )
        out["left_anchor_lobe"] = bool(getattr(candidate, "left_anchor_lobe", False))
        rejected_polish_db = getattr(
            candidate, "ripple_polish_rejected_delta_db", None
        )
        out["ripple_polish_rejected_delta_db"] = (
            None if rejected_polish_db is None else round(float(rejected_polish_db), 3)
        )
        anchor_delay_us = getattr(candidate, "anchor_delay_us", None)
        if anchor_delay_us is not None:
            out["anchor_delay_us"] = round(float(anchor_delay_us), 3)
            snap_delta_us = getattr(candidate, "snap_delta_us", None)
            out["snap_delta_us"] = (
                round(float(snap_delta_us), 3) if snap_delta_us is not None else None
            )
            out["snap_found"] = bool(getattr(candidate, "snap_found", False))
        if candidate.alignment_seed_ripple_db is not None:
            out["alignment_seed_ripple_db"] = round(
                float(candidate.alignment_seed_ripple_db), 4,
            )
            out["flatness_improvement_db"] = round(
                float(candidate.flatness_improvement_db), 4,
            )

    for resp in getattr(analysis, "driver_responses", None) or ():
        role = resp.role
        out[f"{role}_gate_window_ms"] = _gate_window_ms_of(resp)
        out[f"{role}_gate_floor_source"] = _gate_floor_source_of(resp)
        out[f"{role}_gate_disclosure"] = _gate_disclosure_of(resp)
        out[f"{role}_validity_floor_hz"] = resp.validity_floor_hz
        if resp.snr is not None:
            worst = resp.snr.get("worst_relevant") or {}
            out[f"{role}_snr_db"] = worst.get("estimated_snr_db")
            out[f"{role}_snr_verdict"] = driver_snr_verdict(resp)
            out[f"{role}_snr_band"] = worst.get("band_id")
            # The ALIGNMENT-class trio, beside the MAGNITUDE one — a
            # separate law (35 dB, no reduced rung) from magnitude's 25 dB.
            alignment = resp.snr.get(DRIVER_SNR_ALIGNMENT_KEY) or {}
            alignment_worst = alignment.get("worst_relevant") or {}
            out[f"{role}_alignment_snr_db"] = alignment_worst.get(
                "estimated_snr_db"
            )
            out[f"{role}_alignment_snr_verdict"] = driver_alignment_snr_verdict(
                resp
            )
            out[f"{role}_alignment_snr_band"] = alignment_worst.get("band_id")

    for pilot in getattr(analysis, "pilots", None) or ():
        role = pilot.role
        snr_db = getattr(pilot, "snr_db", math.inf)
        out[f"{role}_pilot_snr_db"] = round(snr_db, 2) if math.isfinite(snr_db) else None
        out[f"{role}_captured_delta_db"] = round(float(pilot.captured_delta_db), 3)
        out[f"{role}_programmed_delta_db"] = round(float(pilot.programmed_delta_db), 3)
        out[f"{role}_channel_map_target_rise_db"] = pilot.channel_map_target_rise_db
        out[f"{role}_channel_map_cross_rise_db"] = pilot.channel_map_cross_rise_db
        # Both raws AND the ratio: raws say which half moved, ratio decided CROSS.
        out[f"{role}_channel_map_isolation_db"] = channel_map_isolation_db(
            pilot.channel_map_target_rise_db, pilot.channel_map_cross_rise_db
        )

    gain_plan = getattr(analysis, "gain_plan", None)
    if gain_plan is not None:
        out["gain_plan_snr_floor_ok"] = gain_plan.snr_floor_ok
        out["gain_plan_predicted_peak_dbfs"] = round(
            float(gain_plan.predicted_peak_dbfs), 3
        )
        for role, solve in (getattr(gain_plan, "role_solves", None) or {}).items():
            out[f"{role}_measure_gain_db"] = round(float(solve.gain_db), 3)
            out[f"{role}_measure_gain_reduction_db"] = round(
                float(solve.reduction_db), 3
            )
            out[f"{role}_measure_gain_bound_by"] = solve.bound_by

    for flag in ("pilot_snr_ok", "linearity_ok", "channel_map_ok"):
        value = getattr(analysis, flag, None)
        if value is not None:
            out[flag] = value

    # Absent, not empty, on CHECK/MEASURE (whose glitch verdict is `drift` above).
    integrity = getattr(analysis, "capture_integrity", None)
    if integrity is not None:
        out["integrity_failed"] = ",".join(getattr(integrity, "failed", ()) or ())
        out["integrity_not_evaluated"] = ",".join(
            getattr(integrity, "not_evaluated", ()) or ()
        )
        out["integrity_locate_confidence_min"] = getattr(
            integrity, "locate_confidence_min", None
        )
        out["integrity_schedule_residual_ms_worst"] = getattr(
            integrity, "schedule_residual_ms_worst", None
        )
        out["integrity_clipped_segments"] = ",".join(
            getattr(integrity, "clipped_segments", ()) or ()
        )

    # Present on EVERY phase, unlike the integrity block above.
    ledger = getattr(analysis, "frame_ledger", None)
    if ledger is not None:
        out["frames_received"] = ledger.received_frames
        out["frames_declared"] = ledger.declared_frames
        out["frames_encoded"] = ledger.encoded_frames
        out["frames_render_gaps"] = ledger.render_gaps
        out["frames_render_gap_frames"] = ledger.render_gap_frames
        out["frames_lost_at"] = ",".join(ledger.lost_at)

    summed_response = getattr(analysis, "summed_response", None)
    if summed_response is not None:
        out["verify_gate_window_ms"] = _gate_window_ms_of(summed_response)
        out["verify_gate_floor_source"] = _gate_floor_source_of(summed_response)
        out["verify_gate_disclosure"] = _gate_disclosure_of(summed_response)
        out["verify_validity_floor_hz"] = summed_response.validity_floor_hz

    tracking = getattr(analysis, "verify_tracking", None)
    if tracking:
        for key in ("rms_db", "max_db", "rms_db_notch_excluded", "max_db_notch_excluded"):
            if key in tracking:
                out[key] = tracking[key]
        band = tracking.get("tracking_band_hz")
        if isinstance(band, (list, tuple)) and len(band) == 2:
            out["tracking_band_lo_hz"] = band[0]
            out["tracking_band_hi_hz"] = band[1]
        # Frame terms ride the sidecar BESIDE the raw scalars, never instead
        # of them; present with None terms when no frame could be fitted.
        frame = tracking.get("frame")
        if isinstance(frame, Mapping):
            out["frame_offset_db"] = frame.get("offset_db")
            out["frame_tilt_db_per_octave"] = frame.get("tilt_db_per_octave")
            out["frame_pivot_hz"] = frame.get("pivot_hz")
            out["frame_n_bins"] = frame.get("n_bins")
            tilt_removed = frame.get("tilt_removed")
            if isinstance(tilt_removed, Mapping):
                out["rms_db_tilt_removed"] = tilt_removed.get("rms_db")
                out["max_db_notch_excluded_tilt_removed"] = tilt_removed.get("max_db")

    return out
