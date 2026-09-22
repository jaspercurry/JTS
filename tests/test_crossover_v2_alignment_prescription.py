# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Covers the request gate (shape, provenance, and the one derivation of the
bound), the aligner's commitment of a prescribed delay, the single-owner claim
end to end into the emitted graph and its proof, the round receipt's
provenance, and — the control that matters most — that a session with no
prescription selects exactly what it selected before.

The numbers in the candidate-set tests are the series-2 diagnosis's,
deliberately: the harness exists to measure those candidates, and a test
written against placeholder values would not have caught that the ``0 µs``
control candidate cannot be expressed as a prescription at all.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.contracts import POLARITY_INVERT, POLARITY_KEEP
from jasper.active_speaker.crossover_v2 import coordinator
from jasper.active_speaker.crossover_v2.alignment_prescription import (
    ALIGNMENT_NO_CROSSOVER_REGION,
    ALIGNMENT_PRESCRIPTION_KEY,
    ALIGNMENT_PRESCRIPTION_KIND,
    ALIGNMENT_PRESCRIPTION_MALFORMED,
    ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS,
    ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED,
    ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
    PRESCRIPTION_DELAY_INVALID,
    PRESCRIPTION_FC_UNKNOWN,
    PRESCRIPTION_OUTSIDE_DECLARED_WINDOW,
    PRESCRIPTION_POLARITY_INVALID,
    AlignmentPrescription,
    AlignmentPrescriptionRefused,
    alignment_prescription_from_mapping,
    alignment_prescription_response_format,
    read_alignment_prescription,
)
from jasper.active_speaker.crossover_v2.planning import alignment_to_candidate_fields
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    MeasuredCrossoverCandidateError,
    compile_candidate_config,
    prove_candidate_config,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_ESTIMATED_FLAT_SUM,
    ALIGNMENT_OK,
    AlignmentEstimate,
    MeasurementPriors,
    _build_candidate,
    half_period_us,
    polarity_label,
)

from tests._log_events import event_fields, event_records
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_audio_measurement_program_analysis import (
    SR,
    _band_impulse,
    _roles,
    _synthesize,
)

# --------------------------------------------------------------------------- #
# The series-2 rig, as the banked state records it.
# --------------------------------------------------------------------------- #

#: The commissioned corner on jts3, from ``series2-state-r1b.json``'s
#: ``recomposition_snapshot.preset.crossover_regions[0].fc_hz``.
FC_HZ = 1648.7
#: The measured inter-driver arrival gap: the woofer arrives 405.7 µs EARLIER,
#: so the alignment-correcting delay is negative (it delays the woofer).
#: −405.7 ± 3.3 µs over n = 33, diagnosis §2.2.
BASIS_US = -405.7
#: The diagnosis's corrected candidate set (§3), including the ``0`` control.
ARMS_US = (0.0, -250.0, -350.0, -450.0, -550.0)
#: Every corner the banked r1b ``fc_selection`` actually swept.
SWEPT_CORNERS_HZ = (1600.0, 1648.7, 1658.6, 1719.4, 1782.4, 1847.7)
ARTIFACTS = (
    "captures/xover-series2-2026-08-17/diagnosis/landscape_delay_polarity_r1b.json",
    "captures/xover-series2-2026-08-17/diagnosis/item5_phase_share.json",
)


#: Tonight's speaker's declared delay window: `preview-default-2way` carries
#: ``delay_range_ms = [0.0, 1.0]`` (verified in ``series2-state-r1b.json``),
#: which ``alignment_delay_search_bounds_us`` margin-expands by 0.1 ms to
#: 0–1100 µs. Every candidate is representable here.
TONIGHT_WINDOW_US = (0.0, 1100.0)
#: The OTHER shipped preset shape, and the one that would have bitten:
#: ``bc_de250_dayton_e150he44_v1`` declares ``[0.05, 0.3]`` ms → 0–400 µs, so
#: the two best candidates are outside the hardware's own declaration.
HORN_WINDOW_US = (0.0, 400.0)


def _read(
    body: object,
    *,
    fc_hz: float = FC_HZ,
    declared_bounds_us=TONIGHT_WINDOW_US,
    way_count: int | None = None,
):
    """The gate as the request boundary calls it, on tonight's rig."""
    return read_alignment_prescription(
        body,
        fc_hz=fc_hz,
        declared_bounds_us=declared_bounds_us,
        way_count=way_count,
    )


