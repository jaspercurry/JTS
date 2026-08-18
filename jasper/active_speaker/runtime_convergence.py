# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Apply a proved active-speaker runtime graph to CamillaDSP.

``runtime_contract`` owns which graph is safe.  This module owns the small
effectful step after that proof: materialise a parked graph when necessary,
write CamillaDSP's statefile, and ask the running daemon to load the selected
path.  It deliberately does not render DAC configs, reconcile hardware, or
write outputd state; those belong to the root audio-hardware reconciler.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jasper.active_speaker.runtime_contract import (
    DEFAULT_CAMILLA_STATEFILE,
    DEFAULT_FLAT_OUTPUTD_CONFIG,
    DEFAULT_RING_FLAT_OUTPUTD_CONFIG,
    SafeGraphDecision,
    apply_safe_graph_decision_to_statefile,
    materialise_safe_graph_decision,
    parked_safe_graph_decision,
    safe_graph_for_current_topology,
)
from jasper.output_topology import OutputTopology, load_output_topology_strict


@dataclass(frozen=True)
class RuntimeConvergenceResult:
    """One attempted statefile/live-graph convergence."""

    decision: SafeGraphDecision
    statefile_written: bool
    live_applied: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.decision.ok and self.live_applied

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "decision": self.decision.to_dict(),
            "statefile_written": self.statefile_written,
            "live_applied": self.live_applied,
            "error": self.error,
        }


def _load_selected_graph(
    selected_config_path: str,
    *,
    controller_factory: Callable[[], Any] | None,
) -> tuple[bool, str | None]:
    """Load one already-proved config through CamillaDSP's supported seam."""

    try:
        if controller_factory is None:
            from jasper.camilla import primary_controller

            controller_factory = primary_controller
        applied = bool(
            asyncio.run(
                controller_factory().set_config_file_path(
                    selected_config_path, best_effort=True
                )
            )
        )
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if not applied:
        return False, "CamillaDSP unreachable or rejected the proved graph"
    return True, None


def apply_runtime_graph_decision(
    decision: SafeGraphDecision,
    *,
    topology: OutputTopology,
    statefile_path: str | Path = DEFAULT_CAMILLA_STATEFILE,
    controller_factory: Callable[[], Any] | None = None,
    persist_statefile: bool = True,
) -> RuntimeConvergenceResult:
    """Materialise, persist, and live-load one contract decision.

    A decision that cannot be proved never reaches CamillaDSP.  A statefile
    write that succeeds while CamillaDSP is down still leaves the next daemon
    start safe; callers that are about to replace topology use ``ok`` and
    therefore require the live load too.
    """

    if not decision.ok or not decision.selected_config_path:
        return RuntimeConvergenceResult(
            decision=decision,
            statefile_written=False,
            live_applied=False,
            error=decision.reason,
        )
    try:
        if persist_statefile:
            wrote = apply_safe_graph_decision_to_statefile(
                decision, statefile_path=statefile_path, topology=topology
            )
        else:
            # The CamillaDSP websocket persists config selection itself. This
            # is the web-safe pre-mutation path: it may write generated configs
            # but never opens CamillaDSP's root-owned statefile directly.
            materialise_safe_graph_decision(decision, topology=topology)
            wrote = False
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return RuntimeConvergenceResult(
            decision=decision,
            statefile_written=False,
            live_applied=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    live_applied, error = _load_selected_graph(
        decision.selected_config_path, controller_factory=controller_factory
    )
    return RuntimeConvergenceResult(
        decision=decision,
        statefile_written=wrote,
        live_applied=live_applied,
        error=error,
    )


def park_for_topology(
    topology: OutputTopology,
    *,
    statefile_path: str | Path = DEFAULT_CAMILLA_STATEFILE,
    controller_factory: Callable[[], Any] | None = None,
) -> RuntimeConvergenceResult:
    """Synchronously park audio before replacing saved speaker intent."""

    decision = parked_safe_graph_decision(topology)
    return apply_runtime_graph_decision(
        decision,
        topology=topology,
        statefile_path=statefile_path,
        controller_factory=controller_factory,
        persist_statefile=False,
    )


def converge_saved_topology(
    *,
    topology_path: str | Path | None = None,
    statefile_path: str | Path = DEFAULT_CAMILLA_STATEFILE,
    current_config_path: str | Path | None = None,
    flat_config_path: str | Path = DEFAULT_FLAT_OUTPUTD_CONFIG,
    ring_flat_config_path: str | Path = DEFAULT_RING_FLAT_OUTPUTD_CONFIG,
    coupling: str | None = None,
    controller_factory: Callable[[], Any] | None = None,
) -> RuntimeConvergenceResult:
    """Select and apply the one graph the saved topology permits now."""

    topology = load_output_topology_strict(topology_path)
    decision = safe_graph_for_current_topology(
        topology,
        statefile_path=statefile_path,
        current_config_path=current_config_path,
        flat_config_path=flat_config_path,
        ring_flat_config_path=ring_flat_config_path,
        coupling=coupling,
    )
    return apply_runtime_graph_decision(
        decision,
        topology=topology,
        statefile_path=statefile_path,
        controller_factory=controller_factory,
    )


__all__ = [
    "RuntimeConvergenceResult",
    "apply_runtime_graph_decision",
    "converge_saved_topology",
    "park_for_topology",
]
