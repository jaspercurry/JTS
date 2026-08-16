# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The two measurements one correction round compares (#2291 Phase 3c).

:mod:`jasper.active_speaker.crossover_v2.round_evidence` sits between a
capture and a verdict: it reduces one summed at-the-mark
:class:`~jasper.audio_measurement.program_analysis.ProgramAnalysis` to one side
of the before/after benefit comparison, and it makes the ONE assembly decision
:func:`~jasper.active_speaker.crossover_v2.verification.evaluate_benefit`
explicitly leaves to its caller — which exclusion mask both sides are graded
on.

So these tests are about three things, and deliberately not about verdicts
(``tests/test_crossover_v2_verification.py`` owns those):

1. the reduction is the SHIPPED owners' arithmetic, not a second copy;
2. the mask is a UNION applied to BOTH sides, and comparability failures are
   passed through rather than papered over;
3. :data:`~jasper.active_speaker.crossover_v2.round_evidence.MEASURED_BENEFIT_MARGIN_DB`
   is a FORK of ``material_improvement_db``, not an alias — the whole point of
   #2291's ledger item N8 is that the two must be free to move apart.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import round_evidence
from jasper.active_speaker.crossover_v2.contracts import (
    ADOPTION_ROW_KEEP,
    ADOPTION_ROW_KEEP_FOR_ITERATION,
    ADOPTION_ROW_KEEP_ITERATING,
    ADOPTION_ROW_RESTORE_FAILED,
    ADOPTION_ROW_RESTORE_REGRESSION,
    ADOPTION_ROW_RESTORE_UNSAFE,
    ADOPTION_ROW_RESTORE_UNTRUSTED,
    AdoptionOutcome,
    BenefitStatus,
    CaptureValidity,
    CrossoverV2ContractError,
    EvidenceTrust,
    QualityStatus,
    RealizationStatus,
    SpecStatus,
)
from jasper.active_speaker.crossover_v2.round_evidence import (
    BENEFIT_CURVE_MAX_BINS,
    MEASURED_BENEFIT_MARGIN_DB,
    EntryBaseline,
    MeasuredResponse,
    benefit_comparands,
    measured_response_from_analysis,
)
from jasper.active_speaker.crossover_v2.verification import (
    BENEFIT_BASELINE_UNAVAILABLE,
    BENEFIT_GRID_MISMATCH,
    BENEFIT_POST_UNAVAILABLE,
    BENEFIT_PROGRAM_MISMATCH,
    evaluate_benefit,
)

_MARK = "design_axis_mark"


def _grid(n: int = 2048) -> np.ndarray:
    """An rFFT-shaped frequency axis — starting at DC, as a real one does.

    Deliberately not ``linspace(20, …)``: ``DriverResponse.freqs_hz`` comes off
    an rFFT and its first bin is 0 Hz. Starting the fixture at 20 Hz would make
    the sub-1-Hz region untestable, and that is precisely the region a bool
    ``validity_floor_hz`` would silently clamp at (``float(True) == 1.0``).
    """
    return np.linspace(0.0, 24000.0, n)


def _analysis(
    *,
    program_id: str = "prog-a",
    freqs: np.ndarray | None = None,
    magnitude_db: np.ndarray | None = None,
    validity_floor_hz: float | None = None,
    summed: object | None = ...,  # type: ignore[assignment]
):
    """A ProgramAnalysis-shaped double carrying only what the reducer reads.

    A double rather than a real analysis because building one needs a capture,
    a deconvolution, and a program — none of which this module touches. The
    three attributes read at runtime are named in the reducer's own body
    (``program_id``, ``summed_response``, and the summed response's
    ``freqs_hz``/``magnitude_db``/``validity_floor_hz``), so a double that
    carries exactly those is the whole contract.
    """

    if summed is ...:
        hz = _grid() if freqs is None else freqs
        db = (
            np.sin(np.log10(np.maximum(hz, 1.0)) * 5.0)
            if magnitude_db is None
            else magnitude_db
        )
        summed = SimpleNamespace(
            freqs_hz=hz, magnitude_db=db, validity_floor_hz=validity_floor_hz
        )
    return SimpleNamespace(program_id=program_id, summed_response=summed)


# --------------------------------------------------------------------------- #
# 1. the reduction is the shipped owners'
# --------------------------------------------------------------------------- #


def test_the_curve_is_the_shipped_decimate_then_smooth_not_a_second_copy():
    """Asserted against the owners themselves, so a drifted copy cannot pass.

    ``spec_report_for_predicted_sum`` puts one curve in front of
    ``evaluate_flat_spec`` with exactly these two steps in exactly this order,
    and doing it differently here would grade the round's two captures on a
    different curve from every other spec-graded curve in the subsystem. The
    expected value is computed by CALLING those owners rather than by baking
    numbers, so the pin follows them if they change and fails if this module
    stops using them.
    """
    from jasper.audio_measurement.analysis import smooth_fractional_octave
    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    hz = _grid()
    db = np.sin(np.log10(np.maximum(hz, 1.0)) * 5.0)

    reduced = measured_response_from_analysis(
        _analysis(freqs=hz, magnitude_db=db), reference_mark=_MARK
    )
    assert reduced is not None

    grid, coarse = decimate_curve_to_analysis_grid(
        hz, db, max_bins=BENEFIT_CURVE_MAX_BINS
    )
    expected = smooth_fractional_octave(grid, coarse, fraction=3)

    assert reduced.curve.hz == tuple(float(f) for f in grid)
    assert reduced.curve.db == pytest.approx(list(expected))
    assert len(reduced.curve.hz) <= BENEFIT_CURVE_MAX_BINS


