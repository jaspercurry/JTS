# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Saved timing evidence shared by the round packet and speaker fit view."""

from typing import Any, Mapping, Sequence

from .applied_identity import applied_identity
from .run_manifest import capture_alignment_levels
from .baseline_profile import profile_driver_corrections
from .crossover_v2.conductor_context import driver_spacing_source
from .crossover_v2.round_inputs import SetTakes, capture_identity, latest_measure_takes, take_order


def alignment_evidence(
    take: Mapping[str, Any], sources: Mapping[str, Any],
) -> dict[str, Any]:
    analysis = take.get("analysis") or {}
    evidence = (take.get("quality") or {}).get("evidence") or {}
    roles = sorted(analysis.get("trim_db") or {})
    profile = sources.get("applied_profile")
    corrections = profile_driver_corrections(profile)
    return {
        "roles": roles, "objective": analysis.get("alignment_objective"),
        "confidence": analysis.get("alignment_confidence"), "status": analysis.get("alignment_status"),
        **{key: analysis.get(key) for key in (
            "residual_rms_db", "margin_db", "repeat_spread_db", "repeat_spread_us", "repeat_count",
            "timing_verdict", "timing_saved", "timing_verification", "parallax_us", "trim_db",
            "refinement_delta_us", "epsilon_ppm", "flatness_improvement_db", "anchor_delay_us", "snap_delta_us",
            "polarity_agrees_with_sum", "left_anchor_lobe", "gcc_delay_us",
        )},
        "committed": {"delay_us": analysis.get("delay_us"), "polarity": analysis.get("polarity")},
        "seed": {"delay_us": analysis.get("alignment_seed_delay_us"), "polarity": analysis.get("seed_polarity")},
        "driver_spacing_source": driver_spacing_source(sources.get("draft") or {}),
        "snr": {role: {key: evidence.get(f"snr.{role}.alignment.{key}") for key in ("verdict", "shortfall_db")}
                for role in roles},
        "levels": take.get("alignment") or capture_alignment_levels(evidence, {}),
        "applied": {**(applied_identity(profile) or {}),
                    "corrections": {role: {key: corrections.get(role, {}).get(key) for key in ("delay_ms", "inverted")}
                                    for role in roles}},
    }


def round_alignment(
    manifest: Mapping[str, Any], sources: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pairs = latest_measure_takes(
        ((group, take) for group in manifest.get("sets", ()) for take in SetTakes.from_row(group).takes),
        key=lambda group, take: (*capture_identity(group["capture_basis"], set_id=group["set_id"]),
                                tuple(take["pose"].get(key) for key in ("kind", "deg", "elevation_deg", "distance_m")),
                                tuple(sorted((take.get("analysis") or {})["trim_db"])))
        if (take.get("analysis") or {}).get("trim_db") else None,
    )
    rows = [{"candidate_id": group["capture_basis"].get("candidate_id"),
             "graph_fingerprint": (take.get("analysis") or {}).get("timing_graph_fingerprint") or group["capture_basis"].get("graph_fingerprint"),
             "side": group["capture_basis"].get("side"), "take_id": take["take_id"], "round_id": manifest.get("round_id", manifest.get("run_id")),
             "pose": take["pose"], "record_id": take["artifacts"]["record_id"], "base": group.get("base", False),
             "timing": take.get("timing"), "attempt": take.get("attempt", 0),
             **alignment_evidence(take, sources)}
            for group, take in pairs.values()]
    verified = commissioning_alignment([row for row in rows if row["timing_verification"] is not None])
    saved = (sources.get("applied_profile") or {}).get("timing")
    return rows, {
        "saved": saved,
        "verification": verified["timing_verification"] if verified and verified["timing_saved"] == saved else None,
    }


def commissioning_alignment(rows: Sequence[Mapping[str, Any]], candidate_id: str | None = None) -> Mapping[str, Any] | None:
    return max((row for row in rows if (candidate_id is None or row["base"] or row["candidate_id"] in (None, candidate_id))
                and row["pose"].get("deg") == 0 and row["pose"].get("elevation_deg") == 0),
               key=take_order, default=None)
