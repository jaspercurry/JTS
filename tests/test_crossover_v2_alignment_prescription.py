# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2662: the explicit, bounded, provenance-carrying delay prescription.

Covers the request gate (shape, provenance, and the one derivation of the
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

from jasper.active_speaker.crossover_alignment import POLARITY_INVERT, POLARITY_KEEP
from jasper.active_speaker.crossover_v2 import coordinator
from jasper.active_speaker.crossover_v2.alignment_prescription import (
    ALIGNMENT_NO_CROSSOVER_REGION,
    ALIGNMENT_PRESCRIPTION_KEY,
    ALIGNMENT_PRESCRIPTION_KIND,
    ALIGNMENT_PRESCRIPTION_MALFORMED,
    ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
    ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS,
    ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED,
    ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
    PRESCRIPTION_BASIS_INVALID,
    PRESCRIPTION_DELAY_INVALID,
    PRESCRIPTION_FC_UNKNOWN,
    PRESCRIPTION_OUT_OF_LOBE,
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
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND,
    ALIGNMENT_COMMITMENTS,
    ALIGNMENT_DECLARED_POLARITY_OBJECTIVES,
    ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES,
    ALIGNMENT_OK,
    AlignmentEstimate,
    AppliedAlignment,
    MeasurementPriors,
    _build_candidate,
    _select_alignment_pair,
    half_period_us,
    polarity_label,
)

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
    """The gate and the aligner's own lobe tripwire share one geometry.

    Not a re-derivation: the reader is asked for the widest legal prescription
    and the answer has to BE ``half_period_us``, so the two cannot drift into
    two opinions about where a comb lobe ends.
    """
    lobe_us = half_period_us(FC_HZ)
    widest = _read(
        _arm(BASIS_US + lobe_us), fc_hz=FC_HZ,
    )
    assert widest is not None
    assert widest.residual_us == pytest.approx(lobe_us, abs=1e-9)


@pytest.mark.parametrize("direction", (1.0, -1.0))
def test_exactly_at_the_bound_is_legal_and_past_it_is_refused(direction):
    """Edge-exact, in BOTH directions, and refused by NAME.

    Exactness is legal in this repository's gates. A strict comparison would
    make the legality of a round depend on floating-point noise in the sixth
    decimal of a corner frequency — and, worse, would do it asymmetrically
    depending on which side of the basis the candidate sits.
    """
    lobe_us = half_period_us(FC_HZ)
    at_bound = BASIS_US + direction * lobe_us
    assert abs(at_bound - BASIS_US) <= lobe_us, "the at-bound candidate is at it"
    assert _read(_arm(at_bound), fc_hz=FC_HZ) is not None

    # The smallest representable step past the bound IN THE DELAY, walked until
    # the residual it produces actually exceeds the lobe — the sum's own ULP is
    # coarser than the lobe's, so nudging the lobe instead can round straight
    # back onto the boundary. Asserted rather than assumed, so the test cannot
    # quietly become a second copy of the at-bound case.
    past_delay = at_bound
    for _ in range(8):
        if abs(past_delay - BASIS_US) > lobe_us:
            break
        past_delay = float(np.nextafter(past_delay, direction * np.inf))
    assert abs(past_delay - BASIS_US) > lobe_us, "the past-bound candidate is past it"
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(past_delay), fc_hz=FC_HZ)
    assert excinfo.value.reason == PRESCRIPTION_OUT_OF_LOBE


def test_the_corrected_arm_set_clears_the_bound_and_the_control_arm_does_not():
    """Print-what-you-assert, on the candidates this harness was built to run.

    Four of the diagnosis's five candidates are admissible; the ``0 µs``
    control is not, because zero delay leaves the drivers 405.7 µs apart —
    241° of phase error at Fc, well outside the lobe. That is the correct
    verdict rather than a gap: refusing to bless a known-misaligned state as
    measurement-backed is what the gate is for, and "no prescription" is how
    a control candidate is run.
    """
    admitted = {}
    for arm_us in ARMS_US:
        try:
            admitted[arm_us] = _read(
                _arm(arm_us), fc_hz=FC_HZ,
            ).residual_us
        except AlignmentPrescriptionRefused as exc:
            admitted[arm_us] = exc.reason
    assert admitted == {
        0.0: PRESCRIPTION_OUT_OF_LOBE,
        -250.0: pytest.approx(155.7),
        -350.0: pytest.approx(55.7),
        -450.0: pytest.approx(-44.3),
        -550.0: pytest.approx(-144.3),
    }


