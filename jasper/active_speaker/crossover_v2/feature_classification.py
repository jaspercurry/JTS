# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What KIND of feature is that, read from a banked verdict — and nothing else.

The product register of the classification vocabulary the 2026-08-19 wired
night produced in the lab.  It is the *verdict format*, not the pipeline: this
module runs no excess-group-delay test, re-windows nothing, and computes no
number.  It reads a banked record, types it, and answers one question, once
per sign — **is this feature one a cut (or a boost) may be aimed at?**

**Why a register and not the instrument.**
``docs/active-speaker-tuning-layers-design.md``'s stage P3 rule 1 asks that
every feature be typed before it is corrected.  What a **gate** needs to
honour that is far smaller than a measurement program: the ability to say
"this frequency was classified, and the verdict was one that admits a cut."
So this module is the verdict format alone — it runs no test and computes no
number — and :mod:`.feature_classifier` is the instrument that produces one,
offline over a round's banked captures.  Either that instrument's output or an
operator's own banked lab result reaches a gate through the same reader, which
is the point of keeping the two apart: a round carries verdicts when somebody
classified it, whichever of the two did.

**The reserved name was already here.**  The evidence packet's
``not_evaluated`` block has been publishing the field
``per_bin_minimum_phase_class`` since the packet shipped, and now names it
only for a round nobody classified.  This module is what that field names when
it IS present, so the packet reports one fact under one name whether it has it
or not.

**The verdict strings are the lab's, character for character.**  They are not
re-spelled, re-cased, or normalized into a tidier enum, because a banked
artifact is read by both the tool that wrote it and the gate that consumes it,
and a register that "improved" the spelling would silently stop matching every
record already on disk.  :data:`DEFECT_CUTTABLE` is
``"defect-cuttable (min-phase peak)"``, parenthetical included.

**A defect verdict is necessary and NOT sufficient, and the run log says so in
its own words.**  Run-log §9.2: *"``defect-*`` says EQ is not structurally
barred.  It does NOT say EQ will help."*  Every EQ arm played that night
measured worse against the frozen reference.  So a caller must read
:func:`defect_cuttable_at` and :func:`defect_boostable_at` as *bars*, never as
recommendations — they remove the features that sign of filter is the wrong
instrument for (an interference null, a room mode) and leave the ones where it
is at least the right *kind* of tool.  Whether it helps is what the round
measures afterwards.

**Fail-closed on every unreadable row.**  A row this module cannot type does
not become an ``ambiguous`` verdict; it is dropped, so it can never vouch for
anything.  A dropped row and an absent one are the same fact to a gate —
*no verdict covers that frequency* — and both refuse.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CLASSIFICATIONS",
    "DEFECT_BOOSTABLE",
    "DEFECT_CUTTABLE",
    "EGD_AMBIGUOUS",
    "EGD_MIN_PHASE",
    "EGD_NON_MIN_PHASE",
    "GATE_MOVED",
    "GATE_STABLE",
    "INTERFERENCE_BARRED",
    "ROOM",
    "UNRESOLVED",
    "VERDICT_MATCH_TOLERANCE_OCTAVES",
    "FeatureVerdict",
    "defect_boostable_at",
    "defect_cuttable_at",
    "read_feature_verdicts",
]


# --------------------------------------------------------------------------- #
# the vocabulary — the lab's own strings, verbatim
# --------------------------------------------------------------------------- #

#: The excess-group-delay test's answer. A feature whose excess group delay is
#: a large fraction of the matched non-minimum-phase scale is a cancellation,
#: and EQ is structurally the wrong tool for one: a filter aimed at a null
#: lowers the direct sound and the delayed copy together.
EGD_MIN_PHASE = "MIN-PHASE"
EGD_NON_MIN_PHASE = "NON-MIN-PHASE"
EGD_AMBIGUOUS = "ambiguous"

