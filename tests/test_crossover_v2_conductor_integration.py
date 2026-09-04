# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: the integration reorder, driver linearization, and spec-grading the prediction."""

from __future__ import annotations

import dataclasses
import logging
import types
import numpy as np
import pytest
from dataclasses import replace
from jasper.active_speaker.crossover_v2 import (
    intervention as iv,
    planning,
)
from jasper.active_speaker.crossover_v2_flow import (
    PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    _analysis_json,
    spec_report_for_predicted_sum,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY,
    REASON_CAPTURE_TIMEOUT,
    TRANSIENT_AUTO_RETRY_CODES,
)
from jasper.audio_measurement.program_analysis import (
    CrossoverCandidate,
    DriftEstimate,
    ProgramAnalysis,
    realized_branch_level_match,
    solve_branch_trims,
)
from jasper.active_speaker.flat_spec import (
    evaluate_flat_spec,
    spec_convergence_residual,
)
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginRefused
from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    _DIAG_LOGGER,
    _FIXTURE_RAW_TRIM_DB,
    _LINEARIZABLE_FREQS_HZ,
    _ROOM_SCALE_EXPECTED_RMS_DB,
    _SUMMED_FREQS_HZ,
    _alignment,
    _cloud_conductor,
    _conductor,
    _eligible_measure_analysis,
    _fixture_branch_db,
    _gate_residuals,
    _in_room_summed_db,
    _measure_analysis,
    _one_sided_conductor,
    _run_phase,
    _solve_fixture_raw_trim,
    _verify_analysis,
    _walk_measure_cloud_to_close,
)


# --- conductor integration reorder ------------------------------------------


@pytest.mark.parametrize("woofer_level_db,tweeter_level_db,expected_trim", [
    # The tweeter is louder, so IT is the one attenuated. This direction
    # already worked even under the original hardcoded-woofer-0.0 helper,
    # because the fixture's one shipped pair always happened to have the
    # quieter woofer.
    (0.0, 20.0, {"woofer": 0.0, "tweeter": -20.0}),
    # #1938 gate follow-up (SF-1): the direction that was SILENTLY BROKEN by
    # the woofer-trim hardcode. The woofer is louder here, so the WOOFER must
    # be the one attenuated — but `_solve_fixture_raw_trim` used to return
    # {"woofer": 0.0, "tweeter": round(trim_t, 3)} unconditionally, and for a
    # louder woofer the solved `trim_t` is itself 0.0 (the tweeter needs no
    # attenuation), so the whole dict silently came back {0.0, 0.0} — a no-op
    # that left both branches at their original, still-mismatched levels.
    (20.0, 0.0, {"woofer": -20.0, "tweeter": 0.0}),
])
def test_eligible_measure_analysis_derives_trim_from_its_own_custom_curves(
    woofer_level_db, tweeter_level_db, expected_trim,
):
    """#1938 regression guard, both directions.

    A caller handing ``_eligible_measure_analysis`` CUSTOM ``woofer_db``/
    ``tweeter_db`` curves, with no explicit ``trim_db``, must get a trim
    SOLVED from those curves — never the module constant
    ``_FIXTURE_RAW_TRIM_DB``, which is solved from the DEFAULT curves and is a
    different pair. That silent fallback is the "one speaker's branches,
    another speaker's trim" incoherence :func:`_solve_fixture_raw_trim`'s own
    docstring documents for the default curves, reintroduced through the
    custom-curve parameters (#1938's finding, discovered via
    ``test_prediction_gate_logs_the_improved_path_with_both_terms`` /
    PR #1934 and the two call sites this issue's fix corrected —
    ``test_linearized_ripple_polish_is_skipped_on_a_one_sided_band`` and
    ``test_prediction_gate_refuses_a_correction_that_does_not_improve``).

    Two FLAT curves 20 dB apart, in each direction, make the expected trim a
    closed form — attenuate whichever branch is louder by exactly the gap —
    rather than a number this test would have to take on faith from the
    solver under test.
    """
    freqs = _LINEARIZABLE_FREQS_HZ
    flat_woofer_db = np.full_like(freqs, woofer_level_db)
    flat_tweeter_db = np.full_like(freqs, tweeter_level_db)
    program = types.SimpleNamespace(program_id="fixture_trim_guard")

    analysis = _eligible_measure_analysis(
        program, woofer_db=flat_woofer_db, tweeter_db=flat_tweeter_db,
    )

    assert analysis.candidate.trim_db == expected_trim
    # Not the default-curve constant: the regression this guards against is a
    # fixture that silently returns it regardless of the curves it was
    # actually handed.
    assert analysis.candidate.trim_db != dict(_FIXTURE_RAW_TRIM_DB)
    # _eligible_measure_analysis defaults trim_band_average_db to trim_db
    # when omitted, so it must agree too — a caller reading either field
    # sees the same coherent trim.
    assert analysis.candidate.trim_band_average_db == analysis.candidate.trim_db


def test_non_reference_tier_falls_back_byte_identical_to_trims_only():
    """mic_tier != 'reference' — even with a paired N>=3 both drivers —
    must take the EXACT same path as before this PR: raw trim, empty
    linearization dict."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier="consumer")
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}


def test_reference_tier_but_under_repeated_falls_back_byte_identical():
    """Reference-tier mic but the tweeter has only 1 occurrence (< the
    paired-N gate) — must still fall back, byte-identical."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="reference", tweeter_repeats=0,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}


def test_reference_tier_missing_mic_tier_none_falls_back():
    """mic_tier=None (the field's own default — a legacy/unset analysis)
    must resolve to ineligible, never crash on the `!= "reference"`
    comparison."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier=None)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization == {}


def test_eligible_candidate_fits_both_roles_and_moves_trim_toward_ripple_optimal():
    """The asymmetric-overlap fixture (PR-C offline-validated numbers): a
    tweeter bump squarely inside the crossover overlap band gets fitted
    and corrected, and the re-solved trim moves measurably away from the
    raw (uncorrected) solve — toward what the ACTUAL (linearized) branch
    responses justify, not the raw band-average bias #1667 named."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    candidate = c.candidate
    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    assert candidate.role_attenuations_db != raw_trim
    # The bump correction quiets the tweeter's overlap-band level, so the
    # RESOLVED tweeter trim needs LESS attenuation than the raw solve did
    # (moves toward 0, i.e. strictly greater than the raw fixture trim).
    assert candidate.role_attenuations_db["tweeter"] > raw_trim["tweeter"]

    assert set(candidate.linearization) == {"woofer", "tweeter"}
    tweeter_fit = candidate.linearization["tweeter"]
    assert tweeter_fit["filters"], "expected the tweeter bump to attract a filter"
    assert all(f["gain"] <= 0.0 for f in tweeter_fit["filters"])
    for role_fit in candidate.linearization.values():
        assert role_fit["mic_tier"] == "reference"
        assert role_fit["n_repeats"] == 2
        # This test passes no driver_class_by_role override, so every role
        # fits under the ctor's conservative "unknown" default. A production
        # caller now exists (#1665's resolve_conductor_context — see
        # test_declared_driver_class_reaches_the_compose_envelope_seam
        # below); this test is deliberately about the no-override path.
        assert role_fit["driver_class"] == "unknown"


def _measured_level_frame(conductor, *, woofer_db=None, tweeter_db=None):
    """The trim's OWN level frame, re-measured by the test from published inputs.

    The anchor's give-back is measured over ``branch_level_bands_hz`` — the
    same estimator, averaging domain and half-bands that solved ``raw_trim_db``
    and that grade the committed pair — because a give-back spent against a
    trim has to be measured in that trim's frame. A give-back read over each
    driver's own CORE band (``LinearizationFit.correction_giveback_db``)
    answers a different question, and its per-role DIFFERENCE lands as pure
    inter-driver level error: on the jts3 horn tweeter, 2026-08-19, that was
    3.67 dB of hot tweeter.

    **Every input is sourced independently of the planner**, which is what
    stops this being a restatement of ``plan_linearization``'s own arithmetic:
    the spans come from the conductor's OWN MEASURE program, the
    pre-correction pair is the fixture's own declared branch curves, and the
    post-correction pair is those curves times the correction the candidate
    PUBLISHES. Nothing is read back out of the planner, so a change of band,
    of estimator, of sign, or of which pair is pre and which is post fails
    here rather than being absorbed.

    Returns the frame as a namespace: ``giveback_db`` (per role),
    ``linearized`` (the post-correction pair), ``spans``, and ``freqs``.
    """
    from jasper.active_speaker.linearization_fit import (
        LinearizationFilter,
        complex_correction_response,
    )

    default_woofer_db, default_tweeter_db = _fixture_branch_db()
    curves = {
        "woofer": default_woofer_db if woofer_db is None else woofer_db,
        "tweeter": default_tweeter_db if tweeter_db is None else tweeter_db,
    }
    freqs = _LINEARIZABLE_FREQS_HZ
    program = conductor.program_for_phase(PHASE_MEASURE)
    spans = {
        role: (program.segment(seg).f1_hz, program.segment(seg).f2_hz)
        for role, seg in (("woofer", "sweep_w"), ("tweeter", "sweep_t"))
    }
    raw = {
        role: (10.0 ** (np.asarray(curve) / 20.0)).astype(complex)
        for role, curve in curves.items()
    }
    linearized = {
        role: raw[role] * complex_correction_response(
            [
                LinearizationFilter(**f)
                for f in conductor.candidate.linearization[role]["filters"]
            ],
            freqs,
        )
        for role in ("woofer", "tweeter")
    }

    def _levels(pair):
        _residual_w, _residual_t, level_w, level_t = solve_branch_trims(
            freqs, pair["woofer"], pair["tweeter"], FC_HZ,
            woofer_span_hz=spans["woofer"], tweeter_span_hz=spans["tweeter"],
        )
        return {"woofer": level_w, "tweeter": level_t}

    before, after = _levels(raw), _levels(linearized)
    return types.SimpleNamespace(
        freqs=freqs,
        spans=spans,
        linearized=linearized,
        giveback_db={
            role: before[role] - after[role] for role in ("woofer", "tweeter")
        },
    )


