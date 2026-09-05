# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Is this the room or the speaker: the mechanism findings a round banked.

* ``findings <round-dir> [--phase P]`` — the finding sets
  :mod:`jasper.attribution` banks for this round, read back through
  :func:`~jasper.attribution.storage.read_finding_set` so every citation is
  re-resolved and re-hashed, joined with the band the null detector actually
  ran on. Writes ``findings.json``. Three phases bank one, and a phase that
  never ran reads ``null`` rather than ``0``: "attribution found nothing" and
  "attribution never ran here" are different answers, which is the whole
  reason ``produced_by`` exists. A round carrying none of the three is an
  ANSWER, not a refusal — it is the one this verb exists to give.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
)
from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    RoundViewsError,
    round_inputs,
)
from jasper.active_speaker.crossover_v2.ring_projection import bundle_session_id
from jasper.attribution.mechanisms import (
    MECHANISM_BOUNDARY_SBIR,
    MECHANISM_HF_REFLECTION,
    MECHANISM_LEVEL_FRAME,
)
from jasper.attribution.storage import (
    FindingEvidenceMissing,
    FindingStorageError,
    read_finding_set,
)
from jasper.cli._refusal import EXIT_UNREADABLE, StageFailed, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    default_out,
    refused_by_name,
)

#: The three phases the wizard's attribution route banks a set for: one at
#: each cloud group's close, and the level-frame publisher's on MEASURE.
FINDING_PHASES = (PHASE_MEASURE, PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY)

#: A finding cites evidence this bundle can no longer produce, so the record
#: is not handed back — the one thing ``read_finding_set`` refuses over.
REFUSE_EVIDENCE_MISSING = "findings_evidence_missing"

#: The mechanisms ``echo_band_hz`` bounds, and the ones it does not. The only
#: producer of a reflection or boundary finding is the null identification the
#: cloud group runs over that band; the level-frame finding takes its band
#: from the level-frame record instead and is bounded by nothing here.
BAND_BOUNDS = (MECHANISM_HF_REFLECTION, MECHANISM_BOUNDARY_SBIR)

#: What that means for a reader of an EMPTY set, said once and composed from
#: the ids above so the sentence cannot outlive them.
BAND_DISCLOSURE = (
    f"{'/'.join(BAND_BOUNDS)} findings come only from nulls identified inside "
    f"echo_band_hz, so outside that band their absence is 'not looked at', "
    f"never 'no mechanism found'. {MECHANISM_LEVEL_FRAME} is not bounded by "
    f"it: its band comes from the level-frame record."
)


