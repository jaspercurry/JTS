# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime graph safety helpers for room-correction entry points.

Room correction owns measurement state, not speaker-role policy. Every generated
measurement, apply, or reset graph is re-checked against the saved output
topology before CamillaDSP is allowed to load it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from jasper.active_speaker.runtime_contract import (
    CONTRACT_NORMAL_MONO_FULL_RANGE,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_camilla_graph,
    classify_output_contract,
    flat_program_graph_blocked_reason,
    safe_graph_for_current_topology,
)
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
    return "no legal correction reset graph is available"


def _first_issue(issues: tuple[dict[str, str], ...] | list[dict[str, str]]) -> str:
    return _issue_detail(issues[0]) if issues else "no legal graph is available"


def _load_topology_for_correction() -> OutputTopology:
    try:
        return load_output_topology_strict()
    except OutputTopologyError as exc:
        raise CorrectionRuntimeSafetyError(
            f"saved output topology is unavailable or invalid: {exc}"
        ) from exc


def assert_flat_apply_safe(topology: OutputTopology | None = None) -> None:
    """Refuse the legacy flat apply path under a protected topology.

    The web flow now applies through the topology-aware graph carrier. This is
    the compatibility backstop for older direct callers that still emit a flat
    2-channel program graph. Fail closed: under a roleful/protected topology,
    that graph could send full-range program to a compression driver.
    """
    reason = flat_program_graph_blocked_reason(topology)
    if reason is not None:
        raise CorrectionRuntimeSafetyError(
            "room-correction apply would send full-range program to a protected "
            f"tweeter: {reason}"
        )


def reset_config_path(
    base_config_path: str | Path,
    *,
    statefile_path: str | Path | None = None,
    topology: OutputTopology | None = None,
) -> Path:
    """Return the legal correction reset target for the saved topology."""

    topology = topology or _load_topology_for_correction()
    contract = classify_output_contract(topology)
    base = Path(base_config_path)
    if not contract.requires_roleful_graph:
        if contract.classification == CONTRACT_NORMAL_MONO_FULL_RANGE or contract.issues:
            graph = classify_camilla_graph(
                base,
                topology,
                text=base.read_text(encoding="utf-8"),
                bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
            )
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


def assert_correction_graph_safe(
    text: str,
    *,
    topology: OutputTopology | None = None,
    bass_profile_summary: Mapping[str, Any] | None = None,
) -> None:
    """Refuse a generated graph using host-proved immutable bass evidence."""

    topology = topology or _load_topology_for_correction()
    graph = classify_camilla_graph(
        topology=topology,
        text=text,
        bass_profile_summary=(
            bass_profile_summary
            if isinstance(bass_profile_summary, Mapping)
            else NO_BASS_EXTENSION_PROFILE_SUMMARY
        ),
    )
    if graph.allowed:
        return
    raise CorrectionRuntimeSafetyError(
        "room-correction generated graph is unsafe for the saved output "
        f"topology: {_first_issue(graph.issues)}"
    )
