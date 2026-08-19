# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE blend-region shape correction, prescribed from outside this process.

The third member of a family, and it is worth naming all three before reading
any of it, because the whole design is "be the same thing the other two are":

===========================  =============================  ==================
quantity                     computed here                   prescribed here
===========================  =============================  ==================
inter-driver delay           ``program_analysis``'s aligner  ``.alignment_prescription``
blend-region shape           :mod:`.blend_correction`        **this module**
===========================  =============================  ==================

:mod:`.alignment_prescription` exists because a bench measurement could say the
delay better than the estimator could.  This module exists for the mirror
reason: a reader with the round's whole evidence in front of it — a person, or
a language model the operator is talking to — may see a shape the deterministic
solver's greedy two-cut fit cannot express.  Neither module lets that reader
*act*.  Both turn an outside opinion into **an ordinary candidate**, measured
and graded by exactly the machinery every automatic round goes through.

**The model proposes; the harness disposes.**  Nothing here applies anything,
loads a graph, or grades a round.  It reads one JSON document as hostile data
and returns either a validated :class:`BlendPrescription` or a refusal naming
which gate said no.

**Why the refusal vocabulary is closed and machine-readable.**  A prescriber
that cannot tell *why* it was refused cannot correct itself, and the loop this
module serves is a loop precisely because the next proposal should be better
than the last.  Every refusal below carries a slug from
:data:`PRESCRIPTION_REFUSAL_REASONS` beside a human sentence — the same
by-type-never-by-prose rule
:class:`~.contracts.CrossoverV2ContractError` and
:class:`~.alignment_prescription.AlignmentPrescriptionRefused` already follow.

**What the gate is measured against, and why it is the packet.**
:func:`read_blend_prescription` takes the evidence packet the prescriber
answered (:mod:`.evidence_packet`) and checks the proposal against *that
document's own numbers* — its crossover region, its per-position curves, its
flat reference.  Two things fall out of that one choice:

* The bound is anchored on declared evidence rather than on the incumbent, for
  :mod:`.alignment_prescription`'s reason: a bound measured from what the
  speaker currently plays forbids exactly the correction a bad incumbent needs.
* Provenance is content-addressed instead of asserted.  The alignment
  prescription requires ``basis_artifacts`` — names a human can go and check.
  Here the basis is ONE artifact and it has a fingerprint, so the proposal
  states :data:`PACKET_FINGERPRINT_FIELD` and the gate compares it to the
  packet in hand.  A proposal answering a different round is refused rather
  than silently graded against evidence it never saw, which no list of names
  could catch.

**Cuts and boosts are different classes, and the receipt says which.**  A cut
in this region is the shipped deterministic behaviour
(:mod:`.blend_correction`).  A boost is a NEW permission, opened by owner
ruling on 2026-08-18, and it is deliberately not laundered into looking like
the old one: :attr:`BlendPrescription.prescription_class` is ``"boost"`` for
any proposal containing a positive gain, so a later comparison of
"deterministic vs prescribed" rounds can attribute an outcome to the class that
produced it.  A reader who cannot tell those apart cannot learn anything from
the series.

**What a boost must prove, and why each leg is here.**  The physics is that a
dip has two causes and only one of them can be filled.  A minimum-phase
shortfall — a driver, a box, a baffle radiating less energy there — is fixed by
adding energy.  An interference null is direct sound cancelling a delayed copy,
and boosting it lifts the reflection along with the direct sound, so the null
swallows whatever you feed it.  The instrument that separates those per bin
does not exist yet (it is the queued excess-phase work).  What DOES exist is
the observation that the two behave differently **across positions**: an
interference null moves with the microphone, a radiating shortfall does not.
:func:`positional_support` makes that the deterministic stand-in — the
null-exclusion rule without the null instrument — and
:data:`BOOST_MIN_TESTIFYING_POSITIONS` says why "all but one" needs three
positions to mean anything.

**Denominator visibility is not decoration here.**  A position whose own gate
put its validity floor above the proposed frequency cannot testify about that
frequency, so it is removed from the denominator rather than counted as a
position that failed to see the dip. :class:`PositionalSupport` reports the
count it removed and why, for the reason ``variance_cap`` and the flat-spec
views report theirs: a fraction whose denominator moved silently is a different
measurement wearing the same number.

