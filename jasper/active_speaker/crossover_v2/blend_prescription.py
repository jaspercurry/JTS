# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE blend-region shape correction, prescribed from outside this process.

The model proposes; the harness disposes. Nothing here applies anything, loads
a graph, or grades a round: it reads one JSON document as hostile data and
returns a validated :class:`BlendPrescription` or a refusal naming which gate
said no, by slug from :data:`BLEND_PRESCRIPTION_REFUSAL_REASONS` and never by
prose. Refusals raise and are never clamped to the boundary.

The gate measures against the evidence PACKET the prescriber answered, not
against the incumbent, and provenance is content-addressed: the proposal echoes
:data:`PACKET_FINGERPRINT_FIELD`, so one answering a different round is refused
rather than graded against evidence it never saw.

Cuts and boosts are different classes and the receipt says which. A boost's
physics is why: a minimum-phase shortfall can be filled, an interference null
swallows whatever you feed it. :func:`positional_support` is this class's
deterministic stand-in for that distinction — an interference null moves with
the microphone, a radiating shortfall does not — and it is a SPATIAL test, so
its power is bounded by the angular spread of the cloud it reads. A tightly
clustered walk can support a boost :mod:`.feature_classifier` would call
interference, which is one reason :func:`prescription_route` refuses the boost
class outright today.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

import numpy as np

# Leaf of the crossover_v2 DAG: no session, no flow, no web. Bounds are
# imported from the ONE biquad evaluator and from the deterministic solver
# rather than restated, so a door's ceiling and the arithmetic it protects
# cannot drift apart.
from jasper.active_speaker.branch_chain import chain_response
from jasper.sound.profile import EVALUABLE_Q_MAX, EVALUABLE_Q_MIN

from .blend_correction import (
    BLEND_FILTER_Q,
    BLEND_MAX_FILTERS,
    BLEND_MIN_CUT_DB,
    blend_filters_from_mapping,
)

#: What :func:`~.evidence_packet.packet_positional_evidence` returns: the
#: per-position records, their shared frequency grid, and the flat reference
#: they are read against — meaningful only as one evaluation's output.
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

#: A proposal naming a version this build does not speak is refused, never
#: best-effort parsed.
PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator. This reader is pointed at operator-supplied
#: files and stdin, where "that was the wrong file" is the likelier mistake.
PRESCRIPTION_KIND = "jts_crossover_blend_prescription"

#: The packet field a proposal must echo back.
PACKET_FINGERPRINT_FIELD = "packet_fingerprint"

#: Byte ceiling on one proposal document, read before it is parsed. The largest
#: legitimate proposal is under 1 KiB, so this is two orders of magnitude of
#: slack: the job is to stop a pathological input being parsed at all. It lives
#: with the reader, not the transport, because a file, stdin and a POST body
#: all reach here.
PRESCRIPTION_MAX_BYTES = 64 * 1024

#: Ceiling on the free-text rationale, in characters. It TRUNCATES and
#: discloses rather than refusing (ADR-0207): nothing reads the prose, so a
#: document whose only fault was saying too much should not lose its round.
RATIONALE_MAX_CHARS = 1_200


# --------------------------------------------------------------------------- #
# bounds
# --------------------------------------------------------------------------- #

# A prescribed CUT carries no depth ceiling and no composed ceiling
# (ADR-0207): a cut only removes level and cannot clip, and the round's own
# measured verify with auto-restore is the net. The deterministic solver keeps
# its own caps in `blend_correction` — the envelope bounds the algorithm, never
# the prescriber. What else bounds a cut here is the region
# (`FILTER_OUTSIDE_REGION`), `BLEND_MAX_FILTERS`' slots, and `max_q_for_gain`.