def _arm(arm_us: float, **overrides: object) -> dict:
    """One candidate as the session POST body carries it.

    The positional parameter is deliberately NOT called ``delay_us``: several
    tests below override that very key, and a same-named parameter would make
    those calls a ``TypeError`` instead of a mutation.
    """
    body = {
        "kind": ALIGNMENT_PRESCRIPTION_KIND,
        "artifact_schema_version": ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
        "delay_us": arm_us,
        "basis_delay_us": BASIS_US,
        "basis_artifacts": list(ARTIFACTS),
        "basis_note": "direct arrival gap -405.7 +/- 3.3 us, n=33",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# 1. The bound — one derivation, and it is edge-exact
# --------------------------------------------------------------------------- #


def test_the_bound_is_a_half_period_at_fc_and_nothing_else():
    lobe_us = half_period_us(FC_HZ)
    widest = _read(
        _arm(BASIS_US + lobe_us), fc_hz=FC_HZ,
    )
    assert widest is not None
    assert widest.residual_us == pytest.approx(lobe_us, abs=1e-9)


@pytest.mark.parametrize("direction", (1.0, -1.0))
def test_exactly_at_the_lobe_and_past_it_are_disclosed(direction):
    lobe_us = half_period_us(FC_HZ)
    at_bound = BASIS_US + direction * lobe_us
    assert abs(at_bound - BASIS_US) <= lobe_us
    assert _read(_arm(at_bound)).to_dict()["out_of_lobe"] is False
    past_delay = at_bound
    for _ in range(8):
        if abs(past_delay - BASIS_US) > lobe_us:
            break
        past_delay = float(np.nextafter(past_delay, direction * np.inf))
    assert abs(past_delay - BASIS_US) > lobe_us
    receipt = _read(_arm(past_delay)).to_dict()
    assert receipt["out_of_lobe"] is True
    assert receipt["residual_us"] == past_delay - BASIS_US
    assert receipt["lobe_us"] == lobe_us


def test_the_arm_set_and_control_disclose_residuals():
    receipts = {arm: _read(_arm(arm)).to_dict() for arm in ARMS_US}
    assert {arm: receipt["residual_us"] for arm, receipt in receipts.items()} == {
        0.0: pytest.approx(405.7), -250.0: pytest.approx(155.7),
        -350.0: pytest.approx(55.7), -450.0: pytest.approx(-44.3), -550.0: pytest.approx(-144.3),
    }
    assert {arm: receipt["out_of_lobe"] for arm, receipt in receipts.items()} == {
        arm: arm == 0 for arm in ARMS_US
    }


def test_every_swept_corner_discloses_the_same_four_arms_inside_the_lobe():
    for fc_hz in SWEPT_CORNERS_HZ:
        receipts = {arm: _read(_arm(arm), fc_hz=fc_hz).to_dict() for arm in ARMS_US}
        assert tuple(arm for arm, receipt in receipts.items() if not receipt["out_of_lobe"]) == (-250.0, -350.0, -450.0, -550.0)


def test_the_lobe_is_measured_from_the_basis_not_from_the_incumbent():
    incumbent_us = 96.0
    assert abs(incumbent_us - BASIS_US) / half_period_us(FC_HZ) == pytest.approx(1.6543, abs=5e-4)
    assert abs(incumbent_us - BASIS_US) / (1e6 / FC_HZ) == pytest.approx(0.8272, abs=5e-4)
    assert _read(_arm(-450.0)).out_of_lobe is False
    assert _read(_arm(incumbent_us)).out_of_lobe is True


@pytest.mark.parametrize("mutation,fc_hz", [
    ({}, None), ({}, 0.0), ({}, -1648.7), ({}, float("nan")), ({}, float("inf")), ({}, True), ({}, "x"),
    ({"basis_delay_us": "-405.7"}, FC_HZ), ({"basis_delay_us": False}, FC_HZ), ({"basis_delay_us": float("nan")}, FC_HZ),
    ({"basis_artifacts": []}, FC_HZ), ({"basis_artifacts": None}, FC_HZ),
    ({"basis_artifacts": ["  "]}, FC_HZ), ({"basis_artifacts": [None]}, FC_HZ),
    ({"basis_artifacts": "one.json,two.json"}, FC_HZ), ({"basis_note": 7}, FC_HZ),
])
def test_missing_or_unusable_context_is_disclosed(mutation, fc_hz):
    raw = _arm(-450.0)
    for key in ("basis_delay_us", "basis_artifacts", "basis_note"):
        raw.pop(key)
    if fc_hz != FC_HZ:
        raw["basis_delay_us"] = BASIS_US
    accepted = _read({**raw, **mutation}, fc_hz=fc_hz)
    receipt = accepted.to_dict()
    assert receipt["delay_us"] == -450.0
    assert receipt["basis_delay_us"] == (BASIS_US if fc_hz != FC_HZ else None)
    assert receipt["residual_us"] == (pytest.approx(-44.3) if fc_hz != FC_HZ else None)
    assert receipt["out_of_lobe"] is None
    assert receipt["basis_artifacts"] == []
    assert receipt["basis_note"] == ""
    assert receipt["lobe_us"] == (half_period_us(FC_HZ) if fc_hz == FC_HZ else None)
    assert alignment_prescription_from_mapping(receipt) == accepted
    assert {"prescription_out_of_lobe", "prescription_basis_invalid", "prescription_fc_unknown", "prescription_provenance_missing"}.isdisjoint(ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS)


def test_a_way_one_speaker_refuses_the_door_rather_than_blaming_its_corner():
    """``full_range_passive`` has no crossover region at all, so this door does
    not apply to it — and saying so is a different answer from "the corner is
    unusable", which sends a prescriber to re-derive an impossible number."""
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(-450.0), way_count=1)
    assert excinfo.value.reason == ALIGNMENT_NO_CROSSOVER_REGION
    # Two remedies, so two slugs — they may never be collapsed into one.
    assert ALIGNMENT_NO_CROSSOVER_REGION != PRESCRIPTION_FC_UNKNOWN
    # A real two-way is untouched: the fact is the topology's, not the request's.
    assert _read(_arm(-450.0), way_count=2) is not None


# --------------------------------------------------------------------------- #
# 2. Delay and envelope shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"delay_us": "-450"}, PRESCRIPTION_DELAY_INVALID),
        ({"delay_us": True}, PRESCRIPTION_DELAY_INVALID),
        ({"delay_us": None}, PRESCRIPTION_DELAY_INVALID),
        ({"delay_us": float("nan")}, PRESCRIPTION_DELAY_INVALID),
        ({"delay_us": float("-inf")}, PRESCRIPTION_DELAY_INVALID),
        # The typo that would otherwise silently drop the provenance.
        ({"basis_artifact": ["x"]}, ALIGNMENT_PRESCRIPTION_MALFORMED),
    ],
)
def test_the_reader_is_strict_about_shape(mutation, reason):
    """A string ``float()`` would coerce is refused, and so is a bool.

    ``isinstance(True, int)`` is ``True`` in Python and ``float(True)`` is
    ``1.0``, so a delay of "true" microseconds would otherwise validate. The
    string rule is :func:`blend_filters_from_mapping`'s: accepting one makes
    the reader's strictness depend on the encoder's habits.
    """
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(-450.0, **mutation), fc_hz=FC_HZ)
    assert excinfo.value.reason == reason


@pytest.mark.parametrize("raw", (["not", "a", "mapping"], "text", 7))
def test_a_non_mapping_prescription_is_refused(raw):
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(raw, fc_hz=FC_HZ)
    assert excinfo.value.reason == ALIGNMENT_PRESCRIPTION_MALFORMED


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"kind": "nope"}, ALIGNMENT_PRESCRIPTION_MALFORMED),
        ({"kind": None}, ALIGNMENT_PRESCRIPTION_MALFORMED),
        ({"artifact_schema_version": 2}, ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED),
        ({"artifact_schema_version": None}, ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED),
    ],
)
def test_the_envelope_is_checked_before_any_content_field(mutation, reason):
    """The version+kind envelope, on the established shape.

    Mirrors :mod:`~jasper.active_speaker.crossover_v2.driver_prescription`'s
    own gate: a document naming the wrong kind is malformed, and one naming a
    version this build does not speak is its own, distinct refusal — sent to
    the vocabulary rather than merged into the generic shape reason.
    """
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(-450.0, **mutation), fc_hz=FC_HZ)
    assert excinfo.value.reason == reason