def _inter_driver_level_error_db(frame, trim_db):
    """One trim pair's REALIZED inter-driver level error on the linearized pair.

    The anchor's defining property, and the one the band-matched give-back
    buys: ``raw_trim`` level-matches the PRE-correction pair, and adding back
    exactly what the correction removed FROM THAT SAME BAND puts the
    POST-correction pair at the same handoff level. The residual is therefore
    not "close to zero" by luck — it is zero up to the 3-decimal rounding
    ``_solve_fixture_raw_trim`` applies to the fixture's own raw trim, which
    bounds it at 1e-3 dB.
    """
    return realized_branch_level_match(
        frame.freqs, frame.linearized["woofer"], frame.linearized["tweeter"],
        FC_HZ,
        trim_w_db=trim_db["woofer"], trim_t_db=trim_db["tweeter"],
        woofer_span_hz=frame.spans["woofer"],
        tweeter_span_hz=frame.spans["tweeter"],
    ).difference_db


def test_fit_linearization_wires_ripple_optimal_seeded_by_anchored_giveback(
    monkeypatch,
):
    """#1668 anchored give-back: `_fit_linearization`'s ripple fine-tune must be
    seeded by the ANCHORED trim — each branch's own raw candidate trim plus the
    level its emitted cascade removed, normalized non-positive — NOT the old
    `solve_branch_trims` OVERLAP-band average on the linearized pair (which
    under-returned the give-back on the live JTS3 runs). Spies on the
    module-level imported name to pin that the call happened exactly once, with
    the anchored woofer trim held fixed and the analysis's own polarity sign
    passed through.

    **Which band that give-back is read in moved, and the expectation moved
    with it.** It used to be `LinearizationFit.correction_giveback_db`, a power
    mean over each driver's own CORE band. It is now measured over
    ``branch_level_bands_hz`` — the bands that solved the raw trim and that
    grade the committed pair — so the give-back is spent in the frame it was
    measured in. The old overlap-band objection does not carry to those bands:
    PR-L3 deleted the shared overlap frame, and each branch is now read only on
    its own side of Fc. ``_measured_level_frame`` re-measures the new give-back
    from the fixture's own curves and the candidate's PUBLISHED filters, so the
    expectation below is derived rather than transcribed."""

    calls = []
    real_solve = iv.solve_ripple_optimal_trim

    def _spy(*args, **kwargs):
        # Positional call shape: solve_ripple_optimal_trim(freqs, w_tf,
        # t_tf, fc_hz, *, lo_hz=..., hi_hz=..., seed_trim_db=...,
        # trim_w_db=..., sign=...) -- _fit_linearization passes the first
        # four positionally, the rest by keyword.
        freqs, w_tf, t_tf, fc_hz = args
        calls.append({"freqs": freqs, "w_tf": w_tf, "t_tf": t_tf, "fc_hz": fc_hz, **kwargs})
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    assert len(calls) == 1
    call = calls[0]
    assert call["fc_hz"] == FC_HZ
    assert call["sign"] == 1  # _alignment()'s default polarity="normal"
    # Anchored seed = the anchor's BASE + that branch's own measured give-back,
    # with the shared non-positive normalization shift applied to both roles.
    #
    # The single-datum-owner migration (#2609) deleted the two-voter frame that
    # used to add a reconciled offset to this same anchor. What is left is the
    # one design SSOT — docs/active-speaker-tuning-layers-design.md, "Anchored
    # give-back (the trim)": the committed RAW trim plus that branch's own
    # measured give-back, shared-shift normalized non-positive, no third term.
    #
    # The give-back is re-measured here in the TRIM'S OWN FRAME — see
    # ``_measured_level_frame`` — rather than read off the fit's core-band
    # number, because that is the band the anchor now spends it in.
    base = dict(_FIXTURE_RAW_TRIM_DB)
    frame = _measured_level_frame(c)
    giveback = frame.giveback_db
    unnormalized = {
        r: base[r] + giveback[r] for r in ("woofer", "tweeter")
    }
    shift = max(0.0, max(unnormalized.values()))
    expected_anchored = {r: v - shift for r, v in unnormalized.items()}
    assert call["trim_w_db"] == pytest.approx(expected_anchored["woofer"])
    assert call["seed_trim_db"] == pytest.approx(expected_anchored["tweeter"])
    # …and the property that band-matching buys, asserted independently of the
    # arithmetic above: the seeded pair hands the two linearized branches off
    # at the SAME level. A give-back read in any other band leaves
    # ``giveback_t - giveback_w`` of inter-driver error here instead.
    assert abs(_inter_driver_level_error_db(frame, {
        "woofer": call["trim_w_db"], "tweeter": call["seed_trim_db"],
    })) <= 1e-3

    # What ships is one of the TWO pairs `_fit_linearization` grades — the
    # anchor, or the scan's ripple polish — never the raw trim ("Never the RAW
    # trim, whichever pair wins"). WHICH of the two wins is the PR-L4 level
    # adjudication's business, not this test's: it commits whichever pair the
    # realized inter-driver level instrument scores better, and both branches of
    # that choice have their own pins (test_eligible_candidate_fits_both_roles_
    # and_moves_trim_toward_ripple_optimal for the polish, test_a_disagreeing_
    # frame_whose_realized_check_passes_banks_and_proceeds for the grading).
    #
    # Which pair this fixture lands on has moved more than once, each move
    # worth recording rather than papering over. R10b (panel CC-2(b)) made the
    # fit's `correction_giveback_db` grade the REALIZED biquad cascade instead
    # of `predicted_response`'s Lorentzian, which moved this pair's anchor by
    # +0.124 dB (tweeter -1.383 -> -1.260). BOTH graded pairs moved with it (the
    # polish is seeded from the anchor), in opposite directions: the anchor's
    # realized level error |-0.258| -> |-0.134| dB, the polish's |0.142| ->
    # |0.166| dB. That is what crossed them over. No filter moved.
    #
    # #2106 then collapsed the two pairs into one: the boost the ruling permits
    # (+3.72 dB at 399 Hz on the woofer here) reshapes the linearized branches
    # whose SUMMED ripple the scan minimizes, and for a while the minimum sat
    # exactly on the anchored seed, so the scan's walk was 0.000 dB.
    #
    # Moving the give-back into the trim's own band moved the seed again, and
    # the two pairs are two numbers once more: the scan walks +0.300 dB off the
    # anchor. The adjudication then commits the ANCHOR, and by the mechanism
    # this whole change is about — the band-matched give-back leaves the
    # anchored pair at a realized inter-driver level error of 0.000 dB (the
    # assertion above), against the scan's 0.300 dB, so the level instrument
    # scores the anchor better outright. Asserted below.
    resolved_trim_t, _ripple, _seed = real_solve(
        call["freqs"], call["w_tf"], call["t_tf"], FC_HZ,
        lo_hz=call["lo_hz"], hi_hz=call["hi_hz"],
        seed_trim_db=call["seed_trim_db"], trim_w_db=call["trim_w_db"],
        sign=call["sign"],
    )
    committed_t = c.candidate.role_attenuations_db["tweeter"]
    # The durable invariant, asserted first because it holds whichever way the
    # adjudication goes and on every fixture: what ships is a graded pair, and
    # the raw trim is not one of them.
    assert committed_t in (
        pytest.approx(expected_anchored["tweeter"]),
        pytest.approx(resolved_trim_t),
    )
    assert committed_t != pytest.approx(_FIXTURE_RAW_TRIM_DB["tweeter"])
    # …and the fixture-specific outcome, stated precisely rather than hedged, so
    # a future flip back is visible here rather than silent. The two pairs are
    # genuinely two numbers again (the scan walks +0.300 dB off its seed), so
    # this equality does discriminate between them: what ships is the anchor.
    #
    # WHICH pair the adjudication would pick when they DO differ is not this
    # test's claim and never was (see the paragraph above); its own pins are
    # `test_wild_trim_fallback_follows_levels_not_drift` and
    # `test_healthy_drivers_whose_declared_bands_cross_fc_are_not_refused`.
    # (NOT `test_eligible_candidate_fits_both_roles_and_moves_trim_toward_
    # ripple_optimal`, which #2138's review showed stays green when the
    # adjudication is severed.) What this test still pins, and what stays
    # is #1668's subject: the scan is SEEDED by the anchored give-back
    # (asserted on `seed_trim_db`/`trim_w_db` above) and what ships is never
    # the raw trim.
    #
    # This equality has been written both ways as the fixture moved: against
    # the anchor while the two pairs coincided, then against the scan after
    # deleting PR-L5's offset moved the anchor ~2.2 dB and the level
    # adjudication preferred the polish. Measuring the give-back in the trim's
    # own band moves it back to the anchor, because that give-back is what
    # makes the anchored pair the level-matched one. That is the adjudication
    # working, not a regression — and per the paragraph above, WHICH pair wins
    # is explicitly not this test's claim. What is asserted is the claim it
    # does make.
    assert committed_t == pytest.approx(expected_anchored["tweeter"])
    assert committed_t != pytest.approx(resolved_trim_t)
    assert committed_t != pytest.approx(_FIXTURE_RAW_TRIM_DB["tweeter"])
    assert committed_t in (
        pytest.approx(expected_anchored["tweeter"]),
        pytest.approx(resolved_trim_t),
    )


