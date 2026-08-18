# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2662: the explicit, bounded, provenance-carrying delay prescription.

Covers the request gate (shape, provenance, and the one derivation of the
bound), the aligner's commitment of a prescribed delay, the single-owner claim
end to end into the emitted graph and its proof, the round receipt's
provenance, and — the control that matters most — that a session with no
prescription selects exactly what it selected before.

The numbers in the arm-set tests are the series-2 diagnosis's, deliberately:
the harness exists to measure those arms, and a test written against
placeholder values would not have caught that the ``0 µs`` control arm cannot
be expressed as a prescription at all.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_alignment import POLARITY_KEEP
from jasper.active_speaker.crossover_v2 import coordinator
from jasper.active_speaker.crossover_v2.alignment_prescription import (
    ALIGNMENT_PRESCRIPTION_KEY,
    PRESCRIPTION_BASIS_INVALID,
    PRESCRIPTION_DELAY_INVALID,
    PRESCRIPTION_FC_UNKNOWN,
    PRESCRIPTION_MALFORMED,
    PRESCRIPTION_OUT_OF_LOBE,
    PRESCRIPTION_PROVENANCE_MISSING,
    PRESCRIPTION_REFUSAL_REASONS,
    AlignmentPrescription,
    AlignmentPrescriptionRefused,
    alignment_prescription_from_mapping,
    read_alignment_prescription,
)
from jasper.active_speaker.crossover_v2.planning import alignment_to_candidate_fields
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    MeasuredCrossoverCandidateError,
    build_and_prove_candidate_config,
    compile_candidate_config,
    prove_candidate_config,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_COMMITMENTS,
    ALIGNMENT_DECLARED_POLARITY_OBJECTIVES,
    ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES,
    ALIGNMENT_OK,
    AlignmentEstimate,
    AppliedAlignment,
    MeasurementPriors,
    _select_alignment_pair,
    half_period_us,
)

from tests.test_active_speaker_profile import _two_way_preset

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
#: The diagnosis's corrected arm set (§3), including the ``0`` control.
ARMS_US = (0.0, -250.0, -350.0, -450.0, -550.0)
#: Every corner the banked r1b ``fc_selection`` actually swept.
SWEPT_CORNERS_HZ = (1600.0, 1648.7, 1658.6, 1719.4, 1782.4, 1847.7)
ARTIFACTS = (
    "captures/xover-series2-2026-08-17/diagnosis/landscape_delay_polarity_r1b.json",
    "captures/xover-series2-2026-08-17/diagnosis/item5_phase_share.json",
)


