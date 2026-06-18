"""Runtime graph safety helpers for room-correction entry points.

Room correction owns measurement state, not speaker-role policy. When it needs
to reset CamillaDSP, it asks ``jasper.active_speaker.runtime_contract`` whether
the flat baseline is legal for the saved output topology.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jasper.active_speaker.runtime_contract import (
    CONTRACT_NORMAL_MONO_FULL_RANGE,
    GRAPH_FLAT_FULL_RANGE,
    classify_camilla_graph,
    classify_output_contract,
    safe_graph_for_current_topology,
)
from jasper.output_topology import OutputTopology, load_output_topology


class CorrectionRuntimeSafetyError(RuntimeError):
    """Raised when correction would load an unsafe CamillaDSP graph."""


def _issue_detail(raw: Any) -> str:
    if isinstance(raw, dict):
        message = raw.get("message") or raw.get("code")
        if message:
            return str(message)
    return "no legal correction reset graph is available"


def _first_issue(issues: tuple[dict[str, str], ...] | list[dict[str, str]]) -> str:
    return _issue_detail(issues[0]) if issues else "no legal graph is available"


def flat_measurement_config_path(
    base_config_path: str | Path,
    *,
    topology: OutputTopology | None = None,
) -> Path:
    """Return the flat correction sweep target, or raise if it is unsafe.

    A flat sweep is only a measurement baseline for ordinary full-range output.
    Saved roleful/protected topology, invalid topology, and explicit mono
    topology that would be driven by a wider flat graph all fail before any
    sweep playback starts.
    """

    topology = topology or load_output_topology()
    contract = classify_output_contract(topology)
    base = Path(base_config_path)
    must_probe_graph = (
        contract.requires_roleful_graph
        or contract.classification == CONTRACT_NORMAL_MONO_FULL_RANGE
        or bool(contract.issues)
    )
    if not must_probe_graph:
        return base

    graph = classify_camilla_graph(base, topology)
    if graph.allowed and graph.classification == GRAPH_FLAT_FULL_RANGE:
        return base
    raise CorrectionRuntimeSafetyError(
        "room-correction flat sweep is unsafe for the saved output topology: "
        f"{_first_issue(graph.issues)}"
    )


def reset_config_path(
    base_config_path: str | Path,
    *,
    statefile_path: str | Path | None = None,
    topology: OutputTopology | None = None,
) -> Path:
    """Return the legal correction reset target for the saved topology."""

    topology = topology or load_output_topology()
    contract = classify_output_contract(topology)
    base = Path(base_config_path)
    if not contract.requires_roleful_graph:
        if contract.classification == CONTRACT_NORMAL_MONO_FULL_RANGE or contract.issues:
            graph = classify_camilla_graph(base, topology)
            if not graph.allowed:
                raise CorrectionRuntimeSafetyError(
                    "room-correction reset target is unsafe for the saved "
                    f"output topology: {_first_issue(graph.issues)}"
                )
        return base

    decision = safe_graph_for_current_topology(
        topology,
        statefile_path=statefile_path,
        flat_config_path=base,
    )
    if decision.ok and decision.selected_config_path:
        return Path(decision.selected_config_path)
    raise CorrectionRuntimeSafetyError(
        "room-correction reset has no legal graph for the saved active-speaker "
        f"topology: {_first_issue(decision.issues)}"
    )