def test_linearized_ripple_polish_is_skipped_on_a_one_sided_band(caplog, monkeypatch):
    """PR-L3 review S1: the LINEARIZED ripple fine-tune carries the same
    one-sided-band hazard `program_analysis._build_candidate` guards, reached
    through the same ``overlap_band_hz`` clamp — and THIS is the call site
    whose result becomes ``role_attenuations_db``, the gain the emitted graph
    runs. With the tweeter swept from Fc the band is ``[Fc, 2*Fc]``, where the
    woofer is deep in its skirt and the summed ripple cannot express the
    handoff level. The scan must not run at all; the anchored give-back
    stands, and the skip is disclosed.

    **The realized verdict is SUPPLIED, for the same reason the sibling tests
    below supply theirs.** This harness never captures a summed at-the-mark
    baseline, so ``anchor_trims`` (single-datum-owner migration, #2609) always
    falls back to the raw measured trim — there is no owner in hand to place
    the pair any other way, and no dispute mechanism left to move it. The
    realized-level check is what decides whether that anchor ships, and it is
    supplied directly here rather than provoked, because provoking it is not
    this test's subject: the subject is that a one-sided band skips the scan
    and leaves the anchor standing, which is upstream of every level gate and
    is measured identically regardless of what the realized check says. Same
    reasoning, and the same mechanism, as
    ``test_large_raw_shift_is_accepted_by_the_guard_and_refused_by_the_level_
    check``, which supplies its own verdict for the same reason.
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    calls = []
    monkeypatch.setattr(
        iv, "solve_ripple_optimal_trim",
        lambda *a, **kw: calls.append(kw) or (kw["seed_trim_db"] - 4.0, 0.0, kw["seed_trim_db"]),
    )

    def _matched(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=0.0, difference_db=0.0,
            tolerance_db=3.0, matched=True,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    monkeypatch.setattr(iv, "realized_level_match", _matched)
    fakes = FakeSeams()
    # A defect inside the tweeter's OWN swept band (this conductor sweeps the
    # tweeter from Fc up), so the fit has real work to do and the candidate
    # clears item 2's gate.
    #
    # **Why the override below is still here is an OPEN QUESTION (#2073) — it
    # is NOT what this comment used to say.** The original rationale read: "the
    # shared fixture's bump sits at 1500 Hz — below Fc, i.e. outside this
    # geometry's tweeter band — so the fit barely moves and the session is
    # (correctly) refused …" Both halves stopped being true when R10a moved
    # that bump to +3 dB at 2400 Hz, which is ABOVE this conductor's Fc of
    # 1600 Hz, so it is INSIDE the tweeter's band: driving this setup with the
    # shared fixture and no override returns accepted, with the ripple scan
    # still correctly skipped (measured 2026-08-02, at that same R10a
    # revision). The override is left in place rather than repaired
    # because deciding whether it still earns its keep — its 8 dB at 2500 Hz is
    # a deeper defect than the shared 3 dB, and the give-back arithmetic below
    # is derived from the one-sided curve — is a design call, not a
    # transcription fix. #2073 carries it.
    _one_sided_tweeter_db = 8.0 * np.exp(
        -0.5 * ((np.log2(_LINEARIZABLE_FREQS_HZ / 2500.0) / 0.3) ** 2)
    )
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, tweeter_db=_one_sided_tweeter_db,
    )
    c = _one_sided_conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    assert calls == []  # the scan never ran
    assert "event=correction.crossover_v2_linearization_ripple_trim_skipped" in caplog.text
    assert "reason=ripple_band_one_sided" in caplog.text
    # The applied trim is the anchored give-back, untouched by any scan.
    # #1938: the raw trim has to be derived from THIS fixture's own curves —
    # the default woofer paired with the one-sided tweeter above — not from
    # _FIXTURE_RAW_TRIM_DB, which is solved from the DEFAULT tweeter and is a
    # different pair. Before this fix, `_eligible_measure_analysis` silently
    # defaulted to that mismatched constant too, and this assertion agreed
    # with it only because both sides shared the same wrong number.
    default_woofer_db, _default_tweeter_db = _fixture_branch_db()
    raw_trim = _solve_fixture_raw_trim(default_woofer_db, _one_sided_tweeter_db)
    # The give-back is measured in the band the TRIM is read in — the same
    # estimator and half-bands that solved ``raw_trim`` above — not over each
    # driver's own core band. ``_measured_level_frame`` re-measures it from the
    # fixture's own curves and this candidate's PUBLISHED filters, so the
    # expectation is derived from the same physics the planner saw rather than
    # read back out of it.
    frame = _measured_level_frame(c, tweeter_db=_one_sided_tweeter_db)
    giveback = frame.giveback_db
    # No summed capture in hand (see the docstring), so ``anchor_trims``
    # (single-datum-owner migration, #2609) falls back unconditionally to the
    # raw measured trim: the anchor is ``raw_trim + giveback``, with no third
    # term to add or exclude.
    unnormalized = {r: raw_trim[r] + giveback[r] for r in ("woofer", "tweeter")}
    shift = max(0.0, max(unnormalized.values()))
    for role in ("woofer", "tweeter"):
        assert c.candidate.role_attenuations_db[role] == pytest.approx(
            unnormalized[role] - shift
        )
    # With no scan to move it, the pair that ships IS the anchor — so the
    # property the band-matched give-back buys is directly observable on the
    # emitted gains: the two linearized branches hand off at the same level.
    # This is computed here rather than read off the plan because the realized
    # verdict is supplied by the stub above.
    assert abs(_inter_driver_level_error_db(
        frame, dict(c.candidate.role_attenuations_db)
    )) <= 1e-3
    # The magnitude, as a coarse guard on the fixture itself. It was -7.960 dB
    # while the anchor spent the CORE-band give-back; measuring that give-back
    # in the trim's own band instead moves this horn-shaped tweeter's anchor to
    # -4.918 dB — the same direction and roughly the same size as the jts3
    # correction this change was made for.
    assert c.candidate.role_attenuations_db["tweeter"] == pytest.approx(
        -4.918, abs=0.02
    )
    # ...and the guard never fired, because the trim never left the anchor.
    assert (
        "event=correction.crossover_v2_linearization_trim_rejected" not in caplog.text
    )


def test_straddling_band_still_runs_the_linearized_ripple_polish(caplog):
    """The control for the test above: the DEFAULT fixture's tweeter is swept
    from 300 Hz, so its overlap band straddles Fc and the polish still runs —
    the guard keys on the band, not on 'linearization is happening'."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True
    assert (
        "event=correction.crossover_v2_linearization_ripple_trim_skipped"
        not in caplog.text
    )


def test_linearization_giveback_ledger_carries_both_target_levels(caplog):
    """PR-L3 review S5: the give-back line carries each role's own
    ``target_level_db`` — ``raw_trim_db`` should track the negated difference
    of the two, and a large disagreement is the signature of a level defect
    like the one that shipped the 10 dB-dark tweeter. Mirrors the
    ``branch_level_match`` ledger pinned in
    tests/test_audio_measurement_program_analysis.py."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert _run_phase(c, 2, 2)["accepted"] is True

    assert "event=correction.crossover_v2_linearization_giveback" in caplog.text
    line = next(
        text for text in caplog.text.splitlines()
        if "event=correction.crossover_v2_linearization_giveback" in text
    )
    assert "target_level_db=" in line
    for role in ("woofer", "tweeter"):
        expected = round(
            float(c.candidate.linearization[role]["target_level_db"]), 3
        )
        assert f"'{role}': {expected}" in line


def test_analysis_json_round_trips_trim_band_average_db():
    """#1667 evidence round-trip: `_analysis_json`'s frozen fingerprint
    carries `trim_band_average_db` alongside the applied `trim_db`, rounded
    the same way, so replay/forensics can always compare the two — even
    when the candidate predates this field (`None` passthrough)."""
    freqs = np.linspace(100.0, 20000.0, 64)
    cand = CrossoverCandidate(
        trim_db={"woofer": 0.0, "tweeter": -0.0754},
        polarity="normal", delay_us=150.0,
        predicted_ripple_db=0.03, confidence=0.9,
        trim_band_average_db={"woofer": 0.0, "tweeter": -9.4754},
    )
    analysis = ProgramAnalysis(
        phase="measure", program_id="p1", locations=(),
        drift=DriftEstimate(
            epsilon_ppm=1.0, max_residual_samples=0.0,
            glitch_detected=False,
        ),
        alignment=_alignment(), candidate=cand,
        predicted_sum=(freqs, np.zeros_like(freqs)),
        glitch_detected=False,
    )
    evidence = _analysis_json(analysis)
    assert evidence["trim_db"] == {"woofer": 0.0, "tweeter": -0.0754}
    assert evidence["trim_band_average_db"] == {"woofer": 0.0, "tweeter": -9.4754}

    # Legacy/pre-#1667 construction site: candidate has no evidence field.
    legacy_cand = CrossoverCandidate(
        trim_db={"woofer": 0.0, "tweeter": -2.211}, polarity="normal",
        delay_us=150.0, predicted_ripple_db=0.8, confidence=0.8,
    )
    legacy_analysis = replace(analysis, candidate=legacy_cand)
    legacy_evidence = _analysis_json(legacy_analysis)
    assert legacy_evidence["trim_db"] == {"woofer": 0.0, "tweeter": -2.211}
    assert legacy_evidence["trim_band_average_db"] is None


def test_measure_diag_logs_trim_ripple_gain_db(caplog):
    """#1667 observability: the measure_diag line carries the
    applied-vs-band-average delta for the tweeter trim -- 0.0 when the
    ripple-optimal search left the trim exactly at its seed (or the sanity
    guard fell back to it), the actual recovery amount otherwise. `None`
    only when the candidate predates trim_band_average_db."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: replace(
        _measure_analysis(program),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.1, "tweeter": -0.5},
            polarity="normal", delay_us=150.0,
            predicted_ripple_db=0.03, confidence=0.8,
            trim_band_average_db={"woofer": -3.1, "tweeter": -9.5},
        ),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "trim_ripple_gain_db=9.0" in caplog.text  # -0.5 - (-9.5)
    caplog.clear()

    # No band-average evidence on this candidate (legacy/test construction
    # site) -> None, never a guess.
    fakes2 = FakeSeams()
    fakes2.measure = lambda program: _measure_analysis(program)
    c2 = _conductor(fakes2)
    _run_phase(c2, 1, 1)
    verdict2 = _run_phase(c2, 2, 2)
    assert verdict2["accepted"] is True
    assert "trim_ripple_gain_db=null" in caplog.text


