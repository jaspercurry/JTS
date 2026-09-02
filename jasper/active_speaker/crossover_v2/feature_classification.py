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
barred.  It does NOT say EQ will help."*  Every EQ candidate played that night
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
    "LAB_ROW_FIELDS",
    "LAB_ROW_NOT_AN_UNCERTAINTY",
    "LAB_ROW_UNCERTAINTY",
    "ROOM",
    "UNCERTAINTY_KINDS",
    "UNCERTAINTY_RANDOM",
    "UNCERTAINTY_SYSTEMATIC",
    "UNCERTAINTY_UNSEPARATED",
    "UNRESOLVED",
    "VERDICT_MATCH_TOLERANCE_OCTAVES",
    "FeatureVerdict",
    "defect_boostable_at",
    "defect_cuttable_at",
    "finite_number",
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


# --------------------------------------------------------------------------- #
# the lab row — every column, and which of them are uncertainties
# --------------------------------------------------------------------------- #

#: Every column a classification row carries, in the order the instrument
#: writes them.
#:
#: :class:`FeatureVerdict` is the seven-key GATE view of this row; these are
#: all of it — the two component verdicts' own working, the excess-group-delay
#: numbers the phase call was made on, and the per-gate retention table the
#: gate call was made on. The list lives here rather than in
#: :mod:`.feature_classifier` because this module owns the row schema and that
#: one fills it in, which is the same split the verdict strings already keep.
#:
#: It is a READER'S allowlist as much as an enumeration: the evidence packet
#: copies a banked row field by field through it and publishes the names of
#: anything it held back, because a classification artifact can be an
#: operator's own banked lab result rather than this product's output, and a
#: packet that passed unknown keys straight through would stop being an
#: allowlist. ``tests/test_crossover_v2_feature_classifier.py`` pins this
#: tuple against a real :func:`~.feature_classifier.classify_round` run, so a
#: column the instrument adds cannot silently fall outside it.
LAB_ROW_FIELDS: tuple[str, ...] = (
    "hz",
    "classification",
    "egd_verdict",
    "egd_verdict_raw",
    "gate_verdict",
    "confidence",
    "measured_q",
    "depth_db",
    "pooled_db",
    "is_dip",
    "excursion_us",
    "excursion_sd_us",
    "nbhd_sd_us",
    "p2p_us",
    "nmp_scale_us",
    "frac_of_nmp",
    "z_local",
    "lead_sensitivity_us",
    "clean",
    "resolved_gates",
    "excess_loss_vs_null",
    "gate_slack",
    "centre_shift_oct",
    "gate_notes",
    "controls_ok",
    "timing_corroborated",
    "gate_rungs",
    "pose_persistence",
    "decay",
    "fdw_rungs",
    "cycles_in_primary_gate",
)

#: The two kinds an uncertainty can be, and the reason a published one always
#: says which it is.
#:
#: A RANDOM uncertainty is repeat scatter: measure again and it averages down.
#: A SYSTEMATIC one is a choice or a bias the repeats all share, so measuring
#: again does not move it. Adding one to the other produces a number that is
#: neither, and a reader deciding whether to take more captures needs to know
#: which half of a spread more captures would actually shrink.
UNCERTAINTY_RANDOM = "random"
UNCERTAINTY_SYSTEMATIC = "systematic"

#: The closed set, so a reader can say "that is not a kind I know" rather than
#: treating an unrecognised label as one of the two.
UNCERTAINTY_KINDS = frozenset({UNCERTAINTY_RANDOM, UNCERTAINTY_SYSTEMATIC})

#: The label for a spread that is real but is NOT one of the two kinds, because
#: it contains both and the evidence publishing it cannot separate them.
#:
#: Deliberately **not** a member of :data:`UNCERTAINTY_KINDS`. The closed set is
#: what lets a reader ask "random or systematic?" and get a true answer; a
#: spread that pools the two has no true answer to that question, so admitting
#: a third member would dress a refusal up as a third answer. A figure carrying
#: this label is therefore published apart from the ``fields`` list a block uses
#: for single-kind uncertainties — see the evidence packet's cross-seat sigma
#: block, whose ``uncertainty.unseparated`` is what that looks like.
#:
#: The alternative was to call such a figure "not an uncertainty" the way
#: ``gate_slack`` is, and that is the right answer for a THRESHOLD that merely
#: mixes two kinds in its definition. It is the wrong answer for a genuine
#: spread about a reading: the honest statement is that it IS one, and that
#: which kind it is is something the evidence carrying it cannot say. A block
#: publishing one owes the reader what WOULD say it.
UNCERTAINTY_UNSEPARATED = "unseparated"