def test_every_swept_corner_admits_the_same_four_arms():
    """The bound is validated once, at the commissioned corner — and on this
    candidate set that choice is not load-bearing.

    A FROZEN regression guard over the corners the banked r1b round actually
    swept, back when an alternative-Fc sweep re-scored the same prescription at
    each of them. No live path re-scores a prescription at a second corner — a
    round runs at one corner, and the sweep and the selector that ranked its
    results are both retired (``docs/tuning-master-plan.md`` tickets 2.3, 2.4).
    What the guard is still worth: the half-period lobe tightens with frequency,
    so a corner other than the commissioned one could in principle exclude a
    candidate the boundary admitted. It does not across this banked span — the
    tightest corner in it (1847.7 Hz, lobe 270.6 µs) still clears every
    candidate, whose worst residual is 155.7 µs. Pinned so a future candidate
    set that DOES straddle one of these corners fails here rather than
    surprising a bench session.
    """
    per_corner = {}
    for fc_hz in SWEPT_CORNERS_HZ:
        admitted = []
        for arm_us in ARMS_US:
            try:
                _read(_arm(arm_us), fc_hz=fc_hz)
            except AlignmentPrescriptionRefused:
                continue
            admitted.append(arm_us)
        per_corner[fc_hz] = tuple(admitted)
    assert set(per_corner.values()) == {(-250.0, -350.0, -450.0, -550.0)}


def test_the_bound_is_measured_from_the_basis_not_from_the_incumbent():
    """The design decision, pinned as behaviour.

    The series-2 incumbent (+96.0 µs applied) sits 501.7 µs from the measured
    basis — **1.65 half-period lobes**, 0.83 of a period at Fc. A bound anchored
    on it would refuse every candidate that could fix the misalignment, which is
    how a fail-closed guard becomes decorative. Anchored on the declared basis,
    the optimum candidate is admitted and the incumbent itself would not be.

    The ratio is asserted, not narrated: this docstring justified the design
    deviation with "more than a period and a half" for one review round, which
    was wrong by half a lobe — the conclusion survived, the arithmetic did not,
    and a number a test does not own is a number that can drift again.
    """
    incumbent_us = 96.0
    lobes = abs(incumbent_us - BASIS_US) / half_period_us(FC_HZ)
    assert lobes == pytest.approx(1.6543, abs=5e-4)
    assert abs(incumbent_us - BASIS_US) / (1e6 / FC_HZ) == pytest.approx(
        0.8272, abs=5e-4,
    )
    assert abs(incumbent_us - BASIS_US) > half_period_us(FC_HZ)
    assert _read(_arm(-450.0), fc_hz=FC_HZ) is not None
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(incumbent_us), fc_hz=FC_HZ)
    assert excinfo.value.reason == PRESCRIPTION_OUT_OF_LOBE


@pytest.mark.parametrize("fc_hz", (0.0, -1648.7, float("nan"), float("inf"), True, "x"))
def test_an_unusable_corner_is_its_own_refusal(fc_hz):
    """A corner the bound is undefined at never reads as an out-of-lobe candidate.

    Two different problems, and reporting the second would send an operator to
    re-derive a number that was fine.
    """
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(-450.0), fc_hz=fc_hz)
    assert excinfo.value.reason == PRESCRIPTION_FC_UNKNOWN


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
# 2. Provenance is required, and the shape is strict
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"basis_artifacts": []}, ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING),
        ({"basis_artifacts": None}, ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING),
        ({"basis_artifacts": ["  "]}, ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING),
        ({"basis_artifacts": [None]}, ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING),
        # A bare string would make "a,b" one artifact and ["a","b"] two,
        # decided by punctuation.
        (
            {"basis_artifacts": "one.json,two.json"},
            ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
        ),
        ({"basis_note": 7}, ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING),
    ],
)
def test_a_prescription_without_real_provenance_is_refused(mutation, reason):
    """The bound checks a prescription against a basis somebody NAMED.

    Without the name the bound is arithmetic, not provenance: a prescriber
    could declare any basis it liked and pass. What makes the basis
    trustworthy is that the receipt says where it came from and a human can go
    and read it.
    """
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(_arm(-450.0, **mutation), fc_hz=FC_HZ)
    assert excinfo.value.reason == reason


