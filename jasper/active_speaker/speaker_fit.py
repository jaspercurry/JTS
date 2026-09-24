# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Speaker fit proposals, resolved trims, and banked alignment evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.design_draft import design_draft_view
from jasper.active_speaker.branch_chain import sections_by_role
from jasper.active_speaker.camilla_yaml import boost_headroom_by_role
from jasper.active_speaker.alignment_evidence import alignment_evidence
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
from jasper.active_speaker.crossover_v2.conductor_context import _resolve_driver_class_by_role
from jasper.active_speaker.crossover_v2.intervention import CloudFitTerms, DriverEvidence, NonFiniteTrimError, fit_branches, resolve_trims_after_fit
from jasper.active_speaker.crossover_v2.position_cycle import curves_for_take, take_artifact_path
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, RoundViewsError, capture_identity, latest_measure_takes, prescription_sources, round_artifact_dir, resolve_set
from jasper.active_speaker.crossover_v2.round_views import response_from_banked_curve
from jasper.active_speaker.crossover_v2.spatial import _primary_sweep_bands
from jasper.active_speaker.linearization_envelope import DEFAULT_ENVELOPE_GRID_HZ, EnvelopeCurve, ladder_smooth
from jasper.active_speaker.linearization_budget import fit_budgets_by_role, normalise_fit_budget
from jasper.active_speaker.linearization_fit import (
    FitVocabulary, LinearizationFit, complex_correction_response, linearization_filters_by_role,
)
from jasper.active_speaker.measured_crossover_candidate import room_peqs_from_correction
from jasper.active_speaker.measurement_programs import POSE_KIND_BEARING, REGIME_SUMMED
from jasper.active_speaker.profile import ActiveSpeakerPreset, CrossoverRegion
from jasper.audio_measurement.bundles import relative_artifact_path
from jasper.audio_measurement.mic_identity import mic_tier_for_model
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.series_stats import power_mean_db
from jasper.audio_measurement.spatial_combine import _band_spread, octave_bands_hz
from jasper.output_topology import OutputTopology


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
    return {"ladder": "octave", "bands": bands, "sigma_source": "paired_repeats" if envelope.sigma_db is not None else "unavailable"}


def _read_candidate(path: Path) -> dict[str, Any]:
    candidate = json.loads(path.read_text())
    if not isinstance(candidate, dict) or not all(
        isinstance(candidate.get(key), dict) for key in ("source_preset", "analysis")
    ):
        raise RoundViewsError("candidate requires source_preset and analysis objects")
    return candidate


def _round_candidate(directory: Path, sources: Mapping[str, Any]) -> dict[str, Any]:
    path = directory / "candidate.json"
    if path.is_file():
        return _read_candidate(path)
    if applied := sources.get("applied_profile"):
        return candidate_from_applied_profile(OutputTopology.from_mapping(sources["draft"]["topology"]), applied).to_dict()
    raise RoundViewsError("speaker-fit requires the round's candidate or a banked base")


