# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact complete-tune captures in one clock, window and level reference."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement.alignment import fractional_shift
from jasper.audio_measurement.gating import f_trusted_floor_hz, f_valid_floor_hz
from jasper.audio_measurement.program_analysis import predicted_branch_sum
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate, load_candidate_artifact
from jasper.active_speaker.commissioning_admission import parse_running_graph
from jasper.active_speaker.measured_crossover_candidate import compile_candidate_config

from .forward_model import ForwardModelError, PredictedSum, acceptance_block, predicted_minus_measured_db
from .gate_sweep import N_FFT, PHASE_GATE_LEAD_MS, REFERENCE_RUNG_MS, gated_segment
from .graph_prediction import relative_branch_response
from .round_captures import PoseCapture, capture_row, select_capture

ROLES = ("woofer", "tweeter", "summed")


@dataclass(frozen=True)
class DiagnosticBasis:
    captures: Mapping[str, PoseCapture]
    document: Mapping[str, Any]
    freqs_hz: np.ndarray
    transfers: Mapping[str, np.ndarray]
    band_hz: tuple[float, float]
    window: Mapping[str, Any]

    @property
    def source(self) -> dict[str, Any]:
        capture = self.captures["summed"]
        return {**capture_row(capture), "record_path": str(capture.record_path),
                "record_fingerprint": json_fingerprint(self.document)}


def read_diagnostic(round_dir: Path, capture_id: str, window_ms: float) -> DiagnosticBasis:
    if not np.isfinite(window_ms) or window_ms <= 0:
        raise ForwardModelError("window_ms must be positive and finite")
    captures = {role: select_capture(round_dir, capture_id=capture_id, role=role) for role in ROLES}
    summed = captures["summed"]
    if summed.record_path is None:
        raise ForwardModelError("the capture has no exact record")
    document = json.loads(summed.record_path.read_text())
    if not document.get("branch_diagnostic"):
        raise ForwardModelError("the selected take has no complete-tune branch diagnostic")
    if len({(c.capture_sha256, c.sample_rate, c.graph_fingerprint) for c in captures.values()}) != 1:
        raise ForwardModelError("branches do not share one recording, sample rate and graph")
    rate = summed.sample_rate
    pre = max(float(c.preprocessing["pre_guard_samples"]) for c in captures.values())
    shifts = {
        role: pre - float(c.preprocessing["pre_guard_samples"])
        - float(c.preprocessing["clock_shift_samples"])
        for role, c in captures.items()
    }
    if not all(np.isfinite(value) for value in shifts.values()):
        raise ForwardModelError("the diagnostic clock reference is incomplete")
    margin = int(np.ceil(max(abs(value) for value in shifts.values()))) + 1
    length = max(c.ir.size for c in captures.values())
    aligned = {}
    for role, capture in captures.items():
        if not np.all(np.isfinite(capture.ir)) or not np.any(capture.ir):
            raise ForwardModelError(f"{role} has no finite nonzero impulse")
        aligned[role] = fractional_shift(
            np.pad(capture.ir, (margin, length - capture.ir.size + margin)), shifts[role],
        )
    # The basis branches choose the window; the measured sum never fits its own reference.
    anchor = min(int(np.argmax(abs(aligned[role]))) for role in ROLES[:2])
    span = round(window_ms * rate / 1000)
    lead = round(PHASE_GATE_LEAD_MS * rate / 1000)
    end = anchor + span + 1
    if span < 1 or span + lead + 1 > N_FFT or any(
        end > margin + c.ir.size + shifts[role] for role, c in captures.items()
    ):
        raise ForwardModelError("the common window exceeds the retained impulse or FFT span")
    if any(int(np.argmax(abs(aligned[role]))) >= end for role in ROLES[:2]):
        raise ForwardModelError("the common window does not contain both direct arrivals")
    freqs = np.fft.rfftfreq(N_FFT, 1 / rate)
    band = (
        max(f_trusted_floor_hz(window_ms / 1000), *(c.radiated_band_hz[0] for c in captures.values())),
        min(rate / 2, *(c.radiated_band_hz[1] for c in captures.values())),
    )
    mask = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(mask):
        raise ForwardModelError("the common window and swept bands have no trusted overlap")
    transfers = {
        role: np.fft.rfft(gated_segment(
            ir, rate, gate_ms=window_ms, peak_idx=anchor,
        )[0], n=N_FFT)[mask]
        for role, ir in aligned.items()
    }
    return DiagnosticBasis(captures, document, freqs[mask], transfers, band, {
        "window_ms": window_ms, "anchor_sample": anchor - margin,
        "lead_ms": PHASE_GATE_LEAD_MS,
        "validity_floor_hz": f_valid_floor_hz(window_ms / 1000),
        "trusted_floor_hz": f_trusted_floor_hz(window_ms / 1000),
        "timing_reference": "shared schedule after clock correction; one branch-derived window",
        "normalization": "exact emitted stimulus; no branch or sum level fitting; uncalibrated IR",
    })


