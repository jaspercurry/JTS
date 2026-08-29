# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The little measurement database: seven columns over the banked take files.

One table, one writer — a campaign is dozens of takes across positions and
candidates, and without this the only way to find one is to glob a directory.

**An INDEX over files, never a second store.** The banked records stay the
single source of truth, every column is derivable from them, and
:func:`rebuild` reproduces the table by rescanning. Losing this file loses
zero information, so nothing that decides anything may consult it.

**It lives at the bundle ROOT, outside ``evidence/v1``.** That subtree is
byte-budgeted by ``CommissioningEvidenceStore._authoritative_total``, which
counts every file in it against the session limit and refuses anything that is
not a regular file — neither accounting fits a mutable database and its
journal sidecars.

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
    "INDEX_FILENAME",
    "Measurement",
    "bundle_measurements",
    "find_measurements",
    "index_path",
    "rebuild",
    "record_measurement",
]

#: ``round_views`` is the one reader that walks the bundle root, and it takes
#: directories only — so a file here is invisible to it.
INDEX_FILENAME = "measurements.sqlite3"

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
    """One indexed take. ``path`` is the id ``bank`` returned for it."""

    path: str
    session_id: str
    kind: str
    phase: str
    position_deg: int | None
    candidate_id: str
    captured_at: str | None


def index_path(bundle_dir: Path) -> Path:
    """Where this bundle's index lives."""
    return Path(bundle_dir) / INDEX_FILENAME


@contextmanager
def _connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """One connection per operation — the store calls this from a worker
    thread, and a few dozen writes per campaign buy nothing from a held one.
    """
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

    ``bool`` is an ``int`` subclass, so it is excluded rather than indexed as
    a bearing of 0 or 1.
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

    **The record's own clock, never one stamped at index time.** Re-banking
    identical bytes is idempotent, so an index clock would give one record two
    timestamps and leave :func:`rebuild` unable to reproduce the table it
    replaced. A record carrying no ``captured_at`` — the engine's ``_record()``
    does not — indexes ``None`` rather than a guess.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
    except (OverflowError, ValueError, OSError):
        # A number outside the platform's ``time_t``, or a NaN. Belt for the
        # bank path and unreachable from today's producers there — every clock
        # reaching it originates in a local ``time.time()``, and the store
        # refuses a non-finite float before the write — but kept because an
        # uncaught raise here would cost a bank that had already succeeded.
        # The rescan reaches it for real: ``json.loads`` accepts a bare NaN.
        return None