def test_a_prescription_missing_its_artifacts_key_entirely_is_refused():
    body = _arm(-450.0)
    del body["basis_artifacts"]
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(body, fc_hz=FC_HZ)
    assert excinfo.value.reason == ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"delay_us": "-450"}, PRESCRIPTION_DELAY_INVALID),
        ({"delay_us": True}, PRESCRIPTION_DELAY_INVALID),
        ({"delay_us": None}, PRESCRIPTION_DELAY_INVALID),
        ({"delay_us": float("nan")}, PRESCRIPTION_DELAY_INVALID),
        ({"delay_us": float("-inf")}, PRESCRIPTION_DELAY_INVALID),
        ({"basis_delay_us": "-405.7"}, PRESCRIPTION_BASIS_INVALID),
        ({"basis_delay_us": False}, PRESCRIPTION_BASIS_INVALID),
        ({"basis_delay_us": float("nan")}, PRESCRIPTION_BASIS_INVALID),
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
        (_arm(-450.0, basis_delay_us="x"), FC_HZ),
        (_arm(-450.0, basis_artifacts=[]), FC_HZ),
        (_arm(-450.0), 0.0),
        (_arm(0.0), FC_HZ),
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
        # Still the lobe: 0 µs never gets as far as the window question.
        0.0: PRESCRIPTION_OUT_OF_LOBE,
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


def test_the_hardware_window_is_answered_before_the_measurements_lobe():
    """Two refusals send an operator to two different places.

    A prescription the preset could never emit is refused for THAT — go
    re-declare the region — rather than for a lobe it also happens to miss,
    which would say go re-derive the basis. The ordering is what keeps the
    named reason actionable.
    """
    both_wrong = _arm(-900.0)
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(both_wrong, declared_bounds_us=HORN_WINDOW_US)
    assert excinfo.value.reason == PRESCRIPTION_OUTSIDE_DECLARED_WINDOW
    # …and with no declaration to answer, the same candidate falls through
    # to the measurement's own bound rather than passing.
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        _read(both_wrong, declared_bounds_us=None)
    assert excinfo.value.reason == PRESCRIPTION_OUT_OF_LOBE


def test_a_preset_that_declares_no_window_gates_on_the_lobe_alone():
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


def test_the_read_back_does_not_re_apply_the_bound():
    """One owner for the bound, and it is the request boundary.

    The only mappings that reach the read-back were written after the boundary
    accepted them, so re-applying the bound could not catch a prescription it
    let through — it could only refuse one whose corner moved between the stage
    that MEASURED a round and the stage that GRADES it, discarding the evidence
    of a round that really ran.
    """
    out_of_lobe = _arm(0.0)
    with pytest.raises(AlignmentPrescriptionRefused):
        _read(out_of_lobe, fc_hz=FC_HZ)
    recovered = alignment_prescription_from_mapping(out_of_lobe)
    assert recovered is not None
    assert recovered.delay_us == 0.0


def test_the_read_back_still_refuses_a_mangled_record(caplog):
    """A hand-edited state file must not become half a provenance."""
    with caplog.at_level(logging.WARNING):
        assert alignment_prescription_from_mapping(_arm(-450.0, basis_artifacts=[])) is None
    assert "alignment_prescription_unreadable" in caplog.text
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


def test_the_response_format_reaches_the_evidence_packet(tmp_path):
    """Wired beside topology's own block (#2773's shape), so a reader of the
    packet — not just of the module — learns this door exists."""
    from jasper.active_speaker.crossover_v2.evidence_packet import (
        build_crossover_evidence_packet,
    )

    from tests.test_crossover_v2_blend_prescription import _bundle

    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)
    doors = packet["request_time_prescriptions"]
    assert doors["alignment"] == alignment_prescription_response_format()
    assert doors["topology"]["key"] == "topology_prescription"


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
    assert (
        ap.ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING
        == "prescription_provenance_missing"
    )