def test_the_program_id_and_mark_ride_through_unchanged():
    """Comparability's two identity fields are carried, never re-derived.

    ``program_id`` equality is the whole comparability check
    (``MeasurementComparand``'s docstring: a SHA-256 over the excitation
    schedule, so equal ids are a cryptographic guarantee of same program AND
    same level). A reducer that minted its own id would make that guarantee
    meaningless.
    """
    reduced = measured_response_from_analysis(
        _analysis(program_id="prog-xyz"), reference_mark=_MARK
    )

    assert reduced is not None
    assert reduced.program_id == "prog-xyz"
    assert reduced.reference_mark == _MARK


@pytest.mark.parametrize(
    "analysis",
    [
        None,
        _analysis(summed=None),
        _analysis(program_id=""),
        # A non-finite bin: ``ResponseCurve`` refuses it rather than hashing
        # it, so the reduction cannot produce a comparand. "Cannot compare
        # this" is an answer, not a crash to propagate into a household
        # decision.
        _analysis(
            freqs=_grid(64),
            magnitude_db=np.concatenate([np.zeros(63), [np.nan]]),
        ),
    ],
    ids=["no_analysis", "no_summed_response", "no_program_id", "non_finite_bin"],
)
def test_an_unreducible_capture_is_none_never_a_raise(analysis):
    assert measured_response_from_analysis(analysis, reference_mark=_MARK) is None


def test_a_curve_too_narrow_to_grade_still_reduces_and_the_evaluator_says_so():
    """The reducer reduces; it does not grade, and it does not pre-judge.

    A two-bin curve reaches no spec band, so there is no pooled residual to
    difference — but that is the EVALUATOR's answer
    (:data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_RESIDUAL_UNEVALUABLE`),
    not a reason for the reducer to withhold the capture. Keeping the two
    responsibilities apart is why "unevaluable" arrives with a reason instead
    of as a missing side.
    """
    from jasper.active_speaker.crossover_v2.verification import (
        BENEFIT_RESIDUAL_UNEVALUABLE,
    )

    reduced = measured_response_from_analysis(
        _analysis(
            freqs=np.array([100.0, 200.0]), magnitude_db=np.array([0.0, 1.0])
        ),
        reference_mark=_MARK,
    )
    assert reduced is not None

    baseline, post = benefit_comparands(baseline=reduced, post=reduced)
    verdict = evaluate_benefit(entry_baseline=baseline, post=post, margin_db=0.5)

    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_RESIDUAL_UNEVALUABLE


# --------------------------------------------------------------------------- #
# 2. the validity clamp
# --------------------------------------------------------------------------- #


def test_bins_below_the_captures_own_validity_floor_are_screened():
    """The same clamp the cloud pipeline unions into its spec mask.

    Below ``gating.f_valid_floor_hz`` the response is an artifact of a
    truncated gate window, so those bins must not decide a verdict either way.
    """
    reduced = measured_response_from_analysis(
        _analysis(validity_floor_hz=500.0), reference_mark=_MARK
    )

    assert reduced is not None
    flagged = [
        hz for hz, excluded in zip(reduced.curve.hz, reduced.excluded) if excluded
    ]
    unflagged = [
        hz for hz, excluded in zip(reduced.curve.hz, reduced.excluded) if not excluded
    ]
    assert flagged, "a 500 Hz floor must screen the bins beneath it"
    assert max(flagged) < 500.0
    assert min(unflagged) >= 500.0


@pytest.mark.parametrize(
    ("floor", "expected"),
    [
        (None, (False, False, False)),
        (float("nan"), (False, False, False)),
        (float("inf"), (False, False, False)),
        ("2.0", (False, False, False)),
        # ``True`` is an ``int``, so a clamp that forgot to reject bools would
        # read it as a 1.0 Hz floor. On the production grid the coarsest bin
        # already sits near 23 Hz, so that mistake is INVISIBLE end-to-end —
        # measured: an end-to-end version of this case survived deleting the
        # bool guard outright. Only a sub-1-Hz axis can see it, which is why
        # this one case reaches the clamp directly.
        (True, (False, False, False)),
        (2.0, (True, True, False)),
    ],
    ids=["absent", "nan", "inf", "string", "bool", "a_real_floor"],
)
def test_the_clamp_screens_only_for_a_finite_numeric_floor(floor, expected):
    """The clamp itself, on an axis fine enough to expose every branch.

    Reached by name because the fact under test has no public surface and the
    production grid cannot express it; the helper is named for the rule, so a
    future public accessor can replace the body without touching this test.
    """
    grid = np.array([0.25, 0.75, 5.0])

    assert round_evidence._validity_clamp(grid, floor) == expected


