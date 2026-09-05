# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only snapshot of the outputd failure-reconcile park.

``deploy/bin/jasper-outputd-failure-reconcile`` runs from
``jasper-outputd.service``'s ``ExecStopPost=``. On any failure stop it
refreshes the output-hardware env before systemd's ``Restart=on-failure``
retry, and it bounds itself to one reconcile per window across every failure
class with a stamp file holding the epoch second of that pass. A CONFIG
failure (exit 78, ``EX_CONFIG``) additionally gets one explicit retry; a
second exit 78 inside the same window finds the window already spent, does
nothing, and ``RestartPreventExitStatus=78`` leaves the unit parked.

This module is the ONE reader of that pair of facts (ADR-0233 rule 1), with
two consumers — jasper-doctor's ``check_outputd_failure_reconcile_park`` and
``/state.resilience.outputd_failure_reconcile`` — so the operator and the
household surfaces cannot disagree about whether the box is parked.

**The systemd half is passed in, never re-read here.** Both consumers already
hold a ``systemctl show`` view (the doctor's per-run evidence memo, and
jasper-control's system-metrics sampler), and ADR-0233 rule 2 forbids
``/state`` growing a probe that forks per request.

**Freshness.** Re-read on every call: neither consumer restarts when outputd
parks, so a value captured at import would be permanently wrong.
"""
from __future__ import annotations

import os
import time
from typing import Any, Mapping

#: The unit whose ``ExecStopPost=`` owns the stamp.
UNIT = "jasper-outputd.service"

#: Must equal ``RECONCILE_STAMP`` / ``RECONCILE_WINDOW_SEC`` defaults in
#: ``deploy/bin/jasper-outputd-failure-reconcile``. Pinned against that script
#: by ``tests/test_outputd_failure_reconcile_state.py`` — a literal duplicated
#: across a shell writer and a Python reader is exactly the pair that drifts.
DEFAULT_STAMP_PATH = "/run/jasper-outputd/failure-reconcile.stamp"
DEFAULT_WINDOW_SEC = 300

#: Closed vocabulary for ``snapshot()["reason"]``.
REASON_RUNTIME_DIR_ABSENT = "runtime_dir_absent"
REASON_NO_RECONCILE = "no_reconcile"
REASON_UNREADABLE = "unreadable"
REASON_UNINTELLIGIBLE = "unintelligible"
REASON_UNIT_STATE_UNAVAILABLE = "unit_state_unavailable"
REASON_RECONCILED = "reconciled"
REASON_PARKED = "parked"


def _stamp_path() -> str:
    return os.environ.get(
        "JASPER_OUTPUTD_CONFIG_RETRY_STATE", DEFAULT_STAMP_PATH
    )


def _window_sec() -> int:
    raw = os.environ.get("JASPER_OUTPUTD_CONFIG_RETRY_WINDOW_SEC")
    try:
        return int(raw) if raw else DEFAULT_WINDOW_SEC
    except ValueError:
        return DEFAULT_WINDOW_SEC


def _base(path: str, window: int) -> dict[str, Any]:
    return {
        "present": False,
        "path": path,
        "at": None,
        "age_s": None,
        "window_sec": window,
        "window_spent": False,
        "parked": False,
    }


def snapshot(
    unit_state: Mapping[str, Any] | None = None,
    *,
    path: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Fail-soft read of the failure-reconcile stamp and the park it implies.

    ``unit_state`` is ``jasper-outputd.service``'s record from
    :func:`jasper.service_units.read_unit_states` (``None`` where the caller
    has no systemd view). ``parked`` is True only when a reconcile pass is on
    record AND the unit is sitting ``failed``: the helper already ran and did
    not bring outputd back, and outputd owns the DAC write loop.

    ``reason`` is one of the module's ``REASON_*`` constants:

    ``runtime_dir_absent``
        No ``/run/jasper-outputd`` — systemd removes the RuntimeDirectory when
        the unit stops for good, so there is no evidence either way.
    ``no_reconcile``
        Runtime directory present, no stamp: outputd has not failed this boot.
    ``unreadable`` / ``unintelligible``
        The stamp is there but cannot be read, or does not hold an epoch
        second. Reported distinctly from absent — a surface this module cannot
        read must not report a healthy speaker.
    ``unit_state_unavailable``
        A reconcile is on record but the caller has no systemd view, so the
        park cannot be ruled in or out.
    ``reconciled``
        A reconcile is on record and outputd is not failed — the helper did
        its job.
    ``parked``
        A reconcile is on record and outputd is failed.

    Never raises.
    """
    target = path if path is not None else _stamp_path()
    window = _window_sec()
    out = _base(target, window)

    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except FileNotFoundError:
        if not os.path.isdir(os.path.dirname(target) or "/"):
            out["reason"] = REASON_RUNTIME_DIR_ABSENT
            return out
        out["reason"] = REASON_NO_RECONCILE
        return out
    except OSError as exc:
        out["reason"] = REASON_UNREADABLE
        out["error"] = str(exc)
        return out

    try:
        at = int(raw.strip())
    except ValueError:
        out["present"] = True
        out["reason"] = REASON_UNINTELLIGIBLE
        return out

    age = (time.time() if now is None else now) - at
    out["present"] = True
    out["at"] = at
    out["age_s"] = round(age, 1)
    out["window_spent"] = 0 <= age < window

    if unit_state is None:
        out["reason"] = REASON_UNIT_STATE_UNAVAILABLE
        return out
    if unit_state.get("active_state") == "failed":
        out["parked"] = True
        out["reason"] = REASON_PARKED
        out["result"] = unit_state.get("result")
        return out
    out["reason"] = REASON_RECONCILED
    return out