# --------------------------------------------------------------------------- #
# 4. The aligner commits the prescription — exactly, and says so
# --------------------------------------------------------------------------- #


def _lr4_branches(fc_hz=FC_HZ, n_bins=4097, f_max_hz=24_000.0):
    """The same complementary LR4 pair the selector suite uses."""
    freqs = np.linspace(0.0, f_max_hz, n_bins)
    s = 1j * freqs / fc_hz
    butter2 = s * s + np.sqrt(2.0) * s + 1.0
    return freqs, (1.0 / butter2) ** 2, ((s * s) / butter2) ** 2


def _selector_kwargs(**overrides):
    kwargs = dict(
        fc_hz=FC_HZ, lo_hz=FC_HZ / 2.0, hi_hz=FC_HZ * 2.0,
        trim_w_db=0.0, trim_t_db=0.0,
        anchor_delay_us=0.0, seed_delay_us=0.0, seed_polarity_sign=1,
    )
    kwargs.update(overrides)
    return kwargs


def test_the_prescription_is_committed_exactly_not_snapped_to_a_better_neighbour():
    """The one property a delay sweep cannot give up.

    A matched LR4 pair at its own corner is flattest at zero delay, so the
    automatic objective commits ~0 µs. Handed a prescription it commits the
    prescribed number instead, to the bit — not the nearby grid point that
    scored better, and not the seed the flat-minimum epsilon would otherwise
    prefer. A candidate that silently measured the estimator's answer under
    the candidate's name is the failure this path exists to remove.
    """
    freqs, W, T = _lr4_branches()
    automatic = _select_alignment_pair(freqs, W, T, **_selector_kwargs())
    assert automatic.delay_us == pytest.approx(0.0, abs=1e-9)
    assert automatic.objective == ALIGNMENT_COMMITTED_FLAT_SUM

    prescribed = _select_alignment_pair(
        freqs, W, T, **_selector_kwargs(explicit_delay_us=-450.0),
    )
    assert prescribed.delay_us == -450.0
    assert prescribed.objective == ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    # One point on the delay axis, and the record says so rather than quoting
    # the step of a grid that was never walked.
    assert (prescribed.grid_points, prescribed.grid_step_us) == (1, 0.0)
    # The seed is still scored, so the record shows what the prescription
    # displaced — and the objective honestly reports it lost flatness.
    assert prescribed.seed_ripple_db == pytest.approx(automatic.seed_ripple_db)
    assert prescribed.flatness_improvement_db < 0.0


def test_an_unpinned_prescription_leaves_the_polarity_to_the_objective():
    """Separation of concerns, in one assertion.

    A prescription that states only a delay is about the DELAY. The polarity
    axis is still searched at that delay, so a seed sign that would command a
    null is still overridden, and ``polarity_agrees_with_sum`` reports a
    comparison that really happened rather than ``None``.

    Scoped to UNPINNED since the basin pin: with one, the axis holds and the
    section below grades that instead. The name said "the objective that owns
    it" unqualified, which stopped being the whole story.
    """
    freqs, W, T = _lr4_branches()
    selection = _select_alignment_pair(
        freqs, W, T,
        **_selector_kwargs(seed_polarity_sign=-1, explicit_delay_us=-450.0),
    )
    assert selection.delay_us == -450.0
    assert selection.polarity_sign == 1
    assert selection.polarity_agrees_with_sum is False


@pytest.mark.parametrize("pinned_sign", (1, -1))
def test_a_pinned_basin_is_the_one_the_fit_solves_in(pinned_sign):
    """The basin bug, in both directions, at the SOLVE.

    A matched LR4 pair at ``-450 µs`` scores flattest in ``+1``, so the unpinned
    prescription commits ``+1`` whatever the seed said — that is the fit
    re-rolling the basin. Pinned, the axis holds the requested sign, and the
    delay is still exactly the prescribed number: the pin narrows the search, it
    does not replace it.

    Mutation control: restore ``signs = (1, -1)`` in ``_select_alignment_pair``
    and the ``-1`` case commits ``+1``, failing here.
    """
    freqs, W, T = _lr4_branches()
    unpinned = _select_alignment_pair(
        freqs, W, T, **_selector_kwargs(explicit_delay_us=-450.0),
    )
    assert unpinned.polarity_sign == 1

    pinned = _select_alignment_pair(
        freqs, W, T,
        **_selector_kwargs(
            explicit_delay_us=-450.0, explicit_polarity_sign=pinned_sign,
        ),
    )
    assert pinned.polarity_sign == pinned_sign
    assert pinned.delay_us == -450.0
    # Still a prescription commitment, so the receipt's ``committed`` bit keeps
    # meaning what it means.
    assert pinned.objective == ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    assert pinned.objective in ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES


def test_a_pin_beats_the_seed_the_flat_minimum_would_otherwise_prefer():
    """The pin is a CONSTRAINT, not a tie-break nudge.

    The flat-minimum epsilon resolves a shallow basin toward the seed's own
    polarity. Pinned against the seed, that preference has nothing to reach
    for — the losing sign is not a candidate at all, so a shallow basin cannot
    quietly return it.
    """
    freqs, W, T = _lr4_branches()
    pinned = _select_alignment_pair(
        freqs, W, T,
        **_selector_kwargs(
            seed_polarity_sign=1, explicit_delay_us=-450.0,
            explicit_polarity_sign=-1,
        ),
    )
    assert pinned.polarity_sign == -1
    assert pinned.seed_polarity_sign == 1


@pytest.mark.parametrize("pinned_sign", (1, -1))
def test_a_pinned_round_reports_no_agreement_because_nothing_was_compared(
    pinned_sign,
):
    """The honesty half, and the reason ``polarity_pinned`` is recorded at all.

    ``polarity_agrees_with_sum`` answers "did correlation's polarity survive the
    flat-sum objective". Under a pin the axis held one value and no comparison
    ran, so the honest answer is ``None`` — the same answer the low-SNR path
    already gives. Without this a pin that happened to match the seed would
    publish ``True``: correlation "confirmed" by a search that never ran.
    """
    freqs, W, T = _lr4_branches()
    pinned = _select_alignment_pair(
        freqs, W, T,
        **_selector_kwargs(
            seed_polarity_sign=pinned_sign,
            explicit_delay_us=-450.0, explicit_polarity_sign=pinned_sign,
        ),
    )
    assert pinned.polarity_pinned is True
    assert pinned.polarity_agrees_with_sum is None


def test_a_pinned_basin_holds_through_the_low_snr_refusal_too():
    """That refusal costs the capture's own answers, and a pin is not one.

    An UNPINNED prescription on a refused capture commits declared ``+1``,
    because polarity is the question the capture was refused for. A pin did not
    come from this capture any more than the delay did, so the refusal has
    nothing to say about it — and a round that silently measured ``+1`` under
    an inverted candidate's name is exactly the confound this field removes.
    """
    freqs, W, T = _lr4_branches()
    low_snr = dict(
        branch_snr_insufficient=True,
        applied_alignment=AppliedAlignment(delay_us=96.0),
        explicit_delay_us=-450.0,
    )
    unpinned = _select_alignment_pair(freqs, W, T, **_selector_kwargs(**low_snr))
    assert unpinned.polarity_sign == 1
    assert unpinned.polarity_pinned is False

    pinned = _select_alignment_pair(
        freqs, W, T, **_selector_kwargs(**low_snr, explicit_polarity_sign=-1),
    )
    assert pinned.polarity_sign == -1
    assert pinned.delay_us == -450.0
    assert pinned.objective == ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR
    # The declared-polarity set governs the anchor withdrawal, and the capture
    # is still refused — so membership is unchanged by the pin.
    assert pinned.objective in ALIGNMENT_DECLARED_POLARITY_OBJECTIVES
    assert pinned.polarity_agrees_with_sum is None


