# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass

from ..journey import PHASE_LATERAL


# --------------------------------------------------------------------------- #
# geometry-retry ceiling (#2291 Phase 5c-ii)
# --------------------------------------------------------------------------- #

# How many wider-spread RETAKES of the group's last position the
# geometry-locked check may ask for, once per group.
#
# Retakes rather than appended positions because of the PROTOCOL, not the
# physics: the capture runner completes a set at exactly ``capture_target``
# accepted captures with ``index == accepted_count + 1``, so rejecting a capture
# is the only lever that keeps a plan alive at the same index. Appending is the
# better estimator if the runner ever grows variable-length sets.
#
# Bounded on purpose: `geometry.locked` is a "spread the mic further" hint, not
# a failure, and no amount of mic movement decorrelates a source-fixed null, so
# an unbounded loop would never terminate. Two retakes, then proceed and RECORD
# the verdict.
GEOMETRY_RETRY_POSITIONS = 2


# --------------------------------------------------------------------------- #
# what a group will stand on, and when it asks for another take
# --------------------------------------------------------------------------- #


MIN_RESOLVED_CLOUD_POSITIONS = 2


def group_position_floor(phase: str) -> int:
    """How few resolved positions still lets a group stand.

    A cloud is an AVERAGE, so below :data:`MIN_RESOLVED_CLOUD_POSITIONS` there
    is nothing to combine. The lateral walk is not: its coefficients are the
    anchor's, so a pose nobody could capture costs a robustness sample and
    nothing else — floor ZERO, and the consumer discloses the shortfall.
    """
    return 0 if phase == PHASE_LATERAL else MIN_RESOLVED_CLOUD_POSITIONS


@dataclass(frozen=True)
class GeometryRetake:
    """A warranted geometry retake: which rung to show, and which take it drops.

    ``rung`` indexes the caller's prompt ladder; ``retries_after`` is the
    counter's new value. Both are computed here so the count spent and the
    sentence shown cannot drift apart.
    """

    rung: int
    retries_after: int


def geometry_retake(
    *,
    locked: bool | None,
    thin_evidence: bool | None,
    retries_used: int,
    budget: int,
    group_already_closed: bool,
    have_take_to_replace: bool,
) -> GeometryRetake | None:
    """Whether this group close asks the household to walk two more positions.

    Five conjuncts, none obvious:

    * ``locked`` — the combine could not separate the room's arrivals, the only
      condition a retake can improve.
    * ``thin_evidence`` is NOT set — it marks a verdict resting on the bare
      minimum of usable echo estimates, which the instrument already qualifies;
      a thin lock is disclosed and accepted rather than retried.
    * the budget is not spent.
    * ``group_already_closed`` is False. A voluntary retake re-enters the close
      with the group closed, and the retry branch DROPS the take at this index —
      on a voluntary retake the only copy, since the retention replaced the
      original in place.
    * ``have_take_to_replace``, which is why this returns an object rather than
      a bool: a group can close with its last position SETTLED without a curve,
      and rejecting nothing would re-open the slot whose tries just ran out.
    """
    warranted = (
        locked is True
        and thin_evidence is not True
        and retries_used < budget
        and not group_already_closed
    )
    if not (warranted and have_take_to_replace):
        return None
    return GeometryRetake(rung=retries_used, retries_after=retries_used + 1)
