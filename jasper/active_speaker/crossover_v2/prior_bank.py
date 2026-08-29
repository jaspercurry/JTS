# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A previous session's bank, read back — the "before" a candidate check needs.

Wave 2's first decision, which :mod:`.session_seams` deferred to it in as many
words: *"rebuilding a session over a previous bank is wave 2's first decision …
what ships here is the read door an offline ``analyze`` needs; who constructs
the session around an existing bank, and how it re-learns what that bank
already discloses, is designed there."*

**The shape, in one sentence: a prior bank is ``save``'s inverse.**
:meth:`~.session.TuningSession.save` writes five keys — session id, declared
level, graph fingerprint, banked record ids, disclosures — and this reads
exactly those five back, plus the one thing derived from them that a
candidate check needs: **which banked record is the "before" AT THIS POSE.**
Per pose and never per bank — see :class:`CapturePose`.

**Why a session cannot just be re-opened.** A tuning round crosses a process
boundary and an irreversible act: stage 1 measures the entry baseline
immediately before the household applies, the apply/rollback transaction runs
(§3's one *"not a target. Ever."*), and stage 2 measures again and grades. The
two sides are two sessions on two graphs, and the second one's honest
relationship to the first is *"I read its bank"* — never *"I am it."* That is
why :class:`~.session.TuningSession` refuses to re-open and why this is a
separate, read-only type rather than a constructor flag.

**Read-only, and that is the whole safety property.** Nothing here writes, and
the bank it reads is not this session's to bank into. The defect this replaces
is the opposite shape: today the only durable copy of the entry-baseline curve
lives in ``/var/lib/jasper/active_speaker_crossover_v2_state.json``, whose
``verify_priors`` dict is *rebuilt from the conductor on every persist*, so a
stage-2 write destroys the "before" unless the host remembers to seed it back
in (fragment ``02``'s duplication #2).

**Measurement management is NOT here** (§0's non-goals). Listing sessions,
browsing them, deleting them, and deciding what a bank's retention is are
explicitly future scope. This finds one prior bank by the id its ``save``
returned, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from .contracts import MEASURE_KIND_BASELINE
from .measure_spec import CapabilityStub, stub_for_code

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session_seams import RecordStore

__all__ = ["CapturePose", "PriorBank", "pose_of"]


@dataclass(frozen=True)
class CapturePose:
    """WHERE a capture was taken and at WHAT stimulus level.

    The banked facts that decide whether two captures are of the same thing, so
    a before and an after can be paired. Not a place on its own: a level ladder
    measures one bearing at several stimulus levels, and pairing across rungs
    would difference a quiet capture against a loud one.

    A value rather than four arguments because the match IS an equality — a
    pose either is this one or is not — and four hand-compared fields is where
    a fifth one gets forgotten.

    ``vertical_deg`` is part of the equality for the same reason the bearing
    is: a take from 22° above mark height is not a re-measure of the one taken
    at mark height, and pairing them would hand a verdict a comparand measured
    somewhere else. It defaults to 0 so a pose stated without one still names
    mark height, which is where every capture banked before the field existed
    was taken.
    """

    position_axis: str
    position_deg: int | None
    stimulus_dbfs: float | None
    vertical_deg: int = 0


@dataclass(frozen=True)
class PriorBank:
    """One previous session's banked evidence, read back as a value.

    Constructed by :meth:`read` and never by hand: the fields are what a
    ``save`` wrote, so minting one from literals would assert a bank exists
    that does not. Nothing here holds the store — the whole bank is read in one
    pass at construction, so a bank cannot answer differently twice.

    ``baselines`` maps each :class:`CapturePose` the prior measured a baseline
    at to that baseline's record id, and :meth:`baseline_for` is the only way
    in. **Per POSE, never per bank:** a prior that walked three poses in one
    ``measure`` call banked three "befores", and a single bank-wide answer would
    stamp the last pose's baseline onto a capture taken somewhere else — a
    verdict differencing +30° against −30° and calling the difference the
    correction. The same holds across a level ladder's rungs.

    Later wins when one pose was measured twice: a retake supersedes the attempt
    it followed, and the baseline nearest the act being graded is the one
    *"immediately before apply"* means.

    ``measurement_level_db`` is carried because ruling S8's recipe turns on it —
    *"same drive voltage across every per-driver measurement, no gain touched
    between them"* — so a before and an after taken at different declared levels
    are not comparable. ``graph_fingerprint`` is provenance, and deliberately
    NOT a comparability field: the two sides of a round are measured through
    different graphs, because applying one is the act being graded. This type
    reports both; deciding what to do about a level that differs is ``analyze``'s.
    """

    state_id: str
    session_id: str
    measurement_level_db: float | None
    graph_fingerprint: str
    record_ids: tuple[str, ...]
    baselines: Mapping[CapturePose, str]
    disclosures: tuple[CapabilityStub, ...]

    @classmethod
    async def read(cls, store: "RecordStore", state_id: str) -> "PriorBank | None":
        """The bank one ``save`` left behind, or ``None`` when there is none.

        ``None`` rather than a raise, for the reason
        :meth:`~.session_seams.RecordStore.read` gives: a missing prior is a
        fact a session discloses, not an exception that strands a household
        whose only remaining move is the round being refused (ruling S10).

        Every banked record is read once, here, so the pose index is one
        consistent reading of one bank and a walk costs no store reads per
        capture.
        """
        state = await store.read_state(state_id)
        if state is None:
            return None
        record_ids = _texts(state.get("record_ids"))
        return cls(
            state_id=state_id,
            session_id=str(state.get("session_id") or ""),
            measurement_level_db=_number(state.get("measurement_level_db")),
            graph_fingerprint=str(state.get("graph_fingerprint") or ""),
            record_ids=record_ids,
            baselines=await _baselines_by_pose(store, record_ids),
            disclosures=_disclosures(state.get("disclosures")),
        )

    def baseline_for(self, pose: CapturePose) -> str:
        """The id of the "before" taken at THIS pose, or ``""`` for none.

        ``""`` is an honest answer and never a refusal: a prior that measured
        two of this session's three poses leaves the third capture saying it has
        no comparand, which is what it has.
        """
        return self.baselines.get(pose, "")


async def _baselines_by_pose(
    store: "RecordStore", record_ids: tuple[str, ...],
) -> Mapping[CapturePose, str]:
    """Each pose the prior baselined, against the LAST id that baselined it."""
    found: dict[CapturePose, str] = {}
    for record_id in record_ids:
        record = await store.read(record_id)
        if record is None or record.get("kind") != MEASURE_KIND_BASELINE:
            continue
        found[pose_of(record)] = record_id
    return found


def pose_of(record: Mapping[str, Any]) -> CapturePose:
    """One banked record's pose, read off the fields it already carries.

    ``vertical_deg`` reads 0 for a record banked before the field existed, and
    that is a recovery rather than a guess: every capture this bank could hold
    from before it was taken at mark height, because nothing could state any
    other elevation.

    **Whole degrees only, the same value ``PositionGeometry`` will accept.** A
    float is not truncated into a neighbouring pose — it is not an elevation
    this tree can have written, so it reads as "no elevation stated" exactly as
    a missing key does. ``bool`` is rejected before ``int`` because it
    subclasses it, so a hand-edited ``true`` cannot read back as 1° up.
    """
    degrees = record.get("position_deg")
    level = record.get("stimulus_dbfs")
    elevation = record.get("vertical_deg")
    return CapturePose(
        position_axis=str(record.get("position_axis") or ""),
        position_deg=None if degrees is None else int(degrees),
        stimulus_dbfs=None if level is None else float(level),
        vertical_deg=(
            elevation
            if isinstance(elevation, int) and not isinstance(elevation, bool)
            else 0
        ),
    )


def _texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _disclosures(value: Any) -> tuple[CapabilityStub, ...]:
    """Re-render what the bank disclosed, dropping anything unrecognisable.

    A code this build has no stub for is a hole a LATER build named and this
    one cannot describe; rendering it as an unnamed stub would put a sentence
    in front of a person that says nothing. Dropping it keeps every disclosure
    this session reports one the reader can act on, and the bank still carries
    the raw code for whoever wrote it.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    stubs: list[CapabilityStub] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        stub = stub_for_code(
            str(entry.get("code") or ""), captured=bool(entry.get("captured")),
        )
        if stub is not None:
            stubs.append(stub)
    return tuple(stubs)
