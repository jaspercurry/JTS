# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Park, commit, and live-converge output topology through CamillaDSP.

``runtime_contract`` owns which graph is safe. This module owns the effectful
topology transaction after that proof. It deliberately does not render DAC
configs, reconcile hardware, or write outputd state; those belong to the root
audio-hardware reconciler. It only stops outputd before topology publication so
an already-armed direct DAC lane cannot bypass the parked CamillaDSP graph.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jasper.active_speaker.runtime_contract import (
    SafeGraphDecision,
    build_parked_muted_graph,
    materialise_safe_graph_decision,
    parked_safe_graph_decision,
    safe_graph_for_current_topology,
)
from jasper.output_topology import OutputTopology

OUTPUTD_UNIT = "jasper-outputd.service"


@dataclass(frozen=True)
class RuntimeConvergenceResult:
    """One attempted live-graph convergence."""

    decision: SafeGraphDecision
    live_applied: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.decision.ok and self.live_applied

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "decision": self.decision.to_dict(),
            "live_applied": self.live_applied,
            "error": self.error,
        }


@dataclass(frozen=True)
class TopologyRuntimeMutationResult:
    """Runtime outcome around one committed topology replacement."""

    parked: RuntimeConvergenceResult
    convergence: RuntimeConvergenceResult


def _controller(controller_factory: Callable[[], Any] | None) -> Any:
    if controller_factory is None:
        from jasper.camilla import primary_controller

        controller_factory = primary_controller
    return controller_factory()


async def _park_locked(
    topology: OutputTopology, controller: Any
) -> RuntimeConvergenceResult:
    decision = parked_safe_graph_decision(topology)
    if not decision.ok:
        return RuntimeConvergenceResult(decision, False, decision.reason)
    parked_yaml, proof = build_parked_muted_graph(
        topology,
        config_path=decision.selected_config_path,
    )
    if parked_yaml is None or not proof.allowed:
        return RuntimeConvergenceResult(
            decision,
            False,
            "could not prove the parked all-muted graph",
        )
    try:
        applied = bool(
            await controller.set_active_config_raw(parked_yaml, best_effort=True)
        )
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        return RuntimeConvergenceResult(decision, False, f"{type(exc).__name__}: {exc}")
    return RuntimeConvergenceResult(
        decision,
        applied,
        (
            None
            if applied
            else "CamillaDSP unreachable or rejected the proved parked graph"
        ),
    )