def test_every_refusal_reason_is_in_the_closed_vocabulary():
    """A caller branches on the reason, so the reason has to be a member."""
    raised = set()
    for body, fc_hz in (
        ("not a mapping", FC_HZ),
        (_arm(-450.0, delay_us="x"), FC_HZ),
        (_arm(-450.0, polarity="inverted"), FC_HZ),
        (_arm(-450.0, artifact_schema_version=2), FC_HZ),
    ):
        with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
            _read(body, fc_hz=fc_hz)
        raised.add(excinfo.value.reason)
    # The declared-window refusal needs a NARROWER preset than tonight's to
    # fire at all, which is exactly why it is the one bound here an
    # operator-supplied basis cannot talk its way past.
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(-450.0), declared_bounds_us=HORN_WINDOW_US)
    raised.add(excinfo.value.reason)
    # The way-1 refusal is the other bound no request body can reach.
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(-450.0), way_count=1)
    raised.add(excinfo.value.reason)
    assert raised <= ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS
    assert raised == ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS


# --------------------------------------------------------------------------- #
# 2b. the optional basin pin — the field, and the words it admits
# --------------------------------------------------------------------------- #
#
# Why the field exists at all, from the 2026-08-19 linearization night: three
# successive stage-1 fits at ONE physical configuration solved three different
# basins — (tweeter +34 µs, keep), (woofer +314 µs, invert), (woofer +314 µs,
# keep). The second measured best off axis (2.37 vs 3.10 dB pooled) and was
# kept; the third is the anti-phase notch (3.86 on axis, auto-rolled back). A
# staged EQ round could not hold the measured-best basin, so a one-variable
# round was not expressible.


@pytest.mark.parametrize(
    ("word", "sign"), [(POLARITY_KEEP, 1), (POLARITY_INVERT, -1)],
)
def test_a_pinned_basin_survives_the_gate_as_a_word_and_a_sign(word, sign):
    """Both directions: the bug is symmetric, so the pin has to be.

    The word is what a prescriber writes and what the receipt banks; the sign is
    what the fit searches over. One record owns both so the two cannot drift.
    """
    prescription = _read(_arm(-450.0, polarity=word))

    assert prescription.polarity == word
    assert prescription.polarity_sign == sign
    # …and the bound is unaffected: pinning a basin is not a delay excursion.
    assert prescription.residual_us == pytest.approx(-44.3)


def test_an_unpinned_prescription_is_the_automatic_path_for_the_polarity():
    """Absent and explicit ``null`` are one answer, and it is "do not pin"."""
    assert _read(_arm(-450.0)).polarity is None
    assert _read(_arm(-450.0)).polarity_sign is None
    assert _read(_arm(-450.0, polarity=None)).polarity_sign is None


@pytest.mark.parametrize(
    "value",
    (
        # The measurement frame's words — the likeliest mistake, because the
        # journal and `analysis_json` both speak them one layer away.
        "inverted", "normal",
        # The third polarity ACTION, which a candidate does not admit either.
        "review",
        # Shapes an encoder or a hand-edit produces.
        "flip", "KEEP", "", -1, 1, True,
        # Unhashable: `x in frozenset` would raise TypeError past every refusal
        # handler rather than naming a reason.
        ["keep"], {"polarity": "keep"},
    ),
)
def test_an_unknown_basin_is_refused_by_name_never_ignored(value):
    """A silently-dropped pin would leave the round measuring a re-rolled basin.

    Under the candidate's name, which is the same class of dishonesty the
    delay half of this gate exists to prevent — so it refuses, and refuses
    with its OWN reason rather than the generic malformed one, because a
    misspelled basin sends an operator to the vocabulary and not to the
    shape.
    """
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(-450.0, polarity=value))

    assert excinfo.value.reason == PRESCRIPTION_POLARITY_INVALID
    assert "keep" in excinfo.value.detail and "invert" in excinfo.value.detail


def test_the_pinnable_basins_are_exactly_the_ones_a_candidate_admits():
    """One vocabulary, pinned as a contract rather than as two literals.

    The gate and
    :class:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverAlignment`
    read the two words from the same module, but nothing structural stops one
    side from later admitting a third. A pin the candidate would refuse is a
    round that dies ten minutes downstream instead of at the tap.
    """
    from jasper.active_speaker import measured_crossover_candidate as mcc
    from jasper.active_speaker.crossover_v2 import alignment_prescription as ap

    assert ap._PINNABLE_POLARITIES == mcc._POLARITY_VALUES


def test_the_presets_declared_window_is_asked_at_the_tap_not_ten_minutes_later():
    """The one bound here that does not rest on a requester-supplied number.

    ``crossover_v2_flow.alignment_delay_plausible`` has always screened the
    committed delay against the preset's ``delay_range_ms`` — but downstream,
    inside the MEASURE capture rungs, at a screen whose household copy asks the
    user to move the microphone. On a prescribed candidate that is a lie
    about a number the request could have been refused for immediately, so
    the gate asks the same declaration at the tap under its own reason.

    Pinned on BOTH shipped shapes, because the difference is the whole point:
    ``bc_de250_dayton_e150he44_v1`` declares 0.05–0.3 ms (0–400 µs expanded)
    and would refuse the two best candidates outright, while tonight's speaker
    declares 0–1 ms (0–1100 µs) and admits every one.
    """
    horn_verdicts = {}
    for arm_us in ARMS_US:
        try:
            _read(_arm(arm_us), declared_bounds_us=HORN_WINDOW_US)
            horn_verdicts[arm_us] = "accepted"
        except AlignmentPrescriptionRefused as exc:
            horn_verdicts[arm_us] = exc.reason
    assert horn_verdicts == {
        0.0: "accepted",
        -250.0: "accepted",
        -350.0: "accepted",
        -450.0: PRESCRIPTION_OUTSIDE_DECLARED_WINDOW,
        -550.0: PRESCRIPTION_OUTSIDE_DECLARED_WINDOW,
    }

    # Tonight's rig: the window is not what is doing any work.
    tonight = {
        arm_us: _read(_arm(arm_us)).delay_us
        for arm_us in ARMS_US if arm_us != 0.0
    }
    assert tonight == {-250.0: -250.0, -350.0: -350.0, -450.0: -450.0, -550.0: -550.0}


def test_the_declared_window_refuses_and_the_lobe_discloses():
    both_wrong = _arm(-900.0)
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(both_wrong, declared_bounds_us=HORN_WINDOW_US)
    assert excinfo.value.reason == PRESCRIPTION_OUTSIDE_DECLARED_WINDOW
    assert _read(both_wrong, declared_bounds_us=None).to_dict()["out_of_lobe"] is True


def test_a_preset_that_declares_no_window_discloses_the_lobe():
    """``None`` is ``alignment_delay_search_bounds_us``'s own answer for a
    preset with no ``delay_range_ms``, and it means nothing to gate on — the
    same posture ``alignment_delay_plausible`` takes. It must not read as a
    zero-width window that refuses everything.
    """
    assert _read(_arm(-450.0), declared_bounds_us=None) is not None