@pytest.mark.parametrize(
    "floor",
    [None, float("nan"), float("inf"), "500"],
    ids=["absent", "nan", "inf", "string"],
)
def test_a_floor_that_is_not_a_finite_number_screens_nothing(floor):
    """"No evidence of a floor" is not "the floor is at zero".

    Over-screening is not the safe direction here: it shrinks the graded
    denominator the whole before/after comparison depends on, which is the
    exact hazard ``spec_convergence_residual`` warns a loop about. ``True`` is
    listed because it is an ``int`` and would otherwise clamp at 1 Hz.
    """
    reduced = measured_response_from_analysis(
        _analysis(validity_floor_hz=floor), reference_mark=_MARK
    )

    assert reduced is not None
    assert not any(reduced.excluded)


# --------------------------------------------------------------------------- #
# 3. the one assembly decision — a shared mask
# --------------------------------------------------------------------------- #


def _measured(program_id: str, hz, db, excluded) -> MeasuredResponse:
    from jasper.active_speaker.crossover_v2.contracts import ResponseCurve

    return MeasuredResponse(
        program_id=program_id,
        reference_mark=_MARK,
        curve=ResponseCurve(hz, db),
        excluded=tuple(excluded),
    )


def test_the_two_sides_are_graded_on_the_union_of_their_screens():
    """Union, applied to BOTH — the decision the evaluator delegates here.

    Equal masks mean equal graded bins by construction, so the residual cannot
    fall merely because the honesty screen grew. Asserted as the union of two
    DIFFERENT single-bin screens, which an intersection (or either side's own
    mask carried through unchanged) cannot satisfy.
    """
    hz, db = [100.0, 200.0, 300.0], [0.0, 1.0, 2.0]
    baseline = _measured("p", hz, db, [True, False, False])
    post = _measured("p", hz, db, [False, True, False])

    got_baseline, got_post = benefit_comparands(baseline=baseline, post=post)

    assert got_baseline is not None and got_post is not None
    assert got_baseline.exclusion_mask == (True, True, False)
    assert got_post.exclusion_mask == (True, True, False)


def test_a_grid_disagreement_leaves_each_side_on_its_own_screen():
    """Comparability is the evaluator's answer, never manufactured here.

    A caller that quietly interpolated one side onto the other's grid would be
    inventing the comparability the round exists to check, and the verdict
    would read as a measurement instead of an assumption.

    The discriminating assertion is on the MASKS, not on the verdict: the
    evaluator checks grids before masks, so it reports
    ``incomparable_frequency_grid`` whether or not this function unioned
    anything — a verdict-only assertion would pass for a version that had
    quietly merged two incomparable screens. Two DIFFERENT single-bin screens
    that each survive unchanged is what only the pass-through does.
    """
    baseline = _measured("p", [100.0, 200.0], [0.0, 0.0], [True, False])
    post = _measured("p", [100.0, 250.0], [0.0, 0.0], [False, True])

    got_baseline, got_post = benefit_comparands(baseline=baseline, post=post)

    assert got_baseline is not None and got_post is not None
    assert got_baseline.exclusion_mask == (True, False)
    assert got_post.exclusion_mask == (False, True)
    assert got_baseline.curve.hz != got_post.curve.hz
    verdict = evaluate_benefit(
        entry_baseline=got_baseline, post=got_post, margin_db=0.5
    )
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_GRID_MISMATCH


def test_grids_of_different_lengths_do_not_raise_on_the_way_to_the_verdict():
    """The strict zip is only safe because the grid check runs first.

    ``zip(..., strict=True)`` is deliberate — silently truncating one side's
    mask is the class of bug this whole module exists to prevent — but it
    means a length disagreement that reached it would be a ``ValueError``
    escaping into a household verdict. The grid guard is what keeps it out of
    reach, and this is the pin on that ordering.
    """
    baseline = _measured("p", [100.0, 200.0], [0.0, 0.0], [False, False])
    post = _measured("p", [100.0, 200.0, 300.0], [0.0, 0.0, 0.0], [False] * 3)

    got_baseline, got_post = benefit_comparands(baseline=baseline, post=post)
    verdict = evaluate_benefit(
        entry_baseline=got_baseline, post=got_post, margin_db=0.5
    )

    assert verdict.reason == BENEFIT_GRID_MISMATCH


def test_a_program_disagreement_is_named_by_the_evaluator_not_hidden():
    """The 2026-08-10 shape: a "before" that was never the same question."""
    hz, db = [100.0, 200.0], [0.0, 0.0]
    baseline = _measured("prog-old", hz, db, [False, False])
    post = _measured("prog-new", hz, db, [False, False])

    got_baseline, got_post = benefit_comparands(baseline=baseline, post=post)
    verdict = evaluate_benefit(
        entry_baseline=got_baseline, post=got_post, margin_db=0.5
    )

    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == BENEFIT_PROGRAM_MISMATCH