def test_a_delay_only_prescription_selects_exactly_what_it_selected_before():
    """The regression pin for tonight's shipped callers (the armrun shape).

    Every prescription in the field today states a delay and no polarity. Its
    selection must be unchanged in every field — the polarity still searched,
    the agreement still answered, the delay still exact — so adding the pin
    cannot have moved a round nobody asked to move.
    """
    freqs, W, T = _lr4_branches()
    kwargs = _selector_kwargs(seed_polarity_sign=-1, explicit_delay_us=-450.0)

    before = _select_alignment_pair(freqs, W, T, **kwargs)
    explicitly_unpinned = _select_alignment_pair(
        freqs, W, T, **kwargs, explicit_polarity_sign=None,
    )

    assert before == explicitly_unpinned
    assert before.polarity_pinned is False
    # The two properties a pin would have changed, still answering as they did.
    assert before.polarity_sign == 1
    assert before.polarity_agrees_with_sum is False


def test_the_prescription_outranks_the_low_snr_ladder():
    """That ladder answers "what do we commit when nothing better is known".

    A bench-measured prescription IS something better, and it did not come from
    the capture the SNR verdict refused. What the refusal still costs is the
    POLARITY, which is this capture's question — so the commitment joins the
    declared-polarity set, and with it the anchor withdrawal that set governs.
    """
    freqs, W, T = _lr4_branches()
    held = _select_alignment_pair(
        freqs, W, T,
        **_selector_kwargs(
            branch_snr_insufficient=True,
            applied_alignment=AppliedAlignment(delay_us=96.0),
        ),
    )
    assert held.delay_us == 96.0
    assert held.objective == ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR

    prescribed = _select_alignment_pair(
        freqs, W, T,
        **_selector_kwargs(
            branch_snr_insufficient=True,
            applied_alignment=AppliedAlignment(delay_us=96.0),
            explicit_delay_us=-450.0,
        ),
    )
    assert prescribed.delay_us == -450.0
    assert prescribed.objective == ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR
    assert prescribed.polarity_sign == 1
    assert prescribed.polarity_agrees_with_sum is None


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
    """The BEHAVIOURAL guard on
    :data:`ALIGNMENT_DECLARED_POLARITY_OBJECTIVES` membership, and the
    disclosure of what that membership costs.

    That set has two jobs, and only one of them had a test: the household
    wording (mirrored in the browser module, which the JS test pins) and the
    NUMERIC one — ``_build_candidate`` withdraws the refused capture's anchor
    from the summed model, so ``summed_model_residual_delay_us`` returns 0.0
    and the shipped prediction stays in the independently-aligned frame.
    Dropping the prescription's low-SNR objective from that set turns the
    withdrawal off silently; nothing but this test notices.

    **And the consequence, stated rather than implied.** On this candidate the
    predicted curve is bit-identical no matter which delay was prescribed. That
    is CORRECT — the capture supplied no trustworthy frame, and phasing a model
    the accountability gate can refuse on by an untrusted number is the #2617
    hazard — but it means the candidate has no pre-apply discriminating net:
    two candidates predict the same thing, so nothing before the speaker plays
    can tell them apart. It is also model-only-adoption-proof, in the strongest
    possible sense: on this candidate the model cannot express a preference,
    so an adoption can only ever rest on measurement.
    """
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


def _published_agreement(*, seed_sign: int, prescribed: float | None):
    """The ``polarity_agrees_with_sum`` a finished candidate PUBLISHES.

    ``_analyze_measure`` copies this field onto the estimate every durable
    surface then reads (the receipt's ``analysis_json``, the household review
    row, the journal), so this is the published value one hop from publication
    — and the copy itself is a single pass-through line, mutation-pinned.
    """
    woofer_ir = np.zeros(8192)
    tweeter_ir = np.zeros(8192)
    woofer_ir[1000] = 1.0
    tweeter_ir[1011] = 1.0
    alignment = AlignmentEstimate(
        delay_us=-650.0, raw_delay_us=-650.0, parallax_us=0.0,
        polarity="normal" if seed_sign > 0 else "inverted",
        polarity_sign=seed_sign, confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=-3 / 48_000 * 1e6, snapped_delay_us=None,
    )
    candidate, _predicted = _build_candidate(
        woofer_ir, tweeter_ir, 48_000, 16_384, FC_HZ, "woofer", "tweeter",
        alignment, None,
        alignment_delay_bounds_us=(0.0, 1100.0),
        explicit_alignment_delay_us=prescribed,
    )
    return candidate


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
    assert prescribed.alignment.polarity_agrees_with_sum is not None
    # …and it is the SAME object the selection produced.
    assert (
        prescribed.alignment.polarity_agrees_with_sum
        is prescribed.candidate.polarity_agrees_with_sum
    )


