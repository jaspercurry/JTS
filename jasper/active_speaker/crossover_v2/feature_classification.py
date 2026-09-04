# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What KIND of feature is that, read from a banked verdict — nothing else.
The verdict FORMAT, not the pipeline: :mod:`.feature_classifier` computes the
number (``docs/active-speaker-tuning-layers-design.md`` stage P3 rule 1).
Verdict strings are the 2026-08-19 lab's spelling, character for character —
re-spelling would stop matching records on disk. ``defect-*`` is necessary,
NOT sufficient (run-log §9.2): the two bars remove features sign-of-filter is
the wrong instrument for; they do not recommend. An untypeable row is
dropped, never turned into ``ambiguous``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jasper.json_fields import finite_float

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
    "read_feature_verdicts",
]


#: The excess-group-delay test's answer. A feature whose excess group delay is
#: a large fraction of the matched non-minimum-phase scale is a cancellation,
#: and a filter aimed at one lowers the direct sound and the delayed copy
#: together. A row carries both ``egd_verdict`` (what the instrument ASSERTS)
#: and ``egd_verdict_raw`` (what the numbers said before the known-answer
#: controls gate it) — a different sense of ``_raw`` from the one
#: :mod:`.evidence_packet` uses.
EGD_MIN_PHASE = "MIN-PHASE"
EGD_NON_MIN_PHASE = "NON-MIN-PHASE"
EGD_AMBIGUOUS = "ambiguous"

#: The gate-invariance test's answer, against a matched-Q minimum-phase null
#: model so ordinary gate-driven shrinkage is subtracted rather than misread.
#: ``MOVED`` means the feature is a property of the window. A row whose
#: ladder did not run reports :data:`UNRESOLVED` instead: ``STABLE`` is a
#: finding, and reading an unrun test as one would vouch for a filter with
#: no window evidence at all.
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

#: The closed set, so a reader can say "this is not a verdict I know" rather
#: than silently treating an unknown string as a refusal it can explain.
CLASSIFICATIONS = frozenset({
    INTERFERENCE_BARRED,
    ROOM,
    DEFECT_CUTTABLE,
    DEFECT_BOOSTABLE,
    UNRESOLVED,
})


#: How far a prescribed centre frequency may sit from the classified feature
#: it claims to be aimed at, in octaves either side. A sixth of an octave:
#: the verdicts are read off fractional-octave-smoothed curves, so a centre
#: is not locatable finer than the smoothing width. It absorbs the
#: evidence's own locating error and does NOT keep a filter away from its
#: neighbours — two of the 2026-08-19 record's eight gaps (0.143 and 0.157
#: octaves) sit inside it, both peak–dip pairs, which is why
#: :func:`defect_cuttable_at` lets the NEAREST verdict decide. Symmetric in
#: octaves rather than Hz, like every other frequency tolerance here.
VERDICT_MATCH_TOLERANCE_OCTAVES = 1.0 / 6.0


#: Every column a classification row carries, in the order the instrument
#: writes them. :class:`FeatureVerdict` is the seven-key GATE view of it;
#: the rest is the working the two component verdicts were reached through.
#: It is also a READER'S allowlist: the evidence packet copies a banked row
#: field by field through it and publishes the names of anything it held
#: back, because the artifact may be an operator's own lab result.
#: ``tests/test_crossover_v2_feature_classifier.py`` pins this tuple against
#: a real :func:`~.feature_classifier.classify_round` run.
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
    "gate_notes",
    "controls_ok",
    "timing_corroborated",
    "gate_rungs",
    "gate_sensitivity",
    "pose_persistence",
    "decay",
    "fdw_rungs",
    "cycles_in_primary_gate",
)

#: The two kinds an uncertainty can be. RANDOM is repeat scatter and averages
#: down; SYSTEMATIC is a choice or bias the repeats all share, so measuring
#: again does not move it. Adding one to the other gives a number that is
#: neither.
UNCERTAINTY_RANDOM = "random"
UNCERTAINTY_SYSTEMATIC = "systematic"

#: The closed set, so a reader can say "that is not a kind I know" rather than
#: treating an unrecognised label as one of the two.
UNCERTAINTY_KINDS = frozenset({UNCERTAINTY_RANDOM, UNCERTAINTY_SYSTEMATIC})

#: The label for a spread that is real but is NOT one of the two kinds,
#: because it contains both and the evidence publishing it cannot separate
#: them. Deliberately not a member of :data:`UNCERTAINTY_KINDS`: a third
#: member would dress a refusal up as a third answer. A figure carrying it
#: is published apart from a block's ``fields`` list — see the evidence
#: packet's cross-seat sigma ``uncertainty.unseparated``.
UNCERTAINTY_UNSEPARATED = "unseparated"

#: Which :data:`LAB_ROW_FIELDS` columns ARE uncertainties, and of what. All
#: three are microsecond figures qualifying the same row's ``excursion_us``,
#: and no number here adds one to another. Both random entries are standard
#: DEVIATIONS, which converge as captures are added rather than falling;
#: what falls as ``1/sqrt(n)`` is the standard error of the pooled mean,
#: which this block does not publish ``n`` for.
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

