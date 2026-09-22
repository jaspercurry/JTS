# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Park, commit, and live-converge output topology through CamillaDSP."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jasper import camilla, dsp_apply
from jasper.control import restart_broker
from jasper.sound import runtime as sound_runtime
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.baseline_reemit import reemit_applied_baseline
from jasper.active_speaker.candidate_bank import CandidateBankRefusal
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
from jasper.log_event import log_event
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.runtime_contract import (
    PARKED_MUTED_STATUS,
    SafeGraphDecision,
    apply_safe_graph_decision_to_statefile,
    build_parked_muted_graph,
    materialise_safe_graph_decision,
    parked_safe_graph_decision,
    safe_graph_for_current_topology,
)
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.output_topology import (
    OutputTopology,
    load_output_topology_strict,
    stamp_statefile_convergence,
    topology_config_fingerprint,
)
from jasper.service_units import OUTPUTD_SERVICE

logger = logging.getLogger(__name__)

OUTPUTD_UNIT = OUTPUTD_SERVICE


@dataclass(frozen=True)
class RuntimeConvergenceResult:
    decision: SafeGraphDecision | None
    live_applied: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.decision is not None and self.decision.ok and self.live_applied

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "decision": self.decision.to_dict() if self.decision else None,
            "live_applied": self.live_applied,
            "error": self.error,
        }


PARK_SKIPPED = RuntimeConvergenceResult(None, False)


@dataclass(frozen=True)
class TopologyRuntimeMutationResult:
    parked: RuntimeConvergenceResult
    convergence: RuntimeConvergenceResult


@dataclass(frozen=True)
class StatefileConvergenceResult:
    decision: SafeGraphDecision
    topology: OutputTopology
    statefile_written: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.decision.ok and self.error is None


def converge_boot_statefile(
    *,
    statefile_path: "str | Path",
    topology_path: "str | Path | None" = None,
    topology: OutputTopology | None = None,
    current_config_path: "str | Path | None" = None,
    flat_config_path: "str | Path | None" = None,
    applied_baseline_path: "str | Path | None" = None,
    staged_metadata_path: "str | Path | None" = None,
    consider_applied_baseline: bool = True,
    write_statefile: bool = False,
) -> StatefileConvergenceResult:
    """Select or rebuild the persisted graph; never touch live CamillaDSP."""

    if topology is None:
        topology = load_output_topology_strict(topology_path)
    if write_statefile:
        # The startup gate must see every failed pass, including selection.
        stamp_statefile_convergence(statefile_path, topology, proved=False)
    kwargs: dict[str, Any] = {
        "statefile_path": statefile_path,
        "current_config_path": current_config_path,
        "applied_baseline_path": baseline_profile_state_path(applied_baseline_path),
        "staged_metadata_path": staged_metadata_path,
        "consider_applied_baseline": consider_applied_baseline,
    }
    if flat_config_path is not None:
        kwargs["flat_config_path"] = flat_config_path
    decision = safe_graph_for_current_topology(topology, **kwargs)
    if (
        write_statefile and consider_applied_baseline
        and decision.status in (PARKED_MUTED_STATUS, "select_active_startup")
        and decision.current_graph is not None and not decision.current_graph.allowed
        and decision.preferred_graph is not None and not decision.preferred_graph.allowed
    ):
        try:
            applied = load_applied_baseline_profile_state(kwargs["applied_baseline_path"])
            if applied:
                report = reemit_applied_baseline(topology, applied, playback_device=RING_ACTIVE_PLAYBACK_DEVICE)
                reproved = safe_graph_for_current_topology(topology, **kwargs)
                if (reproved.status in ("preserve_current", "select_active_baseline")
                        and reproved.selected_config_path == str(report.path)):
                    decision = reproved
                log_event(logger, "active_speaker.baseline_reemit", decision=decision.status)
        except (CandidateBankRefusal, OSError, RuntimeError, ValueError, TypeError) as exc:
            log_event(logger, "active_speaker.baseline_reemit", decision=decision.status, error=str(exc))
    if not (write_statefile and decision.ok):
        return StatefileConvergenceResult(decision, topology, False)
    try:
        decision = compose_selected_flat_graph(decision, topology=topology)
        wrote = apply_safe_graph_decision_to_statefile(
            decision,
            statefile_path=statefile_path,
            topology=topology,
        )
    except (
        ActiveSpeakerConfigError,
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as exc:
        return StatefileConvergenceResult(decision, topology, False, f"{exc}")
    stamp_statefile_convergence(statefile_path, topology, proved=True)
    return StatefileConvergenceResult(decision, topology, wrote)


def _controller(controller_factory: Callable[[], Any] | None) -> Any:
    if controller_factory is None:
        controller_factory = camilla.primary_controller
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
    profile_path: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> SafeGraphDecision:
    """Compose saved DSP onto a selected flat carrier, then re-prove it."""

    if decision.status != "select_flat" or not decision.selected_config_path:
        return decision

    kwargs: dict[str, Any] = {}
    if profile_path is not None:
        kwargs["profile_path"] = profile_path
    if config_dir is not None:
        kwargs["config_dir"] = config_dir
    composed = sound_runtime.materialise_saved_dsp_on_carrier(
        decision.selected_config_path,
        **kwargs,
    )
    reproved = safe_graph_for_current_topology(
        topology,
        current_config_path=str(composed),
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
    stay_parked: bool = False,
    parked_reason: str | None = None,
) -> RuntimeConvergenceResult:
    if stay_parked:
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
        )
        try:
            decision = compose_selected_flat_graph(
                decision,
                topology=topology,
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
    if decision.status == PARKED_MUTED_STATUS:
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
    replacement: OutputTopology | None = None,
    controller_factory: Callable[[], Any] | None = None,
    profile_path: str | Path | None = None,
    config_dir: str | Path | None = None,
    stay_parked: bool = False,
    parked_reason: str | None = None,
) -> TopologyRuntimeMutationResult:
    """Park changed intent before commit; re-pins stay parked until Apply."""

    return asyncio.run(
        _park_and_commit_topology(
            topology,
            commit,
            park=(replacement is None or topology_config_fingerprint(replacement)
                  != topology_config_fingerprint(topology)),
            controller_factory=controller_factory,
            profile_path=profile_path,
            config_dir=config_dir,
            stay_parked=stay_parked,
            parked_reason=parked_reason,
        )
    )


