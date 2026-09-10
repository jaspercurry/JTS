# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only snapshot of jasper-camilla-topology-gate's refusal record.

``deploy/bin/jasper-camilla-topology-gate`` runs as
``jasper-camilla.service``'s ``ExecCondition=``. When the graph the statefile
names was proved against a different speaker topology than the one the last
convergence was working on, it writes a record to
``/run/jasper-camilla-topology-gate.state`` and skips the start (#4416 R8). The
gate clears the record on every start it allows, so a present record always
describes the CURRENT refusal.

Readers: ``jasper-doctor``'s ``check_camilla_topology_gate`` and the heal
supervisor. The shared read half, and its fail-soft posture, live in
:mod:`jasper.control.park_record`.
"""
from __future__ import annotations

import os
from typing import Any

from ..json_fields import parse_utc_iso
from . import park_record

#: Must equal ``GATE_STATE``'s default in
#: ``deploy/bin/jasper-camilla-topology-gate``. Pinned against that script by
#: ``tests/test_camilla_topology_gate_script.py``.
DEFAULT_STATE_PATH = "/run/jasper-camilla-topology-gate.state"


def _state_path() -> str:
    return os.environ.get(
        "JASPER_CAMILLA_TOPOLOGY_GATE_STATE", DEFAULT_STATE_PATH
    )


def snapshot(path: str | None = None) -> dict[str, Any]:
    """Fail-soft read of the gate's refusal record.

    ``absent`` and ``unreadable`` come from :mod:`jasper.control.park_record`;
    on top of those a record carrying no ``reason`` is ``unintelligible`` (a
    partial write that still renamed). A ``present`` record carries the gate's
    own ``reason``/``detail``/``unproved``/``proved``/``action``/``re_arm``
    verbatim, plus ``refused_at`` (epoch seconds, ``None`` on a malformed
    stamp).

    ``refused`` is the single boolean a consumer branches on. Never raises.
    """
    target = path if path is not None else _state_path()
    terminal, fields = park_record.read(target)
    if terminal is not None:
        terminal.pop("parked", None)
        terminal["refused"] = False
        return terminal

    reason = fields.get("reason")
    if not reason:
        return {"status": "unintelligible", "refused": False, "path": target}

    refused_utc = fields.get("refused_utc")
    return {
        "status": "present",
        "refused": True,
        "path": target,
        "reason": reason,
        "detail": fields.get("detail"),
        "unproved": fields.get("unproved"),
        "proved": fields.get("proved"),
        "action": fields.get("action"),
        "re_arm": fields.get("re_arm"),
        "refused_utc": refused_utc,
        "refused_at": parse_utc_iso(refused_utc) if refused_utc else None,
    }