#: Columns shaped like an uncertainty that are NOT one, named rather than
#: left out: ``gate_slack`` is a dB figure beside a dB reading, which
#: invites being read as that reading's error bar when it is the bar the
#: reading is TESTED against.
LAB_ROW_NOT_AN_UNCERTAINTY: dict[str, str] = {
    "p2p_us": (
        "peak-to-peak of the excess-group-delay trace across the whole "
        "+/-1/3-octave neighbourhood — the BROAD detector run beside the local "
        "one, because a gentle all-pass lifts a neighbourhood together and the "
        "local metric reads that as flat. It is a signal, not a spread about a "
        "reading"
    ),
    "gate_slack": (
        "the DECISION threshold, in dB, that excess_loss_vs_null is tested "
        "against: how far the null-model-corrected depth may move across the "
        "window ladder before the feature is called the room's. It bounds a "
        "verdict rather than quantifying one — a fixed instrument choice, not "
        "a spread about any reading"
    ),
}


@dataclass(frozen=True)
class FeatureVerdict:
    """One classified feature, as a gate reads it.

    A deliberate SUBSET of the lab row — :data:`LAB_ROW_FIELDS` is all of it,
    and the rest is the working. A gate that read the working would be
    re-deriving a decision the classifier already made, with none of its
    controls; the evidence packet publishes it beside this view for an auditor.
    """

    #: The feature's centre frequency.
    freq_hz: float
    #: One of :data:`CLASSIFICATIONS`, or any other string the artifact carried.
    #: An unknown value is kept so a refusal can quote it; it satisfies no bar.
    classification: str
    #: The two component verdicts, so a refusal can name which test barred the
    #: feature rather than only that something did.
    egd_verdict: str
    gate_verdict: str
    #: ``"high"`` / ``"medium"`` / ``"low"`` — the shared
    #: :data:`~jasper.audio_measurement.quality_model.TrustLevel` words. An
    #: artifact banked before 2026-08-22 carries ``"med"`` and is normalised on
    #: the way in by :func:`read_feature_verdicts`; any other string is kept
    #: verbatim, since an unknown value is evidence about the writer.
    confidence: str
    #: The feature's own measured Q, when the artifact carried one — what a cut's
    #: width should match. Reported rather than enforced: a filter narrower than
    #: its target is a different and cheaper mistake than one wider than it.
    measured_q: float | None
    #: How far the feature departs from its neighbours, dB, unsigned, when the
    #: artifact carried one — a DIP's own depth. Optional because no row of the
    #: 2026-08-19 record carries it, which is why nothing gates on it; it rides
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

        The frequency key is ``hz`` — the banked artifact's own spelling — so this
        output is itself readable by :func:`read_feature_verdicts`.
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

    ``raw`` is the artifact's ``rows`` list or the whole artifact mapping: the
    packet hands one, a durable read-back the other. A row without a readable
    frequency or classification is DROPPED rather than kept as ``ambiguous`` —
    an unreadable record and one the classifier genuinely could not resolve
    are different facts, and only the second is evidence.
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
        freq = finite_float(entry.get("hz"))
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
                measured_q=finite_float(entry.get("measured_q")),
                depth_db=finite_float(entry.get("depth_db")),
            )
        )
    return tuple(out)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


#: The one legacy spelling this column has ever carried. The classifier wrote
#: ``med`` until 2026-08-22 and those artifacts are on disk forever, so the
#: READER maps it and only the reader does. A one-entry table rather than
#: an inline ``if``: the next legacy spelling is a row here.
_LEGACY_CONFIDENCE: dict[str, str] = {"med": "medium"}


def _confidence(value: Any) -> str:
    """A banked ``confidence`` column, in the current vocabulary.

    Tolerant in one direction only: a legacy spelling is normalised, anything
    else is passed through verbatim, because an unrecognised string is
    evidence about who wrote the artifact and repairing it would erase that.
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

    **THE NEAREST VERDICT DECIDES.** ``nearest`` is the closest verdict inside
    ``tolerance_octaves``, whatever it said, or ``None`` when nothing was
    classified there; ``vouching`` is that same verdict only when its
    classification is :data:`DEFECT_CUTTABLE`, never a more agreeable one
    standing further away. Two of the 2026-08-19 record's eight gaps are
    narrower than the tolerance and both are peak–dip pairs, so an
    any-in-radius rule lets a cut aimed at the 4582 Hz minimum-phase dip cite
    the 4149 Hz peak, and cutting a minimum-phase dip deepens it.

    Both values are returned so a refusal can tell "nothing here was ever
    classified" from "the feature there is a minimum-phase dip". Ties fail
    closed, away from cuttable. A non-positive or non-finite ``freq_hz``
    matches nothing rather than raising.
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
    class, through the same helper so the two cannot drift: nearest inside
    ``tolerance_octaves`` decides, a tie fails closed, and a non-positive or
    non-finite ``freq_hz`` matches nothing. ``vouching`` is necessary and NOT
    sufficient — :mod:`.driver_prescription` also requires a measured depth,
    since a boost bounded by none is bounded by policy alone.
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
    target = finite_float(freq_hz)
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
