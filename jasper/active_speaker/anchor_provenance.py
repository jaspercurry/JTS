# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Advisory graph and rig identity for a banked level (ADR-0101)."""
from __future__ import annotations

from typing import Any, Mapping

from .applied_identity import applied_identity
from .state_paths import config_text_sha256


def graph_provenance(candidate_fingerprint: str, compiled_graph: str) -> dict[str, Any]:
    return {"candidate_fingerprint": candidate_fingerprint,
            "compiled_graph_sha256": config_text_sha256(compiled_graph)}


def read_pose(*, arm_offset_deg: float | None = None) -> dict[str, Any]:
    from jasper.audio_measurement.measurement_geometry import load_declared_geometry  # lazy: numpy import budget

    try:
        geometry = load_declared_geometry()
    except (OSError, ValueError, TypeError):
        geometry = None
    return {"geometry": geometry.to_dict() if geometry is not None else None, "arm_offset_deg": arm_offset_deg}


def read_graph(*, compile_graph: bool = False) -> dict[str, Any]:
    from .baseline_profile import load_applied_baseline_profile_state  # lazy: keeps anchor imports numpy-free

    applied = load_applied_baseline_profile_state() or {}
    identity = applied_identity(applied) or {}
    graph = {"candidate_fingerprint": identity.get("candidate"), "compiled_graph_sha256": None}
    if compile_graph:
        from .candidate_parts import candidate_from_applied_profile  # lazy: graph compilation imports numpy
        from .measurement_emit import compile_tuning_graph, load_tuning_declaration  # lazy: graph compilation imports numpy

        try:
            profile = load_tuning_declaration()
            candidate = candidate_from_applied_profile(profile.topology, applied)
            return graph_provenance(candidate.fingerprint, compile_tuning_graph(profile, candidate))
        except (OSError, RuntimeError, ValueError, LookupError):
            pass
    return graph


def provenance_mismatches(record: Mapping[str, Any], *, graph: Mapping[str, Any] | None,
                          pose: Mapping[str, Any] | None) -> dict[str, bool | None]:
    def mismatch(banked: Any, current: Mapping[str, Any] | None) -> bool | None:
        if not isinstance(banked, Mapping) or current is None:
            return None
        known = [banked[key] != current[key] for key in banked
                 if banked[key] is not None and current.get(key) is not None]
        if any(known):
            return True
        return False if known and len(known) == len(banked) else None

    return {"anchor_graph_mismatch": mismatch(record.get("graph"), graph),
            "anchor_pose_mismatch": mismatch(record.get("pose"), pose)}