def test_the_published_polarity_agreement_is_the_one_the_objective_answered():
    """ONE owner for the cross-check, across all three candidates.

    The rule — only a commitment whose POLARITY the flat sum actually chose may
    answer this — lives on
    :attr:`AlignmentPairSelection.polarity_agrees_with_sum`. It was ALSO
    re-derived at the publish site against a single objective, and #2662
    widening the rule split the two: a prescribed round that overrode
    correlation and FLIPPED the polarity published "never asked" while the
    journal, one function away, said the comparison ran and disagreed. Three
    durable surfaces read the published value, so the disagreement was not
    cosmetic.

    The third candidate is the one that matters: it must publish ``False`` — a
    comparison that ran and disagreed — never ``None``.
    """
    automatic = _published_agreement(seed_sign=1, prescribed=None)
    assert automatic.alignment_objective == ALIGNMENT_COMMITTED_FLAT_SUM
    assert automatic.polarity_agrees_with_sum is True

    agreeing = _published_agreement(seed_sign=1, prescribed=-450.0)
    assert agreeing.alignment_objective == ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    assert agreeing.polarity_agrees_with_sum is True

    overriding = _published_agreement(seed_sign=-1, prescribed=-450.0)
    assert overriding.alignment_objective == ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    assert overriding.polarity == "normal"          # correlation said inverted
    assert overriding.polarity_agrees_with_sum is False


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
        _pa, "solve_ripple_optimal_trim",
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

    assert analysis.alignment.polarity_agrees_with_sum is not None


def test_a_prescription_that_reaches_no_commitment_says_so_at_warning(caplog):
    """The disclosure surface, pinned with its fields.

    A candidate that silently measures the trims-only or seed fallback under
    the candidate's name is the failure this whole path exists to remove, and
    the WARNING is what a bench operator has between the round and the
    receipt. Its FIELDS
    are asserted, not just its presence: a line that fires without saying which
    delay was prescribed or what was committed instead cannot be acted on.
    """
    woofer_ir = np.zeros(8192)
    tweeter_ir = np.zeros(8192)
    woofer_ir[1000] = 1.0
    tweeter_ir[1011] = 1.0
    alignment = AlignmentEstimate(
        delay_us=96.0, raw_delay_us=96.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=None, snapped_delay_us=None,
    )
    with caplog.at_level(logging.WARNING):
        _build_candidate(
            woofer_ir, tweeter_ir, 48_000, 16_384, 2000.0, "woofer", "tweeter",
            alignment, None,
            tweeter_sweep_lo_hz=2000.0, woofer_sweep_hi_hz=2000.0,
            explicit_alignment_delay_us=-450.0,
        )
    assert "program_analysis.alignment_prescription_not_committed" in caplog.text
    assert "prescribed_delay_us=-450.0" in caplog.text
    # What was committed INSTEAD — the half an operator has to act on, and the
    # half that reads `null` if the line is emitted before the objective
    # resolves (it was, for one review round).
    assert "committed_delay_us=96.0" in caplog.text
    assert f"objective={ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND}" in caplog.text

    # …and the control: a COMMITTED prescription says nothing, so the line
    # cannot become background noise an operator learns to ignore.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _low_snr_candidate_at(-450.0)
    assert "alignment_prescription_not_committed" not in caplog.text


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
        return caplog.text

    prescribed_text = _emit(-450.0)
    assert "program_analysis.alignment_selection" in prescribed_text
    assert "prescribed_delay_us=-450.0" in prescribed_text

    automatic_text = _emit(None)
    assert "program_analysis.alignment_selection" in automatic_text
    # logfmt renders an absent value as `null`, not `None`.
    assert "prescribed_delay_us=null" in automatic_text


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
        return caplog.text

    assert "prescribed_polarity=null" in _emit(None, None)
    assert "prescribed_polarity=unpinned" in _emit(-450.0, None)

    inverted = _emit(-450.0, -1)
    assert "prescribed_polarity=inverted" in inverted
    # The pin decided the commitment, and the line says so: the agreement is
    # honestly absent rather than claiming a comparison.
    assert "polarity=inverted" in inverted
    assert "polarity_agrees_with_sum=null" in inverted

    assert "prescribed_polarity=normal" in _emit(-450.0, 1)


