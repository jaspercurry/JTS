# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Apply a proved active-speaker runtime graph to CamillaDSP.

``runtime_contract`` owns which graph is safe. This module owns the effectful
step after that proof. Boot callers may persist a selected path; live web
callers load through CamillaDSP's API, so Camilla remains the sole writer of its
statefile. It deliberately does not render DAC configs, reconcile hardware, or
write outputd state; those belong to the root audio-hardware reconciler.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jasper.active_speaker.runtime_contract import (
    DEFAULT_CAMILLA_STATEFILE,
    SafeGraphDecision,
    apply_safe_graph_decision_to_statefile,
    build_parked_muted_graph,
    materialise_safe_graph_decision,
    parked_safe_graph_decision,
    safe_graph_for_current_topology,
)
from jasper.output_topology import OutputTopology


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


@dataclass(frozen=True)
class TopologyRuntimeMutationResult:
    """Runtime outcome around one committed topology replacement."""

    parked: RuntimeConvergenceResult
    committed_topology: OutputTopology
    convergence: RuntimeConvergenceResult
    prior_config_path: str | None

    @property
    def ok(self) -> bool:
        return self.parked.ok and self.convergence.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "parked": self.parked.to_dict(),
            "convergence": self.convergence.to_dict(),
            "prior_config_path": self.prior_config_path,
        }


class TopologyCommitRestoreError(RuntimeError):
    """A topology commit failed after park and the prior graph did not restore."""


