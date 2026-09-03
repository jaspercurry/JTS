# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The S3 attempts loop's decision kernel: every stop reason, and the rules.

The three constraints these tests pin come from the 2026-07-31 fixed-mic
repeat study (``captures/repeat-floor-20260731/README.md``). Each is pinned as
a *behaviour*, not as a transcribed decimal: the claim floor is asserted to be
the computation, the consecutive-only rule is asserted by showing the kernel
has no way to express a fixed baseline, and the repeat cap is asserted through
the clamp the live flow will call.
"""

from __future__ import annotations

import inspect

import pytest

from jasper.active_speaker.attempts_loop import (
    CLAIM_FLOOR_P95_MULTIPLE,
    CONTINUE,
    DECISIONS,
    FLOOR_BASIS_MEASURED,
    FLOOR_BASIS_POLICY,
    FLOOR_SCOPE_ACROSS_SITTINGS,
    FLOOR_SCOPE_WITHIN_SITTING,
    FLOOR_SCOPES,
    PROVENANCE_MODEL_GRADED,
    PROVENANCE_REALIZED,
    REASON_ATTEMPT_NOT_COMPARABLE,
    REASON_AWAITING_FIRST_ATTEMPT,
    REASON_BASELINE_ESTABLISHED,
    REASON_BELOW_CLAIM_FLOOR,
    REASON_BUDGET_EXHAUSTED,
    REASON_DIRECTION_UNKNOWN_ABOVE_FLOOR,
    REASON_FLOOR_METRIC_MISMATCH,
    REASON_GRADED_BINS_SHRANK,
    REASON_IMPROVEMENT_ABOVE_FLOOR,
    REASON_IN_SPEC,
    REASON_NO_MATERIAL_IMPROVEMENT_PREDICTED,
    REASON_PREDECESSOR_NOT_COMPARABLE,
    REASON_PROVENANCE_MISMATCH,
    REASON_REGRESSION_FROM_PREDECESSOR,
    REASON_SITTING_MISMATCH,
    REASON_SITTING_UNRECORDED,
    STOP_BUDGET,
    STOP_CONVERGED,
    STOP_EVIDENCE,
    STOP_FLOOR,
    AttemptBudget,
    AttemptIntegrity,
    AttemptRecord,
    FloorStats,
    decide_next,
    percentile,
    material_improvement_db,
)

METRIC = "max_db_notch_excluded"

# The 13 accepted consecutive-pair deviations from
# captures/repeat-floor-20260731/analysis/refined_A_product_level.json,
# ``shipped_comparator_accepted_pairs.vs_predecessor.rows``. Present so the
# floor arithmetic is checked against the real study rather than a round
# number invented for a test.
BANKED_CONSECUTIVE_PAIRS_DB = (
    0.085368, 0.053913, 0.067807, 0.043458, 0.049502, 0.084889, 0.059320,
    0.039868, 0.070366, 0.047792, 0.043053, 0.035829, 0.051826,
)
BANKED_P95_DB = 0.08508
BANKED_MEDIAN_DB = 0.05183


#: Every attempt in this file is measured in one sitting unless a test is
#: about sittings. That default matches the SHIPPED floor, which is
#: within-sitting (#2081) — so the rest of the suite exercises the production
#: scope rather than a permissive one no speaker has adopted, and the sitting
#: rule's own tests below are the only place the value varies.
SITTING = "sitting-1"


def test_percentile_reproduces_the_banked_studys_own_summary():
    """The claim floor's inputs must be derivable, not transcribed.

    The banked analysis published median 0.05183 and p95 0.08508 for these
    thirteen pairs; the floor it derives is twice that p95. A percentile that
    disagreed would silently move every floor this module computes.
    """
    assert percentile(BANKED_CONSECUTIVE_PAIRS_DB, 50.0) == pytest.approx(
        BANKED_MEDIAN_DB, abs=5e-6,
    )
    assert percentile(BANKED_CONSECUTIVE_PAIRS_DB, 95.0) == pytest.approx(
        BANKED_P95_DB, abs=5e-6,
    )
    assert CLAIM_FLOOR_P95_MULTIPLE * percentile(
        BANKED_CONSECUTIVE_PAIRS_DB, 95.0
    ) == pytest.approx(0.17016, abs=1e-5)


def test_percentile_edges():
    assert percentile([4.0], 95.0) == 4.0
    assert percentile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0], 100.0) == 3.0
    assert percentile([1.0, 2.0, 3.0], 50.0) == 2.0
    with pytest.raises(ValueError):
        percentile([], 50.0)



def _floor(
    claim_floor_db: float = 0.17016,
    metric: str = METRIC,
    *,
    scope: str = FLOOR_SCOPE_WITHIN_SITTING,
) -> FloorStats:
    return FloorStats.from_policy_bar(
        metric=metric,
        claim_floor_db=claim_floor_db,
        source="unit test",
        scope=scope,
    )


def _attempt(
    attempt_id: str,
    *,
    metric: str = METRIC,
    comparable: bool = True,
    reasons: tuple[str, ...] = (),
    provenance: str = PROVENANCE_REALIZED,
    sitting_id: str = SITTING,
    **kwargs: object,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=attempt_id,
        metric=metric,
        provenance=provenance,
        sitting_id=sitting_id,
        integrity=AttemptIntegrity(comparable=comparable, reasons=reasons),
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Constraint 3 — the claim floor is computed, not transcribed
# --------------------------------------------------------------------------


def test_claim_floor_is_twice_the_measured_p95_not_a_transcribed_decimal():
    floor = FloorStats.from_repeat_study(
        metric=METRIC,
        median_db=BANKED_MEDIAN_DB,
        p95_db=BANKED_P95_DB,
        source="captures/repeat-floor-20260731",
        measured_at="2026-07-31",
    )
    assert floor.claim_floor_db == pytest.approx(
        CLAIM_FLOOR_P95_MULTIPLE * BANKED_P95_DB,
    )
    # The number the rule actually yields on the banked study.
    assert floor.claim_floor_db == pytest.approx(0.17016, abs=1e-9)
    # And it is NOT the README's household-facing 0.2, which is this same
    # rule rounded up for display. If someone ever hardcodes 0.2, this fails.
    assert floor.claim_floor_db < 0.2
    assert floor.basis == FLOOR_BASIS_MEASURED


def test_a_larger_measured_p95_moves_the_floor_with_it():
    """Mutation check: the floor tracks the measurement, not a constant."""

    tight = FloorStats.from_repeat_study(
        metric=METRIC, median_db=0.05, p95_db=0.085,
        source="s", measured_at="2026-07-31",
    )
    loose = FloorStats.from_repeat_study(
        metric=METRIC, median_db=0.10, p95_db=0.170,
        source="s", measured_at="2026-07-31",
    )
    assert loose.claim_floor_db == pytest.approx(2 * tight.claim_floor_db)


def test_policy_bar_floor_refuses_to_invent_a_measurement():
    floor = FloorStats.from_policy_bar(
        metric="anything", claim_floor_db=0.5, source="a shipped constant",
        scope=FLOOR_SCOPE_ACROSS_SITTINGS,
    )
    assert floor.basis == FLOOR_BASIS_POLICY
    # A back-solved p95 would turn a policy choice into a fake measurement.
    assert floor.p95_db is None
    assert floor.median_db is None


def test_floor_construction_refuses_nonsense():
    with pytest.raises(ValueError):
        FloorStats.from_repeat_study(
            metric=METRIC, median_db=0.0, p95_db=0.0, source="s", measured_at="",
        )
    with pytest.raises(ValueError):
        FloorStats.from_policy_bar(
            metric="", claim_floor_db=0.5, source="s",
            scope=FLOOR_SCOPE_WITHIN_SITTING,
        )
    with pytest.raises(ValueError):
        FloorStats.from_policy_bar(
            metric="m", claim_floor_db=0.5, source="",
            scope=FLOOR_SCOPE_WITHIN_SITTING,
        )
    with pytest.raises(ValueError):
        FloorStats(
            metric="m", claim_floor_db=0.5, basis="made_up", source="s",
        )
    # #2081: an unrecognised scope is refused for the same reason an
    # unrecognised basis is — a floor nobody can say what it licenses would
    # reach ``licenses_sitting_pair``'s permissive arm by falling through.
    with pytest.raises(ValueError):
        FloorStats(
            metric="m", claim_floor_db=0.5, basis=FLOOR_BASIS_POLICY,
            source="s", scope="whenever",
        )


# --------------------------------------------------------------------------
# Constraint 1 — consecutive attempts only; a fixed baseline is unreachable
# --------------------------------------------------------------------------


def test_decide_next_reads_only_the_last_two_attempts():
    """Drift-accumulating history must not be able to change the verdict.

    Attempt 1 carries a wildly different grade. If the kernel reached past the
    predecessor to any earlier attempt, this delta would change; it does not.
    """

    floor = _floor()
    tail = [
        _attempt("a2", grade_db=5.0),
        _attempt("a3", grade_db=4.0),
    ]
    with_far_baseline = decide_next([_attempt("a1", grade_db=99.0), *tail], floor)
    without = decide_next(tail, floor)
    assert with_far_baseline.magnitude_db == without.magnitude_db
    assert with_far_baseline.improvement_db == without.improvement_db
    assert with_far_baseline.basis_attempt_ids == ("a2", "a3")


def test_kernel_exposes_no_fixed_baseline_mode():
    """Mutation guard for the constraint, not a restatement of it.

    The study measured a fixed early baseline drifting +0.0046 dB/repeat while
    the consecutive comparison stayed flat, so a fixed-baseline option would
    be a slow lie rather than a feature. This test fails the moment a
    parameter appears that could express one.
    """

    parameters = set(inspect.signature(decide_next).parameters)
    assert parameters == {"history", "floor", "budget"}
    forbidden = {"baseline", "reference", "anchor", "first", "origin", "since"}
    assert not any(
        token in name.lower() for name in parameters for token in forbidden
    )


def test_a_non_comparable_predecessor_is_not_skipped_over():
    """The basis gap is always exactly one attempt.

    Reaching back to the last *good* attempt re-opens the drift question
    constraint 1 closed, and no banked gap-of-two deviation exists to
    calibrate what it would cost. So the kernel refuses instead.
    """

    history = [
        _attempt("a1", grade_db=5.0),
        _attempt("a2", comparable=False, reasons=("locate_failed",)),
        _attempt("a3", grade_db=1.0),
    ]
    decision = decide_next(history, _floor())
    assert decision.decision == STOP_EVIDENCE
    assert decision.reason == REASON_PREDECESSOR_NOT_COMPARABLE
    # Emphatically not an improvement of 4.0 dB graded against a1.
    assert decision.improvement_db is None


def test_an_attempt_over_the_repeat_cap_is_disclosed_not_hidden():
    floor = _floor()
    history = [
        _attempt("a1", grade_db=5.0, repeats_used=4),
        _attempt("a2", grade_db=1.0, repeats_used=9),
    ]
    decision = decide_next(history, floor)
    assert decision.repeats_over_cap is True
    assert decide_next(history[:1], floor).repeats_over_cap is False


# --------------------------------------------------------------------------
# The decision table — every stop reason
# --------------------------------------------------------------------------


def test_empty_history_awaits_the_first_attempt():
    decision = decide_next([], _floor())
    assert decision.decision == CONTINUE
    assert decision.reason == REASON_AWAITING_FIRST_ATTEMPT
    assert decision.attempts_used == 0


def test_first_attempt_is_a_baseline_not_a_comparison():
    decision = decide_next([_attempt("a1", grade_db=5.0)], _floor())
    assert decision.decision == CONTINUE
    assert decision.reason == REASON_BASELINE_ESTABLISHED
    assert decision.magnitude_db is None


def test_stop_evidence_when_the_attempt_itself_is_not_comparable():
    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0),
            _attempt("a2", comparable=False, reasons=("agc_behavioral_fail",)),
        ],
        _floor(),
    )
    assert decision.decision == STOP_EVIDENCE
    assert decision.reason == REASON_ATTEMPT_NOT_COMPARABLE
    assert decision.notes == ("agc_behavioral_fail",)


def test_stop_evidence_when_the_floor_measured_a_different_metric():
    """A floor measured on one instrument says nothing about another."""

    decision = decide_next(
        [_attempt("a1", grade_db=5.0), _attempt("a2", grade_db=1.0)],
        _floor(metric="some_other_metric"),
    )
    assert decision.decision == STOP_EVIDENCE
    assert decision.reason == REASON_FLOOR_METRIC_MISMATCH


def test_stop_evidence_when_provenance_is_mixed():
    """A predicted grade and a measured one are different instruments."""

    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0, provenance=PROVENANCE_MODEL_GRADED),
            _attempt("a2", grade_db=1.0, provenance=PROVENANCE_REALIZED),
        ],
        _floor(),
    )
    assert decision.decision == STOP_EVIDENCE
    assert decision.reason == REASON_PROVENANCE_MISMATCH
    assert set(decision.notes) == {PROVENANCE_MODEL_GRADED, PROVENANCE_REALIZED}


# --------------------------------------------------------------------------
# Constraint 4 — a floor licenses only the separation it was measured across
# (issue #2081)
# --------------------------------------------------------------------------


def test_a_within_sitting_floor_refuses_a_pair_from_two_sittings():
    """The #2081 repro, exactly: two applied candidates, one re-placed mic.

    Before the fix this returned ``continue / improvement_above_floor`` with
    ``improvement_db 0.4`` — a claim 2.35x the floor, made across a separation
    the study never measured, and with nothing in the record able to show that
    it had. The numbers below are the ones from the issue's own reproduction.
    """

    floor = FloorStats.from_repeat_study(
        metric=METRIC,
        median_db=BANKED_MEDIAN_DB,
        p95_db=BANKED_P95_DB,
        source="captures/repeat-floor-20260731 — mic bolted in place",
        measured_at="2026-07-31",
    )
    assert floor.scope == FLOOR_SCOPE_WITHIN_SITTING

    pair = [
        _attempt("candidate-OLD", grade_db=4.0, sitting_id="sitting-first-tune"),
        _attempt("candidate-NEW", grade_db=3.6, sitting_id="sitting-second-tune"),
    ]
    # The change is comfortably above the floor, so nothing but the sitting
    # rule can be what stops this.
    assert abs(4.0 - 3.6) > floor.claim_floor_db

    decision = decide_next(pair, floor)
    assert decision.decision == STOP_EVIDENCE
    assert decision.reason == REASON_SITTING_MISMATCH
    # Refused BEFORE any number was computed: an unlicensed comparison must not
    # leave a magnitude or an improvement lying around for a renderer to speak.
    assert decision.magnitude_db is None
    assert decision.improvement_db is None
    assert decision.improved is None
    # The basis and the reason both survive, so a support read can see which
    # two attempts were refused and why.
    assert decision.basis_attempt_ids == ("candidate-OLD", "candidate-NEW")
    assert f"floor scope {FLOOR_SCOPE_WITHIN_SITTING}" in decision.notes
    assert "previous sitting sitting-first-tune" in decision.notes
    assert "latest sitting sitting-second-tune" in decision.notes


def test_the_same_pair_is_graded_when_the_two_attempts_share_a_sitting():
    """The other half of the mutation: only the sitting changes the verdict."""

    floor = FloorStats.from_repeat_study(
        metric=METRIC, median_db=BANKED_MEDIAN_DB, p95_db=BANKED_P95_DB,
        source="captures/repeat-floor-20260731", measured_at="2026-07-31",
    )
    decision = decide_next(
        [
            _attempt("candidate-OLD", grade_db=4.0, sitting_id="one-sitting"),
            _attempt("candidate-NEW", grade_db=3.6, sitting_id="one-sitting"),
        ],
        floor,
    )
    assert decision.decision == CONTINUE
    assert decision.reason == REASON_IMPROVEMENT_ABOVE_FLOOR
    assert decision.improvement_db == pytest.approx(0.4)


def test_two_unrecorded_sittings_are_refused_rather_than_read_as_one():
    """``"" == ""`` is the trap: state written before #2081 carries two blanks.

    Every persisted attempt history on a shipped speaker looks like this on the
    deploy that lands the fix, so the permissive reading is not hypothetical —
    it is what the first upgraded speaker would have done.
    """

    decision = decide_next(
        [
            _attempt("candidate-OLD", grade_db=4.0, sitting_id=""),
            _attempt("candidate-NEW", grade_db=3.6, sitting_id=""),
        ],
        _floor(),
    )
    assert decision.decision == STOP_EVIDENCE
    # Its OWN reason, not the mismatch one: nothing here says the mic moved.
    assert decision.reason == REASON_SITTING_UNRECORDED
    assert "previous sitting unrecorded" in decision.notes
    assert "latest sitting unrecorded" in decision.notes


def test_one_unrecorded_sitting_is_refused_as_unrecorded_not_as_a_mismatch():
    decision = decide_next(
        [
            _attempt("candidate-OLD", grade_db=4.0, sitting_id=""),
            _attempt("candidate-NEW", grade_db=3.6, sitting_id="known"),
        ],
        _floor(),
    )
    assert decision.reason == REASON_SITTING_UNRECORDED


def test_an_across_sittings_floor_grades_a_cross_sitting_pair():
    """The scope is the knob, and it is the floor's — not the kernel's.

    A future re-placement study, or a policy bar about something no microphone
    sits between, licenses the wider comparison by SAYING so. Nothing in
    ``decide_next`` needs editing for that, which is the whole reason the
    licence lives on ``FloorStats``.
    """

    decision = decide_next(
        [
            _attempt("candidate-OLD", grade_db=4.0, sitting_id="first"),
            _attempt("candidate-NEW", grade_db=3.6, sitting_id="second"),
        ],
        _floor(scope=FLOOR_SCOPE_ACROSS_SITTINGS),
    )
    assert decision.decision == CONTINUE
    assert decision.reason == REASON_IMPROVEMENT_ABOVE_FLOOR
    assert decision.improvement_db == pytest.approx(0.4)


def test_an_across_sittings_floor_grades_even_unrecorded_sittings():
    """It licenses the comparison regardless, so it never asks."""

    decision = decide_next(
        [
            _attempt("candidate-OLD", grade_db=4.0, sitting_id=""),
            _attempt("candidate-NEW", grade_db=3.6, sitting_id=""),
        ],
        _floor(scope=FLOOR_SCOPE_ACROSS_SITTINGS),
    )
    assert decision.reason == REASON_IMPROVEMENT_ABOVE_FLOOR


def test_the_sitting_refusal_outranks_the_floor_comparison():
    """Order pin: a SUB-floor cross-sitting change is refused, not "too small".

    Reporting ``below_claim_floor`` here would be the same unlicensed claim
    inverted — "the instrument says nothing changed" is still a statement about
    a pair the instrument was never characterised on.
    """

    decision = decide_next(
        [
            _attempt("candidate-OLD", grade_db=4.0, sitting_id="first"),
            _attempt("candidate-NEW", grade_db=3.99, sitting_id="second"),
        ],
        _floor(),
    )
    assert abs(4.0 - 3.99) < _floor().claim_floor_db
    assert decision.reason == REASON_SITTING_MISMATCH
    assert decision.decision == STOP_EVIDENCE


def test_a_first_attempt_needs_no_sitting_because_it_compares_nothing():
    """One attempt is a baseline, not a pair — the rule has nothing to guard."""

    decision = decide_next([_attempt("only", grade_db=4.0, sitting_id="")], _floor())
    assert decision.decision == CONTINUE
    assert decision.reason == REASON_BASELINE_ESTABLISHED


def test_a_sitting_id_is_never_confused_with_an_attempt_id():
    """Two attempts always differ by id; that says nothing about the mic.

    Pinned because ``attempt_id`` is the obvious field to reach for when
    someone later wonders whether the sitting one is redundant.
    """

    same_sitting = decide_next(
        [
            _attempt("wildly-different-id-1", grade_db=4.0, sitting_id="s"),
            _attempt("wildly-different-id-2", grade_db=3.6, sitting_id="s"),
        ],
        _floor(),
    )
    assert same_sitting.reason == REASON_IMPROVEMENT_ABOVE_FLOOR


def test_every_floor_scope_is_declared_and_the_default_is_the_narrow_one():
    assert FLOOR_SCOPES == {FLOOR_SCOPE_WITHIN_SITTING, FLOOR_SCOPE_ACROSS_SITTINGS}
    # Fail-closed: a floor that never says which separation it covers licenses
    # the narrow comparison only.
    bare = FloorStats(
        metric=METRIC, claim_floor_db=0.5, basis=FLOOR_BASIS_POLICY, source="s",
    )
    assert bare.scope == FLOOR_SCOPE_WITHIN_SITTING
    assert bare.to_dict()["scope"] == FLOOR_SCOPE_WITHIN_SITTING
    # And a repeat study is a within-sitting measurement unless it says it
    # re-placed the mic.
    study = FloorStats.from_repeat_study(
        metric=METRIC, median_db=0.05, p95_db=0.085, source="s",
        measured_at="2026-07-31",
    )
    assert study.scope == FLOOR_SCOPE_WITHIN_SITTING
    assert FloorStats.from_repeat_study(
        metric=METRIC, median_db=0.05, p95_db=0.085, source="s",
        measured_at="2026-07-31", scope=FLOOR_SCOPE_ACROSS_SITTINGS,
    ).scope == FLOOR_SCOPE_ACROSS_SITTINGS


def test_a_policy_bar_must_say_what_it_licenses():
    """No default, because there is no fact behind a declared bar to infer one
    from — see ``from_policy_bar``'s own note."""

    with pytest.raises(TypeError):
        FloorStats.from_policy_bar(  # type: ignore[call-arg]
            metric=METRIC, claim_floor_db=0.5, source="s",
        )