**Fail-closed, never clamped.**  Every refusal raises and names itself.  A
proposal outside a bound is not pulled to the boundary and run:
:mod:`.alignment_prescription`'s reason applies unchanged — the operator asked
for a shape, and a silently different shape is worse than none, because the
receipt would carry this one's name.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

# Leaf of the crossover_v2 DAG, exactly like the two modules it is a sibling
# of: no session, no flow, no web. Its dependencies are the ONE biquad
# evaluator and the module that owns the deterministic version of this same
# quantity — whose bounds are imported rather than restated.
from jasper.active_speaker.branch_chain import chain_response

from .blend_correction import (
    BLEND_FILTER_Q,
    BLEND_MAX_FILTER_CUT_DB,
    BLEND_MAX_FILTERS,
    BLEND_MAX_TOTAL_CUT_DB,
    BLEND_MIN_CUT_DB,
    blend_filters_from_mapping,
)

__all__ = [
    "BOOST_MIN_DIP_DB",
    "BOOST_MIN_TESTIFYING_POSITIONS",
    "PRESCRIPTION_KIND",
    "PRESCRIPTION_MAX_BYTES",
    "PRESCRIPTION_MAX_FILTER_BOOST_DB",
    "PRESCRIPTION_MAX_TOTAL_BOOST_DB",
    "PRESCRIPTION_MIN_Q",
    "PRESCRIPTION_REFUSAL_REASONS",
    "PRESCRIPTION_SCHEMA_VERSION",
    "BlendPrescription",
    "BlendPrescriptionRefused",
    "PositionalSupport",
    "blend_prescription_from_mapping",
    "positional_support",
    "prescription_response_format",
    "read_blend_prescription",
]


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

#: The document version a prescriber answers. Bumped when the shape changes in
#: a way an older prescriber's output would no longer satisfy; a proposal
#: naming a version this build does not speak is refused rather than
#: best-effort parsed, because "the field I needed was renamed" is exactly the
#: failure a lenient reader converts into a silently truncated prescription.
PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, mirroring the room advisor's
#: ``jts_advisor_response``. A document that does not name itself is refused:
#: this reader is pointed at operator-supplied files and stdin, and "that was
#: the wrong file" is a likelier mistake than a malformed one.
PRESCRIPTION_KIND = "jts_crossover_blend_prescription"

#: The packet field a proposal must echo back. Named here because the reader
#: that owns the shape owns the name of the shape — the same rule
#: :data:`~.alignment_prescription.ALIGNMENT_PRESCRIPTION_KEY` follows.
PACKET_FINGERPRINT_FIELD = "packet_fingerprint"

#: Byte ceiling on one proposal document, read before it is parsed.
#:
#: The largest legitimate proposal is two filters (four numbers each), a
#: provenance block, and one bounded rationale sentence — under 1 KiB written
#: generously. 64 KiB is roughly two orders of magnitude of slack, which is the
#: right size for a cap whose job is to stop a pathological input from being
#: parsed at all rather than to police formatting. The cap lives with the
#: reader, not with the transport, because this reader is reached from a file,
#: from stdin, and (later) from a POST body, and a cap that only one of those
#: enforced would be a cap the other two do not have.
PRESCRIPTION_MAX_BYTES = 64 * 1024

#: Ceiling on the free-text rationale, in characters. Mirrors
#: ``calibration_agent.response.TEXT_LIMIT_CHARS``. The text is stored and
#: never parsed for behaviour (see :attr:`BlendPrescription.rationale`), so the
#: cap is about bounding what gets banked, not about trusting it.
RATIONALE_MAX_CHARS = 1_200


# --------------------------------------------------------------------------- #
# bounds — the cut bounds are IMPORTED, the boost bounds are new
# --------------------------------------------------------------------------- #

# A prescribed CUT is bounded by exactly what the deterministic solver bounds
# itself by. These are imported rather than restated so that a prescriber can
# never be granted a cut the shipped solver would refuse to emit, and so that
# re-deriving one of those bounds moves both users at once. Re-exported under
# their own names would be a second vocabulary for one fact; the module refers
# to `BLEND_*` throughout instead.