@pytest.mark.parametrize(
    ("drop", "reason"),
    [("baseline", BENEFIT_BASELINE_UNAVAILABLE), ("post", BENEFIT_POST_UNAVAILABLE)],
)
def test_a_missing_side_is_indeterminate_and_says_which_side(drop, reason):
    hz, db = [100.0, 200.0], [0.0, 0.0]
    present = _measured("p", hz, db, [False, False])

    got_baseline, got_post = benefit_comparands(
        baseline=None if drop == "baseline" else present,
        post=None if drop == "post" else present,
    )
    verdict = evaluate_benefit(
        entry_baseline=got_baseline, post=got_post, margin_db=0.5
    )

    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.reason == reason


# --------------------------------------------------------------------------- #
# 4. the persisted entry baseline
# --------------------------------------------------------------------------- #


def test_the_entry_baseline_round_trips_through_the_durable_shape():
    """It crosses the stage bridge as JSON; nothing may be lost on the way.

    Every field, exhaustively — a partial round-trip is how a curve arrives in
    stage 2 with its mask silently reset to all-false, which would grade the
    two captures over different bins while looking comparable.
    """
    reduced = measured_response_from_analysis(
        _analysis(validity_floor_hz=300.0), reference_mark=_MARK
    )
    assert reduced is not None
    original = EntryBaseline.from_measurement(
        reduced,
        graph_fingerprint="graph-fp",
        captured_at="2026-08-11T00:00:00Z",
        artifact_ref="entry_baseline_a01",
    )

    rehydrated = EntryBaseline.from_dict(original.to_dict())

    assert rehydrated == original
    assert rehydrated is not None
    assert rehydrated.as_measurement() == reduced


def _complete_record(**overrides) -> dict:
    """A record that rehydrates, so a rejection test can vary ONE thing.

    Every negative case below starts from a record that would otherwise
    SUCCEED. Without that, a case meant to pin the mask-length check passes
    for the wrong reason — because it also happened to be missing
    ``program_id`` — and the check it names is not covered at all. (Measured:
    an earlier form of this test survived deleting the length check outright.)
    """
    record = {
        "freqs_hz": [100.0, 200.0],
        "magnitude_db": [0.0, 1.0],
        "excluded": [False, True],
        "program_id": "p",
        "reference_mark": "m",
        "graph_fingerprint": "g",
        "captured_at": "t",
    }
    record.update(overrides)
    return record


def test_the_complete_record_control_rehydrates():
    """The control the rejection cases below are one field away from.

    Without it, every "returns None" assertion could be passing because the
    fixture never rehydrated at all.
    """
    assert EntryBaseline.from_dict(_complete_record()) is not None


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        "not a mapping",
        _complete_record(excluded=[False]),
        _complete_record(excluded=[False, True, False]),
        _complete_record(excluded=None),
        _complete_record(freqs_hz=None),
        _complete_record(magnitude_db=None),
        _complete_record(magnitude_db=[0.0, float("nan")]),
        _complete_record(program_id=""),
        _complete_record(graph_fingerprint=""),
    ],
    ids=[
        "none",
        "empty",
        "not_a_mapping",
        "mask_too_short",
        "mask_too_long",
        "no_mask",
        "no_freqs",
        "no_levels",
        "non_finite_level",
        "empty_program_id",
        "empty_graph_fingerprint",
    ],
)
def test_anything_this_build_did_not_write_rehydrates_as_no_baseline(record):
    """``None``, never a partially-trusted record and never a raise.

    A state file from before this key shipped, a truncated write, and a
    hand-edited file all mean one thing to the round — there is no comparable
    baseline — and that already has an honest verdict.
    """
    assert EntryBaseline.from_dict(record) is None


# --------------------------------------------------------------------------- #
# 5. the margin is a fork, not an alias (#2291 ledger item N8)
# --------------------------------------------------------------------------- #


def test_the_benefit_margin_is_a_literal_this_module_owns_not_a_borrowed_one():
    """The fork, pinned structurally — the only way it CAN be pinned.

    ``material_improvement_db()`` bounds model-vs-hardware error; this bounds
    capture repeatability. They agree today, which is exactly why an equality
    assertion would be worthless: it would pass just as happily if someone
    "simplified" this constant into an alias of that one, and #2291's ledger
    item N8 exists because that simplification is the tempting edit.

    A *behavioural* pin cannot see it either — measured: monkeypatching
    ``PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`` and asserting this constant did
    not follow SURVIVED a mutation that rebound it to
    ``material_improvement_db()`` at import time, because a module constant is
    bound once and the later patch cannot reach it. So the pin is on the
    module's own syntax: this name is assigned a plain numeric literal, and
    neither borrowed name is imported anywhere in the module.
    """
    import ast
    import inspect

    source = inspect.getsource(round_evidence)
    tree = ast.parse(source)

    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "MEASURED_BENEFIT_MARGIN_DB"
            for t in node.targets
        )
    ]
    assert len(assignments) == 1, "one owner, one assignment"
    value = assignments[0].value
    assert isinstance(value, ast.Constant) and isinstance(
        value.value, float
    ), "the margin must be a literal this module owns, not a borrowed call"

    borrowed = {"material_improvement_db", "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB"}
    imported = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not (borrowed & imported), (
        f"the margin's module must not import {borrowed & imported} — "
        "the two constants have to stay free to move apart"
    )


