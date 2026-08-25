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

**Every word here is borrowed from the module that already owns it.** The
duplication that this refactor exists to remove starts with a second spelling
of a vocabulary, so:

* the pose axes are :mod:`.spatial`'s :data:`~.spatial.POSITION_AXES`;
* the capture regimes are
  :data:`~jasper.active_speaker.driver_acoustics.CAPTURE_GEOMETRIES`, which is
  exported for exactly this use — *"so a caller carrying a geometry out of an
  operator-authored document can check it at its own door"*;
* the polarity words are derived from
  :func:`~jasper.audio_measurement.program_analysis.polarity_label`, which its
  own docstring calls *"the ONE spelling of the map"*.

**The four stubs, and which roster row lands each** (§5's instrument roster):

===========================  ==============  ============================
Parameter                    Instrument      What still happens today
===========================  ==============  ============================
``regime=near_field``        R-3             the capture is taken and banked
``polarity=inverted``        R-1             nothing is captured
``level_ladder_dbfs=(…)``    R-4             the declared level is measured
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

from jasper.active_speaker.driver_acoustics import CAPTURE_GEOMETRIES
from jasper.audio_measurement.program_analysis import polarity_label

from .spatial import (
    POSITION_AXES,
    POSITION_AXIS_HORIZONTAL,
    POSITION_AXIS_VERTICAL,
)

__all__ = [
    "DISTORTION_VS_LEVEL_NOT_IMPLEMENTED",
    "INVERTED_POLARITY_NOT_IMPLEMENTED",
    "MEASURE_KINDS",
    "MEASURE_KIND_BASELINE",
    "MEASURE_KIND_CANDIDATE",
    "MEASURE_KIND_VERIFY",
    "MEASURE_REGIMES",
    "NEAR_FIELD_SPLICE_NOT_IMPLEMENTED",
    "POLARITIES",
    "POLARITY_INVERTED",
    "POLARITY_NORMAL",
    "REGIME_NEAR_FIELD",
    "REGIME_REFERENCE_AXIS",
    "STUB_CODES",
    "VERTICAL_AXIS_NOT_IMPLEMENTED",
    "CapabilityStub",
    "MeasureSpec",
    "stubbed_capabilities",
]

#: The three parameterizations of the one ``measure`` verb — ruling S1's
#: *"measuring is measuring"* made visible in the data, and wave 4j's ``kind``
#: index column. A baseline, a candidate check and a re-measure differ by this
#: word and by nothing else in the code that runs them.
MEASURE_KIND_BASELINE = "baseline"
MEASURE_KIND_CANDIDATE = "candidate"
MEASURE_KIND_VERIFY = "verify"
MEASURE_KINDS = (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
)

#: The regime SET is read from the module that declares it, so R-3's splice
#: finds both of its record kinds under one vocabulary. The two names below are
#: this module's handles on that set's members — pinned equal to it by
#: ``tests/test_crossover_v2_engine_skeleton.py``, because a handle that drifts
#: off its set is a parameter nobody can pass.
MEASURE_REGIMES = tuple(sorted(CAPTURE_GEOMETRIES))
REGIME_NEAR_FIELD = "near_field"
REGIME_REFERENCE_AXIS = "reference_axis"

#: The measurement frame's polarity words, DERIVED from their owner rather than
#: respelled — the same discipline :data:`~.capture_source.INTEGRITY_COUNTER_KEYS`
#: applies to the frame-ledger keys. Distinct from the candidate's polarity
#: ACTIONS (``crossover_alignment.POLARITY_KEEP`` / ``POLARITY_INVERT``), which
#: say what a speaker should DO rather than how a capture was taken.
POLARITY_NORMAL = polarity_label(1)
POLARITY_INVERTED = polarity_label(-1)
POLARITIES = (POLARITY_NORMAL, POLARITY_INVERTED)

#: R-3. The near-field capture ships; the splice onto the far-field trace is
#: the ``analyze`` function that does not exist.
NEAR_FIELD_SPLICE_NOT_IMPLEMENTED = "near_field_splice_not_implemented"
#: R-1. The reverse-null is shipped in three parts with its executor deleted,
#: so nothing plays an inverted-polarity stimulus today.
INVERTED_POLARITY_NOT_IMPLEMENTED = "inverted_polarity_not_implemented"
#: R-4. One level is measured today; the ladder across levels, and the consumer
#: that turns it into a measured floor, are both unbuilt.
DISTORTION_VS_LEVEL_NOT_IMPLEMENTED = "distortion_vs_level_not_implemented"
#: R-5a. Three sites refuse a vertical pose deliberately, and a fourth
#: (``crossover_v2_flow.REMOTE_VERTICAL_DISCLOSURE``) tells the household the
#: axis is not covered. Undoing that is real work; it just needs no hardware.
VERTICAL_AXIS_NOT_IMPLEMENTED = "vertical_axis_not_implemented"

#: Every code :func:`stubbed_capabilities` can return, so a caller can CHECK a
#: code rather than trust it, and so a reader can count the holes.
STUB_CODES = frozenset({
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED,
    INVERTED_POLARITY_NOT_IMPLEMENTED,
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED,
    VERTICAL_AXIS_NOT_IMPLEMENTED,
})


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


def _stub(code: str, capability: str, *, captured: bool, owed: str,
          instrument: str) -> CapabilityStub:
    """One stub in the wording shape ruling S12 fixes.

    The canonical example the ruling quotes — *"near-field splice not
    implemented; capture banked, splice pending R-3"* — renders from this
    function exactly, which is why the sentence is composed rather than stored:
    a second stub written by hand would drift out of the shape by its second
    line.
    """
    banked = "capture banked" if captured else "nothing captured"
    return CapabilityStub(
        code=code,
        instrument=instrument,
        captured=captured,
        message=f"{capability} not implemented; {banked}, {owed} pending {instrument}",
    )


@dataclass(frozen=True)
class MeasureSpec:
    """The parameter bundle one ``measure`` runs, and one preset saves.

    ``positions`` are signed whole-degree bearings on ``position_axis``, in the
    frame :class:`~.spatial.PositionGeometry` declares and owns: negative LEFT
    of the design axis as seen from the microphone looking at the speaker. An
    empty tuple is the design axis alone.

    **Nothing here names the mover** (MS-17). Who put the microphone at a
    bearing — an arm, a person, whatever comes next — is provenance the record
    carries and no parameter selects. That is why a third front end needs zero
    engine edits.

    **The vertical axis carries no bearing on this rig.** ``PositionGeometry``
    raises on a vertical pose that states degrees, because raising and lowering
    a microphone commands no angle; a vertical walk's poses are therefore named
    by the preset's prompts and not by this field. R-5a lands those prompts,
    and until it does ``position_axis=vertical`` is a stub.

    ``level_ladder_dbfs`` is R-4's axis. Empty means the session's one declared
    level, which is what every capture uses today and what ruling S8's recipe
    requires of a level-matched set — *same drive voltage, nothing touched
    between measurements*.
    """

    kind: str
    positions: tuple[int, ...] = ()
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
        if self.position_axis not in POSITION_AXES:
            raise ValueError(
                f"a pose axis must be one of {POSITION_AXES}, "
                f"got {self.position_axis!r}"
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
        if self.position_axis == POSITION_AXIS_VERTICAL and self.positions:
            # The same refusal ``PositionGeometry`` makes, made one layer
            # earlier: a bearing on the vertical axis is a number nothing on
            # this rig can command, and a spec that carried one would ask for a
            # capture no record could honestly describe.
            raise ValueError(
                "a vertical walk carries no bearings — this rig raises and "
                f"lowers the microphone rather than swinging it, got "
                f"{self.positions!r}"
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
    stubs: list[CapabilityStub] = []
    if spec.regime == REGIME_NEAR_FIELD:
        stubs.append(_stub(
            NEAR_FIELD_SPLICE_NOT_IMPLEMENTED, "near-field splice",
            captured=True, owed="splice", instrument="R-3",
        ))
    if spec.polarity == POLARITY_INVERTED:
        stubs.append(_stub(
            INVERTED_POLARITY_NOT_IMPLEMENTED, "inverted-polarity capture",
            captured=False, owed="reverse-null", instrument="R-1",
        ))
    if spec.level_ladder_dbfs:
        stubs.append(_stub(
            DISTORTION_VS_LEVEL_NOT_IMPLEMENTED, "distortion-vs-level sweep",
            captured=True, owed="level ladder", instrument="R-4",
        ))
    if spec.position_axis == POSITION_AXIS_VERTICAL:
        stubs.append(_stub(
            VERTICAL_AXIS_NOT_IMPLEMENTED, "vertical-axis walk",
            captured=False, owed="pose prompts", instrument="R-5a",
        ))
    return tuple(stubs)
