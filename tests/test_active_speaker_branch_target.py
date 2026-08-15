# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R10a: the crossover-shaped fit objective (#1817) and its stopband guard
(#1968).

Three groups, in the order a reader should meet them:

1. :mod:`jasper.active_speaker.branch_target`'s own contract — what the record
   promises about shape, level, contribution and the gain band.
2. The #1817 closure, end to end on the fit engine: a driver that is PERFECT
   behind its crossover attracts no correction.
3. The structural guard, including a test that proves it is load-bearing by
   running the identical fit with the guard withheld.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import (
    CrossoverSection,
    chain_response,
    crossover_response_db,
    radiating_band_hz,
)
from jasper.active_speaker.branch_target import (
    SIGNIFICANT_GAIN_DB,
    STOPBAND_GAIN_MARGIN_OCTAVES,
    branch_target,
)
from jasper.active_speaker.linearization_fit import (
    _MIN_FILTER_GAIN_DB,
    SHELF_SLOPE_THRESHOLD_DB_PER_OCT,
    LIFT_SUPPRESSION_REASONS,
    FitVocabulary,
    _lift_stage,
    fit_driver_linearization,
)
from jasper.active_speaker.linearization_envelope import compose_envelope
from jasper.audio_measurement.program_analysis import DriverResponse

_FREQS_HZ = np.linspace(100.0, 22_000.0, 4096)
_FC_HZ = 2000.0
_LOWPASS = (CrossoverSection(fc_hz=_FC_HZ, order=4, highpass=False),)
_HIGHPASS = (CrossoverSection(fc_hz=_FC_HZ, order=4, highpass=True),)


def _driver_response(
    role: str, magnitude_db: np.ndarray, *, n_repeats: int = 2,
) -> DriverResponse:
    def make() -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=_FREQS_HZ, magnitude_db=magnitude_db,
            complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
            gating={}, snr=None, validity_floor_hz=140.0,
        )

    return DriverResponse(
        role=role, freqs_hz=_FREQS_HZ, magnitude_db=magnitude_db,
        complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
        gating={}, snr=None, validity_floor_hz=140.0,
        repeat_responses=tuple(make() for _ in range(n_repeats)),
    )


def _bell(center_hz: float, height_db: float, width_oct: float) -> np.ndarray:
    return height_db * np.exp(
        -0.5 * ((np.log2(_FREQS_HZ / center_hz) / width_oct) ** 2)
    )


# --------------------------------------------------------------------------- #
# 1. the record's own contract
# --------------------------------------------------------------------------- #


def test_a_branch_with_no_committed_crossover_has_no_target():
    """No committed region -> ``None``, never a flat stand-in.

    The emitter builds its filters from the same regions, so such a branch runs
    FULL RANGE in the emitted graph: it has no shape to be held to and no
    stopband to guard. ``None`` is also what routes the fit down its exact
    pre-R10a path, which is what keeps a one-way box and the room-correction
    lane byte-identical.
    """
    assert branch_target((), _FREQS_HZ) is None


def test_the_target_shape_is_the_committed_crossovers_own_magnitude():
    """The shape is not a re-derivation — it IS
    ``branch_chain.crossover_response_db`` of the committed sections, the
    digital filter CamillaDSP realizes. One owner, so the shape the fit aims at
    and the filter the graph runs cannot drift."""
    target = branch_target(_LOWPASS, _FREQS_HZ)
    assert target is not None
    np.testing.assert_allclose(
        target.shape_db, crossover_response_db(_FREQS_HZ, _LOWPASS),
    )