def compose_selected_flat_graph(
    decision: SafeGraphDecision,
    *,
    topology: OutputTopology,
    coupling: str | None,
    profile_path: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> SafeGraphDecision:
    """Compose saved DSP onto a selected flat carrier, then re-prove it.

    This is the single flat-statefile/live-convergence seam. Boot selection and
    the web topology transaction both use it, so a future passive linearization
    cannot be kept live by one path and discarded on restart by the other.
    """

    if decision.status != "select_flat" or not decision.selected_config_path:
        return decision

    from jasper.sound.runtime import materialise_saved_dsp_on_carrier

    kwargs: dict[str, Any] = {"coupling": coupling}
    if profile_path is not None:
        kwargs["profile_path"] = profile_path
    if config_dir is not None:
        kwargs["config_dir"] = config_dir
    composed = materialise_saved_dsp_on_carrier(
        decision.selected_config_path,
        **kwargs,
    )
    reproved = safe_graph_for_current_topology(
        topology,
        current_config_path=str(composed),
        coupling=coupling,
    )
    try:
        composed_matches = (
            Path(str(reproved.selected_config_path)).resolve()
            == Path(composed).resolve()
        )
    except (OSError, RuntimeError, TypeError):
        composed_matches = False
    if reproved.status != "preserve_current" or not composed_matches:
        raise ValueError("saved sound preferences did not pass runtime graph re-proof")
    return reproved


async def _converge_committed_topology(
    topology: OutputTopology,
    *,
    controller: Any,
    prior_config_path: str | None,
    profile_path: str | Path | None,
    config_dir: str | Path | None,
    coupling: str | None,
    stay_parked: bool = False,
    parked_reason: str | None = None,
) -> RuntimeConvergenceResult:
    if stay_parked:
        # The committing caller says this topology change invalidated evidence
        # the graph selector cannot see. ``safe_graph_for_current_topology`` is
        # deliberately identity-blind — it proves a graph legal for the saved
        # SHAPE, and every rung that could fire here (preserve_current on an
        # approved active runtime, select_active_baseline, select_flat) would
        # resume audio. So skip selection entirely and make the pre-commit park
        # durable: silence is the only state that is honest about evidence the
        # box no longer has. The household's route back is the arm ladder,
        # whose gates own the missing evidence.
        decision = parked_safe_graph_decision(
            topology,
            reason=parked_reason or (
                "parked after a topology change that invalidated runtime evidence"
            ),
        )
    else:
        decision = safe_graph_for_current_topology(
            topology,
            current_config_path=prior_config_path,
            coupling=coupling,
        )
        try:
            decision = compose_selected_flat_graph(
                decision,
                topology=topology,
                coupling=coupling,
                profile_path=profile_path,
                config_dir=config_dir,
            )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            return RuntimeConvergenceResult(
                decision,
                False,
                f"{type(exc).__name__}: {exc}",
            )
    if not decision.ok or not decision.selected_config_path:
        return RuntimeConvergenceResult(decision, False, decision.reason)
    # Temporary pre-commit park is raw-only, but committed unconfigured intent
    # must make the proved parked path durable through Camilla's websocket. The
    # synchronous reconciler that follows can then derive lanes from the same
    # final state that will survive a restart, rather than the pre-reset path.
    if decision.status == "parked_muted":
        try:
            materialise_safe_graph_decision(decision, topology=topology)
            applied = bool(
                await controller.set_config_file_path(
                    decision.selected_config_path,
                    best_effort=True,
                )
            )
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
            return RuntimeConvergenceResult(
                decision, False, f"{type(exc).__name__}: {exc}"
            )
        return RuntimeConvergenceResult(
            decision,
            applied,
            None if applied else "CamillaDSP rejected the proved parked graph path",
        )
    try:
        applied = bool(
            await controller.set_config_file_path(
                decision.selected_config_path,
                best_effort=True,
            )
        )
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        return RuntimeConvergenceResult(decision, False, f"{type(exc).__name__}: {exc}")
    return RuntimeConvergenceResult(
        decision,
        applied,
        None if applied else "CamillaDSP rejected the selected graph",
    )


def park_and_commit_topology(
    topology: OutputTopology,
    commit: Callable[[], OutputTopology],
    *,
    controller_factory: Callable[[], Any] | None = None,
    profile_path: str | Path | None = None,
    config_dir: str | Path | None = None,
    coupling: str | None = None,
    stay_parked: bool = False,
    parked_reason: str | None = None,
) -> TopologyRuntimeMutationResult:
    """Park, durably commit topology, then converge under one graph lock.

    ``stay_parked`` keeps the speaker silent after the commit instead of
    converging onto a graph. Pass it when the committed change invalidated
    evidence the graph selector cannot see — per-lane channel identity is the
    case that exists today (:func:`jasper.output_topology.repin_composite_child_serials`).
    """

    return asyncio.run(
        _park_and_commit_topology(
            topology,
            commit,
            controller_factory=controller_factory,
            profile_path=profile_path,
            config_dir=config_dir,
            coupling=coupling,
            stay_parked=stay_parked,
            parked_reason=parked_reason,
        )
    )


async def _park_and_commit_topology(
    topology: OutputTopology,
    commit: Callable[[], OutputTopology],
    *,
    controller_factory: Callable[[], Any] | None,
    profile_path: str | Path | None,
    config_dir: str | Path | None,
    coupling: str | None,
    stay_parked: bool = False,
    parked_reason: str | None = None,
) -> TopologyRuntimeMutationResult:
    from jasper.dsp_apply import (
        CANONICAL_DSP_WRITER_LOCK_PATH,
        camilla_graph_mutation,
    )
    from jasper.fanin.coupling_reconcile import read_persisted_coupling
    from jasper.control.restart_broker import manage_units

    controller = _controller(controller_factory)
    lock_path = getattr(
        controller, "_graph_mutation_lock_path", CANONICAL_DSP_WRITER_LOCK_PATH
    )
    async with camilla_graph_mutation(
        source="output_topology.replace",
        lock_path=lock_path,
    ):
        outputd_stop = manage_units(
            OUTPUTD_UNIT,
            verb="stop",
            reason="output topology replace",
            no_block=False,
            timeout=15.0,
        )
        if not outputd_stop.get("ok"):
            raise RuntimeError(
                str(outputd_stop.get("error") or "could not stop outputd safely")
            )
        resolved_coupling = (
            coupling if coupling is not None else read_persisted_coupling()
        )
        try:
            prior_path = await controller.get_config_file_path(best_effort=True)
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            prior_path = None
        parked = await _park_locked(topology, controller)
        if not parked.ok:
            raise RuntimeError(
                parked.error or "could not safely park audio before changing topology"
            )
        # A durable atomic write can raise while syncing the parent directory
        # after the new topology has already replaced the old file. The
        # publication outcome is then unknown, so the only safe response is to
        # keep the proved all-muted graph and propagate the commit error.
        committed = commit()
        convergence = await _converge_committed_topology(
            committed,
            controller=controller,
            prior_config_path=prior_path,
            profile_path=profile_path,
            config_dir=config_dir,
            coupling=resolved_coupling,
            stay_parked=stay_parked,
            parked_reason=parked_reason,
        )
        return TopologyRuntimeMutationResult(
            parked=parked,
            convergence=convergence,
        )


__all__ = [
    "RuntimeConvergenceResult",
    "TopologyRuntimeMutationResult",
    "compose_selected_flat_graph",
    "park_and_commit_topology",
]