def design_clouds(inputs: RoundInputs, manifest: Mapping[str, Any]) -> dict[str, CloudFitTerms]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for group in manifest.get("sets", ()):
        basis = group["capture_basis"]
        key = (*capture_identity(basis, set_id=group["set_id"]), basis.get("role"))
        groups.setdefault(key, []).append(group)
    clouds: dict[str, CloudFitTerms] = {}
    for (_, _, _, _, role), members in groups.items():
        bearings = latest_measure_takes(
            ((group, take) for group in members for take in group["takes"]),
            key=lambda group, take: (take["pose"]["deg"], take["pose"].get("elevation_deg")) if (
                role and role != REGIME_SUMMED and (take.get("role") or role) == role
                and take["pose"].get("kind") == POSE_KIND_BEARING and take["pose"].get("deg") is not None
            ) else None,
        )
        cloud = CloudFitTerms(n_positions=len(bearings))
        # Standard error needs two positions; see linearization_envelope.position_spread_db.
        if len(bearings) >= 2:
            try:
                responses = []
                lo, hi = 0.0, float("inf")
                for _group, take in bearings.values():
                    curve = take.get("curve")
                    if not curve:
                        path = take_artifact_path(inputs.session_dir, take["artifacts"]["record_id"])
                        relative_artifact_path(inputs.session_dir, path)
                        curve = next(c for c in json.loads(path.read_text()).get("curves", ()) if c["role"] == role)
                    parsed = response_from_banked_curve(curve)
                    if parsed is None or parsed[0].role != role:
                        raise ValueError("design pose has no fit response")
                    responses.append(parsed[0])
                    lo = max(lo, parsed[1][0], parsed[0].freqs_hz[0], parsed[0].fit_floor_hz or 0.0)
                    hi = min(hi, parsed[1][1], parsed[0].freqs_hz[-1])
                grid = DEFAULT_ENVELOPE_GRID_HZ[(DEFAULT_ENVELOPE_GRID_HZ >= lo) & (DEFAULT_ENVELOPE_GRID_HZ <= hi)]
                if grid.size < 2:
                    raise ValueError("design poses have no shared fit band")
                stacked = np.stack([
                    np.interp(grid, response.freqs_hz, response.magnitude_db) for response in responses
                ])
                cloud = replace(cloud, band_spread=_band_spread(grid, stacked), boost_responses=tuple(responses))
            except (OSError, ValueError, TypeError, LookupError, StopIteration):
                pass
        clouds.update((group["set_id"], cloud) for group in members)
    return clouds


def fit_feature_curves(cloud: CloudFitTerms) -> list[tuple[np.ndarray, np.ndarray]]:
    curves = []
    for response in cloud.boost_responses:
        grid = DEFAULT_ENVELOPE_GRID_HZ
        grid = grid[(grid >= max(response.freqs_hz[0], response.fit_floor_hz or 0.0)) & (grid <= response.freqs_hz[-1])]
        if grid.size < 2: continue
        curves.append((grid, ladder_smooth(grid, np.interp(grid, response.freqs_hz, response.magnitude_db))))
    return curves


def _fit_vocabularies(
    candidate: Mapping[str, Any], budgets: Mapping[str, Mapping[str, Any]],
) -> dict[str, FitVocabulary]:
    sections = sections_by_role(CrossoverRegion.from_mapping(region)
                                for region in candidate["source_preset"].get("crossover_regions") or ())
    trims = candidate.get("role_attenuations_db") or {}
    linearization = linearization_filters_by_role(candidate.get("linearization") or {})
    room = candidate.get("room_correction") or {}
    context = {role: (sections.get(role, ()), float(trims.get(role, 0.0)))
               for role in sections.keys() | budgets.keys() | trims.keys() | linearization.keys()}
    room_peqs = room_peqs_from_correction(room, ActiveSpeakerPreset.from_mapping(candidate["source_preset"])) if room else ()
    vocabularies = {}
    for role, budget in budgets.items():
        headroom = boost_headroom_by_role(
            branch_context=context,
            linearization={name: filters for name, filters in linearization.items() if name != role},
            room_peqs=room_peqs,
        )
        remaining = headroom[role]["program_headroom_remaining_db"]
        vocabularies[role] = FitVocabulary(
            allow_boost=True, per_filter_boost_cap_db=remaining, composed_boost_cap_db=remaining,
        ).with_budget(budget)
    return vocabularies


#: A fit with a NaN or infinite filter term: published as unavailable, never
#: as a number JSON cannot carry.
REASON_FIT_NOT_FINITE = "fit_not_finite"


def _filters_finite(fit: LinearizationFit) -> bool:
    return bool(np.isfinite([(one.freq, one.q, one.gain) for one in fit.filters]).all())


