# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact complete-tune captures in one clock, window and level reference."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement.alignment import fractional_shift
from jasper.audio_measurement.gating import PHASE_GATE_LEAD_MS, f_trusted_floor_hz, f_valid_floor_hz, gated_segment
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate
from jasper.active_speaker.commissioning_admission import parse_running_graph
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate, compile_candidate_config

from .forward_model import ForwardModelError, PredictedSum, acceptance_block, predicted_minus_measured_db
from .gate_sweep import N_FFT, REFERENCE_RUNG_MS
from .graph_prediction import GraphPredictionError, RelativeGraphResponse, relative_branch_response
from .round_captures import PoseCapture, capture_fingerprint, capture_row, select_capture_roles

DEFAULT_BRANCHES = ("woofer", "tweeter")


@dataclass(frozen=True)
class DiagnosticBasis:
    captures: Mapping[str, PoseCapture]
    freqs_hz: np.ndarray
    transfers: Mapping[str, np.ndarray]
    band_hz: tuple[float, float]
    window: Mapping[str, Any]

    @property
    def branches(self) -> tuple[str, ...]:
        return tuple(role for role in self.transfers if role != "summed")

    @property
    def document(self) -> Mapping[str, Any]:
        return self.captures["summed"].record_document

    @cached_property
    def source(self) -> dict[str, Any]:
        capture = self.captures["summed"]
        return {**capture_row(capture), "record_path": str(capture.record_path),
                "capture_fingerprint": capture_fingerprint(capture)}


def read_diagnostic(round_dir: Path, capture_id: str, window_ms: float,
                    *, branch_roles: tuple[str, str] = DEFAULT_BRANCHES,
                    omitted: list[dict[str, str]] | None = None) -> DiagnosticBasis:
    if not np.isfinite(window_ms) or window_ms <= 0:
        raise ForwardModelError("window_ms must be positive and finite", detail={"field": "window_ms", "capture_id": capture_id})
    if len(branch_roles) != 2 or any(not isinstance(role, str) or not role or role == "summed" for role in branch_roles) or len(set(branch_roles)) != 2:
        raise ForwardModelError("select two distinct recorded branch identities", detail={"field": "branch_roles"})
    captures = select_capture_roles(round_dir, capture_id=capture_id, roles=(*branch_roles, "summed"), omitted=omitted)
    summed = captures["summed"]
    rate = summed.sample_rate
    pre = max(float(c.preprocessing["pre_guard_samples"]) for c in captures.values())
    shifts = {
        role: pre - float(c.preprocessing["pre_guard_samples"])
        - float(c.preprocessing["clock_shift_samples"])
        for role, c in captures.items()
    }
    if not all(np.isfinite(value) for value in shifts.values()):
        raise ForwardModelError("the diagnostic clock reference is incomplete", detail={"field": "clock_shift_samples", "capture_id": capture_id})
    margin = int(np.ceil(max(abs(value) for value in shifts.values()))) + 1
    length = max(c.ir.size for c in captures.values())
    aligned = {}
    for role, capture in captures.items():
        if not np.all(np.isfinite(capture.ir)) or not np.any(capture.ir):
            raise ForwardModelError("no finite nonzero impulse", detail={"role": role, "capture_id": capture_id})
        aligned[role] = fractional_shift(
            np.pad(capture.ir, (margin, length - capture.ir.size + margin)), shifts[role],
        )
    # The basis branches choose the window; the measured sum never fits its own reference.
    anchor = min(int(np.argmax(abs(aligned[role]))) for role in branch_roles)
    span = round(window_ms * rate / 1000)
    lead = round(PHASE_GATE_LEAD_MS * rate / 1000)
    end = anchor + span + 1
    if span < 1 or span + lead + 1 > N_FFT or any(
        end > margin + c.ir.size + shifts[role] for role, c in captures.items()
    ):
        raise ForwardModelError("the common window exceeds the retained impulse or FFT span", detail={"field": "window_ms", "capture_id": capture_id, "window_ms": window_ms})
    if any(int(np.argmax(abs(aligned[role]))) >= end for role in branch_roles):
        raise ForwardModelError("the common window does not contain both direct arrivals", detail={"field": "window_ms", "capture_id": capture_id, "window_ms": window_ms})
    freqs = np.fft.rfftfreq(N_FFT, 1 / rate)
    band = (
        max(f_trusted_floor_hz(window_ms / 1000), *(c.radiated_band_hz[0] for c in captures.values())),
        min(rate / 2, *(c.radiated_band_hz[1] for c in captures.values())),
    )
    mask = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(mask):
        raise ForwardModelError("the common window and swept bands have no trusted overlap", detail={"capture_id": capture_id, "band_hz": list(band)})
    transfers = {
        role: np.fft.rfft(gated_segment(
            ir, rate, gate_ms=window_ms, peak_idx=anchor,
        )[0], n=N_FFT)[mask]
        for role, ir in aligned.items()
    }
    return DiagnosticBasis(captures, freqs[mask], transfers, band, {
        "window_ms": window_ms, "anchor_sample": anchor - margin,
        "lead_ms": PHASE_GATE_LEAD_MS,
        "validity_floor_hz": f_valid_floor_hz(window_ms / 1000),
        "trusted_floor_hz": f_trusted_floor_hz(window_ms / 1000),
        "timing_reference": "shared schedule after clock correction; one branch-derived window",
        "normalization": "exact emitted stimulus; no branch or sum level fitting; uncalibrated IR",
    })