#: The gate-invariance test's answer, against a matched-Q minimum-phase null
#: model so ordinary gate-driven shrinkage is subtracted rather than misread.
#: ``MOVED`` means the feature is a property of the window — a room arrival,
#: not the driver.
GATE_STABLE = "STABLE"
GATE_MOVED = "MOVED"

#: The composed verdict. Exactly five values, and the precedence between them
#: is the lab report's: a NON-MIN-PHASE reading bars the feature whatever the
#: gate said, and a MOVED gate makes it the room's whatever the phase said.
INTERFERENCE_BARRED = "interference-barred"
ROOM = "room"
DEFECT_CUTTABLE = "defect-cuttable (min-phase peak)"
DEFECT_BOOSTABLE = "defect-boostable (min-phase dip)"
UNRESOLVED = "ambiguous"

#: The closed set, for a reader that wants to say "this is not a verdict I
#: know" rather than silently treating an unknown string as a refusal it can
#: explain. Both answers refuse; only one of them is honest about why.
CLASSIFICATIONS = frozenset({
    INTERFERENCE_BARRED,
    ROOM,
    DEFECT_CUTTABLE,
    DEFECT_BOOSTABLE,
    UNRESOLVED,
})

# A banked classification artifact is identified by its FILENAME and its row
# shape, and deliberately not by a `kind` discriminator.
#
# An earlier version of this module declared `CLASSIFICATION_ARTIFACT_KIND =
# "jts_feature_classification"` and documented it as the name such an artifact
# "must carry". Nothing checked it, and — decisively — the real 2026-08-19
# artifact does not have one: its top-level keys are `schema`, `thresholds` and
# `rows`. The constant therefore described a shape that does not exist, and
# enforcing it would have refused the only record there is. Deleted rather than
# enforced: the packet names the file it reads
# (`evidence_packet.CLASSIFICATION_ARTIFACT`) and `read_feature_verdicts` types
# the rows, which is the whole of what a reader needs. If a producer is ever
# built in-product, giving its output a `kind` is that PR's decision to make
# against a shape it controls.

#: How far a prescribed centre frequency may sit from the classified feature
#: it claims to be aimed at, in octaves either side.
#:
#: A sixth of an octave. The verdicts are read off fractional-octave-smoothed
#: curves, so a feature's centre is not locatable finer than the smoothing
#: width in the first place, and a tolerance tighter than the evidence's own
#: resolution would refuse honest proposals for a decimal.
#:
#: **It does NOT keep a filter away from its neighbours, and an earlier version
#: of this comment claimed it did.** That claim quoted three of the 2026-08-19
#: record's nine features and generalized from the two widest gaps. All eight
#: gaps, in octaves, are:
#:
#:     1037 →1406  0.439      4582 →5396  0.236
#:     1406 →2057  0.549      5396 →6245  0.211
#:     2057 →4149  1.012      6245 →8530  0.450
#:     4149 →4582  **0.143**  8530 →9509  **0.157**
#:
#: Two of the eight sit INSIDE this tolerance, and both are peak–dip pairs:
#: 4149 (cuttable peak) beside 4582 (boostable dip), and 8530 (boostable dip)
#: beside 9509 (cuttable peak). So on the real record a filter aimed squarely at
#: a dip has a cuttable peak inside its match radius, which is exactly why
#: :func:`defect_cuttable_at` lets the NEAREST verdict decide rather than
#: letting any in-radius cuttable one vouch. The tolerance's job is to absorb
#: the evidence's own locating error; keeping features apart is the nearest
#: rule's job, and it does not need them to be far apart.
#:
#: Deliberately symmetric in OCTAVES rather than in Hz: a fixed Hz window would
#: be generous at 300 Hz and absurd at 12 kHz, and every other frequency
#: tolerance in this subsystem is logarithmic for that reason.
VERDICT_MATCH_TOLERANCE_OCTAVES = 1.0 / 6.0