def test_centring_moves_shape_not_level():
    """``centred_on`` subtracts the shape's own median over the level band, so
    the target curve's median there equals ``target_level_db`` exactly.

    This is the property that let #1817 be closed WITHOUT changing what
    ``target_level_db`` means to its four level consumers: the scalar still
    reads as "where this driver's passband sits", and the curve adds only the
    difference from flat.
    """
    target = branch_target(_LOWPASS, _FREQS_HZ)
    assert target is not None
    level_mask = (_FREQS_HZ >= 200.0) & (_FREQS_HZ <= 1600.0)
    centred = target.centred_on(level_mask)

    assert float(np.median(centred.shape_db[level_mask])) == pytest.approx(0.0)
    curve = centred.target_curve_db(-12.5)
    assert float(np.median(curve[level_mask])) == pytest.approx(-12.5)
    # …and the SHAPE itself is untouched — only its offset moved.
    np.testing.assert_allclose(
        np.diff(centred.shape_db), np.diff(target.shape_db), atol=1e-12,
    )


def test_an_empty_level_mask_centres_nothing():
    """A median over no bins is not a level, so the record is returned
    unchanged rather than shifted by a NaN."""
    target = branch_target(_LOWPASS, _FREQS_HZ)
    assert target is not None
    assert target.centred_on(np.zeros_like(_FREQS_HZ, dtype=bool)) is target


def test_contribution_is_an_amplitude_fraction_that_falls_through_the_crossover():
    """``contribution`` is this branch's output as a fraction of its own full
    output: ~1.0 deep in the passband, falling monotonically through the
    crossover, always inside [0, 1]. It reads the RAW shape (never the centred
    one) because it is a physical quantity, not a shape relative to a level."""
    target = branch_target(_LOWPASS, _FREQS_HZ)
    assert target is not None
    assert np.all(target.contribution >= 0.0)
    assert np.all(target.contribution <= 1.0)

    def at(hz: float) -> float:
        return float(target.contribution[int(np.argmin(np.abs(_FREQS_HZ - hz)))])

    assert at(200.0) == pytest.approx(1.0, abs=1e-3)
    assert at(_FC_HZ) == pytest.approx(0.5, abs=0.02)   # LR4 is -6 dB at Fc
    assert at(4.0 * _FC_HZ) < 0.02
    # Monotonic for a lowpass.
    assert np.all(np.diff(target.contribution) <= 1e-9)


def test_the_gain_band_is_the_passband_widened_by_the_declared_margin():
    """#1968's rule, in one assertion: gain is permitted out to half an octave
    past the acoustic passband edge, and the passband edge is
    ``radiating_band_hz`` (the -3 dB span) — not Fc, and not the fit band."""
    for sections in (_LOWPASS, _HIGHPASS):
        target = branch_target(sections, _FREQS_HZ)
        assert target is not None
        lo_hz, hi_hz = radiating_band_hz(sections)
        assert target.passband_hz == (lo_hz, hi_hz)

        widen = 2.0 ** STOPBAND_GAIN_MARGIN_OCTAVES
        expected_lo = lo_hz / widen if lo_hz > 0.0 else lo_hz
        expected_hi = hi_hz * widen if np.isfinite(hi_hz) else hi_hz
        assert target.gain_band_hz[0] == pytest.approx(expected_lo)
        assert target.gain_band_hz[1] == pytest.approx(expected_hi)
        np.testing.assert_array_equal(
            target.gain_permitted,
            (_FREQS_HZ >= expected_lo) & (_FREQS_HZ <= expected_hi),
        )


def test_the_lowpass_gain_band_ends_where_the_research_says_it_should():
    """The concrete number behind the constant, so a future edit has to argue
    with it: for LR4 the -3 dB edge is 0.801*Fc, so half an octave past it is
    1.133*Fc, where the crossover is 8.46 dB down."""
    target = branch_target(_LOWPASS, _FREQS_HZ)
    assert target is not None
    assert target.passband_hz[1] == pytest.approx(0.8012 * _FC_HZ, rel=1e-3)
    assert target.gain_band_hz[1] == pytest.approx(1.1331 * _FC_HZ, rel=1e-3)

    edge_db = float(crossover_response_db(
        np.array([target.gain_band_hz[1]]), _LOWPASS,
    )[0])
    assert edge_db == pytest.approx(-8.46, abs=0.15)


