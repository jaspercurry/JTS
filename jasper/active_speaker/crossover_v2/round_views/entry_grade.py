# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grading the state the round started from."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline
from jasper.active_speaker.flat_spec import FlatSpecReport, evaluate_flat_spec
from jasper.audio_measurement.gating import ENTANGLEMENT_SOURCE_UNKNOWN

from .banked import BankedRound

#: Why an entry state could not be graded when the take IS banked but will not
#: rehydrate. ``EntryBaseline.from_dict`` owns that rule and answers ``None`` for
#: every member of the set; it does not say which member it was, and neither
#: does this.
ENTRY_STATE_UNREADABLE = (
    "this round banked an entry_baseline take, but it does not rehydrate into "
    "a gradeable baseline — its curve, exclusion mask, or identity fields are "
    "absent, disagree in length, or are not finite"
)


@dataclass(frozen=True)
class EntryStateGrade:
    """The graph a round ENTERED on, graded — or the named reason it was not.

    A reader, not a new capture and not a second grader: the entry-baseline take
    is write-once (ruling S3's offline promise) and ``report`` is a real
    :class:`~jasper.active_speaker.flat_spec.FlatSpecReport` from the shipped
    evaluator, so it reads side by side with the round's own ``spec`` block.

    **The frame is the ROUND's, the exclusion mask is the TAKE's.** The mask is
    :func:`~.round_evidence._validity_clamp`'s output — the bins below THIS
    capture's own reflection gate — not the cloud's interference screen over a
    different capture. Grading this curve through the round's post-apply
    exclusions would report deviations at bins nobody vouched for.
    :func:`_entry_frame` owns what a round that graded no after is stated in.

    ``round_ordinal`` / ``round_ordinal_epoch`` are read from the banked flow
    state; ``None`` is "not recorded". They ride here because "the entry state
    was this flat" means one thing at round 1 of a fresh box and another at
    round 1 after a republish reset the count. ``available`` ``False`` carries a
    non-empty ``reason`` and no report, and the reverse.
    """

    available: bool
    reason: str
    program_id: str
    reference_mark: str
    graph_fingerprint: str
    captured_at: str
    artifact_ref: str
    report: FlatSpecReport | None
    round_ordinal: int | None = None
    round_ordinal_epoch: int | None = None

    @classmethod
    def unavailable(
        cls,
        reason: str,
        *,
        round_ordinal: int | None = None,
        round_ordinal_epoch: int | None = None,
    ) -> "EntryStateGrade":
        return cls(
            available=False, reason=reason, program_id="", reference_mark="",
            graph_fingerprint="", captured_at="", artifact_ref="", report=None,
            round_ordinal=round_ordinal, round_ordinal_epoch=round_ordinal_epoch,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "program_id": self.program_id,
            "reference_mark": self.reference_mark,
            # WHICH entry state was graded: a first round's entry graph is the
            # declarations-derived config a fresh box wears, a later round's is
            # whatever the previous round left, told apart by this and nothing
            # else here.
            "graph_fingerprint": self.graph_fingerprint,
            "captured_at": self.captured_at,
            "artifact_ref": self.artifact_ref,
            # WHICH round, and which epoch of the count. Round 1 of a fresh box
            # and round 1 after a reset are the same ordinal, different facts.
            "round_ordinal": self.round_ordinal,
            "round_ordinal_epoch": self.round_ordinal_epoch,
            "report": None if self.report is None else self.report.to_dict(),
        }


def _banked_series_position(state_path: Path | None) -> tuple[int | None, int | None]:
    """``(round_ordinal, round_ordinal_epoch)`` off the round's flow state.

    ``None`` for either field the record does not carry — "not recorded", never
    zero. Read here rather than off the evidence packet because the packet's
    ``round_receipt`` block publishes identities and not the ordinal. ``bool`` is
    rejected before ``int`` because it subclasses it.
    """

    def _count(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    if state_path is None or not state_path.is_file():
        return None, None
    try:
        state = json.loads(state_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(state, Mapping):
        return None, None
    receipt = state.get("round_receipt")
    ordinal = _count(receipt.get("round_ordinal")) if isinstance(receipt, Mapping) else None
    return ordinal, _count(state.get("round_ordinal_epoch"))


def _entry_frame(report: FlatSpecReport | None) -> tuple[int, dict[str, Any]]:
    """``(smoothing_fraction, frame_kwargs)`` for an entry baseline — the round's
    own when it graded one, nothing when it did not.

    The frame is the ROUND's so a before and an after are stated over one span. A
    round that banked no cloud group graded no after, so the honest frame is no
    frame: unclamped, on ``0``, this module's spelling for *not attested*. The
    emitted report echoes both clamps as ``None`` on its face, so the grade
    discloses which frame produced it. The room's floor is READ BACK rather than
    re-derived: a floor recomputed here would be a second opinion about one room.
    """
    if report is None:
        return 0, {
            "trusted_floor_hz": None,
            "trusted_ceiling_hz": None,
            "entanglement_floor_hz": None,
            "entanglement_floor_source": ENTANGLEMENT_SOURCE_UNKNOWN,
        }
    return report.smoothing_fraction, report.frame_kwargs


def entry_state_grade(banked: BankedRound) -> EntryStateGrade:
    """Grade the entry state this round measured before it applied anything.

    Reads the banked entry-baseline take out of the round's evidence packet,
    rehydrates it through :meth:`~.round_evidence.EntryBaseline.from_dict` and
    hands the arrays to the shipped
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`. It requires no
    cloud group, which is what makes it reachable: the measure stage is the only
    stage that banks an entry baseline and the only one that banks no cloud
    (#3478). ``packet["entry_baseline"]`` is indexed rather than fetched with a
    default, so a missing key lands in the ``KeyError`` arm the CLI already
    treats as an unreadable round.
    """

    ordinal, epoch = _banked_series_position(banked.inputs.state_path)
    block = banked.packet["entry_baseline"]
    if not block.get("available"):
        return EntryStateGrade.unavailable(
            str(block.get("reason") or ""),
            round_ordinal=ordinal, round_ordinal_epoch=epoch,
        )
    baseline = EntryBaseline.from_dict(block)
    if baseline is None:
        return EntryStateGrade.unavailable(
            ENTRY_STATE_UNREADABLE,
            round_ordinal=ordinal, round_ordinal_epoch=epoch,
        )
    smoothing_fraction, frame_kwargs = _entry_frame(banked.report)
    report = evaluate_flat_spec(
        np.asarray(baseline.curve.hz, dtype=float),
        np.asarray(baseline.curve.db, dtype=float),
        np.asarray(baseline.excluded, dtype=bool),
        smoothing_fraction=smoothing_fraction,
        **frame_kwargs,
    )
    return EntryStateGrade(
        available=True,
        reason="",
        program_id=baseline.program_id,
        reference_mark=baseline.reference_mark,
        graph_fingerprint=baseline.graph_fingerprint,
        captured_at=baseline.captured_at,
        artifact_ref=baseline.artifact_ref,
        report=report,
        round_ordinal=ordinal,
        round_ordinal_epoch=epoch,
    )
