# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""N candidates at one pose, ranked by their own delta-probe gradings (#3498 WP4).

The candidate cycle plays several configurations at ONE held pose and grades
each against what it commanded
(:func:`~jasper.active_speaker.delta_probe.classify_delta_probe`).  This module
answers the one question that then remains — *of the maps that graded, which
realized its own commanded shape best?* — and nothing else:

* It measures nothing.  Every number it reports came off a map.
* It adopts nothing.  Whether the winner is KEPT is
  :func:`~.verification.decide_adoption`'s question over its four axes; a rank
  is not a permission, and a top two sitting inside the measurement's own
  repeat floor is a tie this instrument cannot break.
* It owns no verdict vocabulary: the exclusion set is
  :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS` and
  :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_UNGRADED_VERDICTS`,
  imported rather than restated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ..delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    DELTA_PROBE_UNGRADED_VERDICTS,
    DeltaProbeMap,
)

__all__ = [
    "COMPARISON_REASONS",
    "REASON_INSIDE_REPEAT_FLOOR",
    "REASON_NO_SURVIVOR",
    "REASON_REPEAT_FLOOR_UNKNOWN",
    "REASON_SEPARATED",
    "REASON_SINGLE_CANDIDATE",
    "CandidateComparison",
    "CandidateRank",
    "compare_candidates",
]

#: The top two are further apart than the repeat floor: a real ordering.
REASON_SEPARATED = "separated"
#: They are not, so the winner is named without a claim that it is better.
REASON_INSIDE_REPEAT_FLOOR = "inside_repeat_floor"
#: No usable floor was supplied, so "far enough" is unknown — the winner is
#: still named and the separation claim withheld rather than assumed.
REASON_REPEAT_FLOOR_UNKNOWN = "repeat_floor_unknown"
#: Fewer than two candidates survived, so there is no comparison behind the
#: winner.
REASON_SINGLE_CANDIDATE = "single_candidate"
#: Every graded map was excluded, so there is no winner to name.
REASON_NO_SURVIVOR = "no_candidate_survived"

#: The closed vocabulary of :attr:`CandidateComparison.reason`.
COMPARISON_REASONS: frozenset[str] = frozenset({
    REASON_SEPARATED,
    REASON_INSIDE_REPEAT_FLOOR,
    REASON_REPEAT_FLOOR_UNKNOWN,
    REASON_SINGLE_CANDIDATE,
    REASON_NO_SURVIVOR,
})

_EXCLUDED_VERDICTS: frozenset[str] = (
    DELTA_PROBE_ROLLBACK_VERDICTS | DELTA_PROBE_UNGRADED_VERDICTS
)


@dataclass(frozen=True)
class CandidateRank:
    """One candidate's grade, read off its own map and nothing else."""

    candidate_id: str
    verdict: str
    max_error_db: float
    rms_error_db: float
    #: The number this candidate was actually ORDERED on: the map's
    #: frame-removed RMS where it carries one, and ``rms_error_db`` where it
    #: does not. The frame is the offset and tilt the classifier fitted where
    #: the correction commanded nothing (:attr:`DeltaProbeMap.frame`), so
    #: ranking on the raw RMS would order candidates partly on a shift the
    #: classifier itself declared uncommanded.
    ranked_rms_db: float
    gain_factor: float | None
    exceedance_octaves: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateComparison:
    """The ranked field, its winner, and how much to trust the ordering.

    ``ranked`` carries EVERY graded candidate — survivors in rank order first,
    then the excluded ones by id — so a reader sees the field that played, not
    only the part of it that survived.
    """

    ranked: tuple[CandidateRank, ...]
    winner: str
    reason: str
    separated: bool
    #: How many poses the gradings behind this comparison were measured at. A
    #: rank taken at one pose is a one-pose claim, and this is what says so.
    pose_count: int

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ranked": [rank.to_dict() for rank in self.ranked]}


def _rank(candidate_id: str, probe: DeltaProbeMap) -> CandidateRank:
    frame_removed = probe.frame_removed_rms_db
    return CandidateRank(
        candidate_id=candidate_id,
        verdict=probe.verdict,
        max_error_db=probe.max_error_db,
        rms_error_db=probe.rms_error_db,
        # Keyed on the FIELD and not on a verdict: a map either measured a
        # frame or it did not, and that is the whole question here.
        ranked_rms_db=(
            probe.rms_error_db if frame_removed is None else frame_removed
        ),
        gain_factor=probe.gain_factor,
        exceedance_octaves=probe.exceedance_octaves,
    )


def compare_candidates(
    gradings: Mapping[str, DeltaProbeMap],
    *,
    pose_count: int,
    repeat_floor_db: float | None = None,
) -> CandidateComparison:
    """Rank ``gradings`` by realized-vs-commanded RMS error, ascending.

    Args:
      gradings: one graded map per candidate id.
      pose_count: how many poses those maps were measured at — disclosed, not
        checked: this module ranks what it is handed.
      repeat_floor_db: how far apart two candidates must sit to be ordered at
        all, in the SAME units as the number ranked on — RMS dB over the probe
        band. Deriving one from a repeat study is the caller's, not this
        module's: ``None`` or a non-positive value is "no usable floor", which
        withholds the separation claim instead of substituting a threshold.

    ``winner`` is ``""`` only when nothing survived exclusion. Fewer than two
    SURVIVORS — which includes fewer than two gradings — names the survivor and
    reports :data:`REASON_SINGLE_CANDIDATE`.
    """
    ranked = [_rank(cid, probe) for cid, probe in sorted(gradings.items())]
    survivors = sorted(
        (rank for rank in ranked if rank.verdict not in _EXCLUDED_VERDICTS),
        key=lambda rank: (rank.ranked_rms_db, rank.candidate_id),
    )
    excluded = tuple(rank for rank in ranked if rank.verdict in _EXCLUDED_VERDICTS)
    if not survivors:
        return CandidateComparison(
            ranked=excluded, winner="", reason=REASON_NO_SURVIVOR,
            separated=False, pose_count=pose_count,
        )
    if len(survivors) < 2:
        reason, separated = REASON_SINGLE_CANDIDATE, False
    elif repeat_floor_db is None or repeat_floor_db <= 0.0:
        reason, separated = REASON_REPEAT_FLOOR_UNKNOWN, False
    else:
        margin = survivors[1].ranked_rms_db - survivors[0].ranked_rms_db
        # Strictly greater: a margin exactly ON the floor is the floor, not an
        # ordering the instrument resolved.
        separated = margin > repeat_floor_db
        reason = REASON_SEPARATED if separated else REASON_INSIDE_REPEAT_FLOOR
    return CandidateComparison(
        ranked=tuple(survivors) + excluded,
        winner=survivors[0].candidate_id,
        reason=reason,
        separated=separated,
        pose_count=pose_count,
    )