def predict_transfer(basis: DiagnosticBasis, changes: Mapping[str, np.ndarray]) -> np.ndarray:
    return predicted_branch_sum(
        basis.transfers["woofer"] * changes.get("woofer", 1),
        basis.transfers["tweeter"] * changes.get("tweeter", 1),
        0, 0, 1, freqs_hz=basis.freqs_hz, residual_delay_us=0,
    )


def prediction_record(basis: DiagnosticBasis, transfer: np.ndarray) -> PredictedSum:
    return PredictedSum(
        basis.freqs_hz, 20 * np.log10(np.maximum(abs(transfer), 1e-12)),
        basis.band_hz, str(basis.captures["summed"].record_path),
    )


def compare_transfer(basis: DiagnosticBasis, transfer: np.ndarray, measured: DiagnosticBasis) -> dict:
    predicted = prediction_record(basis, transfer)
    actual = measured.transfers["summed"]
    delta = predicted_minus_measured_db(
        predicted, measured.freqs_hz, 20 * np.log10(np.maximum(abs(actual), 1e-12)),
        band_hz=measured.band_hz,
    )
    raw = np.asarray(delta["delta_db"]) + delta["level_offset_db"]
    delta.update(raw_rms_db=float(np.sqrt(np.mean(raw**2))), raw_max_abs_db=float(np.max(abs(raw))))
    same_take = basis.source == measured.source
    level_reasons = [] if same_take else ["separate_capture_gain_unverified"]
    for key in ("main_volume_db", "session_volume_db"):
        source_level = (basis.document.get("provenance") or {}).get(key)
        measured_level = (measured.document.get("provenance") or {}).get(key)
        if not same_take and (source_level is None or measured_level is None):
            level_reasons.append(f"{key}_missing")
        elif source_level != measured_level:
            level_reasons.append(f"{key}_changed")
    delta["level_comparability"] = {
        "verified": same_take, "reasons": level_reasons,
        "raw_metrics": "observed level differences; configuration-only attribution requires comparable capture gain and volume",
    }
    delta["phase"] = {"status": "unavailable", "reason": "separate recordings have no shared absolute time origin"}
    if same_take and np.array_equal(basis.freqs_hz, measured.freqs_hz):
        # Deep cancellations and weak bins cannot support a confident phase error.
        reference = abs(basis.transfers["woofer"]) + abs(basis.transfers["tweeter"])
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


def _candidate(path: Path):
    candidate = load_candidate_artifact(path)
    if candidate is None:
        raise ForwardModelError(f"{path}: no intact complete candidate")
    return candidate


def _recorded_graph(basis: DiagnosticBasis) -> Mapping[str, Any]:
    graph = (basis.document.get("provenance") or {}).get("graph") or {}
    config = graph.get("config")
    if not isinstance(config, Mapping) or json_fingerprint(config) != graph.get("fingerprint"):
        raise ForwardModelError("the take has no intact played graph snapshot; retain a new diagnostic take")
    return config


