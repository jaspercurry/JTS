# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The explicit, admissibility-gated, provenance-carrying crossover pin.

Covers the request gate (shape, provenance, and every bound), the single-owner
claim that a PINNED corner and a DECLARED one are admissible on identical terms,
that the pin reaches the emitted filters under a region name apply accepts, the
durable read-back, and — the control that matters most — that a request with no
prescription changes nothing.

The declarations in the candidate tests are jts3's own, read out of the banked
``captures/armloop-first-drive-2026-08/r1-baseline-summed/design-draft.json``
on 2026-08-20, deliberately: this gate exists so a pre-registered Fc/slope
tournament can run on that speaker, and a test written against placeholder
declarations cannot say which of its candidates that speaker actually admits.

Two candidates of that tournament were refused ``outside_declared_search_band``
by an invented ``crossover_search_band_hz`` until the 2026-08-22 owner ruling
deleted it (#2870). Both drivers' declared HARD bands admitted them the whole
time, so they are admitted here now, and the refusals this file still pins are
exactly the two that name a component-damage mechanism.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from jasper.active_speaker.driver_protection import (
    PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE,
)
from jasper.active_speaker.crossover_v2.fc_sweep import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
    _fc_rejection,
    recornered_preset,
)
from jasper.active_speaker.crossover_v2.topology_prescription import (
    TOPOLOGY_AUTHORITY_OPERATOR_PINNED,
    TOPOLOGY_FC_INVALID,
    TOPOLOGY_MALFORMED,
    TOPOLOGY_NO_CROSSOVER_REGION,
    TOPOLOGY_ORDER_INVALID,
    TOPOLOGY_ORDER_UNSUPPORTED,
    TOPOLOGY_PRESCRIPTION_KEY,
    TOPOLOGY_PRESCRIPTION_KIND,
    TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS,
    TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED,
    TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION,
    TOPOLOGY_PROVENANCE_MISSING,
    TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT,
    TopologyPrescription,
    TopologyPrescriptionRefused,
    apply_topology_pin,
    candidate_topology,
    read_topology_prescription,
    topology_prescription_from_mapping,
    topology_prescription_response_format,
)
from jasper.active_speaker.profile import SUPPORTED_LR_ORDERS

# --------------------------------------------------------------------------- #
# jts3's own declarations, and the candidates they were meant to serve
# --------------------------------------------------------------------------- #

#: The upper driver's declared floor — the tweeter's permitted excitation band
#: bottom, which is also its declared protective high-pass corner.
DECLARED_FLOOR_HZ = 1600.0
#: The lower driver's declared hard ceiling.
LOWER_CEILING_HZ = 4000.0
#: A maker who PUBLISHES 24 dB/octave. Not jts3's DE250 — B&C publish 12 for
#: that driver (:data:`DE250_PUBLISHED_SLOPE` below) — but the shape that must
#: still refuse order 2, because a published condition is the authority in both
#: directions. It was jts3's number here only because the gate used to read the
#: DERIVED ``max(published, 24)``; the 2026-08-23 ruling ended that.
DECLARED_SLOPE = 24.0
#: What B&C actually print for the DE250: "Recommended Crossover 1.6 kHz — 12
#: dB/oct. or higher slope high-pass filter", read off the datasheet line quoted
#: in the 2026-08-23 owner ruling.
DE250_PUBLISHED_SLOPE = 12.0
#: What the speaker is commissioned at today.
INCUMBENT_HZ = 1648.7
#: The provenance a real pin carries: the banked artifact it was drawn from.
ARTIFACTS = ("armloop-first-drive-2026-08/offline-fc-search",)


def _pin(corner_hz: float, /, **overrides: object) -> dict:
    """One candidate as the session POST body carries it.

    The positional parameter is deliberately NOT called ``fc_hz``: several
    tests below override that very key, and a same-named parameter would make
    those calls a ``TypeError`` instead of a mutation.
    """
    body: dict = {
        "kind": TOPOLOGY_PRESCRIPTION_KIND,
        "artifact_schema_version": TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION,
        "fc_hz": corner_hz,
        "order": 4,
        "basis_artifacts": list(ARTIFACTS),
        "basis_note": "offline candidate search, no measured ranking",
    }
    body.update(overrides)
    return body


def _read(raw, **overrides):
    """The gate at jts3's declarations, with any one of them overridable."""
    kwargs = {
        "declared_floor_hz": DECLARED_FLOOR_HZ,
        "lower_driver_ceiling_hz": LOWER_CEILING_HZ,
        "minimum_slope_db_per_octave": DECLARED_SLOPE,
        "beaming_ceiling_hz": None,
    }
    kwargs.update(overrides)
    return read_topology_prescription(raw, **kwargs)


# --------------------------------------------------------------------------- #
# 1. Absence is the automatic path
# --------------------------------------------------------------------------- #


def test_no_prescription_is_none_and_not_a_refusal():
    """The overwhelming majority of rounds, and they must cost nothing."""
    assert _read(None) is None


def test_the_entry_key_is_owned_by_the_reader_that_owns_the_shape():
    """One spelling, in the module that parses it — not at the web boundary."""
    assert TOPOLOGY_PRESCRIPTION_KEY == "topology_prescription"


# --------------------------------------------------------------------------- #
# 2. The closed field set
# --------------------------------------------------------------------------- #