def test_stop_evidence_when_an_above_floor_change_has_no_known_direction():
    """A magnitude-only comparator moved a lot. Which way is unknown."""

    decision = decide_next(
        [
            _attempt("a1"),
            _attempt("a2", deviation_from_predecessor_db=9.0),
        ],
        _floor(),
    )
    assert decision.decision == STOP_EVIDENCE
    assert decision.reason == REASON_DIRECTION_UNKNOWN_ABOVE_FLOOR
    assert decision.magnitude_db == pytest.approx(9.0)
    assert decision.improved is None


def test_stop_evidence_when_an_improvement_rode_a_shrinking_denominator():
    """flat_spec hands this hazard to the loop by name; here is where it lands."""

    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0, n_graded_bins=120),
            _attempt("a2", grade_db=1.0, n_graded_bins=40),
        ],
        _floor(),
    )
    assert decision.decision == STOP_EVIDENCE
    assert decision.reason == REASON_GRADED_BINS_SHRANK


def test_a_growing_denominator_does_not_block_an_improvement():
    """The guard is asymmetric: more bins only makes an improvement harder."""

    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0, n_graded_bins=40),
            _attempt("a2", grade_db=1.0, n_graded_bins=120),
        ],
        _floor(),
    )
    assert decision.decision == CONTINUE
    assert decision.reason == REASON_IMPROVEMENT_ABOVE_FLOOR


