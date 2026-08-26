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
cannot differ between the two surfaces. Shape, field vocabulary, and
fail-soft posture mirror :mod:`jasper.control.content_lane_state`, the
outputd park this one is the core-graph twin of.

**Freshness.** Re-read on every call: jasper-control is not restarted when
the graph parks, so a value captured at import would be permanently wrong.
"""
from __future__ import annotations

import os
from typing import Any

from ..env_load import parse_env_text

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

    Returns one of four shapes, discriminated by ``status``:

    ``{"status": "absent", "parked": False}``
        No record — the recovery handler has not parked the graph this boot
        (``/run`` wipes at boot, so this is per-boot truth).

    ``{"status": "unreadable", "parked": False, "error": ...}``
        The record exists but could not be read. Reported distinctly from
        "absent" on purpose: a permissions regression must not read as a
        healthy speaker.

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
    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {"status": "absent", "parked": False}
    except OSError as exc:
        return {
            "status": "unreadable",
            "parked": False,
            "path": target,
            "error": str(exc),
        }

    try:
        fields = parse_env_text(text)
    except Exception:  # noqa: BLE001 - a malformed record must not raise here
        fields = {}

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
