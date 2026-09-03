# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime graph safety helpers for room-correction entry points.

Room correction owns measurement state, not speaker-role policy. Every generated
measurement or apply graph is re-checked against the saved output topology
before CamillaDSP is allowed to load it.
"""

from __future__ import annotations

from typing import Any, Mapping

from jasper.active_speaker.runtime_contract import classify_camilla_graph
from jasper.output_topology import (
    OutputTopology,
    OutputTopologyError,
    load_output_topology_strict,
)


class CorrectionRuntimeSafetyError(RuntimeError):
    """Raised when correction would load an unsafe CamillaDSP graph."""


def _issue_detail(raw: Any) -> str:
    if isinstance(raw, dict):
        message = raw.get("message") or raw.get("code")
        if message:
            return str(message)
    return "no legal correction graph is available"


def _first_issue(issues: tuple[dict[str, str], ...] | list[dict[str, str]]) -> str:
    return _issue_detail(issues[0]) if issues else "no legal graph is available"


def _load_topology_for_correction() -> OutputTopology:
    try:
        return load_output_topology_strict()
    except OutputTopologyError as exc:
        raise CorrectionRuntimeSafetyError(
            f"saved output topology is unavailable or invalid: {exc}"
        ) from exc


def assert_correction_graph_safe(
    text: str,
    *,
    topology: OutputTopology | None = None,
    bass_profile_summary: Mapping[str, Any] | None = None,
) -> None:
    """Refuse a generated graph using host-proved immutable bass evidence."""

    topology = topology or _load_topology_for_correction()
    if bass_profile_summary is not None and not isinstance(
        bass_profile_summary, Mapping
    ):
        raise CorrectionRuntimeSafetyError(
            "room-correction bass authority evidence is invalid"
        )
    graph = classify_camilla_graph(
        topology=topology,
        text=text,
        bass_profile_summary=bass_profile_summary,
    )
    if graph.allowed:
        return
    raise CorrectionRuntimeSafetyError(
        "room-correction generated graph is unsafe for the saved output "
        f"topology: {_first_issue(graph.issues)}"
    )