def predict_transfer(basis: DiagnosticBasis, changes: Mapping[str, np.ndarray]) -> np.ndarray:
    return np.sum([basis.transfers[role] * changes.get(role, 1) for role in basis.branches], axis=0)


def prediction_record(basis: DiagnosticBasis, transfer: np.ndarray) -> PredictedSum:
    return PredictedSum(
        basis.freqs_hz, 20 * np.log10(np.maximum(abs(transfer), 1e-12)),
        basis.band_hz, str(basis.captures["summed"].record_path),
    )


def compare_transfer(basis: DiagnosticBasis, transfer: np.ndarray) -> dict:
    predicted = prediction_record(basis, transfer)
    actual = basis.transfers["summed"]
    delta = predicted_minus_measured_db(
        predicted, basis.freqs_hz, 20 * np.log10(np.maximum(abs(actual), 1e-12)),
        band_hz=basis.band_hz,
    )
    raw = np.asarray(delta["delta_db"]) + delta["level_offset_db"]
    delta.update(raw_rms_db=float(np.sqrt(np.mean(raw**2))), raw_max_abs_db=float(np.max(abs(raw))))
    delta["level_comparability"] = {
        "verified": True, "reasons": [],
        "raw_metrics": "observed level differences; configuration-only attribution requires comparable capture gain and volume",
    }
    # Deep cancellations and weak bins cannot support a confident phase error.
    reference = np.sum([abs(basis.transfers[role]) for role in basis.branches], axis=0)
    reliable = (np.minimum(abs(transfer), abs(actual)) > np.max(reference) * 1e-3)
    reliable &= np.minimum(abs(transfer), abs(actual)) > reference * .01
    phase = np.degrees(np.angle(transfer * actual.conjugate()))
    delta["phase"] = {
        "status": "available" if np.any(reliable) else "unavailable",
        "rms_deg": float(np.sqrt(np.mean(phase[reliable]**2))) if np.any(reliable) else None,
        "max_abs_deg": float(np.max(abs(phase[reliable]))) if np.any(reliable) else None,
        "included_points": int(np.sum(reliable)), "excluded_points": int(np.sum(~reliable)),
        "limits": "exclude below -60 dB of peak branch sum or -40 dB relative cancellation; SNR is not established",
    }
    return delta


def _recorded_graph(basis: DiagnosticBasis) -> Mapping[str, Any]:
    graph = (basis.document.get("provenance") or {}).get("graph") or {}
    config = graph.get("config")
    if not isinstance(config, Mapping) or json_fingerprint(config) != graph.get("fingerprint"):
        raise ForwardModelError(
            "the take has no intact played graph snapshot; retain a new diagnostic take",
            reason="forward_model_graph_mismatch",
            detail={"capture_id": basis.source["capture_id"], "graph_fingerprint": graph.get("fingerprint")},
        )
    return config


def _relative(source, target, basis, channels, source_inputs, target_inputs) -> RelativeGraphResponse:
    try:
        return relative_branch_response(
            source, target, basis.freqs_hz,
            role_output_channels=channels,
            source_input_weights_by_role=source_inputs,
            target_input_weights_by_role=target_inputs,
            valid_band_hz_by_role={role: basis.band_hz for role in channels},
        )
    except GraphPredictionError as exc:
        raise ForwardModelError(str(exc), detail={"capture_id": basis.source["capture_id"], "field": "relative_graph"}) from exc


def _metric_summary(delta: Mapping[str, Any]) -> dict:
    return {key: delta[key] for key in (
        "compared_band_hz", "compared_points", "level_offset_db", "rms_db",
        "max_abs_db", "raw_rms_db", "raw_max_abs_db", "phase",
        "level_comparability",
    )}