#: Widest Q one prescribed BOOST may use — the deterministic solver's own
#: emitted Q, imported. A narrow boost is a headroom risk rather than a quality
#: one: boost is charged on a SAMPLED grid, and no fixed resolution bounds an
#: arbitrarily narrow boost's between-bin peak, so this ceiling is what keeps
#: the composed-boost reading a valid upper bound. The per-driver class
#: deliberately does NOT share it (:data:`~.driver_prescription.DRIVER_MAX_BOOST_Q`
#: is 8.0).
PRESCRIPTION_MAX_BOOST_Q = BLEND_FILTER_Q


def max_q_for_gain(gain_db: float) -> float:
    """The widest Q one prescribed filter may use, by the SIGN of its gain.

    A boost gets :data:`PRESCRIPTION_MAX_BOOST_Q`, a POLICY ceiling; everything
    else — ``0.0`` included — gets
    :data:`~jasper.sound.profile.EVALUABLE_Q_MAX`, an INSTRUMENT-fidelity one
    (past it the f64 biquad cascade stops evaluating the filter asked for:
    measured +6.99 dB realized from a requested Q 8e14 on an admitted -3.0 dB
    cut). Same predicate :func:`_check_bounds` derives
    :attr:`BlendPrescription.prescription_class` from, so a filter cannot be a
    cut for the receipt and a boost for its Q bound.
    """
    return PRESCRIPTION_MAX_BOOST_Q if gain_db > 0.0 else EVALUABLE_Q_MAX


#: Per-filter BOOST ceiling, dB — this class's alone, and tunable by ruling
#: rather than by a caller. Deliberately a separate constant from the
#: deterministic solver's :data:`~.blend_correction.BLEND_MAX_FILTER_CUT_DB`
#: and from :data:`~.driver_prescription.DRIVER_MAX_FILTER_BOOST_DB` (12.0),
#: which ``tests/test_crossover_v2_driver_prescription.py`` pins as an
#: INEQUALITY so one edit cannot move both.
PRESCRIPTION_MAX_FILTER_BOOST_DB = 3.0

#: Ceiling on the COMPOSED boost's peak over the region, dB — enforced on the
#: evaluated cascade rather than a sum of gains, because two boosts whose
#: skirts overlap deliver more than either alone. The FIRST of two independent
#: bounds: the emitter re-charges the composed graph at the graph boundary and
#: refuses there too. This class's alone, pinned against the driver class's own
#: ceiling as an inequality.
PRESCRIPTION_MAX_TOTAL_BOOST_DB = 4.0

#: How deep a per-position deviation must be to count as "the dip is present
#: here", dB. :data:`BLEND_MIN_CUT_DB` under a reading: it is this program's own
#: measured model-tracking error, so a shallower dip is indistinguishable from
#: the instrument.
BOOST_MIN_DIP_DB = BLEND_MIN_CUT_DB

#: The fewest positions that must testify before the all-but-one rule means
#: anything. Three: "present at every position but at most one" is VACUOUS at
#: two — it admits a dip seen at exactly one — and undefined at one. Below it
#: the receipt carries no positional finding at all rather than a verdict.
BOOST_MIN_TESTIFYING_POSITIONS = 3

#: How many positions may miss the dip and still leave it supported. Read
#: together with the count above.
BOOST_MAX_DISSENTING_POSITIONS = 1


# --------------------------------------------------------------------------- #
# refusal vocabulary — closed, by slug, never by prose
# --------------------------------------------------------------------------- #

PRESCRIPTION_TOO_LARGE = "prescription_too_large"
#: ``BLEND_PRESCRIPTION_`` prefixed on these three so the Python identifiers do
#: not collide with :mod:`.alignment_prescription`'s bare ones. Only the
#: identifiers differ — the VALUES are the same strings that door uses.
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

#: A boost that cleared every bar above and still has nowhere to go — a
#: statement about the SEAM, not the proposal, and deliberately the LAST gate:
#: a prescriber learns whether its boost would have qualified before it learns
#: no route carries one. See :func:`prescription_route`.
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

#: The candidate field a cut-class prescription lands in: the flat, pre-split,
#: common-mode list the deterministic solver writes, which is what a
#: prescription of this region IS.
BLEND_CANDIDATE_FIELD = "blend_correction"

