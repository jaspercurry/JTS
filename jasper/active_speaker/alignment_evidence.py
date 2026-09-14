# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Saved timing evidence shared by the round packet and speaker fit view."""

from typing import Any, Mapping

from .applied_identity import applied_identity
from .baseline_profile import profile_driver_corrections
from .crossover_v2.round_inputs import SetTakes
from .design_draft import declared_driver_spacing_m


def alignment_evidence(
    analysis: Mapping[str, Any], quality: Mapping[str, Any], draft: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    evidence = quality.get("evidence") or {}
    snr = {}
    roles = sorted(analysis.get("trim_db") or {})
    for role in roles:
        block = (((evidence.get("snr") or {}).get(role) or {}).get("alignment") or {})
        worst = block.get("worst_relevant") or {}
        band: Mapping[str, Any] = next((b for b in block.get("bands", ()) if b.get("band_id") == worst.get("band_id")), {})
        snr[role] = {
            "verdict": worst.get("verdict", evidence.get(f"snr.{role}.alignment.verdict")),
            "shortfall_db": worst.get("shortfall_db", band.get("shortfall_db", evidence.get(f"snr.{role}.alignment.shortfall_db"))),
        }
    corrections = profile_driver_corrections(profile)
    provenance = (profile or {}).get("corrections_provenance") or {}
    return {
        "roles": roles, "objective": analysis.get("alignment_objective"),
        "confidence": analysis.get("alignment_confidence"),
        **{key: analysis.get(key) for key in (
            "delay_us", "polarity", "summed_fit_rms_db", "summed_fit_margin", "delay_interval_us",
            "summed_fit_verdict", "alignment_seed_delay_us", "seed_polarity", "parallax_us",
            "drift_us", "flatness_improvement_db", "anchor_delay_us", "snap_delta_us",
        )},
        "committed": {"delay_us": analysis.get("delay_us"), "polarity": analysis.get("polarity"),
                      "ripple_db": analysis.get("predicted_ripple_db")},
        "seed": {"delay_us": analysis.get("alignment_seed_delay_us"), "polarity": analysis.get("seed_polarity"),
                 "ripple_db": analysis.get("alignment_seed_ripple_db")},
        "parallax_from_declared_spacing": declared_driver_spacing_m(draft) is not None,
        "snr": snr,
        "applied": {**(applied_identity(profile) or {}),
                    "corrections": {role: {key: corrections.get(role, {}).get(key) for key in ("delay_ms", "inverted")}
                                    for role in roles},
                    "corrections_provenance": {role: {key: provenance.get(role, {}).get(key) for key in ("delay_ms", "inverted")}
                                               for role in roles}},
    }


def round_alignment(
    manifest: Mapping[str, Any], draft: Mapping[str, Any], profile: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    pairs: dict[tuple[Any, ...], tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for group in manifest.get("sets", ()):
        basis = group["capture_basis"]
        for take in SetTakes.from_row(group).on_axis:
            analysis = take.get("analysis") or {}
            if take.get("phase") != "measure" or analysis.get("alignment_objective") is None:
                continue
            key = (basis.get("candidate_id"), basis.get("graph_fingerprint"), basis.get("side"),
                   tuple(sorted(analysis.get("trim_db") or {})))
            previous = pairs.get(key)
            if previous is None or (take.get("timing") or {}).get("ended_s", 0) >= (previous[1].get("timing") or {}).get("ended_s", 0):
                pairs[key] = group, take
    return [{"candidate_id": group["capture_basis"].get("candidate_id"),
             "graph_fingerprint": group["capture_basis"].get("graph_fingerprint"),
             "side": group["capture_basis"].get("side"), "take_id": take["take_id"],
             "pose": take["pose"], "record_id": take["artifacts"]["record_id"],
             **alignment_evidence(take["analysis"], take.get("quality") or {}, draft, profile)}
            for group, take in pairs.values()]