def test_the_two_constants_agree_today_and_the_docstring_says_why():
    """Recording the coincidence, so the fork is not mistaken for a typo.

    A reader who finds two 0.5s and no explanation reasonably concludes one is
    redundant. The equality is asserted here precisely so that the day it
    stops holding, this test — not a puzzled reviewer — is what notices.
    """
    from jasper.active_speaker import attempts_loop

    assert MEASURED_BENEFIT_MARGIN_DB == pytest.approx(
        attempts_loop.material_improvement_db()
    )
    doc = round_evidence.__dict__["__doc__"] or ""
    assert doc  # the module explains the split; the constant explains the fork
    source = round_evidence.MEASURED_BENEFIT_MARGIN_DB
    assert isinstance(source, float)


# --------------------------------------------------------------------------- #
# 6. the round, composed — evaluate_round and the receipt
# --------------------------------------------------------------------------- #


def _integrity(*, failed=(), not_evaluated=()):
    """A CaptureIntegrity-shaped double: the two properties the rule reads."""
    return SimpleNamespace(failed=tuple(failed), not_evaluated=tuple(not_evaluated))


def _post(*, tracking_db=1.0, integrity=..., program_id="prog-a"):
    """A post-apply VERIFY analysis double."""
    hz = _grid()
    db = np.sin(np.log10(np.maximum(hz, 1.0)) * 5.0)
    return SimpleNamespace(
        program_id=program_id,
        summed_response=SimpleNamespace(
            freqs_hz=hz, magnitude_db=db, validity_floor_hz=None
        ),
        capture_integrity=_integrity() if integrity is ... else integrity,
        verify_tracking={"max_db_notch_excluded": tracking_db},
    )


def _flatter(analysis, *, factor: float):
    """The same capture with its deviation scaled — flatter below 1.0.

    Scaling the deviation is what moves the pooled residual; a constant offset
    would not, because the evaluator normalizes each curve to its own
    reference band. So the improved/regressed fixtures below differ in the
    quantity actually being graded, not in level.
    """
    hz = np.asarray(analysis.summed_response.freqs_hz, dtype=float)
    db = np.asarray(analysis.summed_response.magnitude_db, dtype=float) * factor
    return SimpleNamespace(
        program_id=analysis.program_id,
        summed_response=SimpleNamespace(
            freqs_hz=hz, magnitude_db=db, validity_floor_hz=None
        ),
        capture_integrity=analysis.capture_integrity,
        verify_tracking=analysis.verify_tracking,
    )


def _baseline_from(analysis, *, graph_fingerprint="entry-graph") -> EntryBaseline:
    reduced = measured_response_from_analysis(analysis, reference_mark=_MARK)
    assert reduced is not None
    return EntryBaseline.from_measurement(
        reduced,
        graph_fingerprint=graph_fingerprint,
        captured_at="2026-08-11T00:00:00Z",
        artifact_ref="entry_baseline_a01",
    )


def _round(post, baseline, **overrides):
    kwargs = {
        "post_analysis": post,
        "entry_baseline": baseline,
        "spec_report": None,
        "tracking": getattr(post, "verify_tracking", None),
        "realization_tolerance_db": 1.5,
        "reference_mark": _MARK,
        "boosted": False,
        "rollback_available": True,
    }
    kwargs.update(overrides)
    return round_evidence.evaluate_round(**kwargs)


def test_an_unusable_capture_short_circuits_and_the_other_three_say_why():
    """Not computed and discarded — not computed at all.

    ``verification_result`` already collapses the statuses, so a version that
    graded all four and then overwrote three would produce the same CONTRACT.
    What it would not produce is honest LOGGING: the discarded verdicts would
    still be carrying numbers, and the journal would show a benefit that no
    usable capture supports. The reason strings are the discriminator.
    """
    from jasper.active_speaker.crossover_v2.verification import (
        CAPTURE_INTEGRITY_FAILED,
    )

    post = _post(integrity=_integrity(failed=("glitch",)))
    evaluation = _round(post, _baseline_from(_post()))

    assert evaluation.capture.reason == CAPTURE_INTEGRITY_FAILED
    assert evaluation.realization.reason == CAPTURE_INTEGRITY_FAILED
    assert evaluation.benefit.reason == CAPTURE_INTEGRITY_FAILED
    assert evaluation.spec.reason == CAPTURE_INTEGRITY_FAILED
    assert evaluation.post_residual_db is None
    assert evaluation.adoption.outcome is not AdoptionOutcome.KEEP


def test_a_model_tracking_pass_does_not_override_a_measured_regression():
    """The 2026-08-10 shape, end to end through the composition.

    Tracking well inside its 1.5 dB tolerance, and a speaker that measurably
    got worse. The shipped build called that passed; the table calls it a
    restore, and this asserts the composition really routes both statuses in
    rather than short-circuiting on the tracking answer.
    """

    flat_baseline = _baseline_from(_flatter(_post(), factor=0.2))
    worse_post = _flatter(_post(tracking_db=0.4), factor=2.0)

    evaluation = _round(worse_post, flat_baseline)

    assert evaluation.realization.status is RealizationStatus.MATCHED
    assert evaluation.benefit.status is BenefitStatus.REGRESSED
    assert evaluation.adoption.outcome is AdoptionOutcome.RESTORE