def test_a_negative_gain_margin_is_a_caller_bug():
    """It would narrow the gain band INSIDE the passband the fit is entitled to
    correct, so it raises rather than quietly refusing the fit its own band."""
    with pytest.raises(ValueError, match="gain_margin_octaves"):
        branch_target(_LOWPASS, _FREQS_HZ, gain_margin_octaves=-0.1)


def test_the_records_arrays_cannot_be_mutated_in_place():
    """Four consumers inside one fit read these; a stage that scaled one in
    place would silently re-grade every stage after it."""
    target = branch_target(_LOWPASS, _FREQS_HZ)
    assert target is not None
    for array in (target.shape_db, target.contribution, target.gain_permitted):
        with pytest.raises(ValueError):
            array[0] = 0.0


def test_the_significant_gain_epsilon_is_the_fits_own_minimum_filter_gain():
    """``branch_target`` states this value rather than importing it (the import
    runs the other way), so it is pinned equal here — a drift would either
    refuse cascades over differences the fit cannot express, or let real gain
    through unguarded."""
    assert SIGNIFICANT_GAIN_DB == _MIN_FILTER_GAIN_DB


# --------------------------------------------------------------------------- #
# 2. #1817 closed, on the fit engine
# --------------------------------------------------------------------------- #


def _perfect_branch_env():
    """A driver that is FLAT behind its own LR4 low-pass — i.e. its measured
    curve is exactly the crossover."""
    magnitude_db = crossover_response_db(_FREQS_HZ, _LOWPASS)
    resp = _driver_response("woofer", magnitude_db)
    envelope = compose_envelope(
        "woofer", resp, excited_band_hz=(150.0, 6000.0),
        mic_tier="reference", driver_class="unknown",
    )
    return resp, envelope


def _perfect_branch_fit(*, shaped: bool):
    resp, envelope = _perfect_branch_env()
    # On the ENVELOPE's grid, which is the fit's own — see
    # ``test_a_target_built_on_the_wrong_grid_is_refused_loudly``.
    target = branch_target(_LOWPASS, envelope.freqs_hz) if shaped else None
    return fit_driver_linearization(
        resp, envelope,
        # The BOOST vocabulary, because that is the condition under which
        # #1817 was measured: a cut-only fit reads a crossover-shaped branch
        # as falling everywhere and simply places nothing, so the defect only
        # becomes reachable once the fit is permitted to add level ("with
        # PR-L5's boost vocabulary it could finally 'fix' it").
        vocabulary=FitVocabulary(allow_boost=True),
        radiating_band_hz=radiating_band_hz(_LOWPASS),
        target=target,
    )


def test_a_target_built_on_the_wrong_grid_is_refused_loudly():
    """The composer has both the driver response's native grid and the
    envelope's in scope, and only the envelope's is the fit's. Handing the
    wrong one used to surface as an ``IndexError`` from inside a median
    several frames down; it is a named ``ValueError`` at the boundary now.
    """
    resp, envelope = _perfect_branch_env()
    wrong_grid = branch_target(_LOWPASS, _FREQS_HZ)
    assert wrong_grid is not None
    assert wrong_grid.shape_db.shape != envelope.freqs_hz.shape
    with pytest.raises(ValueError, match="different grid"):
        fit_driver_linearization(resp, envelope, target=wrong_grid)