#: Which :data:`LAB_ROW_FIELDS` columns ARE uncertainties, and of what.
#:
#: All three are microsecond figures qualifying the same row's ``excursion_us``,
#: and each says which kind it is and what it is a spread OF, because they are
#: not interchangeable: the two random ones describe noise, whose effect on a
#: pooled reading averaging can reduce, and the systematic one is a bias that
#: repetition does not touch at all. No number here adds one to another.
#:
#: **None of the three is itself a quantity that shrinks with more captures, and
#: an earlier draft of these strings said two of them were.** Both random
#: entries are standard DEVIATIONS, which converge on a population value as
#: captures are added rather than falling; the quantity that falls as
#: ``1/sqrt(n)`` is the standard ERROR of the pooled mean, which this block does
#: not publish and cannot be used to form, because it does not publish ``n``.
LAB_ROW_UNCERTAINTY: dict[str, dict[str, str]] = {
    "excursion_sd_us": {
        "kind": UNCERTAINTY_RANDOM,
        "of": (
            "capture-to-capture scatter of this row's excursion_us: the sample "
            "standard deviation over the round's captures, published as 0.0 at "
            "one capture, where it is undefined. It CONVERGES as captures are "
            "added rather than shrinking — going from one capture to two "
            "typically moves it up off 0.0. What falls with more captures is "
            "the standard error of the pooled mean, sd/sqrt(n), and this block "
            "does not publish n"
        ),
    },
    "nbhd_sd_us": {
        "kind": UNCERTAINTY_RANDOM,
        "of": (
            "the excess-group-delay trace's own scatter across the feature's "
            "+/-1/3-octave neighbourhood with the feature band excluded, "
            "averaged over the round's captures. It is the local noise floor "
            "this row's z_local divides by, and being a mean of per-capture "
            "scatters it likewise converges rather than shrinking"
        ),
    },
    "lead_sensitivity_us": {
        "kind": UNCERTAINTY_SYSTEMATIC,
        "of": (
            "how far excursion_us moves when the phase window's 1 ms pre-peak "
            "lead is taken away — the same captures read through one different "
            "analysis choice. Repetition does not reduce it, which is the whole "
            "difference between this row's kind and the two above"
        ),
    },
}

#: Columns shaped like an uncertainty that are NOT one, and why not.
#:
#: Named rather than left out. Both read like a spread and sit in the same row
#: as three that ARE uncertainties, and ``gate_slack`` is the reason this second
#: list exists at all: it is the LARGER of a fixed floor and a random 3-sigma,
#: which is exactly the shape that mixes the two kinds in a single figure. It is
#: not an uncertainty, and saying so is the only way to publish it without
#: breaking the rule above.
LAB_ROW_NOT_AN_UNCERTAINTY: dict[str, str] = {
    "p2p_us": (
        "peak-to-peak of the excess-group-delay trace across the whole "
        "+/-1/3-octave neighbourhood — the BROAD detector run beside the local "
        "one, because a gentle all-pass lifts a neighbourhood together and the "
        "local metric reads that as flat. It is a signal, not a spread about a "
        "reading"
    ),
    "gate_slack": (
        "the per-gate DECISION threshold excess_loss_vs_null is tested "
        "against: the larger of the instrument's fixed retention slack and "
        "three times the paired retention standard error. It bounds a verdict "
        "rather than quantifying one, and reading it as an uncertainty would "
        "pool a systematic floor with a random scale"
    ),
}