def _failing_spec_report(analysis):
    from jasper.active_speaker.flat_spec import evaluate_flat_spec

    hz = np.asarray(analysis.summed_response.freqs_hz, dtype=float)
    report = evaluate_flat_spec(
        hz,
        np.asarray(analysis.summed_response.magnitude_db, dtype=float) * 8.0,
        np.zeros(hz.size, dtype=bool),
    )
    assert report.overall_passed is False
    return report


def test_realized_and_improved_keeps_even_with_a_failing_spec():
    """"Improved and still out of spec" is a valid keep — #2160's boundary.

    The post-cloud spec verdict is wired in as an ANSWER, not a gate. If it
    were a gate, a first pass that honestly moved the speaker forward would be
    thrown away for not being perfect.

    #2537 left this cell exactly where it was — a realized, improved round is
    still a plain ``KEEP`` — and put the failing band on the receipt as the next
    round's target beside it.

    **AMENDED BY #2602.** The graph is still kept, which is the whole claim of
    this test and of #2160's boundary. What changed is that this round no
    longer *ends the series*: the report it failed against still measures real
    tilt and ripple, so the fourth axis says a flatter result is reachable and
    the round lands on row 6 instead of row 1. The assertion is written as
    "keeps, and specifically keeps-while-iterating" rather than loosened to
    "some keeping outcome", so a regression that stopped iterating here would
    still fail.
    """

    rough_baseline = _baseline_from(_flatter(_post(), factor=6.0))
    better_post = _flatter(_post(tracking_db=0.4), factor=1.0)

    evaluation = _round(
        better_post, rough_baseline, spec_report=_failing_spec_report(better_post)
    )

    assert evaluation.benefit.status is BenefitStatus.IMPROVED
    assert evaluation.spec.status is SpecStatus.FAILED
    assert evaluation.quality.status is QualityStatus.PASSED
    assert evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert evaluation.adoption.row == ADOPTION_ROW_KEEP_ITERATING
    assert any(
        target.startswith("spec:")
        for target in evaluation.quality.evidence["targets"]
    )


def test_the_acoustic_grade_survives_a_round_with_no_comparable_baseline():
    """The attempts ledger and the benefit verdict are different questions.

    The loop differences consecutive ATTEMPTS — its kernel reads only the last
    two entries on purpose — so a round with no "before" still has an honest
    absolute grade to bank. Pinning them apart is what stops a missing
    baseline from silently blanking the ledger.
    """
    evaluation = _round(_post(), None)

    assert evaluation.benefit.status is BenefitStatus.INDETERMINATE
    assert evaluation.benefit.reason == BENEFIT_BASELINE_UNAVAILABLE
    assert evaluation.post_residual_db is not None
    assert evaluation.post_residual_bins is not None
    assert evaluation.post_residual_db > 0.0


def test_the_acoustic_grade_is_the_number_the_benefit_verdict_used():
    """One computation, two readers — never two that can disagree."""
    baseline = _baseline_from(_flatter(_post(), factor=3.0))

    evaluation = _round(_post(), baseline)

    assert evaluation.benefit.evidence["post_residual_db"] == pytest.approx(
        evaluation.post_residual_db
    )
    assert evaluation.benefit.evidence["n_bins"] == evaluation.post_residual_bins


def test_a_restore_the_host_cannot_perform_escalates_it_never_keeps():
    """No anchor plus a restore verdict is ``recovery_required``, loudly."""

    flat_baseline = _baseline_from(_flatter(_post(), factor=0.2))
    worse_post = _flatter(_post(tracking_db=0.4), factor=2.0)

    evaluation = _round(worse_post, flat_baseline, rollback_available=False)

    assert evaluation.benefit.status is BenefitStatus.REGRESSED
    assert evaluation.adoption.outcome is AdoptionOutcome.RECOVERY_REQUIRED
    assert evaluation.adoption.reason.startswith(
        "restore_required_without_rollback_anchor"
    )


def test_a_failed_restore_outranks_every_other_answer():

    rough_baseline = _baseline_from(_flatter(_post(), factor=6.0))
    better_post = _flatter(_post(tracking_db=0.4), factor=1.0)

    evaluation = _round(better_post, rough_baseline, restore_failed=True)

    # The evidence says keep; the speaker is in neither graph, so it does not.
    assert evaluation.benefit.status is BenefitStatus.IMPROVED
    assert evaluation.adoption.outcome is AdoptionOutcome.RECOVERY_REQUIRED


def test_an_unprovable_benefit_keeps_the_measured_graph_boosted_or_not():
    """#2537's correction, at the composed-round level.

    This test used to pin the opposite: a round with no comparable baseline
    RESTORED when the intervention carried a boost and asked the household when
    it did not. That is the rule that threw away the 2026-08-15 JTS3 cycle-4
    candidate — a usable capture, a tracked realization, a measured pooled
    residual of 0.915 dB, reverted to a state nobody had measured.

    An unprovable benefit is now a QUALITY unknown: the applied state WAS
    measured, so it stays and the missing comparison becomes a target. The
    boost modifier is invisible here because the evidence is trusted; it
    survives only on the untrusted row (pinned in
    ``tests/test_crossover_v2_verification.py``).
    """

    post = _post()

    boosted = _round(post, None, boosted=True)
    cut_only = _round(post, None, boosted=False)

    assert boosted.benefit.status is BenefitStatus.INDETERMINATE
    assert boosted.trust.status is EvidenceTrust.TRUSTED
    assert boosted.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert boosted.adoption.row == ADOPTION_ROW_KEEP_FOR_ITERATION
    # Boosted and cut-only agree completely: the modifier reads nothing here.
    assert cut_only.adoption == boosted.adoption
    assert any(
        target.startswith("benefit:")
        for target in boosted.quality.evidence["targets"]
    )


