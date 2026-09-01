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
:data:`BLEND_PRESCRIPTION_REFUSAL_REASONS` beside a human sentence — the same
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
swallows whatever you feed it.  :mod:`.feature_classifier` separates those, but
per detected FEATURE rather than per bin, and only for a round somebody
classified offline — it is the per-DRIVER class's bar, and nothing has wired it
to this one.  What this class has is the observation that the two behave
differently **across positions**: an interference null moves with the
microphone, a radiating shortfall does not.
:func:`positional_support` makes that the deterministic stand-in — the
null-exclusion rule without the null instrument — and
:data:`BOOST_MIN_TESTIFYING_POSITIONS` says why "all but one" needs three
positions to mean anything.

**And the stand-in is weaker than the instrument it stands in for, measured
rather than assumed.**  Run over the 2026-08-18 blend round (four positions,
roughly ±7° and ±22°), the bar discriminates sharply at most frequencies — 1
of 4 positions at 1210 Hz, 0 of 4 at 3232 Hz — and passes 4 of 4 at 1018 Hz
and 1616 Hz.  It is a *spatial* test, so its power is bounded by the angular
spread of the cloud it reads: a feature that moves with the microphone more
slowly than that spread reads as stable.  A tightly-clustered walk can
therefore support a boost :mod:`.feature_classifier` would classify as
interference.  That is one of the two reasons
:func:`prescription_route` refuses the boost class outright today rather than
treating this bar as sufficient — the bar's job is to say whether a proposal
*would* qualify, not to authorize it.

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
from typing import Any, NoReturn

import numpy as np

# Leaf of the crossover_v2 DAG, exactly like the two modules it is a sibling
# of: no session, no flow, no web. Its dependencies are the ONE biquad
# evaluator and the module that owns the deterministic version of this same
# quantity — whose bounds are imported rather than restated.
from jasper.active_speaker.branch_chain import chain_response
# The evaluable Q range a Q-parameterised filter can actually be realized
# at — owned by the ONE biquad evaluator, imported rather than restated so a
# door's ceiling and the arithmetic it protects cannot drift apart.
from jasper.sound.profile import EVALUABLE_Q_MAX, EVALUABLE_Q_MIN

from .blend_correction import (
    BLEND_FILTER_Q,
    BLEND_MAX_FILTERS,
    BLEND_MIN_CUT_DB,
    blend_filters_from_mapping,
)

#: The three-part answer :func:`~.evidence_packet.packet_positional_evidence`
#: returns: the per-position records, their shared frequency grid, and the flat
#: reference they are read against. Aliased here because this module is the one
#: that consumes it and a bare triple in a signature says nothing about why the
#: three travel together — they are only meaningful as one evaluation's output.
PositionalEvidence = tuple[list[dict[str, Any]], list[float], float]