def test_a_flat_driver_behind_its_crossover_attracts_no_correction():
    """**#1817, closed.** The canonical instance: a driver with nothing wrong
    with it, measured through its own LR4, must not be corrected at all.

    Against the FLAT target the same driver attracted a lift just inside its
    own band edge — the issue measured +2.379 dB at 1570.6 Hz (0.79*Fc) — for
    the sole reason that its crossover's rolloff read as a driver deficit.
    Against the crossover-shaped target there is nothing to correct, because
    the branch already IS its target.

    The residual is pinned too, not just the filter count: it falls from
    2.5759 dB rms to 0.0124 dB, which is the statement that the shaped target
    is not merely *declining* to correct but is actually the curve the branch
    already sits on.

    **The 0.0124 was 0.0230 before #2523**, on the same fixture and the same
    filters (none). What moved is the band the FIT claim is made over: the
    residual now runs to the SOLVE band's edge rather than the adaptive trim's,
    and the bins it drops are the ones where the branch's own measured curve
    parts company with the IDEAL crossover it is graded against. Both numbers
    say "this branch already IS its target"; the smaller one says it over the
    span the fit actually solved. VERIFY and OBSERVE still report the wider
    spans, so nothing about the deep stopband stopped being disclosed.
    """
    shaped = _perfect_branch_fit(shaped=True)
    assert shaped.filters == ()
    assert shaped.headroom_cost_db == 0.0
    assert shaped.lift_requested_db == 0.0
    assert shaped.residual_rms_db == pytest.approx(0.0124, abs=0.002)


def test_the_flat_target_is_what_put_a_filter_on_a_perfect_driver():
    """The other half of the claim above, so it is a MEASURED contrast rather
    than an assertion about one arm.

    Identical driver, identical envelope, identical radiating bound; the only
    difference is whether the fit is handed its branch's shape. The flat-target
    arm corrects a driver that is already perfect.

    **The flat arm reproduces #1817's own measurement to four decimals** —
    one Peaking filter, +2.3786 dB at 1570.6 Hz — which is what makes this a
    regression record for that issue rather than a demonstration of some
    neighbouring effect.
    """
    flat = _perfect_branch_fit(shaped=False)
    shaped = _perfect_branch_fit(shaped=True)

    assert len(flat.filters) == 1, (
        "the flat-target arm must correct this perfect driver for the "
        "contrast to mean anything"
    )
    defect = flat.filters[0]
    assert defect.biquad_type == "Peaking"
    assert defect.gain == pytest.approx(2.3786, abs=0.001)
    assert defect.freq == pytest.approx(1570.6, rel=1e-3)
    # …and it sits inside the radiating band, so #1809's bound did NOT catch
    # it. That bound and this target close different halves of one defect.
    assert defect.freq < radiating_band_hz(_LOWPASS)[1]

    assert shaped.filters == ()
    assert shaped.residual_rms_db < flat.residual_rms_db


def test_no_target_is_the_pre_r10a_fit_byte_for_byte():
    """The default path is untouched. This is what keeps every caller that
    predates R10a — including the room-correction lane, which never supplies a
    target — on exactly the fit it had.
    """
    magnitude_db = crossover_response_db(_FREQS_HZ, _LOWPASS) + _bell(600.0, 5.0, 0.3)
    resp = _driver_response("woofer", magnitude_db)
    envelope = compose_envelope(
        "woofer", resp, excited_band_hz=(150.0, 6000.0),
        mic_tier="reference", driver_class="unknown",
    )
    implicit = fit_driver_linearization(resp, envelope)
    explicit = fit_driver_linearization(resp, envelope, target=None)
    assert implicit.to_dict() == explicit.to_dict()


# --------------------------------------------------------------------------- #
# 3. the structural stopband-gain guard
# --------------------------------------------------------------------------- #


#: Where this fixture's fit band stops, Hz. Load-bearing — see
#: :func:`_lift_inputs`.
_FIXTURE_BAND_HI_HZ = 2000.0