def capture_prediction(
    round_dir: Path, *, capture_id: str,
    candidate: MeasuredCrossoverCandidate | None = None,
    basis_candidate: MeasuredCrossoverCandidate | None = None,
) -> dict[str, Any]:
    omitted: list[dict[str, str]] = []
    basis = read_diagnostic(round_dir, capture_id, REFERENCE_RUNG_MS, omitted=omitted)
    reconstruction_tf = predict_transfer(basis, {})
    reconstruction = compare_transfer(basis, reconstruction_tf)
    changes = None
    if candidate is not None:
        try:
            source_candidate = (
                basis_candidate if basis_candidate is not None
                else find_banked_candidate(basis.source["candidate_id"]).candidate
            )
        except CandidateBankRefusal as exc:
            raise ForwardModelError("source candidate lookup failed", reason="forward_model_source_candidate_unavailable", detail={
                "candidate_id": basis.source["candidate_id"], "lookup_reason": exc.code, "lookup_detail": exc.detail,
            }) from exc
        if source_candidate.fingerprint != basis.source["candidate_id"]:
            raise ForwardModelError("source candidate does not match the exact recorded candidate", reason="forward_model_candidate_mismatch", detail={
                "capture_id": capture_id, "expected_candidate_id": basis.source["candidate_id"], "actual_candidate_id": source_candidate.fingerprint,
            })
        if candidate.source_preset != source_candidate.source_preset or candidate.room_correction or source_candidate.room_correction:
            raise ForwardModelError("this forecast requires the same speaker base and no room correction")
        outputs = candidate.source_preset.channel_map.outputs
        channels = {output.driver_role: output.index for output in outputs}
        if len(outputs) != 2 or set(channels) != set(basis.branches):
            raise ForwardModelError("candidate prediction needs an unambiguous output binding for each recorded branch",
                                    detail={"field": "branch_output_binding", "branches": list(basis.branches)})
        _recorded_graph(basis)
        source_graph, target_graph = [parse_running_graph(compile_candidate_config(c, playback_device="prediction")) for c in (source_candidate, candidate)]
        stereo = {role: {0: 1.0, 1: 1.0} for role in channels}
        changes = _relative(source_graph, target_graph, basis, channels, stereo, stereo)
        valid = np.logical_and.reduce(list(changes.usable_by_role.values()))
        if not np.any(valid):
            raise ForwardModelError("source graph has no usable branch overlap for replacement prediction")
        basis = replace(basis, freqs_hz=basis.freqs_hz[valid], transfers={role: tf[valid] for role, tf in basis.transfers.items()})
        transfer = predict_transfer(basis, {role: tf[valid] for role, tf in changes.responses_by_role.items()})
    else:
        if basis_candidate is not None:
            raise ForwardModelError("source candidate lookup is only needed with --candidate-json")
        transfer = reconstruction_tf
    predicted = prediction_record(basis, transfer)
    summary = {
        "basis": basis.source, "omitted": omitted,
        "candidate_id": candidate.fingerprint if candidate is not None else basis.source["candidate_id"],
        "window": dict(basis.window),
        "branches": list(basis.branches),
        "reconstruction": _metric_summary(reconstruction),
        "acceptance": acceptance_block(
            str(basis.captures["summed"].record_path) if candidate is None else None
        ),
        "limits": "Reconstruction checks this take only. Forecast assumes linear operation and unchanged setup; inspect window sensitivity. Magnitude errors can be dominated by low-SNR cancellation bins. This result does not authorize or block playback.",
    }
    summary["prediction_fingerprint"] = json_fingerprint({
        "basis": basis.source["capture_fingerprint"], "candidate_id": summary["candidate_id"],
        "window": basis.window,
        "prediction": {key: value for key, value in predicted.to_dict().items() if key != "take_path"},
    })
    return {
        "schema_version": 1, "kind": "jts_capture_prediction", "summary": summary,
        "prediction": predicted.to_dict(), "reconstruction": reconstruction,
        "relative_graph": changes.to_dict() if changes is not None else None,
        "limitations": [
            "Reconstruction tests this take; a changed-candidate forecast needs its own recording.",
            "Predictions assume linear operation and unchanged speaker, placement and capture chain.",
            "A finite window can change filter transients; inspect window sensitivity before narrow correction.",
            "Fixed base protection and device settings are held.",
            "No score or mismatch here vetoes a safe experiment.",
        ],
    }
