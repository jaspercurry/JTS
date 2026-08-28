# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The little measurement database: six columns over the banked take files.

One table, six columns, one writer, one reader — a campaign is dozens of takes
across positions and candidates, and without this the only way to find one is
to glob a directory.

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

from .position_cycle import POSITION_EVIDENCE_KIND

__all__ = [
    "INDEX_FILENAME",
    "MEASURE_KIND_KEY",
    "Measurement",
    "find_measurements",
    "index_path",
    "rebuild",
    "record_measurement",
]

#: ``round_views`` is the one reader that walks the bundle root, and it takes
#: directories only — so a file here is invisible to it.
INDEX_FILENAME = "measurements.sqlite3"

#: Where the store files every take, relative to the artifacts root. One
#: bundle can carry more than one round, so the relay segment is a wildcard.
_TAKE_GLOB = "crossover_v2/*/positions/*.json"

#: A capture record names its MEASUREMENT kind — ``baseline`` / ``candidate``
#: / ``verify`` — where the banked file has to name its ARTIFACT kind, because
#: ``position_cycle``'s readers accept a file only when its ``kind`` is
#: :data:`~.position_cycle.POSITION_EVIDENCE_KIND` while ``PriorBank.read``
#: selects records by the measurement kind. Two questions, so two keys, and
#: the ``kind`` column here is asking the second one. Defined in this module
#: rather than in ``record_store`` because the store imports the index, so a
#: second spelling would let the writer and this reader drift apart.
MEASURE_KIND_KEY = "measure_kind"

_TABLE = """
CREATE TABLE IF NOT EXISTS measurements (
    path         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    position_deg INTEGER,
    candidate_id TEXT NOT NULL DEFAULT '',
    captured_at  TEXT
)
"""

_COLUMNS = (
    "path", "session_id", "kind", "position_deg", "candidate_id", "captured_at",
)


@dataclass(frozen=True)
class Measurement:
    """One indexed take. ``path`` is the id ``bank`` returned for it."""

    path: str
    session_id: str
    kind: str
    position_deg: int | None
    candidate_id: str
    captured_at: str | None


def index_path(bundle_dir: Path) -> Path:
    """Where this bundle's index lives."""
    return Path(bundle_dir) / INDEX_FILENAME


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
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
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _row(path: str, document: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The six columns off one banked file, or ``None`` if it is not a take.

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


def rebuild(db_path: Path, artifacts_dir: Path) -> int:
    """Rescan every banked take under ``artifacts_dir``; return the row count.

    The table is replaced, so a rebuild reproduces the index rather than
    merging into a stale one.
    """
    rows = []
    for take in sorted(Path(artifacts_dir).glob(_TAKE_GLOB)):
        row = _row(take.relative_to(artifacts_dir).as_posix(), _load(take))
        if row is not None:
            rows.append(row)
    with _connect(db_path) as connection:
        connection.execute("DELETE FROM measurements")
        _insert(connection, rows)
    return len(rows)


def find_measurements(
    db_path: Path,
    *,
    session_id: str | None = None,
    kind: str | None = None,
    position_deg: int | None = None,
    candidate_id: str | None = None,
) -> tuple[Measurement, ...]:
    """The takes matching every filter given, ordered by ``path``.

    By path and not by ``captured_at``: the timestamp is the record's own and
    can be absent, where the path is this table's key.
    """
    filters = {
        "session_id": session_id, "kind": kind,
        "position_deg": position_deg, "candidate_id": candidate_id,
    }
    named = [(name, value) for name, value in filters.items() if value is not None]
    where = "".join(f" AND {name} = ?" for name, _ in named)
    with _connect(db_path) as connection:
        rows = connection.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM measurements "
            f"WHERE 1{where} ORDER BY path",
            [value for _, value in named],
        ).fetchall()
    return tuple(Measurement(*row) for row in rows)
