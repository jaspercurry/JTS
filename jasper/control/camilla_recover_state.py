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
``check_camilla_recover_park``. The shared engine, and the reasoning behind
its fail-soft posture, live in :mod:`jasper.control.park_record`.
"""
from __future__ import annotations

from typing import Any

from . import park_record

#: Must equal ``PARK_STATE``'s default in
#: ``deploy/bin/jasper-camilla-recover``. Pinned against that script by
#: ``tests/test_camilla_recover_script.py`` — a literal duplicated across a
#: shell writer and a Python reader is exactly the pair that drifts.
DEFAULT_STATE_PATH = "/run/jasper-camilla-recover.state"

SPEC = park_record.ParkRecordSpec(
    default_path=DEFAULT_STATE_PATH,
    path_env_var="JASPER_CAMILLA_RECOVER_PARK_STATE",
    fields=("reason", "detail", "action", "re_arm"),
    timestamp_field="parked_utc",
    timestamp_format="iso",
    required_field="reason",
)


def snapshot(path: str | None = None) -> dict[str, Any]:
    """Fail-soft read of the core-graph park record. Never raises.

    ``absent``/``unreadable``/``unintelligible`` (no ``reason``) never
    report a healthy speaker; ``present`` carries the writer's own
    ``reason``/``detail``/``action``/``re_arm`` verbatim, plus ``parked_at``
    (epoch seconds — the one park-timestamp name on the wire, converted from
    the record's own ``parked_utc``). See
    :func:`jasper.control.park_record.snapshot` for the shared shape.
    """
    return park_record.snapshot(SPEC, path)