def _lift_inputs():
    """A branch whose deficit sits right at its radiating edge — the shape that
    makes a boost's SKIRT reach into the stopband.

    The dip is centred at the -3 dB edge itself (0.801*Fc = 1602.9 Hz) and is
    broad, so the bell the designer answers it with is low-Q and wide.

    **``band_mask`` stops at 2000 Hz, and that is the whole point of the
    fixture.** The gain band ends at 2266.8 Hz, so every bin the guard must
    refuse lies ABOVE the fit band — which is what makes this fixture able to
    tell the shipped whole-grid guard apart from a mask-limited one. An earlier
    version of this fixture ran the mask to 6000 Hz; the offending bins then sat
    INSIDE it, ``realized_db[band_mask & ~gain_permitted]`` saw them too, and
    the test passed against the very mutation it exists to forbid (caught by
    adversarial review of PR #1999, 2026-08-01).
    ``test_a_mask_limited_guard_would_miss_these_bins_entirely`` is the
    counterfactual that keeps it that way.
    """
    grid_hz = np.geomspace(150.0, 20_000.0, 512)
    shape_db = crossover_response_db(grid_hz, _LOWPASS)
    edge_hz = radiating_band_hz(_LOWPASS)[1]
    dip_db = -8.0 * np.exp(-0.5 * ((np.log2(grid_hz / edge_hz) / 0.35) ** 2))
    working_db = shape_db + dip_db

    target = branch_target(_LOWPASS, grid_hz)
    assert target is not None
    band_mask = (grid_hz >= 150.0) & (grid_hz <= _FIXTURE_BAND_HI_HZ)
    lift_mask = band_mask & (grid_hz <= edge_hz)

    class _Env:
        allowed_depth_db = np.full_like(grid_hz, 12.0)

    return grid_hz, working_db, target, band_mask, lift_mask, _Env()


def _run_lift(*, guarded: bool):
    grid_hz, working_db, target, band_mask, lift_mask, env = _lift_inputs()
    return grid_hz, target, band_mask, _lift_stage(
        grid_hz, working_db, target.target_curve_db(0.0), env,
        band_mask, (), FitVocabulary(allow_boost=True),
        lift_mask=lift_mask,
        contribution=target.contribution,
        gain_permitted=target.gain_permitted if guarded else None,
    )


def _realized_db(filters, grid_hz):
    biquads = [
        {"biquad_type": f.biquad_type, "freq": f.freq, "q": f.q, "gain": f.gain}
        for f in filters
    ]
    return 20.0 * np.log10(
        np.maximum(np.abs(chain_response(biquads, grid_hz)), 1e-12)
    )


def test_the_guard_is_load_bearing_a_boost_skirt_does_reach_the_stopband():
    """**This is the test that would FAIL on the pre-R10a fit.**

    Identical inputs, run twice, with the gain mask withheld from one arm.
    Unguarded, the lift stage answers a deficit at the radiating edge with a
    bell whose SKIRT puts significant gain past the half-octave line — gain the
    branch's own crossover then attenuates. That is #1809's pathology arriving
    by the one route #1809's own mask cannot see, because that mask bounds
    where the deficit is READ, not where the emitted filter reaches.

    The pre-existing realization gate cannot be relied on to catch it either:
    it tests ``realized_db[band_mask]``, and whether the stopband falls inside
    ``band_mask`` is not guaranteed — see
    :func:`test_a_mask_limited_guard_would_miss_these_bins_entirely`.
    """
    grid_hz, target, band_mask, unguarded = _run_lift(guarded=False)
    assert unguarded.filters, "the fixture must emit a boost to mean anything"

    realized_db = _realized_db(unguarded.filters, grid_hz)
    stopband = ~target.gain_permitted
    worst_db = float(np.max(realized_db[stopband]))
    assert worst_db > SIGNIFICANT_GAIN_DB, (
        f"unguarded lift put only {worst_db:.3f} dB past the gain band; the "
        "fixture no longer exercises the defect"
    )