async def _load_selected_graph(
    selected_config_path: str,
    *,
    controller_factory: Callable[[], Any] | None,
    persist_statefile: bool,
) -> tuple[bool, str | None]:
    """Load one already-proved config through CamillaDSP's supported seam."""

    try:
        controller = _controller(controller_factory)
        if persist_statefile:
            applied = bool(
                await controller.set_config_file_path(
                    selected_config_path, best_effort=True
                )
            )
        else:
            candidate = Path(selected_config_path).read_text(encoding="utf-8")
            try:
                normalized = await controller.normalize_config_raw(
                    candidate, best_effort=True
                )
                active = await controller.get_active_config_raw(best_effort=True)
            except AttributeError:
                normalized = None
                active = None
            if normalized is not None and normalized == active:
                return True, None
            applied = bool(
                await controller.set_config_file_path(
                    selected_config_path, best_effort=True
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
    """Materialise and live-load one contract decision.

    A decision that cannot be proved never reaches CamillaDSP. Boot callers can
    also persist the selected path. Live callers leave path persistence to
    CamillaDSP and reload the selected path only when its normalized YAML
    differs from the running graph.
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
    live_applied, error = asyncio.run(
        _load_selected_graph(
            decision.selected_config_path,
            controller_factory=controller_factory,
            persist_statefile=persist_statefile,
        )
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
    controller_factory: Callable[[], Any] | None = None,
) -> RuntimeConvergenceResult:
    """Temporarily park audio without changing Camilla's persisted path."""

    return asyncio.run(
        _park_for_topology(topology, controller_factory=controller_factory)
    )


def _controller(controller_factory: Callable[[], Any] | None) -> Any:
    if controller_factory is None:
        from jasper.camilla import primary_controller

        controller_factory = primary_controller
    return controller_factory()


async def _park_locked(topology: OutputTopology, controller: Any) -> RuntimeConvergenceResult:
    decision = parked_safe_graph_decision(topology)
    if not decision.ok:
        return RuntimeConvergenceResult(decision, False, False, decision.reason)
    parked_yaml, proof = build_parked_muted_graph(
        topology,
        config_path=decision.selected_config_path,
    )
    if parked_yaml is None or not proof.allowed:
        return RuntimeConvergenceResult(
            decision,
            False,
            False,
            "could not prove the parked all-muted graph",
        )
    try:
        applied = bool(
            await controller.set_active_config_raw(parked_yaml, best_effort=True)
        )
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        return RuntimeConvergenceResult(
            decision, False, False, f"{type(exc).__name__}: {exc}"
        )
    return RuntimeConvergenceResult(
        decision,
        False,
        applied,
        None if applied else "CamillaDSP unreachable or rejected the proved parked graph",
    )


async def _park_for_topology(
    topology: OutputTopology,
    *,
    controller_factory: Callable[[], Any] | None,
) -> RuntimeConvergenceResult:
    from jasper.dsp_apply import (
        CANONICAL_DSP_WRITER_LOCK_PATH,
        camilla_graph_mutation,
    )

    controller = _controller(controller_factory)
    lock_path = getattr(
        controller, "_graph_mutation_lock_path", CANONICAL_DSP_WRITER_LOCK_PATH
    )
    async with camilla_graph_mutation(
        source="output_topology.park",
        lock_path=lock_path,
    ):
        return await _park_locked(topology, controller)


async def _restore_prior_config(controller: Any, prior_path: str | None) -> str | None:
    if not prior_path:
        return "CamillaDSP did not report a prior config path"
    try:
        restored = bool(
            await controller.set_config_file_path(prior_path, best_effort=True)
        )
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None if restored else "CamillaDSP rejected the prior config path"


async def _converge_committed_topology(
    topology: OutputTopology,
    *,
    controller: Any,
    prior_config_path: str | None,
    profile_path: str | Path | None,
    config_dir: str | Path | None,
    coupling: str | None,
) -> RuntimeConvergenceResult:
    decision = safe_graph_for_current_topology(
        topology,
        current_config_path=prior_config_path,
        coupling=coupling,
    )
    if decision.status == "select_flat" and decision.selected_config_path:
        try:
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
            decision = safe_graph_for_current_topology(
                topology,
                current_config_path=str(composed),
                coupling=coupling,
            )
            try:
                composed_matches = (
                    Path(str(decision.selected_config_path)).resolve()
                    == Path(composed).resolve()
                )
            except (OSError, RuntimeError, TypeError):
                composed_matches = False
            if decision.status != "preserve_current" or not composed_matches:
                return RuntimeConvergenceResult(
                    decision,
                    False,
                    False,
                    "saved sound preferences did not pass runtime graph re-proof",
                )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            return RuntimeConvergenceResult(
                decision,
                False,
                False,
                f"{type(exc).__name__}: {exc}",
            )
    if not decision.ok or not decision.selected_config_path:
        return RuntimeConvergenceResult(
            decision, False, False, decision.reason
        )
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
                decision, False, False, f"{type(exc).__name__}: {exc}"
            )
        return RuntimeConvergenceResult(
            decision,
            False,
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
        return RuntimeConvergenceResult(
            decision, False, False, f"{type(exc).__name__}: {exc}"
        )
    return RuntimeConvergenceResult(
        decision,
        False,
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
) -> TopologyRuntimeMutationResult:
    """Park, durably commit topology, then converge under one graph lock."""

    return asyncio.run(
        _park_and_commit_topology(
            topology,
            commit,
            controller_factory=controller_factory,
            profile_path=profile_path,
            config_dir=config_dir,
            coupling=coupling,
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
) -> TopologyRuntimeMutationResult:
    from jasper.dsp_apply import (
        CANONICAL_DSP_WRITER_LOCK_PATH,
        camilla_graph_mutation,
    )

    controller = _controller(controller_factory)
    lock_path = getattr(
        controller, "_graph_mutation_lock_path", CANONICAL_DSP_WRITER_LOCK_PATH
    )
    async with camilla_graph_mutation(
        source="output_topology.replace",
        lock_path=lock_path,
    ):
        try:
            prior_path = await controller.get_config_file_path(best_effort=True)
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            prior_path = None
        parked = await _park_locked(topology, controller)
        if not parked.ok:
            raise RuntimeError(
                parked.error or "could not safely park audio before changing topology"
            )
        try:
            committed = commit()
        except Exception as exc:  # noqa: BLE001 - every commit failure needs rollback
            restore_error = await _restore_prior_config(controller, prior_path)
            if restore_error is not None:
                raise TopologyCommitRestoreError(
                    "topology commit failed after audio was parked and the prior "
                    f"graph could not be restored: {restore_error}"
                ) from exc
            raise
        convergence = await _converge_committed_topology(
            committed,
            controller=controller,
            prior_config_path=prior_path,
            profile_path=profile_path,
            config_dir=config_dir,
            coupling=coupling,
        )
        return TopologyRuntimeMutationResult(
            parked=parked,
            committed_topology=committed,
            convergence=convergence,
            prior_config_path=prior_path,
        )


__all__ = [
    "RuntimeConvergenceResult",
    "TopologyCommitRestoreError",
    "TopologyRuntimeMutationResult",
    "apply_runtime_graph_decision",
    "park_and_commit_topology",
    "park_for_topology",
]