def test_both_prescription_objectives_are_registered_in_the_vocabulary():
    """A commitment nothing can name is a commitment a forensic reader loses."""
    assert ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES <= ALIGNMENT_COMMITMENTS
    # The trusted-capture candidate keeps its residual (its anchor IS
    # trustworthy); the refused-capture candidate gives it up with the rest
    # of that refusal.
    assert ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION not in (
        ALIGNMENT_DECLARED_POLARITY_OBJECTIVES
    )
    assert ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR in (
        ALIGNMENT_DECLARED_POLARITY_OBJECTIVES
    )
    # Substring-clean in both directions: a consumer matching an objective by
    # substring must not read the two as one commitment.
    assert ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION not in (
        ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR
    )
    assert ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR not in (
        ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    )


# --------------------------------------------------------------------------- #
# 5. The control: no prescription changes nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("snr_insufficient", (False, True))
@pytest.mark.parametrize("seed_polarity_sign", (1, -1))
def test_a_session_with_no_prescription_selects_exactly_what_it_selected_before(
    snr_insufficient, seed_polarity_sign,
):
    """The automatic path is untouched, asserted rather than assumed.

    ``explicit_delay_us=None`` must produce a byte-identical selection to
    omitting the keyword entirely — the positive control for every claim above,
    and the reason PR #1649's snap radius needed no widening.
    """
    freqs, W, T = _lr4_branches()
    kwargs = _selector_kwargs(
        seed_polarity_sign=seed_polarity_sign,
        seed_delay_us=40.0,
        anchor_delay_us=40.0,
        branch_snr_insufficient=snr_insufficient,
        applied_alignment=AppliedAlignment(delay_us=96.0),
    )
    assert (
        _select_alignment_pair(freqs, W, T, **kwargs)
        == _select_alignment_pair(freqs, W, T, explicit_delay_us=None, **kwargs)
    )


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
        tier="express",
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
        # The reachable rail, and the reason this field exists at all.
        (ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND, False),
        (ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR, False),
        (ALIGNMENT_COMMITTED_FLAT_SUM, False),
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


def test_the_uncommitted_rail_is_reachable_and_the_receipt_reports_it():
    """The rail END TO END, not asserted from a hand-picked objective string.

    A real capture, a real prescription, a band with no scorable bin — the
    machinery commits the SEED, and the receipt built from that round's
    objective says the candidate did not run.
    """
    woofer_ir = np.zeros(8192)
    tweeter_ir = np.zeros(8192)
    woofer_ir[1000] = 1.0
    tweeter_ir[1011] = 1.0
    seed_us = 96.0
    alignment = AlignmentEstimate(
        delay_us=seed_us, raw_delay_us=seed_us, parallax_us=0.0,
        polarity="normal", polarity_sign=1, confidence=0.9, status=ALIGNMENT_OK,
        anchor_delay_us=None, snapped_delay_us=None,
    )
    candidate, _predicted = _build_candidate(
        # The two branches' declared sweeps MEET at Fc instead of overlapping,
        # so the scoring band holds no bin, `_select_alignment_pair` returns
        # None, and the seed stands — with the estimate still ALIGNMENT_OK.
        woofer_ir, tweeter_ir, 48_000, 16_384, 2000.0, "woofer", "tweeter",
        alignment, None,
        tweeter_sweep_lo_hz=2000.0, woofer_sweep_hi_hz=2000.0,
        explicit_alignment_delay_us=-450.0,
    )
    assert candidate.alignment_objective == ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND
    assert candidate.delay_us == pytest.approx(seed_us)

    prescription = _read(_arm(-450.0), fc_hz=FC_HZ)
    banked = _round_measurements_for(
        prescription, objective=candidate.alignment_objective,
    )["alignment_prescription"]
    assert banked["delay_us"] == -450.0
    assert banked["committed"] is False


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
            rollback=None,
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