def test_a_mask_limited_guard_would_miss_these_bins_entirely():
    """**The counterfactual, carried in the test rather than left to a
    reviewer's mutation.**

    The shipped guard reads ``realized_db[~gain_permitted]`` — the WHOLE grid.
    The plausible-looking alternative is to reuse the stage's existing
    band-limited idiom, ``realized_db[band_mask & ~gain_permitted]``. This
    asserts the two are not the same test: on this fixture the offending bins
    lie entirely ABOVE the fit band, so the mask-limited form sees **zero** of
    them and would let the boost through.

    Without this, the suite could not tell the two apart. The first version of
    this fixture ran ``band_mask`` to 6000 Hz, which put every offending bin
    inside it; the mask-limited mutation then passed all twenty tests in this
    module (adversarial review of PR #1999, 2026-08-01). The fixture is now
    built so that it cannot.
    """
    grid_hz, target, band_mask, unguarded = _run_lift(guarded=False)
    realized_db = _realized_db(unguarded.filters, grid_hz)
    stopband = ~target.gain_permitted

    shipped = realized_db[stopband] > SIGNIFICANT_GAIN_DB
    mask_limited = realized_db[band_mask & stopband] > SIGNIFICANT_GAIN_DB

    assert int(shipped.sum()) > 0, "the fixture must offend for this to mean anything"
    assert int(mask_limited.sum()) == 0, (
        "the offending bins must lie OUTSIDE band_mask, or this fixture cannot "
        "distinguish the shipped guard from a mask-limited one"
    )
    # …and the reason is geometric, stated so a future edit to either bound
    # trips here rather than silently un-discriminating the fixture.
    assert float(np.max(grid_hz[band_mask])) < target.gain_band_hz[1]


def test_the_guard_refuses_the_cascade_that_would_have_put_gain_there():
    """The same inputs WITH the mask: the stage refuses, names why, and emits
    no boost at all rather than a trimmed one.

    Refusing outright (rather than shrinking) is the conservative arm and
    matches the stage's two existing realization refusals — a cascade the
    guard rejects is one the designer built against the wrong constraint, and
    re-deriving it under the constraint is a search this stage does not run.
    """
    _grid_hz, _target, _band_mask, guarded = _run_lift(guarded=True)
    assert guarded.suppressed_reason == "stopband_gain"
    assert guarded.from_boost_db == 0.0
    assert all(f.gain <= 0.0 for f in guarded.filters)


def test_every_lift_suppression_reason_is_enumerated():
    """The new reason joins the pinned set, so a suppression path cannot ship
    an un-enumerated string (the same rule the set already held)."""
    assert "stopband_gain" in LIFT_SUPPRESSION_REASONS
    _grid_hz, _target, _band_mask, guarded = _run_lift(guarded=True)
    assert guarded.suppressed_reason in LIFT_SUPPRESSION_REASONS


def _lift_with(*, deficit_hz: float, contribution: bool):
    """One lift-stage run against a single dip at ``deficit_hz``, with the
    contribution weight supplied or withheld. Everything else identical."""
    grid_hz = np.geomspace(150.0, 20_000.0, 512)
    target = branch_target(_LOWPASS, grid_hz)
    assert target is not None
    shape_db = crossover_response_db(grid_hz, _LOWPASS)
    dip = -6.0 * np.exp(-0.5 * ((np.log2(grid_hz / deficit_hz) / 0.25) ** 2))
    band_mask = (grid_hz >= 150.0) & (grid_hz <= 6000.0)

    class _Env:
        allowed_depth_db = np.full_like(grid_hz, 12.0)

    return _lift_stage(
        grid_hz, shape_db + dip, target.target_curve_db(0.0), _Env(),
        band_mask, (), FitVocabulary(allow_boost=True),
        lift_mask=band_mask,
        contribution=target.contribution if contribution else None,
        gain_permitted=target.gain_permitted,
    )


def test_contribution_weighting_shrinks_a_low_contribution_deficit():
    """#1968's contribution weighting, gain side only — as BEHAVIOUR, not as
    an array of numbers.

    The same 6 dB dip is placed twice: once deep in the passband, once out
    where the crossover has already taken this branch mostly out of the sum.
    In the passband the weight is inert (the branch is at full output, so
    there is nothing to discount). Out at the edge it materially shrinks what
    the stage asks for — which is the whole point: headroom spent where the
    listener barely hears it is headroom wasted.
    """
    passband_on = _lift_with(deficit_hz=400.0, contribution=True)
    passband_off = _lift_with(deficit_hz=400.0, contribution=False)
    assert passband_on.requested_db == pytest.approx(
        passband_off.requested_db, rel=0.02
    ), "in the passband the weight should be ~inert"

    edge_on = _lift_with(deficit_hz=1500.0, contribution=True)
    edge_off = _lift_with(deficit_hz=1500.0, contribution=False)
    assert edge_off.requested_db > 0.0
    assert edge_on.requested_db < 0.8 * edge_off.requested_db, (
        f"weighted {edge_on.requested_db:.3f} dB vs unweighted "
        f"{edge_off.requested_db:.3f} dB — the weight is not biting"
    )