def test_no_prescription_reads_as_the_automatic_path():
    """Absence is not a refusal — it is every ordinary round."""
    assert _read(None, fc_hz=FC_HZ) is None
    assert _read({}.get(ALIGNMENT_PRESCRIPTION_KEY),
                                       fc_hz=FC_HZ) is None


# --------------------------------------------------------------------------- #
# 3. The durable read-back: same shape rules, deliberately not the bound
# --------------------------------------------------------------------------- #


def test_the_read_back_preserves_an_out_of_lobe_delay():
    out_of_lobe = _arm(0.0)
    assert _read(out_of_lobe, fc_hz=FC_HZ).to_dict()["out_of_lobe"] is True
    recovered = alignment_prescription_from_mapping(out_of_lobe)
    assert recovered is not None
    assert recovered.delay_us == 0.0


def test_the_read_back_still_refuses_a_mangled_record(caplog):
    """A hand-edited state file must not become half a provenance."""
    with caplog.at_level(logging.WARNING):
        assert alignment_prescription_from_mapping(_arm(-450.0, delay_us="invalid")) is None
    assert event_records(
        caplog, "correction.crossover_v2_alignment_prescription_unreadable"
    )
    assert alignment_prescription_from_mapping(None) is None


def test_a_prescription_round_trips_through_its_receipt_shape():
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    record = prescription.to_dict()
    assert record["residual_us"] == pytest.approx(-44.3)
    assert record["basis_artifacts"] == list(ARTIFACTS)
    assert alignment_prescription_from_mapping(
        {k: v for k, v in record.items() if k != "residual_us"}
    ) == prescription


def test_the_receipt_carries_kind_and_schema_version():
    """The envelope, on the established shape.

    Mirrors :data:`~jasper.active_speaker.crossover_v2.driver_prescription.
    DriverPrescription.to_dict`'s ``kind``/``artifact_schema_version`` pair —
    the same fields, the same job: a reader handed this record can tell what
    it is without guessing from its field names alone.
    """
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    record = prescription.to_dict()
    assert record["kind"] == ALIGNMENT_PRESCRIPTION_KIND
    assert record["artifact_schema_version"] == ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION


def test_a_mangled_durable_block_reads_as_absent_never_as_half_a_prescription():
    """The tolerant-read rule every door in this family shares, for a record
    that is genuinely unreadable rather than merely pre-envelope.

    Mirrors ``tests/test_crossover_v2_driver_prescription.py``'s
    ``test_a_mangled_durable_block_reads_as_absent_never_as_half_a_
    prescription``: ``None``, an unrecognised ``kind``, and a totally empty
    mapping (missing ``delay_us``/``basis_delay_us`` too, so this is not the
    retrofit case) all read as ``None`` rather than raising. See
    ``test_a_pre_envelope_record_round_trips_through_the_read_back`` for the
    shape that DOES carry a real prescription and DOES round-trip.
    """
    assert alignment_prescription_from_mapping(None) is None
    assert alignment_prescription_from_mapping({"kind": "nope"}) is None
    assert alignment_prescription_from_mapping({}) is None


def test_a_pre_envelope_record_round_trips_through_the_read_back():
    """The retrofit contract: durable state predates this envelope.

    ``verify_priors.alignment_prescription`` is carried unconditionally
    across a deploy (``correction_crossover_v2.persist_conductor_state``),
    and #2662/#2773 shipped writing it days before this envelope existed, so
    a live speaker can already hold a record naming neither ``kind`` nor
    ``artifact_schema_version``. Refusing it would silently mis-grade a round
    already in flight — see :func:`~jasper.active_speaker.crossover_v2.
    alignment_prescription._parse_prescription`'s ``read_back`` paragraph.

    Generated from a REAL accepted prescription's own ``to_dict()`` with the
    two envelope keys removed, not hand-typed, so this is exactly the shape a
    prior build wrote rather than a guess at it.
    """
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    pre_envelope_record = prescription.to_dict()
    del pre_envelope_record["kind"]
    del pre_envelope_record["artifact_schema_version"]
    recovered = alignment_prescription_from_mapping(pre_envelope_record)
    assert recovered is not None
    assert recovered.delay_us == prescription.delay_us
    assert recovered.basis_delay_us == prescription.basis_delay_us
    assert recovered.basis_artifacts == prescription.basis_artifacts


@pytest.mark.parametrize("keep", ["kind", "artifact_schema_version"])
def test_naming_only_one_envelope_field_is_not_the_legacy_shape(keep):
    """EITHER field present, even correctly, with the other missing, is not
    the wholly-absent shape the retrofit tolerates — it tried to speak the
    envelope and got it wrong."""
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    record = prescription.to_dict()
    other = "artifact_schema_version" if keep == "kind" else "kind"
    del record[other]
    assert alignment_prescription_from_mapping(record) is None


def test_a_future_schema_version_still_refuses_even_on_read_back():
    """The retrofit posture tolerates a wholly-absent envelope, never a
    present-but-wrong one — a document naming a version this build does not
    speak is refused under both the request gate and the durable read-back."""
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    record = prescription.to_dict()
    record["artifact_schema_version"] = 2
    assert alignment_prescription_from_mapping(record) is None


def test_the_response_format_advertises_exactly_the_refusals_that_exist():
    """#2773's discoverability, kept honest, mirroring
    ``test_crossover_v2_topology_prescription.py``'s own test of the same
    name: a prescriber reading the contract block learns the same vocabulary
    the gate actually raises."""
    advertised = alignment_prescription_response_format()["refusals"]
    assert set(advertised) == set(ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS)


def test_the_response_format_names_the_request_time_door_and_its_severity():
    """The other two prescription classes stage through the prescriber CLI;
    this one and the topology pin are request-body keys whose refusal takes
    the whole session. Before this test the alignment door had NO discovery
    surface at all — a prescriber could only learn its shape by reading the
    module source."""
    block = alignment_prescription_response_format()
    assert block["key"] == ALIGNMENT_PRESCRIPTION_KEY
    assert block["entry"] == "request_body"
    assert "jasper-crossover-prescriber" in block["entry_detail"]
    assert "refuses the whole session" in block["severity"]
    # The envelope is discoverable in the same block a prescriber reads for
    # every other field, not left to be learned by a refusal.
    assert str(ALIGNMENT_PRESCRIPTION_KIND) in block["fields"]["kind"]
    assert str(ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION) in (
        block["fields"]["artifact_schema_version"]
    )