def speaker_fit(
    inputs: RoundInputs, manifest: Mapping[str, Any], set_id: str, take_id: str | None = None,
    *, budget: Mapping[str, Any] | None = None,
    clouds_by_set: Mapping[str, CloudFitTerms] | None = None, sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = resolve_set(inputs, set_id, manifest=manifest)
    take_id = selected.take_id(take_id)
    take = next(take for take in selected.takes if take["take_id"] == take_id)
    path = take_artifact_path(inputs.session_dir, take["artifacts"]["record_id"])
    relative_artifact_path(inputs.session_dir, path)
    record = json.loads(path.read_text())
    program = ExcitationProgram.from_dict(record["program"])
    if program.phase != "measure":
        raise RoundViewsError("speaker-fit requires a Speaker MEASURE take")
    if program.program_id != selected.capture_basis["program_id"] or record["take_id"] != take_id:
        raise RoundViewsError("selected take does not match its manifest")
    directory, _ = round_artifact_dir(inputs.session_dir)
    assert directory is not None
    sources = prescription_sources(inputs) if sources is None else sources
    try:
        candidate = _round_candidate(directory, sources)
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
    draft = sources.get("draft") or {}
    classes = _resolve_driver_class_by_role(draft)
    budgets = fit_budgets_by_role(design_draft_view(draft).get("driver_safety_profile") or {})
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
    if clouds_by_set is None:
        clouds_by_set = design_clouds(inputs, manifest)
    clouds = {group["capture_basis"].get("role") or "": clouds_by_set[group["set_id"]]
              for group in manifest["sets"] if group["set_id"] in clouds_by_set
              and any(t["selected"] and t["take_id"] == take_id for t in group["takes"])}
    regions = [CrossoverRegion.from_mapping(region)
               for region in candidate["source_preset"].get("crossover_regions") or ()]
    sections = sections_by_role(regions)
    vocabularies = _fit_vocabularies(candidate, {role: {**budgets.get(role, {}), **overrides} for role in bands})
    curves = {curve["role"]: curve for curve in curves_for_take(record, manifest)}
    drivers = []
    for role, band in bands.items():
        response = response_from_banked_curve(curves[role])
        if response is None:
            raise RoundViewsError(f"fit inputs are not banked for {role}")
        drivers.append(DriverEvidence(role, response[0], band, classes.get(role, "unknown")))
    branches = fit_branches(
        drivers, sections=sections, mic_tiers={driver.role: tier for driver in drivers},
        vocabulary=vocabularies,
        cloud={role: clouds[role] for role in bands if role in clouds},
    )
    trim_decision: dict[str, Any] | None = None
    if len(drivers) == 2:
        try:
            trim_decision = {"committed_db": resolve_trims_after_fit(drivers, branches.fits, regions)}
        except NonFiniteTrimError as exc:
            trim_decision = {"status": "unavailable", "reason": exc.refusal_reason}
        except ValueError:
            trim_decision = {"status": "unavailable", "reason": "handover_band_unmeasured"}
    finite = {role: _filters_finite(fit) for role, fit in branches.fits.items()}
    handover_shifts = {}
    for role, fit in branches.fits.items():
        grid = np.unique(np.concatenate([
            np.geomspace(section.fc_hz / 2, section.fc_hz * 2, 49)
            for section in sections.get(role, ())
        ])) if sections.get(role) else np.array([])
        correction_db = 20 * np.log10(np.maximum(np.abs(complex_correction_response(fit.filters, grid)), 1e-12))
        handover_shifts[role] = power_mean_db(correction_db) if grid.size and finite[role] else None
    linearization = {driver.role: {
        "boost_evidence": {"design_poses": clouds[driver.role].n_positions,
                  "band_spread": [asdict(band) for band in clouds[driver.role].band_spread]}
        if driver.role in clouds else {"design_poses": 0, "band_spread": []},
        "per_filter_boost_cap_db": vocabularies[driver.role].per_filter_boost_cap_db,
        "composed_boost_cap_db": vocabularies[driver.role].composed_boost_cap_db,
        "excited_band_hz": list(driver.excited_band_hz),
        "envelope": _envelope_answer(branches.envelopes[driver.role]),
        "handover_level_shift_db": handover_shifts[driver.role],
        "fit": branches.fits[driver.role].to_dict() if finite[driver.role] else {
            "role": driver.role, "reason_summary": {"unavailable": REASON_FIT_NOT_FINITE}},
    } for driver in drivers}
    selected_fit = linearization.get(selected.capture_basis.get("role") or drivers[0].role, linearization[drivers[0].role])
    return dict(
        set_id=selected.set_id, take_id=take_id,
        boost_evidence=selected_fit["boost_evidence"], linearization=linearization,
        alignment=alignment_evidence({**take, "analysis": analysis}, sources),
        trim_decision=trim_decision,
    )