def test_an_unknown_field_is_refused_rather_than_ignored():
    """A misspelled ``basis_artifact`` that was quietly dropped would leave a
    pinned round claiming provenance nobody declared."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, basis_artifact=["typo"]))
    assert excinfo.value.reason == TOPOLOGY_MALFORMED
    assert "basis_artifact" in excinfo.value.detail


def test_polarity_is_refused_here_because_the_alignment_door_owns_it():
    """The one-field-one-owner rule, made a refusal rather than a convention.

    Polarity is pinned through ``alignment_prescription``, which translates the
    candidate's action word into the measurement frame's sign. A second door
    onto the same knob is how two halves of one decision enter by two paths and
    disagree — so a prescriber that puts it here learns at the tap.
    """
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, polarity="invert"))
    assert excinfo.value.reason == TOPOLOGY_MALFORMED
    assert "polarity" in excinfo.value.detail


def test_a_prescription_must_be_a_mapping():
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(["fc_hz", 2400.0])
    assert excinfo.value.reason == TOPOLOGY_MALFORMED


def test_every_accepted_field_survives_the_round_trip_the_receipt_needs():
    """``to_dict()`` must re-parse, or the receipt is unreadable at grading.

    The blend prescription's own lesson, pinned here before it can be
    relearned: the durable record round-trips through a reader that REFUSES an
    unknown field rather than ignoring it, so one extra key on the way out
    makes the whole record ``None`` on the way back in.
    """
    pinned = _read(_pin(2400.0))
    assert pinned is not None
    again = topology_prescription_from_mapping(pinned.to_dict())
    assert again is not None
    assert again.to_dict() == pinned.to_dict()


def test_the_receipt_carries_kind_and_schema_version():
    """The envelope, on the established shape.

    Mirrors :data:`~jasper.active_speaker.crossover_v2.driver_prescription.
    DriverPrescription.to_dict`'s ``kind``/``artifact_schema_version`` pair.
    """
    pinned = _read(_pin(2400.0))
    assert pinned is not None
    record = pinned.to_dict()
    assert record["kind"] == TOPOLOGY_PRESCRIPTION_KIND
    assert record["artifact_schema_version"] == TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"kind": "nope"}, TOPOLOGY_MALFORMED),
        ({"kind": None}, TOPOLOGY_MALFORMED),
        ({"artifact_schema_version": 2}, TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED),
        ({"artifact_schema_version": None}, TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED),
    ],
)
def test_the_envelope_is_checked_before_any_content_field(mutation, reason):
    """The version+kind envelope, on the established shape.

    Mirrors :mod:`~jasper.active_speaker.crossover_v2.driver_prescription`'s
    own gate: a document naming the wrong kind is malformed, and one naming a
    version this build does not speak is its own, distinct refusal.
    """
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, **mutation))
    assert excinfo.value.reason == reason


# --------------------------------------------------------------------------- #
# 3. Shape refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"fc_hz": "2400"}, TOPOLOGY_FC_INVALID),
        ({"fc_hz": True}, TOPOLOGY_FC_INVALID),
        ({"fc_hz": float("nan")}, TOPOLOGY_FC_INVALID),
        ({"fc_hz": float("inf")}, TOPOLOGY_FC_INVALID),
        ({"fc_hz": 0.0}, TOPOLOGY_FC_INVALID),
        ({"fc_hz": -2400.0}, TOPOLOGY_FC_INVALID),
        ({"order": "4"}, TOPOLOGY_ORDER_INVALID),
        ({"order": 4.0}, TOPOLOGY_ORDER_INVALID),
        ({"order": True}, TOPOLOGY_ORDER_INVALID),
        ({"order": None}, TOPOLOGY_ORDER_INVALID),
        ({"order": 6}, TOPOLOGY_ORDER_UNSUPPORTED),
        ({"order": 1}, TOPOLOGY_ORDER_UNSUPPORTED),
        ({"order": 16}, TOPOLOGY_ORDER_UNSUPPORTED),
        ({"basis_artifacts": None}, TOPOLOGY_PROVENANCE_MISSING),
        ({"basis_artifacts": []}, TOPOLOGY_PROVENANCE_MISSING),
        ({"basis_artifacts": "a,b"}, TOPOLOGY_PROVENANCE_MISSING),
        ({"basis_artifacts": ["  "]}, TOPOLOGY_PROVENANCE_MISSING),
        ({"basis_artifacts": [7]}, TOPOLOGY_PROVENANCE_MISSING),
        ({"basis_note": 7}, TOPOLOGY_PROVENANCE_MISSING),
        ({"authority": 7}, TOPOLOGY_MALFORMED),
    ],
)
def test_one_malformed_field_names_its_own_reason(mutation, reason):
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, **mutation))
    assert excinfo.value.reason == reason


@pytest.mark.parametrize("field", ["fc_hz", "order", "basis_artifacts"])
def test_each_required_field_is_required(field):
    body = _pin(2400.0)
    del body[field]
    with pytest.raises(TopologyPrescriptionRefused):
        _read(body)


def test_a_float_order_is_refused_rather_than_truncated():
    """``int(4.7)`` is ``4``, and an order that quietly became a different
    order is exactly the silently-different candidate this gate exists to
    prevent."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, order=4.7))
    assert excinfo.value.reason == TOPOLOGY_ORDER_INVALID


def test_an_unsupported_order_is_told_which_orders_exist():
    """"6" is a well-formed integer and an ordinary thing to type; the refusal
    has to send a prescriber to the supported set, not to the shape."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, order=6))
    assert excinfo.value.reason == TOPOLOGY_ORDER_UNSUPPORTED
    for order in sorted(SUPPORTED_LR_ORDERS):
        assert str(order) in excinfo.value.detail


# --------------------------------------------------------------------------- #
# 4. The declared frequency bounds — and that they are the shared predicate's
# --------------------------------------------------------------------------- #


def test_a_corner_below_the_declared_floor_is_refused():
    """1500 Hz — under the DE250's 1600 Hz diaphragm-excursion floor. A genuine
    component-damage stop, and #2870 leaves it exactly where it was."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(1500.0))
    assert excinfo.value.reason == FC_REJECT_BELOW_DECLARED_FLOOR
    # The declaration the operator has to go and edit, named in the sentence.
    assert "1600.0" in excinfo.value.detail


