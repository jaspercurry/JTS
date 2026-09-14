# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Saved timing evidence shared by the round packet and speaker fit view."""

from typing import Any, Mapping, Sequence

from .applied_identity import applied_identity
from .baseline_profile import profile_corrections_provenance, profile_driver_corrections
from .crossover_v2.conductor_context import driver_spacing_source
from .crossover_v2.round_inputs import SetTakes, capture_identity, take_order


def alignment_evidence(
    take: Mapping[str, Any], sources: Mapping[str, Any],
) -> dict[str, Any]:
    analysis = take.get("analysis") or {}
    evidence = (take.get("quality") or {}).get("evidence") or {}
    roles = sorted(analysis.get("trim_db") or {})
    profile = sources.get("applied_profile")
    corrections = profile_driver_corrections(profile)
    provenance = profile_corrections_provenance(profile)
    return {
        "roles": roles, "objective": analysis.get("alignment_objective"),
        "confidence": analysis.get("alignment_confidence"), "status": analysis.get("alignment_status"),
        **{key: analysis.get(key) for key in (
            "summed_fit_rms_db", "summed_fit_margin", "delay_interval_us",
            "summed_fit_verdict", "parallax_us", "trim_db",
            "drift_us", "flatness_improvement_db", "anchor_delay_us", "snap_delta_us",
        )},
        "committed": {"delay_us": analysis.get("delay_us"), "polarity": analysis.get("polarity"),
                      "ripple_db": analysis.get("predicted_ripple_db")},
        "seed": {"delay_us": analysis.get("alignment_seed_delay_us"), "polarity": analysis.get("seed_polarity"),
                 "ripple_db": analysis.get("alignment_seed_ripple_db")},
        "driver_spacing_source": driver_spacing_source(sources.get("draft") or {}),
        "snr": {role: {key: evidence.get(f"snr.{role}.alignment.{key}") for key in ("verdict", "shortfall_db")}
                for role in roles},
        "applied": {**(applied_identity(profile) or {}),
                    "corrections": {role: {key: corrections.get(role, {}).get(key) for key in ("delay_ms", "inverted")}
                                    for role in roles},
                    "corrections_provenance": {role: {key: provenance.get(role, {}).get(key) for key in ("delay_ms", "inverted")}
                                               for role in roles}},
    }


def round_alignment(
    manifest: Mapping[str, Any], sources: Mapping[str, Any],
) -> list[dict[str, Any]]:
    pairs: dict[tuple[Any, ...], tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for group in manifest.get("sets", ()):
        basis = group["capture_basis"]
        for take in SetTakes.from_row(group).on_axis:
            analysis = take.get("analysis") or {}
            if take.get("phase") != "measure" or not analysis.get("trim_db"):
                continue
            key = (*capture_identity(basis, set_id=group["set_id"]),
                   tuple(sorted(analysis.get("trim_db") or {})))
            previous = pairs.get(key)
            if previous is None or take_order(take) >= take_order(previous[1]):
                pairs[key] = group, take
    return [{"candidate_id": group["capture_basis"].get("candidate_id"),
             "graph_fingerprint": group["capture_basis"].get("graph_fingerprint"),
             "side": group["capture_basis"].get("side"), "take_id": take["take_id"],
             "pose": take["pose"], "record_id": take["artifacts"]["record_id"], "base": group.get("base", False),
             "timing": take.get("timing"), "attempt": take.get("attempt", 0),
             **alignment_evidence(take, sources)}
            for group, take in pairs.values()]


def commissioning_alignment(rows: Sequence[Mapping[str, Any]], candidate_id: str) -> Mapping[str, Any] | None:
    return max((row for row in rows if row["base"] or row["candidate_id"] in (None, candidate_id)),
               key=take_order, default=None)