def test_the_shelf_slope_gate_reads_the_crossover_not_the_driver():
    """The shelf stage's own instance of #1817, pinned to the numbers.

    A FLAT tweeter behind a 2 kHz LR4 high-pass has no slope of its own. Its
    high-pass drags the bottom of the fit band down, and a regression over the
    raw curve reads that as the DRIVER rising steeply — arming a gate whose
    threshold is 3.0 dB/oct with 5.70 dB/oct of pure crossover.

    Deliberately asserts the SLOPE, not an emitted filter: in this
    configuration no shelf results anyway (the corner selection takes the low
    side, whose drop below target is negative). The stage is being asked the
    wrong question and saved by an unrelated downstream test, which is worth
    fixing but is not worth over-claiming — see ``_shelf_stage``'s docstring.
    """
    grid_hz = np.geomspace(150.0, 20_000.0, 512)
    measured_db = crossover_response_db(grid_hz, _HIGHPASS)
    level_mask = (grid_hz >= radiating_band_hz(_HIGHPASS)[0]) & (grid_hz <= 18_000.0)
    target = branch_target(_HIGHPASS, grid_hz)
    assert target is not None
    shape_db = target.centred_on(level_mask).shape_db

    band = (grid_hz >= 800.0) & (grid_hz <= 18_000.0)
    log2f = np.log2(grid_hz[band])
    raw_slope = float(np.polyfit(log2f, measured_db[band], 1)[0])
    deshaped_slope = float(
        np.polyfit(log2f, (measured_db - shape_db)[band], 1)[0]
    )

    assert raw_slope == pytest.approx(5.6957, abs=0.01)
    assert raw_slope > SHELF_SLOPE_THRESHOLD_DB_PER_OCT
    assert deshaped_slope == pytest.approx(0.0, abs=0.01)
    assert deshaped_slope < SHELF_SLOPE_THRESHOLD_DB_PER_OCT


def test_cuts_are_deliberately_not_contribution_weighted():
    """The documented departure from #1968, pinned so it cannot be "tidied"
    into agreement with the research later.

    Read literally, weighting the whole least-squares error would also shrink
    CUT authority outside the passband. #1809 measured that case and ruled the
    other way: a cut there is ordinary useful work — the crossover attenuated
    the band but did not silence it, whatever leaks through still reaches the
    sum, and a cut spends no headroom. So the fit must still place cuts past
    the radiating edge.
    """
    magnitude_db = crossover_response_db(_FREQS_HZ, _LOWPASS) + _bell(2600.0, 6.0, 0.25)
    resp = _driver_response("woofer", magnitude_db)
    envelope = compose_envelope(
        "woofer", resp, excited_band_hz=(150.0, 6000.0),
        mic_tier="reference", driver_class="unknown",
    )
    fit = fit_driver_linearization(
        resp, envelope,
        vocabulary=FitVocabulary(allow_boost=True),
        radiating_band_hz=radiating_band_hz(_LOWPASS),
        target=branch_target(_LOWPASS, envelope.freqs_hz),
    )
    edge_hz = radiating_band_hz(_LOWPASS)[1]
    beyond = [f for f in fit.filters if f.freq > edge_hz and f.gain < 0.0]
    assert beyond, (
        "a bump past the radiating edge must still attract a CUT; only GAIN "
        f"is bounded out there (filters: {fit.filters})"
    )