#: Top-level fields a proposal may carry. Anything else is refused rather than
#: ignored: a misspelled ``filters`` that silently dropped the prescription
#: would leave the gate accepting an empty one.
_PRESCRIPTION_FIELDS = frozenset({
    "artifact_schema_version",
    "kind",
    PACKET_FINGERPRINT_FIELD,
    "prescriber",
    "filters",
    "rationale",
    # Written BY the gate, accepted on the way back in so a durable block
    # round-trips through this parser. A request that supplies them is
    # harmless: the gate re-derives every one of them.
    "prescription_class",
    "band_hz",
    "positional_support",
    "rationale_dropped_chars",
})

#: Fields ONE filter may carry — the reduced record ``chain_response``, the
#: emitter and ``blend_filters_from_mapping`` all speak, so there is no
#: translation layer here to get wrong.
_FILTER_FIELDS = frozenset({"biquad_type", "freq", "q", "gain"})

#: Keys no proposal may contain at any depth: a model reaching past "numbers
#: into a fixed shape" toward config, coefficients, or execution. Adopted from
#: ``calibration_agent.response._PROHIBITED_KEYS`` and copied rather than
#: imported, because importing it would pull an OpenAI client into this leaf;
#: ``tests/test_crossover_v2_blend_prescription.py`` pins the room set as a
#: subset so the two cannot drift. Public because
#: :mod:`.driver_prescription` gates the same class of attempt and a second
#: hand-written blocklist is how one falls behind the other.
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
    # This domain's own two: the delay is `alignment_prescription`'s gate, and
    # the blend region is common-mode by construction, so it names no role.
    "delay_us",
    "role_attenuations_db",
})


