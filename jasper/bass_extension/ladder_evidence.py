# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The protection ladder's evidence: where it is banked, how it is graded, and
how the prescription door reads it back.

ONE module owns the contract both sides of the door depend on: the view
(``jasper-round-views bass-ladder``) writes ``bass_ladder/<target_id>.json``
beside the fit it protects, and ``jasper-crossover-prescriber`` reads exactly
those files here. The fail rule is code-owned and its constants are the
rung's own :class:`~jasper.bass_extension.targets.MarginPolicy` — there is no
threshold to configure, and the plan's section 8 / ADR-0260 pointer carries
no numbers of its own.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.active_speaker.crossover_v2.blend_prescription import prescription_sha256
from jasper.bass_extension.candidate_field import EVIDENCE_PASS
from jasper.bass_extension.targets import MarginPolicy

__all__ = [
    "BASS_LADDER_DIRNAME",
    "EVIDENCE_FAIL",
    "STEP_COMPRESSION",
    "STEP_FLOOR_LIMITED",
    "STEP_GAP",
    "STEP_INCIDENT",
    "STEP_THD",
    "LadderStep",
    "bass_ladder_evidence",
    "grade_ladder",
    "ladder_document_name",
    "ladder_document_path",
]

#: Row 3.4's per-target protection artifacts, banked beside the fit they
#: protect. The reader's contract: an object naming the ``target_id`` it
#: measured, its ``verdict`` and the ``max_level_db`` that verdict bounds it
#: to.
BASS_LADDER_DIRNAME = "bass_ladder"

#: The verdict the door refuses on. :data:`EVIDENCE_PASS` is the only one
#: :mod:`jasper.bass_extension.candidate_field` admits at the persistence
#: boundary, so a failed ladder is published for the operator and is inert at
#: the door.
EVIDENCE_FAIL = "fail"

#: Why a step is the last one. ``gap`` is the only reason that names the step
#: BEFORE it: an unmeasured level between two banked ones is not a proven one,
#: so the ladder stops at the last step it actually walked to.
STEP_INCIDENT = "incident"
STEP_FLOOR_LIMITED = "floor_limited"
STEP_THD = "thd"
STEP_COMPRESSION = "compression"
STEP_GAP = "gap"

#: A banked ladder rung's own dBFS may sit this far past the margin's step
#: before the pair stops being consecutive. Half a dB of composer rounding,
#: not a tolerance an operator may widen.
_GAP_SLACK_DB = 0.5


@dataclass(frozen=True)
class LadderStep:
    """One banked take of the ladder, as the fail rule reads it.

    ``fundamental_db`` is the DECONVOLVED fundamental in the rung's own
    extension band — the transfer, normalised against the sweep that produced
    it, so a linear system reads the same number at every step and a
    compression shortfall IS the drop between two of them. ``thd_ratio`` is
    the root-sum-square of the CLEAN harmonic orders over that band, and
    ``clean_orders`` which orders those were — an order sitting on the
    measurement floor describes the instrument, not the driver, and is
    excluded and disclosed rather than counted as clean.

    ``incident`` is the take's own, empty on a take that banked normally.
    ``fundamental_db``/``thd_ratio`` are ``None`` on a step no clean order
    survived, which ends the ladder exactly as an incident does.
    """

    take_id: str
    stimulus_dbfs: float
    level_db: float
    fundamental_db: float | None
    thd_ratio: float | None
    clean_orders: tuple[int, ...]
    floor_limited_orders: tuple[int, ...] = ()
    incident: str = ""


def ladder_document_name(target_id: str) -> str:
    """``bass_ladder/<target_id>.json``, with the id proved a plain segment.

    The ids reach both sides of this contract from documents an operator
    wrote, so a rung may not name a path — one segment, never a traversal.
    """
    if target_id in {"", ".", ".."} or Path(target_id).name != target_id:
        raise ValueError(
            f"a ladder target id is one path segment, got {target_id!r}"
        )
    return f"{BASS_LADDER_DIRNAME}/{target_id}.json"


def ladder_document_path(fit_dir: Path, target_id: str) -> Path:
    """Where one rung's ladder evidence is banked, spelled once."""
    return Path(fit_dir) / ladder_document_name(target_id)


def bass_ladder_evidence(
    fit_dir: Path, target_ids: Sequence[str]
) -> dict[str, Mapping[str, Any]]:
    """The banked ladder evidence for the targets a document adopts.

    Only those: reading the whole directory would let a document that adopts
    one rung pay for every file beside it. The digest is computed HERE, over
    the bytes read, for the room median's reason -- the evidence a rung shows
    must name the file that was read. A file that is unreadable, is not an
    object, or measured another target is DROPPED, so one bad file cannot
    admit or refuse another target.
    """
    evidence: dict[str, Mapping[str, Any]] = {}
    for target_id in dict.fromkeys(target_ids):
        try:
            payload = ladder_document_path(fit_dir, target_id).read_bytes()
            document = json.loads(payload)
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(document, Mapping) or document.get("target_id") != target_id:
            continue
        evidence[target_id] = {**document, "sha256": prescription_sha256(payload)}
    return evidence