def test_a_corner_above_the_lower_drivers_ceiling_is_refused():
    """5000 Hz — above the woofer's own declared 4000 Hz hard ceiling. The
    second damage stop, likewise untouched by #2870."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(5000.0))
    assert excinfo.value.reason == FC_REJECT_ABOVE_LOWER_DRIVER_BAND
    assert "4000.0" in excinfo.value.detail


def test_the_corner_the_invented_search_band_used_to_refuse_is_admitted():
    """#2870's acceptance case, on jts3's own declared bands.

    2600 Hz is the closest horn/woofer beamwidth match for the 190 mm R-OSSE
    waveguide (112.0 deg against 115.2 deg). Both declarations admit it
    comfortably — it is a kilohertz above the tweeter's 1600 Hz floor and well
    under the woofer's 4000 Hz ceiling — yet it was refused
    ``outside_declared_search_band`` purely by an invented 2500 Hz edge that
    named no mechanism. That was the nanny the ruling removed.
    """
    pinned = _read(_pin(2600.0))
    assert pinned is not None and pinned.fc_hz == 2600.0
    # And the receipt records the two bounds it actually cleared, so a reader
    # six weeks later can see WHICH declarations admitted it.
    assert pinned.checked_against_floor_hz == DECLARED_FLOOR_HZ
    assert pinned.checked_against_ceiling_hz == LOWER_CEILING_HZ


def test_the_gate_has_no_third_frequency_bound_left():
    """The deletion, pinned at the gate rather than only at the predicate.

    Nothing between the two declared hard edges may refuse a pin any more, and
    the retired keyword is gone from the signature rather than merely ignored —
    a caller still passing it learns immediately instead of believing a bound
    is being applied that is not.
    """
    for corner_hz in (1600.0, 1800.0, 2400.0, 2600.0, 3000.0, 4000.0):
        assert _read(_pin(corner_hz)) is not None, corner_hz
    with pytest.raises(TypeError):
        _read(_pin(2600.0), search_band_hz=(1600.0, 2500.0))


@pytest.mark.parametrize("corner_hz", [
    1400.0, 1599.9, 1600.0, 1600.1, 1648.7, 2000.0,
    2499.9, 2500.0, 2500.1, 2600.0, 3999.9, 4000.0, 4000.1, 5000.0,
])
def test_a_pin_and_a_declared_corner_are_admissible_on_identical_terms(corner_hz):
    """The single-owner proof, and the reason the gate imports a private name.

    An operator who pins the corner their declarations already permit must not
    be refused, and one who pins a corner those declarations exclude must not be
    admitted. Two spellings of the same comparisons is exactly how those two
    answers drift apart on one speaker — so the gate asks
    ``fc_sweep._fc_rejection``, and this walks a grid across every declared
    edge to prove it still does.

    Neither side has a beaming term: #1675 makes the ka onset guidance rather
    than a fence, so the shared predicate carries none.
    """
    expected = _fc_rejection(
        corner_hz, DECLARED_FLOOR_HZ, LOWER_CEILING_HZ,
    )
    if expected is None:
        pinned = _read(_pin(corner_hz))
        assert pinned is not None and pinned.fc_hz == corner_hz
        return
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(corner_hz))
    assert excinfo.value.reason == expected


@pytest.mark.parametrize("corner_hz", [1600.0, 2500.0])
def test_a_corner_exactly_at_a_declared_edge_is_legal(corner_hz):
    """The 2026-08-17 owner ruling, applied to every edge: "if the
    manufacturer says 1600, we should be able to do it. no nannies." A strict
    comparison would make a round's legality depend on floating-point noise."""
    pinned = _read(_pin(corner_hz))
    assert pinned is not None
    assert pinned.fc_hz == corner_hz


# --------------------------------------------------------------------------- #
# 5. The slope bound — the refusal a tournament's order-2 candidate exists to produce
# --------------------------------------------------------------------------- #


def test_an_order_below_the_declared_slope_is_refused_with_both_numbers():
    """C3's receipt.

    Nothing downstream applies this: the declared clamp is read by the
    COMMISSIONING admission path and by the derived protection filter, while
    crossover apply compares corner FREQUENCIES only. Without this refusal an
    order-2 candidate at a legal corner runs with less sub-Fc attenuation than
    the tweeter's own declaration asks for, silently, on a receipt carrying
    the candidate's name.
    """
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, order=2))
    assert excinfo.value.reason == TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT
    # Both halves of the comparison, so the sentence is actionable without
    # going to look the declaration up.
    assert "12 dB/octave" in excinfo.value.detail
    assert "24 dB/octave" in excinfo.value.detail


def test_an_order_exactly_meeting_the_declared_slope_is_legal():
    """Inclusive, for the declared-edge ruling's reason: order 4 IS 24 dB per
    octave, and the declaration asks for 24."""
    pinned = _read(_pin(2400.0, order=4))
    assert pinned is not None
    assert pinned.slope_db_per_octave == DECLARED_SLOPE


def test_a_steeper_order_than_declared_is_legal():
    pinned = _read(_pin(2400.0, order=8))
    assert pinned is not None
    assert pinned.slope_db_per_octave == 48.0


def test_an_undeclared_slope_gates_nothing():
    """``None`` means no slope was declared — never a guessed default, on
    ``declared_protection_highpass_floor_hz``'s never-nanny rule. Inventing a
    requirement where the operator declared none is the nanny behaviour the
    2026-08-14 ruling excludes."""
    pinned = _read(_pin(2400.0, order=2), minimum_slope_db_per_octave=None)
    assert pinned is not None
    assert pinned.order == 2
    assert pinned.checked_against_slope_db_per_octave is None


def test_the_frequency_bounds_are_answered_before_the_slope():
    """The two send a prescriber to different places — re-declare the band
    versus re-choose the order — and a corner outside the hardware's declared
    range is the more fundamental of the two answers. A candidate that fails
    both must be told the frequency one."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(5000.0, order=2))
    assert excinfo.value.reason == FC_REJECT_ABOVE_LOWER_DRIVER_BAND


def test_the_de250_admits_order_2_because_its_maker_publishes_12():
    """The 2026-08-23 owner ruling, at jts3's real driver.

    B&C print "1.6 kHz — 12 dB/oct. or higher" for the DE250, and this pin —
    2400 Hz at order 2 — is what the gate refused as "below the protected
    driver's declared minimum of 24 dB/octave". Nobody declared 24: that was
    ``max(published, PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE)``, a code figure the
    2026-08-22 ruling bars from refusing anything. Order 2 IS 12 dB/octave, the
    corner clears both declared bands, so it is admitted — and disclosed.
    """
    pinned = _read(
        _pin(2400.0, order=2),
        minimum_slope_db_per_octave=DE250_PUBLISHED_SLOPE,
    )
    assert pinned is not None
    assert pinned.order == 2
    assert pinned.slope_db_per_octave == DE250_PUBLISHED_SLOPE
    assert pinned.checked_against_slope_db_per_octave == DE250_PUBLISHED_SLOPE


def test_the_de250s_declared_corner_still_refuses_a_pin_below_it():
    """The half of the envelope that DOES name a damage mechanism is untouched.

    De-nannying the slope did not widen the frequency gate: 1500 Hz is below the
    driver's declared 1.6 kHz floor and is refused at order 2 exactly as it is at
    order 4, by the shared predicate the automatic path uses.
    """
    for order in (2, 4):
        with pytest.raises(TopologyPrescriptionRefused) as excinfo:
            _read(
                _pin(1500.0, order=order),
                minimum_slope_db_per_octave=DE250_PUBLISHED_SLOPE,
            )
        assert excinfo.value.reason == FC_REJECT_BELOW_DECLARED_FLOOR


def test_a_published_slope_above_the_recommendation_still_refuses_order_2():
    """The declaration is the authority in BOTH directions.

    A maker publishing 24 gets order 2 refused, and a maker publishing 36 —
    above this build's own commissioning recommendation — gets order 4 refused
    too. Nothing about the ruling makes a published number advisory; it makes
    the UNPUBLISHED one advisory.
    """
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, order=2), minimum_slope_db_per_octave=24.0)
    assert excinfo.value.reason == TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, order=4), minimum_slope_db_per_octave=36.0)
    assert excinfo.value.reason == TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT
    assert "published minimum of 36 dB/octave" in excinfo.value.detail


def test_the_refusal_says_published_not_declared_minimum():
    """The message is the whole of what an operator reads, and the old one
    attributed this build's 24 to the manufacturer."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, order=2))
    assert "published minimum" in excinfo.value.detail
    assert "declared minimum" not in excinfo.value.detail


