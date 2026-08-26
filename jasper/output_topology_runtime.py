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
import subprocess
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

# Read-only unit probes are deliberately not brokered (any user may run them),
# so this asks systemd directly. Bounded because a save handler waits on it.
_UNIT_OUTCOME_TIMEOUT_SEC = 5.0
_UNIT_OUTCOME_PROPERTIES = ("ActiveState", "Result", "ExecMainStatus")


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


def _unit_outcome(unit: str) -> dict[str, str]:
    """systemd's own verdict on the reconcile pass that just ran.

    Empty when systemd cannot be asked at all; callers treat that as "no
    verdict" rather than as a failure.
    """

    argv = ["systemctl", "show"]
    for prop in _UNIT_OUTCOME_PROPERTIES:
        argv += ["--property", prop]
    argv.append(unit)
    try:
        show = subprocess.run(
            argv,
            check=False,
            timeout=_UNIT_OUTCOME_TIMEOUT_SEC,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if show.returncode != 0:
        return {}
    properties: dict[str, str] = {}
    for line in show.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in _UNIT_OUTCOME_PROPERTIES:
            properties[key] = value.strip()
    return properties


def _with_reconcile_outcome(unit: str, started: dict[str, Any]) -> dict[str, Any]:
    """Replace the broker's START verdict with the reconcile's OUTCOME.

    The broker's ``ok``/``rc`` say systemd accepted and ran the unit; a
    ``Type=oneshot`` reconciler that refuses its candidate and preserves the
    runtime env (``EX_CONFIG`` 78) starts perfectly well and converges
    nothing (#2493). Gate on ``ActiveState`` and merely report ``Result`` —
    the rule ``jasper.cli.doctor.correction`` already applies, because
    ``ActiveState`` is the broader signal and survives a marker rename.
    """

    properties = _unit_outcome(unit)
    if properties.get("ActiveState") != "failed":
        return started
    exit_status = properties.get("ExecMainStatus", "")
    return {
        **started,
        "ok": False,
        "unit": unit,
        "active_state": "failed",
        "result": properties.get("Result", "") or "unknown",
        "exit_status": int(exit_status) if exit_status.isdigit() else None,
        "error": f"{unit} failed to converge",
    }


def trigger_reconcile(*, reason: str = "output_topology_reset") -> dict[str, Any]:
    """Synchronously ask both topology consumers to apply saved state."""

    from jasper.control.restart_broker import manage_units

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
            if result.get("ok"):
                result = _with_reconcile_outcome(unit, result)
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        log_event(
            logger,
            "output_topology.reconcile",
            reason=reason,
            unit=unit,
            ok=bool(result.get("ok")),
            unit_result=result.get("result"),
            exit_status=result.get("exit_status"),
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
