# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Read retained bass evidence at each captured resolved window gain."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from jasper.bass_extension.dynamic import DynamicBassDescriptor, loudness_boost_db, validate_dynamic_bass_descriptor
from jasper.json_fields import finite_float

from .bass_comparison import CHANGE_FIELDS, bass_capture_context
from .bass_fit import REFERENCE_BAND_HZ, fit_bass_shape
from .bass_level_evidence import bass_ladder_evidence, bass_level_evidence
from .crossover_v2.measurement_context import compare_capture_basis
from .crossover_v2.refusal_copy import CrossoverV2Refused

LEVEL_FIELD = "level_db"
REFERENCE_FIELD = "loudness_volume_db"
PROGRAM_FIELD = "program_id"
LEVEL_FIELDS = (LEVEL_FIELD, REFERENCE_FIELD, PROGRAM_FIELD)


def level_key(basis: Mapping[str, Any], **identity: Any) -> tuple[float, float, str]:
    volume, reference = (finite_float(basis.get(key)) for key in (LEVEL_FIELD, REFERENCE_FIELD))
    program = basis.get(PROGRAM_FIELD)
    missing = [key for key, value in zip((LEVEL_FIELD, REFERENCE_FIELD), (volume, reference))
               if value is None or not -100 <= value <= 0]
    if not isinstance(program, str) or not program.strip():
        missing.append(PROGRAM_FIELD)
    if missing:
        raise CrossoverV2Refused({**identity, "fields": missing}, code="bass_table_window_gain_missing")
    assert volume is not None and reference is not None and isinstance(program, str)
    return volume, reference, program


def fit_bass_table(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    candidate_id: str, descriptor: Mapping[str, Any] | None,
    reference_band_hz: tuple[float, float] = REFERENCE_BAND_HZ,
) -> dict[str, Any]:
    if not pairs:
        raise CrossoverV2Refused(code="bass_fit_inputs_missing")
    try:
        descriptor = validate_dynamic_bass_descriptor(descriptor) if descriptor is not None else None
    except ValueError as exc:
        raise CrossoverV2Refused({"candidate_id": candidate_id}, code="bass_fit_candidate_unreadable") from exc
    settings = DynamicBassDescriptor(**descriptor) if descriptor is not None else None
    first: dict[str, Any] = {}
    interventions = (*CHANGE_FIELDS["volume"], "pose_key")
    groups: dict[tuple[float, float, str], list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
    contexts = []
    for before, after in pairs:
        for take in (before, after):
            if take.get("diagnostics", {}).get("integrity_failed"):
                raise CrossoverV2Refused({"record_path": take["record_path"]}, code="bass_table_capture_integrity_failed")
        context = bass_capture_context(before)
        key = level_key(context, record_path=before["record_path"])
        level_key(bass_capture_context(after), record_path=after["record_path"])
        first = first or context
        comparison = compare_capture_basis(context, first, interventions=interventions,
                                           required=tuple(key for key in first if key not in interventions))
        if comparison["incompatible_fields"]:
            raise CrossoverV2Refused(comparison, code="bass_table_capture_context_changed")
        groups[key].append((before, after))
        contexts.append(comparison)
    levels, ladder = [], []
    for key, group in sorted(groups.items()):
        prescribed = loudness_boost_db(key[1], settings) if settings else None
        aligned = fit_bass_shape(group, candidate_id=candidate_id, reference_band_hz=reference_band_hz)
        ladder.append(aligned["groups"])
        levels.append({"level_key": dict(zip(LEVEL_FIELDS, key)),
                       **bass_level_evidence(aligned, descriptor=descriptor, prescribed_boost_db=prescribed)})
    bass_ladder_evidence(levels, ladder)
    return {"schema": "jts_bass_table/1", "candidate_id": candidate_id,
            "reference_band_hz": list(reference_band_hz), "smoothing_fraction": 3,
            "tested_volume_range_db": [min(key[0] for key in groups), max(key[0] for key in groups)],
            "stimulus_dbfs": first["stimulus_dbfs"], "capture_context": contexts, "levels": levels,
            "limits": ["This table reads the recorded stimulus and bass-reference settings; it is not a runtime schedule.",
                       "No extrapolation beyond measured boost or resolved window gains. Harmonics describe measured headroom, not a hardware limit.",
                       "Compression is prescribed minus realized boost, including both compressor and driver action.",
                       "The single-repeat harmonic evidence floor is 1 dB, not a hearing threshold; repeats combine base and candidate band standard deviations in quadrature at each pose. Decreases are not rises.",
                       "Curves use medians within each pose, then across poses. SPL uses the same pose weighting on each take's calibrated loudest half-second statistic.",
                       "Repeat spread is the RMS of fundamental standard deviations within repeated poses and both arms. Position spread is not yet estimated.",
                       "Ladder growth uses common qualified bins within each stack and pose; harmonic growth adds the fundamental to the relative harmonic reading. Knee allowances combine relative-harmonic repeat spreads in quadrature, or use the largest per-band SNR margin when repeats are missing, divided by the fader step. Headroom subtracts each row's measured SPL from the last clean rung; an unreached knee is extrapolated.",
                       "Missing frequency bins remain unproven."]}
