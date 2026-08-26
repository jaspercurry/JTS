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

This module is the reader, and it is deliberately ONE reader with two
consumers — ``jasper-doctor``'s ``check_camilla_recover_park`` and
``/state.resilience.camilla_recover`` — so the operator-facing verdict
cannot differ between the two surfaces. It is the core-graph twin of
:mod:`jasper.control.content_lane_state`; the shared read half, and the
reasoning behind its fail-soft posture, live in
:mod:`jasper.control.park_record`.

**Freshness.** Re-read on every call: jasper-control is not restarted when
the graph parks, so a value captured at import would be permanently wrong.
"""
from __future__ import annotations

import os
from typing import Any

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
        ``re_arm`` carried verbatim. The writer only ever writes on a park,
        so a legible record IS a park.

    Never raises.
    """
    target = path if path is not None else _state_path()
    terminal, fields = park_record.read(target)
    if terminal is not None:
        return terminal

    reason = fields.get("reason")
    if not reason:
        return {"status": "unintelligible", "parked": False, "path": target}

    return {
        "status": "present",
        "parked": True,
        "path": target,
        "reason": reason,
        "parked_utc": fields.get("parked_utc"),
        "detail": fields.get("detail"),
        "action": fields.get("action"),
        "re_arm": fields.get("re_arm"),
    }