def test_stop_floor_when_the_change_is_smaller_than_the_instrument_resolves():
    decision = decide_next(
        [
            _attempt("a1", grade_db=5.000),
            _attempt("a2", grade_db=4.900),
        ],
        _floor(claim_floor_db=0.17016),
    )
    assert decision.decision == STOP_FLOOR
    assert decision.reason == REASON_BELOW_CLAIM_FLOOR
    assert decision.magnitude_db == pytest.approx(0.1, abs=1e-9)
    # Sub-floor is not a regression; it is unknowable either way. The
    # improvement is still disclosed, just not claimable.
    assert decision.improved is None
    assert decision.improvement_db == pytest.approx(0.1, abs=1e-9)


def test_stop_floor_fires_on_a_sub_floor_regression_too():
    decision = decide_next(
        [_attempt("a1", grade_db=4.900), _attempt("a2", grade_db=5.000)],
        _floor(claim_floor_db=0.17016),
    )
    assert decision.decision == STOP_FLOOR
    assert decision.improved is None


def test_continue_on_a_material_improvement():
    decision = decide_next(
        [_attempt("a1", grade_db=5.0), _attempt("a2", grade_db=1.0)], _floor(),
    )
    assert decision.decision == CONTINUE
    assert decision.reason == REASON_IMPROVEMENT_ABOVE_FLOOR
    assert decision.improved is True
    assert decision.improvement_db == pytest.approx(4.0)
    assert decision.basis_attempt_ids == ("a1", "a2")
    assert decision.should_continue is True


