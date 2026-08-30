# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Selecting banked takes: seven columns, rescanned from the files each read.

A campaign is dozens of takes across positions and candidates, and without this
the only way to find one is to glob a directory and parse every hit by hand.

**No index file exists** (ADR-0198). Every read rescans the banked takes into
an in-memory table and selects over that, so there is nothing to write,
nothing to go stale, and nothing to reconcile — the take files are the single
source of truth at the read side as well as the write side. The rescan costs
nothing these callers were not already paying: each opens every selected take
anyway.

Only measurement takes get a row: five of the store's six artifact kinds are
group payloads, a candidate, a receipt and a finding set.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..commissioning_evidence_store import EVIDENCE_ROOT
from .contracts import (
    BANKED_TAKE_GLOB,
    MEASURE_KIND_KEY,
    POSITION_EVIDENCE_KIND,
)

__all__ = [
    "Measurement",
    "bundle_measurements",
]

#: The in-memory table every read builds and throws away.
_TABLE = """
CREATE TABLE IF NOT EXISTS measurements (
    path         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    phase        TEXT NOT NULL DEFAULT '',
    position_deg INTEGER,
    candidate_id TEXT NOT NULL DEFAULT '',
    captured_at  TEXT
)
"""

_COLUMNS = (
    "path", "session_id", "kind", "phase", "position_deg", "candidate_id",
    "captured_at",
)


@dataclass(frozen=True)
class Measurement:
    """One selected take. ``path`` is the id ``bank`` returned for it."""

    path: str
    session_id: str
    kind: str
    phase: str
    position_deg: int | None
    candidate_id: str
    captured_at: str | None


@contextmanager
def _connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """One connection per read, over ``:memory:`` — nothing here is durable."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_TABLE)
        yield connection
        connection.commit()
    finally:
        connection.close()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _position_deg(value: Any) -> int | None:
    """The signed whole-degree bearing, or ``None`` where none was commanded.

    ``bool`` is an ``int`` subclass, so it is excluded rather than read as a
    bearing of 0 or 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _captured_at(value: Any) -> str | None:
    """The take's own capture time as ISO-8601 UTC, or ``None``.

    **The builders disagree about the type**: the cloud position emits a Unix
    epoch ``float`` where the lateral pose, the entry baseline and the phase
    capture emit ``%Y-%m-%dT%H:%M:%SZ``. Normalized here rather than at the
    builders, which would rewrite records the store already wrote once.

    **The record's own clock, never one stamped at read time.** A read clock
    would give one record a different timestamp on every scan. A record
    carrying no ``captured_at`` — the engine's ``_record()`` does not — reads
    ``None`` rather than a guess.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
    except (OverflowError, ValueError, OSError):
        # A number outside the platform's ``time_t``, or a NaN. The rescan
        # reaches it for real: ``json.loads`` accepts a bare NaN.
        return None


def _row(path: str, document: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The seven columns off one banked file, or ``None`` if it is not a take.

    Read off the file's own shape — the enveloped payload, not the record
    handed to ``bank`` — so a selection reads the bytes that were banked.
    """
    if document.get("kind") != POSITION_EVIDENCE_KIND:
        return None
    return (
        path,
        _text(document.get("session_id")),
        _text(document.get(MEASURE_KIND_KEY)),
        _text(document.get("phase")),
        _position_deg(document.get("position_deg")),
        _text(document.get("candidate_id")),
        _captured_at(document.get("captured_at")),
    )


def _insert(connection: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    """``REPLACE`` because re-banking identical bytes is idempotent."""
    connection.executemany(
        f"INSERT OR REPLACE INTO measurements ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_COLUMNS))})",
        rows,
    )


def _load(take: Path) -> Mapping[str, Any]:
    """One banked file's JSON, or empty when it is not readable — a file a
    rescan cannot parse is one it has nothing to select, not a reason to stop.
    """
    try:
        document = json.loads(take.read_text())
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _scan(artifacts_dir: Path) -> list[tuple[Any, ...]]:
    """Every banked take under ``artifacts_dir``, as rows."""
    rows = []
    for take in sorted(Path(artifacts_dir).glob(BANKED_TAKE_GLOB)):
        row = _row(take.relative_to(artifacts_dir).as_posix(), _load(take))
        if row is not None:
            rows.append(row)
    return rows


def _select(
    connection: sqlite3.Connection,
    *,
    kind: str | None,
    phase: str | None,
    position_deg: int | None,
) -> tuple[Measurement, ...]:
    filters = {"kind": kind, "phase": phase, "position_deg": position_deg}
    named = [(name, value) for name, value in filters.items() if value is not None]
    where = "".join(f" AND {name} = ?" for name, _ in named)
    rows = connection.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM measurements "
        f"WHERE 1{where} ORDER BY path",
        [value for _, value in named],
    ).fetchall()
    return tuple(Measurement(*row) for row in rows)


def bundle_measurements(
    bundle_dir: Path,
    *,
    kind: str | None = None,
    phase: str | None = None,
    position_deg: int | None = None,
) -> tuple[Measurement, ...]:
    """One bundle's takes, matching every filter — the offline reader's door.

    **The take files ARE the index.** Every read rescans them into an in-memory
    table and selects over that, so there is no file to go stale, none to
    write, and none to reconcile. It costs nothing these callers were not
    already paying: each of them opens every selected take anyway.

    Three filter axes and not seven: these are the three the banked corpus is
    actually asked for — ``position_cycle``'s ``takes_by_position`` and its
    kind listing, and the phase every take reader selects on
    (:func:`~.position_cycle.read_lateral_take` and its entry-baseline
    sibling). Every column is on :class:`Measurement`, so a reader that needs
    to select by another one adds the axis when it exists rather than before.

    ``phase`` is what a take IS — the walk pose, the entry baseline, a CHECK —
    where ``kind`` is what it MEASURES (baseline / candidate / verify). Two
    questions, two columns, exactly as ``contracts.MEASURE_KIND_KEY`` says.

    Ordered by ``path`` and not by ``captured_at``: the timestamp is the
    record's own and can be absent, where the path is the take's key.

    The rows SELECT; the take files still DECIDE. Every caller re-reads the
    file it was pointed at through its own accept rule.
    """
    artifacts = Path(bundle_dir) / EVIDENCE_ROOT / "artifacts"
    with _connect(":memory:") as connection:
        _insert(connection, _scan(artifacts))
        return _select(
            connection, kind=kind, phase=phase, position_deg=position_deg,
        )