def _arm(arm_us: float, **overrides: object) -> dict:
    """One arm as the session POST body carries it.

    The positional parameter is deliberately NOT called ``delay_us``: several
    tests below override that very key, and a same-named parameter would make
    those calls a ``TypeError`` instead of a mutation.
    """
    body = {
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
    widest = read_alignment_prescription(
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
    depending on which side of the basis the arm sits.
    """
    lobe_us = half_period_us(FC_HZ)
    at_bound = BASIS_US + direction * lobe_us
    assert abs(at_bound - BASIS_US) <= lobe_us, "the at-bound arm is really at it"
    assert read_alignment_prescription(_arm(at_bound), fc_hz=FC_HZ) is not None

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
    assert abs(past_delay - BASIS_US) > lobe_us, "the past-bound arm is really past it"
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        read_alignment_prescription(_arm(past_delay), fc_hz=FC_HZ)
    assert excinfo.value.reason == PRESCRIPTION_OUT_OF_LOBE


def test_the_corrected_arm_set_clears_the_bound_and_the_control_arm_does_not():
    """Print-what-you-assert, on the arms this harness was built to run.

    Four of the diagnosis's five arms are admissible; the ``0 µs`` control is
    not, because zero delay leaves the drivers 405.7 µs apart — 241° of phase
    error at Fc, well outside the lobe. That is the correct verdict rather than
    a gap: refusing to bless a known-misaligned state as measurement-backed is
    what the gate is for, and "no prescription" is how a control arm is run.
    """
    admitted = {}
    for arm_us in ARMS_US:
        try:
            admitted[arm_us] = read_alignment_prescription(
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
    arm set that choice is not load-bearing.

    An alternative-Fc sweep re-scores the same prescription at other corners,
    where a tighter half-period could in principle exclude an arm the boundary
    admitted. It does not here: the tightest corner the r1b sweep evaluated
    (1847.7 Hz, lobe 270.6 µs) still clears every arm, whose worst residual is
    155.7 µs. Pinned so a future arm set that DOES straddle a swept corner
    fails this test rather than surprising a bench session.
    """
    per_corner = {}
    for fc_hz in SWEPT_CORNERS_HZ:
        admitted = []
        for arm_us in ARMS_US:
            try:
                read_alignment_prescription(_arm(arm_us), fc_hz=fc_hz)
            except AlignmentPrescriptionRefused:
                continue
            admitted.append(arm_us)
        per_corner[fc_hz] = tuple(admitted)
    assert set(per_corner.values()) == {(-250.0, -350.0, -450.0, -550.0)}


def test_the_bound_is_measured_from_the_basis_not_from_the_incumbent():
    """The design decision, pinned as behaviour.

    The series-2 incumbent (+96.0 µs applied) sits 501.7 µs from the measured
    basis — more than a period and a half at Fc. A bound anchored on it would
    refuse every arm that could fix the misalignment, which is how a
    fail-closed guard becomes decorative. Anchored on the declared basis, the
    optimum arm is admitted and the incumbent itself would not be.
    """
    incumbent_us = 96.0
    assert abs(incumbent_us - BASIS_US) > half_period_us(FC_HZ)
    assert read_alignment_prescription(_arm(-450.0), fc_hz=FC_HZ) is not None
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        read_alignment_prescription(_arm(incumbent_us), fc_hz=FC_HZ)
    assert excinfo.value.reason == PRESCRIPTION_OUT_OF_LOBE


@pytest.mark.parametrize("fc_hz", (0.0, -1648.7, float("nan"), float("inf"), True, "x"))
def test_an_unusable_corner_is_its_own_refusal(fc_hz):
    """A corner the bound is undefined at never reads as an out-of-lobe arm.

    Two different problems, and reporting the second would send an operator to
    re-derive a number that was fine.
    """
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        read_alignment_prescription(_arm(-450.0), fc_hz=fc_hz)
    assert excinfo.value.reason == PRESCRIPTION_FC_UNKNOWN


# --------------------------------------------------------------------------- #
# 2. Provenance is required, and the shape is strict
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"basis_artifacts": []}, PRESCRIPTION_PROVENANCE_MISSING),
        ({"basis_artifacts": None}, PRESCRIPTION_PROVENANCE_MISSING),
        ({"basis_artifacts": ["  "]}, PRESCRIPTION_PROVENANCE_MISSING),
        ({"basis_artifacts": [None]}, PRESCRIPTION_PROVENANCE_MISSING),
        # A bare string would make "a,b" one artifact and ["a","b"] two,
        # decided by punctuation.
        ({"basis_artifacts": "one.json,two.json"}, PRESCRIPTION_PROVENANCE_MISSING),
        ({"basis_note": 7}, PRESCRIPTION_PROVENANCE_MISSING),
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
        read_alignment_prescription(_arm(-450.0, **mutation), fc_hz=FC_HZ)
    assert excinfo.value.reason == reason


def test_a_prescription_missing_its_artifacts_key_entirely_is_refused():
    body = _arm(-450.0)
    del body["basis_artifacts"]
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        read_alignment_prescription(body, fc_hz=FC_HZ)
    assert excinfo.value.reason == PRESCRIPTION_PROVENANCE_MISSING


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
        ({"basis_artifact": ["x"]}, PRESCRIPTION_MALFORMED),
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
        read_alignment_prescription(_arm(-450.0, **mutation), fc_hz=FC_HZ)
    assert excinfo.value.reason == reason


@pytest.mark.parametrize("raw", (["not", "a", "mapping"], "text", 7))
def test_a_non_mapping_prescription_is_refused(raw):
    with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
        read_alignment_prescription(raw, fc_hz=FC_HZ)
    assert excinfo.value.reason == PRESCRIPTION_MALFORMED


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
    ):
        with pytest.raises(AlignmentPrescriptionRefused) as excinfo:
            read_alignment_prescription(body, fc_hz=fc_hz)
        raised.add(excinfo.value.reason)
    assert raised <= PRESCRIPTION_REFUSAL_REASONS
    assert raised == PRESCRIPTION_REFUSAL_REASONS