def test_an_above_floor_regression_continues_but_never_reads_as_approval():
    decision = decide_next(
        [_attempt("a1", grade_db=1.0), _attempt("a2", grade_db=5.0)], _floor(),
    )
    assert decision.decision == CONTINUE
    assert decision.reason == REASON_REGRESSION_FROM_PREDECESSOR
    assert decision.improved is False
    assert decision.improvement_db == pytest.approx(-4.0)


def test_stop_converged_when_nothing_material_is_predicted_to_remain():
    bar = material_improvement_db()
    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0),
            _attempt(
                "a2", grade_db=1.0, predicted_remaining_improvement_db=bar / 10.0,
            ),
        ],
        _floor(),
    )
    assert decision.decision == STOP_CONVERGED
    assert decision.reason == REASON_NO_MATERIAL_IMPROVEMENT_PREDICTED


def test_a_material_predicted_remainder_keeps_the_loop_going():
    bar = material_improvement_db()
    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0),
            _attempt(
                "a2", grade_db=1.0, predicted_remaining_improvement_db=bar * 10.0,
            ),
        ],
        _floor(),
    )
    assert decision.decision == CONTINUE


def test_stop_converged_when_already_in_spec():
    decision = decide_next([_attempt("a1", grade_db=5.0, in_spec=True)], _floor())
    assert decision.decision == STOP_CONVERGED
    assert decision.reason == REASON_IN_SPEC