def _read_json(path: Path) -> Mapping[str, Any]:
    """One banked document, or ``{}`` for anything this cannot read as one."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, Mapping) else {}


def _echo_band(
    artifact_dir: Path,
) -> tuple[list[float] | None, Mapping[str, Any] | None]:
    """The band the null detector ran on, off the cloud group that banked it.

    The group's WHOLE provenance block rides back, not just its ``source``:
    the HF-regime clamp can raise the applied band's lower edge above the
    declared one (#1763), and a reader told only "declared" would take a
    narrowed band for the contract's.

    ``(None, None)`` for a round that banked no cloud group — a measure-stage
    round's ordinary shape, and the reason the band is disclosed rather than
    assumed.
    """
    for phase in (PHASE_CLOUD_VERIFY, PHASE_CLOUD_MEASURE):
        cloud = _read_json(artifact_dir / f"{phase}.json")
        band = cloud.get("echo_band_hz")
        if isinstance(band, list) and len(band) == 2:
            provenance = cloud.get("echo_band_provenance")
            return (
                [float(band[0]), float(band[1])],
                provenance if isinstance(provenance, Mapping) else None,
            )
    return None, None


def _phase_table(
    store: CommissioningEvidenceStore, capture_session_id: str, phase: str,
) -> dict[str, Any]:
    """One phase's banked set, or the row that says it never banked one.

    ``count`` is ``None`` for an absent artifact and ``0`` for a set that ran
    and found nothing; ``produced_by`` names which producer wrote it, and its
    PRESENCE is what tells those two apart.
    """
    finding_set = read_finding_set(
        store, capture_session_id=capture_session_id, phase=phase,
    )
    if finding_set is None:
        return {"phase": phase, "present": False, "produced_by": None,
                "count": None, "findings": []}
    return {
        "phase": phase,
        "present": True,
        "produced_by": finding_set.produced_by,
        "count": len(finding_set.findings),
        "findings": [finding.to_dict() for finding in finding_set.findings],
    }


def _tables(
    session_dir: Path, capture_session_id: str, phases: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Every requested phase, read through the store that wrote them."""
    store = CommissioningEvidenceStore.open(
        session_dir, expected_session_id=bundle_session_id(session_dir),
    )
    return [_phase_table(store, capture_session_id, phase) for phase in phases]


def _cmd_findings(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    artifact_dir, why = round_artifact_dir(inputs.session_dir)
    if artifact_dir is None:
        raise StageFailed(EXIT_UNREADABLE, RoundViewsError(why))
    phases = (args.phase,) if args.phase else FINDING_PHASES
    try:
        tables = stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _tables,
            inputs.session_dir, artifact_dir.name, phases,
        )
    except FindingEvidenceMissing as exc:
        return refused_by_name(REFUSE_EVIDENCE_MISSING, str(exc))
    except (FindingStorageError, CommissioningEvidenceStoreError) as exc:
        # A store that will not open and a set that cannot be trusted as read
        # are one instruction — look at the bundle — and neither is a view
        # declining a round it read. Both are ``RuntimeError`` subclasses, so
        # no stage claims them without this.
        raise StageFailed(EXIT_UNREADABLE, exc) from exc
    band_hz, band_provenance = _echo_band(artifact_dir)
    summary = {
        "round_dir": str(round_dir),
        "banked": inputs.banked,
        "capture_session_id": artifact_dir.name,
        # ``null`` is never-ran and ``0`` is ran-and-found-nothing.
        "phases": {row["phase"]: row["count"] for row in tables},
        "findings": sum(row["count"] or 0 for row in tables),
        "mechanisms": sorted({
            str(finding["mechanism"])
            for row in tables for finding in row["findings"]
        }),
        "echo_band_hz": band_hz,
        "echo_band_provenance": band_provenance,
        "echo_band_bounds_mechanisms": list(BAND_BOUNDS),
        "echo_band_disclosure": BAND_DISCLOSURE,
    }
    written = _write(
        {"summary": summary, "tables": tables}, args.out,
        default_out(inputs, round_dir, ARTIFACT_BY_VIEW[args.command].artifact),
    )
    source = (band_provenance or {}).get("source") or "source not recorded"
    band = (
        "NOT RECORDED (this round banked no cloud group)" if band_hz is None
        else f"{band_hz[0]:g}-{band_hz[1]:g} Hz ({source})"
    )
    banked = [row["phase"] for row in tables if row["present"]]
    return answer(
        args.command, out=written, **summary,
        line=(
            f"findings: {summary['findings']} finding(s) from "
            f"{len(banked)}/{len(tables)} phase(s) banked "
            f"({', '.join(banked) or 'none'}); echo band {band} bounds "
            f"{'/'.join(BAND_BOUNDS)} only"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    findings = sub.add_parser(
        "findings",
        help="the mechanism findings this round banked, and the band that bounds them",
    )
    findings.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    findings.add_argument(
        "--phase", default=None, choices=FINDING_PHASES,
        help="read only this phase's set (default: all three). The artifact "
             "then covers that phase alone, at the same path — name --out to "
             "keep an all-phases one",
    )
    findings.add_argument("--out", default=None, help="write the result here (- for stdout)")
    findings.set_defaults(func=_cmd_findings)