def test_an_unusable_capture_still_fails_a_boost_closed():
    """The one place ``unproven_boost_failed_closed`` survives (#2537).

    With no usable capture there is no measurement of the applied state at all,
    so the pre-#2537 sentence still holds verbatim: a boost whose benefit we
    cannot show is energy we put into a driver and cannot justify.
    """

    unusable = _post(integrity=_integrity(failed=("clipped_run",)))
    boosted = _round(unusable, None, boosted=True)

    assert boosted.capture.status is CaptureValidity.UNUSABLE
    assert boosted.trust.status is EvidenceTrust.UNTRUSTED
    assert boosted.adoption.outcome is AdoptionOutcome.RESTORE


def test_the_spec_answer_never_changes_whether_the_graph_is_kept():
    """Spec is "any" in every row of the issue's table; pinned by permutation.

    #2160's wire adds an answer to the receipt. If it ever adds a gate, that
    is a table change with evidence attached — and this test is what makes
    that a deliberate act rather than a quiet one.

    **AMENDED BY #2602, and narrowed to the half that is actually the
    invariant.** This used to assert ``len(outcomes) == 1`` and
    ``len(rows) == 1``. It cannot, now, and the reason is the ruling itself:
    the fourth axis reads the post-apply report's measured tilt and ripple to
    decide whether another round is worth running, so a report with 2 dB of
    tilt left and a report that is already flat MUST reach different rows.
    Demanding one row would be demanding that the series cannot iterate toward
    flat, which is the behaviour #2602 exists to add.

    What was always the point of the pin — and what is asserted below,
    unweakened — is that **spec cannot take a graph off a speaker**. Every
    permutation still lands on a keeping outcome, none restores, and none
    escalates. That is the "improved and still out of spec is an honest first
    pass" guarantee, and it does not depend on the row being constant.

    Two further protections stay in force, neither of them this test's:

    * The spec VERDICT still moves nothing. ``evaluate_round_quality``'s own
      permutation pin (``test_the_spec_answer_never_changes_the_quality``, in
      the verification suite) is untouched — ``decide_adoption`` never reads
      ``SpecStatus``, and the fourth axis reads measured dB rather than a
      pass/fail against a tolerance row.
    * #2537's second reason for the pin — that spec numbers were computed with
      no intersection against the session's trusted floor, so a decision keyed
      on them would inherit a gate-length term — **was a precondition, and it
      has since been met.** #2551 landed that intersection (``BandResult``
      carries ``graded_lo_hz``; ``FlatSpecReport`` carries
      ``trusted_floor_hz``), which is what makes an axis reading these numbers
      admissible now and would not have then.
    """
    from jasper.active_speaker.flat_spec import evaluate_flat_spec

    rough_baseline = _baseline_from(_flatter(_post(), factor=6.0))
    better_post = _flatter(_post(tracking_db=0.4), factor=1.0)
    hz = np.asarray(better_post.summed_response.freqs_hz, dtype=float)
    reports = [
        None,
        _failing_spec_report(better_post),
        evaluate_flat_spec(hz, np.zeros(hz.size), np.zeros(hz.size, dtype=bool)),
    ]

    evaluations = [
        _round(better_post, rough_baseline, spec_report=report)
        for report in reports
    ]
    outcomes = {evaluation.adoption.outcome for evaluation in evaluations}
    rows = {evaluation.adoption.row for evaluation in evaluations}
    statuses = {evaluation.spec.status for evaluation in evaluations}

    assert outcomes <= {
        AdoptionOutcome.KEEP, AdoptionOutcome.KEEP_FOR_ITERATION
    }, "spec must never take the graph off the speaker"
    assert not rows & {
        ADOPTION_ROW_RESTORE_UNSAFE,
        ADOPTION_ROW_RESTORE_UNTRUSTED,
        ADOPTION_ROW_RESTORE_REGRESSION,
        ADOPTION_ROW_RESTORE_FAILED,
    }, "…nor route a permutation onto a restoring row"
    assert len(statuses) == 3, "…while still producing three different answers"
    # And the rows that DO differ differ only in whether another round runs —
    # both of them keep. Without this, "outcomes are all keeping" could be
    # satisfied by a table that had quietly stopped distinguishing them.
    assert rows <= {ADOPTION_ROW_KEEP, ADOPTION_ROW_KEEP_ITERATING}
    # Not inert, though: it reaches the receipt as a target for the next round.
    failing = next(
        evaluation for evaluation in evaluations
        if evaluation.spec.status is SpecStatus.FAILED
    )
    assert any(
        target.startswith("spec:")
        for target in failing.quality.evidence["targets"]
    )


