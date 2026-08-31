# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What one ``measure`` asks for, and what the engine admits it cannot do yet.

``docs/REFACTOR-TUNING-2026-08.md`` §1 settles the verb set (ruling S1) and §4
ruling S12 settles this module's reason for existing: **the mic-only parameter
surface ships COMPLETE from day one, with the unbuilt regimes as LOUD STUBS.**
A surface that grows one parameter per capability is the thing S12 forbids,
because a preset naming a stubbed regime today must simply start working when
the capability lands, with no API change and no caller edit.

**A preset is a saved :class:`MeasureSpec`, and nothing more.** Picking a
preset is picking parameters; adding one is writing data. A preset that would
require an engine edit is a design error, not a new preset — so this module
holds the parameter vocabulary and no preset catalogue.

**The vocabulary lives in :mod:`.contracts`, not here.** Quoting it from the
modules that own the words (``spatial``, ``driver_acoustics``,
``program_analysis``) costs ~1,100 imported modules including ``numpy`` — a
price a 1 GB Pi's always-on daemon would pay to reach five string literals, and
one this package's own ``__init__`` refuses to pay for its own numpy-heavy
modules, for exactly this reason. ``contracts`` declares them and
``tests/test_crossover_v2_engine_skeleton.py`` pins each equal to its owner's
spelling, so the cheap copy cannot drift off the real one.