def test_driver_class_by_role_ctor_param_threads_into_the_fit():
    """The driver_class_by_role ctor param (default None -> every role
    "unknown") was #1668 PR-C's forward-looking seam for #1665's
    component-entry declarations. #1665 has since landed
    (jasper.web.correction_crossover_v2.resolve_conductor_context is the
    production caller); this test pins the ctor-level wiring with a
    hand-typed override, and
    test_declared_driver_class_reaches_the_compose_envelope_seam below closes
    the other half by driving this SAME param from the resolver's real
    output."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes, driver_class_by_role={"tweeter": "compression_horn"})
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization["tweeter"]["driver_class"] == "compression_horn"
    # The woofer wasn't named in the override -> stays "unknown".
    assert c.candidate.linearization["woofer"]["driver_class"] == "unknown"


def test_declared_driver_class_reaches_the_compose_envelope_seam():
    """#1665: a design draft's declared driver_class, resolved by the REAL
    production helper (jasper.web.correction_crossover_v2's
    _resolve_driver_class_by_role — not a hand-typed literal), reaches
    compose_envelope through the exact ctor param the sibling test above
    proved works. Closes the seam #1668 PR-C's own test left open (its
    docstring said "no production caller populates it yet")."""
    from jasper.active_speaker.crossover_v2.conductor_context import (
        _resolve_driver_class_by_role,
    )

    draft = {
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "model": "A"},
                {
                    "role": "tweeter",
                    "model": "B",
                    "driver_class": "compression_horn",
                },
            ],
            "crossover_candidates": [],
        },
    }
    driver_class_by_role = _resolve_driver_class_by_role(draft)
    assert driver_class_by_role == {"tweeter": "compression_horn"}

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes, driver_class_by_role=driver_class_by_role)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert c.candidate.linearization["tweeter"]["driver_class"] == "compression_horn"
    assert c.candidate.linearization["woofer"]["driver_class"] == "unknown"


def test_large_raw_shift_is_accepted_by_the_guard_and_disclosed_by_the_level_check(
    caplog,
):
    """The two layers, on one fixture — guard pair (a) plus PR-L4 item 1.

    #1668 CD-horn re-anchor: the wild-trim guard is anchored to the
    ripple-optimal tweeter trim's OWN seed, NOT the raw candidate trim.

    **Since the single-datum-owner migration (#2609) the anchor's base is
    unconditional in this harness.** This file never captures a summed
    at-the-mark baseline, so ``anchor_trims`` always falls back to the raw
    measured trim — there is no owner in hand to place the pair any other way.
    The −20 dB raw trim therefore reaches the anchor untouched (−20.918 dB),
    the scan sits 9.700 dB away from it, and the wild-trim guard fires. The
    guard is still anchored to the seed and not to the raw trim; on this
    fixture those two are the same number, so nothing here can tell them apart
    any more. The claim survives, separably, on the DEFAULT fixture in
    ``test_wild_scan_drift_falls_back_to_anchored_pair_with_warning`` and
    ``test_a_rejected_scan_is_not_committed_however_well_it_levels``.

    What PR-L4 item 1 adds is the half the guard never had: a raw trim 20 dB
    away from what these branches justify is *invisible to drift from the
    anchor* — the anchor is the thing that is wrong — and the realized-level
    check SEES it. This is the 2026-07-27 failure shape in miniature, and
    (since #2609) item 1 is the only level check left to catch it.

    **It reports; it no longer refuses** (doctrine deviation (i)). The round
    proceeds and the candidate is published, carrying the disagreement as a
    banked finding and a journal line. That is the intended consequence of the
    demotion and this test is where it is visible end-to-end: the assertions
    below are inverted from what they were, not deleted.

    **What the demotion does NOT change, since this fixture is exactly where
    someone would look for it.** ``MIN_TRIM_SANITY_MARGIN_RATIO``'s ``M >= 2T``
    floor was argued from the gate REFUSING — see that constant for the
    restated version. The floor still earns its keep, because what it now
    guarantees is that a fallback big enough to matter is one the gate SAYS
    something about rather than one it is silent on. What bounds absolute
    loudness here is unchanged and is elsewhere: the trims are attenuations
    clamped non-positive, and the output limiters and volume rail sit
    downstream of every number in this test.

    **The realized verdict is supplied.** Item 1 grades the committed pair's
    realized inter-driver level; the −20 dB is a raw-trim INPUT the fit's
    anchor would otherwise repair on its own (giveback alone can bring a
    healthy pair back in range), so the realized instrument is held at "still
    mislevelled" here to keep the arm reachable. What this test is about is the
    guard, not item 1's own arithmetic.
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    far_raw_trim = {"woofer": 0.0, "tweeter": -20.0}
    fakes.measure = lambda program: _eligible_measure_analysis(program, trim_db=far_raw_trim)
    c = _conductor(fakes)

    def _still_mislevelled(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-20.0, difference_db=-20.0,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            iv, "realized_level_match", _still_mislevelled
        )
        _run_phase(c, 1, 1)
        # No CaptureBeginRefused: the round proceeds past the level check.
        _run_phase(c, 2, 2)
    assert LINEARIZATION_TRIM_SANITY_MARGIN_DB > 0  # the constant exists and is positive
    # The guard fires (see the docstring): the anchor carries the raw −20 dB
    # trim almost untouched — this fixture's level-band give-back is 0.917 dB,
    # so the anchor lands at −19.083 — and the scan sits 9.800 dB away.
    # Asserted with its drift, because "the guard fired" without the number is
    # the shape of telemetry nobody can check.
    #
    # (Was 9.700 against an anchor of −20.0 while the give-back was measured
    # over the driver's core band. The band-matched give-back moved this
    # fixture 0.917 dB and the drift with it; the guard's behaviour — fires,
    # commits the anchored pair, and lets item 1 grade it — is unchanged. Item 1
    # refused when that note was written; deviation (i) changed what item 1 does
    # with the pair, not what this guard does.)
    assert "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    assert "drift_db=9.8" in caplog.text
    assert "committed=anchored" in caplog.text
    # …and item 1's own realized-level check DISCLOSES the 20 dB it sees.
    assert "event=correction.crossover_v2_level_match_finding" in caplog.text
    assert "tolerance_db=3.0" in caplog.text
    assert "difference_db=-20.0" in caplog.text
    # The round proceeded: a candidate exists and was published, carrying the
    # finding. Inverted from the pre-demotion assertions on purpose — the
    # household gets a proposal plus the reservation, not silence.
    assert c.candidate is not None
    assert len(fakes.published_candidates) == 1
    assert fakes.banked_findings != []


def test_wild_scan_drift_falls_back_to_anchored_pair_with_warning(caplog, monkeypatch):
    """#1668 anchored give-back, guard pair (b): when the ripple-optimal tweeter
    scan drifts implausibly far from the ANCHOR, the guard fires and the
    conductor falls back to the ANCHORED pair — NOT the raw trim (raw trim +
    emitted filters is the known VERIFY-mismatch class). Crafting a scan that
    walks that far against a synthetic fixture is awkward, so the ripple-optimal
    solve is monkeypatched to return a far-from-anchor trim.

    PR-L4 item 9: the fallback is no longer chosen by drift alone. The event now
    carries both candidate pairs' realized level errors and which one was
    committed, and the anchor wins HERE because it levels better — which is what
    the guard was always assuming and never checking.
    """
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)

    captured: dict = {}

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        # Force the resolved tweeter trim 20 dB below the anchored seed.
        return kwargs["seed_trim_db"] - 20.0, 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    # Committed the ANCHORED pair, NOT the raw trim and NOT the wild scan value.
    committed = c.candidate.role_attenuations_db
    assert set(committed) == {"woofer", "tweeter"}
    assert committed["woofer"] == pytest.approx(captured["trim_w_db"])
    assert committed["tweeter"] == pytest.approx(captured["seed_trim_db"])
    assert committed != dict(_FIXTURE_RAW_TRIM_DB)
    assert "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    assert "anchored_trim_db=" in caplog.text
    assert "fallback_trim_db=" in caplog.text
    # PR-L4 item 9: the rejection names WHY this pair won, in levels.
    assert "committed=anchored" in caplog.text
    assert "anchored_level_error_db=" in caplog.text
    assert "resolved_level_error_db=" in caplog.text
    # linearization itself still gets reported — only the trim falls back.
    assert set(c.candidate.linearization) == {"woofer", "tweeter"}


def test_a_rejected_scan_is_not_committed_however_well_it_levels(caplog, monkeypatch):
    """#2291's second acceptance criterion, at the conductor.

    Beyond the sanity margin the scan is REJECTED and the level-preserving
    anchor is committed — even when the scan levels better, which is exactly
    the case this fixture constructs (scan 0.2 dB, anchor 2.5 dB).

    **This assertion is inverted from what it pinned before #2291 Phase 2b**,
    and the inversion is the product. PR-L4 item 9 had the guard commit
    whichever pair levelled better *whether or not it had just been rejected*,
    on the 2026-07-27 evidence that drift alone points the wrong way: a scan
    that had walked 5.500 dB was walking TOWARD a correct level. #2291 is the
    later ruling — a guard whose rejection is telemetry is not a guard, and on
    2026-08-10 that policy shipped a −13.013 dB tweeter trim under the word
    "rejected". What replaces the old behaviour is not a blind fallback: the
    anchor is level-preserving by construction, and the committed pair still
    faces the realized-level assertion, so a badly-levelled anchor produces a
    refusal rather than a hot speaker.

    The graded level errors are still COMPUTED and still disclosed — the guard
    did not stop measuring, it stopped letting the measurement overrule the
    rejection — which is what the two ``*_level_error_db`` assertions below
    read.

    **Why the level verdicts are supplied rather than provoked (PR-L5).** This
    test used to drive the anchor mislevelled with a 12 dB-dark raw trim. That
    lever is gone, and gone on purpose: the shared level frame makes the anchor
    ``give-back + system_level − core_level``, in which the raw trim cancels
    out of every branch's level RELATIVE to the others — a dark raw trim can no
    longer mislevel the anchored pair, and one 12 dB off is refused as a frame
    disagreement long before this branch. That is the ladder working. What
    remains worth pinning is the guard's DECISION, so the two level verdicts
    are supplied directly and the physical scenario that used to produce them
    is left retired.
    """
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)

    seed: dict[str, float] = {}

    def _scan(*_a, **k):
        # 7 dB BELOW the anchor: past the 6 dB margin (so the guard fires) and
        # still a legal attenuation — the candidate refuses a positive trim
        # outright, and a bigger walk would fail the prediction gate downstream
        # on a fixture whose subject is the guard, not the gate.
        seed["tweeter"] = k["seed_trim_db"]
        return k["seed_trim_db"] - 7.0, 0.0, k["seed_trim_db"]

    def _match(_freqs, _w, _t, _fc_hz, trims_db, _woofer_role, tweeter_role, **_kw):
        # The SCANNED pair levels well; the anchor's does not. Both inside the
        # assertion tolerance, so the session lives and the committed pair is
        # what this test can read.
        scanned = trims_db[tweeter_role] < seed["tweeter"] - 3.0
        difference = 0.2 if scanned else 2.5
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=difference, difference_db=difference,
            tolerance_db=3.0, matched=True,
            woofer_band_hz=(1000.0, 2000.0), tweeter_band_hz=(2000.0, 4000.0),
        )

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _scan)
    monkeypatch.setattr(
        iv, "realized_level_match", _match,
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    # The session now COMPLETES, and that is a consequence of the fix rather
    # than a relaxation. Item 2 refused this fixture before #2291 Phase 2b
    # because the committed pair WAS the 7 dB-drifted scan, which measures
    # worse than its own baseline (see the swept table below: −1.524 dB at
    # drift 7). Rejecting the scan commits the anchor, which is the drift-0 row
    # — +0.657 dB, comfortably over the floor. The gate did not stop
    # discriminating; it stopped being handed a mistrim to catch.
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    # The guard FIRED (drift 7 dB > the 6 dB margin) and committed the ANCHOR,
    # although the scan levels better. Both level errors are still measured and
    # still disclosed, which is what makes the rejection auditable rather than
    # merely stated.
    assert "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text
    assert "committed=anchored" in caplog.text
    assert "committed=resolved" not in caplog.text
    assert "strategy=anchored_committed_after_sanity_drift" in caplog.text
    assert "anchored_level_error_db=2.5" in caplog.text
    assert "resolved_level_error_db=0.2" in caplog.text
    # **The swept drift table this fixture's verdicts come from** (R10a, #1817),
    # kept because it is what makes the acceptance above readable. Measured by
    # sweeping the forced drift and reading
    # ``event=correction.crossover_v2_prediction_gate`` with the pre-2b policy,
    # so every row is the COMMITTED SCAN being graded (baseline 0.957 dB rms in
    # every row; the floor is 0.5 dB):
    #
    #   drift dB     0      1      2      3       4       5       6      7      8
    #   improve  +0.657 +0.657 +0.657 +0.657  -0.324  -0.688  -1.087 -1.524 -1.998
    #   verdict   accept accept accept accept  refuse  refuse  refuse refuse refuse
    #
    # The gate DISCRIMINATES on this fixture: a correct trim ships, a mistrim of
    # 4 dB or more is caught as the regression it is. Under the flat target it
    # refused at every drift including 0.0 (-0.293 dB), because the fit's own
    # crossover-fighting cuts made even an untouched trim fail to beat its
    # baseline — the gate could not tell a wild trim from a good one.
    #
    # The last two columns are what #2291 removed from the shipping path: past
    # the 6.0 dB margin the pair no longer reaches this gate at all, because it
    # is no longer the committed pair. The gate stays as the backstop for the
    # 0-6 dB band, where a scan is still trusted to polish.


def test_anchored_trim_is_raw_plus_giveback_and_normalized_non_positive():
    """#1668 anchored give-back, the core math: each role's committed trim is
    its raw trim plus that branch's own measured LEVEL-BAND give-back, with a
    shared shift applied so no role lands POSITIVE (a boost the emitter would
    refuse). Pinned end-to-end against the conductor's committed trims.

    **This test was INERT until 2026-08-19, and how it was inert is the useful
    part.** It computed its expectation from ``correction_giveback_db`` — the
    core-band number that no longer places the trim — and then compared the
    tweeter against it under ``LINEARIZATION_TRIM_SANITY_MARGIN_DB``, a **6.0 dB**
    tolerance. The two RULES' committed anchors differ by 1.835 dB on this
    fixture (−2.691 core-band against −0.856 level-band; the 0.918 dB figure is
    the give-back *differential*, which is a different quantity and not what
    this assertion compared). Either way both sit far inside 6.0, so the
    tolerance swallowed the whole difference and the test passed on BOTH sides
    of the band change: mutating the production anchor did not move it. Its
    woofer leg was degenerate too (raw 0.0, shift equal to the woofer's own
    give-back, so it asserted 0.0 == 0.0 whatever the give-back was).

    It now reads the same frame production does (``_measured_level_frame``,
    shared with this file's other anchor tests) and grades to 1e-9, so the
    arithmetic is actually pinned. The 6.0 dB constant it used to lean on is a
    SCAN-drift guard and was never a tolerance on the anchor's own math.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    frame = _measured_level_frame(c)
    giveback = frame.giveback_db
    # Every branch that emitted filters reports a positive give-back.
    assert giveback["tweeter"] > 0.0
    # ``anchor_trims`` (single-datum-owner migration, #2609) has no summed
    # capture in hand in this harness, so it places the pair on the raw
    # measured trim alone: the anchor is ``raw_trim + giveback``, with no
    # third term.
    unnormalized = {
        r: raw_trim[r] + giveback[r] for r in ("woofer", "tweeter")
    }
    shift = max(0.0, max(unnormalized.values()))
    anchored = {r: v - shift for r, v in unnormalized.items()}

    committed = c.candidate.role_attenuations_db
    # No committed trim is a boost. The hearing-safety invariant.
    assert all(v <= 1e-9 for v in committed.values())
    # Both roles are committed at their anchor exactly on this fixture — the
    # scan walks off it and the level adjudication commits the anchor.
    assert committed["woofer"] == pytest.approx(anchored["woofer"], abs=1e-9)
    assert committed["tweeter"] == pytest.approx(anchored["tweeter"], abs=1e-9)
    # And the property the arithmetic exists to produce, asserted independently
    # of it: the committed pair hands the two branches off at the same level.
    assert abs(_inter_driver_level_error_db(frame, committed)) <= 1e-3


def test_anchored_normalization_shift_prevents_a_positive_trim(monkeypatch):
    """The normalize step: when a branch's own give-back exceeds its raw
    attenuation the unnormalized anchor would be POSITIVE; the shared shift must
    pull every role non-positive while preserving their RELATIVE leveling.

    **The raw-trim override this test used to carry is gone (R10a, #1817), and
    re-deriving it is what showed it had never done anything.** It forced
    ``{"woofer": 0.0, "tweeter": 0.0}`` on the reasoning that "any positive
    give-back pushes the unnormalized anchor above 0 and forces the shift" —
    but the raw trim always cancelled out of the frame-offset term that used
    to ride the same anchor, so overriding it changed nothing about the shift.
    Since the single-datum-owner migration (#2609) that term is gone outright:
    this harness has no summed capture in hand, so the anchor is
    unconditionally ``raw_trim + giveback``, and the raw trim is once again the
    ordinary input it always looked like.

    What the override DID do was starve a gate this test is not about. It moves
    the RAW predicted sum, which is item 2's baseline, so a zeroed trim leaves
    less than the 0.5 dB of headroom the improvement floor needs. Same sweep,
    reading ``event=correction.crossover_v2_prediction_gate`` (``after`` is
    0.300 dB rms in every row):

        raw tweeter trim dB   0.0    -0.5   -1.0   -1.5  -1.773   -2.0   -3.0
        baseline rms dB     0.647   0.708  0.792  0.895   0.957  1.012  1.284
        improvement dB      0.347   0.408  0.492  0.595   0.657  0.712  0.984
        ledger verdict      under   under  under   over    over   over   over

    (The three left-hand columns REFUSED when this table was measured; since
    the nanny burn-down the same rows bank ``not_an_improvement`` and the
    round proceeds. The arithmetic is unchanged, which is what the table is
    about.)

    So the honest value for a fixture field nobody had derived is: don't
    override it. Using ``_FIXTURE_RAW_TRIM_DB`` — solved from the same branch
    curves the conductor is handed — keeps this test's subject bit-for-bit and
    stops it riding a floor it has nothing to say about.
    """

    def _spy(*args, **kwargs):
        # Commit the anchor itself (no scan drift) so the committed pair is the
        # normalized anchor verbatim.
        return kwargs["seed_trim_db"], 0.0, kwargs["seed_trim_db"]

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", _spy)

    raw_trim = dict(_FIXTURE_RAW_TRIM_DB)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True

    # The anchor's give-back is measured over the bands the trim is read in,
    # not over each driver's own core band; ``_measured_level_frame``
    # re-measures it from the fixture's own curves and the candidate's
    # PUBLISHED filters. The premise this test needs — a woofer give-back that
    # exceeds its raw attenuation — is asserted below rather than assumed, so a
    # band that stopped producing one would fail here loudly.
    giveback = _measured_level_frame(c).giveback_db
    # No summed capture in hand, so the anchor is unconditionally
    # ``raw_trim + giveback`` (single-datum-owner migration, #2609) — no
    # third term to add.
    unnormalized = {
        r: raw_trim[r] + giveback[r] for r in ("woofer", "tweeter")
    }
    # The premise this test is built on: the woofer's own give-back exceeds
    # its raw attenuation (0.0 dB), so its unnormalized anchor is a BOOST the
    # emitter would refuse.
    assert giveback["woofer"] > -raw_trim["woofer"]
    assert unnormalized["woofer"] > 0.0
    assert max(unnormalized.values()) > 0.0, "fixture must actually need the shift"
    shift = max(unnormalized.values())
    expected = {r: v - shift for r, v in unnormalized.items()}

    committed = c.candidate.role_attenuations_db
    assert all(v <= 1e-9 for v in committed.values())  # nothing became a boost
    assert committed["woofer"] == pytest.approx(expected["woofer"])
    assert committed["tweeter"] == pytest.approx(expected["tweeter"])
    # Relative leveling preserved exactly by the shared shift.
    assert (committed["tweeter"] - committed["woofer"]) == pytest.approx(
        unnormalized["tweeter"] - unnormalized["woofer"]
    )


def test_wild_trim_boundary_exact_passes_just_above_falls_back(caplog, monkeypatch):
    """The sanity margin is an exclusive upper bound (matches this file's other
    boundary comparators): a seed drift EXACTLY at the margin is trusted, one
    hair over trips the guard. Seed-anchored (#1668), so the ripple-optimal
    solve is monkeypatched to return a controlled distance from its own seed.

    Pinned on the guard's OWN event rather than on the committed trim: since
    PR-L4 the trim a session ends up carrying is the joint outcome of this
    boundary AND the realized-level comparison (item 9) AND the publish-time
    assertion (item 1) — three decisions, and reading the trim alone could not
    tell which one moved. A drift of exactly 6.0 dB IS trusted here, and the
    resulting 6 dB-mislevelled pair is then refused downstream: the guard's
    bound and the accountability gate are different questions, deliberately.
    """

    def _run_at(drift_db: float):
        caplog.clear()
        monkeypatch.setattr(
            iv, "solve_ripple_optimal_trim",
            lambda *a, **k: (k["seed_trim_db"] - drift_db, 0.0, k["seed_trim_db"]),
        )
        fakes = FakeSeams()
        fakes.measure = lambda program: _eligible_measure_analysis(program)
        c = _conductor(fakes)
        _run_phase(c, 1, 1)
        try:
            _run_phase(c, 2, 2)
        except CaptureBeginRefused:
            pass  # the level gate's verdict; this test is about the guard's
        return "event=correction.crossover_v2_linearization_trim_rejected" in caplog.text

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    assert _run_at(LINEARIZATION_TRIM_SANITY_MARGIN_DB) is False
    assert _run_at(LINEARIZATION_TRIM_SANITY_MARGIN_DB + 0.5) is True


# --------------------------------------------------------------------------- #
# PR-L4 item 2 — spec-grade the prediction before auto-apply
# --------------------------------------------------------------------------- #


def test_predicted_spec_report_is_graded_on_the_shared_analysis_grid():
    """``spec_report_for_predicted_sum`` decimates before it smooths.

    Not cosmetic. ``smooth_fractional_octave`` is an O(bins x window) Python
    loop — ~11 s on a laptop at a raw 512k-point prediction grid, worse on a
    Pi 5 — and this runs at the confirm seam with a household waiting on the
    apply. It block-averages onto ``MAX_ANALYSIS_BINS`` first, the bound the
    combiner already adopted for the same reason, which is also what puts the
    predicted curve at the same grid density as the measured one it is compared
    against."""
    from jasper.audio_measurement.spatial_combine import MAX_ANALYSIS_BINS

    freqs = np.fft.rfftfreq(1 << 16, 1.0 / 48000.0)
    assert freqs.size > MAX_ANALYSIS_BINS  # the fixture must exercise the bound
    report = spec_report_for_predicted_sum((freqs, np.zeros(freqs.size)))

    assert report is not None
    graded_bins = sum(band.n_bins for band in report.bands)
    assert 0 < graded_bins <= MAX_ANALYSIS_BINS
    # A flat curve is flat at any grid density.
    assert report.overall_passed is True


def test_predicted_spec_report_is_unknown_never_a_pass_on_bad_input():
    """``None`` in, ``None`` out — and a malformed pair degrades the same way
    rather than raising into the confirm seam. The caller must read that as
    "no evidence", which the gate test below pins."""
    assert spec_report_for_predicted_sum(None) is None
    assert spec_report_for_predicted_sum((np.array([]), np.array([]))) is None
    assert spec_report_for_predicted_sum(("not", "arrays")) is None


def test_prediction_gate_allows_a_materially_better_correction():
    """The happy path, with the arithmetic shown rather than assumed: the
    fixture's RAW two-branch model and its LINEARIZED one are far enough apart
    that the gate passes, and the session applies."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(c)

    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.candidate is not None

    before_rms_db, after_rms_db = _gate_residuals(c)
    assert (before_rms_db - after_rms_db) >= PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB


@pytest.mark.parametrize("pre_apply_scale", [0.4, 1.0, 2.5])
def test_prediction_gate_verdict_does_not_depend_on_the_room(pre_apply_scale):
    """PR-L4 review B1, the regression that motivated the frame change.

    The first cut compared the model's residual against the MEASURED in-room
    cloud's, which made the verdict a function of the ROOM: holding the
    correction constant and varying only the pre-apply measurement flipped a
    passing session into the gate's failing arm (a refusal at the time; a
    ``not_an_improvement`` ledger entry since the nanny burn-down), and every
    BETTER room fared worse. Both of the gate's terms are now the same instrument
    at the same position, so scaling the room's own measured response — the
    only thing this parametrization changes — must not move the verdict at
    all."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    scaled = _in_room_summed_db() * pre_apply_scale
    fakes.verify = lambda program: _verify_analysis(program, summed_db=scaled)
    c = _cloud_conductor(fakes)

    verdict = _walk_measure_cloud_to_close(c)
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.candidate is not None
    # ...and the room really did move, so this is not a no-op fixture.
    measured_rms_db = c.group_cloud_result(PHASE_CLOUD_MEASURE)["flatness"]["rms_db"]
    assert measured_rms_db == pytest.approx(
        _ROOM_SCALE_EXPECTED_RMS_DB[pre_apply_scale], abs=0.05
    )


def test_prediction_gate_banks_a_correction_that_does_not_improve_and_proceeds(caplog):
    """PR-L4 item 2, after the nanny burn-down (doctrine deviation (c)).

    This used to REFUSE at the confirm seam and leave the speaker untouched.
    It was a forecast vetoing the measurement that would have settled the
    question — it took jts3's first prescribed-boost round on 2026-08-22 with
    it — and the doctrine's authority model puts a prediction on the proposing
    side of that line. So the gate now banks its verdict and the round
    proceeds: the ledger says ``not_an_improvement``, the household is not
    told its speaker was left alone, and what decides the correction's fate is
    the measured round that follows.

    **Mutation guard.** Restoring the veto makes `_walk_measure_cloud_to_close`
    raise ``CaptureBeginRefused`` and fails the first assertion here.

    Driven through the REAL threshold by a realistic bad correction — a driver
    pair whose fit cannot help, so the linearized model lands essentially on top
    of the raw one (PR-L4 review: the previous version monkeypatched the
    threshold to 100 dB, which proved the arithmetic ran and nothing about
    whether the shipped number does anything).

    **The fixture changed with PR-L5, because its old subject did.** It used to
    be a broad woofer-only suckout, "structurally unable to correct" on the
    reasoning that everything around it would have to come DOWN. Both halves of
    that stopped being true: boost can now fill a suckout, and the shared level
    frame repairs the inter-driver level error a woofer-only defect creates. A
    dense comb replaces it, and it is un-correctable for a reason no later PR
    can quietly undo — there are far more notches than the 8-filter budget, and
    chasing comb structure is precisely what the null doctrine forbids. It is
    put in BOTH drivers so the frame has nothing to fix either.

    **The comb got denser and deeper with #1809**, for a reason worth keeping
    on the record: at 6 dB / 3 cycles per octave the correction USED to be a
    regression only because the fit was boosting inside each driver's own
    crossover stopband, and each branch's stopband is the other's passband —
    so the two stopband boosts stacked in the summed prediction. Bound the lift
    to each driver's radiating band and that shape's correction becomes a
    genuine improvement (it now lands in spec). At 9 dB / 5 cycles per octave
    the comb is un-correctable on its own merits — ~35 notches against an
    8-filter budget — and the ledger reads a 0.001 dB improvement."""
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    freqs = _LINEARIZABLE_FREQS_HZ
    comb_db = 9.0 * np.sin(2.0 * np.pi * np.log2(freqs / 200.0) * 5.0)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=comb_db, tweeter_db=comb_db,
    )
    c = _cloud_conductor(fakes)

    verdict = _walk_measure_cloud_to_close(c)

    # No refusal, and the round produced a real candidate to measure.
    assert verdict["candidate_fingerprint"]
    assert c.candidate is not None
    assert c.last_failure_code is None
    # The verdict the forecast reached is on the record, at WARNING, with the
    # numbers a reader needs to weigh it.
    assert "reason=not_an_improvement" in caplog.text
    assert "required_db=0.5" in caplog.text
    assert "improvement_db=" in caplog.text


def test_prediction_gate_tolerance_is_the_models_own_tracking_error():
    """The third tolerance's derivation, pinned like its two siblings (PR-L4
    review: it was the only one without a test).

    Since B1 made both terms the same instrument, the comparison carries no
    measurement noise — so the threshold is a product-policy floor, and the
    floor is the gap between what the model predicts and what the hardware
    realizes. ``_fit_linearization`` records that as ~0.5 dB for the complex
    correction model on JTS3. An improvement smaller than the model's own
    tracking error is not one we can honestly claim."""
    complex_model_tracking_error_db = 0.5
    assert PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB == complex_model_tracking_error_db
    # And well under the zero-phase model it replaced (~2.0 dB), which is the
    # regime where "improvement" would have been indistinguishable from noise.
    assert PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB < 2.0


def test_prediction_gate_is_silent_when_the_prediction_meets_the_spec(monkeypatch):
    """A prediction that passes the spec needs no improvement argument — and
    must not be gated on one, or the flattest speakers would be refused
    hardest. Pinned with an absurd threshold so only the early return can
    explain the pass."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    monkeypatch.setattr(flow_mod, "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB", 100.0)
    monkeypatch.setattr(
        flow_mod, "spec_report_for_predicted_sum",
        lambda predicted_sum: evaluate_flat_spec(
            _SUMMED_FREQS_HZ, np.zeros(_SUMMED_FREQS_HZ.size),
        ),
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]


def test_prediction_gate_treats_an_ungradeable_prediction_as_unknown(monkeypatch):
    """An absent report is the gate having no evidence to refuse on — never a
    pass being granted, and never a refusal manufactured out of a missing
    number. Same unknown-vs-zero discipline as every other honesty instrument
    in this flow."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    monkeypatch.setattr(flow_mod, "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB", 100.0)
    monkeypatch.setattr(flow_mod, "spec_report_for_predicted_sum", lambda _s: None)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]


def test_prediction_gate_abstains_when_no_fit_ran(caplog, monkeypatch):
    """The trims-only lane has no before/after to compare.

    When linearization is ineligible (or SF2 caught a fit failure), the
    LINEARIZED prediction IS ``analysis.predicted_sum`` — the same object — so
    the two terms are identical and the improvement is exactly 0. Refusing on
    that would kill every trims-only candidate on the strength of arithmetic
    rather than evidence, so the gate abstains and says which path it took."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    monkeypatch.setattr(flow_mod, "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB", 100.0)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="consumer",  # ineligible ⇒ no fit ⇒ no linearized sum
    )
    c = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(c)
    assert verdict["candidate_fingerprint"] and "auto_apply" not in verdict
    assert c.candidate.linearization == {}
    assert "reason=no_linearization" in caplog.text


def test_prediction_gate_logs_a_ledger_line_on_every_path(caplog):
    """PR-L4 review S4: the gate speaks whether or not it refuses, mirroring
    item 1's own ledger. A gate that is silent on success makes "it passed" and
    "it never ran" indistinguishable in the journal — the first question a
    field diagnosis of a dark speaker would ask."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]

    assert "event=correction.crossover_v2_prediction_gate" in caplog.text
    # PR-L5 moved this fixture's OUTCOME, not the ledger's contract: the shared
    # level frame flattens the default pair enough that its predicted sum now
    # meets the spec outright, which is the gate's ``predicted_in_spec`` early
    # return rather than its ``improved`` one. The claim under test — that the
    # gate speaks on every path — is what this asserts, and it is stronger for
    # covering an early-return path.
    assert "reason=predicted_in_spec" in caplog.text
    # The terms the taken path can honestly report are on the line, so the
    # verdict is re-derivable from the journal alone.
    for ledger_field in ("after_rms_db=", "required_db="):
        assert ledger_field in caplog.text


def test_the_stashed_prediction_verdict_is_the_full_resolution_grade():
    """Two-stage commission D4, the "one grading instrument" pin.

    The verdict the conductor holds for the host to persist must be the grade
    of the FULL-RESOLUTION prediction — the same tuple the accountability seam
    grades — and not a re-grade of what survives persistence. This asserts
    the identity AND that the identity is a real constraint: the 512-point
    ``_decimate_sum`` reduction is demonstrably a different instrument, grading
    45/154/206 bins per band where the full 2048-point curve grades
    180/617/823 (re-derived post-#1858: before that fix's block-average,
    ``_decimate_sum`` was a raw stride and graded 45/155/205 on this same
    fixture — the two differ by one bin in two bands because a block-average
    output point sits at its block's mean frequency rather than the block's
    first raw bin, not because the instruments-differ claim below changed).
    Two reports built from those two inputs can disagree on a narrow band,
    and the screen this feeds exists to state one honest spec verdict."""
    from jasper.web.correction_crossover_v2 import _decimate_sum

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]

    report = c.measure_predicted_spec_report
    assert report is not None
    stashed = dict(report)
    comparison = stashed.pop("comparison")
    assert comparison["reason"] == "predicted_in_spec"
    # It IS the full-resolution grade.
    assert stashed == spec_report_for_predicted_sum(c.measure_predicted_sum).to_dict()

    # ...and the thing it is NOT is reachable, so the assertion above is not
    # satisfied by the two instruments happening to agree.
    decimated = _decimate_sum(c.measure_predicted_sum)
    assert len(decimated["freqs_hz"]) < c.measure_predicted_sum[0].size
    re_graded = spec_report_for_predicted_sum((
        np.asarray(decimated["freqs_hz"], dtype=float),
        np.asarray(decimated["magnitude_db"], dtype=float),
    )).to_dict()
    assert re_graded != stashed
    assert [b["n_bins"] for b in re_graded["bands"]] != [
        b["n_bins"] for b in stashed["bands"]
    ]


def test_the_prediction_verdict_is_stashed_on_the_trims_only_lane_too():
    """The hoist above the trims-only abstain, pinned.

    A candidate with no linearization still commits trims and still predicts a
    response, so it HAS a gradeable prediction — and the gate's own abstain
    (which is about having no before/after to COMPARE) must not be what decides
    whether the household is shown a verdict. Before D4 the grade sat below
    that abstain and this lane reached the wire with no verdict at all, which
    would have rendered "we could not predict this" over a prediction we can
    grade."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="consumer",  # ineligible ⇒ no fit ⇒ no linearized sum
    )
    c = _cloud_conductor(fakes)
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]

    assert c.candidate.linearization == {}
    report = c.measure_predicted_spec_report
    assert report is not None
    stashed = dict(report)
    assert stashed.pop("comparison")["reason"] == "no_linearization"
    assert stashed == spec_report_for_predicted_sum(c.measure_predicted_sum).to_dict()