def _receipt_kwargs(evaluation, baseline, **overrides):
    kwargs = dict(
        round_id="cap_round_1",
        evaluation=evaluation,
        entry_baseline=baseline,
        entry_graph_fingerprint="entry-graph",
        rollback_anchor={"candidate_fingerprint": "anchor-fp"},
        proposal_fingerprint="proposal-fp",
        proposal_fingerprint_kind="intervention_proposal",
        applied_graph_fingerprint="applied-fp",
        post_measurement={"program_id": "prog-a"},
        restore_result=None,
        evidence_identities={"commanded_delta": "delta-fp"},
        created_at="2026-08-11T00:05:00Z",
    )
    kwargs.update(overrides)
    return kwargs


def test_the_receipt_carries_the_baselines_identity_not_its_curve():
    """A receipt is a record, not a second copy of the measurement.

    The curve already has two owners that outlive this record (the retained
    capture and the durable ``verify_priors``). Copying it here would make the
    receipt large and tie its fingerprint to a decimation choice.
    """
    baseline = _baseline_from(_post())
    evaluation = _round(_post(), baseline)

    receipt = round_evidence.build_round_receipt(
        **_receipt_kwargs(evaluation, baseline)
    )

    assert receipt.entry_baseline["program_id"] == baseline.program_id
    assert receipt.entry_baseline["artifact_ref"] == "entry_baseline_a01"
    assert receipt.entry_baseline["n_bins"] == len(baseline.curve.hz)
    assert "freqs_hz" not in receipt.entry_baseline
    assert "magnitude_db" not in receipt.entry_baseline
    assert receipt.verification is evaluation.result
    assert receipt.adoption is evaluation.adoption
    assert receipt.fingerprint


def test_the_receipt_fingerprint_moves_when_a_committed_value_moves():
    """Otherwise it is a checksum of nothing in particular."""
    baseline = _baseline_from(_post())
    evaluation = _round(_post(), baseline)
    base = _receipt_kwargs(evaluation, baseline)

    reference = round_evidence.build_round_receipt(**base).fingerprint
    assert round_evidence.build_round_receipt(**base).fingerprint == reference

    for field, changed in (
        ("round_id", "cap_round_2"),
        ("entry_graph_fingerprint", "other-graph"),
        ("proposal_fingerprint", "other-proposal"),
        # #2392: the SAME digest under the other regime is a different receipt.
        # Without this row the marker could ride outside the fingerprint and a
        # banked receipt could be relabelled without its digest noticing.
        ("proposal_fingerprint_kind", "candidate"),
        ("applied_graph_fingerprint", "other-applied"),
        ("rollback_anchor", {"candidate_fingerprint": "other-anchor"}),
        ("post_measurement", {"program_id": "prog-b"}),
        ("evidence_identities", {"commanded_delta": "other-delta"}),
        ("created_at", "2026-08-11T00:06:00Z"),
    ):
        moved = _receipt_kwargs(evaluation, baseline, **{field: changed})
        assert (
            round_evidence.build_round_receipt(**moved).fingerprint != reference
        ), f"{field} is committed and must reach the fingerprint"


def test_a_recovery_required_round_must_record_what_the_restore_did():
    """The contract's own invariant, reached through the assembler.

    A recovery that cannot say what the restore did is not a receipt — and
    the assembler must not be a way around that.
    """

    flat_baseline = _baseline_from(_flatter(_post(), factor=0.2))
    worse_post = _flatter(_post(tracking_db=0.4), factor=2.0)
    evaluation = _round(worse_post, flat_baseline, restore_failed=True)

    with pytest.raises(CrossoverV2ContractError):
        round_evidence.build_round_receipt(
            **_receipt_kwargs(evaluation, flat_baseline, restore_result=None)
        )

    assert round_evidence.build_round_receipt(
        **_receipt_kwargs(
            evaluation,
            flat_baseline,
            restore_result={"restored": False, "error": "socket"},
        )
    ).fingerprint


def test_the_margin_is_positive_and_the_evaluator_accepts_it():
    """A margin the evaluator would refuse is a margin nothing can use.

    ``evaluate_benefit`` validates ``margin_db`` through the contracts
    module's positive-dB rule, so this both pins the sign and proves the two
    modules agree about what a margin is.
    """
    side = measured_response_from_analysis(_analysis(), reference_mark=_MARK)
    assert side is not None
    baseline, post = benefit_comparands(baseline=side, post=side)

    verdict = evaluate_benefit(
        entry_baseline=baseline, post=post, margin_db=MEASURED_BENEFIT_MARGIN_DB
    )

    assert MEASURED_BENEFIT_MARGIN_DB > 0.0
    # The same capture on both sides: exactly zero improvement, which is
    # inside any positive margin — so the round declines to claim a change it
    # did not measure, and the margin it declined against is disclosed.
    assert verdict.status is BenefitStatus.INDETERMINATE
    assert verdict.evidence["improvement_db"] == pytest.approx(0.0)
    assert verdict.evidence["margin_db"] == pytest.approx(MEASURED_BENEFIT_MARGIN_DB)
