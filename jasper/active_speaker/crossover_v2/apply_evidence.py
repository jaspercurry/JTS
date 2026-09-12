# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Read apply-time trial evidence and disclose its independent quality advice."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jasper.audio_measurement.bundles import BundleError
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis import CaptureIntegrity, IntegrityCheck, MeasurementGeometry, analyze_program_capture
from jasper.audio_measurement.household_mic import resolve_setup_calibration
from jasper.audio_measurement.wired_capture import decode_wav_to_mono
from .. import bundles
from ..commissioning_evidence_store import EVIDENCE_ROOT, CommissioningEvidenceStoreError
from ..flat_spec import FlatSpecReport, evaluate_flat_spec
from ..measured_crossover_candidate import MeasuredCrossoverCandidate
from ..run_manifest import RUN_MANIFEST_FILENAME, TAKE_MEASURED
from .contracts import VERIFY_TOLERANCE_DB, CaptureValidity
from .record_index import reopen_measurement_capture
from .round_evidence import MEASURED_BENEFIT_MARGIN_DB, EntryBaseline, benefit_comparands, measured_response_from_analysis
from .round_inputs import iter_round_sessions, round_artifact_dir
from .verification import (
    Verdict, evaluate_benefit, evaluate_capture_validity, evaluate_realization,
    evaluate_spec, verification_result,
)


def candidate_trial_manifest(fingerprint: str, profile: Mapping[str, Any]) -> dict[str, Any] | None:
    graph = str((profile.get("config") or {}).get("sha256") or "")[:16]
    found: list[tuple[bool, float, dict[str, Any]]] = []
    for bundle in iter_round_sessions(bundles.sessions_dir() / "apply"):
        directory, _ = round_artifact_dir(bundle)
        if directory is None:
            continue
        path = directory / RUN_MANIFEST_FILENAME
        try:
            document = json.loads(path.read_text())
            for group in document.get("sets", []):
                basis = group.get("capture_basis") or {}
                if basis.get("candidate_id") == fingerprint and basis.get("role") == "summed":
                    trial = {**document, "set": group, "bundle": str(bundle), "manifest_path": str(path)}
                    found.append((document.get("status") == "complete" and basis.get("submitted_graph_fingerprint") == graph,
                                  path.stat().st_mtime, trial))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return max(found, key=lambda row: row[:2])[2] if found else None


def trial_records(trial: Mapping[str, Any]):
    bundle = Path(trial["bundle"])
    for take in trial["set"]["takes"]:
        record_id = take["artifacts"]["record_id"]
        path = bundle / EVIDENCE_ROOT / "artifacts" / record_id
        record, wav = reopen_measurement_capture(bundle, path)
        yield take, record, wav


def trial_is_intact(trial: Mapping[str, Any]) -> bool:
    try:
        basis = trial["set"]["capture_basis"]
        rows = 0
        for take, record, captured in trial_records(trial):
            rows += 1
            if (captured is None or take["quality"]["status"] != TAKE_MEASURED
                    or record.get("candidate_id") != basis["candidate_id"]
                    or record.get("graph_scope") != "candidate"
                    or record.get("graph_fingerprint") != basis["submitted_graph_fingerprint"]
                    or record.get("wav_sha256") != take["artifacts"].get("wav_sha256")):
                return False
        return rows > 0
    except (OSError, ValueError, TypeError, AttributeError, KeyError, BundleError, CommissioningEvidenceStoreError):
        return False


def changed_layers(profile: Mapping[str, Any], applied: Mapping[str, Any] | None) -> list[str]:
    now = profile.get("recomposition_snapshot") or {}
    before = (applied or {}).get("recomposition_snapshot") or {}
    speaker = ("preset", "corrections", "linearization", "blend_correction")
    return [name for name, keys in (("speaker", speaker), ("room", ("room_correction",)),
                                    ("bass", ("bass_extension",)))
            if not before or any(now.get(key) != before.get(key) for key in keys)]