def test_the_gates_ledger_and_the_stashed_verdict_never_disagree(caplog):
    """One session, one prediction, one verdict — on both surfaces.

    The trims-only ledger line carries the after-report the hoist produces, so
    a field read of the journal and a read of ``/state`` cannot state different
    things about the same prediction. (The gate's DECISION is still recorded
    separately, by ``reason=no_linearization``.)"""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="consumer",
    )
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    assert "reason=no_linearization" in caplog.text
    report = spec_report_for_predicted_sum(c.measure_predicted_sum)
    stashed = dict(c.measure_predicted_spec_report)
    assert stashed.pop("comparison")["reason"] == "no_linearization"
    assert report.to_dict() == stashed
    # ``log_event`` renders booleans JSON-style, so compare in its vocabulary
    # rather than Python's.
    assert f"after_passed={'true' if report.overall_passed else 'false'}" in caplog.text
    rms_db = round(float(spec_convergence_residual(report).rms_db), 3)
    assert f"after_rms_db={rms_db}" in caplog.text


def test_an_ungradeable_prediction_stashes_none_and_names_itself(caplog, monkeypatch):
    """D4's ``None`` propagation and its named log line.

    An absent report is a user-visible dead end — the review screen renders "we
    could not predict this" and refuses Apply on it — so it gets a line
    somebody can grep for, carrying WHICH of the two causes fired. ``None``
    must never be papered over into a fabricated verdict."""
    from jasper.active_speaker import crossover_v2_flow as flow_mod

    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    monkeypatch.setattr(flow_mod, "spec_report_for_predicted_sum", lambda _s: None)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _cloud_conductor(fakes)
    # Unknown is not a refusal: the session still completes (the gate has no
    # evidence to refuse on), it just carries no verdict.
    assert _walk_measure_cloud_to_close(c)["candidate_fingerprint"]

    assert c.measure_predicted_spec_report is None
    assert "event=correction.crossover_v2_prediction_ungradeable" in caplog.text
    # The prediction existed; the evaluator is what refused it.
    assert "why=evaluator_refused" in caplog.text
    assert "why=no_prediction" not in caplog.text