def _finite(value: Any) -> float | None:
    """One real number out of banked JSON, or ``None`` — never a coercion.

    ``bool`` is rejected because it is an ``int`` in Python; strings are
    rejected because ``float("1037")`` succeeds and would make this reader's
    strictness depend on whoever encoded the artifact. ``OverflowError`` is
    caught because an arbitrary-precision ``int`` is legal JSON, passes the
    isinstance check, and then raises rather than returning infinity.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class FeatureVerdict:
    """One classified feature, as a gate reads it.

    A deliberate SUBSET of the lab row, which carries thirty-odd columns of
    working (per-gate retention, null-model scales, z-scores, centre shifts).
    Those are how the verdict was reached and they belong in the artifact; a
    gate that read them would be re-deriving a decision the classifier already
    made, with none of its controls. What is kept is the verdict, the two
    tests behind it, how confident the classifier was, and the two disclosures
    that bound what it could see.
    """

    #: The feature's centre frequency.
    freq_hz: float
    #: One of :data:`CLASSIFICATIONS`, or any other string the artifact
    #: carried. An unknown value is kept rather than rejected so a refusal can
    #: quote it; it simply satisfies no bar.
    classification: str
    #: The two component verdicts, so a refusal can say which test barred the
    #: feature rather than only that something did.
    egd_verdict: str
    gate_verdict: str
    #: ``"high"`` / ``"med"`` / ``"low"``, as the classifier reported it.
    confidence: str
    #: The feature's own measured Q, when the artifact carried one. This is
    #: what a prescriber should match a cut's width to; it is reported rather
    #: than enforced, because a filter narrower than its target is a different
    #: (and cheaper) mistake than one wider than it.
    measured_q: float | None
    #: How far the feature departs from its neighbours, dB, unsigned, when the
    #: artifact carried one — a DIP's own depth. Optional because the
    #: 2026-08-19 record does not carry it; a boost bar that needs it refuses
    #: by name rather than substituting a number (see
    #: :mod:`.driver_prescription`'s ``driver_feature_depth_unavailable``).
    depth_db: float | None
    #: The classifier's own disclosure that a horizontal-turntable capture
    #: cannot see vertical-plane interference at all. A verdict carrying it is
    #: not wrong; it is bounded, and a reader that hid the bound would be
    #: reporting more certainty than the instrument had.
    #:
    #: **A banked LAB artifact's ``false`` here is not a vertical-plane
    #: sighting.** The 2026-08-19 records compute the field as "fewer than two
    #: gates resolved", which is a fact about the gate test and not about the
    #: plane, so most of their rows read ``false`` off a horizontal walk.
    #: :mod:`.feature_classifier` emits ``true`` unconditionally for exactly
    #: this reason — but the boost bar honours whatever the artifact says, so a
    #: legacy record can still clear a bar this field exists to hold. Tracked
    #: as issue #2783; do not read a ``false`` from an artifact this product
    #: did not write as evidence.
    vertical_blind: bool

    @property
    def is_defect_cuttable(self) -> bool:
        """Is a cut at least the right KIND of instrument for this feature?

        Not "will a cut help" — see this module's docstring and run-log §9.2.
        """
        return self.classification == DEFECT_CUTTABLE

    @property
    def is_defect_boostable(self) -> bool:
        """Is a boost at least the right KIND of instrument for this feature?

        The mirror of :attr:`is_defect_cuttable`, and just as insufficient: a
        boost additionally owes a depth and a vertical-plane sighting, and
        :mod:`.driver_prescription` is where those are required.
        """
        return self.classification == DEFECT_BOOSTABLE

    def to_dict(self) -> dict[str, Any]:
        """The verdict as a refusal, a packet block, or a receipt carries it.

        The frequency key is ``hz`` rather than ``freq_hz`` — the banked
        artifact's own spelling — so this output is itself readable by
        :func:`read_feature_verdicts`. The evidence packet publishes a typed
        block and the gate reads it back through the one reader; a tidier key
        here would have made the packet's own block the one shape that reader
        could not parse.
        """
        return {
            "hz": self.freq_hz,
            "classification": self.classification,
            "egd_verdict": self.egd_verdict,
            "gate_verdict": self.gate_verdict,
            "confidence": self.confidence,
            "measured_q": self.measured_q,
            "depth_db": self.depth_db,
            "vertical_blind": self.vertical_blind,
        }


def read_feature_verdicts(raw: Any) -> tuple[FeatureVerdict, ...]:
    """Type a banked classification artifact's rows. Never raises.

    ``raw`` is the artifact's ``rows`` list, or the whole artifact mapping —
    both are accepted because the packet hands one and a durable read-back
    hands the other, and a reader that refused the wrapper would make the
    packet spell the artifact's internal layout.

    A row without a readable frequency or classification is DROPPED. That is
    the fail-closed direction and the only one available: a row that cannot be
    typed cannot vouch for a cut, and keeping it as an ``ambiguous`` verdict
    would put an unreadable record in the same bucket as one the classifier
    genuinely could not resolve. Those are different facts and only the second
    is evidence.
    """
    rows: Any = raw
    if isinstance(raw, Mapping):
        rows = raw.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return ()
    out: list[FeatureVerdict] = []
    for entry in rows:
        if not isinstance(entry, Mapping):
            continue
        freq = _finite(entry.get("hz"))
        if freq is None or freq <= 0.0:
            continue
        classification = entry.get("classification")
        if not isinstance(classification, str) or not classification.strip():
            continue
        out.append(
            FeatureVerdict(
                freq_hz=freq,
                classification=classification.strip(),
                egd_verdict=_text(entry.get("egd_verdict")),
                gate_verdict=_text(entry.get("gate_verdict")),
                confidence=_text(entry.get("confidence")),
                measured_q=_finite(entry.get("measured_q")),
                depth_db=_finite(entry.get("depth_db")),
                vertical_blind=_blind(entry.get("vertical_blind")),
            )
        )
    return tuple(out)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _blind(value: Any) -> bool:
    """Vertical-blindness, fail-CLOSED: only a literal ``false`` — or nothing at
    all — reads as SIGHTED.

    By IDENTITY, not truthiness and not equality. The rows are hand-authored, so
    ``"true"``, ``1`` and ``"yes"`` are realistic spellings of the flag, and the
    previous ``is True`` test admitted every one of them as sighted — the one
    direction that lets a boost through a bound the classifier had declared.
    ``0`` is read as BLIND for the reason this module refuses coercion
    everywhere else: ``0 == False`` in Python, so an equality test would quietly
    accept an integer nobody said was a boolean. A verdict is bounded until it
    says plainly that it is not.
    """
    return value is not False and value is not None


def defect_cuttable_at(
    verdicts: Sequence[FeatureVerdict],
    freq_hz: float,
    *,
    tolerance_octaves: float = VERDICT_MATCH_TOLERANCE_OCTAVES,
) -> tuple[FeatureVerdict | None, FeatureVerdict | None]:
    """The verdict that vouches for a cut at ``freq_hz``, and the nearest one.

    **THE NEAREST VERDICT DECIDES.** Returns ``(vouching, nearest)``:

    * ``nearest`` is the closest verdict inside ``tolerance_octaves``, whatever
      it said, or ``None`` when nothing was classified there at all.
    * ``vouching`` is that same verdict when — and only when — its
      classification is :data:`DEFECT_CUTTABLE`. It is never a different, more
      agreeable verdict standing further away.

    **It used to be "any cuttable verdict inside the radius vouches", and that
    was wrong on the record it was written against.** Two of the 2026-08-19
    record's eight gaps are narrower than the tolerance and both are peak–dip
    pairs (see :data:`VERDICT_MATCH_TOLERANCE_OCTAVES`). Under the old rule a
    cut aimed squarely at the 4582 Hz minimum-phase DIP found the cuttable
    4149 Hz peak 0.143 octaves away and was ACCEPTED, banking a receipt that
    cited the peak — and cutting a minimum-phase dip *deepens* it, which is the
    precise harm the refusal this feeds exists to prevent. Same shape at
    8530 Hz (dip) borrowing 9509 Hz (peak). Four of the nine features are dips;
    the old rule made three of the four cuttable.

    The rule is therefore the ordinary one for a claim about a frequency: the
    closest claim owns it. A prescriber that wants to cut the 4149 Hz peak aims
    at 4149 Hz, and the nearest verdict is then the peak's own.

    Both values are returned because a refusal needs to tell two cases apart.
    "No feature at this frequency was ever classified" sends a prescriber to run
    the classifier; "the feature there is a minimum-phase dip" tells it the
    answer is no and why, and that a different filter will not fix it. A single
    boolean collapses two different instructions into one.

    **Ties fail closed.** Two verdicts exactly equidistant is pathological
    rather than impossible, and a non-cuttable one wins that tie: a rule whose
    answer depended on the artifact's row order would be a rule the record could
    silently change.

    A non-positive or non-finite ``freq_hz`` matches nothing rather than
    raising: this function is reachable from a durable artifact and its contract
    is a finding, not an exception.
    """
    return _vouching_at(verdicts, freq_hz, tolerance_octaves, DEFECT_CUTTABLE)


def defect_boostable_at(
    verdicts: Sequence[FeatureVerdict],
    freq_hz: float,
    *,
    tolerance_octaves: float = VERDICT_MATCH_TOLERANCE_OCTAVES,
) -> tuple[FeatureVerdict | None, FeatureVerdict | None]:
    """The verdict that vouches for a BOOST at ``freq_hz``, and the nearest one.

    :func:`defect_cuttable_at` with :data:`DEFECT_BOOSTABLE` as the eligible
    class, through the same helper so the two cannot drift: nearest verdict
    inside ``tolerance_octaves`` decides, a tie fails closed away from
    boostable, and a non-positive or non-finite ``freq_hz`` matches nothing.

    ``vouching`` is necessary and NOT sufficient. A minimum-phase dip is the
    only feature a boost is structurally the right tool for, and it is still
    the wrong tool when the classifier could not see the vertical plane or did
    not report the dip's depth. :mod:`.driver_prescription` requires both.
    """
    return _vouching_at(verdicts, freq_hz, tolerance_octaves, DEFECT_BOOSTABLE)


def _vouching_at(
    verdicts: Sequence[FeatureVerdict],
    freq_hz: float,
    tolerance_octaves: float,
    eligible: str,
) -> tuple[FeatureVerdict | None, FeatureVerdict | None]:
    """``(vouching, nearest)`` — the ONE nearest-verdict-decides rule.

    One body rather than two, so the cut bar and the boost bar cannot disagree
    about which verdict owns a frequency. ``eligible`` is the only difference
    between them, and it is also what the tie-break moves away from.
    """
    target = _finite(freq_hz)
    if target is None or target <= 0.0 or tolerance_octaves <= 0.0:
        return None, None
    nearest: FeatureVerdict | None = None
    nearest_distance = math.inf
    for verdict in verdicts:
        if verdict.freq_hz <= 0.0:
            continue
        distance = abs(math.log2(verdict.freq_hz / target))
        if distance > tolerance_octaves:
            continue
        if nearest is None or distance < nearest_distance:
            nearest, nearest_distance = verdict, distance
        elif distance == nearest_distance and nearest.classification == eligible:
            # The tie-break, and it only ever moves AWAY from eligible.
            nearest = verdict
    if nearest is None:
        return None, None
    return (nearest if nearest.classification == eligible else None), nearest
