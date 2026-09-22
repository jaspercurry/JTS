# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Rebuild an applied baseline without changing the boot pointer."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jasper import atomic_io
from jasper.active_speaker import baseline_profile, candidate_parts, measurement_emit, runtime_contract
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.output_topology import OutputTopology
from jasper.sound import settings


@dataclass(frozen=True)
class BaselineReemitResult:
    path: Path
    classification: str
    byte_count: int


def reemit_applied_baseline(
    topology: OutputTopology, applied: Mapping[str, Any], *,
    playback_device: str, out: str | Path | None = None,
) -> BaselineReemitResult:
    """Compile and prove against the supplied topology before atomic publication."""
    declaration = measurement_emit.load_tuning_declaration(topology, playback_device=playback_device)
    preference_filters, trim_db = settings.saved_sound_layers()
    text = measurement_emit.compile_tuning_graph(
        declaration, candidate=candidate_parts.candidate_from_applied_profile(topology, applied),
        preference_filters=preference_filters, output_trim_db=trim_db,
    )
    graph = runtime_contract.classify_bass_extension_graph(
        topology, evidence_source="desired", graph_text=text, applied_baseline_state=applied,
    )
    if not graph.allowed or graph.classification != runtime_contract.GRAPH_APPROVED_ACTIVE_RUNTIME:
        raise ActiveSpeakerConfigError(
            f"re-emitted baseline failed runtime proof: {graph.classification}; {graph.issues}",
            code="baseline_reemit_reproof_failed",
        )
    if out is not None:
        target = Path(out)
        if not target.parent.exists():
            raise FileNotFoundError(target.parent)
        atomic_io.atomic_write_text(target, text, mode=0o640)
    else:
        config = applied.get("config")
        raw_target = config.get("path") if isinstance(config, Mapping) else None
        if not isinstance(raw_target, str) or not raw_target.strip():
            raise ActiveSpeakerConfigError("applied baseline has no config path", code="baseline_config_missing")
        target = Path(raw_target)
        # Preserve operator-set permissions on an existing artifact.
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            mode = 0o640
        atomic_io.atomic_write_text(target, text, mode=mode, durable=True)
        baseline_profile.promote_applied_baseline_candidate(applied)
    return BaselineReemitResult(target, graph.classification, len(text))