def test_an_admitted_shallow_pin_discloses_the_commissioning_recommendation():
    """Disclose-and-recommend, on the receipt rather than only on /sound/.

    ``crossover_preview`` warns a DESIGN below the 24 dB/octave floor, and until
    this ruling that was the only surface that named it — a pinned measurement
    arm crossed shallower with nothing on its record to say so. The number rides
    the prescription now, the beaming ceiling's sibling and read the same way.
    """
    pinned = _read(
        _pin(2400.0, order=2),
        minimum_slope_db_per_octave=DE250_PUBLISHED_SLOPE,
    )
    assert pinned is not None
    assert pinned.recommended_slope_db_per_octave == (
        PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )
    # The comparison a reader makes: this pin crossed shallower than recommended.
    assert pinned.slope_db_per_octave < pinned.recommended_slope_db_per_octave
    assert pinned.to_dict()["recommended_slope_db_per_octave"] == (
        PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )


def test_the_recommendation_is_disclosed_even_when_nothing_was_published():
    """The round whose receipt most needs it: no published condition at all, so
    no slope refusal was possible, and the recommendation is the only slope
    figure a later reader has."""
    pinned = _read(_pin(2400.0, order=2), minimum_slope_db_per_octave=None)
    assert pinned is not None
    assert pinned.checked_against_slope_db_per_octave is None
    assert pinned.recommended_slope_db_per_octave == (
        PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )


def test_the_disclosed_recommendation_survives_the_durable_read_back():
    """A receipt banked today must still say what the pin crossed against when
    it is graded, on the same round-trip rule the other stamped fields keep."""
    pinned = _read(
        _pin(2400.0, order=2),
        minimum_slope_db_per_octave=DE250_PUBLISHED_SLOPE,
    )
    assert pinned is not None
    again = topology_prescription_from_mapping(pinned.to_dict())
    assert again is not None
    assert again.recommended_slope_db_per_octave == (
        PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )
    assert again.checked_against_slope_db_per_octave == DE250_PUBLISHED_SLOPE


def test_a_receipt_banked_before_the_disclosure_still_reads_back():
    """A block this build wrote last week names no recommendation. It is a
    document this repository already wrote, so it loads with the field absent
    rather than being refused for not carrying a field it predates."""
    stale = _read(_pin(2400.0)).to_dict()
    del stale["recommended_slope_db_per_octave"]
    again = topology_prescription_from_mapping(stale)
    assert again is not None
    assert again.recommended_slope_db_per_octave is None
    assert again.fc_hz == 2400.0


def test_the_slope_relation_matches_the_one_confirmed_protection_uses():
    """``order * 6`` has one meaning in this repository.

    ``branch_chain.confirmed_protection_sections`` turns a declared slope into
    the smallest supported order that meets it by exactly this relation; this
    gate runs the same relation in the other direction. A second constant would
    make a declaration and its filter disagree about what "24 dB/octave" is.
    """
    for order in sorted(SUPPORTED_LR_ORDERS):
        pinned = _read(_pin(2400.0, order=order), minimum_slope_db_per_octave=None)
        assert pinned is not None
        assert pinned.slope_db_per_octave == order * 6.0


# --------------------------------------------------------------------------- #
# 6. The beaming prior is disclosed, never enforced
# --------------------------------------------------------------------------- #


def test_a_pin_above_the_beaming_onset_is_admitted_and_disclosed():
    """#1675 defines the ka ceiling as guidance to warn on rather than a fence,
    so no admissibility bound anywhere reads it — ``fc_sweep._fc_rejection``
    carries no beaming term for the automatic path either. A pinned corner IS
    its round's configured corner, so enforcing it here would be stricter about
    this speaker than that path is."""
    pinned = _read(_pin(2400.0), beaming_ceiling_hz=1800.0)
    assert pinned is not None
    # Admitted despite sitting ABOVE the onset…
    assert pinned.fc_hz > 1800.0
    # …and the receipt carries the number a reader compares it against, so the
    # candidate is disclosed rather than silently fine.
    assert pinned.beaming_ceiling_hz == 1800.0
    assert pinned.to_dict()["beaming_ceiling_hz"] == 1800.0


def test_a_pin_below_the_beaming_onset_still_records_what_it_cleared():
    pinned = _read(_pin(1700.0), beaming_ceiling_hz=1800.0)
    assert pinned is not None
    assert pinned.beaming_ceiling_hz == 1800.0
    assert pinned.fc_hz < 1800.0


def test_an_undeclared_diameter_records_an_absent_prior_not_a_cleared_one():
    """An absent prior is not a satisfied one, and the ``None`` is what says so
    — a receipt showing ``0.0`` would claim a comparison nobody could make."""
    pinned = _read(_pin(2400.0), beaming_ceiling_hz=None)
    assert pinned is not None
    assert pinned.beaming_ceiling_hz is None
    assert pinned.to_dict()["beaming_ceiling_hz"] is None


# --------------------------------------------------------------------------- #
# 7. What the gate stamps, and the authority caveat
# --------------------------------------------------------------------------- #


def test_the_gate_records_what_it_compared_this_pin_to():
    """A receipt that states only the corner cannot say what it cleared: a
    reader finding ``2400`` cannot otherwise tell a 2500 Hz declared ceiling
    from a 4000 Hz one, and the declaration is nowhere else in the block."""
    pinned = _read(_pin(2400.0), beaming_ceiling_hz=1800.0)
    assert pinned is not None
    assert pinned.checked_against_floor_hz == DECLARED_FLOOR_HZ
    assert pinned.checked_against_ceiling_hz == LOWER_CEILING_HZ
    assert pinned.checked_against_slope_db_per_octave == DECLARED_SLOPE
    assert pinned.beaming_ceiling_hz == 1800.0


def test_the_authority_caveat_is_stamped_on_every_accepted_pin():
    """The corner was PINNED by an operator from an offline argument, and no
    shipped path ranks one topology against another. A receipt read six weeks
    later must not be mistakable for a measured verdict, so the caveat travels
    on the record rather than living in a doc that outlives nothing."""
    pinned = _read(_pin(2400.0))
    assert pinned is not None
    assert pinned.authority == TOPOLOGY_AUTHORITY_OPERATOR_PINNED
    assert pinned.to_dict()["authority"] == TOPOLOGY_AUTHORITY_OPERATOR_PINNED