__all__ = [
    "BLEND_CANDIDATE_FIELD",
    "BLEND_PRESCRIPTION_MALFORMED",
    "BLEND_PRESCRIPTION_PROVENANCE_MISSING",
    "BLEND_PRESCRIPTION_REFUSAL_REASONS",
    "BOOST_MIN_DIP_DB",
    "BOOST_MIN_TESTIFYING_POSITIONS",
    "BOOST_ROUTE_UNAVAILABLE",
    "PRESCRIPTION_KIND",
    "PRESCRIPTION_MAX_BOOST_Q",
    "PRESCRIPTION_MAX_BYTES",
    "PRESCRIPTION_MAX_FILTER_BOOST_DB",
    "PRESCRIPTION_MAX_TOTAL_BOOST_DB",
    "PRESCRIPTION_SCHEMA_VERSION",
    "PROHIBITED_PRESCRIPTION_KEYS",
    "BlendPrescription",
    "BlendPrescriptionRefused",
    "PositionalEvidence",
    "PositionalSupport",
    "blend_prescription_from_mapping",
    "blend_prescription_to_candidate_fields",
    "find_prohibited_keys",
    "max_q_for_gain",
    "positional_support",
    "prescription_response_format",
    "prescription_route",
    "prescription_sha256",
    "read_blend_prescription",
    "read_prescription_bytes",
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
#: **It truncates and discloses rather than refusing** (ADR-0207) —
#: ``driver_prescription.RATIONALE_MAX_CHARS``'s 2026-08-29 demotion, applied
#: to this door's copy of the same field.
RATIONALE_MAX_CHARS = 1_200


# --------------------------------------------------------------------------- #
# bounds — a cut's depth is the prescriber's to spend; its Q is the
# instrument's evaluable range, not a policy ceiling
# --------------------------------------------------------------------------- #

# A prescribed CUT carries no depth ceiling and no composed ceiling
# (ADR-0207, owner ruling 2026-08-31): a cut only ever removes level and
# cannot clip, no downstream check re-imposes one, and the round's own
# measured verify with auto-restore is the net. The deterministic solver
# keeps its own caps in `blend_correction` — the envelope bounds the
# algorithm, never the prescriber. A cut's Q is bounded to
# jasper.sound.profile's [EVALUABLE_Q_MIN, EVALUABLE_Q_MAX] (see
# `max_q_for_gain`): an INSTRUMENT-fidelity bound, not a policy one — past
# EVALUABLE_Q_MAX the f64 biquad cascade stops evaluating the filter that was
# actually asked for (measured +6.99 dB REALIZED from a requested Q 8e14,
# admitted -3.0 dB cut). What else bounds a cut here is the region
# (`FILTER_OUTSIDE_REGION`) and `BLEND_MAX_FILTERS`' slots.

#: Widest Q one prescribed BOOST may use — the deterministic solver's own
#: emitted Q, imported. That number is not arbitrary in
#: :mod:`.blend_correction`: a narrower filter "chases a feature this
#: instrument cannot resolve and the room will not reproduce off-axis", and
#: 2.0 is the Q every series-1 fit actually realized on hardware.
#:
#: A narrow boost is a headroom risk, not a quality one: every dB of boost is
#: charged against the graph's finite budget on a SAMPLED grid, and no fixed
#: resolution can bound an arbitrarily narrow boost's between-bin peak — so
#: the ceiling is what keeps the composed-boost reading a valid upper bound.
#: The boost class is additionally refused outright by
#: :func:`prescription_route` today, so this ceiling binds nothing that ships;
#: it is here so the bound is stated rather than inherited when that route
#: opens.
#:
#: **The per-driver class deliberately does NOT share this ceiling** — it
#: bounds a boost at 8.0 (see
#: :data:`~.driver_prescription.DRIVER_MAX_BOOST_Q`). Both classes bound a
#: cut's Q instead to the instrument's own evaluable range (see
#: :func:`max_q_for_gain`) — fidelity, not policy, and the same range either
#: way since both share the one evaluator.
PRESCRIPTION_MAX_BOOST_Q = BLEND_FILTER_Q


def max_q_for_gain(gain_db: float) -> float:
    """The widest Q one prescribed filter may use, by the SIGN of its gain.

    The ONE place the split is decided, so the validator, the refusal message
    it prints, and the response format a prescriber is handed cannot disagree
    about which ceiling applies to which filter.

    ``gain_db > 0`` is a boost and gets :data:`PRESCRIPTION_MAX_BOOST_Q` — a
    POLICY ceiling: a boost's composed SPL spend is read on a sampled grid,
    and no fixed grid bounds an arbitrarily narrow boost's between-bin peak.

    Everything else — including exactly ``0.0`` — gets
    :data:`~jasper.sound.profile.EVALUABLE_Q_MAX`. That ceiling is NOT
    policy: a cut still spends no headroom and is free up to it, but past it
    the f64 biquad cascade stops evaluating the filter that was actually
    asked for — measured +6.99 dB REALIZED from a requested Q 8e14, admitted
    -3.0 dB cut. It is INSTRUMENT fidelity, owned by
    :mod:`jasper.sound.profile` and imported rather than restated (same
    shape as ``driver_prescription.driver_max_q_for_gain``).

    That is deliberately the same predicate :func:`_check_bounds` derives
    :attr:`BlendPrescription.prescription_class` from, so a filter can never
    be a cut for the class receipt and a boost for its Q bound.
    """
    return PRESCRIPTION_MAX_BOOST_Q if gain_db > 0.0 else EVALUABLE_Q_MAX


#: Per-filter BOOST ceiling, dB. Owner ruling, 2026-08-18: the boost class is
#: opened at 3.0 dB rather than at the 12.0 dB the established in-band
#: linearization boost already enjoys, because a new permission should not open
#: at the old permission's ceiling.
#:
#: **Deliberately a separate constant from** the deterministic solver's own
#: :data:`~.blend_correction.BLEND_MAX_FILTER_CUT_DB` (3.0 dB today as well —
#: a coincidence of two different derivations, and no door bound at all since
#: ADR-0207). Collapsing them onto one name would make a later ruling about
#: boosts silently re-tune what the deterministic solver may cut.
#:
#: Tunable by ruling, not by a caller: there is no keyword that widens it.
#:
#: **This ceiling is THIS class's alone.** It was restated by
#: :data:`~.driver_prescription.DRIVER_MAX_FILTER_BOOST_DB` on the same
#: 2026-08-18 ruling, and the two were pinned EQUAL, until ruling R8
#: (``docs/tuning-master-plan.md``, 2026-08-21) overturned that ruling **for the
#: driver class only** and moved its ceiling to the fit engine's 12.0 dB rail.
#: So a change here is a change to what the BLEND class may boost, and nothing
#: else. ``tests/test_crossover_v2_driver_prescription.py`` now pins the pair as
#: an INEQUALITY — this constant at 3.0 against the driver class's 12.0 — so the
#: two can no longer be moved by one edit, in either direction.
PRESCRIPTION_MAX_FILTER_BOOST_DB = 3.0

#: Ceiling on the COMPOSED boost's peak over the region, dB — enforced on the
#: evaluated cascade rather than on a sum of gains, because two boosts whose
#: skirts overlap deliver more than either alone.
#:
#: Its own justification is
#: that headroom is finite and shared: every dB of boost is charged against the
#: emitter's own positive-boost accounting, so this ceiling's job is to keep one
#: prescription from consuming a budget the rest of the graph needs. It is the
#: FIRST of two independent bounds, never the only one — the emitter re-charges
#: the composed graph's boost at the graph boundary and refuses there too.
#:
#: **This ceiling is THIS class's alone.** It was restated by
#: :data:`~.driver_prescription.DRIVER_MAX_COMPOSED_BOOST_DB` and pinned equal,
#: and on that seam it was load-bearing rather than latent — it was the number
#: that bounded a prescribed boost's maximum-SPL spend. Ruling R8
#: (``docs/tuning-master-plan.md``, 2026-08-21) ended both facts: the driver
#: class's composed ceiling is now owner policy at 12.0 dB, restated from no
#: neighbour, and the spend it bounds is that class's own
#: ``MAX_SPL_SPEND_BOUND_DB`` = **13.0 dB**, not the 5.0 this pairing once
#: implied. The pin in ``tests/test_crossover_v2_driver_prescription.py`` is now
#: an INEQUALITY, so editing this number moves the blend class only.
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
#: the rule can say nothing, and the receipt carries no positional finding at
#: all rather than a verdict — "go and measure", which the prescriber can act
#: on, and which refuses nothing.
BOOST_MIN_TESTIFYING_POSITIONS = 3

#: How many positions may miss the dip and still leave it supported. One, per
#: the proposed bar. Kept as a named constant beside the count above because
#: the two only make sense read together.
BOOST_MAX_DISSENTING_POSITIONS = 1


# --------------------------------------------------------------------------- #
# refusal vocabulary — closed, by slug, never by prose
# --------------------------------------------------------------------------- #

PRESCRIPTION_TOO_LARGE = "prescription_too_large"
#: ``BLEND_PRESCRIPTION_`` prefixed on these three: they would otherwise
#: collide with :mod:`.alignment_prescription`'s own bare
#: ``PRESCRIPTION_MALFORMED`` / ``PRESCRIPTION_PROVENANCE_MISSING`` /
#: ``PRESCRIPTION_REFUSAL_REASONS`` — two different closed vocabularies for two
#: different seams, sharing one name. Values are unchanged; only the Python
#: identifiers moved, to :data:`DRIVER_PRESCRIPTION_MALFORMED`'s own
#: sibling-module style.
BLEND_PRESCRIPTION_MALFORMED = "prescription_malformed"
PRESCRIPTION_SCHEMA_UNSUPPORTED = "prescription_schema_unsupported"
PRESCRIPTION_PACKET_MISMATCH = "prescription_packet_mismatch"
BLEND_PRESCRIPTION_PROVENANCE_MISSING = "prescription_provenance_missing"
PRESCRIPTION_PROHIBITED_FIELD = "prescription_prohibited_field"
FILTER_MALFORMED = "filter_malformed"
FILTER_COUNT_EXCEEDED = "filter_count_exceeded"
FILTER_OUTSIDE_REGION = "filter_outside_region"
FILTER_Q_OUT_OF_RANGE = "filter_q_out_of_range"
FILTER_BOOST_TOO_HIGH = "filter_boost_too_high"
COMPOSED_BOOST_EXCEEDED = "composed_boost_exceeded"
REGION_UNAVAILABLE = "region_unavailable"
STRICT_READER_DISAGREEMENT = "strict_reader_disagreement"

#: A boost that cleared every bar above and still has nowhere to go.
#:
#: This is a statement about the SEAM, not about the proposal, and it is
#: deliberately the last gate rather than the first: a prescriber is told
#: whether its boost would have qualified before it is told that no route
#: carries one, because those are different pieces of information and the
#: first is the one that decides whether the route is worth building. See
#: :func:`prescription_route` for the two structural facts behind it.
BOOST_ROUTE_UNAVAILABLE = "boost_route_unavailable"

BLEND_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    PRESCRIPTION_TOO_LARGE,
    BLEND_PRESCRIPTION_MALFORMED,
    PRESCRIPTION_SCHEMA_UNSUPPORTED,
    PRESCRIPTION_PACKET_MISMATCH,
    BLEND_PRESCRIPTION_PROVENANCE_MISSING,
    PRESCRIPTION_PROHIBITED_FIELD,
    FILTER_MALFORMED,
    FILTER_COUNT_EXCEEDED,
    FILTER_OUTSIDE_REGION,
    FILTER_Q_OUT_OF_RANGE,
    FILTER_BOOST_TOO_HIGH,
    COMPOSED_BOOST_EXCEEDED,
    REGION_UNAVAILABLE,
    STRICT_READER_DISAGREEMENT,
    BOOST_ROUTE_UNAVAILABLE,
})

