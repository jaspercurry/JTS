# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Speaker fit proposals and the engine's banked alignment and trim evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.crossover_v2.conductor_context import _resolve_driver_class_by_role
from jasper.active_speaker.crossover_v2.intervention import DriverEvidence, boost_allowed, fit_branches
from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_MEASURE, STAGE_MEASURE_CAPABILITIES, open_stage
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, RoundViewsError, round_artifact_dir
from jasper.active_speaker.crossover_v2.round_views import response_from_banked_curve
from jasper.active_speaker.crossover_v2.spatial import _primary_sweep_bands
from jasper.active_speaker.linearization_envelope import EnvelopeCurve
from jasper.active_speaker.linearization_budget import fit_budgets_by_role, normalise_fit_budget
from jasper.active_speaker.linearization_fit import FitVocabulary
from jasper.active_speaker.measurement_programs import PURPOSE_SPEAKER, run_purpose
from jasper.audio_measurement.bundles import relative_artifact_path
from jasper.audio_measurement.mic_identity import mic_tier_for_model
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.spatial_combine import octave_bands_hz

from .crossover_v2.round_inputs import resolve_set


class SpeakerFitUnreadable(RoundViewsError):
    pass


def _envelope_answer(envelope: EnvelopeCurve) -> dict[str, Any]:
    bands = []
    for center, lo, hi in octave_bands_hz(envelope.freqs_hz[0], envelope.freqs_hz[-1]):
        mask = (envelope.freqs_hz >= lo) & (envelope.freqs_hz <= hi)
        depth = envelope.allowed_depth_db[mask]
        if depth.size:
            bands.append({
                "center_hz": center, "band_hz": [lo, hi],
                "min_depth_db": float(np.min(depth)), "max_depth_db": float(np.max(depth)),
            })
    return {"bands": bands, "sigma_source": "paired_repeats" if envelope.sigma_db is not None else "unavailable"}


def _read_candidate(path: Path) -> dict[str, Any]:
    candidate = json.loads(path.read_text())
    if not isinstance(candidate, dict) or not all(
        isinstance(candidate.get(key), dict) for key in ("source_preset", "analysis")
    ):
        raise RoundViewsError("candidate requires source_preset and analysis objects")
    return candidate


