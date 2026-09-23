# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-side ``systemctl`` probes: the one place that asks whether a unit runs.

The WRITE side — verbs, the polkit roster, transition timeouts — belongs to
:mod:`jasper.control.restart_broker`; ``systemctl show`` records and single
properties (``LoadState``, ``ActiveState``, ``ExecStart``, ...) belong to
:mod:`jasper.service_units`. This module owns the ``is-active`` /
``is-enabled`` / ``is-failed`` reads: :func:`unit_state` for one unit,
:func:`unit_states` for a batch, and :func:`async_unit_probe` on an event loop.
Classifiers interpret the word systemd printed.

Callers keep their own semantics by parameter rather than by a private copy:
``timeout`` is theirs, and ``activating_is_live`` picks the verdict a
``Type=oneshot`` still running its foreground command gets.
"""
from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

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


@dataclass(frozen=True)
class UnitState:
    """One ``systemctl <query> <unit>`` read: the state word, or why there
    is none.

    ``word`` is the lowercased state TEXT, and ``None`` ONLY when the spawn
    itself failed -- then ``error`` carries it (``FileNotFoundError`` for a
    box without systemctl, which callers distinguish from a real failure).
    ``rc``/``stderr`` are the diagnostic for a probe that completed but whose
    word no classifier recognises. No classifier here reads ``rc``: all three
    queries exit non-zero for legitimate FALSE words, so a manager/D-Bus error
    must not masquerade as disabled or inactive.
    """

    query: str
    word: str | None
    error: BaseException | None = None
    rc: int | None = None
    stderr: str = ""


def unit_state(query: str, unit: str, *, timeout: float) -> UnitState:
    """Run one ``systemctl <query> <unit>`` (``is-active``, ``is-enabled``,
    ``is-failed``). Fail-soft: a missing ``systemctl``, a timeout or a
    manager error is reported, never raised."""
    try:
        proc = subprocess.run(
            ["systemctl", query, unit],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return UnitState(query=query, word=None, error=e)
    return UnitState(
        query=query,
        word=(proc.stdout or "").strip().lower(),
        rc=proc.returncode,
        stderr=(proc.stderr or "").strip(),
    )


async def async_unit_probe(
    query: str, unit: str, *, timeout: float,
) -> subprocess.CompletedProcess[str] | None:
    """Read a unit without blocking the loop; kill and reap on timeout/cancel."""
    args = ["systemctl", query, unit]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, TimeoutError):
        return None
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await proc.communicate()
    except (asyncio.CancelledError, TimeoutError) as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError, OSError):
            async with asyncio.timeout(1.0):
                await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return None
    return subprocess.CompletedProcess(
        args, cast(int, proc.returncode),
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


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
    live = _RUNNING_OR_STARTING if activating_is_live else _RUNNING
    return state in live


def unit_query(result: UnitState) -> bool | None:
    """Tri-state truth for one :func:`unit_state` read. PURE.

    True/False for a word :data:`_QUERY_TRUE_STATES`/:data:`_QUERY_FALSE_STATES`
    classifies for that query, None when systemd could not be asked or answered
    a word neither set recognises for it -- fail-soft, never guessed.
    """
    if result.word is None:
        return None
    if result.word in _QUERY_TRUE_STATES.get(result.query, frozenset()):
        return True
    if result.word in _QUERY_FALSE_STATES.get(result.query, frozenset()):
        return False
    return None


def unit_active(unit: str, *, timeout: float, activating_is_live: bool) -> bool:
    """Whether ``unit`` counts as running.

    ``activating_is_live`` is the caller's verdict on a ``Type=oneshot`` whose
    foreground command has not exited yet: a caller reading job liveness wants
    True (a multi-minute install must not look interrupted), a caller reading
    readiness wants False. A failed probe is never live.
    """
    state = unit_state("is-active", unit, timeout=timeout).word
    return state_is_live(state or UNKNOWN, activating_is_live=activating_is_live)
