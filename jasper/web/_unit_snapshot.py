# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One bounded, request-scoped systemd snapshot for wizard state pages.

The callers pass fixed unit names owned by their page; no request data reaches
``systemctl``.  A non-zero return code can still carry useful records (for
example when one requested unit is missing), so parsing is deliberately based
on stdout rather than the aggregate exit status.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)

_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")


@dataclass(frozen=True)
class UnitState:
    """The two systemd properties needed by the source-management pages."""

    load_state: str | None = None
    active_state: str | None = None

    @property
    def available(self) -> bool:
        return self.load_state == "loaded"

    @property
    def active(self) -> bool:
        return self.active_state == "active"

    @property
    def activating(self) -> bool:
        return self.active_state == "activating"


@dataclass(frozen=True)
class UnitSnapshot:
    """Parsed records from one ``systemctl show`` invocation."""

    states: dict[str, UnitState]
    error: str = ""

    def state(self, unit: str) -> UnitState:
        return self.states.get(unit, UnitState())

    def available(self, unit: str) -> bool:
        return self.state(unit).available

    def active(self, unit: str) -> bool:
        return self.state(unit).active

    def activating(self, unit: str) -> bool:
        return self.state(unit).activating


def _parse_show_output(output: str, allowed_units: frozenset[str]) -> dict[str, UnitState]:
    states: dict[str, UnitState] = {}
    for record in re.split(r"\n\s*\n", output.strip()):
        properties: dict[str, str] = {}
        for line in record.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value
        unit = properties.get("Id")
        if unit not in allowed_units:
            continue
        states[unit] = UnitState(
            load_state=properties.get("LoadState"),
            active_state=properties.get("ActiveState"),
        )
    return states


def probe_unit_snapshot(
    units: Iterable[str],
    *,
    timeout: float = 5.0,
) -> UnitSnapshot:
    """Read fixed unit load/activity state with one bounded subprocess.

    Duplicate names are collapsed in first-seen order. Invalid or empty unit
    lists fail closed without invoking systemd. Missing records remain unknown,
    which makes all three boolean predicates false.
    """

    requested = tuple(dict.fromkeys(units))
    if not requested:
        return UnitSnapshot({}, error="no systemd units requested")
    invalid = tuple(unit for unit in requested if not _UNIT_NAME_RE.fullmatch(unit))
    if invalid:
        return UnitSnapshot({}, error=f"invalid systemd unit names: {', '.join(invalid)}")

    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                "--no-pager",
                "--property=Id",
                "--property=LoadState",
                "--property=ActiveState",
                *requested,
            ],
            check=False,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("batched systemd unit probe failed: %s", exc)
        return UnitSnapshot({}, error=str(exc))

    states = _parse_show_output(proc.stdout or "", frozenset(requested))
    missing = tuple(unit for unit in requested if unit not in states)
    errors: list[str] = []
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        errors.append(detail or f"systemctl exited {proc.returncode}")
    if missing:
        errors.append(f"no state returned for: {', '.join(missing)}")
    error = "; ".join(errors)
    if error:
        logger.debug("batched systemd unit probe incomplete: %s", error)
    return UnitSnapshot(states, error=error)


__all__ = ["UnitSnapshot", "UnitState", "probe_unit_snapshot"]