**The three stubs, and which roster row lands each** (§5's instrument roster):

===========================  ==============  ============================
Parameter                    Instrument      What still happens today
===========================  ==============  ============================
``regime=near_field``        R-3             the capture is taken and banked
``level_ladder_dbfs=(…)``    R-4             every rung plays and banks
``position_axis=vertical``   R-5a            the capture is taken and banked
===========================  ==============  ============================

``polarity=inverted`` (R-1) needs no stub: the flip plays and banks through
:func:`inverted_roles_for`, and the null depth it produces is read by
:mod:`.delay_landscape`.

Impedance (R-6) gets **no** stub: it needs hardware that may never exist, and a
parameter for it would be the speculative flexibility the charter forbids.

Side-effect-free, in the register :mod:`.spatial` and :mod:`.capture_source`
established: a stub is a value returned to a caller, never a log line and never
a raise. Refusing to WORK is what ruling S10 retires; disclosing what we cannot
do is what it keeps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from jasper.audio_measurement.null_walk import MAX_DSP_DELAY_US

from .contracts import (
    DRIVER_ROLES,
    MEASURE_KINDS,
    MEASURE_REGIMES,
    POLARITIES,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
    POSITION_AXIS_HORIZONTAL,
    POSITION_AXIS_VERTICAL,
    REGIME_NEAR_FIELD,
    REGIME_REFERENCE_AXIS,
)

__all__ = [
    "DISTORTION_VS_LEVEL_NOT_IMPLEMENTED",
    "NEAR_FIELD_SPLICE_NOT_IMPLEMENTED",
    "STUB_CODES",
    "VERTICAL_AXIS_NOT_IMPLEMENTED",
    "CapabilityStub",
    "MeasureSpec",
    "inverted_roles_for",
    "level_trims_for",
    "measurement_delays_for",
    "stubbed_capabilities",
]

#: R-3. The near-field capture ships; the splice onto the far-field trace is
#: the analysis that does not exist.
NEAR_FIELD_SPLICE_NOT_IMPLEMENTED = "near_field_splice_not_implemented"
#: R-4. Every rung of the ladder plays and banks its own record; what does not
#: exist is the consumer that turns the set into a measured floor.
DISTORTION_VS_LEVEL_NOT_IMPLEMENTED = "distortion_vs_level_not_implemented"
#: R-5a. A vertical pose now plays and banks, labelled with the elevation the
#: operator was asked for (:attr:`~.spatial.PositionGeometry.vertical_deg`) —
#: a person raises the microphone, and no automation is asked to. What does not
#: exist is the consumer that reads lobing back out of an elevation set;
#: ``crossover_v2_flow.REMOTE_VERTICAL_DISCLOSURE`` separately tells the
#: household that an externally positioned walk covers the horizontal axis
#: only, which stays true — a positioner cannot raise the microphone.
VERTICAL_AXIS_NOT_IMPLEMENTED = "vertical_axis_not_implemented"


@dataclass(frozen=True)
class CapabilityStub:
    """One named hole in the engine's own capability, said out loud.

    ``code`` is the machine fact and the thing to assert on; ``message`` is the
    sentence a person reads. ``captured`` is the discriminator that matters
    operationally: a stub whose capture still happened has evidence waiting for
    the analysis that will read it, and a stub whose capture did not happen has
    nothing banked and nothing to re-analyze later.

    ``instrument`` names the §5 roster row that closes the hole, so the
    disclosure points at the work instead of merely regretting its absence.
    """

    code: str
    instrument: str
    captured: bool
    message: str


@dataclass(frozen=True)
class _StubRow:
    """The four facts one hole is rendered from."""

    capability: str
    owed: str
    instrument: str
    captured: bool


def _stub(code: str, row: _StubRow) -> CapabilityStub:
    """One stub in the wording shape ruling S12 fixes.

    The canonical example the ruling quotes renders from this function exactly,
    which is why the sentence is composed rather than stored: a second stub
    written by hand would drift out of the shape by its second line.
    """
    banked = "capture banked" if row.captured else "nothing captured"
    return CapabilityStub(
        code=code,
        instrument=row.instrument,
        captured=row.captured,
        message=(
            f"{row.capability} not implemented; {banked}, "
            f"{row.owed} pending {row.instrument}"
        ),
    )


#: One row per named hole. Kept as data so a fifth stub joins the engine's
#: vocabulary by adding a row here and nowhere else.
_ROWS: dict[str, _StubRow] = {
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED: _StubRow(
        "near-field splice", "splice", "R-3", captured=True,
    ),
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED: _StubRow(
        "distortion-vs-level sweep", "level ladder", "R-4", captured=True,
    ),
    VERTICAL_AXIS_NOT_IMPLEMENTED: _StubRow(
        "vertical-axis analysis", "elevation read", "R-5a", captured=True,
    ),
}

#: Built once at import. Each stub is a frozen value that depends on nothing
#: but its code, so rebuilding one per ``measure`` call bought nothing.
_STUBS = {code: _stub(code, row) for code, row in _ROWS.items()}

#: Every code :func:`stubbed_capabilities` can return, so a caller can CHECK a
#: code rather than trust it, and so a reader can count the holes. Derived from
#: the table above rather than re-listed, so a fifth stub joins it by existing.
STUB_CODES = frozenset(_STUBS)


@dataclass(frozen=True)
class MeasureSpec:
    """The parameter bundle one ``measure`` runs, and one preset saves.

    ``positions`` are signed whole-degree bearings on ``position_axis``, in the
    frame :class:`~.spatial.PositionGeometry` declares and owns: negative LEFT
    of the design axis as seen from the microphone looking at the speaker. An
    empty tuple means the design axis alone, and the session spells that
    :data:`~.contracts.DESIGN_AXIS_DEG` — the same ``0`` that module's own
    ``_DESIGN_AXIS_GEOMETRY`` uses for a capture with no prompted move — so
    ``positions=()`` and ``positions=(0,)`` name one pose and produce one
    record shape, never two spellings of the same place.

    ``pose_prompts`` is what the mover was TOLD, one per position, and it is
    the ``place`` block's ``prompt`` field (§wave 4). MS-17 keeps it on the
    shared shape whichever mover satisfied it: an arm-driven record carries the
    field rather than growing a second record shape. Empty means no prompt was
    issued; a non-empty tuple must be as long as ``positions``.

    **Nothing here names the mover** (MS-17). Who put the microphone at a
    bearing — an arm, a person, whatever comes next — is provenance the record
    carries and no parameter selects. That is why a third front end needs zero
    engine edits.

    **The vertical axis carries no BEARING on this rig, and that is not the
    same as carrying no value.** Nothing here swings in elevation, so a
    vertical walk states no ``positions``; where the microphone was RAISED to
    is ``vertical_deg``, one signed whole-degree elevation above mark height
    for the whole spec, in :class:`~.spatial.PositionGeometry`'s frame. It
    defaults to 0 because a spec nobody raised measured at mark height, and 0
    says that truly. The walk is performed by a person, so nothing here refuses
    a value on it; what R-5a still owes is the analysis that reads an elevation
    set back out (:data:`VERTICAL_AXIS_NOT_IMPLEMENTED`).

    ``vertical_deg`` is one number rather than a tuple beside ``positions``
    deliberately: a spec measures one elevation across every bearing it walks,
    and a per-position elevation would be a second pose table nothing asks for.

    ``level_ladder_dbfs`` is R-4's axis, and it moves the **stimulus**, never
    the claim: each rung is a stimulus level in dBFS played through the one
    fader claim this session holds. Ruling S8's recipe depends on that split —
    *same drive voltage, nothing touched between measurements* is a statement
    about the fader, and a ladder that re-levelled the claim would break it.
    Empty means the single stimulus the program declares, which is what every
    capture uses today.

    ``inverted_role`` is the SECOND half of ``polarity`` and never a capability
    of its own (ruling S12): *inverted* is the regime, and the reverse-null's
    whole content is WHICH branch was flipped. The two are therefore checked
    together — a flipped branch with no regime and a regime with no branch are
    both refused — because a record that said only *"inverted"* would not name
    the measurement it took, and a reader would have to guess the sign
    convention it is comparing against.

    **The convention: the flip is RELATIVE to the design polarity the graph
    would otherwise carry**, not an absolute *"this branch reads inverted"*. On
    the production MEASURE shape the base polarity is all-false, so it reduces
    to assignment; on the legacy applied-response shape a ``polarity=inverted``
    record can name a graph whose source reads ``inverted: false`` by double
    negation. The emitted ``# inverted_roles=[…]`` metadata comment is what
    disambiguates the two, so a reader compares the pair by that and never by
    the flag.
    """

    kind: str
    positions: tuple[int, ...] = ()
    pose_prompts: tuple[str, ...] = ()
    position_axis: str = POSITION_AXIS_HORIZONTAL
    vertical_deg: int = 0
    regime: str = REGIME_REFERENCE_AXIS
    polarity: str = POLARITY_NORMAL
    inverted_role: str = ""
    level_ladder_dbfs: tuple[float, ...] = ()
    candidate_id: str = ""
    #: R-1's delay coordinate: which branch carries it, and how much. The pair
    #: behaves like ``polarity``/``inverted_role`` — stating one without the
    #: other is a spec that means two things. Zero on every other capture, which
    #: is what keeps their graphs byte-identical.
    delayed_role: str = ""
    delay_us: float = 0.0
    #: Whether this capture's graph carries the box's own per-driver
    #: level-match trims. A BOOLEAN and never the numbers: the trims are a
    #: property of the speaker, resolved on-box from banked evidence at the one
    #: precedence owner, so an operator who hand-carried values would be
    #: measuring through a level match some other box was measured for. False
    #: on every other capture, which is what keeps their graphs byte-identical.
    level_matched: bool = False

    def __post_init__(self) -> None:
        if self.kind not in MEASURE_KINDS:
            raise ValueError(
                f"a measure kind must be one of {MEASURE_KINDS}, got {self.kind!r}"
            )
        if self.regime not in MEASURE_REGIMES:
            raise ValueError(
                f"a capture regime must be one of {MEASURE_REGIMES}, "
                f"got {self.regime!r}"
            )
        if self.polarity not in POLARITIES:
            raise ValueError(
                f"a capture polarity must be one of {POLARITIES}, "
                f"got {self.polarity!r}"
            )
        if self.polarity == POLARITY_INVERTED:
            if self.inverted_role not in DRIVER_ROLES:
                raise ValueError(
                    "an inverted-polarity capture must name the driver branch "
                    f"it flips, one of {DRIVER_ROLES}, got "
                    f"{self.inverted_role!r}"
                )
        elif self.inverted_role:
            raise ValueError(
                f"inverted_role={self.inverted_role!r} needs "
                f"polarity={POLARITY_INVERTED!r}; a {self.polarity!r} capture "
                "flips no branch"
            )
        if bool(self.delayed_role) != bool(self.delay_us):
            raise ValueError(
                "delayed_role and delay_us are one decision with two halves: "
                f"got delayed_role={self.delayed_role!r} with "
                f"delay_us={self.delay_us!r}"
            )
        if self.delayed_role and self.delayed_role not in DRIVER_ROLES:
            # Checked exactly as `inverted_role` is, and for a sharper reason:
            # an unknown role emits a Delay filter the pipeline never
            # references, so the capture plays with NO delay and banks as a
            # delayed take. A silent wrong measurement, which is what ruling
            # S12 refuses.
            raise ValueError(
                "a delayed capture must name a real driver branch, one of "
                f"{DRIVER_ROLES}, got {self.delayed_role!r}"
            )
        if not math.isfinite(self.delay_us):
            raise ValueError(f"delay_us must be finite, got {self.delay_us!r}")
        if self.delay_us < 0.0 or self.delay_us > MAX_DSP_DELAY_US:
            # The sign frame lives in the walk coordinate, which names the
            # branch; what reaches a Delay filter is always non-negative.
            raise ValueError(
                f"delay_us is a non-negative microsecond value at or below "
                f"{MAX_DSP_DELAY_US:g}, got {self.delay_us!r}"
            )
        for bearing in self.positions:
            # Whole degrees, for the reason `PositionGeometry` gives: the poses
            # come from tape-measure offsets to a mark placed "about" 1 m out,
            # and a tenth of a degree would claim a precision the placement
            # never had. `bool` is an `int` and is never a bearing.
            if isinstance(bearing, bool) or not isinstance(bearing, int):
                raise ValueError(
                    "a pose bearing is a whole number of degrees, got "
                    f"{bearing!r}"
                )
        if self.positions and self.position_axis == POSITION_AXIS_VERTICAL:
            # Two declared fields saying incompatible things, checked together
            # for the reason ``polarity``/``inverted_role`` are: ``positions``
            # are HORIZONTAL bearings, and nothing on this rig commands one on
            # a vertical walk. Not an axis block — ``vertical_deg`` is free on
            # either axis — but the invariant every downstream reader is told
            # to rely on: a vertical walk's takes carry no bearing, which is
            # what keeps them out of every pooled bearing set.
            raise ValueError(
                f"a {POSITION_AXIS_VERTICAL!r} walk commands no horizontal "
                f"bearing, so it states no positions; got {self.positions!r}. "
                "State where the microphone was raised to with vertical_deg"
            )
        if self.pose_prompts and len(self.pose_prompts) != len(self.positions or (0,)):
            raise ValueError(
                "pose_prompts must name every position or none: "
                f"{len(self.pose_prompts)} prompts for "
                f"{len(self.positions or (0,))} positions"
            )
        self._check_pose_axis()

    def _check_pose_axis(self) -> None:
        """Axis, bearing and elevation, checked by the module that owns the frame.

        Deliberately not a second copy of the rule: building one
        :class:`~.spatial.PositionGeometry` re-uses that class's own refusals,
        so this cannot drift off them — which the hand-written copy this
        replaces already had, by one word.

        The import is deferred because :mod:`.spatial` costs ~1,100 modules
        including ``numpy``, and a spec is constructed by callers that have no
        other reason to pay for it. Only the paths that state a pose pay.
        """
        from .spatial import MARK_DISTANCE_M, PositionGeometry

        bearings: tuple[int | None, ...] = self.positions or (None,)
        for bearing in bearings:
            PositionGeometry(
                axis=self.position_axis,
                degrees=bearing,
                mark_distance_m=MARK_DISTANCE_M,
                vertical_deg=self.vertical_deg,
            )


def measurement_delays_for(spec: MeasureSpec) -> dict[str, float]:
    """The per-role delay map this spec's graph must carry.

    The delay twin of :func:`inverted_roles_for`, and the ONE translation from
    the measurement's delay words into the emitter's vocabulary, so the value
    is folded in exactly one place. Empty for every spec that names no delay,
    which is what keeps an ordinary program's graph byte-identical.
    """
    if not spec.delayed_role:
        return {}
    return {spec.delayed_role: spec.delay_us}


def level_trims_for(
    spec: MeasureSpec, resolved_db: Mapping[str, float] | None,
) -> dict[str, float]:
    """The per-role attenuation this spec's graph must carry.

    The third translation beside :func:`measurement_delays_for` and
    :func:`inverted_roles_for`, and the only place the spec's BOOLEAN meets the
    numbers. ``resolved_db`` is what the session was opened with — resolved
    once, on-box, from the banked evidence the box owns — so this function
    chooses between applying it and applying nothing, and never derives a
    value. Empty for every spec that asks for no level match, which is what
    keeps an ordinary capture's graph byte-identical.

    A spec that asks for one when the session holds no trims answers empty
    rather than raising: the refusal for that belongs at session open, where
    the evidence question is asked once and an operator can still act on the
    answer.
    """
    if not spec.level_matched:
        return {}
    return {str(role): float(db) for role, db in (resolved_db or {}).items()}


def inverted_roles_for(spec: MeasureSpec) -> tuple[str, ...]:
    """The driver branches this spec's graph must carry sign-flipped.

    The ONE translation from the measurement's polarity words into the graph
    seam's vocabulary, so the sign decision is made in exactly one place and a
    reader can grep for it. Empty for every normal-polarity spec, which is what
    keeps a non-inverted install byte-identical to what it always emitted.

    Cheap on purpose — a plain tuple over one string. The graph seam takes it
    and hands it to the emitter; nothing between them re-derives it from the
    polarity word.
    """
    if spec.polarity != POLARITY_INVERTED:
        return ()
    return (spec.inverted_role,)


def stubbed_capabilities(spec: MeasureSpec) -> tuple[CapabilityStub, ...]:
    """Every capability this spec asks for that the engine has not built.

    Total and side-effect-free: a spec asking for nothing unbuilt returns an
    empty tuple, and a spec asking for several returns several. The caller
    decides what to do with them — :meth:`~.session.TuningSession.measure`
    treats a stub whose ``captured`` is ``False`` as "there is nothing to play"
    and one whose ``captured`` is ``True`` as "play, bank, and say what the
    banked evidence is still owed."
    """
    codes: list[str] = []
    if spec.regime == REGIME_NEAR_FIELD:
        codes.append(NEAR_FIELD_SPLICE_NOT_IMPLEMENTED)
    if spec.level_ladder_dbfs:
        codes.append(DISTORTION_VS_LEVEL_NOT_IMPLEMENTED)
    if spec.position_axis == POSITION_AXIS_VERTICAL or spec.vertical_deg:
        # Keyed on the ELEVATION, not on the axis word: the two are orthogonal,
        # so a horizontal walk raised off mark height banks the same unanalysed
        # evidence a vertical walk does and is owed the same disclosure.
        codes.append(VERTICAL_AXIS_NOT_IMPLEMENTED)
    return tuple(_STUBS[code] for code in codes)
