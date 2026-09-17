# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-side ``systemctl`` probes: the one place that asks whether a unit runs.

The WRITE side — verbs, the polkit roster, transition timeouts — belongs to
:mod:`jasper.control.restart_broker`; multi-property ``systemctl show`` records
belong to :mod:`jasper.service_units`. This module owns the ``is-active`` word
and the single-property reads behind "is it up" and "is it installed".

Callers keep their own semantics by parameter rather than by a private copy:
``timeout`` is theirs, and ``activating_is_live`` picks the verdict a
``Type=oneshot`` still running its foreground command gets.
"""
from __future__ import annotations

import subprocess
from collections.abc import Sequence

#: What a probe that could not reach systemd reports. ``systemctl is-active``
#: never prints this for a unit it resolved, so it cannot collide with a real
#: state word.
UNKNOWN = "unknown"

#: ``systemctl is-active`` exits 0 for exactly these (systemd's own
#: ``active_states[]``), which is the verdict the ``--quiet`` exit-code callers
#: read before this module existed.
_RUNNING = frozenset({"active", "reloading"})
_RUNNING_OR_STARTING = _RUNNING | {"activating"}


def unit_states(units: Sequence[str], *, timeout: float) -> dict[str, str]:
    """``systemctl is-active <units…>`` → ``{unit: state word}``.

    One spawn answers every unit: ``is-active`` prints one line per argument,
    in argument order. Its exit code is non-zero whenever any unit is not
    active — the normal case for a roster probe — so only stdout is read.
    Fail-soft: absent systemd, a timeout, or a line-count mismatch resolves
    every unit to :data:`UNKNOWN` rather than raising.
    """
    if not units:
        return {}
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", *units],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return {unit: UNKNOWN for unit in units}
    lines = proc.stdout.splitlines()
    if len(lines) != len(units):
        return {unit: UNKNOWN for unit in units}
    return {unit: (lines[i].strip() or UNKNOWN) for i, unit in enumerate(units)}


def state_is_live(state: str, *, activating_is_live: bool) -> bool:
    """Whether an ``is-active`` word counts as running.

    Split out of :func:`unit_active` for callers that already hold the word —
    a batched read, or a probe whose failure they want to raise on.
    """
    return state in (_RUNNING_OR_STARTING if activating_is_live else _RUNNING)


def unit_active(unit: str, *, timeout: float, activating_is_live: bool) -> bool:
    """Whether ``unit`` counts as running.

    ``activating_is_live`` is the caller's verdict on a ``Type=oneshot`` whose
    foreground command has not exited yet: a caller reading job liveness wants
    True (a multi-minute install must not look interrupted), a caller reading
    readiness wants False. A failed probe is never live.
    """
    state = unit_states([unit], timeout=timeout)[unit]
    return state_is_live(state, activating_is_live=activating_is_live)


def unit_property(unit: str, prop: str, *, timeout: float) -> str | None:
    """One ``systemctl show`` property value, or ``None`` when the probe failed.

    Unlike :func:`unit_states` this separates "systemd answered, with an empty
    value" from "we could not ask", which the tri-state callers need.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, f"--property={prop}", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def unit_loaded(unit: str, *, timeout: float) -> bool:
    """Whether systemd could load ``unit``'s unit file.

    A failed probe is not loaded: a box that never installs a unit and a box
    whose probe broke both mean "do not offer it".
    """
    return unit_property(unit, "LoadState", timeout=timeout) == "loaded"
