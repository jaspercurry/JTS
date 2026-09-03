# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Selecting banked takes: eight columns, rescanned from the files each read.

No index file exists (ADR-0198): every read rescans the banked takes and filters
them in Python, so the take files are the single source of truth at the read
side as well as the write side.
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


def _captured_at(value: Any) -> str | None:
    """The take's own capture time as ISO-8601 UTC, or ``None``.

    The builders disagree about the type: the cloud position emits a Unix epoch
    ``float`` where the lateral pose, the entry baseline and the phase capture
    emit ``%Y-%m-%dT%H:%M:%SZ``.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
    except (OverflowError, ValueError, OSError):
        # ``json.loads`` accepts a bare NaN, and a number outside the
        # platform's ``time_t`` lands here too.
        return None


def _row(path: str, document: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The eight columns off one banked file, or ``None`` if it is not a take."""
    if document.get("kind") != POSITION_EVIDENCE_KIND:
        return None
    return (
        path,
        _text(document.get("session_id")),
        _text(document.get(MEASURE_KIND_KEY)),
        _text(document.get("phase")),
        _position_deg(document.get("position_deg")),
        # A pose is always at SOME height: absent or malformed reads as 0.
        _position_deg(document.get("vertical_deg")) or 0,
        _text(document.get("candidate_id")),
        _captured_at(document.get("captured_at")),
    )


def _load(take: Path) -> Mapping[str, Any]:
    """One banked file's JSON, or empty when it is not readable."""
    try:
        document = json.loads(take.read_text())
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _scan(artifacts_dir: Path) -> list[tuple[Any, ...]]:
    """Every banked take under ``artifacts_dir``, as rows sorted by path.

    Sorted on the relative path STRING, not ``Path`` object order: the two
    disagree when one session directory's name is a ``.``/``-``-extended prefix
    of another's.
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

    ``phase`` is what a take IS (the walk pose, the entry baseline, a CHECK);
    ``kind`` is what it MEASURES (baseline / candidate / verify).
    A pose is a bearing AND a height, so a caller naming only ``position_deg``
    is handed raised seats too; every axis is ``None``-means-no-filter, and it
    is the pose readers above this that pin the height they mean. The rows
    select; the take files still decide — every caller re-reads the file it was
    pointed at through its own accept rule.
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