#: The candidate field a cut-class prescription lands in.
#:
#: :attr:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverCandidate.blend_correction`
#: — the flat, pre-split, common-mode list the deterministic solver writes,
#: which is exactly what a prescription of this region IS. Named here so a
#: caller folding one onto a candidate does not spell the field itself, on
#: :data:`~.alignment_prescription.ALIGNMENT_PRESCRIPTION_KEY`'s rule.
BLEND_CANDIDATE_FIELD = "blend_correction"

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
    # Written BY the gate, not supplied to it — but accepted on the way back in
    # so a durable block round-trips through the SAME parser rather than
    # needing a second, laxer one. A request that supplies them is harmless:
    # `read_blend_prescription` re-derives the class from the gains it just
    # validated, takes the band from the packet rather than from the document,
    # and recomputes the positional finding. Exactly
    # `alignment_prescription`'s treatment of `checked_at_fc_hz`/`lobe_us`,
    # and pinned by
    # `test_a_supplied_gate_written_field_is_ignored_not_trusted`.
    "prescription_class",
    "band_hz",
    "positional_support",
    "rationale_dropped_chars",
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
#:
#: **Public because it has a second consumer.** :mod:`.driver_prescription`
#: gates a different seam against the same class of attempt, and a second
#: hand-written copy of a security blocklist is exactly how one of them
#: silently falls behind a key added to the other. The name is this module's
#: because this module is where the vocabulary was first written down; the
#: FACT is the family's.
PROHIBITED_PRESCRIPTION_KEYS = frozenset({
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

    Carries a ``reason`` from :data:`BLEND_PRESCRIPTION_REFUSAL_REASONS` beside
    the human ``detail``, and — for the refusals a prescriber can act on — an
    ``evidence`` mapping saying what was actually measured. A model told only
    "refused: composed_boost_exceeded" can guess; one told the peak and the
    ceiling it passed can fix its own proposal.
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

    This class's deterministic stand-in for a per-bin minimum-phase
    classification. :mod:`.feature_classifier` types detected FEATURES, not
    bins, and feeds the per-DRIVER bar; nothing has wired it here. Its whole
    content is a fraction and the disclosure of how that fraction's
    denominator was arrived at.
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
    #: How many characters of the submitted rationale were dropped to fit
    #: :data:`RATIONALE_MAX_CHARS`. ``None`` on documents read back from a
    #: bank that predates the field; ``0`` means the whole rationale was
    #: banked.
    rationale_dropped_chars: int | None = None

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
            "rationale_dropped_chars": self.rationale_dropped_chars,
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
        # A reader handed one contract must be able to find the other, or the
        # packet is only the single document a prescriber reads for whichever
        # class it happened to open first. Named rather than described, so the
        # pointer stays true when either contract's own prose changes.
        "the_other_class": (
            "this contract is for the SUMMED blend region. One driver's own "
            "full-band shape is a different class with different bounds and its "
            "own contract in this packet's 'driver_response_format' block; a "
            "filter aimed outside this region is refused here at any Q"
        ),
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
                f"free text; the first {RATIONALE_MAX_CHARS} characters are "
                "banked and any excess is dropped, with the dropped count on "
                "the receipt. It is stored for a human reader and is NEVER "
                "parsed for behaviour: no argument made here can widen a "
                "bound below."
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
            "q_max_boost": PRESCRIPTION_MAX_BOOST_Q,
            "cuts_are_free": (
                "a cut (gain <= 0) carries no depth ceiling and no composed "
                "ceiling: any depth the arithmetic can evaluate is "
                "admitted, and the round's own measured verify with "
                "auto-restore is the net. Its Q must sit in "
                f"[{EVALUABLE_Q_MIN:g}, {EVALUABLE_Q_MAX:g}] (ADR-0207) — "
                "not a policy ceiling but the range this system's evaluator "
                "and emitter realize faithfully. A boost (gain > 0) is "
                f"capped at Q {PRESCRIPTION_MAX_BOOST_Q:g} — its composed "
                "SPL spend is read on a sampled grid, and no fixed grid can "
                "bound an arbitrarily narrow boost's between-bin peak"
            ),
            "freq_must_be_inside": "the packet's crossover_region.band_hz",
            "max_filter_boost_db": PRESCRIPTION_MAX_FILTER_BOOST_DB,
            "max_composed_boost_db": PRESCRIPTION_MAX_TOTAL_BOOST_DB,
            "composed_caps_are_evaluated": (
                "the composed boost cap is checked on the evaluated biquad "
                "cascade over the region, not on a sum of gains: two filters "
                "whose skirts overlap deliver more than either alone"
            ),
        },
        "boost_positional_finding": {
            "note": (
                "a positive gain is a distinct prescription class and carries "
                "a positional finding on the receipt. It REFUSES NOTHING: a "
                "dip appearing at only one seat is evidence it may be an "
                "interference null, which swallows added energy instead of "
                "being filled by it — weigh it, and let the next measurement "
                "settle it. The delta probe rolls a boost back if it proves "
                "spatially costly."
            ),
            "min_testifying_positions": BOOST_MIN_TESTIFYING_POSITIONS,
            "max_dissenting_positions": BOOST_MAX_DISSENTING_POSITIONS,
            "min_dip_db": BOOST_MIN_DIP_DB,
            "denominator": (
                "positions whose own validity floor sits above the proposed "
                "frequency cannot testify and are removed from the "
                "denominator; the finding reports both counts"
            ),
            "if_evidence_is_missing": (
                "no positional finding is recorded at all — measure more "
                "positions to get one; the prescription is not refused for it"
            ),
        },
        "refusal_reasons": sorted(BLEND_PRESCRIPTION_REFUSAL_REASONS),
        "prohibited_keys": sorted(PROHIBITED_PRESCRIPTION_KEYS),
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


def _finite_or_none(value: Any) -> float | None:
    """One real number, or ``None`` — never a raise, never a coercion.

    The reading counterpart of :func:`_finite_number`, which refuses; this one
    reports. Same three rejections for the same reasons — ``bool`` because it
    is an ``int`` subclass, non-numerics because ``float()`` would coerce a
    string the contract never promised, and non-finite values — plus the
    ``OverflowError`` an arbitrary-precision ``int`` raises after passing the
    isinstance check. Used everywhere :func:`positional_support` reads a number
    out of banked evidence, so there is one answer to "what is a usable number
    here" rather than one per call site.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


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
    # Every numeric input is coerced ONCE, here, through the same guard. This
    # function is public and reachable with values that never passed
    # `_finite_number`: on the shipped path `_check_boost_evidence` hands it a
    # validated frequency and a packet-derived grid, but a caller reading a
    # hand-edited banked artifact can hand it anything JSON admits. An
    # arbitrary-precision int passes an isinstance check and then raises from
    # `float()` / `np.asarray`, which is a crash where the contract promises a
    # finding — so an unusable input is reported as "nothing could testify"
    # rather than propagated.
    target_hz = _finite_or_none(freq_hz)
    reference = _finite_or_none(reference_db)
    try:
        grid = np.asarray(freqs_hz, dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        grid = np.asarray([], dtype=np.float64)
    if (
        target_hz is None
        or reference is None
        or grid.ndim != 1
        or grid.size == 0
        or not np.all(np.isfinite(grid))
    ):
        return PositionalSupport(
            freq_hz=target_hz if target_hz is not None else float("nan"),
            evaluated_at_hz=float("nan"),
            n_positions=len(positions), n_testifying=0, n_with_dip=0,
            excluded_reason=(
                "the packet carries no usable frequency grid, centre "
                "frequency, or flat reference"
            ),
        )
    index = int(np.argmin(np.abs(grid - target_hz)))
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
        # A position row reaches here verbatim — the packet's allowlist copies
        # the field without coercing it — so a bignum floor is an ordinary
        # hostile input on this path rather than a hypothetical. It crashed the
        # CLI end to end before this guard.
        #
        # ABSENT and UNREADABLE are kept apart, and they resolve opposite ways.
        # Absent means the capture's gate established no floor, so the position
        # may testify about any frequency. Unreadable means a floor WAS
        # recorded and cannot be read — and treating that as "no floor" would
        # be fail-open in the one direction that matters here, letting a
        # position vouch for a frequency its own gate may have excluded. That
        # is a bin this function cannot read, which is what `unreadable`
        # already means.
        raw_floor = entry.get("validity_floor_hz")
        if raw_floor is not None:
            floor = _finite_or_none(raw_floor)
            if floor is None:
                unreadable += 1
                continue
            if evaluated_at < floor:
                below_floor += 1
                continue
        value = _finite_or_none(magnitude[index])
        if value is None:
            unreadable += 1
            continue
        testifying += 1
        if value - reference <= -BOOST_MIN_DIP_DB:
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
        freq_hz=target_hz,
        evaluated_at_hz=evaluated_at,
        n_positions=len(positions),
        n_testifying=testifying,
        n_with_dip=with_dip,
        excluded_reason="; ".join(reasons),
    )


# --------------------------------------------------------------------------- #
# the request gate
# --------------------------------------------------------------------------- #


def _refuse(reason: str, detail: str, **evidence: Any) -> NoReturn:
    raise BlendPrescriptionRefused(reason, detail, evidence=evidence or None)


def _finite_number(value: Any, *, reason: str, field: str) -> float:
    """One numeric field, strictly — no coercion, ever.

    ``bool`` is refused because it is an ``int`` in Python and ``gain=True``
    would read as a +1 dB boost. Strings are refused because ``float("1900")``
    succeeds: accepting one would make this reader's strictness depend on the
    encoder's habits rather than on the contract, and the record would then
    disagree with :func:`~.blend_correction.blend_filters_from_mapping`, which
    refuses both. Deliberately STRICTER than the room advisor's
    ``_numeric_in_range``, which does coerce — that reader hands its numbers to
    a different substrate; this one hands them to a reader that will refuse
    them.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(reason, f"{field} must be a number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        # An arbitrary-precision int passes the isinstance check above and then
        # cannot be made a float: `10 ** 400` is a legal JSON number and a legal
        # Python int, and `float()` raises rather than returning inf. Refusing
        # here keeps it inside the closed vocabulary; without this it escaped
        # the gate entirely as an OverflowError, which the CLI then reported
        # with the evidence-unreadable exit code — blaming the round for a
        # fault in the document.
        _refuse(reason, f"{field} is too large to be a filter coefficient")
    if not math.isfinite(number):
        _refuse(reason, f"{field} must be finite, got {number!r}")
    return number


def find_prohibited_keys(value: Any, *, depth: int = 0) -> list[str]:
    """Every blocked key anywhere in the document, at any depth.

    Recursive like ``calibration_agent.response._find_prohibited_keys``, and
    depth-bounded unlike it: this reader is pointed at operator-supplied files,
    and a deeply nested document is a cheap way to spend a recursion limit in a
    process that is about to touch the DSP graph.

    Public for :data:`PROHIBITED_PRESCRIPTION_KEYS`' reason — the walk and the
    set it walks against are one fact, and a sibling gate that imported the set
    but re-wrote the walk would get the depth bound wrong on its own schedule.
    """
    if depth > 12:
        return ["<nesting too deep>"]
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).strip().lower() in PROHIBITED_PRESCRIPTION_KEYS:
                found.append(str(key).strip().lower())
            found.extend(find_prohibited_keys(child, depth=depth + 1))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found.extend(find_prohibited_keys(child, depth=depth + 1))
    return found


def _parse_filters(raw: Any) -> tuple[dict[str, Any], ...]:
    """The filter list's SHAPE, and none of its bounds.

    Split from the bounds for :func:`~.alignment_prescription._parse_prescription`'s
    reason: the shape is what a durable read-back must also re-check, and the
    bounds are what only the request boundary applies.
    """
    if raw is None:
        _refuse(FILTER_MALFORMED, "a prescription must state a filters list")
    if isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        _refuse(FILTER_MALFORMED, f"filters must be a list, got {type(raw).__name__}")
    if not isinstance(raw, Sequence):
        _refuse(FILTER_MALFORMED, f"filters must be a list, got {type(raw).__name__}")
    if len(raw) > BLEND_MAX_FILTERS:
        _refuse(
            FILTER_COUNT_EXCEEDED,
            f"a prescription may carry at most {BLEND_MAX_FILTERS} filters, "
            f"got {len(raw)}",
            n_filters=len(raw),
            max_filters=BLEND_MAX_FILTERS,
        )
    out: list[dict[str, Any]] = []
    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} must be an object, got {type(entry).__name__}",
            )
        unknown = sorted(set(entry) - _FILTER_FIELDS)
        if unknown:
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} carries unknown field(s): {', '.join(unknown)}",
            )
        if entry.get("biquad_type") != "Peaking":
            # Peaking only, matching the emitter's own type allowlist. A shelf
            # across the blend region re-levels it, which is the trim's fact.
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} must be a Peaking biquad, got "
                f"{entry.get('biquad_type')!r}",
            )
        freq = _finite_number(
            entry.get("freq"), reason=FILTER_MALFORMED, field=f"filter {position} freq"
        )
        q = _finite_number(
            entry.get("q"), reason=FILTER_MALFORMED, field=f"filter {position} q"
        )
        gain = _finite_number(
            entry.get("gain"), reason=FILTER_MALFORMED, field=f"filter {position} gain"
        )
        if freq <= 0.0:
            _refuse(FILTER_MALFORMED, f"filter {position} freq must be positive")
        out.append({"biquad_type": "Peaking", "freq": freq, "q": q, "gain": gain})
    return tuple(out)