#: Widest Q one prescribed filter may use — the deterministic solver's own
#: emitted Q, imported. That number is not arbitrary in
#: :mod:`.blend_correction`: a narrower filter "chases a feature this
#: instrument cannot resolve and the room will not reproduce off-axis", and
#: 2.0 is the Q every series-1 fit actually realized on hardware. A prescriber
#: allowed past it would be allowed a shape the shipped solver forbids itself,
#: which is a weakening of the region contract by the back door.
PRESCRIPTION_MAX_Q = BLEND_FILTER_Q

#: Narrowest Q one prescribed filter may use. Below this a Peaking filter is a
#: broadband tilt rather than a shape correction, and broadband level is the
#: trim's fact (decision 10 clause (c)) — a prescriber reaching for it is
#: answering a question this seam does not own.
PRESCRIPTION_MIN_Q = 0.5

#: Per-filter BOOST ceiling, dB. Owner ruling, 2026-08-18: the boost class is
#: opened at 3.0 dB rather than at the 12.0 dB the established in-band
#: linearization boost already enjoys, because a new permission should not open
#: at the old permission's ceiling.
#:
#: **Deliberately a separate constant from** :data:`BLEND_MAX_FILTER_CUT_DB`,
#: which is 3.0 dB today as well. The equality is a coincidence of two
#: different derivations: the cut ceiling is "what the blind zone was shown to
#: hide, plus one model error", traceable to the series-1 measurements; this
#: one is a conservative opening bar set by ruling and expected to move on
#: evidence. Collapsing them onto one name would make a later ruling about
#: boosts silently re-tune what the deterministic solver may cut.
#:
#: Tunable by ruling, not by a caller: there is no keyword that widens it.
PRESCRIPTION_MAX_FILTER_BOOST_DB = 3.0

#: Ceiling on the COMPOSED boost's peak over the region, dB — the same shape as
#: :data:`BLEND_MAX_TOTAL_CUT_DB`, enforced the same way: on the evaluated
#: cascade rather than on a sum of gains, because two boosts whose skirts
#: overlap deliver more than either alone.
#:
#: Separate from the cut ceiling for the reason above. Its own justification is
#: that headroom is finite and shared: every dB of boost is charged against the
#: emitter's own positive-boost accounting, so this ceiling's job is to keep one
#: prescription from consuming a budget the rest of the graph needs. It is the
#: FIRST of two independent bounds, never the only one — the emitter re-charges
#: the composed graph's boost at the graph boundary and refuses there too.
PRESCRIPTION_MAX_TOTAL_BOOST_DB = 4.0

#: How deep a per-position deviation must be to count as "the dip is present
#: here", dB. :data:`BLEND_MIN_CUT_DB` imported under a reading rather than
#: restated: it is this program's own measured model-tracking error, the
#: smallest dB it can honestly claim to have observed, and a "dip" shallower
#: than that is indistinguishable from the instrument.
BOOST_MIN_DIP_DB = BLEND_MIN_CUT_DB

#: The fewest positions that must be able to testify about a frequency before
#: the all-but-one rule means anything.
#:
#: Three, and the arithmetic is the argument. "Present at every measured
#: position but at most one" is *vacuous* at two positions — it admits a dip
#: seen at exactly one of them, which is precisely the single-point artifact
#: the rule exists to exclude — and undefined at one. Three is the smallest
#: count at which the rule can refuse anything, so it is the floor. Below it
#: the answer is :data:`INSUFFICIENT_POSITIONAL_EVIDENCE`: not "no", but "go
#: and measure", which is a different instruction and the prescriber can act
#: on it.
BOOST_MIN_TESTIFYING_POSITIONS = 3

#: How many positions may miss the dip and still leave it supported. One, per
#: the proposed bar. Kept as a named constant beside the count above because
#: the two only make sense read together.
BOOST_MAX_DISSENTING_POSITIONS = 1


# --------------------------------------------------------------------------- #
# refusal vocabulary — closed, by slug, never by prose
# --------------------------------------------------------------------------- #