def _round_candidate(directory: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    path = directory / "candidate.json"
    if path.is_file():
        return _read_candidate(path)
    from jasper.active_speaker.candidate_bank import find_banked_candidate  # lazy: bank scan cost

    base = next((group["capture_basis"].get("candidate_id") for group in manifest["sets"]
                 if group["capture_basis"].get("graph_scope") == "candidate"
                 and group["capture_basis"].get("candidate_id")), None)
    if not base:
        raise RoundViewsError("speaker-fit requires the round's candidate or a banked base")
    return find_banked_candidate(base).candidate.to_dict()


def _production_vocabulary(inputs: RoundInputs, candidate: dict[str, Any]) -> str:
    if inputs.state_path is None:
        raise RoundViewsError("production vocabulary requires the capture's journey state")
    state = json.loads(inputs.state_path.read_text())
    plan = open_stage(
        STAGE_MEASURE_CAPABILITIES, index_phase_map=dict(enumerate(state["session_phases"])),
    ).plan
    allowed = boost_allowed(
        post_apply_verifies=plan.post_apply_verifies,
        cloud_phase_planned=PHASE_CLOUD_MEASURE in plan.phases,
        cloud_present=bool(candidate.get("exclusion_evidence")),
    )
    return "bounded_boost" if allowed else "cut_only"


def speaker_fit(
    inputs: RoundInputs, manifest: Mapping[str, Any], set_id: str, take_id: str | None = None,
    *, vocabulary: str | None = None, budget: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = resolve_set(inputs, set_id, manifest=manifest)
    take_id = selected.take_id(take_id)
    take = next(take for take in selected.takes if take["take_id"] == take_id)
    path = take_artifact_path(inputs.session_dir, take["artifacts"]["record_id"])
    relative_artifact_path(inputs.session_dir, path)
    record = json.loads(path.read_text())
    program = ExcitationProgram.from_dict(record["program"])
    if run_purpose(manifest["program"]) != PURPOSE_SPEAKER or program.phase != "measure":
        raise RoundViewsError("speaker-fit requires a Speaker MEASURE take")
    if program.program_id != selected.capture_basis["program_id"] or record["take_id"] != take_id:
        raise RoundViewsError("selected take does not match its manifest")
    directory, _ = round_artifact_dir(inputs.session_dir)
    assert directory is not None
    try:
        candidate = _round_candidate(directory, manifest)
        vocabulary = vocabulary or _production_vocabulary(inputs, candidate)
    except (OSError, ValueError, TypeError, LookupError) as exc:
        raise SpeakerFitUnreadable(str(exc)) from exc
    analysis = take.get("analysis") or candidate["analysis"]
    if analysis["program_id"] != program.program_id:
        raise RoundViewsError("banked analysis does not match the selected program")
    matching_takes = {take["take_id"] for group in manifest["sets"]
                      if group["capture_basis"].get("program_id") == program.program_id
                      for take in group["takes"] if take["selected"]}
    if not take.get("analysis") and matching_takes != {take_id}:
        raise RoundViewsError("banked analysis cannot distinguish the selected program's takes")
    if not inputs.banked or inputs.design_draft_path is None:
        raise RoundViewsError("speaker-fit requires the banked driver declaration")
    draft = json.loads(inputs.design_draft_path.read_text())
    classes = _resolve_driver_class_by_role(draft)
    budgets = fit_budgets_by_role(draft.get("driver_safety_profile") or {})
    overrides = normalise_fit_budget(budget or {})
    calibration = (record.get("capture_setup") or {}).get("calibration") or {}
    applied = record.get("capture_calibration") or {}
    model = calibration.get("model") if (
        applied.get("applied") is True and applied.get("calibration_id") == calibration.get("calibration_id")
    ) else None
    tier = mic_tier_for_model(model)
    bands = _primary_sweep_bands(program)
    if not 1 <= len(bands) <= 2:
        raise RoundViewsError("speaker-fit requires one or two measured driver roles")
    curves = {curve["role"]: curve for curve in record.get("curves") or []} or {
        row["role"]: row["curve"] for group in manifest["sets"] for row in group["takes"]
        if row["take_id"] == take_id and row.get("role") and row.get("curve")}
    drivers = []
    for role, band in bands.items():
        response = response_from_banked_curve(curves[role])
        if response is None:
            raise RoundViewsError(f"fit inputs are not banked for {role}")
        drivers.append(DriverEvidence(role, response[0], band, classes.get(role, "unknown"),
                                      {**budgets.get(role, {}), **overrides}))
    branches = fit_branches(
        drivers, source_preset=candidate["source_preset"], mic_tiers={driver.role: tier for driver in drivers},
        vocabulary=FitVocabulary(allow_boost=vocabulary == "bounded_boost"),
    )
    return dict(
        set_id=selected.set_id, take_id=take_id, vocabulary=vocabulary,
        linearization={driver.role: {
            "excited_band_hz": list(driver.excited_band_hz),
            "envelope": _envelope_answer(branches.envelopes[driver.role]),
            "fit": branches.fits[driver.role].to_dict(),
        } for driver in drivers},
        alignment={
            "committed": {"delay_us": analysis.get("delay_us"), "polarity": analysis.get("polarity"),
                          "ripple_db": analysis.get("predicted_ripple_db")},
            "seed": {"delay_us": analysis.get("alignment_seed_delay_us"), "polarity": analysis.get("seed_polarity"),
                     "ripple_db": analysis.get("alignment_seed_ripple_db")},
            "objective": analysis.get("alignment_objective"),
            **{key: analysis.get(key) for key in (
                "drift_us", "flatness_improvement_db", "anchor_delay_us", "snap_delta_us",
            )},
        },
        trim=analysis.get("trim_decision"),
    )