def finite_number(value: Any) -> float | None:
    """One real number out of banked JSON, or ``None`` — never a coercion.

    ``bool`` is rejected because it is an ``int`` in Python; strings are
    rejected because ``float("1037")`` succeeds and would make this reader's
    strictness depend on whoever encoded the artifact. ``OverflowError`` is
    caught because an arbitrary-precision ``int`` is legal JSON, passes the
    isinstance check, and then raises rather than returning infinity.

    Public because the evidence packet asks the same question of a banked
    member curve's samples, and all three traps above are ones a second copy
    would have to keep remembering. One reader, one answer.
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

    A deliberate SUBSET of the lab row — :data:`LAB_ROW_FIELDS` is all of it,
    and the rest is working (per-gate retention, null-model scales, z-scores,
    centre shifts). That working is how the verdict was reached and it belongs
    in the artifact, and the evidence packet publishes it beside this view for
    a reader who wants to audit the call; a GATE that read it would be
    re-deriving a decision the classifier already made, with none of its
    controls. What is kept here is the verdict, the two tests behind it, how
    confident the classifier was, and the two measurements a prescriber's own
    bounds are taken from.
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
    #: ``"high"`` / ``"medium"`` / ``"low"`` — the shared
    #: :data:`~jasper.audio_measurement.quality_model.TrustLevel` words, as the
    #: classifier reported it. An artifact banked before 2026-08-22 carries the
    #: instrument's old ``"med"`` spelling in this column and is normalised to
    #: ``"medium"`` on the way in by :func:`read_feature_verdicts` — so a bar
    #: reading a typed verdict never has to know which era wrote it. Any other
    #: string is kept verbatim, same as ``classification``: an unknown value is
    #: evidence about the writer, not something to silently repair.
    confidence: str
    #: The feature's own measured Q, when the artifact carried one. This is
    #: what a prescriber should match a cut's width to; it is reported rather
    #: than enforced, because a filter narrower than its target is a different
    #: (and cheaper) mistake than one wider than it.
    measured_q: float | None
    #: How far the feature departs from its neighbours, dB, unsigned, when the
    #: artifact carried one — a DIP's own depth. Optional because the
    #: 2026-08-19 record does not carry it, which is why nothing gates on it:
    #: :mod:`.driver_prescription` bounded a boost by this number until
    #: 2026-08-23, and since NOT ONE row of that record carries one, the bar
    #: refused every boost the real record could have produced. It now rides
    #: the receipt's ``classification_basis`` for a reader to weigh.
    depth_db: float | None

    @property
    def is_defect_cuttable(self) -> bool:
        """Is a cut at least the right KIND of instrument for this feature?

        Not "will a cut help" — see this module's docstring and run-log §9.2.
        """
        return self.classification == DEFECT_CUTTABLE

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
        freq = finite_number(entry.get("hz"))
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
                confidence=_confidence(entry.get("confidence")),
                measured_q=finite_number(entry.get("measured_q")),
                depth_db=finite_number(entry.get("depth_db")),
            )
        )
    return tuple(out)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


#: The one legacy spelling this column has ever carried, and what it means now.
#:
#: The classifier wrote ``med`` until 2026-08-22 — alone against every sibling
#: answering the same "how much do I trust this number?" question, all of which
#: wrote ``medium``. The writer now emits ``medium``; artifacts banked before
#: the change are on disk forever, so the READER maps the old spelling and only
#: the reader does. Deliberately a one-entry table rather than an inline
#: ``if``: the next legacy spelling, if there is one, is a row here rather than
#: a second branch somewhere else.
_LEGACY_CONFIDENCE: dict[str, str] = {"med": "medium"}


def _confidence(value: Any) -> str:
    """A banked ``confidence`` column, in the current vocabulary.

    Tolerant in one direction only: a legacy spelling is normalised, anything
    else — including a value from an instrument this product has never seen —
    is passed through verbatim, because an unrecognised string is evidence
    about who wrote the artifact and repairing it would erase that.
    """
    text = _text(value)
    return _LEGACY_CONFIDENCE.get(text, text)


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
    the wrong tool when the classifier did not report the dip's depth — a boost
    bounded by no measured depth is bounded by policy alone.
    :mod:`.driver_prescription` requires it.
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
    target = finite_number(freq_hz)
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
