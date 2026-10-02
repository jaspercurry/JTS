# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Speaker fit proposals, resolved trims, and banked alignment evidence."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.design_draft import design_draft_view
from jasper.active_speaker.design_inputs import declared_by_target
from jasper.active_speaker.crossover_section import sections_by_role
from jasper.active_speaker.camilla_yaml import MAX_PROGRAM_HEADROOM_DB
from jasper.active_speaker.alignment_evidence import alignment_evidence
from jasper.active_speaker.candidate_parts import COMPOSITION_INVALID, candidate_from_applied_profile, program_charge_db
from jasper.active_speaker.crossover_v2.intervention import CloudFitTerms, DriverEvidence, NonFiniteTrimError, fit_branches, resolve_trims_after_fit
from jasper.active_speaker.crossover_v2.pose_curve import WINDOW_GATED
from jasper.active_speaker.crossover_v2.position_cycle import take_curve
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, RoundViewsError, capture_identity, latest_measure_takes, prescription_sources, resolve_set
from jasper.active_speaker.crossover_v2.round_views import response_from_banked_curve
from jasper.active_speaker.crossover_v2.spatial import _primary_sweep_bands
from jasper.active_speaker.linearization_envelope import DEFAULT_ENVELOPE_GRID_HZ, EnvelopeCurve, ladder_smooth
from jasper.active_speaker.linearization_budget import fit_budgets_by_role, normalise_fit_budget
from jasper.active_speaker.linearization_fit import (
    FitVocabulary, LinearizationFit, complex_correction_response, unavailable_fit,
)
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate, MeasuredCrossoverCandidateError
from jasper.active_speaker.measurement_programs import POSE_KIND_BEARING, REGIME_SUMMED
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.run_manifest import view_sets
from jasper.audio_measurement.evidence_reasons import REASON_FIT_NOT_FINITE, EvidenceUnavailable, unavailable
from jasper.audio_measurement.mic_identity import mic_tier_for_model
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.series_stats import power_mean_db
from jasper.audio_measurement.spatial_combine import _band_spread, octave_bands_hz


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


def _round_candidate(sources: Mapping[str, Any]) -> MeasuredCrossoverCandidate:
    if candidate := sources.get("candidate"):
        return MeasuredCrossoverCandidate.from_mapping(candidate)
    if applied := sources.get("applied_profile"):
        return candidate_from_applied_profile(None, applied)
    raise RoundViewsError("speaker-fit requires the round's candidate or a banked base")


def design_clouds(
    manifest: Mapping[str, Any], *, refused: dict[str, EvidenceUnavailable] | None = None,
) -> dict[str, CloudFitTerms]:
    """Each set's design cloud. A design pose without a fit input refuses by
    code; with ``refused``, its sets get no cloud and ``refused`` names the
    refusal by set id instead."""
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for group in view_sets(manifest):
        basis = group["capture_basis"]
        key = (*capture_identity(basis, set_id=group["set_id"]), basis.get("role"))
        groups.setdefault(key, []).append(group)
    clouds: dict[str, CloudFitTerms] = {}
    for (_, _, _, _, role), members in groups.items():
        bearings = latest_measure_takes(
            ((group, take) for group in members for take in group["takes"]),
            key=lambda group, take: (take["pose"]["azimuth_deg"], take["pose"].get("elevation_deg")) if (
                role and role != REGIME_SUMMED
                and take["pose"].get("kind") == POSE_KIND_BEARING and take["pose"].get("azimuth_deg") is not None
            ) else None,
        )
        cloud = CloudFitTerms(n_positions=len(bearings))
        # Standard error needs two positions; see linearization_envelope.position_spread_db.
        if len(bearings) >= 2:
            try:
                responses = []
                lo, hi = 0.0, float("inf")
                for _group, take in bearings.values():
                    parsed = response_from_banked_curve(take_curve(take, role, WINDOW_GATED, required=True))
                    if parsed[0].role != role:
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
            except EvidenceUnavailable as refusal:
                if refused is None:
                    raise
                refused.update((group["set_id"], refusal) for group in members)
                continue
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
    base: MeasuredCrossoverCandidate, budgets: Mapping[str, Mapping[str, Any]],
) -> dict[str, FitVocabulary]:
    try:
        # The charge without each role's own chain (#5909).
        spent = {role: program_charge_db(replace(base, linearization={
            name: fit for name, fit in base.linearization.items() if name != role})) for role in budgets}
    except (MeasuredCrossoverCandidateError, ActiveSpeakerConfigError) as exc:
        raise SpeakerFitUnreadable(str(exc), code=getattr(exc, "code", COMPOSITION_INVALID)) from exc
    vocabularies = {}
    for role, budget in budgets.items():
        remaining = max(0.0, MAX_PROGRAM_HEADROOM_DB - spent[role])
        vocabularies[role] = FitVocabulary(
            allow_boost=True, per_filter_boost_cap_db=remaining, composed_boost_cap_db=remaining,
        ).with_budget(budget)
    return vocabularies


