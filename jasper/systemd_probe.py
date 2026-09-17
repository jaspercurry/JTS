# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-side ``systemctl`` probes: the one place that asks whether a unit runs.

The WRITE side — verbs, the polkit roster, transition timeouts — belongs to
:mod:`jasper.control.restart_broker`; multi-property ``systemctl show`` records
belong to :mod:`jasper.service_units`. This module owns the ``is-active``
word, the ``is-active``/``is-enabled``/``is-failed`` tri-state queries
(:func:`unit_query`) shared by the multiroom reconciler and the source-intent
coordinator, and the single-property reads behind "is it up" and "is it
installed".

Callers keep their own semantics by parameter rather than by a private copy:
``timeout`` is theirs, and ``activating_is_live`` picks the verdict a
``Type=oneshot`` still running its foreground command gets.
"""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

#: What a probe that could not reach systemd reports. ``systemctl is-active``
#: never prints this for a unit it resolved, so it cannot collide with a real
#: state word.
UNKNOWN = "unknown"

#: ``systemctl is-active`` exits 0 for exactly these (systemd's own
#: ``active_states[]``), which is the verdict the ``--quiet`` exit-code callers
#: read before this module existed.
_RUNNING = frozenset({"active", "reloading"})
_RUNNING_OR_STARTING = _RUNNING | {"activating"}

#: State words :func:`unit_query` classifies as True/False, keyed by query
#: verb. Anything else it sees for that verb -- a word absent from both sets,
#: or the query has no entry -- resolves to an unresolved (None) verdict.
_QUERY_TRUE_STATES: dict[str, frozenset[str]] = {
    "is-active": frozenset({"active"}),
    "is-enabled": frozenset({"enabled", "enabled-runtime"}),
    "is-failed": frozenset({"failed"}),
}
_QUERY_FALSE_STATES: dict[str, frozenset[str]] = {
    "is-active": frozenset({"inactive", "failed"}),
    "is-enabled": frozenset({
        "alias", "static", "indirect", "disabled", "generated", "transient",
        "linked", "linked-runtime", "masked", "masked-runtime", "not-found",
    }),
    "is-failed": frozenset({
        "active", "activating", "deactivating", "inactive", "maintenance",
        "reloading",
    }),
}


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


@dataclass(frozen=True)
class QueryResult:
    """One :func:`unit_query` probe's outcome. ``verdict`` is the tri-state
    answer most callers want; ``proc`` (a completed, unrecognised probe) and
    ``error`` (a spawn/timeout failure) carry the raw detail a caller that
    logs its own diagnostic needs when ``verdict`` is unresolved (``None``).
    Exactly one of ``proc``/``error`` is set whenever ``verdict is None``."""
    verdict: bool | None
    proc: subprocess.CompletedProcess[str] | None = None
    error: BaseException | None = None


def unit_query(query: str, unit: str, *, timeout: float) -> QueryResult:
    """Tri-state truth for one ``systemctl <query> <unit>`` probe
    (``is-active``, ``is-enabled``, ``is-failed``): True/False for a state
    word :data:`_QUERY_TRUE_STATES`/:data:`_QUERY_FALSE_STATES` classifies
    for that query, None when systemd could not be asked (missing
    ``systemctl``, a timeout, a manager/D-Bus error) or answered a word
    neither set recognises for that query -- fail-soft, never guessed.
    """
    try:
        proc = subprocess.run(
            ["systemctl", query, unit],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return QueryResult(verdict=None, error=e)
    state = (proc.stdout or "").strip().lower()
    if state in _QUERY_TRUE_STATES.get(query, frozenset()):
        return QueryResult(verdict=True, proc=proc)
    if state in _QUERY_FALSE_STATES.get(query, frozenset()):
        return QueryResult(verdict=False, proc=proc)
    return QueryResult(verdict=None, proc=proc)


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