async def _park_and_commit_topology(
    topology: OutputTopology,
    commit: Callable[[], OutputTopology],
    *,
    park: bool,
    controller_factory: Callable[[], Any] | None,
    profile_path: str | Path | None,
    config_dir: str | Path | None,
    stay_parked: bool = False,
    parked_reason: str | None = None,
) -> TopologyRuntimeMutationResult:
    controller = _controller(controller_factory)
    lock_path = getattr(
        controller, "_graph_mutation_lock_path", dsp_apply.CANONICAL_DSP_WRITER_LOCK_PATH
    )
    async with dsp_apply.camilla_graph_mutation(
        source="output_topology.replace",
        lock_path=lock_path,
    ):
        if park:
            outputd_stop = restart_broker.manage_units(
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
        try:
            prior_path = await controller.get_config_file_path(best_effort=True)
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            prior_path = None
        parked = PARK_SKIPPED
        if park:
            parked = await _park_locked(topology, controller)
            if not parked.ok:
                raise RuntimeError(
                    parked.error or "could not safely park audio before changing topology"
                )
        # A durable atomic write still raises on a pre-publish content-fsync
        # failure (nothing published, safe to propagate and stay parked). A
        # post-publish directory-fsync failure is fail-soft in atomic_io: the
        # new topology is already on disk, so it is not reported as a commit
        # failure here.
        committed = commit()
        convergence = await _converge_committed_topology(
            committed,
            controller=controller,
            prior_config_path=prior_path,
            profile_path=profile_path,
            config_dir=config_dir,
            stay_parked=stay_parked,
            parked_reason=parked_reason,
        )
        return TopologyRuntimeMutationResult(parked, convergence)


__all__ = [
    "RuntimeConvergenceResult",
    "StatefileConvergenceResult",
    "TopologyRuntimeMutationResult",
    "compose_selected_flat_graph",
    "converge_boot_statefile",
    "park_and_commit_topology",
]
