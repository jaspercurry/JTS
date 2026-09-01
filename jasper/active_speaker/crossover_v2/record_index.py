# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Selecting banked takes: eight columns, rescanned from the files each read.

A campaign is dozens of takes across positions and candidates, and without this
the only way to find one is to glob a directory and parse every hit by hand.

**No index file exists** (ADR-0198). Every read rescans the banked takes and
filters them in Python, so there is nothing to write, nothing to go stale,
and nothing to reconcile — the take files are the single source of truth at
the read side as well as the write side. The rescan costs nothing these
callers were not already paying: each opens every selected take anyway.

Only measurement takes get a row: five of the store's six artifact kinds are
group payloads, a candidate, a receipt and a finding set.
"""

from __future__ import annotations

import json
import time
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


@dataclass(frozen=True)
class Measurement:
    """One selected take. ``path`` is the id ``bank`` returned for it."""

    path: str
    session_id: str
    kind: str
    phase: str
    position_deg: int | None
    vertical_deg: int
    candidate_id: str
    captured_at: str | None


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


def _vertical_deg(value: Any) -> int:
    """The signed whole-degree elevation above mark height, 0 where absent.

    Unlike :func:`_position_deg` this has no ``None``: a pose is always at SOME
    height, and a record banked before the key existed was taken at the mark.
    ``bool`` is excluded for the same reason it is there.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
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
    """The eight columns off one banked file, or ``None`` if it is not a take.

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
        _vertical_deg(document.get("vertical_deg")),
        _text(document.get("candidate_id")),
        _captured_at(document.get("captured_at")),
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
    """Every banked take under ``artifacts_dir``, as rows sorted by path.

    Sorted on the relative path STRING (``r[0]``) after every row is built,
    not on ``Path`` object order: ``BANKED_TAKE_GLOB`` has a wildcard
    directory segment, and when one session directory's name is a
    ``.``/``-``-extended prefix of another's, ``Path`` comparison (component-
    by-component) disagrees with plain string order on which sorts first.
    """
    rows = []
    for take in Path(artifacts_dir).glob(BANKED_TAKE_GLOB):
        row = _row(take.relative_to(artifacts_dir).as_posix(), _load(take))
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: r[0])
    return rows


def bundle_measurements(
    bundle_dir: Path,
    *,
    kind: str | None = None,
    phase: str | None = None,
    position_deg: int | None = None,
    vertical_deg: int | None = None,
    candidate_id: str | None = None,
) -> tuple[Measurement, ...]:
    """One bundle's takes, matching every filter — the offline reader's door.

    **The take files ARE the index.** Every read rescans them and filters in
    Python, so there is no file to go stale, none to write, and none to
    reconcile. It costs nothing these callers were not already paying: each
    of them opens every selected take anyway.

    Five filter axes and not eight: these are the ones the banked corpus is
    actually asked for — ``position_cycle``'s ``takes_by_position`` and its
    kind listing, the phase every take reader selects on
    (:func:`~.position_cycle.read_lateral_take` and its entry-baseline
    sibling), and ``candidate_id``, which arrived with the ``jasper-measure``
    door: that door refuses to bank a variant take without one precisely so the
    variants can be selected apart afterwards, and a label nothing can select
    by would be decoration. Every column is on :class:`Measurement`, so a
    reader that needs to select by another one adds the axis when it exists
    rather than before.

    ``phase`` is what a take IS — the walk pose, the entry baseline, a CHECK —
    where ``kind`` is what it MEASURES (baseline / candidate / verify). Two
    questions, two columns, exactly as ``contracts.MEASURE_KIND_KEY`` says.

    ``position_deg`` and ``vertical_deg`` are ONE key, not two axes that happen
    to sit beside each other: a pose is a bearing AND a height, so a selector
    naming only the bearing would hand a raised seat to a caller asking for the
    design axis. Both stay ``None``-means-no-filter here like every other
    axis; it is the pose readers above this that pin the height they mean.

    Ordered by ``path`` and not by ``captured_at``: the timestamp is the
    record's own and can be absent, where the path is the take's key —
    :func:`_scan`'s sort key IS that path string, not ``Path`` object order,
    which can disagree with it.

    The rows SELECT; the take files still DECIDE. Every caller re-reads the
    file it was pointed at through its own accept rule.
    """
    artifacts = Path(bundle_dir) / EVIDENCE_ROOT / "artifacts"
    rows = (Measurement(*columns) for columns in _scan(artifacts))
    return tuple(
        row for row in rows
        if (kind is None or row.kind == kind)
        and (phase is None or row.phase == phase)
        and (position_deg is None or row.position_deg == position_deg)
        and (vertical_deg is None or row.vertical_deg == vertical_deg)
        and (candidate_id is None or row.candidate_id == candidate_id)
    )