PRESCRIPTION_TOO_LARGE = "prescription_too_large"
PRESCRIPTION_MALFORMED = "prescription_malformed"
PRESCRIPTION_SCHEMA_UNSUPPORTED = "prescription_schema_unsupported"
PRESCRIPTION_PACKET_MISMATCH = "prescription_packet_mismatch"
PRESCRIPTION_PROVENANCE_MISSING = "prescription_provenance_missing"
PRESCRIPTION_PROHIBITED_FIELD = "prescription_prohibited_field"
FILTER_MALFORMED = "filter_malformed"
FILTER_COUNT_EXCEEDED = "filter_count_exceeded"
FILTER_OUTSIDE_REGION = "filter_outside_region"
FILTER_Q_OUT_OF_RANGE = "filter_q_out_of_range"
FILTER_CUT_TOO_DEEP = "filter_cut_too_deep"
FILTER_BOOST_TOO_HIGH = "filter_boost_too_high"
COMPOSED_CUT_EXCEEDED = "composed_cut_exceeded"
COMPOSED_BOOST_EXCEEDED = "composed_boost_exceeded"
REGION_UNAVAILABLE = "region_unavailable"
INSUFFICIENT_POSITIONAL_EVIDENCE = "insufficient_positional_evidence"
BOOST_DIP_NOT_STABLE = "boost_dip_not_stable"
STRICT_READER_DISAGREEMENT = "strict_reader_disagreement"

PRESCRIPTION_REFUSAL_REASONS = frozenset({
    PRESCRIPTION_TOO_LARGE,
    PRESCRIPTION_MALFORMED,
    PRESCRIPTION_SCHEMA_UNSUPPORTED,
    PRESCRIPTION_PACKET_MISMATCH,
    PRESCRIPTION_PROVENANCE_MISSING,
    PRESCRIPTION_PROHIBITED_FIELD,
    FILTER_MALFORMED,
    FILTER_COUNT_EXCEEDED,
    FILTER_OUTSIDE_REGION,
    FILTER_Q_OUT_OF_RANGE,
    FILTER_CUT_TOO_DEEP,
    FILTER_BOOST_TOO_HIGH,
    COMPOSED_CUT_EXCEEDED,
    COMPOSED_BOOST_EXCEEDED,
    REGION_UNAVAILABLE,
    INSUFFICIENT_POSITIONAL_EVIDENCE,
    BOOST_DIP_NOT_STABLE,
    STRICT_READER_DISAGREEMENT,
})

#: Top-level fields a proposal may carry. Anything else is refused rather than
#: ignored, on :data:`~.alignment_prescription._PRESCRIPTION_FIELDS`' reason: a
#: misspelled ``filters`` that silently dropped the whole prescription would
#: leave the gate cheerfully accepting an empty one.
_PRESCRIPTION_FIELDS = frozenset({
    "artifact_schema_version",
    "kind",
    PACKET_FINGERPRINT_FIELD,
    "prescriber",
    "filters",
    "rationale",
})

#: Fields ONE filter may carry — the reduced record
#: :func:`~jasper.active_speaker.branch_chain.chain_response`, the emitter, and
#: :func:`~.blend_correction.blend_filters_from_mapping` all speak. A
#: prescriber writes into the shape the machinery already reads; there is no
#: translation layer here to get wrong.
_FILTER_FIELDS = frozenset({"biquad_type", "freq", "q", "gain"})

#: Keys no proposal may contain at any depth. Adopted wholesale from
#: ``calibration_agent.response._PROHIBITED_KEYS`` — the same recursive
#: blocklist guarding the same class of attempt (a model reaching past "numbers
#: into a fixed shape" toward config, coefficients, or execution), extended
#: with the two this domain owns. Copied rather than imported: importing would
#: make this leaf depend on a package that pulls in the sound-profile
#: substrate and an OpenAI client, and the shared thing is the vocabulary
#: rather than the tuple. ``tests/test_crossover_v2_blend_prescription.py``
#: pins that the room set stays a subset, so the two cannot drift apart
#: silently.
_PROHIBITED_KEYS = frozenset({
    "audio_bytes",
    "camilladsp_config",
    "camilladsp_yaml",
    "coefficients",
    "command",
    "dsp_yaml",
    "execute",
    "fir_coefficients",
    "fir_taps",
    "raw_audio",
    "shell",
    "set_config_file_path",
    "set_volume",
    "volume",
    "volume_db",
    "yaml",
    # This domain's own two: a prescriber may not name the delay (that is
    # `alignment_prescription`'s gate, with its own bound) and may not name a
    # role (the blend region is common-mode by construction — see
    # `blend_correction`'s scope tripwire).
    "delay_us",
    "role_attenuations_db",
})