def test_no_prescription_reads_as_the_automatic_path():
    """Absence is not a refusal — it is every ordinary round."""
    assert read_alignment_prescription(None, fc_hz=FC_HZ) is None
    assert read_alignment_prescription({}.get(ALIGNMENT_PRESCRIPTION_KEY),
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
        read_alignment_prescription(out_of_lobe, fc_hz=FC_HZ)
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
    prescription = read_alignment_prescription(_arm(-450.0), fc_hz=FC_HZ)
    record = prescription.to_dict()
    assert record["residual_us"] == pytest.approx(-44.3)
    assert record["basis_artifacts"] == list(ARTIFACTS)
    assert alignment_prescription_from_mapping(
        {k: v for k, v in record.items() if k != "residual_us"}
    ) == prescription


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
    prefer. An arm that silently measured the estimator's answer under the
    arm's name is the failure this path exists to remove.
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


def test_the_prescription_leaves_the_polarity_to_the_objective_that_owns_it():
    """Separation of concerns, in one assertion.

    A prescription is about the DELAY. The polarity axis is still searched at
    that delay, so a seed sign that would command a null is still overridden,
    and ``polarity_agrees_with_sum`` reports a comparison that really happened
    rather than ``None``.
    """
    freqs, W, T = _lr4_branches()
    selection = _select_alignment_pair(
        freqs, W, T,
        **_selector_kwargs(seed_polarity_sign=-1, explicit_delay_us=-450.0),
    )
    assert selection.delay_us == -450.0
    assert selection.polarity_sign == 1
    assert selection.polarity_agrees_with_sum is False


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


def test_both_prescription_objectives_are_registered_in_the_vocabulary():
    """A commitment nothing can name is a commitment a forensic reader loses."""
    assert ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES <= ALIGNMENT_COMMITMENTS
    # The trusted-capture arm keeps its residual (its anchor IS trustworthy);
    # the refused-capture arm gives it up with the rest of that refusal.
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
        analysis, woofer_role="woofer", tweeter_role="tweeter",
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
    # Exactly one non-zero delay in the emitted graph, and it is the arm.
    delays = [
        float(line.split(":", 1)[1])
        for line in yaml_text.splitlines()
        if line.strip().startswith("delay:")
    ]
    assert sorted(d for d in delays if d) == [pytest.approx(0.45)]


def test_moving_the_prescription_moves_that_field_and_nothing_else():
    """Mutation-pinned one-owner: two arms, one changed line.

    Two same-sign arms differ only in magnitude, so if any SECOND place in the
    emitted graph carried the delay this diff would be wider than one line.
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
    """No bypass: the arm goes through the same compile-and-prove every
    automatic candidate does, including the headroom recompute and the static
    delay binding proof.

    Mutation-pinned in the direction that matters — tamper the emitted delay
    and the proof refuses by its own code, so an arm whose graph does not
    carry the prescribed number cannot reach a speaker.
    """
    candidate = _candidate_for(-450.0)
    yaml_text = build_and_prove_candidate_config(
        candidate, playback_device="hw:ActiveDAC",
    )
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


def _evidence_for(prescription):
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
    )


def _evaluation_stub():
    """The two attributes ``_round_measurements`` reads, and nothing else."""
    return type("_E", (), {"blend": None, "region_benefit": None})()


def _round_measurements_for(prescription):
    return coordinator._round_measurements(
        _evidence_for(prescription), _evaluation_stub(),
    )


def test_an_adopted_arms_receipt_names_what_its_timing_rests_on():
    """The adoption record has to carry the provenance, not just the number."""
    prescription = read_alignment_prescription(_arm(-450.0), fc_hz=FC_HZ)
    banked = _round_measurements_for(prescription)["alignment_prescription"]
    assert banked["delay_us"] == -450.0
    assert banked["basis_delay_us"] == BASIS_US
    assert banked["residual_us"] == pytest.approx(-44.3)
    assert banked["basis_artifacts"] == list(ARTIFACTS)
    assert "n=33" in banked["basis_note"]


def test_an_ordinary_round_banks_no_prescription_block():
    """Absence and presence each mean exactly one thing on a receipt."""
    assert "alignment_prescription" not in _round_measurements_for(None)


def test_the_prescription_is_not_an_instruction_the_next_round_inherits():
    """It rides with the MEASUREMENTS, never in the round identity.

    ``_round_identity`` is the instruction channel the next round reads back as
    its incumbent. A prescription there is how an arm gets re-run without being
    asked for — each arm of a delay sweep is prescribed explicitly.
    """
    prescription = read_alignment_prescription(_arm(-450.0), fc_hz=FC_HZ)
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
    # all, so no arm can be re-run without being asked for. Asserted over the
    # built mapping, not over a docstring.
    assert "alignment_prescription" not in repr(decision.receipt_identity)


def test_the_record_is_frozen():
    """A prescription cannot be edited after the gate accepted it."""
    prescription = read_alignment_prescription(_arm(-450.0), fc_hz=FC_HZ)
    assert isinstance(prescription, AlignmentPrescription)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prescription.delay_us = 0.0  # type: ignore[misc]