def _relative(source, target, basis, channels, source_inputs, target_inputs):
    return relative_branch_response(
        source, target, basis.freqs_hz,
        role_output_channels=channels,
        source_input_weights_by_role=source_inputs,
        target_input_weights_by_role=target_inputs,
        valid_band_hz_by_role={role: basis.band_hz for role in channels},
    )


def _metric_summary(delta: Mapping[str, Any] | None) -> dict | None:
    if delta is None:
        return None
    return {key: delta[key] for key in (
        "compared_band_hz", "compared_points", "level_offset_db", "rms_db",
        "max_abs_db", "raw_rms_db", "raw_max_abs_db", "phase",
        "level_comparability",
    )}


def capture_prediction(
    round_dir: Path, *, capture_id: str, window_ms: float | None = None,
    candidate_path: Path | None = None, basis_candidate_path: Path | None = None,
    candidate_root: Path | None = None, measured_round: Path | None = None,
    measured_capture_id: str | None = None,
    expected_prediction_fingerprint: str | None = None,
) -> dict[str, Any]:
    if measured_round is not None and measured_capture_id is None:
        raise ForwardModelError("--measured-round requires an exact --measured-capture-id")
    if expected_prediction_fingerprint is not None and measured_capture_id is None:
        raise ForwardModelError("an expected prediction fingerprint requires a measured capture")
    basis = read_diagnostic(round_dir, capture_id, REFERENCE_RUNG_MS if window_ms is None else window_ms)
    reconstruction_tf = predict_transfer(basis, {})
    reconstruction = compare_transfer(basis, reconstruction_tf, basis)
    candidate = None
    changes = None
    channels = None
    if candidate_path is not None:
        candidate = _candidate(candidate_path)
        try:
            source_candidate = (
                _candidate(basis_candidate_path) if basis_candidate_path is not None
                else find_banked_candidate(basis.source["candidate_id"], root=candidate_root).candidate
            )
        except CandidateBankRefusal as exc:
            raise ForwardModelError(f"source candidate: {exc.code}: {exc.detail}") from exc
        if source_candidate.fingerprint != basis.source["candidate_id"]:
            raise ForwardModelError("source candidate does not match the exact recorded candidate")
        if candidate.source_preset != source_candidate.source_preset or candidate.room_correction or source_candidate.room_correction:
            raise ForwardModelError("this forecast requires the same speaker base and no room correction")
        outputs = candidate.source_preset.channel_map.outputs
        channels = {output.driver_role: output.index for output in outputs}
        if len(outputs) != 2 or set(channels) != set(ROLES[:2]):
            raise ForwardModelError("the diagnostic predicts one woofer and one tweeter")
        _recorded_graph(basis)
        graphs = [parse_running_graph(compile_candidate_config(c, playback_device="prediction")) for c in (source_candidate, candidate)]
        stereo = {role: {0: 1.0, 1: 1.0} for role in channels}
        changes = _relative(*graphs, basis, channels, stereo, stereo)
        valid = np.logical_and.reduce(list(changes.usable_by_role.values()))
        if not np.any(valid):
            raise ForwardModelError("source graph has no usable branch overlap for replacement prediction")
        basis = replace(basis, freqs_hz=basis.freqs_hz[valid], transfers={role: tf[valid] for role, tf in basis.transfers.items()})
        transfer = predict_transfer(basis, {role: tf[valid] for role, tf in changes.responses_by_role.items()})
    else:
        if basis_candidate_path is not None or candidate_root is not None:
            raise ForwardModelError("source candidate lookup is only needed with --candidate-json")
        transfer = reconstruction_tf
    predicted = prediction_record(basis, transfer)
    measured = None
    comparison = None
    context = None
    if measured_capture_id is not None:
        measured = read_diagnostic(measured_round or round_dir, measured_capture_id, basis.window["window_ms"])
        expected_candidate = candidate.fingerprint if candidate is not None else basis.source["candidate_id"]
        if not expected_candidate or measured.source["candidate_id"] != expected_candidate:
            raise ForwardModelError("comparison take does not name the predicted candidate")
        pose_fields = ("position_deg", "vertical_deg", "mark_distance_m", "pose_kind", "seat_offset_m")
        if any(basis.document.get(key) != measured.document.get(key) for key in pose_fields):
            raise ForwardModelError("comparison take has a different declared microphone pose")
        if candidate is not None:
            def input_weights(read):
                records = {r["role"]: r for r in read.document["branch_diagnostic"]["responses"]}
                return {role: {int(records[role]["input_channel"]): 1.0} for role in channels}
            observed = _relative(
                _recorded_graph(basis), _recorded_graph(measured), basis, channels,
                input_weights(basis), input_weights(measured),
            )
            for role in channels:
                planned = changes.responses_by_role[role][valid]
                if not np.all(observed.usable_by_role[role]) or not np.allclose(observed.responses_by_role[role], planned, rtol=1e-6, atol=1e-8):
                    raise ForwardModelError("recorded graph change differs from the predicted complete candidate change")
        elif json_fingerprint(_recorded_graph(basis)) != json_fingerprint(_recorded_graph(measured)):
            raise ForwardModelError("a same-candidate repeat requires the same played graph")
        comparison = compare_transfer(basis, transfer, measured)
        level_keys = ("main_volume_db", "session_volume_db")
        context = {
            "basis_volume_db": {key: (basis.document.get("provenance") or {}).get(key) for key in level_keys},
            "measured_volume_db": {key: (measured.document.get("provenance") or {}).get(key) for key in level_keys},
            "pose": {key: basis.document.get(key) for key in pose_fields},
            "limitation": "Declared pose equality does not prove unchanged placement or capture gain. Level error includes any such change.",
        }
    summary = {
        "basis": basis.source, "candidate_id": candidate.fingerprint if candidate is not None else basis.source["candidate_id"],
        "measured": measured.source if measured is not None else None,
        "window": dict(basis.window),
        "reconstruction": _metric_summary(reconstruction),
        "predicted_minus_measured": _metric_summary(comparison),
        "acceptance": acceptance_block(
            str(measured.captures["summed"].record_path) if measured is not None
            else str(basis.captures["summed"].record_path) if candidate is None else None
        ),
        "comparison_kind": "changed_candidate" if measured is not None and candidate is not None else "same_candidate_repeat" if measured is not None else "unmeasured_forecast" if candidate is not None else "same_take_reconstruction",
        "comparison_context": context,
        "limits": "Reconstruction checks this take only. Forecast assumes linear operation and unchanged setup; inspect window sensitivity. Magnitude errors can be dominated by low-SNR cancellation bins. This result does not authorize or block playback.",
    }
    summary["prediction_fingerprint"] = json_fingerprint({
        "basis": basis.source, "candidate_id": summary["candidate_id"],
        "window": basis.window, "prediction": predicted.to_dict(),
    })
    if expected_prediction_fingerprint is not None and expected_prediction_fingerprint != summary["prediction_fingerprint"]:
        raise ForwardModelError("the comparison does not match the saved prediction fingerprint")
    summary["forecast_binding"] = {
        "status": "matched" if expected_prediction_fingerprint is not None else "not_requested",
        "expected_prediction_fingerprint": expected_prediction_fingerprint,
    }
    return {
        "schema_version": 1, "kind": "jts_capture_prediction", "summary": summary,
        "prediction": predicted.to_dict(), "reconstruction": reconstruction,
        "predicted_minus_measured": comparison,
        "relative_graph": changes.to_dict() if changes is not None else None,
        "limitations": [
            "Reconstruction tests this take; a changed-candidate forecast needs its own recording.",
            "Predictions assume linear operation and unchanged speaker, placement and capture chain.",
            "A finite window can change filter transients; inspect window sensitivity before narrow correction.",
            "Fixed base protection and device settings are held; observed graph changes are checked when comparing.",
            "No score or mismatch here vetoes a safe experiment.",
        ],
    }
