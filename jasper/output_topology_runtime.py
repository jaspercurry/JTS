# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime operations for saved speaker-output topology intent.

The web surface and the recovery CLI share this owner.  It parks audio before
changing durable topology, clears setup evidence only after that commit, and
asks the root hardware reconciler to converge the final saved intent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from jasper.log_event import log_event
from jasper.output_topology import (
    OutputTopology,
    OutputTopologyError,
    load_output_topology_strict,
    new_topology_draft,
    output_topology_mutation,
    topology_path,
)

logger = logging.getLogger("jasper.output_topology_runtime")

RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"
GROUPING_RECONCILE_UNIT = "jasper-grouping-reconcile.service"
RECONCILE_UNITS = (GROUPING_RECONCILE_UNIT, RECONCILE_UNIT)


def topology_summary(topology: OutputTopology) -> dict[str, Any]:
    """Return the small durable-topology summary used by runtime callers."""

    return {
        "readable": True,
        "name": topology.name,
        "status": topology.status,
        "topology_id": topology.topology_id,
        "device_label": topology.hardware.device_label,
        "physical_output_count": topology.hardware.physical_output_count,
        "speaker_groups": [
            {"id": group.id, "mode": group.mode}
            for group in topology.speaker_groups
        ],
    }


def read_before(path: str | Path | None) -> dict[str, Any]:
    """Read the existing topology for reset reporting without making it fatal."""

    try:
        return topology_summary(load_output_topology_strict(path))
    except OutputTopologyError as exc:
        return {"readable": False, "error": str(exc), "speaker_groups": []}


def trigger_reconcile(*, reason: str = "output_topology_reset") -> dict[str, Any]:
    """Synchronously ask both topology consumers to apply saved state."""

    from jasper.control.restart_broker import manage_units
    from jasper.web._unit_snapshot import probe_unit_snapshot

    result: dict[str, Any] = {"ok": True}
    for unit in RECONCILE_UNITS:
        try:
            result = manage_units(
                unit,
                verb="start",
                reason=reason,
                no_block=False,
                timeout=15.0,
            )
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not result.get("ok") and probe_unit_snapshot([unit]).state(unit).activating:
            # The blocking start above gave up at its 15s budget, but this
            # reconciler measures 25.5-26s on a Pi Zero 2 W
            # (restart_broker.py's _CAMILLA_START_EXEC_TIMEOUT_CEILING_SEC
            # comment) -- systemd's own job is still running after our
            # client stopped waiting on it. That is not a failure, just a
            # pass that outlives our patience, so say so distinctly rather
            # than let it read as the needs_attention a real failure would
            # be (#3094).
            result = {**result, "converging": True}
        log_event(
            logger,
            "output_topology.reconcile",
            reason=reason,
            unit=unit,
            ok=bool(result.get("ok")),
            converging=bool(result.get("converging")),
            error=result.get("error"),
            level=logging.INFO if result.get("ok") else logging.WARNING,
        )
        if not result.get("ok"):
            return result
    return result


def reset_to_unconfigured(
    *,
    path: str | Path | None = None,
    reconcile: bool = True,
    controller_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Park audio, clear setup, save empty intent, then run the reconciler."""

    from jasper.active_speaker.reset import clear_active_speaker_setup_state
    from jasper.active_speaker.runtime_convergence import park_and_commit_topology

    target = topology_path(path)
    with output_topology_mutation(target) as mutation:
        after = new_topology_draft()
        try:
            snapshot = mutation.snapshot()
            before = topology_summary(snapshot.topology)
            park_topology = snapshot.topology
        except OutputTopologyError as exc:
            # Corrupt intent cannot describe a safe route. Park with the
            # topology-independent empty draft, then replace the bad bytes.
            before = {
                "readable": False,
                "error": str(exc),
                "speaker_groups": [],
            }
            park_topology = after
        setup_reset: dict[str, Any]

        def commit_unconfigured() -> OutputTopology:
            nonlocal setup_reset
            mutation.save(after)
            try:
                setup_reset = clear_active_speaker_setup_state()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                # The new empty intent remains authoritative. Cleanup failure
                # is reported, never converted into an old-graph restore.
                setup_reset = {
                    "status": "partial",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            return after

        runtime = park_and_commit_topology(
            park_topology,
            commit_unconfigured,
            controller_factory=controller_factory,
        )
        reconcile_result = (
            trigger_reconcile() if reconcile else {"ok": None, "skipped": True}
        )
    log_event(
        logger,
        "output_topology.reset",
        path=str(target),
        before_status=before.get("status"),
        before_groups=len(before.get("speaker_groups") or []),
        after_status=after.status,
        after_groups=len(after.speaker_groups),
        parked_ok=runtime.parked.ok,
        runtime_convergence_ok=runtime.convergence.ok,
        cleanup_status=setup_reset.get("status"),
        cleanup_error=setup_reset.get("error"),
        reconcile_ok=reconcile_result.get("ok"),
    )
    return {
        "topology_path": str(target),
        "before": before,
        "after": topology_summary(after),
        "parked": runtime.parked.to_dict(),
        "runtime_convergence": runtime.convergence.to_dict(),
        "active_speaker_reset": setup_reset,
        "reconcile": reconcile_result,
    }


__all__ = [
    "RECONCILE_UNIT",
    "read_before",
    "reset_to_unconfigured",
    "topology_summary",
    "trigger_reconcile",
]