class BlendPrescriptionRefused(ValueError):
    """One prescription this module would not accept, and why.

    Carries a ``reason`` from :data:`PRESCRIPTION_REFUSAL_REASONS` beside the
    human ``detail``, and — for the refusals a prescriber can act on — an
    ``evidence`` mapping saying what was actually measured. A model told only
    "refused: boost_dip_not_stable" can guess; one told "3 of 4 testifying
    positions saw the dip, 4 needed" can fix its own proposal.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.evidence: Mapping[str, Any] = dict(evidence or {})

    def to_dict(self) -> dict[str, Any]:
        """The refusal as the prescriber reads it back."""
        return {
            "accepted": False,
            "reason": self.reason,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class PositionalSupport:
    """Whether one frequency's dip is a property of the speaker or the seat.

    The deterministic stand-in for the per-bin minimum-phase classifier that
    does not exist yet. Its whole content is a fraction and the disclosure of
    how that fraction's denominator was arrived at.
    """

    #: The proposed centre frequency this was evaluated at, and the grid bin
    #: actually read. They differ by up to half a bin; stating both is what
    #: lets a reader tell a real answer from one snapped to a distant bin.
    freq_hz: float
    evaluated_at_hz: float
    #: Every position in the packet's cloud.
    n_positions: int
    #: Those whose own validity floor admits :attr:`evaluated_at_hz`. The
    #: denominator. A position that cannot testify is REMOVED rather than
    #: counted as one that failed to see the dip — the opposite convention
    #: would let a low-frequency proposal fail for lack of evidence it was
    #: never entitled to.
    n_testifying: int
    #: Those testifying positions whose deviation at that bin is at least
    #: :data:`BOOST_MIN_DIP_DB` below the flat reference.
    n_with_dip: int
    #: Why testifying positions were removed, when any were. Empty otherwise.
    excluded_reason: str

    @property
    def supported(self) -> bool:
        """The all-but-one rule, over a denominator large enough to mean it."""
        if self.n_testifying < BOOST_MIN_TESTIFYING_POSITIONS:
            return False
        dissenting = self.n_testifying - self.n_with_dip
        return dissenting <= BOOST_MAX_DISSENTING_POSITIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "freq_hz": self.freq_hz,
            "evaluated_at_hz": self.evaluated_at_hz,
            "n_positions": self.n_positions,
            "n_testifying": self.n_testifying,
            "n_with_dip": self.n_with_dip,
            "excluded_reason": self.excluded_reason,
            "min_testifying_positions": BOOST_MIN_TESTIFYING_POSITIONS,
            "max_dissenting_positions": BOOST_MAX_DISSENTING_POSITIONS,
            "min_dip_db": BOOST_MIN_DIP_DB,
            "supported": self.supported,
        }


@dataclass(frozen=True)
class BlendPrescription:
    """A validated blend-region correction and the evidence that justifies it.

    ``filters`` is a TOTAL, not a delta — the whole correction the next round
    applies, incumbent included, exactly as
    :attr:`~.blend_correction.BlendCorrection.filters` is. A prescriber reading
    the packet sees the incumbent it must account for.
    """

    #: The prescribed biquads, in emission order, normalized to the reduced
    #: record the emitter reads.
    filters: tuple[dict[str, Any], ...]
    #: ``"cut"`` when every gain is non-positive, ``"boost"`` when any gain is
    #: positive. The receipt's attribution key: the two classes are graded the
    #: same way and must stay separable when the series is read back.
    prescription_class: str
    #: The packet fingerprint this answered, so a round receipt names the exact
    #: evidence document the prescription was derived from.
    packet_fingerprint: str
    #: Who authored it. Both fields required — a prescription with no author is
    #: a number with no way to ask about it later.
    prescriber_model: str
    prescriber_operator: str
    #: The region the proposal was checked against, echoed from the packet.
    band_hz: tuple[float, float]
    #: The positional-support finding for each boost, in filter order. Empty
    #: for a cut-class prescription: cuts do not need it, because cutting a
    #: null makes the region flatter at every position rather than feeding one.
    positional_support: tuple[PositionalSupport, ...] = ()
    #: The prescriber's own words. **Never parsed for behaviour** — no branch
    #: in this module or any caller reads it, and it is excluded by
    #: construction from every instruction this harness renders (see
    #: :func:`prescription_response_format`). It exists so a human reading a
    #: receipt six weeks later can see what the model thought it was doing.
    rationale: str = ""

    @property
    def is_boost(self) -> bool:
        return self.prescription_class == "boost"

    def to_dict(self) -> dict[str, Any]:
        """The receipt's view: what was prescribed, and what justifies it."""
        return {
            "artifact_schema_version": PRESCRIPTION_SCHEMA_VERSION,
            "kind": PRESCRIPTION_KIND,
            "prescription_class": self.prescription_class,
            "filters": [dict(f) for f in self.filters],
            "band_hz": [self.band_hz[0], self.band_hz[1]],
            PACKET_FINGERPRINT_FIELD: self.packet_fingerprint,
            "prescriber": {
                "model": self.prescriber_model,
                "operator": self.prescriber_operator,
            },
            "positional_support": [s.to_dict() for s in self.positional_support],
            "rationale": self.rationale,
        }