def _row(step: LadderStep, *, compression_db: float | None) -> dict[str, Any]:
    """One step's published row. A take's own incident rides it whenever the
    take carries one, whichever rule ended the ladder — the gap check runs
    first, and a row that dropped the incident would hide why the step is
    there at all."""
    return {
        **({"incident": step.incident} if step.incident else {}),
        "take_id": step.take_id,
        "stimulus_dbfs": step.stimulus_dbfs,
        "level_db": step.level_db,
        "fundamental_db": step.fundamental_db,
        "thd_ratio": step.thd_ratio,
        "clean_orders": list(step.clean_orders),
        "floor_limited_orders": list(step.floor_limited_orders),
        "compression_db": compression_db,
        "verdict": EVIDENCE_PASS,
    }


def _failed(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {**row, "verdict": EVIDENCE_FAIL, "reason": reason}


def grade_ladder(
    steps: Sequence[LadderStep],
    *,
    target_id: str,
    margin: MarginPolicy,
    basis: Mapping[str, Any],
) -> dict[str, Any]:
    """The ladder's document: which steps the rung proved, and to what level.

    Walked from the lowest banked level up, and the FIRST failing step ends
    it — a rung is inadmissible at and above the level where it failed, so
    nothing above a failure is read. Four ways a step ends the ladder, all
    with the rung's own margin as the constant:

    * the take carries an incident, or no clean harmonic order survived the
      measurement floor — an unproven step, never a passing one;
    * total harmonic distortion over the clean orders exceeds
      ``thd_fail_ratio``;
    * the fundamental's rise over the previous step falls short of the
      stimulus step by more than ``compression_fail_db`` — measured as the
      DROP in the deconvolved transfer, which is that shortfall exactly;
    * the stimulus step itself is larger than ``rung_step_db`` — an unmeasured
      level between two banked ones is not a proven one, so the ladder ends at
      the last step before the gap.

    ``max_level_db`` is the proven fader plus the highest passing step's peak
    dBFS: the fader at which a full-scale program reaches the level the ladder
    proved. It is at or below 0 by construction, and a positive one is refused
    rather than published — the door would take it as a boost licence.
    """
    ordered = sorted(steps, key=lambda step: step.stimulus_dbfs)
    rows: list[dict[str, Any]] = []
    passing: list[LadderStep] = []
    for step in ordered:
        previous = passing[-1] if passing else None
        compression_db: float | None = None
        if previous is not None:
            gap_db = step.stimulus_dbfs - previous.stimulus_dbfs
            if gap_db > margin.rung_step_db + _GAP_SLACK_DB:
                rows.append(_failed(_row(step, compression_db=None), STEP_GAP))
                break
            if (
                previous.fundamental_db is not None
                and step.fundamental_db is not None
            ):
                compression_db = previous.fundamental_db - step.fundamental_db
        row = _row(step, compression_db=compression_db)
        if step.incident:
            rows.append(_failed(row, STEP_INCIDENT))
            break
        if (
            not step.clean_orders
            or step.thd_ratio is None
            or step.fundamental_db is None
            or not math.isfinite(step.thd_ratio)
        ):
            rows.append(_failed(row, STEP_FLOOR_LIMITED))
            break
        if step.thd_ratio > margin.thd_fail_ratio:
            rows.append(_failed(row, STEP_THD))
            break
        if compression_db is not None and compression_db > margin.compression_fail_db:
            rows.append(_failed(row, STEP_COMPRESSION))
            break
        rows.append(row)
        passing.append(step)

    document: dict[str, Any] = {
        "target_id": target_id,
        "verdict": EVIDENCE_FAIL,
        "steps": rows,
        "margin": {
            "policy": margin.name,
            "rung_step_db": margin.rung_step_db,
            "thd_fail_ratio": margin.thd_fail_ratio,
            "compression_fail_db": margin.compression_fail_db,
        },
        "basis": dict(basis),
    }
    if not passing:
        return document
    highest = passing[-1]
    max_level_db = highest.level_db + highest.stimulus_dbfs
    if not math.isfinite(max_level_db) or max_level_db > 0.0:
        document["refused"] = "max_level_db_positive"
        return document
    document["verdict"] = EVIDENCE_PASS
    document["max_level_db"] = max_level_db
    return document
