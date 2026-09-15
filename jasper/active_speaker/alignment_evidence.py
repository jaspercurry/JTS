# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Saved timing evidence shared by the round packet and speaker fit view."""

from collections import Counter
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.program_analysis.model import ALIGNMENT_COMMITTED_SUMMED_FIT
from jasper.audio_measurement.program_analysis.response import half_period_us
from jasper.json_fields import finite_float

from .applied_identity import applied_identity
from .run_manifest import capture_alignment_levels
from .baseline_profile import profile_corrections_provenance, profile_driver_corrections
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
    provenance = profile_corrections_provenance(profile)
    return {
        "roles": roles, "objective": analysis.get("alignment_objective"),
        "confidence": analysis.get("alignment_confidence"), "status": analysis.get("alignment_status"),
        **{key: analysis.get(key) for key in (
            "summed_fit_rms_db", "summed_fit_margin", "delay_interval_us",
            "summed_fit_verdict", "parallax_us", "trim_db",
            "refinement_delta_us", "epsilon_ppm", "flatness_improvement_db", "anchor_delay_us", "snap_delta_us",
            "polarity_agrees_with_sum", "left_anchor_lobe", "snr_waived_roles", "gcc_delay_us",
        )},
        "committed": {"delay_us": analysis.get("delay_us"), "polarity": analysis.get("polarity")},
        "seed": {"delay_us": analysis.get("alignment_seed_delay_us"), "polarity": analysis.get("seed_polarity")},
        "driver_spacing_source": driver_spacing_source(sources.get("draft") or {}),
        "snr": {role: {key: evidence.get(f"snr.{role}.alignment.{key}") for key in ("verdict", "shortfall_db")}
                for role in roles},
        "levels": take.get("alignment") or capture_alignment_levels(evidence, {}),
        "applied": {**(applied_identity(profile) or {}),
                    "corrections": {role: {key: corrections.get(role, {}).get(key) for key in ("delay_ms", "inverted")}
                                    for role in roles},
                    "corrections_provenance": {role: {key: provenance.get(role, {}).get(key) for key in ("delay_ms", "inverted")}
                                               for role in roles}},
    }


def round_alignment(
    manifest: Mapping[str, Any], sources: Mapping[str, Any], *, fc_hz: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pairs = latest_measure_takes(
        ((group, take) for group in manifest.get("sets", ()) for take in SetTakes.from_row(group).takes),
        key=lambda group, take: (*capture_identity(group["capture_basis"], set_id=group["set_id"]),
                                tuple(take["pose"].get(key) for key in ("kind", "deg", "elevation_deg", "distance_m")),
                                tuple(sorted((take.get("analysis") or {})["trim_db"])))
        if (take.get("analysis") or {}).get("trim_db") else None,
    )
    rows = [{"candidate_id": group["capture_basis"].get("candidate_id"),
             "graph_fingerprint": group["capture_basis"].get("graph_fingerprint"),
             "side": group["capture_basis"].get("side"), "take_id": take["take_id"],
             "pose": take["pose"], "record_id": take["artifacts"]["record_id"], "base": group.get("base", False),
             "timing": take.get("timing"), "attempt": take.get("attempt", 0),
             **alignment_evidence(take, sources)}
            for group, take in pairs.values()]
    fc = finite_float(fc_hz)
    lobe = half_period_us(fc) if fc is not None and fc > 0 else None
    committed = [row for row in rows if finite_float(row["committed"]["delay_us"]) is not None
                 and row["committed"]["polarity"] in ("normal", "inverted")]
    pose_counts = Counter(str(row["pose"].get("deg")) for row in committed)
    folded = {}
    if lobe is not None:
        for row in committed:
            pose = str(row["pose"].get("deg"))
            key = f"{pose}@{row['take_id']}" if pose_counts[pose] > 1 else pose
            folded[key] = round(row["committed"]["delay_us"]
                                - (lobe if row["committed"]["polarity"] == "inverted" else 0), 3)
    decided = max((row for row in committed if row["objective"] == ALIGNMENT_COMMITTED_SUMMED_FIT),
                  key=take_order, default=None)
    delays = [row["committed"]["delay_us"] for row in committed]
    return rows, {
        "folded_delay_us": folded,
        "spread_us": round(max(folded.values()) - min(folded.values()), 3) if folded else None,
        "lobe_us": lobe,
        "lobes_agree": (len({row["committed"]["polarity"] for row in committed}) == 1
                        and max(delays) - min(delays) <= lobe) if delays and lobe is not None else None,
        "decided_by": {"pose": decided["pose"].get("deg"), "objective": decided["objective"],
                       "take_id": decided["take_id"]} if decided else None,
        "snr_waived": [row["take_id"] for row in committed
                       if row["objective"] == ALIGNMENT_COMMITTED_SUMMED_FIT and row["snr_waived_roles"]],
    }


def commissioning_alignment(rows: Sequence[Mapping[str, Any]], candidate_id: str) -> Mapping[str, Any] | None:
    return max((row for row in rows if (row["base"] or row["candidate_id"] in (None, candidate_id))
                and row["pose"].get("deg") == 0 and row["pose"].get("elevation_deg") == 0),
               key=take_order, default=None)