def test_the_old_unprefixed_names_colliding_with_blend_prescription_are_gone():
    """This module's own members of the three-name collision with
    :mod:`.blend_prescription` — ``PRESCRIPTION_MALFORMED`` /
    ``PRESCRIPTION_PROVENANCE_MISSING`` / ``PRESCRIPTION_REFUSAL_REASONS``,
    each renamed here to an ``ALIGNMENT_``-prefixed name. The bare names must
    not still be attributes of this module.
    """
    from jasper.active_speaker.crossover_v2 import alignment_prescription as ap

    assert not hasattr(ap, "PRESCRIPTION_MALFORMED")
    assert not hasattr(ap, "PRESCRIPTION_PROVENANCE_MISSING")
    assert not hasattr(ap, "PRESCRIPTION_REFUSAL_REASONS")
    assert ap.ALIGNMENT_PRESCRIPTION_MALFORMED == "prescription_malformed"
    assert "prescription_provenance_missing" not in ap.ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS


# --------------------------------------------------------------------------- #
# 4. The aligner commits the prescription — exactly, and says so
# --------------------------------------------------------------------------- #


def _lr4_branches(fc_hz=FC_HZ, n_bins=4097, f_max_hz=24_000.0):
    """The same complementary LR4 pair the selector suite uses."""
    freqs = np.linspace(0.0, f_max_hz, n_bins)
    s = 1j * freqs / fc_hz
    butter2 = s * s + np.sqrt(2.0) * s + 1.0
    return freqs, (1.0 / butter2) ** 2, ((s * s) / butter2) ** 2


def _overlapping_branches() -> tuple[np.ndarray, np.ndarray]:
    """Two BAND-LIMITED driver IRs whose passbands overlap across Fc.

    Bare impulses will not do, and finding that out is what a mutation harness
    is for: two full-band deltas sum to a magnitude the residual delay barely
    moves (three distinct dB values across the whole curve), so a test asserting
    "these two candidates predict identically" passed even with the anchor
    withdrawal switched OFF. Band-limited branches that actually overlap
    make the summed magnitude a real function of the residual, which is
    the only way the withdrawal's consequence is observable at all.
    """
    return (
        _band_impulse(200, 150.0, 6000.0, 1.0, n=8192),
        _band_impulse(211, 300.0, 20000.0, 0.7, n=8192),
    )


def _low_snr_candidate_at(prescribed_us: float):
    """One candidate off a real capture the SNR verdict refused for alignment."""
    woofer_ir, tweeter_ir = _overlapping_branches()
    alignment = AlignmentEstimate(
        delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=-3 / 48_000 * 1e6, snapped_delay_us=None,
    )
    return _build_candidate(
        woofer_ir, tweeter_ir, 48_000, 16_384, FC_HZ, "woofer", "tweeter",
        alignment, None,
        alignment_delay_bounds_us=(0.0, 1100.0),
        branch_snr_insufficient=True,
        explicit_alignment_delay_us=prescribed_us,
    )


def test_the_low_snr_arm_withdraws_the_anchor_and_its_model_goes_arm_blind():
    """Low-SNR prescriptions cannot trust the measured arrival anchor."""
    a, (freqs_a, pred_a) = _low_snr_candidate_at(-350.0)
    b, (freqs_b, pred_b) = _low_snr_candidate_at(-550.0)

    # Both committed their own candidate, exactly…
    assert (a.delay_us, b.delay_us) == (-350.0, -550.0)
    assert a.alignment_objective == ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR
    # …the withdrawal fired, so neither model carries a residual…
    assert a.snap_delta_us != pytest.approx(0.0, abs=1e-9)
    # …and the prediction is therefore identical across two different
    # candidates.
    assert np.array_equal(freqs_a, freqs_b)
    assert np.array_equal(pred_a, pred_b)


def test_a_trusted_capture_keeps_its_residual_so_its_model_tracks_the_arm():
    """The other side of the same membership, and why it is not symmetric.

    On a capture that PASSED its SNR verdict the anchor is trustworthy, so the
    prescription's commitment stays out of the declared-polarity set and its
    model carries ``prescribed − anchor``. Two candidates then predict
    differently — the pre-apply net the low-SNR candidate above does without.
    """
    woofer_ir, tweeter_ir = _overlapping_branches()

    def _at(prescribed_us):
        alignment = AlignmentEstimate(
            delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
            polarity="normal", polarity_sign=1, confidence=0.9,
            status=ALIGNMENT_OK,
            anchor_delay_us=-3 / 48_000 * 1e6, snapped_delay_us=None,
        )
        return _build_candidate(
            woofer_ir, tweeter_ir, 48_000, 16_384, FC_HZ, "woofer", "tweeter",
            alignment, None,
            alignment_delay_bounds_us=(0.0, 1100.0),
            explicit_alignment_delay_us=prescribed_us,
        )

    a, (_fa, pred_a) = _at(-350.0)
    b, (_fb, pred_b) = _at(-550.0)
    assert a.alignment_objective == ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    assert b.alignment_objective == ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    assert not np.array_equal(pred_a, pred_b)


def _analyzed(prescribed_us: float | None, polarity_sign: int | None = None):
    """A REAL published :class:`ProgramAnalysis`, prescription and all.

    Through ``analyze_program_capture`` rather than ``_build_candidate``: the
    two-owner defect this pins lived at the PUBLISH site, one function further
    out, so a test that stopped at the candidate could not see it — and did
    not, for one mutation round. The branches are inverted relative to each
    other, so correlation reads the seed as inverted and the flat sum has a
    real disagreement available to it.
    """
    from jasper.audio_measurement.program import build_measure_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        analyze_program_capture,
    )

    program = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    capture = _synthesize(
        program,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, -0.7),
        epsilon=0.0,
    )
    return analyze_program_capture(
        program, capture, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=2000.0,
            explicit_alignment_delay_us=prescribed_us,
            explicit_alignment_polarity_sign=polarity_sign,
        ),
        geometry=MeasurementGeometry(),
    )


def test_the_publish_site_carries_the_selections_answer_not_its_own():
    """The two-owner defect, pinned where it actually lived.

    ``_analyze_measure`` re-derived this cross-check against a single
    objective while the selection used the widened rule, so a PRESCRIBED round
    published ``None`` — "never asked" — to every durable surface while the
    journal said the comparison ran. Asserted on the published estimate, and
    against the candidate the same analysis carries, so the two cannot drift
    apart again without this failing.
    """
    automatic = _analyzed(None)
    assert automatic.alignment.polarity_agrees_with_sum is not None
    assert (
        automatic.alignment.polarity_agrees_with_sum
        is automatic.candidate.polarity_agrees_with_sum
    )

    prescribed = _analyzed(-450.0)
    assert prescribed.candidate.alignment_objective == (
        ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    )
    # The published answer is a real comparison, never "never asked"…
    assert prescribed.alignment.polarity_agrees_with_sum is None
    # …and it is the SAME object the selection produced.
    assert (
        prescribed.alignment.polarity_agrees_with_sum
        is prescribed.candidate.polarity_agrees_with_sum
    )