def test_stop_budget_when_the_attempt_budget_is_spent():
    budget = AttemptBudget(target_attempts=1, hard_cap_attempts=2)
    decision = decide_next(
        [_attempt("a1", grade_db=5.0), _attempt("a2", grade_db=1.0)],
        _floor(),
        budget=budget,
    )
    assert decision.decision == STOP_BUDGET
    assert decision.reason == REASON_BUDGET_EXHAUSTED
    # The improvement is still disclosed — the loop stopped for want of tries,
    # not for want of progress.
    assert decision.improvement_db == pytest.approx(4.0)


def test_the_floor_outranks_the_budget():
    """When both hold, "too small to resolve" is the more useful truth."""

    budget = AttemptBudget(target_attempts=1, hard_cap_attempts=2)
    decision = decide_next(
        [_attempt("a1", grade_db=5.0), _attempt("a2", grade_db=4.99)],
        _floor(),
        budget=budget,
    )
    assert decision.decision == STOP_FLOOR


def test_convergence_outranks_the_budget():
    budget = AttemptBudget(target_attempts=1, hard_cap_attempts=2)
    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0),
            _attempt("a2", grade_db=1.0, in_spec=True),
        ],
        _floor(),
        budget=budget,
    )
    assert decision.decision == STOP_CONVERGED