class BlendPrescriptionRefused(ValueError):
    """One prescription this module would not accept, and why.

    ``reason`` is from :data:`BLEND_PRESCRIPTION_REFUSAL_REASONS`; ``evidence``
    carries what was actually measured, so a prescriber can fix its proposal
    rather than guess.
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

    A fraction, plus the disclosure of how its denominator was arrived at.
    """

    #: The proposed centre frequency, and the grid bin actually read. They
    #: differ by up to half a bin; both are stated so a reader can tell a real
    #: answer from one snapped to a distant bin.
    freq_hz: float
    evaluated_at_hz: float
    #: Every position in the packet's cloud.
    n_positions: int
    #: Those whose own validity floor admits :attr:`evaluated_at_hz` — the
    #: denominator. A position that cannot testify is REMOVED, never counted as
    #: one that failed to see the dip.
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
    applies, incumbent included.
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
    #: Who authored it. Both fields required.
    prescriber_model: str
    prescriber_operator: str
    #: The region the proposal was checked against, echoed from the packet.
    band_hz: tuple[float, float]
    #: The positional-support finding for each boost, in filter order. Empty on
    #: a cut-class prescription: cutting a null flattens the region at every
    #: position rather than feeding one.
    positional_support: tuple[PositionalSupport, ...] = ()
    #: The prescriber's own words. NEVER parsed for behaviour — no branch here
    #: or in any caller reads it.
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

    One owner for the instructions a prescriber is given and the gate it is
    judged by, so the two cannot describe different shapes. It is a PURE
    CONSTANT — nothing banked, measured, or household-authored reaches it,
    which is what makes prompt injection through the packet structurally
    impossible rather than filtered.
    """
    return {
        "artifact_schema_version": PRESCRIPTION_SCHEMA_VERSION,
        "kind": "jts_crossover_blend_prescription_contract",
        # A reader handed one contract must be able to find the other.
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

    The reading counterpart of :func:`_finite_number`, which refuses. Same
    rejections — ``bool`` (an ``int`` subclass), non-numerics, non-finite
    values — plus the ``OverflowError`` an arbitrary-precision ``int`` raises
    after passing the isinstance check.
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

    Reads the system's OWN persisted per-position curves against its OWN flat
    reference, with no re-smoothing and no re-derivation. Every argument comes
    from one packet, so the curves and the reference cannot come from
    different evaluations. ``positions`` entries are the packet's position
    records: ``magnitude_db`` on the shared ``freqs_hz`` grid, plus
    ``validity_floor_hz`` where the capture's own gate established one.
    """
    # Every numeric input is coerced ONCE, here. This function is public and
    # reachable with values that never passed `_finite_number` — a hand-edited
    # banked artifact can hand it anything JSON admits — so an unusable input
    # is reported as "nothing could testify" rather than raising.
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
        # curve is not evidence. ABSENT and UNREADABLE resolve OPPOSITE ways:
        # absent means no floor was established and the position may testify
        # about any frequency, while unreadable means a floor was recorded and
        # cannot be read — treating that as "no floor" would let a position
        # vouch for a frequency its own gate may have excluded.
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

    ``bool`` is refused because it is an ``int`` and ``gain=True`` would read as
    a +1 dB boost; strings because ``float("1900")`` succeeds, and
    :func:`~.blend_correction.blend_filters_from_mapping` refuses both.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(reason, f"{field} must be a number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        # `10 ** 400` is a legal JSON number and a legal Python int that passes
        # the isinstance check, and `float()` raises rather than returning inf.
        # Refusing here keeps it inside the closed vocabulary instead of
        # escaping the gate as an OverflowError.
        _refuse(reason, f"{field} is too large to be a filter coefficient")
    if not math.isfinite(number):
        _refuse(reason, f"{field} must be finite, got {number!r}")
    return number


def find_prohibited_keys(value: Any, *, depth: int = 0) -> list[str]:
    """Every blocked key anywhere in the document, at any depth.

    Depth-bounded: this reader is pointed at operator-supplied files, and a
    deeply nested document is a cheap way to spend a recursion limit. Public
    for :data:`PROHIBITED_PRESCRIPTION_KEYS`' reason — the walk and the set it
    walks against are one fact.
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

    The shape is what a durable read-back must also re-check; the bounds are
    what only the request boundary applies.
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
        # The evaluator's own floor: below EVALUABLE_Q_MIN, `_biquad_coeffs`
        # silently clamps eff_q and the emitter spells the filter "q: 0.0000" —
        # not a shape this system can realize, whatever the gain's sign.
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
        # 10**(gain/40) is exactly 0.0 below ~-12960 dB, and `_biquad_coeffs`
        # divides by it — an uncaught ZeroDivisionError at evaluation time.
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
    ``chain_response``, the ONE biquad evaluator here, so this gate and the
    emitter's headroom charge cannot disagree about what CamillaDSP realizes.
    There is no composed CUT arm (ADR-0207): a cut spends no headroom.

    Evaluated on the DENSER of the packet's own grid and a dense log sweep over
    the region, never on whichever happens to be supplied — a coarse axis steps
    over a narrow filter's peak (measured: up to 0.43 dB under-read at the
    eight-bin floor), which would make the bound a property of the evidence
    document rather than of the filters.
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

    The numbers ride the receipt as provenance for the prescriber and the
    household to weigh; a prediction about whether a filter would help may not
    veto the measurement that settles it
    (``docs/measurement-loop-doctrine.md`` §2 and §5), and the delta probe
    rolls a boost back on ``spatially_costly`` downstream. An empty return on a
    boost-class prescription means the packet could not answer at all — such a
    prescription carries at least one boosting filter by construction, so
    ``[]`` cannot mean "nothing to evaluate". What bounds the COST of an
    admitted boost is ``_check_bounds``.
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
    """Who authored this, strictly and non-blank. Both halves required."""
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

    Truncates rather than refusing (ADR-0207), counting the loss onto
    :attr:`BlendPrescription.rationale_dropped_chars`. Still strictly TEXT.
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
    # BEFORE the unknown-field check: every prohibited key is also an unknown
    # one, so checking shape first would report a prescriber reaching for
    # `volume_db` as a typo.
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

    The three keywords are the evidence packet's own answers, read out by
    :mod:`.evidence_packet`'s named readers. Taking VALUES rather than the
    packet keeps this module a leaf of the DAG (the packet imports the response
    format from here). All three are required and undefaulted: they are the
    only inputs a prescriber willing to lie cannot forge, so a caller that
    forgot one would lose the evidence's opinion and never know.

    Order is deliberate — shape, identity, region, per-filter bounds, composed
    cascade, the positional bar for a boost, and last the route — because each
    stage sends a prescriber somewhere different. The bounds are INCLUSIVE, so
    a round's legality does not turn on float noise.
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

    # The authority on whether a cut list is acceptable stays
    # `blend_filters_from_mapping`; everything above is the diagnostic layer
    # that says WHY. Asked LAST, so this module can never accept a cut the
    # shipped reader would refuse, however its own bounds drift.
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

    A cut routes to :data:`BLEND_CANDIDATE_FIELD`, byte-shaped like a solved
    one. A boost does not, on two independent structural facts:

    1. The blend stage is deliberately NOT a term in
       ``camilla_yaml.total_headroom_db`` — a cuts-only stage needs no
       absorption — so a boost there would be un-absorbed and would spend the
       room layer's allocation. Adding that term is a gain-structure change.
    2. The one seam that DOES carry a positive gain, ``linearization``, is
       per-ROLE and admits a prescribed boost only against a banked
       ``defect-boostable`` verdict for the named driver. A SUMMED packet
       cannot say which driver a region's deficit belongs to, so writing one
       into a fingerprinted field would persist an attribution nothing
       measured.

    The bars above still run first, so a prescriber learns whether its boost
    would have qualified.
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

    The value must enter at CANDIDATE-BUILD time:
    ``MeasuredCrossoverCandidate.fingerprint`` is ``field(init=False)``, so a
    prescription applied after construction is either invisible to the
    fingerprint or refused as ``candidate_tampered``. ``{}`` for ``None``, so a
    caller can splat it unconditionally.

    It RE-ASKS :func:`prescription_route` rather than trusting its input was
    gated — a :class:`BlendPrescription` can be built directly or read back by
    :func:`blend_prescription_from_mapping`, neither of which routes — which
    makes "a boost can never populate ``blend_correction``" true of this
    function rather than of the current call graph.
    """
    if prescription is None:
        return {}
    return {
        prescription_route(prescription): [dict(f) for f in prescription.filters]
    }


def blend_prescription_from_mapping(raw: Any) -> BlendPrescription | None:
    """A prescription read back out of this repository's own durable state.

    Shape and provenance only — the bounds have one owner and it is the request
    gate; re-applying them here could only refuse a round that really ran —
    and ``None`` instead of a raise. A durable record is EXACTLY ``to_dict()``
    and carries nothing else: this reader refuses an unknown field rather than
    ignoring it, so one extra key makes the whole record unreadable.

    It does NOT route: it re-derives ``prescription_class`` from the gains but
    applies no seam check, which is why
    :func:`blend_prescription_to_candidate_fields` asks
    :func:`prescription_route` itself.
    """
    if raw is None:
        return None
    try:
        # The dropped-character count is discarded: this reader holds the
        # already-truncated text and cannot know what was originally written.
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

    The size cap is applied to the BYTES, before ``json.loads`` sees them: a
    cap enforced after parsing has already paid what it exists to avoid.
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
        # Deeply nested arrays exhaust the interpreter stack inside the parser,
        # well under the byte cap: ~20 KB of `[[[[...]]]]` does it. A
        # RecursionError is a RuntimeError, so it matches neither arm above.
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
    """The digest of the bytes actually parsed — what was read, not what was
    meant, so a later reader can prove which document produced a round."""
    return hashlib.sha256(payload).hexdigest()
