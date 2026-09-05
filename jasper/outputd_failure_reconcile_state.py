# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only snapshot of the outputd failure-reconcile park.

``deploy/bin/jasper-outputd-failure-reconcile`` (jasper-outputd.service's
``ExecStopPost=``) writes the record read here on the branches that leave
outputd parked; that unit's ``ExecStartPost=`` removes it once outputd is READY
again. Why exit 78 parks lives in the script — the actor that knows.

ONE reader, two consumers (ADR-0233 rule 1): jasper-doctor's
``check_outputd_failure_reconcile_park`` and
``/state.resilience.outputd_failure_reconcile``. The systemd half is passed in,
never re-read here: both consumers hold a ``systemctl show`` view already and
rule 2 forbids ``/state`` growing a per-request fork. Re-read on every call —
neither consumer restarts when outputd parks.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from .control import park_record
from .service_units import unit_failed

#: The unit whose ``ExecStopPost=`` writes the record and ``ExecStartPost=``
#: removes it.
UNIT = "jasper-outputd.service"

#: Must equal ``PARK_RECORD``'s default in the script and the path the unit's
#: ``ExecStartPost=`` removes; pinned against both by
#: ``tests/test_outputd_failure_reconcile_state.py``. Deliberately outside
#: ``RuntimeDirectory=jasper-outputd``, which systemd deletes on the very stop
#: this record reports.
DEFAULT_RECORD_PATH = "/run/jasper-outputd-failure-reconcile.park"

#: Closed vocabulary for ``snapshot()["reason"]``.
REASON_PARKED = "parked"
REASON_UNIT_FAILED = "unit_failed"
REASON_RECORD_STALE = "park_record_stale"
REASON_UNOBSERVED = "unobserved"
REASON_OK = "ok"


def snapshot(
    unit_state: Mapping[str, Any] | None = None,
    *,
    path: str | None = None,
) -> dict[str, Any]:
    """Fail-soft read of the outputd park record. Never raises.

    ``unit_state`` is ``jasper-outputd.service``'s record from
    :func:`jasper.service_units.read_unit_states`, ``None`` where the caller has
    no systemd view. ``parked`` is True iff the record exists — the helper
    writes one only where it knows outputd is parked, so the record IS the park,
    and the unit view serves only to spot a stale one. ``reason``:

    * ``parked`` — record present; ``parked_at``/``exit_status``/
      ``park_reason`` carry the writer's fields, None where a partial write
      lost them.
    * ``unit_failed`` — no record, outputd failed: something other than a
      spent exit-78 window stopped it.
    * ``park_record_stale`` — record present, outputd running: the removal
      hook did not fire.
    * ``unobserved`` — unreadable, or absent with no systemd view. A surface
      this module cannot read must not report a healthy speaker.
    * ``ok`` — no record, outputd running.
    """
    target = path if path is not None else os.environ.get(
        "JASPER_OUTPUTD_RECONCILE_PARK_STATE", DEFAULT_RECORD_PATH
    )
    out: dict[str, Any] = {"path": target, "present": False, "parked": False}
    terminal, fields = park_record.read(target)

    if terminal is not None:
        if terminal.get("status") == "unreadable":
            out["error"] = terminal.get("error")
            out["reason"] = REASON_UNOBSERVED
        elif unit_state is None:
            out["reason"] = REASON_UNOBSERVED
        else:
            out["reason"] = (
                REASON_UNIT_FAILED if unit_failed(unit_state) else REASON_OK
            )
        return out

    out.update({
        "present": True,
        "parked_at": _epoch(fields.get("parked_at")),
        "exit_status": fields.get("exit_status"),
        "park_reason": fields.get("reason"),
    })
    if unit_state is not None and not unit_failed(unit_state):
        out["reason"] = REASON_RECORD_STALE
        return out
    out["parked"] = True
    out["reason"] = REASON_PARKED
    return out


def _epoch(raw: str | None) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None