# --------------------------------------------------------------------------- #
# the response format — ONE owner, two consumers
# --------------------------------------------------------------------------- #


def prescription_response_format() -> dict[str, Any]:
    """The contract a prescriber must satisfy, as data.

    Rendered into the evidence packet's ``response_format`` block by
    :mod:`.evidence_packet` and enforced by :func:`read_blend_prescription`
    here — one owner, so the instructions a prescriber is given and the gate it
    is judged by cannot describe different shapes. It is the same job
    ``calibration_agent.response.response_contract`` does for the room domain,
    and it deliberately mirrors that function's shape rather than inventing a
    second way to say the same thing.

    **It is a pure constant.** Nothing banked, measured, or household-authored
    reaches it, which is what makes prompt injection through the packet
    structurally impossible rather than merely filtered: a packet's
    instructions are these bytes whatever the round measured.
    ``tests/test_crossover_v2_blend_prescription.py`` pins that by comparing
    the block across packets built from different evidence.
    """
    return {
        "artifact_schema_version": PRESCRIPTION_SCHEMA_VERSION,
        "kind": "jts_crossover_blend_prescription_contract",
        "required_top_level": {
            "artifact_schema_version": PRESCRIPTION_SCHEMA_VERSION,
            "kind": PRESCRIPTION_KIND,
            PACKET_FINGERPRINT_FIELD: (
                "copy the packet's own fingerprint field verbatim; a "
                "prescription that names a different packet is refused"
            ),
            "prescriber": {
                "model": "the model that authored this, e.g. 'claude-opus-5'",
                "operator": "the person who ran it",
            },
            "filters": (
                f"0 to {BLEND_MAX_FILTERS} objects, each exactly "
                "{biquad_type: 'Peaking', freq: <Hz>, q: <number>, "
                "gain: <dB>}"
            ),
        },
        "optional_top_level": {
            "rationale": (
                f"free text, at most {RATIONALE_MAX_CHARS} characters. It is "
                "stored for a human reader and is NEVER parsed for behaviour: "
                "no argument made here can widen a bound below."
            ),
        },
        "filters_are_a_total": (
            "prescribe the WHOLE correction the next round should apply, not a "
            "delta against the incumbent. The packet states the incumbent the "
            "measurement was taken through."
        ),
        "bounds": {
            "max_filters": BLEND_MAX_FILTERS,
            "biquad_type": "Peaking",
            "q_min": PRESCRIPTION_MIN_Q,
            "q_max": PRESCRIPTION_MAX_Q,
            "freq_must_be_inside": "the packet's crossover_region.band_hz",
            "max_filter_cut_db": BLEND_MAX_FILTER_CUT_DB,
            "max_composed_cut_db": BLEND_MAX_TOTAL_CUT_DB,
            "max_filter_boost_db": PRESCRIPTION_MAX_FILTER_BOOST_DB,
            "max_composed_boost_db": PRESCRIPTION_MAX_TOTAL_BOOST_DB,
            "composed_caps_are_evaluated": (
                "the composed caps are checked on the evaluated biquad "
                "cascade over the region, not on a sum of gains: two filters "
                "whose skirts overlap deliver more than either alone"
            ),
        },
        "boost_bar": {
            "note": (
                "a positive gain is a distinct prescription class and must "
                "clear an extra, positional bar. A dip that appears at only "
                "one seat is presumed to be an interference null, which "
                "swallows added energy instead of being filled by it."
            ),
            "min_testifying_positions": BOOST_MIN_TESTIFYING_POSITIONS,
            "max_dissenting_positions": BOOST_MAX_DISSENTING_POSITIONS,
            "min_dip_db": BOOST_MIN_DIP_DB,
            "denominator": (
                "positions whose own validity floor sits above the proposed "
                "frequency cannot testify and are removed from the "
                "denominator; the refusal reports both counts"
            ),
            "if_evidence_is_missing": (
                f"refused as '{INSUFFICIENT_POSITIONAL_EVIDENCE}' rather than "
                "as a no — measure more positions and propose again"
            ),
        },
        "refusal_reasons": sorted(PRESCRIPTION_REFUSAL_REASONS),
        "prohibited_keys": sorted(_PROHIBITED_KEYS),
        "execution_boundary": {
            "model_may_propose": True,
            "model_may_execute": False,
            "model_may_grade_itself": False,
            "jts_validates_and_measures": True,
            "note": (
                "an accepted prescription becomes an ordinary measured "
                "candidate: the same admission, safety-envelope, headroom, "
                "variance and verify gates every automatic round faces, and "
                "the round's own adoption verdict decides keep or restore"
            ),
        },
    }