def test_an_arm_that_asked_nothing_publishes_none_not_a_false_agreement():
    """The other direction of the same honesty.

    On the low-SNR candidate the polarity is the DECLARATION, not a flat-sum
    result, so no comparison happened — and recording "correlation agreed"
    because nothing disagreed with it is the dishonesty the field exists to
    avoid.
    """
    candidate, _predicted = _low_snr_candidate_at(-450.0)
    assert candidate.alignment_objective == ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR
    assert candidate.polarity_agrees_with_sum is None


@pytest.mark.parametrize(
    ("pinned_sign", "word"), [(1, POLARITY_KEEP), (-1, POLARITY_INVERT)],
)
def test_a_pinned_basin_reaches_the_candidate_as_the_graphs_polarity_field(
    pinned_sign, word,
):
    """End to end, through the REAL analysis, to the field the graph applies.

    ``_analyzed`` runs ``analyze_program_capture`` on a synthesized capture
    whose branches are inverted relative to each other, so the automatic answer
    is a real result rather than a default — and the pin has to survive every
    hop from the prior to
    :func:`~jasper.active_speaker.crossover_v2.planning.alignment_to_candidate_fields`,
    which is where the measurement frame's word becomes the candidate's action.

    The prescribed delay rides through unchanged in both cases, which is what
    makes this a pin on the BASIN rather than on the whole alignment.
    """
    analysis = _analyzed(-450.0, polarity_sign=pinned_sign)

    assert analysis.alignment.polarity_sign == pinned_sign
    assert analysis.alignment.polarity == polarity_label(pinned_sign)
    # …and the published agreement is honestly absent, one hop from every
    # durable surface that reads it.
    assert analysis.alignment.polarity_agrees_with_sum is None

    _magnitude, _role, polarity = alignment_to_candidate_fields(
        analysis, roles=("woofer", "tweeter"),
    )
    assert polarity == word


@pytest.mark.parametrize("pinned_sign", (1, -1))
def test_a_pinned_basin_reaches_the_household_row_as_an_instruction(pinned_sign):
    """The pin survives into the frozen evidence the review screen is built from.

    #2607 S3 reopened by a new route: the household row words a polarity as
    "Inverted (measured)" unless something tells it otherwise, and a pinned
    round commits the same ``explicit_prescription_committed`` an unpinned
    prescription does — so the objective cannot be that something. The bit is,
    and it has two hops to survive before any screen sees it: the carry onto the
    candidate, and the freeze into ``analysis_json``. Either one silently
    dropped puts "measured" over an operator's instruction.

    Asserted on the REAL analysis, so both hops are exercised; the projection
    from here to the payload is pinned in ``tests/test_crossover_envelope_v2.py``
    and the copy itself in ``tests/js/crossover_polarity_provenance_test.mjs``.
    """
    from jasper.active_speaker.crossover_v2.planning import analysis_json

    analysis = _analyzed(-450.0, polarity_sign=pinned_sign)
    assert analysis.candidate.polarity_pinned is True
    assert analysis_json(analysis)["polarity_pinned"] is True


def test_an_unpinned_round_is_not_labelled_an_instruction():
    """The control, and the half that keeps the fix scoped.

    An unpinned prescription commits the SAME objective, so a fix keyed off the
    objective would have reworded every prescribed round. This is what fails if
    the bit ever starts being inferred rather than carried.
    """
    from jasper.active_speaker.crossover_v2.planning import analysis_json

    analysis = _analyzed(-450.0)
    assert analysis.candidate.polarity_pinned is False
    assert analysis_json(analysis)["polarity_pinned"] is False


def test_a_rejected_ripple_polish_reaches_the_durable_candidate_evidence(monkeypatch):
    """The disclosure's second hop, on the REAL analysis.

    A rejected polish commits the band-average seed, so ``trim_db`` and
    ``trim_band_average_db`` come out equal — byte-identical to an admitted
    no-op polish and to the one-sided skip. ``ripple_polish_rejected_delta_db``
    is the only field that separates the three, and it is only worth carrying
    if it survives the freeze into ``analysis_json``, which is what the receipt
    binds by fingerprint. Pinned here rather than in the unit test for the
    guard because the unit test stops at the candidate.

    **Mutation guard.** Dropping the key from ``analysis_json`` fails the last
    assertion while the candidate one still passes — which is exactly the
    silent half-wiring this pins against.
    """
    from jasper.active_speaker.crossover_v2.planning import analysis_json
    from jasper.audio_measurement import program_analysis as _pa

    # A polish beyond the coupled bound, so the guard rejects it.
    excursion_db = _pa.REALIZED_LEVEL_MATCH_TOLERANCE_DB + 1.0
    monkeypatch.setattr(
        _pa.dispatch, "solve_ripple_optimal_trim",
        lambda *a, **kw: (kw["seed_trim_db"] + excursion_db, 0.0, kw["seed_trim_db"]),
    )
    analysis = _analyzed(-450.0)
    rejected = analysis.candidate.ripple_polish_rejected_delta_db
    # Asserted, never skipped-if-absent: this fixture's ripple band straddles Fc
    # so the polish genuinely runs, and a conditional skip here would turn a
    # future fixture change into silent lost coverage.
    assert rejected is not None, "the polish did not run — fixture no longer straddles Fc"
    assert rejected == pytest.approx(excursion_db)
    assert analysis_json(analysis)["ripple_polish_rejected_delta_db"] == pytest.approx(
        excursion_db
    )


def test_an_unpinned_analysis_still_solves_its_own_basin():
    """The control for the pair above, and the regression pin for today.

    Same capture, same prescribed delay, no pin: the objective still answers the
    polarity question and still publishes an agreement. If the pin had leaked a
    default into the automatic path, this is what would go ``None``.
    """
    analysis = _analyzed(-450.0)

    assert analysis.alignment.polarity_agrees_with_sum is None


