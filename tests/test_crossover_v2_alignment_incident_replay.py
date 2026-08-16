"""Incident replay: the alignment that committed a commanded null (issue #2598).

**Characterization tests of a located defect.** On 2026-08-15/16 a jts3
commissioning session applied, fourteen rounds running, an LR4 crossover at
1648.7 Hz with the tweeter branch INVERTED and delayed 96.0 us — while the
preset it was graded against declares both drivers non-inverted. A
Linkwitz-Riley pair sums flat only in matched polarity; inverted, it commands a
null. Three rounds of trims and linearization then tried to fit around a dip the
alignment itself had ordered.

The root cause, from the same evidence: polarity was the SIGN OF A GCC-PHAT
CORRELATION PEAK, evaluated at -292 us while the applied delay was +96 us, on a
tweeter impulse response whose own capture verdict was ``insufficient`` SNR
(-1.2 dB; the woofer's was 44.0 dB / ``ok``). The flat-sum discriminator that
would have caught it was computed every round and thrown away.

What the fixture banks, and what this module builds
---------------------------------------------------
The fixture is the round as it happened: the preset's declared crossover
region, the per-branch capture-SNR verdicts, the alignment scalars the applied
candidate froze, one more MEASURE capture's diagnostic (the defect is
deterministic, so a second round is a second witness), and the three 511-bin
VERIFY curves the round retained.

The branch pair is BUILT, not banked, and built from the DECLARED DESIGN: the
production Linkwitz-Riley transfers
(``jasper.active_speaker.branch_chain.crossover_response_complex``) at the
banked corner, with the tweeter set the banked level-match trim hot — which is
what that trim measured. Two reasons it is built rather than replayed: the
retained evidence holds summed magnitude curves, not the complex per-branch
transfer functions the selector reads; and the declared design is what VERIFY
grades against, so a defect visible in it is a defect in the shipped intent, not
an artifact of one room.
``test_the_declared_design_model_reproduces_the_incident_at_scale`` is what
earns that substitution, and it earns it AT SCALE, not at precision: compared in
the frame the box shipped, the model says 14.2 dB and the box said 10.5, and
both dwarf the sub-dB the declared polarity would have left. An ideal filter
pair is not this speaker's drivers; a fixture that claimed decimal agreement
between them would be claiming something it had not measured.

Nothing here is tuned to jts3. Every number that could be is read from the
fixture; the model is the preset's own filter math; the assertions are about a
mechanism (an inverted LR pair nulls at its corner) that holds at any corner, in
any room, for any driver pair.

Injection surface: NONE. No seam, stub, spy or monkeypatch — these tests call
``_select_alignment_pair`` and ``_build_candidate`` directly on arrays built
here. Re-derivation: the fixture's sources are a LIVE speaker's runtime state
(``/var/lib/jasper/...`` on jts3, read-only over ssh, each source's sha256 in
the fixture's ``_provenance``), not a retained capture bundle, so there is no
``--check`` re-derivation script of the kind
``scripts/derive-crossover-incident-fixture.py`` provides for the 2026-08-10
incident; the provenance hashes are the audit trail instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import (
    crossover_response_complex,
    sections_by_role,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_FLAT_MINIMUM_EPSILON_DB,
    ALIGNMENT_OK,
    AlignmentEstimate,
    AppliedAlignment,
    _build_candidate,
    _ripple_db,
    _select_alignment_pair,
    overlap_band_hz,
    predicted_branch_sum,
    summed_model_residual_delay_us,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "crossover_v2_alignment_incident_20260816"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


SESSION_CONTEXT = _fixture("session_context")
MEASURE_ALIGNMENT = _fixture("measure_alignment")
VERIFY_CURVES = _fixture("verify_curves")

REGION = SESSION_CONTEXT["crossover_region"]
APPLIED = MEASURE_ALIGNMENT["applied_candidate"]
FC_HZ = REGION["fc_hz"]
#: The pair the box committed: correlation's polarity at correlation's delay.
SEED_POLARITY_SIGN = -1 if APPLIED["polarity"] == "inverted" else 1
SEED_DELAY_US = APPLIED["delay_us"]
ANCHOR_DELAY_US = APPLIED["anchor_delay_us"]
TRIM_W_DB = APPLIED["trim_band_average_db"]["woofer"]
TRIM_T_DB = APPLIED["trim_band_average_db"]["tweeter"]
#: What the BOX computed for the pair it actually shipped, in the frame it
#: shipped it. Both numbers are the pre-#2598 candidate's own semantics:
#: ``alignment_seed_ripple_db`` was the summed ripple AT THE ANCHOR (zero
#: residual) and ``flatness_improvement_db`` was ``anchor − selected``, so the
#: difference is the ripple at the committed +96 us delay. Deriving it rather
#: than reading ``predicted_ripple_db`` is the whole point: that field is the
#: ZERO-RESIDUAL number, and comparing a model carrying +34.1 us of residual
#: against it compares two different quantities (#2607 review C2).
BOX_COMMITTED_RIPPLE_DB = (
    APPLIED["alignment_seed_ripple_db"] - APPLIED["flatness_improvement_db"]
)
#: How far an IDEAL filter model may sit from this speaker's measured branches
#: at the same pair and the same frame, before the substitution stops being
#: credible. Deliberately loose: the model is two textbook Linkwitz-Riley
#: sections and the box measured two real drivers in a real room, so the two
#: are expected to differ by dB, not by hundredths. What the fixture rests on
#: is the SCALE of the finding — both say this pair leaves more than 10 dB of
#: ripple where the declared polarity leaves under 1 — not on agreement to any
#: precision.
MODEL_VS_BOX_TOLERANCE_DB = 5.0
#: Declared |delay| search bound, margin-expanded exactly as
#: ``crossover_v2_flow.alignment_delay_search_bounds_us`` does (0.1 ms).
DELAY_BOUNDS_US = (
    max(0.0, REGION["delay_range_ms"][0] - 0.1) * 1000.0,
    (REGION["delay_range_ms"][1] + 0.1) * 1000.0,
)
SR = 48_000


def _declared_branches(n_bins: int = 8193, f_max_hz: float = 24_000.0):
    """``(freqs, W, T)`` for the preset's DECLARED crossover at the banked corner.

    The production section builder, so the filters are the ones the emitted
    graph runs. The tweeter carries ``-trim_t`` dB of gain: the level match
    measured that gap and the trim cancels it, so a model that omits it would
    be scoring a pair the trim then un-levels.
    """
    region = SimpleNamespace(
        fc_hz=REGION["fc_hz"],
        order=REGION["order"],
        lower_driver=REGION["lower_driver"],
        upper_driver=REGION["upper_driver"],
    )
    sections = sections_by_role([region])
    freqs = np.linspace(0.0, f_max_hz, n_bins)
    woofer = crossover_response_complex(freqs, sections=sections["woofer"])
    tweeter = crossover_response_complex(
        freqs, sections=sections["tweeter"],
    ) * 10.0 ** (-TRIM_T_DB / 20.0)
    return freqs, woofer, tweeter


def _summed_db(freqs, W, T, sign, delay_us, band):
    summed = predicted_branch_sum(
        W[band], T[band], TRIM_W_DB, TRIM_T_DB, sign,
        freqs_hz=freqs[band],
        residual_delay_us=summed_model_residual_delay_us(ANCHOR_DELAY_US, delay_us),
    )
    return summed, 20.0 * np.log10(np.maximum(np.abs(summed), 1e-12))


def test_the_fixture_is_the_incident_as_banked():
    """Identity guard. If any of these move, the fixture is a different event
    and every number below is about something else.
    """
    for name in ("session_context", "measure_alignment", "verify_curves"):
        prov = _fixture(name)["_provenance"]
        assert prov["issue"] == 2598
        assert prov["candidate_fingerprint"] == (
            "85a83ace1eaefb92838161f3f0676752937764a19f316334dc3827c1fff36375"
        )
        assert prov["sources"], "a fixture with no cited source is not evidence"

    # The declared design: a 4th-order Linkwitz-Riley pair, BOTH drivers
    # non-inverted. This is the target VERIFY grades against.
    assert REGION["target_type"] == "LinkwitzRiley"
    assert REGION["order"] == 4
    assert REGION["lower_polarity"] == "non-inverted"
    assert REGION["upper_polarity"] == "non-inverted"
    assert FC_HZ == pytest.approx(1648.7)

    # What shipped instead.
    assert APPLIED["polarity"] == "inverted"
    assert APPLIED["delay_us"] == pytest.approx(95.997, abs=1e-3)
    # Correlation's own seed was 388 us away from the delay that shipped, and on
    # the far side of zero — the sign this pair rested on was read at a lag the
    # candidate itself did not believe.
    assert APPLIED["alignment_seed_delay_us"] == pytest.approx(-291.996, abs=1e-3)
    assert APPLIED["delay_us"] - APPLIED["alignment_seed_delay_us"] > 380.0

    # And the branch that sign was read from was not measurable.
    assert SESSION_CONTEXT["capture_snr"]["tweeter"]["verdict"] == "insufficient"
    assert SESSION_CONTEXT["capture_snr"]["tweeter"]["snr_db"] < 0.0
    assert SESSION_CONTEXT["capture_snr"]["woofer"]["verdict"] == "ok"
    # Deterministic, not a one-off: a second MEASURE capture in the same
    # session, same answer.
    assert MEASURE_ALIGNMENT["one_more_round"]["polarity"] == "inverted"
    assert MEASURE_ALIGNMENT["one_more_round"]["alignment_status"] == ALIGNMENT_OK

    # The round ended honestly but empty-handed, which is the cost being paid.
    assert VERIFY_CURVES["adoption"]["outcome"] == "keep_for_iteration"
    assert VERIFY_CURVES["adoption"]["reason"] == "benefit_unproven"


def test_the_declared_design_model_reproduces_the_incident_at_scale():
    """The substitution this module rests on, stated at the precision it has.

    An ideal model of the DECLARED design, carrying the banked pair AT THE
    BANKED DELAY, and the box's own number for that same pair and that same
    frame: 14.2 dB against 10.5 dB. They agree that the committed pair leaves
    more than 10 dB of ripple in the blend, which is the finding; they do not
    agree to a fraction of a dB, and nothing here needs them to.

    An earlier version of this test claimed 0.10 dB. That comparison was
    CROSS-FRAME (#2607 review C2): it put the model at +34.1 us of residual
    beside ``predicted_ripple_db``, which is the box's ZERO-RESIDUAL quantity.
    Matched up properly the two frames disagree by +3.8 dB (here) and +46 dB
    (at zero residual) — an ideal filter pair is not this speaker's drivers,
    and a tolerance of 0.15 dB was really pinning +/-0.63 us of delay.
    """
    freqs, W, T = _declared_branches()
    lo, hi = SESSION_CONTEXT["crossover_region_hz"]
    band = (freqs >= lo) & (freqs <= hi)
    shipped, _db = _summed_db(freqs, W, T, SEED_POLARITY_SIGN, SEED_DELAY_US, band)
    kept, _kept_db = _summed_db(freqs, W, T, +1, SEED_DELAY_US, band)
    modelled = _ripple_db(freqs[band], shipped, lo, hi)

    # Same pair, same delay, same band: the model and the box agree on scale.
    assert modelled > 10.0
    assert BOX_COMMITTED_RIPPLE_DB > 10.0
    assert abs(modelled - BOX_COMMITTED_RIPPLE_DB) < MODEL_VS_BOX_TOLERANCE_DB
    # And the gap that matters dwarfs the disagreement: the declared polarity
    # at the same delay is more than 9 dB flatter than either number above, so
    # no plausible model-vs-speaker error reverses the finding.
    assert _ripple_db(freqs[band], kept, lo, hi) < 1.0
    assert min(modelled, BOX_COMMITTED_RIPPLE_DB) - 1.0 > 9.0
    # The crossover region the box graded IS the nominal Fc-octave band, so the
    # fixture's banked span and the shipped derivation agree.
    assert (lo, hi) == pytest.approx(overlap_band_hz(FC_HZ))


def test_the_committed_pair_is_a_commanded_null():
    """The defect, in one comparison: same branches, same delay, one sign.

    Inverted — what shipped — the blend collapses inside the crossover region.
    Non-inverted, it is flat to a fraction of a dB. So the dip three rounds
    chased was ordered by the alignment, not left over by the drivers.
    """
    freqs, W, T = _declared_branches()
    lo, hi = SESSION_CONTEXT["crossover_region_hz"]
    band = (freqs >= lo) & (freqs <= hi)

    shipped, shipped_db = _summed_db(freqs, W, T, SEED_POLARITY_SIGN, SEED_DELAY_US, band)
    kept, _kept_db = _summed_db(freqs, W, T, +1, SEED_DELAY_US, band)

    null_hz = float(freqs[band][int(np.argmin(shipped_db))])
    assert shipped_db.min() < -12.0
    # The null sits in the blend, within a half-octave of the corner it was
    # ordered at — that is what makes it the crossover's and not the room's.
    assert FC_HZ / 1.5 <= null_hz <= FC_HZ * 1.5
    assert _ripple_db(freqs[band], shipped, lo, hi) > 10.0
    assert _ripple_db(freqs[band], kept, lo, hi) < 1.0


def test_the_flat_sum_selector_rejects_the_commanded_null():
    """The fix. Given the incident's own seed, the joint objective commits the
    declared polarity and says out loud that correlation lost.
    """
    freqs, W, T = _declared_branches()
    lo, hi = SESSION_CONTEXT["crossover_region_hz"]

    selection = _select_alignment_pair(
        freqs, W, T,
        fc_hz=FC_HZ, lo_hz=lo, hi_hz=hi,
        trim_w_db=TRIM_W_DB, trim_t_db=TRIM_T_DB,
        anchor_delay_us=ANCHOR_DELAY_US,
        seed_delay_us=SEED_DELAY_US,
        seed_polarity_sign=SEED_POLARITY_SIGN,
        delay_bounds_us=DELAY_BOUNDS_US,
    )
    assert selection is not None
    assert selection.objective == ALIGNMENT_COMMITTED_FLAT_SUM
    assert selection.polarity_sign == +1
    assert selection.polarity_agrees_with_sum is False
    # The seed pair is scored, so the improvement is a real comparison and not
    # a claim. Compared in the frame the box shipped — its own committed-pair
    # ripple, derived in BOX_COMMITTED_RIPPLE_DB — at the honest tolerance an
    # ideal model earns against measured drivers (#2607 review C2).
    assert selection.seed_ripple_db > 10.0
    assert abs(
        selection.seed_ripple_db - BOX_COMMITTED_RIPPLE_DB
    ) < MODEL_VS_BOX_TOLERANCE_DB
    assert selection.ripple_db < 1.0
    assert selection.flatness_improvement_db > 10.0
    # The delay is not thrown away with the polarity. Correlation's delay is
    # inside the flat basin once the sign is right, so the regularization keeps
    # it — the selector changes what it must and nothing else.
    assert selection.delay_us == pytest.approx(SEED_DELAY_US, abs=1e-9)
    # And the search really was a search: both polarities across a grid whose
    # step derives from the corner, never a hardcoded microsecond count.
    assert selection.grid_points > 100
    assert selection.grid_step_us == pytest.approx(1e6 / FC_HZ / 61, rel=0.02)
    assert all(
        abs(d) <= DELAY_BOUNDS_US[1]
        for d in (selection.delay_us, ANCHOR_DELAY_US)
    )


def test_a_delay_alone_cannot_rescue_the_inverted_pair():
    """Why polarity and delay are ONE decision, and why the objective needs
    both axes rather than a polarity fix bolted onto the old delay search.

    A delay can very nearly fake a polarity: half a period at the corner is
    180 degrees, so the inverted pair does have a best case. Two things are
    wrong with it, and this pins both. It is still measurably worse than the
    declared pair — by more than the search's own flat-minimum epsilon, which
    is what stops the regularization from keeping correlation's sign — and it
    buys that flatness with a delay a QUARTER MILLISECOND away from the
    physical peak gap, a time offset the arrival measurement does not support.
    The declared pair is flat at the anchor itself.
    """
    freqs, W, T = _declared_branches()
    lo, hi = SESSION_CONTEXT["crossover_region_hz"]
    band = (freqs >= lo) & (freqs <= hi)

    span_us = 1e6 / FC_HZ  # one period at the corner: the ambiguity interval
    grid = [
        float(d)
        for d in np.linspace(
            ANCHOR_DELAY_US - span_us, ANCHOR_DELAY_US + span_us, 241,
        )
        if abs(d) <= DELAY_BOUNDS_US[1]
    ]
    assert len(grid) == 241, "the declared bound must not truncate this sweep"

    def _best(sign):
        ripples = [
            _ripple_db(freqs[band], _summed_db(freqs, W, T, sign, d, band)[0], lo, hi)
            for d in grid
        ]
        i = int(np.argmin(ripples))
        return ripples[i], grid[i]

    best_inverted, at_inverted = _best(-1)
    best_kept, at_kept = _best(+1)
    assert best_kept < 0.1
    assert best_inverted > 0.5
    assert best_inverted - best_kept > ALIGNMENT_FLAT_MINIMUM_EPSILON_DB
    # The declared pair is flat AT the measured arrival gap; the inverted one
    # has to walk most of a quarter period away from it to get close.
    assert at_kept == pytest.approx(ANCHOR_DELAY_US, abs=0.5 * span_us / 120)
    assert abs(at_inverted - ANCHOR_DELAY_US) > 200.0


def _refused_selection(freqs, W, T, *, applied_alignment=None):
    """The incident's own inputs, with its own tweeter verdict refusing."""
    lo, hi = SESSION_CONTEXT["crossover_region_hz"]
    return _select_alignment_pair(
        freqs, W, T,
        fc_hz=FC_HZ, lo_hz=lo, hi_hz=hi,
        trim_w_db=TRIM_W_DB, trim_t_db=TRIM_T_DB,
        anchor_delay_us=ANCHOR_DELAY_US,
        seed_delay_us=SEED_DELAY_US,
        seed_polarity_sign=SEED_POLARITY_SIGN,
        delay_bounds_us=DELAY_BOUNDS_US,
        branch_snr_insufficient=True,
        applied_alignment=applied_alignment,
    )


def test_insufficient_branch_snr_commits_the_declared_design():
    """The precondition. With the incident's own tweeter verdict, the pair is
    not searched at all: it is committed to the declared design — declared
    polarity, and no delay, because this speaker has none applied to hold — and
    the objective string says which commitment that is.

    Disclosed, not blocked (the never-nanny rule): the household still gets an
    alignment, and it is the one the preset asks for rather than one read off a
    -1.2 dB capture.
    """
    freqs, W, T = _declared_branches()
    assert SESSION_CONTEXT["capture_snr"]["tweeter"]["verdict"] == "insufficient"

    selection = _refused_selection(freqs, W, T)
    assert selection is not None
    assert selection.objective == ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR
    assert selection.polarity_sign == +1
    # Neither the anchor nor the seed — both are this refused capture's own
    # answer (#2617), and the anchor is exactly the quantity a low-SNR capture
    # gets wrong.
    assert selection.delay_us == 0.0
    assert selection.delay_us != pytest.approx(ANCHOR_DELAY_US)
    assert selection.delay_us != pytest.approx(SEED_DELAY_US)
    assert selection.grid_points == 1
    # The declined pair is still scored, so the record shows what was refused —
    # and the cross-check says NOT-ASKED rather than claiming a disagreement no
    # flat sum was allowed to reach on this capture.
    assert selection.seed_polarity_sign == SEED_POLARITY_SIGN
    assert selection.seed_ripple_db > 10.0
    assert selection.polarity_agrees_with_sum is None


def test_a_refused_capture_holds_the_delay_this_speaker_already_plays():
    """Issue #2617, on the incident's own branches.

    The box had already applied 96.0 µs on the tweeter when this capture came
    back refused. Holding that — the best configuration available given the
    evidence, per the roadmap's ethos — is the commitment; the anchor this
    capture produced is reported as declined evidence and never becomes the
    delay. The polarity half is unchanged from #2607.

    **What this banked fixture CANNOT discriminate, stated so it is not
    misread** (#2622 correctness lens, C-n3): on this round the applied delay
    and the correlation SEED are the same number — 95.997 µs, because the seed
    is what the previous round committed — so an implementation that committed
    ``seed_delay_us`` here would leave this test green. It discriminates
    against the ANCHOR (61.87 µs, asserted below), which is the value #2617
    removed, and that is the whole of what it proves. The seed half of the
    invariant is pinned on a synthetic fixture where the two differ:
    ``test_select_alignment_pair_holds_the_applied_delay_on_an_unmeasurable_branch``
    in ``tests/test_audio_measurement_program_analysis.py`` (seed 90 µs,
    anchor 25 µs, held −140 µs — three distinguishable numbers).
    """
    freqs, W, T = _declared_branches()
    applied_us = APPLIED["delay_us"]

    selection = _refused_selection(
        freqs, W, T, applied_alignment=AppliedAlignment(applied_us),
    )
    assert selection is not None
    assert selection.objective == ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR
    assert selection.delay_us == pytest.approx(applied_us)
    assert selection.delay_us != pytest.approx(ANCHOR_DELAY_US)
    assert selection.polarity_sign == +1
    assert selection.grid_points == 1
    assert selection.polarity_agrees_with_sum is None
    # The declined anchor is still scored beside it, exactly as on the
    # no-applied-delay path — declining evidence is not discarding it.
    assert selection.seed_ripple_db == pytest.approx(
        _refused_selection(freqs, W, T).seed_ripple_db
    )


def test_the_production_candidate_build_ships_the_committed_pair():
    """End to end through ``_build_candidate``: the same branches as impulse
    responses, the incident's seed as the ``AlignmentEstimate``, and the
    candidate that comes out carries the declared polarity plus the evidence a
    reader needs to see that correlation disagreed.

    The dB scale is smaller here than in the analytic tests above and that is
    expected, not a weaker claim: ``_aligned_branch_tf`` puts each branch
    through the production arrival window and reflection gate, and truncating
    an ideal Linkwitz-Riley tail partially fills in an ideal null. The
    magnitudes live in the analytic tests; what this one pins is that the
    production build reads the selector's answer and persists its evidence.
    """
    n_fft = 16_384
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SR)
    region = SimpleNamespace(
        fc_hz=REGION["fc_hz"], order=REGION["order"],
        lower_driver=REGION["lower_driver"], upper_driver=REGION["upper_driver"],
    )
    sections = sections_by_role([region])
    # Both branches referenced to the SAME arrival index: `_aligned_branch_tf`
    # re-references each to its own peak anyway, and the frame the model reads
    # is `committed - anchor`, which the estimate below supplies.
    t0_s = 0.02
    phase = np.exp(-1j * 2.0 * np.pi * freqs * t0_s)
    woofer_ir = np.fft.irfft(
        crossover_response_complex(freqs, sections=sections["woofer"]) * phase, n=n_fft,
    )
    tweeter_ir = np.fft.irfft(
        crossover_response_complex(freqs, sections=sections["tweeter"])
        * 10.0 ** (-TRIM_T_DB / 20.0) * phase,
        n=n_fft,
    )
    alignment = AlignmentEstimate(
        delay_us=APPLIED["alignment_seed_delay_us"],
        raw_delay_us=APPLIED["alignment_seed_delay_us"],
        parallax_us=0.0,
        polarity=APPLIED["polarity"],
        polarity_sign=SEED_POLARITY_SIGN,
        confidence=APPLIED["alignment_confidence"],
        status=ALIGNMENT_OK,
        anchor_delay_us=ANCHOR_DELAY_US,
        snapped_delay_us=SEED_DELAY_US,
    )

    candidate, _predicted = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, FC_HZ,
        REGION["lower_driver"], REGION["upper_driver"], alignment, None,
        alignment_delay_bounds_us=DELAY_BOUNDS_US,
    )
    assert candidate.polarity == "normal"
    assert candidate.alignment_objective == ALIGNMENT_COMMITTED_FLAT_SUM
    assert candidate.seed_polarity_sign == SEED_POLARITY_SIGN
    assert candidate.flatness_improvement_db > 0.5
    # Non-negative by construction: the seed pair is always one of the scored
    # candidates, so the committed pair can never be flatter-in-name-only.
    assert candidate.flatness_improvement_db >= -ALIGNMENT_FLAT_MINIMUM_EPSILON_DB
    assert abs(candidate.delay_us) <= DELAY_BOUNDS_US[1]