def test_an_absent_prediction_names_the_other_cause(caplog):
    """The second ``why``: nothing was predicted at all, so there was never a
    curve to grade. Separated from the evaluator's refusal because the two have
    different remedies and collapsing them would make the line unactionable.

    Reached without monkeypatching the evaluator — an analysis that carries no
    ``predicted_sum`` on the trims-only lane (nothing overrides it there) is the
    real shape of this cause."""
    caplog.set_level(logging.WARNING, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: dataclasses.replace(
        _eligible_measure_analysis(program, mic_tier="consumer"),
        predicted_sum=None,
    )
    c = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(c)

    assert c.measure_predicted_sum is None
    assert c.measure_predicted_spec_report is None
    assert "why=no_prediction" in caplog.text
    assert "why=evaluator_refused" not in caplog.text


def test_an_accountability_gate_no_longer_stamps_a_failure_code():
    """The accountability gate has no refusal left to name to the host.

    This test used to assert the opposite — that item 1's refusal reached the
    household as ``driver_levels_disagree`` rather than as a manufactured
    ``capture_timeout``. The realized-level demotion (doctrine deviation (i))
    removed the refusal, so the correct assertion is the inverse: the same
    fixture that used to raise now completes with no failure code stamped at
    all. Kept rather than deleted because ``last_failure_code`` staying ``None``
    is exactly what a reader needs to see to know the round really did proceed.

    The realized verdict is supplied for the reason its sibling above gives:
    since the #1866 ruling a frame disagreement banks a finding and proceeds,
    so a mislevelled pair has to be handed to the gate rather than provoked."""
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

    fakes = FakeSeams()
    far_raw_trim = {"woofer": 0.0, "tweeter": -20.0}
    fakes.measure = lambda program: _eligible_measure_analysis(program, trim_db=far_raw_trim)
    c = _conductor(fakes)

    def _still_mislevelled(*_a, **_kw):
        return RealizedLevelMatch(
            level_w_db=0.0, level_t_db=-20.0, difference_db=-20.0,
            tolerance_db=3.0, matched=False,
            woofer_band_hz=(800.0, 1600.0), tweeter_band_hz=(1600.0, 3200.0),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            iv, "realized_level_match", _still_mislevelled
        )
        _run_phase(c, 1, 1)
        assert c.last_failure_code is None
        _run_phase(c, 2, 2)
    assert c.last_failure_code is None
    assert c.last_failure_code != REASON_CAPTURE_TIMEOUT


def test_the_accountability_reasons_are_gone_from_the_registry():
    """No refusal, no registry row — the nanny burn-down's own bookkeeping.

    A registry row for a refusal nothing raises is copy that can never be read,
    and leaving one behind is how a veto comes back quietly: the row is the
    thing a future change would reach for. Item 2's two went with deviation
    (c); item 1's ``driver_levels_disagree`` went with deviation (i). All three
    absences are asserted together because they are one rule applied three
    times.

    A durable state persisted before either change can still carry these
    literals, and ``_failure_history_note`` reads the registry with ``.get``,
    so an old code with no row degrades to the generic clause rather than
    raising — which is why deleting the row is safe as well as correct.
    """
    assert "driver_levels_disagree" not in REASON_REGISTRY
    assert "correction_not_an_improvement" not in REASON_REGISTRY
    assert "prescribed_correction_not_an_improvement" not in REASON_REGISTRY
    assert "driver_levels_disagree" not in TRANSIENT_AUTO_RETRY_CODES


# --------------------------------------------------------------------------- #
# SF2 / SF3 (adversarial review, 2026-07-24 — #1668 PR-C review)
# --------------------------------------------------------------------------- #
#
# SF2: an eligible speaker whose fit engine raises must degrade EXACTLY to
# the ineligible path (raw trim, empty linearization) -- never fail the
# whole MEASURE accept. SF3: crossover_v2_measure_diag's new
# `linearization=` field names which of the five outcomes this attempt's
# candidate build took, for corpus-review greppability.


def test_fit_engine_bug_falls_back_to_raw_trim_with_warning(caplog, monkeypatch):
    """SF2: an eligible pair (reference tier, both paired N>=3) whose fit
    call raises must behave EXACTLY like an ineligible one -- raw trim,
    empty linearization dict, MEASURE still accepted -- never propagate and
    fail the whole accept over a bug in the fit engine."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)

    def _boom(analysis, cand, cloud=None, **_kw):
        raise ValueError("simulated fit engine bug")

    monkeypatch.setattr(c, "_plan_linearization", _boom)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}
    assert c.candidate.linearization_outcome == "fit_failed"
    # Anchored to the SAME record two ways: startswith() rather than a bare
    # `in caplog.text` substring search (the journal_dropped line's own
    # `dropped_event=` field ends in "event=", so a substring search would
    # also match a drop of this same event), and the `reason=` check reads
    # off that specific record rather than the whole caplog blob, so a drop
    # line whose port also raised ValueError could not satisfy both
    # assertions the way two independent bare-substring checks could (#2368).
    fit_failed_lines = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith(f"event={planning.EVENT_FIT_FAILED} ")
    ]
    assert fit_failed_lines, "the fit_failed event was never said"
    assert "reason=ValueError" in fit_failed_lines[0]
    assert "linearization=fit_failed" in caplog.text


def test_cut_only_invariant_violation_falls_back_instead_of_crashing(caplog, monkeypatch):
    """N1 x SF2 interaction: linearization_fit.fit_driver_linearization's own
    cut-only invariant (N1, this same review) raises RuntimeError, not
    ValueError. SF2's catch must include RuntimeError specifically so THAT
    safety net degrades to the raw-trim fallback like any other fit bug,
    instead of escaping and crashing the whole MEASURE accept -- the two
    review fixes must compose, not merely coexist."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)

    def _boom(analysis, cand, cloud=None, **_kw):
        raise RuntimeError("linearization fit emitted a boost")

    monkeypatch.setattr(c, "_plan_linearization", _boom)
    verdict = _run_phase(c, 2, 2)

    assert verdict["accepted"] is True
    assert c.candidate.role_attenuations_db == dict(_FIXTURE_RAW_TRIM_DB)
    assert c.candidate.linearization == {}
    assert "reason=RuntimeError" in caplog.text
    assert "linearization=fit_failed" in caplog.text


def test_candidate_built_linearization_field_fitted(caplog):
    """SF3: the fitted outcome.

    The field lives on ``correction.crossover_v2_candidate_built`` since the
    2026-07-27 timing move; it could not stay on ``..._measure_diag``, which is
    emitted before the candidate exists whenever a session runs a cloud group.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "event=correction.crossover_v2_candidate_built" in caplog.text
    assert "linearization=fitted" in caplog.text
    # The retired location must not quietly come back carrying a value it
    # cannot know on a cloud session.
    measure_diag = next(
        line for line in caplog.text.splitlines()
        if "event=correction.crossover_v2_measure_diag" in line
    )
    assert "linearization=" not in measure_diag
    # Gauge fix (2026-07-24): the SAME outcome is now stamped onto the
    # persisted candidate — this is the single writer's value threading all
    # the way to the artifact, not just the log line.
    assert c.candidate.linearization_outcome == "fitted"


def test_candidate_built_linearization_field_ineligible_mic_tier(caplog):
    """SF3: the ineligible_mic_tier outcome."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program, mic_tier="consumer")
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "linearization=ineligible_mic_tier" in caplog.text
    assert c.candidate.linearization_outcome == "ineligible_mic_tier"


def test_candidate_built_linearization_field_ineligible_repeats(caplog):
    """SF3: the ineligible_repeats outcome."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, mic_tier="reference", tweeter_repeats=0,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "linearization=ineligible_repeats" in caplog.text
    assert c.candidate.linearization_outcome == "ineligible_repeats"


def test_candidate_built_linearization_field_trim_rejected(caplog, monkeypatch):
    """SF3: the trim_rejected outcome (fit succeeded, but the ripple-optimal
    tweeter re-solve drifted implausibly far from its band-average seed and
    fell back to the seed pair -- distinct from "fitted" even though
    linearization is populated in both). Seed-anchored (#1668), so force the
    drift by monkeypatching the ripple-optimal solve."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    monkeypatch.setattr(
        iv, "solve_ripple_optimal_trim",
        lambda *a, **k: (k["seed_trim_db"] - 20.0, 0.0, k["seed_trim_db"]),
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert "linearization=trim_rejected" in caplog.text
    assert c.candidate.linearization_outcome == "trim_rejected"


def test_no_linearization_claim_at_all_when_the_verdict_is_rejected(caplog):
    """SF3, in its post-timing-move shape: a MEASURE verdict rejected before
    the candidate is ever built (here, the pre-existing glitch check) makes NO
    linearization claim anywhere.

    Before the move this was a ``linearization=""`` field on the measure diag —
    "never a stale value from a prior attempt, and never a guess about a path
    that was never taken." The field moved to the candidate-built event, which
    simply does not fire on a rejection, so the same promise is now kept by
    silence rather than by an empty string. What must NOT happen either way is
    a value: a rejected MEASURE has no linearization outcome to report."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program, glitch=True)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is False
    assert "event=correction.crossover_v2_candidate_built" not in caplog.text
    assert "linearization=" not in caplog.text
    assert c.candidate is None