def test_the_selection_event_names_the_prescribed_delay(caplog):
    """The second disclosure surface: ``prescribed_delay_us`` on the selection
    line is what makes "this round's delay was prescribed, not searched"
    greppable in a journal.

    ``None`` on every ordinary round is half the contract — a field that were
    always present would not separate the two — so both are asserted.
    """
    freqs, W, T = _lr4_branches()
    woofer_ir = np.zeros(8192)
    tweeter_ir = np.zeros(8192)
    woofer_ir[1000] = 1.0
    tweeter_ir[1011] = 1.0
    del freqs, W, T

    def _emit(prescribed):
        alignment = AlignmentEstimate(
            delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
            polarity="normal", polarity_sign=1, confidence=0.9,
            status=ALIGNMENT_OK,
            anchor_delay_us=-3 / 48_000 * 1e6, snapped_delay_us=None,
        )
        caplog.clear()
        with caplog.at_level(logging.INFO):
            _build_candidate(
                woofer_ir, tweeter_ir, 48_000, 16_384, FC_HZ, "woofer", "tweeter",
                alignment, None,
                alignment_delay_bounds_us=(0.0, 1100.0),
                explicit_alignment_delay_us=prescribed,
            )
        return event_fields(caplog, "program_analysis.alignment_selection")

    assert _emit(-450.0)["prescribed_delay_us"] == "-450.0"

    # logfmt renders an absent value as `null`, not `None`.
    assert _emit(None)["prescribed_delay_us"] == "null"


def test_the_selection_event_names_the_prescribed_basin(caplog):
    """The deciding value for a basin sweep, on the line that decided it.

    Three states, because two would not separate them: ``null`` when no
    prescription was made at all (matching its delay sibling, so a non-null
    value stays greppable as "prescribed"), ``unpinned`` when a prescription
    left the basin to the objective, and the basin itself when one was pinned.

    Spelled in the analysis frame so the three polarity fields on this ONE line
    — ``seed_polarity``, ``prescribed_polarity``, ``polarity`` — read in one
    vocabulary and can be compared by eye. The request's own word (keep/invert)
    is what the receipt banks, and is asserted where the receipt is.
    """
    woofer_ir = np.zeros(8192)
    tweeter_ir = np.zeros(8192)
    woofer_ir[1000] = 1.0
    tweeter_ir[1011] = 1.0

    def _emit(prescribed, polarity_sign):
        alignment = AlignmentEstimate(
            delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
            polarity="normal", polarity_sign=1, confidence=0.9,
            status=ALIGNMENT_OK,
            anchor_delay_us=-3 / 48_000 * 1e6, snapped_delay_us=None,
        )
        caplog.clear()
        with caplog.at_level(logging.INFO):
            _build_candidate(
                woofer_ir, tweeter_ir, 48_000, 16_384, FC_HZ, "woofer", "tweeter",
                alignment, None,
                alignment_delay_bounds_us=(0.0, 1100.0),
                explicit_alignment_delay_us=prescribed,
                explicit_alignment_polarity_sign=polarity_sign,
            )
        return event_fields(caplog, "program_analysis.alignment_selection")

    assert _emit(None, None)["prescribed_polarity"] == "null"
    assert _emit(-450.0, None)["prescribed_polarity"] == "unpinned"

    inverted = _emit(-450.0, -1)
    assert inverted["prescribed_polarity"] == "inverted"
    # The pin decided the commitment, and the line says so: the agreement is
    # honestly absent rather than claiming a comparison.
    assert inverted["polarity"] == "inverted"
    assert inverted["polarity_agrees_with_sum"] == "null"

    assert _emit(-450.0, 1)["prescribed_polarity"] == "normal"


# --------------------------------------------------------------------------- #
# 5. The control: no prescription changes nothing
# --------------------------------------------------------------------------- #


def test_the_prior_defaults_to_absent():
    """A construction site that predates this field runs the automatic path."""
    assert MeasurementPriors().explicit_alignment_delay_us is None
    assert MeasurementPriors().explicit_alignment_polarity_sign is None


# --------------------------------------------------------------------------- #
# 6. ONE field, ONE owner — end to end into the emitted graph
# --------------------------------------------------------------------------- #


def _candidate_for(delay_us: float) -> MeasuredCrossoverCandidate:
    """A candidate whose alignment came from a prescribed delay.

    Built through the production fold — ``alignment_to_candidate_fields`` — so
    the sign convention under test is the shipped one rather than a restatement
    of it here.
    """
    estimate = AlignmentEstimate(
        delay_us=delay_us,
        raw_delay_us=delay_us,
        parallax_us=0.0,
        polarity="normal",
        polarity_sign=1,
        confidence=0.9,
        status=ALIGNMENT_OK,
    )
    analysis = type("_A", (), {"alignment": estimate})()
    magnitude, role, polarity = alignment_to_candidate_fields(
        analysis, roles=("woofer", "tweeter"),
    )
    return MeasuredCrossoverCandidate(
        program_id="prog-abc123",
        analysis={"drift_ppm": 12.5, "sweeps": ["w", "t", "w"]},
        source_preset=ActiveSpeakerPreset.from_mapping(_two_way_preset("mono")),
        role_attenuations_db={"woofer": 0.0, "tweeter": -3.5},
        alignment=MeasuredCrossoverAlignment(
            delay_us=magnitude, delay_role=role, polarity=polarity,
        ),
    )


def test_a_prescribed_delay_reaches_the_graph_as_the_one_delay_field():
    """The single-owner claim, at the far end of the chain.

    The prescription is folded by the SAME function every automatic round goes
    through, lands on the candidate's one alignment value, and is emitted as
    one Delay filter. A negative prescription delays the WOOFER — the sign is
    destroyed and re-encoded as the role, which is the fold's whole contract.
    """
    candidate = _candidate_for(-450.0)
    assert candidate.alignment.delay_us == 450.0
    assert candidate.alignment.delay_role == "woofer"
    assert candidate.alignment.polarity == POLARITY_KEEP

    yaml_text = compile_candidate_config(candidate, playback_device="hw:ActiveDAC")
    # Exactly one non-zero delay in the emitted graph, and it is the candidate.
    delays = [
        float(line.split(":", 1)[1])
        for line in yaml_text.splitlines()
        if line.strip().startswith("delay:")
    ]
    assert sorted(d for d in delays if d) == [pytest.approx(0.45)]


def test_moving_the_prescription_moves_that_field_and_nothing_else():
    """Mutation-pinned one-owner: two candidates, one changed line.

    Two same-sign candidates differ only in magnitude, so if any SECOND place
    in the emitted graph carried the delay this diff would be wider than one
    line.
    """
    a = compile_candidate_config(_candidate_for(-350.0), playback_device="hw:ActiveDAC")
    b = compile_candidate_config(_candidate_for(-450.0), playback_device="hw:ActiveDAC")
    changed = [
        (x, y) for x, y in zip(a.splitlines(), b.splitlines(), strict=True) if x != y
    ]
    assert len(changed) == 1
    assert changed[0][0].strip() == "delay: 0.3500"
    assert changed[0][1].strip() == "delay: 0.4500"


