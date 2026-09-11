# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only snapshot of jasper-camilla-recover's core-graph park record.

``deploy/bin/jasper-camilla-recover`` runs from
``jasper-camilla.service``'s ``OnFailure=``. When its one bounded recovery
pass cannot bring the graph back, it writes a park record to
``/run/jasper-camilla-recover.state`` and stops CamillaDSP out-of-band so
the unit cannot exhaust another restart burst and re-enter the handler
(ADR-0175, issue #2564).

This module is the reader for ``jasper-doctor``'s
``check_camilla_recover_park``. The shared read half, and the reasoning
behind its fail-soft posture, live in :mod:`jasper.control.park_record`.
"""
from __future__ import annotations

import os
from typing import Any

from ..json_fields import parse_utc_iso
from . import park_record

#: Must equal ``PARK_STATE``'s default in
#: ``deploy/bin/jasper-camilla-recover``. Pinned against that script by
#: ``tests/test_camilla_recover_script.py`` — a literal duplicated across a
#: shell writer and a Python reader is exactly the pair that drifts.
DEFAULT_STATE_PATH = "/run/jasper-camilla-recover.state"


def _state_path() -> str:
    return os.environ.get(
        "JASPER_CAMILLA_RECOVER_PARK_STATE", DEFAULT_STATE_PATH
    )


def snapshot(path: str | None = None) -> dict[str, Any]:
    """Fail-soft read of the core-graph park record.

    ``absent`` and ``unreadable`` come from :mod:`jasper.control.park_record`.
    On top of those this module discriminates:

    ``{"status": "unintelligible", "parked": False, ...}``
        A record with no ``reason`` — reachable only through a partial write
        that still renames. Same posture as ``unreadable``: a surface this
        module cannot read must not report a healthy speaker.

    ``{"status": "present", "parked": True, ...}``
        A park record, with the writer's own ``reason``/``detail``/``action``/
        ``re_arm``/``parked_utc`` carried verbatim, plus ``parked_at`` (epoch
        seconds, parsed from ``parked_utc``; ``None`` on a malformed stamp).

    Every verdict carries ``last_park``: the park jasper-camilla.service's
    ``ExecStartPost=`` retired, or ``None`` where none was. The record lives
    in ``/run``, so this answers for THIS boot — a graph that parked and came
    back is otherwise invisible once the record is gone (R15, #4416).

    Never raises.
    """
    target = path if path is not None else _state_path()
    last_park = _last_park(target)
    terminal, fields = park_record.read(target)
    if terminal is not None:
        return {**terminal, "last_park": last_park}

    reason = fields.get("reason")
    if not reason:
        return {
            "status": "unintelligible",
            "parked": False,
            "path": target,
            "last_park": last_park,
        }

    parked_utc = fields.get("parked_utc")
    return {
        "status": "present",
        "parked": True,
        "path": target,
        "reason": reason,
        "detail": fields.get("detail"),
        "action": fields.get("action"),
        "re_arm": fields.get("re_arm"),
        "parked_utc": parked_utc,
        "parked_at": parse_utc_iso(parked_utc) if parked_utc else None,
        "last_park": last_park,
    }


def _last_park(path: str) -> dict[str, Any] | None:
    """This record's own fields from the most recently retired park."""
    fields = park_record.read_last(path)
    if fields is None:
        return None
    parked_utc = fields.get("parked_utc")
    return {
        "reason": fields.get("reason"),
        "parked_utc": parked_utc,
        "parked_at": parse_utc_iso(parked_utc) if parked_utc else None,
        "unparked_at": park_record.epoch_seconds(fields.get("unparked_at")),
    }
