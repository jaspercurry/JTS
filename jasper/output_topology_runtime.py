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
    save_output_topology,
    topology_path,
)

logger = logging.getLogger("jasper.output_topology_runtime")

RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"


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
    """Synchronously ask the root reconciler to apply saved topology state."""

    from jasper.control.restart_broker import manage_units

    result = manage_units(
        RECONCILE_UNIT,
        verb="start",
        reason=reason,
        no_block=False,
        timeout=15.0,
    )
    log_event(
        logger,
        "output_topology.reconcile",
        reason=reason,
        unit=RECONCILE_UNIT,
        ok=bool(result.get("ok")),
        error=result.get("error"),
        level=logging.INFO if result.get("ok") else logging.WARNING,
    )
    return result


def reset_to_unconfigured(
    *,
    path: str | Path | None = None,
    reconcile: bool = True,
    topology_to_park: OutputTopology | None = None,
    commit_unconfigured: Callable[[], OutputTopology] | None = None,
) -> dict[str, Any]:
    """Park audio, clear setup, save empty intent, then run the reconciler.

    ``commit_unconfigured`` is the web caller's locked second
    optimistic-concurrency check and sole durable write. It runs after
    parking; its lock must cover both validation and ``save_output_topology``.
    The CLI has no browser snapshot, so it uses the normal local commit.
    """

    from jasper.active_speaker.reset import clear_active_speaker_setup_state
    from jasper.active_speaker.runtime_convergence import park_for_topology

    target = topology_path(path)
    before = read_before(path)
    if topology_to_park is None:
        try:
            topology_to_park = load_output_topology_strict(path)
        except OutputTopologyError:
            topology_to_park = new_topology_draft()
    parked = park_for_topology(topology_to_park)
    if not parked.ok:
        raise RuntimeError("could not safely park audio before resetting topology")

    if commit_unconfigured is not None:
        after = commit_unconfigured()
    else:
        after = new_topology_draft()
        save_output_topology(after, path)
    setup_reset = clear_active_speaker_setup_state()
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
        parked_ok=parked.ok,
        reconcile_ok=reconcile_result.get("ok"),
    )
    return {
        "topology_path": str(target),
        "before": before,
        "after": topology_summary(after),
        "parked": parked.to_dict(),
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