def test_the_measured_round_dips_where_the_model_puts_the_null():
    """The hardware half. The round's own retained VERIFY curves show the dip
    the model predicts, inside the crossover region — so this is a replay of a
    measured event, not a theory about one.
    """
    freqs = np.asarray(VERIFY_CURVES["freqs_hz"], dtype=float)
    measured = np.asarray(VERIFY_CURVES["measured_db"], dtype=float)
    predicted = np.asarray(VERIFY_CURVES["predicted_db"], dtype=float)
    lo, hi = SESSION_CONTEXT["crossover_region_hz"]
    region = (freqs >= lo) & (freqs <= hi)

    def _worst_dip(curve):
        centred = curve[region] - np.median(curve[region])
        i = int(np.argmin(centred))
        return float(centred[i]), float(freqs[region][i])

    measured_depth, measured_hz = _worst_dip(measured)
    predicted_depth, predicted_hz = _worst_dip(predicted)
    # A blend that should be flat by design is 4 dB down at its worst point.
    assert measured_depth < -3.0
    assert FC_HZ / 1.5 <= measured_hz <= FC_HZ * 1.5
    # The round's OWN model already put a dip in the same half-octave: the
    # prediction was not wrong about the speaker, the alignment it modelled was
    # wrong about the design.
    assert predicted_depth < -3.0
    assert abs(np.log2(predicted_hz / measured_hz)) < 0.5
