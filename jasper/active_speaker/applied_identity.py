# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Names of the candidate, its layers and the compiled config in an applied record."""

from typing import Any, Mapping

from jasper.audio_measurement.evidence_identity import json_fingerprint

from .measurement_programs import CANDIDATE_LAYERS

#: What every program plays through: the applied snapshot less each program's own layer (ADR-0420).
BASE_LAYER = "base"


def layer_fingerprints(applied_state: Mapping[str, Any] | None) -> dict[str, str]:
    """Each applied layer that plays, by its fingerprint: :data:`BASE_LAYER` and each program's own
    candidate layer. The snapshot holds no preference EQ, so a preference save moves none (ADR-0420)."""
    snapshot = dict((applied_state or {}).get("recomposition_snapshot") or {})
    snapshot.pop("measured_candidate_fingerprint", None)
    layers = {name: snapshot.pop(name, None) for name in CANDIDATE_LAYERS}
    return {name: json_fingerprint({name: value})
            for name, value in ((BASE_LAYER, snapshot), *layers.items()) if value}


def applied_identity(applied_state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if applied_state is None:
        return None
    config = applied_state.get("config") or {}
    return {
        "candidate": (applied_state.get("source") or {}).get("measured_candidate_fingerprint"),
        "record": (config.get("sha256") or "")[:12] or None,
        "config_path": config.get("path"),
        "applied_at": applied_state.get("applied_at"),
        "layer_fingerprints": layer_fingerprints(applied_state),
    }