def _check_bounds(
    filters: tuple[dict[str, Any], ...], band_hz: tuple[float, float]
) -> str:
    """Every per-filter bound, and the class the gains add up to."""
    lo, hi = band_hz
    boosts = 0
    for position, entry in enumerate(filters):
        freq = float(entry["freq"])
        q = float(entry["q"])
        gain = float(entry["gain"])
        if not lo <= freq <= hi:
            _refuse(
                FILTER_OUTSIDE_REGION,
                f"filter {position} at {freq:.1f} Hz is outside the crossover "
                f"region {lo:.1f}-{hi:.1f} Hz",
                freq_hz=freq,
                band_hz=[lo, hi],
            )
        # The evaluator's own floor: below EVALUABLE_Q_MIN, _biquad_coeffs
        # silently clamps eff_q and the emitter spells the filter "q:
        # 0.0000" — not a shape this system can realize, whatever the
        # gain's sign (this also covers the old zero/negative check). The
        # old policy floor is gone (ADR-0207); `max_q_for_gain` owns the
        # boost arm's policy ceiling and the cut arm's instrument-fidelity
        # one.
        if q < EVALUABLE_Q_MIN:
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} q {q:g} is below {EVALUABLE_Q_MIN:g}: "
                "spelled 'q: 0.0000' by the emitter and clamped by the "
                "evaluator, not a shape this system can realize",
            )
        q_max = max_q_for_gain(gain)
        if q > q_max:
            _refuse(
                FILTER_Q_OUT_OF_RANGE,
                f"filter {position} Q {q:g} is past {q_max:g} for a "
                f"{'boost' if gain > 0.0 else 'cut'}",
                q=q,
                q_max=q_max,
            )
        # A gain this deep underflows 64-bit arithmetic: 10**(gain/40) is
        # exactly 0.0 below ~-12960 dB, and _biquad_coeffs divides by it (the
        # Peaking denominator's alpha/amp) — an uncaught ZeroDivisionError.
        # The gate must fail closed on its own input rather than let it
        # escape as an unhandled exception at evaluation time.
        if 10.0 ** (gain / 40.0) == 0.0:
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} gain {gain:g} dB underflows 64-bit "
                "arithmetic and cannot be evaluated or emitted",
            )
        if gain > PRESCRIPTION_MAX_FILTER_BOOST_DB:
            _refuse(
                FILTER_BOOST_TOO_HIGH,
                f"filter {position} boosts {gain:.2f} dB, past the "
                f"{PRESCRIPTION_MAX_FILTER_BOOST_DB:g} dB per-filter ceiling",
                gain_db=gain,
                max_boost_db=PRESCRIPTION_MAX_FILTER_BOOST_DB,
            )
        if gain > 0.0:
            boosts += 1
    return "boost" if boosts else "cut"