def _row(path: str, document: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The seven columns off one banked file, or ``None`` if it is not a take.

    Read off the file's own shape — the enveloped payload, not the record
    handed to ``bank`` — so indexing at bank time and indexing by rescan read
    the same bytes and cannot disagree.
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
    rescan cannot parse is one it has nothing to index, not a reason to stop.
    """
    try:
        document = json.loads(take.read_text())
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def record_measurement(
    db_path: Path, path: str, document: Mapping[str, Any],
) -> bool:
    """Index one banked file; return whether it was a measurement take."""
    row = _row(path, document)
    if row is None:
        return False
    with _connect(db_path) as connection:
        _insert(connection, [row])
    return True


def _scan(artifacts_dir: Path) -> list[tuple[Any, ...]]:
    """Every banked take under ``artifacts_dir``, as rows."""
    rows = []
    for take in sorted(Path(artifacts_dir).glob(BANKED_TAKE_GLOB)):
        row = _row(take.relative_to(artifacts_dir).as_posix(), _load(take))
        if row is not None:
            rows.append(row)
    return rows


def rebuild(db_path: Path, artifacts_dir: Path) -> int:
    """Rescan every banked take under ``artifacts_dir``; return the row count.

    The table is replaced, so a rebuild reproduces the index rather than
    merging into a stale one.
    """
    rows = _scan(artifacts_dir)
    try:
        _write_all(db_path, rows)
    except sqlite3.DatabaseError:
        # Bytes that are not a database, or a table whose columns predate the
        # ones being written — `CREATE TABLE IF NOT EXISTS` leaves an existing
        # table alone, so a file written by an older build refuses the insert.
        # Replacing the file IS the recovery for both: rebuilding is exactly
        # the operation that can afford to lose it.
        Path(db_path).unlink(missing_ok=True)
        _write_all(db_path, rows)
    return len(rows)


def _write_all(db_path: Path, rows: list[tuple[Any, ...]]) -> None:
    with _connect(db_path) as connection:
        connection.execute("DELETE FROM measurements")
        _insert(connection, rows)


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


def find_measurements(
    db_path: Path,
    *,
    kind: str | None = None,
    phase: str | None = None,
    position_deg: int | None = None,
) -> tuple[Measurement, ...]:
    """The takes matching every filter given, ordered by ``path``.

    Three filter axes and not seven: these are the three the banked corpus is
    actually asked for — ``position_cycle``'s ``takes_by_position`` and its
    kind listing, and the phase every take reader selects on
    (:func:`~.position_cycle.read_lateral_take` and its entry-baseline
    sibling). Every column is on :class:`Measurement`, so a reader that needs
    to select by another one adds the axis when it exists rather than before.

    ``phase`` is what a take IS — the walk pose, the entry baseline, a CHECK —
    where ``kind`` is what it MEASURES (baseline / candidate / verify). Two
    questions, two columns, exactly as ``contracts.MEASURE_KIND_KEY`` says.

    Ordered by path and not by ``captured_at``: the timestamp is the record's
    own and can be absent, where the path is this table's key.
    """
    with _connect(db_path) as connection:
        return _select(
            connection, kind=kind, phase=phase, position_deg=position_deg,
        )


def bundle_measurements(
    bundle_dir: Path,
    *,
    kind: str | None = None,
    phase: str | None = None,
    position_deg: int | None = None,
) -> tuple[Measurement, ...]:
    """One bundle's takes, matching every filter — the offline reader's door.

    **Reads the index; never writes it.** Every caller here is an offline
    reader over a banked corpus, and ``jasper-crossover-prescriber status`` is
    pinned to leave that corpus byte-identical, so a read that rebuilt the
    table would be a reader with a side effect.

    **And never trusts it either.** The store's own write into the index is
    contained (``BankedRecordStore._index`` logs and continues, because a bank
    that succeeded must not report failure), and a table an older build wrote
    can be missing a column this query names — so an index that does not
    account for every take file on disk is skipped and the corpus rescanned
    into memory. Same rows, same SQL, no file. That is what keeps *losing this
    file loses nothing* true at the READ side as well as the write side, and
    it costs nothing these callers were not already paying: each of them opens
    every selected take anyway.

    The rows SELECT; the take files still DECIDE. Every caller re-reads the
    file it was pointed at through its own accept rule, so the index can widen
    a reader's candidate set but can never admit a record the file rejects.
    """
    bundle_dir = Path(bundle_dir)
    artifacts = bundle_dir / EVIDENCE_ROOT / "artifacts"
    db_path = index_path(bundle_dir)
    if db_path.is_file():
        try:
            if _row_count(db_path) == _take_file_count(artifacts):
                return find_measurements(
                    db_path, kind=kind, phase=phase, position_deg=position_deg,
                )
        except sqlite3.Error:
            # A table this build cannot query at all. The rescan below is the
            # answer; the store's own next `rebuild` is what repairs the file.
            pass
    with _connect(":memory:") as connection:
        _insert(connection, _scan(artifacts))
        return _select(
            connection, kind=kind, phase=phase, position_deg=position_deg,
        )


def _row_count(db_path: Path) -> int:
    with _connect(db_path) as connection:
        return int(connection.execute(
            "SELECT count(*) FROM measurements"
        ).fetchone()[0])


def _take_file_count(artifacts_dir: Path) -> int:
    """How many take files exist, WITHOUT parsing any of them.

    The cheap half of :func:`_scan`, and the reason the check is worth making:
    a count that disagrees sends the read to the files, and a count that
    agrees is the only case where trusting the table costs nothing. A
    non-take JSON filed under ``positions/`` makes them disagree forever,
    which loses a shortcut and never an answer.
    """
    return sum(1 for _ in Path(artifacts_dir).glob(BANKED_TAKE_GLOB))
