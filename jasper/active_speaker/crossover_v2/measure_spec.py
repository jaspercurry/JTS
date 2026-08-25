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
one this package's own ``__init__`` refuses to pay for ``forward_model`` for
exactly this reason. ``contracts`` declares them and
``tests/test_crossover_v2_engine_skeleton.py`` pins each equal to its owner's
spelling, so the cheap copy cannot drift off the real one.

**The four stubs, and which roster row lands each** (§5's instrument roster):

===========================  ==============  ============================
Parameter                    Instrument      What still happens today
===========================  ==============  ============================
``regime=near_field``        R-3             the capture is taken and banked
``polarity=inverted``        R-1             nothing is captured
``level_ladder_dbfs=(…)``    R-4             every rung plays and banks
``position_axis=vertical``   R-5a            nothing is captured
===========================  ==============  ============================

Impedance (R-6) gets **no** stub: it needs hardware that may never exist, and a
parameter for it would be the speculative flexibility the charter forbids.

Side-effect-free, in the register :mod:`.spatial` and :mod:`.capture_source`
established: a stub is a value returned to a caller, never a log line and never
a raise. Refusing to WORK is what ruling S10 retires; disclosing what we cannot
do is what it keeps.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
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
    "INVERTED_POLARITY_NOT_IMPLEMENTED",
    "NEAR_FIELD_SPLICE_NOT_IMPLEMENTED",
    "STUB_CODES",
    "VERTICAL_AXIS_NOT_IMPLEMENTED",
    "CapabilityStub",
    "MeasureSpec",
    "stubbed_capabilities",
]

#: R-3. The near-field capture ships; the splice onto the far-field trace is
#: the ``analyze`` function that does not exist.
NEAR_FIELD_SPLICE_NOT_IMPLEMENTED = "near_field_splice_not_implemented"
#: R-1. The reverse-null is shipped in three parts with its executor deleted,
#: so nothing plays an inverted-polarity stimulus today.
INVERTED_POLARITY_NOT_IMPLEMENTED = "inverted_polarity_not_implemented"
#: R-4. Every rung of the ladder plays and banks its own record; what does not
#: exist is the ``analyze`` consumer that turns the set into a measured floor.
DISTORTION_VS_LEVEL_NOT_IMPLEMENTED = "distortion_vs_level_not_implemented"
#: R-5a. Three sites refuse a vertical pose deliberately, and a fourth
#: (``crossover_v2_flow.REMOTE_VERTICAL_DISCLOSURE``) tells the household the
#: axis is not covered. Undoing that is real work; it just needs no hardware.
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

    def aborted(self) -> "CapabilityStub":
        """The same hole, re-rendered for a call that captured nothing.

        A spec can trip two stubs at once — one whose capture still ships and
        one that stops the stimulus dead. When the second wins, the first must
        stop claiming *"capture banked"*, or the disclosure lies about evidence
        that does not exist. That is ruling S12's honesty clause turned on the
        disclosure itself.
        """
        if not self.captured:
            return self
        return _stub(
            self.code, _CAPABILITY[self.code], captured=False,
            owed=_OWED[self.code], instrument=self.instrument,
        )


def _stub(code: str, capability: str, *, captured: bool, owed: str,
          instrument: str) -> CapabilityStub:
    """One stub in the wording shape ruling S12 fixes.

    The canonical example the ruling quotes renders from this function exactly,
    which is why the sentence is composed rather than stored: a second stub
    written by hand would drift out of the shape by its second line.
    """
    banked = "capture banked" if captured else "nothing captured"
    return CapabilityStub(
        code=code,
        instrument=instrument,
        captured=captured,
        message=f"{capability} not implemented; {banked}, {owed} pending {instrument}",
    )


#: What each hole is called in the sentence, and what it still owes. Kept as
#: data so :meth:`CapabilityStub.aborted` can re-render a stub without a second
#: copy of either phrase.
_CAPABILITY = {
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED: "near-field splice",
    INVERTED_POLARITY_NOT_IMPLEMENTED: "inverted-polarity capture",
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED: "distortion-vs-level sweep",
    VERTICAL_AXIS_NOT_IMPLEMENTED: "vertical-axis walk",
}
_OWED = {
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED: "splice",
    INVERTED_POLARITY_NOT_IMPLEMENTED: "reverse-null",
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED: "level ladder",
    VERTICAL_AXIS_NOT_IMPLEMENTED: "pose prompts",
}
_INSTRUMENT = {
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED: "R-3",
    INVERTED_POLARITY_NOT_IMPLEMENTED: "R-1",
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED: "R-4",
    VERTICAL_AXIS_NOT_IMPLEMENTED: "R-5a",
}
_CAPTURED = {
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED: True,
    INVERTED_POLARITY_NOT_IMPLEMENTED: False,
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED: True,
    VERTICAL_AXIS_NOT_IMPLEMENTED: False,
}

#: Built once at import. Each stub is a frozen value that depends on nothing
#: but its code, so rebuilding one per ``measure`` call bought nothing.
_STUBS = {
    code: _stub(
        code, _CAPABILITY[code], captured=_CAPTURED[code],
        owed=_OWED[code], instrument=_INSTRUMENT[code],
    )
    for code in _CAPABILITY
}

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

    **The vertical axis carries no bearing on this rig.** A vertical walk's
    poses are named by ``pose_prompts`` and not by degrees; R-5a lands those
    prompts, and until it does ``position_axis=vertical`` is a stub. The
    refusal that enforces it is :class:`~.spatial.PositionGeometry`'s own — see
    :meth:`__post_init__`.

    ``level_ladder_dbfs`` is R-4's axis, and it moves the **stimulus**, never
    the claim: each rung is a stimulus level in dBFS played through the one
    fader claim this session holds. Ruling S8's recipe depends on that split —
    *same drive voltage, nothing touched between measurements* is a statement
    about the fader, and a ladder that re-levelled the claim would break it.
    Empty means the single stimulus the program declares, which is what every
    capture uses today.
    """

    kind: str
    positions: tuple[int, ...] = ()
    pose_prompts: tuple[str, ...] = ()
    position_axis: str = POSITION_AXIS_HORIZONTAL
    regime: str = REGIME_REFERENCE_AXIS
    polarity: str = POLARITY_NORMAL
    level_ladder_dbfs: tuple[float, ...] = ()
    candidate_id: str = ""

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
        if self.pose_prompts and len(self.pose_prompts) != len(self.positions or (0,)):
            raise ValueError(
                "pose_prompts must name every position or none: "
                f"{len(self.pose_prompts)} prompts for "
                f"{len(self.positions or (0,))} positions"
            )
        self._check_pose_axis()

    def _check_pose_axis(self) -> None:
        """Axis and bearing, checked by the module that owns the frame.

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
            )


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
    if spec.polarity == POLARITY_INVERTED:
        codes.append(INVERTED_POLARITY_NOT_IMPLEMENTED)
    if spec.level_ladder_dbfs:
        codes.append(DISTORTION_VS_LEVEL_NOT_IMPLEMENTED)
    if spec.position_axis == POSITION_AXIS_VERTICAL:
        codes.append(VERTICAL_AXIS_NOT_IMPLEMENTED)
    return tuple(_STUBS[code] for code in codes)