def test_a_prescribed_arm_is_proved_by_the_ordinary_derivation():
    """No bypass: the candidate goes through the same compile-and-prove every
    automatic candidate does, including the headroom recompute and the static
    delay binding proof.

    Mutation-pinned in the direction that matters — tamper the emitted delay
    and the proof refuses by its own code, so a candidate whose graph does not
    carry the prescribed number cannot reach a speaker.
    """
    candidate = _candidate_for(-450.0)
    yaml_text = compile_candidate_config(candidate, playback_device="hw:ActiveDAC")
    prove_candidate_config(candidate, yaml_text)
    # The headroom recompute ran on THIS candidate's graph.
    assert "active_baseline_headroom" in yaml_text

    tampered = yaml_text.replace("delay: 0.45", "delay: 0.35")
    assert tampered != yaml_text
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        prove_candidate_config(candidate, tampered)
    assert excinfo.value.code == "delay_graph_proof_failed"


# --------------------------------------------------------------------------- #
# 7. The receipt names the measured basis
# --------------------------------------------------------------------------- #


#: The minimal usable VERIFY analysis ``run_round`` grades, mirroring
#: ``tests/test_crossover_v2_round_wiring.py``'s own direct-round harness.
_USABLE_ANALYSIS = SimpleNamespace(
    capture_integrity=SimpleNamespace(failed=(), not_evaluated=()),
    verify_tracking={"max_db_notch_excluded": 0.1, "n_bins": 10},
    summed_response=None,
    program_id="prog-1",
)


def _evidence_for(prescription, objective=ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION):
    return coordinator.RoundEvidence(
        session_id="cap_direct",

        post_analysis=_USABLE_ANALYSIS,
        entry_baseline=None,
        spec_report=None,
        proposal_fingerprint="a" * 64,
        commanded_delta_present=False,
        realization_tolerance_db=1.0,
        reference_mark="design_axis",
        proposal_fingerprint_kind="candidate",
        candidate_fingerprint="b" * 64,
        delta_probe=None,
        round_ordinal=1,
        previous_objectives=None,
        alignment_prescription=prescription,
        alignment_objective=objective,
    )


def _evaluation_stub():
    """The two attributes ``_round_measurements`` reads, and nothing else."""
    return type("_E", (), {"blend": None, "region_benefit": None})()


def _round_measurements_for(prescription, **kwargs):
    return coordinator._round_measurements(
        _evidence_for(prescription, **kwargs), _evaluation_stub(),
    )


def test_an_adopted_arms_receipt_names_what_its_timing_rests_on():
    """The adoption record has to carry the provenance, not just the number."""
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    banked = _round_measurements_for(prescription)["alignment_prescription"]
    assert banked["delay_us"] == -450.0
    assert banked["basis_delay_us"] == BASIS_US
    assert banked["residual_us"] == pytest.approx(-44.3)
    assert banked["basis_artifacts"] == list(ARTIFACTS)
    assert "n=33" in banked["basis_note"]
    assert banked["objective"] == ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    assert banked["committed"] is True
    # …and what the residual was checked against, so a reader is not left
    # guessing which corner's lobe 44.3 µs cleared.
    assert banked["checked_at_fc_hz"] == pytest.approx(FC_HZ)
    assert banked["lobe_us"] == pytest.approx(half_period_us(FC_HZ))
    # A delay-only candidate banks an explicit "no basin was pinned", which is
    # a different fact from a receipt written before the field existed.
    assert banked["polarity"] is None


@pytest.mark.parametrize("word", (POLARITY_KEEP, POLARITY_INVERT))
def test_a_pinned_arms_receipt_banks_the_basin_in_the_operators_own_words(word):
    """What the round ASKED for, in the vocabulary it was asked in.

    The receipt is the surface a human reads a week later to know why a
    configuration was kept, and "invert" is the word the bench notes and the
    candidate's own alignment use. The analysis frame's ``inverted`` lives on
    the journal line beside the other two polarity fields; the two frames meet
    at exactly one translation, which
    ``test_a_pinned_basin_survives_the_gate_as_a_word_and_a_sign`` owns.
    """
    prescription = _read(_arm(-450.0, polarity=word), fc_hz=FC_HZ)
    banked = _round_measurements_for(prescription)["alignment_prescription"]

    assert banked["polarity"] == word
    # The pin does not change what the candidate asked of the timing.
    assert banked["delay_us"] == -450.0
    assert banked["committed"] is True


@pytest.mark.parametrize(
    ("objective", "committed"),
    [
        (ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION, True),
        (ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR, True),
        (ALIGNMENT_ESTIMATED_FLAT_SUM, False),
        # No candidate committed at all is a THIRD answer, not a "no".
        ("", None),
    ],
)
def test_the_receipt_says_whether_the_arm_actually_ran(objective, committed):
    """A candidate's provenance without its outcome can credit a round that
    never measured the candidate.

    The rail is reachable, not theoretical: an ``ALIGNMENT_OK`` estimate whose
    band holds no scorable bin makes ``_select_alignment_pair`` return ``None``,
    and the seed — the estimator's own lobe-hopping answer — is committed while
    the round still carries the prescription's name. A grader reading only the
    prescription would call that "the candidate measured better".
    """
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    banked = _round_measurements_for(
        prescription, objective=objective,
    )["alignment_prescription"]
    assert banked["objective"] == objective
    assert banked["committed"] is committed


def test_an_ordinary_round_banks_no_prescription_block():
    """Absence and presence each mean exactly one thing on a receipt."""
    assert "alignment_prescription" not in _round_measurements_for(None)


def test_the_prescription_is_not_an_instruction_the_next_round_inherits():
    """It rides with the MEASUREMENTS, never in the round identity.

    ``_round_identity`` is the instruction channel the next round reads back as
    its incumbent. A prescription there is how a candidate gets re-run
    without being asked for — each candidate of a delay sweep is prescribed
    explicitly.
    """
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    published: list[dict] = []
    decision = coordinator.run_round(
        _evidence_for(prescription),
        coordinator.RoundPorts(
            rollback_available=None,
            applied_boosts=(lambda: False),
            entry_graph_fingerprint=(lambda: "graph-1"),
            publish_round_receipt=(lambda payload: published.append(dict(payload))
                                   or "f" * 64),
        ),
    )
    # The banked receipt names the basis…
    assert len(published) == 1
    banked = published[0]["round_measurements"]["alignment_prescription"]
    assert banked["basis_delay_us"] == BASIS_US
    # …and the identity the NEXT round reads back carries no prescription at
    # all, so no candidate can be re-run without being asked for. Asserted
    # over the built mapping, not over a docstring.
    assert "alignment_prescription" not in repr(decision.receipt_identity)


def test_the_record_is_frozen():
    """A prescription cannot be edited after the gate accepted it."""
    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    assert isinstance(prescription, AlignmentPrescription)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prescription.delay_us = 0.0  # type: ignore[misc]