# --------------------------------------------------------------------------- #
# the positional bar
# --------------------------------------------------------------------------- #


def positional_support(
    freq_hz: float,
    *,
    positions: Sequence[Mapping[str, Any]],
    freqs_hz: Sequence[float],
    reference_db: float,
) -> PositionalSupport:
    """Is the dip at ``freq_hz`` a property of the speaker or of one seat?

    Reads the system's OWN persisted per-position curves against the system's
    OWN flat reference, with no re-smoothing and no re-derivation — the
    invariant the lab's ``dip_table.py`` held and the reason its numbers could
    be trusted beside the round's. Every argument comes from one packet, so
    the curves and the reference cannot come from different evaluations.

    ``positions`` entries are the packet's position records: ``magnitude_db``
    on the shared ``freqs_hz`` grid, plus ``validity_floor_hz`` where the
    capture's own gate established one.
    """
    grid = np.asarray(freqs_hz, dtype=np.float64)
    if grid.ndim != 1 or grid.size == 0 or not np.all(np.isfinite(grid)):
        return PositionalSupport(
            freq_hz=float(freq_hz), evaluated_at_hz=float("nan"),
            n_positions=len(positions), n_testifying=0, n_with_dip=0,
            excluded_reason="the packet carries no usable frequency grid",
        )
    index = int(np.argmin(np.abs(grid - float(freq_hz))))
    evaluated_at = float(grid[index])

    testifying = 0
    with_dip = 0
    below_floor = 0
    unreadable = 0
    for entry in positions:
        if not isinstance(entry, Mapping):
            unreadable += 1
            continue
        magnitude = entry.get("magnitude_db")
        if not isinstance(magnitude, Sequence) or isinstance(magnitude, (str, bytes)):
            unreadable += 1
            continue
        if len(magnitude) != grid.size:
            unreadable += 1
            continue
        # A position's own gate may have established a floor below which its
        # curve is not evidence. Asking it about a frequency under that floor
        # is asking for an answer it already declined to give.
        floor = entry.get("validity_floor_hz")
        if (
            isinstance(floor, (int, float))
            and not isinstance(floor, bool)
            and math.isfinite(float(floor))
            and evaluated_at < float(floor)
        ):
            below_floor += 1
            continue
        value = magnitude[index]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            unreadable += 1
            continue
        value = float(value)
        if not math.isfinite(value):
            unreadable += 1
            continue
        testifying += 1
        if value - reference_db <= -BOOST_MIN_DIP_DB:
            with_dip += 1

    reasons = []
    if below_floor:
        reasons.append(
            f"{below_floor} position(s) have a validity floor above "
            f"{evaluated_at:.1f} Hz"
        )
    if unreadable:
        reasons.append(f"{unreadable} position curve(s) were unreadable")
    return PositionalSupport(
        freq_hz=float(freq_hz),
        evaluated_at_hz=evaluated_at,
        n_positions=len(positions),
        n_testifying=testifying,
        n_with_dip=with_dip,
        excluded_reason="; ".join(reasons),
    )