def _check_composed(
    filters: tuple[dict[str, Any], ...],
    band_hz: tuple[float, float],
    freqs_hz: Sequence[float] | None,
) -> None:
    """The composed BOOST cap, on the EVALUATED cascade, not a sum of gains.

    Two filters whose skirts overlap deliver more than either alone. Through
    :func:`~jasper.active_speaker.branch_chain.chain_response` — the ONE biquad
    evaluator in this codebase — so this gate and the emitter's own headroom
    charge cannot disagree about what CamillaDSP will realize. The composed
    CUT arm is gone (ADR-0207): a cut spends no headroom, and the solver's own
    composed ceiling in :func:`~.blend_correction._fit_cuts` bounds the
    algorithm, never the prescriber.

    Evaluated on the **denser** of the packet's own grid and a dense log sweep
    over the region — never on whichever happens to be supplied. A coarse axis
    can step over a narrow filter's peak: at the eight-bin floor an earlier cut
    of this function under-read the composed extreme by up to 0.43 dB, which is
    the bound reading low because the evidence document was thin. Taking
    the denser makes the bound a property of the filters instead. The packet
    grid is preferred when it IS denser, so a well-populated round is still
    judged on the system's own axis.
    """
    if not filters:
        return
    lo, hi = band_hz
    fallback = np.geomspace(lo, hi, 512)
    grid = fallback
    if freqs_hz:
        candidate = np.asarray(list(freqs_hz), dtype=np.float64)
        inside = candidate[(candidate >= lo) & (candidate <= hi)]
        if inside.size > fallback.size:
            grid = inside
    composed = 20.0 * np.log10(
        np.maximum(np.abs(np.asarray(chain_response(filters, grid))), 1e-12)
    )
    peak_boost = float(np.max(composed))
    if peak_boost > PRESCRIPTION_MAX_TOTAL_BOOST_DB:
        _refuse(
            COMPOSED_BOOST_EXCEEDED,
            f"the composed cascade boosts {peak_boost:.2f} dB at its peak over "
            f"the region, past the {PRESCRIPTION_MAX_TOTAL_BOOST_DB:g} dB ceiling",
            composed_boost_db=peak_boost,
            max_composed_boost_db=PRESCRIPTION_MAX_TOTAL_BOOST_DB,
        )


def _check_boost_evidence(
    filters: tuple[dict[str, Any], ...],
    evidence: PositionalEvidence | None,
) -> tuple[PositionalSupport, ...]:
    """The positional finding, per boosting filter. **It refuses nothing.**

    Every number this used to refuse on now rides the receipt as provenance —
    ``n_testifying``, ``n_with_dip`` and ``supported`` per boost — for the
    prescriber and the household to weigh. Two refusals went with the nanny
    burn-down: ``INSUFFICIENT_POSITIONAL_EVIDENCE`` and
    ``BOOST_DIP_NOT_STABLE`` were a PREDICTION about whether a filter would
    help, vetoing the measurement that settles it, which the doctrine's
    authority model forbids (``docs/measurement-loop-doctrine.md`` §2 and §5).
    The instrument that disposes already runs downstream: the delta probe
    rolls a boost back on ``spatially_costly``.

    An empty return on a boost-class prescription means the packet could not
    answer at all — no per-position curves, or fewer than
    :data:`BOOST_MIN_TESTIFYING_POSITIONS` positions. That is legible because
    a boost-class prescription carries at least one boosting filter by
    construction, so ``[]`` cannot mean "nothing to evaluate".

    What bounds the COST of an admitted boost is untouched and lives
    elsewhere — the composed and per-filter caps ``_check_bounds`` applies.
    """
    if evidence is None:
        return ()
    positions, freqs_hz, reference_db = evidence
    if len(positions) < BOOST_MIN_TESTIFYING_POSITIONS:
        return ()
    findings: list[PositionalSupport] = []
    for entry in filters:
        if float(entry["gain"]) <= 0.0:
            continue
        findings.append(
            positional_support(
                float(entry["freq"]),
                positions=positions,
                freqs_hz=freqs_hz,
                reference_db=reference_db,
            )
        )
    return tuple(findings)