def test_a_request_cannot_forge_the_stamped_fields():
    """They are accepted on the way in so a durable block round-trips through
    ONE parser — and overwritten, so a request that supplies them is harmless
    rather than authoritative."""
    pinned = _read(_pin(
        2400.0,
        authority="peer_reviewed_measurement",
        checked_against_floor_hz=1.0,
        checked_against_ceiling_hz=99999.0,
        checked_against_slope_db_per_octave=0.0,
        beaming_ceiling_hz=99999.0,
        slope_db_per_octave=999.0,
    ))
    assert pinned is not None
    assert pinned.authority == TOPOLOGY_AUTHORITY_OPERATOR_PINNED
    assert pinned.checked_against_floor_hz == DECLARED_FLOOR_HZ
    assert pinned.checked_against_ceiling_hz == LOWER_CEILING_HZ
    assert pinned.checked_against_slope_db_per_octave == DECLARED_SLOPE
    assert pinned.beaming_ceiling_hz is None
    # Derived, never read: a request claiming 999 dB/octave gets order * 6.
    assert pinned.to_dict()["slope_db_per_octave"] == 24.0


# --------------------------------------------------------------------------- #
# 8. Every raise names a member of the closed vocabulary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        ["not", "a", "mapping"],
        {"fc_hz": 2400.0, "order": 4, "basis_artifacts": ["a"], "nope": 1},
        {"order": 4, "basis_artifacts": ["a"]},
        {"fc_hz": "2400", "order": 4, "basis_artifacts": ["a"]},
        {"fc_hz": 2400.0, "basis_artifacts": ["a"]},
        {"fc_hz": 2400.0, "order": 6, "basis_artifacts": ["a"]},
        {"fc_hz": 2400.0, "order": 4},
        {"fc_hz": 2600.0, "order": 4, "basis_artifacts": ["a"]},
        {"fc_hz": 1500.0, "order": 4, "basis_artifacts": ["a"]},
        {"fc_hz": 2400.0, "order": 2, "basis_artifacts": ["a"]},
    ],
)
def test_every_refusal_carries_a_reason_from_the_closed_set(raw):
    """By type and code, never by prose — the rule ``PLAN_REFUSAL_REASONS``
    sets. A caller must be able to branch without reading a message."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(raw)
    assert excinfo.value.reason in TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS


def test_a_way_one_speaker_refuses_the_door_rather_than_blaming_its_corner():
    """``full_range_passive`` has no crossover region to re-topologize, and
    saying THAT is a different answer from an inadmissible corner — which sends
    a prescriber to re-choose a number that can never exist."""
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0), way_count=1)
    assert excinfo.value.reason == TOPOLOGY_NO_CROSSOVER_REGION
    # Two remedies, so two slugs — they may never be collapsed into one.
    assert TOPOLOGY_NO_CROSSOVER_REGION != TOPOLOGY_FC_INVALID
    # A real two-way is untouched: the fact is the topology's, not the pin's.
    assert _read(_pin(2400.0), way_count=2) is not None


def test_the_response_format_advertises_exactly_the_refusals_that_exist():
    """#2773's discoverability, kept honest: a prescriber reading the contract
    block learns the same vocabulary the gate actually raises."""
    advertised = topology_prescription_response_format()["refusals"]
    assert set(advertised) == set(TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS)


def test_the_response_format_names_the_request_time_door_and_its_severity():
    """The other two prescription classes stage through the prescriber CLI;
    this one and the alignment pin are request-body keys whose refusal takes
    the whole session. A reader who found only the staged contracts would never
    learn the request-time doors exist."""
    block = topology_prescription_response_format()
    assert block["key"] == TOPOLOGY_PRESCRIPTION_KEY
    assert block["entry"] == "request_body"
    assert "jasper-crossover-prescriber" in block["entry_detail"]
    assert "refuses the whole session" in block["severity"]
    assert block["authority"] == TOPOLOGY_AUTHORITY_OPERATOR_PINNED
    # The caveat a prescriber must read before believing a pinned receipt.
    assert "not a measured ranking" in block["authority_detail"]
    # The envelope a prescriber must send, discoverable beside the content
    # fields rather than left implicit.
    assert str(TOPOLOGY_PRESCRIPTION_KIND) in block["fields"]["kind"]
    assert str(TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION) in (
        block["fields"]["artifact_schema_version"]
    )


# --------------------------------------------------------------------------- #
# 9. The durable read-back — same shape, deliberately not the bounds
# --------------------------------------------------------------------------- #


def test_the_read_back_does_not_reapply_the_bounds():
    """A prescription whose DECLARATIONS moved between the stage that measured
    the round and the stage that grades it must still be readable: refusing
    there could only discard the evidence of a round that really ran. The
    bounds have one owner, and it is the boundary."""
    banked = _read(_pin(2400.0))
    assert banked is not None
    # A record from a round whose speaker has since re-declared a narrower
    # ceiling. It was legal when it was accepted; grading must still read it.
    record = banked.to_dict()
    record["checked_against_ceiling_hz"] = 2000.0
    again = topology_prescription_from_mapping(record)
    assert again is not None
    assert again.fc_hz == 2400.0


#: A receipt this repository ACTUALLY WROTE before #2870, emitted by
#: ``origin/main``'s own ``read_topology_prescription`` at jts3's declarations
#: on 2026-08-22 and frozen verbatim — not a hand-typed approximation of one.
#: Its ``checked_against_search_band_hz`` is the field the ruling retired.
BANKED_PRE_2870_RECEIPT = {
    "artifact_schema_version": 1,
    "authority": "operator_pinned_no_measured_ranking",
    "basis_artifacts": ["armloop-first-drive-2026-08/offline-fc-search"],
    "basis_note": "offline candidate search, no measured ranking",
    "beaming_ceiling_hz": None,
    "checked_against_ceiling_hz": 4000.0,
    "checked_against_floor_hz": 1600.0,
    "checked_against_search_band_hz": [1600.0, 2500.0],
    "checked_against_slope_db_per_octave": 24.0,
    "fc_hz": 2400.0,
    "kind": "jts_crossover_topology_prescription",
    "order": 4,
    "slope_db_per_octave": 24.0,
}


def test_a_receipt_banked_before_the_field_was_retired_still_reads_back(caplog):
    """#2870 hazard 2, pinned: the deploy that deletes a field must not make
    every receipt written before it unreadable.

    ``_PRESCRIPTION_FIELDS`` refuses an unknown field rather than ignoring it,
    so a banked receipt carrying ``checked_against_search_band_hz`` would have
    become ``None`` at grading — silently discarding the provenance of a round
    that really ran. The read-back drops a RETIRED field instead, on the same
    posture split the pre-envelope tolerance already uses.

    ``None`` is the failure mode to catch, so the WARNING is asserted absent:
    a receipt that reads back but logs unreadable is still a receipt somebody
    will go looking for.
    """
    with caplog.at_level(logging.WARNING):
        again = topology_prescription_from_mapping(dict(BANKED_PRE_2870_RECEIPT))
    assert again is not None
    assert again.fc_hz == 2400.0
    assert again.order == 4
    # Everything the build still speaks survives the drop unharmed.
    assert again.checked_against_floor_hz == 1600.0
    assert again.checked_against_ceiling_hz == 4000.0
    assert again.checked_against_slope_db_per_octave == 24.0
    assert again.authority == TOPOLOGY_AUTHORITY_OPERATOR_PINNED
    assert again.basis_artifacts == (
        "armloop-first-drive-2026-08/offline-fc-search",
    )
    assert "crossover_v2_topology_prescription_unreadable" not in caplog.text
    # DROPPED, never read: the retired value must not reappear on the way out,
    # or a deleted concept keeps riding receipts this build cannot interpret.
    assert "checked_against_search_band_hz" not in again.to_dict()


def test_the_request_gate_still_refuses_a_freshly_authored_retired_field():
    """The other half of the posture split, and the reason it is a split.

    Tolerance is for documents this repository already wrote. A prescriber
    AUTHORING ``checked_against_search_band_hz`` today is talking to a build
    that no longer exists, and must learn at the tap rather than have the field
    silently dropped — which would look like a bound being applied that is not.
    """
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(2400.0, checked_against_search_band_hz=[1600.0, 2500.0]))
    assert excinfo.value.reason == TOPOLOGY_MALFORMED
    assert "checked_against_search_band_hz" in excinfo.value.detail


def test_the_read_back_still_refuses_a_mangled_shape_and_says_so(caplog):
    """A hand-edited or truncated state file is a real input here, and a round
    graded at the wrong corner is worse than one graded with no provenance.
    ``None`` plus one WARNING, so an empty slot on a receipt is always
    distinguishable from a silently mangled one."""
    with caplog.at_level(logging.WARNING):
        assert topology_prescription_from_mapping({"fc_hz": 2400.0}) is None
    assert "crossover_v2_topology_prescription_unreadable" in caplog.text


def test_a_mangled_durable_block_reads_as_absent_never_as_half_a_prescription():
    """The tolerant-read rule every door in this family shares, for a record
    that is genuinely unreadable rather than merely pre-envelope.

    Mirrors ``tests/test_crossover_v2_driver_prescription.py``'s
    ``test_a_mangled_durable_block_reads_as_absent_never_as_half_a_
    prescription``: ``None``, an unrecognised ``kind``, and a totally empty
    mapping (missing ``fc_hz``/``order`` too, so this is not the retrofit
    case) all read as ``None`` rather than raising. See
    ``test_a_pre_envelope_record_round_trips_through_the_read_back`` for the
    shape that DOES carry a real pin and DOES round-trip.
    """
    assert topology_prescription_from_mapping(None) is None
    assert topology_prescription_from_mapping({"kind": "nope"}) is None
    assert topology_prescription_from_mapping({}) is None


def test_a_pre_envelope_record_round_trips_through_the_read_back():
    """The retrofit contract: durable state predates this envelope.

    ``verify_priors.topology_prescription`` is carried unconditionally across
    a deploy (``correction_crossover_v2.persist_conductor_state``), and
    #2662/#2773 shipped writing it days before this envelope existed, so a
    live speaker can already hold a record naming neither ``kind`` nor
    ``artifact_schema_version``. Refusing it would silently grade a pinned
    round's VERIFY against the crossover the speaker used to run — see
    :func:`~jasper.active_speaker.crossover_v2.topology_prescription.
    _parse_prescription`'s ``read_back`` paragraph.

    Generated from a REAL pinned prescription's own ``to_dict()`` with the
    two envelope keys removed, not hand-typed, so this is exactly the shape a
    prior build wrote rather than a guess at it.
    """
    pinned = _read(_pin(2400.0))
    assert pinned is not None
    pre_envelope_record = pinned.to_dict()
    del pre_envelope_record["kind"]
    del pre_envelope_record["artifact_schema_version"]
    recovered = topology_prescription_from_mapping(pre_envelope_record)
    assert recovered is not None
    assert recovered.fc_hz == pinned.fc_hz
    assert recovered.order == pinned.order
    assert recovered.basis_artifacts == pinned.basis_artifacts


@pytest.mark.parametrize("keep", ["kind", "artifact_schema_version"])
def test_naming_only_one_envelope_field_is_not_the_legacy_shape(keep):
    """EITHER field present, even correctly, with the other missing, is not
    the wholly-absent shape the retrofit tolerates — it tried to speak the
    envelope and got it wrong."""
    pinned = _read(_pin(2400.0))
    assert pinned is not None
    record = pinned.to_dict()
    other = "artifact_schema_version" if keep == "kind" else "kind"
    del record[other]
    assert topology_prescription_from_mapping(record) is None


def test_a_future_schema_version_still_refuses_even_on_read_back():
    """The retrofit posture tolerates a wholly-absent envelope, never a
    present-but-wrong one — a document naming a version this build does not
    speak is refused under both the request gate and the durable read-back."""
    pinned = _read(_pin(2400.0))
    assert pinned is not None
    record = pinned.to_dict()
    record["artifact_schema_version"] = 2
    assert topology_prescription_from_mapping(record) is None


def test_the_read_back_of_nothing_is_nothing_and_is_silent(caplog):
    """The ordinary round: no pin was made. It must not look like a failure."""
    with caplog.at_level(logging.WARNING):
        assert topology_prescription_from_mapping(None) is None
    assert caplog.text == ""


def test_a_pre_gate_record_reads_back_without_the_stamped_fields():
    """A hand-built block that never went through the gate is missing the
    GATE's own context, not malformed — refusing it would cost a round its
    provenance entirely.

    ``kind``/``artifact_schema_version`` are on the OTHER side of that line:
    they are part of the document's required SHAPE, exactly as ``fc_hz`` /
    ``order`` / ``basis_artifacts`` always were, so this record carries them —
    what it omits is only the five fields the GATE stamps after its bounds
    pass (``authority``, the four ``checked_against_*`` / beaming fields).
    """
    again = topology_prescription_from_mapping({
        "kind": TOPOLOGY_PRESCRIPTION_KIND,
        "artifact_schema_version": TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION,
        "fc_hz": 2400.0, "order": 4, "basis_artifacts": ["bench"],
    })
    assert again == TopologyPrescription(
        fc_hz=2400.0, order=4, basis_artifacts=("bench",),
    )
    assert again.authority == ""
    assert again.checked_against_floor_hz is None


# --------------------------------------------------------------------------- #
# 10. The pin reaches the filters, and the region keeps a name apply accepts
# --------------------------------------------------------------------------- #


def test_a_pinned_order_reaches_both_branches_filters():
    """A round that crosses at a prescribed order must compose, fit, predict,
    emit and verify through that order — not wear it as a label.

    Through the route production takes: ``apply_topology_pin`` re-corners the
    PRESET, and every downstream branch reads its sections back off that preset
    with ``sections_by_role``. So this asserts the pinned order survives the one
    hop that carries it to the filters.
    """
    import dataclasses

    from jasper.active_speaker.branch_chain import sections_by_role

    @dataclasses.dataclass(frozen=True)
    class _Preset:
        crossover_regions: tuple

    @dataclasses.dataclass(frozen=True)
    class _Frozen:
        id: str
        fc_hz: float
        order: int
        lower_driver: str = "woofer"
        upper_driver: str = "tweeter"

    preset = _Preset((_Frozen("woofer_tweeter_1649hz", INCUMBENT_HZ, 4),))
    moved = recornered_preset(preset, fc_hz=2400.0, order=2)
    sections = sections_by_role(moved.crossover_regions)
    assert set(sections) == {"woofer", "tweeter"}
    for role_sections in sections.values():
        for section in role_sections:
            assert section.fc_hz == 2400.0
            assert section.order == 2
    # …and the two halves still split the same way they always did.
    assert sections["woofer"][0].highpass is False
    assert sections["tweeter"][0].highpass is True


def test_the_recornered_region_id_is_spelled_the_way_apply_recompiles_it():
    """The contract with a module this one may not import.

    ``baseline_profile.build_baseline_profile`` admits a reviewed candidate
    only when its ``source_preset`` equals — by whole-dataclass ``!=``, ``id``
    included — the preset ``staging.compile_preset_from_crossover_preview``
    recompiles from the SAVED declaration, which spells it
    ``f"{lower_role}_{upper_role}_{int(round(frequency))}hz"``. A region left
    named for the old corner, or given a name recompilation can never produce
    (an ``_lr2`` suffix, say), is refused ``measured_candidate_preset_mismatch``
    forever. Change this format only together with staging's.
    """
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class _Preset:
        crossover_regions: tuple

    @dataclasses.dataclass(frozen=True)
    class _Frozen:
        id: str
        fc_hz: float
        order: int
        lower_driver: str = "woofer"
        upper_driver: str = "tweeter"

    preset = _Preset((_Frozen("woofer_tweeter_1649hz", INCUMBENT_HZ, 4),))
    moved = recornered_preset(preset, fc_hz=2400.0, order=2)
    region = moved.crossover_regions[0]
    # Exactly staging's spelling, and NOTHING about the order in it.
    assert region.id == f"woofer_tweeter_{int(round(2400.0))}hz"
    assert region.id == "woofer_tweeter_2400hz"
    assert region.fc_hz == 2400.0
    assert region.order == 2


def test_recornering_without_an_order_leaves_the_declared_order_alone():
    """The automatic path's behaviour, byte for byte: ``order`` moves ONLY when
    an operator pinned one, so an unpinned round keeps its declared slope."""
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class _Preset:
        crossover_regions: tuple

    @dataclasses.dataclass(frozen=True)
    class _Frozen:
        id: str
        fc_hz: float
        order: int
        lower_driver: str = "woofer"
        upper_driver: str = "tweeter"

    preset = _Preset((_Frozen("woofer_tweeter_1649hz", INCUMBENT_HZ, 4),))
    region = recornered_preset(preset, fc_hz=2750.0).crossover_regions[0]
    assert region.id == "woofer_tweeter_2750hz"
    assert region.order == 4


# --------------------------------------------------------------------------- #
# 11. The pre-flight, as an executable record
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("arm", "corner_hz", "order"),
    [
        ("C1", 2600.0, 4),
        ("C2", 4000.0, 4),
    ],
)
def test_the_pre_registered_tournament_arms_run_on_jts3s_own_declarations(
    arm, corner_hz, order,
):
    """The pre-flight, banked so it cannot be re-derived wrongly later — and
    the clearest single measure of what #2870 changed.

    Until the 2026-08-22 ruling all three pre-registered candidates were refused
    ``outside_declared_search_band``: they sit above a 2500 Hz edge that neither
    driver declared as a limit on anything, so the tournament could not run at
    all. Both drivers' HARD bands admitted every one of them — 2600 and 4000 are
    inside [1600, 4000] — and now the gate says so.

    C3 is deliberately not in this table: its corner is legal now, so its
    order-2 slope becomes its FIRST refusal rather than its second (below).
    """
    pinned = _read(_pin(corner_hz, order=order))
    assert pinned is not None, arm
    assert pinned.fc_hz == corner_hz
    assert pinned.order == order


def test_the_order_2_candidate_is_refused_for_its_slope_now_its_corner_is_legal():
    """C3, whose second refusal used to be hidden behind its corner.

    jts3's tweeter declares 24 dB/octave; order 2 is 12. That is a declaration
    about what the crossover's own filter must do, so it still refuses — the
    ruling deleted a bound that named no mechanism, not the ones that do.
    """
    with pytest.raises(TopologyPrescriptionRefused) as excinfo:
        _read(_pin(4000.0, order=2))
    assert excinfo.value.reason == TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT


def test_a_candidates_topology_is_read_off_its_own_preset():
    """The household surface reports what the CANDIDATE crosses at.

    Not what a session believes it asked for. On a pinned round the two agree
    by construction, and this is the reading that keeps saying so rather than
    assuming it — a candidate built at one corner and labelled with another is
    the incoherence the whole pin exists to prevent, so the label comes off the
    graph.
    """
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class _Preset:
        crossover_regions: tuple

    @dataclasses.dataclass(frozen=True)
    class _Frozen:
        id: str
        fc_hz: float
        order: int
        lower_driver: str = "woofer"
        upper_driver: str = "tweeter"

    candidate = SimpleNamespace(
        source_preset=_Preset((_Frozen("woofer_tweeter_2200hz", 2200.0, 8),)),
    )
    assert candidate_topology(candidate) == {
        "fc_hz": 2200.0,
        "order": 8,
        # Derived from THIS candidate's order, never a constant: order 8 is 48
        # dB/octave, and a reader that hardcoded the common order-4 answer would
        # tell a household its 48 dB/octave crossover was 24.
        "slope_db_per_octave": 48.0,
    }


@pytest.mark.parametrize(
    "regions",
    [
        (),
        None,
    ],
)
def test_a_candidate_with_no_crossover_region_names_no_corner(regions):
    """``sections_by_role`` already reads an absent region as "this role runs
    full range". There is no corner to name, and inventing one would be the
    guess that function refuses to make."""
    candidate = SimpleNamespace(
        source_preset=SimpleNamespace(crossover_regions=regions),
    )
    assert candidate_topology(candidate) is None


def test_a_candidate_with_no_preset_at_all_names_no_corner():
    """The duck-typed read must not raise on a stand-in that carries none — a
    ``/state`` projection is fail-soft, and a missing corner is honestly absent
    rather than an exception on a snapshot path."""
    assert candidate_topology(SimpleNamespace()) is None
    assert candidate_topology(None) is None


@pytest.mark.parametrize("order", ["4", 4.0, True, None])
def test_a_candidate_whose_order_is_unreadable_names_no_corner(order):
    """Half a topology is worse than none on a household surface: a corner with
    no trustworthy slope would render a number nobody can check."""
    candidate = SimpleNamespace(
        source_preset=SimpleNamespace(
            crossover_regions=(SimpleNamespace(fc_hz=2200.0, order=order),),
        ),
    )
    assert candidate_topology(candidate) is None


def test_applying_no_pin_returns_the_very_same_preset():
    """The automatic path must not rebuild its own crossover regions.

    ``is`` and not ``==``: an equal copy would still be a new object every
    ordinary round, and the cheapest way to be sure nothing was re-cornered is
    to be handed the same object back.
    """
    preset = SimpleNamespace(crossover_regions=())
    assert apply_topology_pin(None, preset=preset, fc_hz=INCUMBENT_HZ) == (
        preset, INCUMBENT_HZ,
    )
    assert apply_topology_pin(None, preset=preset, fc_hz=INCUMBENT_HZ)[0] is preset


def test_applying_a_pin_moves_the_preset_and_the_corner_together():
    """One decision, both halves — the thing two hand-written call sites drifted
    apart on before this function existed."""
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class _Preset:
        crossover_regions: tuple

    @dataclasses.dataclass(frozen=True)
    class _Frozen:
        id: str
        fc_hz: float
        order: int
        lower_driver: str = "woofer"
        upper_driver: str = "tweeter"

    pinned = _read(_pin(2200.0, order=8))
    assert pinned is not None
    preset = _Preset((_Frozen("woofer_tweeter_1649hz", INCUMBENT_HZ, 4),))
    moved, corner = apply_topology_pin(
        pinned, preset=preset, fc_hz=INCUMBENT_HZ,
    )
    assert corner == 2200.0
    region = moved.crossover_regions[0]
    assert (region.fc_hz, region.order) == (2200.0, 8)
    assert region.id == "woofer_tweeter_2200hz"
    # …and the corner the caller gets back is the region's own, so a session
    # opened from this pair cannot hold two crossovers.
    assert corner == region.fc_hz


def test_the_announced_capture_program_follows_the_corner_it_is_built_at():
    """WHY a pinned round's capture plan must be built at the pinned corner.

    The session and the capture spec are built from what used to be one number,
    and a pin that moved only the session would leave two corners in one round.
    The entry-baseline program the plan announces is stage 2's own anchor — the
    pair ``program_for_phase`` compares — and ``build_verify_program`` is
    fc-dependent in TWO places:

    * the summed sweep's low bound, ``min(VERIFY_F_LO_HZ, fc / 2)``, live below
      fc = 300 Hz;
    * the leading pilot's high bound,
      ``min(VERIFY_PILOT_F_HI_HZ, fc / VERIFY_PILOT_FC_CLEARANCE_RATIO)``, live
      below fc = **2000 Hz**.

    The second is the one that matters, and an earlier version of this test
    missed it: it covers most of a two-way's legal pin band — all of jts3's
    1600-2500 Hz up to 2000 — so the two-corner bug was live on the very rounds
    this door exists to run, not the sub-300 Hz curiosity the first reading
    called it. Both are pinned here so neither can be argued away again.

    The web boundary now passes its own ``session_fc_hz`` / ``verify_fc_hz`` to
    both spec builders. That WIRING is not covered by a test: the builders sit
    inside a capture-hosting callback this suite cannot reach without the
    autouse stage harness. Verified by inspection at both call sites in
    ``jasper/web/correction_crossover_v2.py``.
    """
    from jasper.audio_measurement.program import (
        VERIFY_F_LO_HZ,
        VERIFY_PILOT_F_HI_HZ,
        VERIFY_PILOT_FC_CLEARANCE_RATIO,
        build_verify_program,
    )

    def _pilot_hi(fc_hz: float) -> float:
        program = build_verify_program(fc_hz, leading_pilot_gains_db=(-30.0, -20.0))
        pilots = [
            seg for seg in program.segments
            if seg.f2_hz is not None and seg.segment_id != "sweep_verify"
        ]
        assert pilots, "the verify program announces no leading pilot"
        return float(pilots[0].f2_hz)

    # THE SWEEP's low bound: inert above 300 Hz, live below it.
    assert build_verify_program(2400.0).segment("sweep_verify").f1_hz == VERIFY_F_LO_HZ
    assert build_verify_program(250.0).segment("sweep_verify").f1_hz == 125.0

    # THE PILOT's high bound: live all the way to 2000 Hz, which is what puts
    # the divergence inside jts3's own declared 1600-4000 Hz pin band.
    clamped_at = VERIFY_PILOT_F_HI_HZ * VERIFY_PILOT_FC_CLEARANCE_RATIO
    assert clamped_at == 2000.0
    assert _pilot_hi(DECLARED_FLOOR_HZ) == 640.0
    assert _pilot_hi(INCUMBENT_HZ) == INCUMBENT_HZ / VERIFY_PILOT_FC_CLEARANCE_RATIO
    assert _pilot_hi(1800.0) == 720.0
    # …and only from 2000 Hz up does it stop moving.
    assert _pilot_hi(2000.0) == VERIFY_PILOT_F_HI_HZ
    assert _pilot_hi(LOWER_CEILING_HZ) == VERIFY_PILOT_F_HI_HZ
    # The incumbent and a legal pin inside the same band announce DIFFERENT
    # programs — the whole reason the spec must take the round's own corner.
    assert _pilot_hi(INCUMBENT_HZ) != _pilot_hi(1800.0)


def test_an_arm_inside_the_declared_band_at_a_declared_slope_is_admitted():
    """…and the door is not merely a wall: a tournament re-registered inside
    what this speaker actually declares runs."""
    pinned = _read(_pin(2200.0, order=4))
    assert pinned is not None
    assert (pinned.fc_hz, pinned.order) == (2200.0, 4)
    assert pinned.basis_artifacts == ARTIFACTS