def test_evidence_outranks_everything():
    budget = AttemptBudget(target_attempts=1, hard_cap_attempts=1)
    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0),
            _attempt("a2", grade_db=1.0, comparable=False, reasons=("bad",)),
        ],
        _floor(),
        budget=budget,
    )
    assert decision.decision == STOP_EVIDENCE


# --------------------------------------------------------------------------
# Budget, records, and the walk
# --------------------------------------------------------------------------


def test_budget_defaults_are_three_and_four_and_are_both_disclosed():
    budget = AttemptBudget()
    assert (budget.target_attempts, budget.hard_cap_attempts) == (3, 4)
    decision = decide_next([_attempt("a1", grade_db=1.0)], _floor())
    assert decision.budget.to_dict() == {
        "target_attempts": 3, "hard_cap_attempts": 4,
    }


def test_budget_refuses_an_impossible_shape():
    with pytest.raises(ValueError):
        AttemptBudget(target_attempts=0)
    with pytest.raises(ValueError):
        AttemptBudget(target_attempts=4, hard_cap_attempts=3)


def test_attempt_record_refuses_a_negative_magnitude():
    with pytest.raises(ValueError):
        _attempt("a1", deviation_from_predecessor_db=-1.0)


def test_attempt_record_refuses_unknown_provenance():
    with pytest.raises(ValueError):
        _attempt("a1", provenance="vibes")