def verification_disclosure(
    candidate: MeasuredCrossoverCandidate, trial: Mapping[str, Any],
    applied: Mapping[str, Any] | None, *, profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    layers = changed_layers(profile or {}, applied)
    previous = (applied or {}).get("trial_verification") or {}
    reuse = bool(layers) and "speaker" not in layers and bool(previous.get("speaker_evidence"))
    takes = []
    for take, record, wav in trial_records(trial):
        raw_integrity = record.get("capture_integrity") or {}
        integrity = CaptureIntegrity(checks=tuple(IntegrityCheck(**check) for check in raw_integrity.get("checks", ())))
        capture = evaluate_capture_validity(integrity)
        realization = evaluate_realization(tracking=record.get("verify_tracking"), tolerance_db=VERIFY_TOLERANCE_DB)
        raw_spec = record.get("spec_report") or {}
        spec = evaluate_spec(FlatSpecReport.from_dict(raw_spec) if raw_spec.get("bands") else None)
        baseline = EntryBaseline.from_dict(record.get("entry_baseline"))
        post = None
        analysis_error = None
        if record.get("program") and wav is not None:
            try:
                program = ExcitationProgram.from_dict(record["program"])
                samples, rate = decode_wav_to_mono(wav)
                calibration = resolve_setup_calibration(record.get("capture_setup"), device=record.get("capture_device"))
                analysis = analyze_program_capture(program, samples, rate,
                    calibration=calibration.curve if calibration else None,
                    geometry=MeasurementGeometry(), capture_report=record.get("capture_integrity"))
                capture = evaluate_capture_validity(analysis.capture_integrity)
                realization = evaluate_realization(tracking=analysis.verify_tracking, tolerance_db=VERIFY_TOLERANCE_DB)
                post = measured_response_from_analysis(analysis, reference_mark=str(take.get("pose_index", "")))
                if post is not None:
                    spec = evaluate_spec(evaluate_flat_spec(post.curve.freqs_hz, post.curve.magnitude_db,
                                                           exclusion_mask=post.excluded))
            except (ValueError, KeyError, OSError) as exc:
                analysis_error = type(exc).__name__
        before, after = benefit_comparands(baseline=baseline.as_measurement() if baseline else None, post=post)
        benefit = evaluate_benefit(entry_baseline=before, post=after, margin_db=MEASURED_BENEFIT_MARGIN_DB)
        if reuse:
            evidence = previous["speaker_evidence"]
            capture = Verdict(CaptureValidity(evidence["capture_validity"]), "speaker_trial_reused", evidence)
            realization = Verdict(type(realization.status)(evidence["realization"]), "speaker_trial_reused", evidence)
        result = verification_result(capture=capture, realization=realization, benefit=benefit, spec=spec)
        takes.append({"take_id": take["take_id"], **result.to_dict(), "analysis_error": analysis_error})
    # Preserve every take's advice; disagreement is explicit, never a selected best take.
    dimensions = {key: next(iter(values)) if len(values) == 1 else "mixed"
                  for key in ("capture_validity", "realization", "benefit", "spec")
                  for values in ({row[key] for row in takes},)}
    speaker_evidence = previous["speaker_evidence"] if reuse else {
        **{key: dimensions[key] for key in ("capture_validity", "realization")},
        "candidate_fingerprint": candidate.fingerprint, "manifest_path": trial["manifest_path"],
        "set_id": trial["set"]["set_id"],
    }
    return {**dimensions, "candidate_fingerprint": candidate.fingerprint,
            "manifest_path": trial["manifest_path"], "set_id": trial["set"]["set_id"],
            "graph_fingerprint": trial["set"]["capture_basis"]["submitted_graph_fingerprint"],
            "layers_changed": layers, "speaker_evidence_reused": reuse,
            "speaker_evidence": speaker_evidence, "takes": takes}