def _filters_finite(fit: LinearizationFit) -> bool:
    return bool(np.isfinite([(one.freq, one.q, one.gain) for one in fit.filters]).all())


def speaker_fit(
    inputs: RoundInputs, manifest: Mapping[str, Any], set_id: str | None, take_id: str | None = None,
    *, budget: Mapping[str, Any] | None = None,
    clouds_by_set: Mapping[str, CloudFitTerms] | None = None, sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = resolve_set(inputs, set_id, take=take_id, manifest=manifest)
    take_id = selected.take_id(take_id)
    record = next(take for take in selected.takes if take["take_id"] == take_id)
    program = ExcitationProgram.from_dict(record["program"])
    if program.phase != "measure":
        raise RoundViewsError("speaker-fit requires a Speaker MEASURE take")
    if program.stimulus_id != selected.capture_basis["stimulus_id"]:
        raise RoundViewsError("selected take does not match its manifest")
    sources = prescription_sources(inputs) if sources is None else sources
    try:
        base = _round_candidate(sources)
    except (OSError, ValueError, TypeError, LookupError) as exc:
        raise SpeakerFitUnreadable(str(exc), code=getattr(exc, "code", None)) from exc
    if not inputs.banked or inputs.design_draft_path is None:
        raise RoundViewsError("speaker-fit requires the banked driver declaration")
    draft = sources.get("draft") or {}
    classes = declared_by_target(draft, "driver_class")
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
    vocabularies = _fit_vocabularies(base, {role: {**budgets.get(role, {}), **overrides} for role in bands})
    if clouds_by_set is None:
        clouds_by_set = design_clouds(manifest)
    clouds = {group["capture_basis"].get("role") or "": clouds_by_set[group["set_id"]]
              for group in manifest["sets"] if group["set_id"] in clouds_by_set
              and any(t["selected"] and t["take_id"] == take_id for t in group["takes"])}
    regions = list(base.source_preset.crossover_regions)
    sections = sections_by_role(regions)
    drivers = [DriverEvidence(role, response_from_banked_curve(take_curve(record, role, WINDOW_GATED, required=True))[0], band,
                              classes.get(role, "unknown")) for role, band in bands.items()]
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
            trim_decision = unavailable(exc.refusal_reason)
        except ValueError:
            trim_decision = unavailable("handover_band_unmeasured")
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
        "fit": (branches.fits[driver.role].to_dict() if finite[driver.role]
                else unavailable_fit(driver.role, REASON_FIT_NOT_FINITE)),
    } for driver in drivers}
    selected_fit = linearization.get(selected.role, linearization[drivers[0].role])
    return dict(
        set_id=selected.set_id, take_id=take_id,
        boost_evidence=selected_fit["boost_evidence"], linearization=linearization,
        alignment=alignment_evidence(record, sources),
        trim_decision=trim_decision,
    )