def _prescriber(raw: Any) -> tuple[str, str]:
    """Who authored this, strictly and non-blank.

    Both halves required. A model with no operator cannot be asked what it was
    told; an operator with no model cannot be compared against the next run.
    This is :data:`BLEND_PRESCRIPTION_PROVENANCE_MISSING` applied to
    authorship rather than to a measured basis — the packet fingerprint
    carries the basis.
    """
    if not isinstance(raw, Mapping):
        _refuse(
            BLEND_PRESCRIPTION_PROVENANCE_MISSING,
            "a prescription must carry a prescriber object naming its model "
            "and operator",
        )
    unknown = sorted(set(raw) - {"model", "operator"})
    if unknown:
        _refuse(
            BLEND_PRESCRIPTION_PROVENANCE_MISSING,
            f"prescriber carries unknown field(s): {', '.join(unknown)}",
        )
    values: list[str] = []
    for field in ("model", "operator"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            _refuse(
                BLEND_PRESCRIPTION_PROVENANCE_MISSING,
                f"prescriber.{field} must be a non-blank name",
            )
        values.append(" ".join(value.split()))
    return values[0], values[1]


def _rationale(raw: Any) -> tuple[str, int]:
    """The prescriber's own words, banked to the ceiling, and what was dropped.

    **It truncates rather than refusing** (ADR-0207), on
    ``driver_prescription._rationale``'s 2026-08-29 argument: nothing reads
    the prose, so a document whose only fault was saying too much used to
    lose its whole round re-authoring words no gate consults. What was
    dropped is counted onto
    :attr:`BlendPrescription.rationale_dropped_chars`. Still strictly TEXT —
    a mapping means the document was built by something that does not speak
    this contract.
    """
    if raw is None:
        return "", 0
    if not isinstance(raw, str):
        _refuse(
            BLEND_PRESCRIPTION_MALFORMED,
            f"rationale must be text, got {type(raw).__name__}",
        )
    text = " ".join(raw.split())
    return text[:RATIONALE_MAX_CHARS], max(0, len(text) - RATIONALE_MAX_CHARS)


def _parse_prescription(
    raw: Mapping[str, Any],
) -> tuple[tuple[dict[str, Any], ...], str, str, str, str, int]:
    """Shape, identity and provenance — and none of the bounds.

    Shared whole between the request gate and the durable read-back, so the
    only thing that differs between those two is their gate policy.
    """
    if not isinstance(raw, Mapping):
        _refuse(
            BLEND_PRESCRIPTION_MALFORMED,
            f"a prescription must be a mapping, got {type(raw).__name__}",
        )
    # The blocklist runs BEFORE the unknown-field check, and the order is the
    # point: every prohibited key is also an unknown one, so checking shape
    # first would report a prescriber reaching for `volume_db` or
    # `role_attenuations_db` as a typo. Those are different facts — one is a
    # misspelling, the other is an attempt to reach past "numbers into a fixed
    # shape" — and only the second is worth a distinct slug.
    prohibited = sorted(set(find_prohibited_keys(raw)))
    if prohibited:
        _refuse(
            PRESCRIPTION_PROHIBITED_FIELD,
            f"a prescription may not name {', '.join(prohibited)}: it supplies "
            "numbers into a fixed shape, never configuration, coefficients, or "
            "a per-role value",
            prohibited=prohibited,
        )
    unknown = sorted(set(raw) - _PRESCRIPTION_FIELDS)
    if unknown:
        _refuse(
            BLEND_PRESCRIPTION_MALFORMED,
            f"unknown prescription field(s): {', '.join(unknown)}",
        )
    if raw.get("kind") != PRESCRIPTION_KIND:
        _refuse(
            BLEND_PRESCRIPTION_MALFORMED,
            f"a prescription must name kind={PRESCRIPTION_KIND!r}, got "
            f"{raw.get('kind')!r}",
        )
    version = raw.get("artifact_schema_version")
    if version != PRESCRIPTION_SCHEMA_VERSION:
        _refuse(
            PRESCRIPTION_SCHEMA_UNSUPPORTED,
            f"this build speaks prescription schema {PRESCRIPTION_SCHEMA_VERSION}, "
            f"got {version!r}",
            supported=PRESCRIPTION_SCHEMA_VERSION,
        )
    fingerprint = raw.get(PACKET_FINGERPRINT_FIELD)
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        _refuse(
            BLEND_PRESCRIPTION_PROVENANCE_MISSING,
            f"a prescription must echo the packet's {PACKET_FINGERPRINT_FIELD}",
        )
    model, operator = _prescriber(raw.get("prescriber"))
    rationale, rationale_dropped = _rationale(raw.get("rationale"))
    return (
        _parse_filters(raw.get("filters")),
        fingerprint.strip(),
        model,
        operator,
        rationale,
        rationale_dropped,
    )


def read_blend_prescription(
    raw: Mapping[str, Any] | None,
    *,
    packet_fingerprint: Any,
    band_hz: tuple[float, float] | None,
    positional_evidence: PositionalEvidence | None,
) -> BlendPrescription | None:
    """THE request gate. One point, and the one place every bound is applied.

    ``None`` when there is no prescription — the deterministic path, untouched.
    Otherwise a validated :class:`BlendPrescription`, or
    :class:`BlendPrescriptionRefused` naming which gate said no.

    The three keyword arguments are the evidence packet's own answers, read out
    of it by :mod:`.evidence_packet`'s three named readers
    (``packet["packet_fingerprint"]``, ``packet_region_band_hz``,
    ``packet_positional_evidence``). Taking VALUES rather than the packet is
    what keeps this module a leaf of the DAG — the packet imports the response
    format from here, so importing the packet back would be a cycle — and it
    is exactly the shape
    :func:`~.alignment_prescription.read_alignment_prescription` already has,
    for the reason stated there: that module may not import the flow, so the
    caller derives the bound's inputs from their single owner and hands them
    in.

    **All three are required and undefaulted**, on that same function's rule.
    Every other bound in this gate rests on a number the prescriber itself
    supplied; these three are the only inputs a prescriber willing to lie
    cannot forge. A caller that forgot one would lose the evidence's own
    opinion and never know, which is precisely what a defaulted keyword hides.

    **Order is deliberate.** Shape, then identity, then the region, then the
    per-filter bounds, then the composed cascade, then — for a boost — the
    positional bar, and last the route. Each stage sends a prescriber somewhere
    different, and reporting a later failure for an earlier cause would send it
    to re-derive a number that was fine.

    **The bounds are inclusive.** A filter exactly at a ceiling is legal; one
    past it is refused. Exactness is legal in this repository's gates, and a
    strict comparison would make a round's legality depend on floating-point
    noise in the sixth decimal of a frequency.
    """
    if raw is None:
        return None
    (
        filters, fingerprint, model, operator, rationale, rationale_dropped,
    ) = _parse_prescription(raw)

    if not isinstance(packet_fingerprint, str) or not packet_fingerprint:
        _refuse(
            PRESCRIPTION_PACKET_MISMATCH,
            "the evidence packet carries no fingerprint to compare against",
        )
    if fingerprint != packet_fingerprint:
        _refuse(
            PRESCRIPTION_PACKET_MISMATCH,
            "this prescription answers a different evidence packet "
            f"({fingerprint[:12]}...) than the one supplied "
            f"({packet_fingerprint[:12]}...)",
            prescription_answers=fingerprint,
            packet_is=packet_fingerprint,
        )

    if band_hz is None:
        _refuse(
            REGION_UNAVAILABLE,
            "this packet establishes no crossover region, so there is no band a "
            "prescription could be checked against",
        )
    band = band_hz

    prescription_class = _check_bounds(filters, band)
    _check_composed(
        filters, band, positional_evidence[1] if positional_evidence else None
    )

    support: tuple[PositionalSupport, ...] = ()
    if prescription_class == "boost":
        support = _check_boost_evidence(filters, positional_evidence)

    # Belt and braces, and the braces are the shipped reader. Everything above
    # is a DIAGNOSTIC layer whose job is to say WHY; the authority on whether a
    # cut list is acceptable remains `blend_filters_from_mapping`, a predicate
    # with no reason. Asking it last means this module can never accept a cut
    # the shipped reader would refuse, however this module's own bounds drift —
    # and the mutation battery proves that relationship rather than asserting
    # it.
    if prescription_class == "cut":
        vouched = blend_filters_from_mapping([dict(f) for f in filters])
        if vouched is None or [dict(f) for f in vouched] != [dict(f) for f in filters]:
            _refuse(
                STRICT_READER_DISAGREEMENT,
                "the shipped persisted-correction reader would not vouch for "
                "this filter list, so it is not one this system can persist",
            )

    prescription = BlendPrescription(
        filters=filters,
        prescription_class=prescription_class,
        packet_fingerprint=fingerprint,
        prescriber_model=model,
        prescriber_operator=operator,
        band_hz=band,
        positional_support=support,
        rationale=rationale,
        rationale_dropped_chars=rationale_dropped,
    )
    prescription_route(prescription)
    return prescription


def prescription_route(prescription: BlendPrescription) -> str:
    """Which candidate field this prescription lands in, or a refusal.

    **A cut routes.** :data:`BLEND_CANDIDATE_FIELD` is the flat, pre-split,
    common-mode list the deterministic solver already writes, so a prescribed
    cut is byte-shaped like a solved one and passes the same emitter gates.

    **A boost does not route, and the reason is structural rather than
    procedural.** Two independent facts, either alone sufficient:

    1. **The summed region has no boosting seam, and opening one is a
       hearing-safety change.** ``camilla_yaml.MAX_BLEND_CORRECTION_GAIN_DB``
       is ``0.0`` and ``_validated_blend_correction`` refuses rather than
       clamps — but the load-bearing fact is one layer down: the blend stage is
       deliberately **not a term in** ``camilla_yaml.total_headroom_db``. It is
       absent because a cuts-only stage needs no absorption. A boost there
       would be un-absorbed and would silently spend the room layer's headroom
       allocation instead of charging its own. Adding that term is a change to
       the gain-structure accounting, reviewed as such; it is not something an
       intake may route around.
    2. **The one seam that DOES carry a positive gain is per-role, and this
       packet is summed evidence.**
       :attr:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverCandidate.linearization`
       carries per-driver boosts to 12 dB, absorbed correctly by
       ``linearization_headroom_db``, and since 2026-08-19
       :mod:`.driver_prescription` can prescribe into it. Both producers are
       per-DRIVER: the fit derives from the per-branch MEASURE sweeps
       (``LinearizationRequest`` raises rather than accept a role with no
       measured response), and the prescription needs a banked
       ``defect-boostable`` verdict for the named driver. A summed cloud
       supplies neither — it cannot say how much of a region's deficit belongs
       to the woofer, which is :mod:`.blend_correction`'s scope tripwire
       verbatim, and every round in the shipped corpus reports both per-branch
       verify claims as ``not_evaluated``/``no_per_branch_verify_capture``.
       Writing a per-driver boost inferred from summed evidence into a
       FINGERPRINTED field would persist an attribution nothing measured.

    So the refusal is honest rather than conservative: no route exists that
    does not either weaken a pinned invariant or bank an unmeasured claim. The
    bars above still run first, on purpose — a prescriber, and an owner
    deciding whether to fund the seam, learns whether the boost would have
    qualified, which is the evidence that decision needs.
    """
    if not prescription.is_boost:
        return BLEND_CANDIDATE_FIELD
    _refuse(
        BOOST_ROUTE_UNAVAILABLE,
        "this boost clears every shape and evidence bar, and there is still no "
        "seam THIS class can carry it on: the summed blend stage refuses a "
        "positive gain and is not a headroom term (opening it is a "
        "gain-structure change). The per-driver linearization seam does carry "
        "one, and admits it only against a banked defect-boostable verdict for "
        "the named driver — evidence a summed packet cannot supply, because it "
        "cannot say which driver a region's deficit belongs to. Propose it as "
        "a per-driver prescription against a round that banked one",
        blocked_by=[
            "blend_stage_is_not_a_headroom_term",
            "per_driver_seam_needs_a_banked_defect_boostable_verdict",
        ],
        bars_cleared=True,
    )


def blend_prescription_to_candidate_fields(
    prescription: BlendPrescription | None,
) -> dict[str, Any]:
    """The candidate fields a validated prescription contributes.

    The sibling of :func:`~.planning.alignment_to_candidate_fields`, and it
    exists for the same reason: a caller folding an outside value onto a
    candidate should not spell the field, and the value must enter **at
    candidate-build time** rather than be stamped on afterwards.

    That entry point is not a style choice.
    :attr:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverCandidate.fingerprint`
    is ``field(init=False)`` — a content hash the caller cannot set, re-derived
    on read and refused as ``candidate_tampered``. A prescription applied after
    construction would either be invisible to the fingerprint or would break
    it; entering here makes a prescribed correction tamper-protected exactly
    like a solved one, which is what lets the next round read it back as its
    own incumbent.

    ``{}`` for ``None``, so a caller can splat it unconditionally and the
    no-prescription path stays byte-identical to today's.

    **It re-asks the route rather than trusting that the gate already did.**
    :func:`read_blend_prescription` calls :func:`prescription_route` before it
    returns, so every prescription reaching here through either of today's two
    callers — ``jasper-crossover-prescriber`` and the round's own blend reader
    (``CrossoverV2Session._blend_prescription``) — is already a cut. But that is
    a fact about those callers, and this function is the last thing between a
    prescription and a fingerprinted candidate field. A
    :class:`BlendPrescription` can also be built directly, or read back by
    :func:`blend_prescription_from_mapping`, neither of which routes. Asking
    the one owner of the rule again costs a function call and makes the
    docstring's promise — a boost can never populate ``blend_correction`` —
    true of the FUNCTION rather than of the current call graph.
    """
    if prescription is None:
        return {}
    return {
        prescription_route(prescription): [dict(f) for f in prescription.filters]
    }


def blend_prescription_from_mapping(raw: Any) -> BlendPrescription | None:
    """A prescription read back out of this repository's own durable state.

    The read-back half of the pair, on
    :func:`~.alignment_prescription.alignment_prescription_from_mapping`'s
    rule: the same shape and provenance checks, deliberately NOT the bounds,
    and ``None`` instead of a raise.

    Why no bounds. The only mappings that reach here were written by
    :func:`read_blend_prescription` accepting them, so re-applying a bound
    could not catch anything the boundary let through — it could only refuse a
    prescription whose region moved between the round that measured it and the
    stage that grades it, and refusing there would discard the evidence of a
    round that really ran. The bounds have one owner and it is the boundary.

    Why still strict about shape. A hand-edited state file is a real input, and
    a receipt that banked half a prescription would claim provenance it does
    not have.

    **Who reads it.** ``jasper-crossover-prescriber propose`` and ``stage``
    both write exactly the shape this parses (``BlendPrescription.to_dict``),
    so the pair round-trips today.

    The second consumer this paragraph anticipated is A9's live-flow wiring, and
    it arrived as predicted: ``correction_crossover_v2``'s
    ``blend_prescription_prior_from_state`` rehydrates the record stage 1 banked
    so the stage that GRADES a round can name what it was prescribed — the same
    job :func:`~.alignment_prescription.alignment_prescription_from_mapping`
    does for the delay, on the same route, for the same reason.

    That consumer is also why a durable record is EXACTLY ``to_dict()`` and
    carries nothing else. A9's first shape folded the document's digest in
    beside it; this reader refuses an unknown field rather than ignoring it, so
    one extra key made the whole record unreadable and stage 2 silently
    rehydrated ``None``. The strictness worked — it is the reason a receipt
    cannot bank half a prescription — and the fix was to give the digest its own
    slot rather than to loosen the reader.

    A9's OTHER read, of a document that has not been through the gate this
    process, does not come here: that one goes through
    :func:`read_blend_prescription` with the bounds (see
    :mod:`.prescription_spool`).

    Note that this reader does NOT route: it re-derives
    ``prescription_class`` from the gains but applies no bound and no seam
    check, which is why
    :func:`blend_prescription_to_candidate_fields` asks
    :func:`prescription_route` itself rather than assuming its input was gated.
    """
    if raw is None:
        return None
    try:
        # The dropped-character count is discarded rather than carried: what
        # this reader holds is the already-truncated text, so it cannot know
        # what the prescriber originally wrote. `rationale_dropped_chars`
        # takes its `None` default below, mirroring
        # `driver_prescription.driver_prescription_from_mapping`'s identical
        # convention for the identical field.
        filters, fingerprint, model, operator, rationale, _dropped = (
            _parse_prescription(raw)
        )
    except BlendPrescriptionRefused:
        return None
    band_raw = raw.get("band_hz") if isinstance(raw, Mapping) else None
    band: tuple[float, float] | None = None
    if isinstance(band_raw, (list, tuple)) and len(band_raw) == 2:
        try:
            lo, hi = float(band_raw[0]), float(band_raw[1])
        except (TypeError, ValueError, OverflowError):
            band = None
        else:
            if math.isfinite(lo) and math.isfinite(hi) and 0.0 < lo < hi:
                band = (lo, hi)
    if band is None:
        return None
    return BlendPrescription(
        filters=filters,
        prescription_class=(
            "boost" if any(float(f["gain"]) > 0.0 for f in filters) else "cut"
        ),
        packet_fingerprint=fingerprint,
        prescriber_model=model,
        prescriber_operator=operator,
        band_hz=band,
        rationale=rationale,
    )


def read_prescription_bytes(payload: bytes) -> Mapping[str, Any]:
    """Decode one proposal document, treating every byte as hostile.

    The size cap is applied to the BYTES, before ``json.loads`` ever sees them:
    a cap enforced after parsing has already paid the cost it exists to avoid.
    Same posture as the web layer's ``read_json_object(max_bytes=)``, owned
    here because this reader is reached from a file and from stdin as well, and
    a cap only one caller enforced would be a cap the others lack.
    """
    if len(payload) > PRESCRIPTION_MAX_BYTES:
        _refuse(
            PRESCRIPTION_TOO_LARGE,
            f"a prescription may be at most {PRESCRIPTION_MAX_BYTES} bytes, got "
            f"{len(payload)}",
            max_bytes=PRESCRIPTION_MAX_BYTES,
            got_bytes=len(payload),
        )
    try:
        document = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError:
        _refuse(BLEND_PRESCRIPTION_MALFORMED, "a prescription must be UTF-8 text")
    except json.JSONDecodeError as exc:
        _refuse(
            BLEND_PRESCRIPTION_MALFORMED,
            f"a prescription must be valid JSON: {exc.msg}",
        )
    except RecursionError:
        # Deeply nested arrays exhaust the interpreter stack inside the parser
        # itself, well under the byte cap: ~20 KB of `[[[[...]]]]` does it. A
        # RecursionError is a RuntimeError, so it matched neither arm above and
        # escaped as an uncaught exception. Caught by its own name rather than
        # by widening either arm, because it is a fact about the document's
        # SHAPE rather than about its encoding or its syntax. The refusal path
        # from here is shallow, so it runs on the unwound stack.
        _refuse(
            BLEND_PRESCRIPTION_MALFORMED, "a prescription is nested too deeply to parse"
        )
    if not isinstance(document, dict):
        _refuse(
            BLEND_PRESCRIPTION_MALFORMED,
            f"a prescription must be a JSON object, got {type(document).__name__}",
        )
    return document


def prescription_sha256(payload: bytes) -> str:
    """The digest of the bytes actually parsed.

    Provenance for what was read rather than for what was meant. It goes on the
    receipt beside the prescriber's name so a later reader can prove which
    document produced a round.
    """
    return hashlib.sha256(payload).hexdigest()
