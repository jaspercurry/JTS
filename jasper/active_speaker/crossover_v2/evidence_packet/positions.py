# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_reasons import unavailable

from ...commissioning_evidence_store import EVIDENCE_ROOT
from .. import position_cycle
from ..journey import PHASE_LATERAL
from ..record_index import Measurement, whole_degrees

#: Where a round banks one JSON record per accepted take, INSIDE the round
#: directory :func:`round_artifact_dir` returns:
#: :meth:`~.record_store.BankedRecordStore.bank` publishes
#: ``crossover_v2/{capture}/positions/{take_id}.json`` under
#: ``{EVIDENCE_ROOT}/artifacts/``. :mod:`.position_cycle` reaches the same
#: files from the BANKED ROUND root, which is why the accept rule is imported
#: from there rather than restated here.
POSITIONS_SUBDIR = "positions"


def _ordinal(value: Any) -> int:
    """A sort key from an identity field, or ``0`` when it is not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def banked_takes(
    session_dir: Path,
    rows: Sequence[Measurement],
    phase: str | None,
    read: Callable[[Path], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Every banked take of one ``phase``, narrowed by its own accept rule.

    ``rows`` is the bundle's measurement index, scanned ONCE per packet
    (:func:`~.record_index.bundle_measurements` rescans on every call).
    ``phase`` of ``None`` takes every take the round banked. ``read`` still
    OPENS each selected file and may reject it: the index narrows the
    candidates, the record decides.
    """
    artifacts = session_dir / EVIDENCE_ROOT / "artifacts"
    takes = [
        read(artifacts / row.path)
        for row in rows
        if phase is None or row.phase == phase
    ]
    return [take for take in takes if take is not None]


def _distinct_degrees(takes: list[Any], field: str) -> list[int]:
    """The sorted whole degrees ``field`` carries across ``takes``."""
    degrees = (whole_degrees(take.get(field)) for take in takes)
    return sorted({value for value in degrees if value is not None})


def _lateral_poses_block(
    session_dir: Path, rows: Sequence[Measurement],
) -> dict[str, Any]:
    """The signed bearings a lateral walk banked, one row per accepted take.

    Read through :func:`~.position_cycle.read_lateral_take`, the same accept
    rule :func:`~.position_cycle.position_cycle_document` uses for these files.

    ``position_deg`` is the SIGNED whole-degree bearing, negative LEFT of the
    design axis: a commanded pose recorded verbatim, not a measurement with a
    spread, so this block publishes no uncertainty.

    Both survivors and superseded takes are listed, because the speaker keeps
    both on disk deliberately.
    """
    takes = banked_takes(
        session_dir, rows, PHASE_LATERAL, position_cycle.read_lateral_take,
    )
    if not takes:
        return {
            **unavailable("source_absent", (
                f"this round banked no {PHASE_LATERAL} take records under "
                f"{POSITIONS_SUBDIR}/ — its walk was refused at take time, its "
                "poses were never accepted, or the round ran no lateral walk "
                "at all"
            )),
            "n_takes": 0,
        }
    # Coerced rather than cast: a hand-edited sidecar with a non-numeric index
    # sorts first instead of raising.
    takes.sort(key=lambda take: (_ordinal(take["index"]), _ordinal(take["attempt"])))
    return {
        "status": "available",
        "n_takes": len(takes),
        "takes": takes,
        # ``bool`` subclasses ``int``, so a ``true`` in either field would
        # otherwise publish 1 as a degree.
        "angles_deg": _distinct_degrees(takes, "position_deg"),
        "elevations_deg": _distinct_degrees(takes, "vertical_deg"),
        "source": f"{POSITIONS_SUBDIR}/<take_id>.json",
        "note": (
            "position_deg is signed whole degrees, negative LEFT of the design "
            "axis. Membership is every ACCEPTED take, a superseded attempt "
            "included — which is a different set from the conductor's live "
            "lateral_poses, where a retake replaces the attempt it supersedes "
            "and only the latest per index survives"
        ),
    }