def test_an_explicit_comparator_deviation_beats_a_subtracted_one():
    """The comparator measured the change; subtraction only estimates it."""

    decision = decide_next(
        [
            _attempt("a1", grade_db=5.0),
            _attempt("a2", grade_db=1.0, deviation_from_predecessor_db=0.05),
        ],
        _floor(),
    )
    assert decision.magnitude_db == pytest.approx(0.05)
    assert decision.decision == STOP_FLOOR
    # Direction is still known, and still disclosed, even though the
    # magnitude came from elsewhere.
    assert decision.improvement_db == pytest.approx(4.0)


def test_every_decision_serialises_with_the_numbers_it_used():
    decision = decide_next(
        [_attempt("a1", grade_db=5.0), _attempt("a2", grade_db=1.0)], _floor(),
    )
    payload = decision.to_dict()
    assert payload["decision"] in DECISIONS
    assert payload["basis_attempt_ids"] == ["a1", "a2"]
    assert payload["magnitude_db"] == pytest.approx(4.0)
    assert payload["floor"]["claim_floor_db"] == pytest.approx(0.17016)
    assert payload["provenance"] == PROVENANCE_REALIZED
    assert payload["budget"]["hard_cap_attempts"] == 4


def test_material_improvement_bar_is_the_shipped_constant_not_a_copy():
    from jasper.active_speaker.crossover_v2.attempt_grading import (
        PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    )

    assert material_improvement_db() == PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB


def test_kernel_imports_nothing_at_module_scope_but_stdlib():
    """Import-time lightness, pinned where it is actually decidable.

    Statically, not by watching ``sys.modules``: by the time any test runs,
    numpy is long since imported by something else, so an in-process check
    could never fail. Reading this module's own top-level imports is a complete
    proof — if none of them reaches outside the standard library, importing it
    cannot pull numpy no matter what the rest of the tree does.

    This is also the guard on the deferred import in
    :func:`material_improvement_db`. Hoisting that ``crossover_v2_flow`` import
    to module scope — the obvious "tidy-up" — pulls numpy and scipy into every
    import of the kernel and fails right here.
    """

    import ast
    import sys

    import jasper.active_speaker.attempts_loop as kernel

    tree = ast.parse(inspect.getsource(kernel))
    module_scope: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_scope.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_scope.append(node.module or "")

    roots = {name.split(".")[0] for name in module_scope if name}
    # `__future__` is a compiler directive, not a package.
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    assert roots <= allowed, (
        f"kernel imports {sorted(roots - allowed)} at module scope; "
        "import-time lightness is part of this module's contract"
    )
    assert "numpy" not in roots and "scipy" not in roots


def test_the_convergence_bar_is_reached_by_a_deferred_import():
    """The runtime cost the docstring admits to, shown rather than asserted.

    Not a complaint — it is the price of one source of truth for the shipped
    bar. Pinned so the docstring's admission and the code stay one fact.
    """

    source = inspect.getsource(material_improvement_db)
    assert "from jasper.active_speaker.crossover_v2_flow import" in source
    # ...and it really does resolve to the shipped constant when called.
    assert material_improvement_db() > 0.0


def test_kernel_performs_no_io():
    """The purity claim, checked from the module's own imports.

    The split between this kernel and ``model_error_store`` only means
    something if the kernel really cannot touch a disk.
    """

    import jasper.active_speaker.attempts_loop as kernel

    source = inspect.getsource(kernel)
    for forbidden in ("import os", "import json", "open(", "Path(", "requests"):
        assert forbidden not in source, f"kernel reached for {forbidden!r}"
