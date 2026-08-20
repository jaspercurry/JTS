# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The objective grades in the referee's currency, and reports a PROFILE.

Two claims are on trial here and they are different claims. **Commensurability**
— a predicted win and a measured win are the same quantity — is tested by
grading the same arrays through the shipped path and through this module and
requiring the same number. **Profile honesty** — that one high chunk and one low
chunk do not read as flat — is tested on synthetic curves whose right answer is
known by construction, because a curve nobody planted teaches nothing when the
assertion passes.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.attempt_grading import (
    PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
)
from jasper.active_speaker.crossover_v2.objective import (
    ADVISORY_WEIGHTS, CHUNK_FRACTION, CHUNK_REGRESSION_BAR_DB,
    DI_COVERAGE_HORIZONTAL_ONLY, ChunkDeviation, FlatnessProfile, GradingFrame,
    ObjectiveWeights, directivity_continuity, dominates, flatness_profile,
    score_prediction, spec_graded_curve,
)
from jasper.active_speaker.flat_spec import (
    BEST_EFFORT_ABOVE_HZ, GATED_SPEC_LOWER_EDGE_HZ, evaluate_flat_spec,
    spec_convergence_residual,
)
from jasper.active_speaker.flat_spec_views import (
    PositionCurve, role_split_flatness,
)
from jasper.audio_measurement.spatial_combine import DEFAULT_SPEC_FRACTION

# One capture's own 2.5/T honesty floor, not a constant — the value the
# 2026-08-18/19 jts3 sessions carried, used here so the band under test is the
# band those sessions were graded over.
FLOOR_HZ = 357.14285714285717


def _grid(n_bins: int = 4096) -> np.ndarray:
    """A linear analysis grid, the shape the decimator hands the grader."""
    return np.linspace(20.0, 20000.0, n_bins)


def _flat(freqs: np.ndarray, level_db: float = -20.0) -> np.ndarray:
    return np.full(freqs.shape, float(level_db), dtype=np.float64)


def _graded(freqs: np.ndarray, curve_db: np.ndarray, *, floor_hz=FLOOR_HZ):
    """One evaluation, reduced and graded exactly as the objective does."""
    return spec_graded_curve(freqs, curve_db, trusted_floor_hz=floor_hz)


def _profile(freqs: np.ndarray, curve_db: np.ndarray, *, frame=None, floor_hz=FLOOR_HZ):
    graded = _graded(freqs, curve_db, floor_hz=floor_hz)
    return flatness_profile(
        graded,
        frame=frame or GradingFrame.self_referenced(trusted_floor_hz=floor_hz),
        view="probe",
    )


def _bump(freqs: np.ndarray, curve_db: np.ndarray, band_hz, amount_db: float):
    """``curve_db`` with one band moved by ``amount_db``, returned as a copy."""
    moved = np.array(curve_db, dtype=np.float64, copy=True)
    inside = (freqs >= band_hz[0]) & (freqs < band_hz[1])
    moved[inside] += float(amount_db)
    return moved


def _chunk(
    f_lo_hz: float, f_hi_hz: float, *,
    level_db: float, worst_db: float, rms_db: float,
) -> ChunkDeviation:
    """One chunk stated directly, so a discriminating pair can be EXACT.

    ``dominates`` is a pure function of two profiles, and the pair that
    separates its three candidate regression metrics has to hold a chunk whose
    mean and worst bin move in opposite directions by chosen amounts. Planting
    that in a curve and pushing it through the 1/3-octave smoother would leave
    the deciding numbers to whatever the smoother happened to do, and smear the
    trap into its neighbours besides. Stated here instead; the route from a
    curve to these fields is ``flatness_profile``'s, and has its own tests.
    """
    center_hz = math.sqrt(f_lo_hz * f_hi_hz)
    return ChunkDeviation(
        f_lo_hz=f_lo_hz,
        f_hi_hz=f_hi_hz,
        center_hz=center_hz,
        n_bins=12,
        level_db=level_db,
        worst_db=worst_db,
        worst_hz=center_hz,
        rms_db=rms_db,
    )


def _profile_of(chunks: tuple[ChunkDeviation, ...]) -> FlatnessProfile:
    """A profile carrying exactly ``chunks``, headline numbers derived from them."""
    worst = max(chunks, key=lambda chunk: abs(chunk.worst_db))
    return FlatnessProfile(
        view="probe",
        reference_db=-20.0,
        reference_policy="self",
        band_hz=(chunks[0].f_lo_hz, chunks[-1].f_hi_hz),
        chunks=chunks,
        worst_chunk_db=worst.worst_db,
        worst_chunk_hz=worst.worst_hz,
        tilt_db_per_octave=0.0,
        tilt_span_db=0.0,
        rms_db=float(np.sqrt(np.mean([chunk.rms_db ** 2 for chunk in chunks]))),
        n_bins=sum(chunk.n_bins for chunk in chunks),
        evaluable=True,
    )


# --------------------------------------------------------------------------
# The chunk width, and where the band comes from
# --------------------------------------------------------------------------


def test_a_chunk_is_one_smoothing_window_wide() -> None:
    """The chunk width is the spec smoothing fraction, not a second number."""
    assert CHUNK_FRACTION == DEFAULT_SPEC_FRACTION


def test_chunks_step_geometrically_at_the_declared_fraction() -> None:
    freqs = _grid()
    profile = _profile(freqs, _flat(freqs))

    ratios = [
        chunk.f_hi_hz / chunk.f_lo_hz
        # the last chunk is truncated at the band edge, so it is excluded from
        # the ratio check rather than allowed to fail it
        for chunk in profile.chunks[:-1]
    ]
    assert ratios
    for ratio in ratios:
        assert ratio == pytest.approx(2.0 ** (1.0 / CHUNK_FRACTION), rel=1e-12)


def test_the_graded_band_reads_the_session_floor_and_never_hardcodes_it() -> None:
    """357.14 Hz is one capture's 2.5/T, not a constant this module owns."""
    freqs = _grid()

    with_floor = _profile(freqs, _flat(freqs), floor_hz=FLOOR_HZ)
    assert with_floor.band_hz == (FLOOR_HZ, BEST_EFFORT_ABOVE_HZ)

    without_floor = _profile(freqs, _flat(freqs), floor_hz=None)
    assert without_floor.band_hz == (GATED_SPEC_LOWER_EDGE_HZ, BEST_EFFORT_ABOVE_HZ)

    higher = _profile(freqs, _flat(freqs), floor_hz=800.0)
    assert higher.band_hz == (800.0, BEST_EFFORT_ABOVE_HZ)


# --------------------------------------------------------------------------
# RULING B — the profile, and the trap it exists to catch
# --------------------------------------------------------------------------


def test_one_hot_chunk_and_one_cold_chunk_do_not_read_as_flat() -> None:
    """The owner's trap case, stated as a number rather than as a principle.

    A curve +3 dB across one third-octave and -3 dB across another averages to
    a neutral level and a modest RMS. It is not flat, and the profile has to say
    so out loud — which is the whole reason acceptance reads the profile and not
    the scalar.
    """
    freqs = _grid()
    curve = _flat(freqs)
    curve = _bump(freqs, curve, (1000.0, 1260.0), +3.0)
    curve = _bump(freqs, curve, (2000.0, 2520.0), -3.0)

    profile = _profile(freqs, curve)

    # The scalar summary is unremarkable...
    assert profile.rms_db < 1.0
    # ...and the level is very nearly neutral: the two bumps cancel.
    assert abs(profile.tilt_span_db) < 1.0
    # ...but the worst chunk is the real number, and it is the planted one.
    assert profile.worst_chunk_abs_db == pytest.approx(3.0, abs=0.35)
    assert 900.0 <= profile.worst_chunk_hz <= 2600.0

    # And the two regions are BOTH visible, with opposite signs, which is what
    # the scalar destroyed.
    hot = [chunk for chunk in profile.chunks if chunk.level_db > 1.0]
    cold = [chunk for chunk in profile.chunks if chunk.level_db < -1.0]
    assert hot and cold


def test_a_flat_curve_reads_as_flat() -> None:
    """The control. Without it the assertions above pass against any bug."""
    freqs = _grid()
    profile = _profile(freqs, _flat(freqs))

    assert profile.evaluable
    assert profile.worst_chunk_abs_db == pytest.approx(0.0, abs=1e-6)
    assert profile.rms_db == pytest.approx(0.0, abs=1e-6)
    assert profile.tilt_db_per_octave == pytest.approx(0.0, abs=1e-9)


def test_tilt_recovers_a_planted_slope_in_db_per_octave() -> None:
    """A pure slope in log-frequency comes back as the slope that was planted."""
    freqs = _grid()
    planted = -1.5
    curve = -20.0 + planted * np.log2(np.maximum(freqs, 1.0) / 1000.0)

    profile = _profile(freqs, curve)

    assert profile.tilt_db_per_octave == pytest.approx(planted, abs=0.02)
    octaves = math.log2(profile.band_hz[1] / profile.band_hz[0])
    assert profile.tilt_span_db == pytest.approx(planted * octaves, abs=0.1)


def test_a_broadband_tilt_is_visible_even_when_the_rms_is_modest() -> None:
    """The defect that shipped a +2.5 dB-hot tweeter behind a decent RMS.

    A rising slope puts most of its cost at the ends of the band, so the RMS
    around the band's own power mean stays smaller than the end-to-end error.
    The tilt is what names it.
    """
    freqs = _grid()
    curve = -20.0 + 0.55 * np.log2(np.maximum(freqs, 1.0) / 1000.0)

    profile = _profile(freqs, curve)

    assert abs(profile.tilt_span_db) > 2.5
    assert profile.rms_db < abs(profile.tilt_span_db) / 2.0


def test_a_local_notch_diverges_the_max_norm_from_the_rms() -> None:
    """Max-norm and RMS answer different questions, and one of them is heard.

    The notch is one third-octave wide because that is the resolution the grade
    HAS: the curve arrives 1/3-octave smoothed, so a 200 Hz notch at 3 kHz is
    already averaged across a ~700 Hz window before anything here sees it, and
    reads as 1.2 dB rather than 8. That is the referee's own blindness, not this
    module's, and a profile that claimed to see through it would be inventing
    resolution.
    """
    freqs = _grid()
    curve = _bump(freqs, _flat(freqs), (2520.0, 3175.0), -8.0)

    profile = _profile(freqs, curve)

    assert profile.worst_chunk_abs_db > 7.0
    assert profile.rms_db < profile.worst_chunk_abs_db / 3.0


def test_a_floor_above_the_whole_spec_raises_out_of_the_evaluator() -> None:
    """The EVALUATOR's refusal, surfaced rather than swallowed.

    Named for what it asserts. A floor above the whole spec never reaches this
    module's own no-surviving-chunk branch, because ``evaluate_flat_spec``
    refuses an empty reference band first; what this pins is that the objective
    lets that ``ValueError`` out instead of turning it into a flat report. The
    module's own branch has its own test below.
    """
    freqs = _grid()
    profile = _profile(freqs, _flat(freqs), floor_hz=None)
    assert profile.evaluable

    with pytest.raises(ValueError):
        _profile(freqs, _flat(freqs), floor_hz=19_000.0)


def test_a_curve_with_no_surviving_chunk_is_unevaluable_never_zero() -> None:
    """``flatness_profile``'s honesty branch, reached rather than described.

    The docstring promises that a curve with no surviving chunk is
    ``evaluable=False`` with a reason — "never a zero, which would read as
    perfect". A zero worst chunk and a zero RMS are what a defensive
    implementation would most naturally return here, and they are the two
    numbers every consumer of this profile reads, so the promise is worth a
    test of its own.

    Reached the one way a real evaluation reaches it: a published exclusion mask
    covering the whole graded span. Masking is the honesty screen's own output,
    so this is the shape the branch exists for, not a contrived one.
    """
    freqs = _grid()
    graded = _graded(freqs, _flat(freqs))
    masked = dataclasses.replace(
        graded, excluded=np.ones(np.asarray(graded.freqs_hz).shape, dtype=bool),
    )

    # Non-vacuity: the SAME evaluation with nothing masked grades fine, so the
    # refusal below is the mask's doing rather than a broken fixture.
    assert flatness_profile(
        graded,
        frame=GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ),
        view="probe",
    ).evaluable

    profile = flatness_profile(
        masked,
        frame=GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ),
        view="probe",
    )

    assert profile.evaluable is False
    assert "no bin survived" in profile.not_evaluated_reason
    # NEVER A ZERO — every headline number is absent, not flat.
    assert profile.worst_chunk_db is None
    assert profile.worst_chunk_abs_db is None
    assert profile.worst_chunk_hz is None
    assert profile.rms_db is None
    assert profile.tilt_db_per_octave is None
    assert profile.chunks == ()
    assert profile.n_bins == 0


# --------------------------------------------------------------------------
# RULING A — commensurability, proven by grading the same arrays twice
# --------------------------------------------------------------------------


def test_the_profile_rms_equals_the_referees_own_residual() -> None:
    """``profile.rms_db`` IS ``spec_convergence_residual``, not a lookalike.

    The three spec bands tile ``[250, 16000)`` with no gap and no overlap, and
    the residual is their bin-count-weighted RMS — which is the RMS over the
    union of their included bins. The profile takes that same RMS over that same
    union. If these two ever diverge, the profile's summary has stopped being
    the referee's number and every comparison built on it is in another
    currency.
    """
    freqs = _grid()
    rng = np.random.default_rng(11)
    curve = _flat(freqs) + rng.normal(scale=1.5, size=freqs.size)

    graded = _graded(freqs, curve)
    profile = flatness_profile(
        graded,
        frame=GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ),
        view="probe",
    )
    residual = spec_convergence_residual(graded.report)

    assert residual.evaluable
    assert profile.n_bins == residual.n_bins
    assert profile.rms_db == pytest.approx(residual.rms_db, rel=1e-12)


def test_the_role_pooling_matches_role_split_flatness_exactly() -> None:
    """This module's per-role pool is the shipped pool, to floating point.

    ``_pool_rms``'s docstring promises this test by name: the identity is
    written out locally rather than reached across a module boundary for a
    private, and what keeps the two from drifting is this assertion.
    """
    from jasper.active_speaker.crossover_v2.objective import _pool_rms

    freqs = np.geomspace(200.0, 18_000.0, 240)
    rng = np.random.default_rng(3)
    positions = tuple(
        PositionCurve(
            position_id=f"p{index}",
            role="onax" if index < 3 else "offax",
            freqs_hz=freqs,
            magnitude_db=-20.0 + rng.normal(scale=1.2, size=freqs.size),
            smoothing_fraction=6,
            degrees=float(index),
        )
        for index in range(5)
    )
    frame_report = evaluate_flat_spec(
        freqs, positions[0].magnitude_db, None, trusted_floor_hz=FLOOR_HZ,
    )

    split = role_split_flatness(frame_report, positions, primary_role="onax")

    for role_flatness in (split.primary, *split.others):
        pairs = []
        for position in positions:
            if position.role != role_flatness.role:
                continue
            report = evaluate_flat_spec(
                np.asarray(position.freqs_hz, dtype=float),
                np.asarray(position.magnitude_db, dtype=float),
                None,
                smoothing_fraction=position.smoothing_fraction,
                trusted_floor_hz=frame_report.trusted_floor_hz,
            )
            residual = spec_convergence_residual(report)
            pairs.append((float(residual.n_bins), float(residual.rms_db)))
        assert _pool_rms(pairs) == pytest.approx(role_flatness.rms_db, rel=1e-15)


def test_scoring_never_averages_one_role_into_another() -> None:
    """Two roles, two numbers, and one seat's defect survives into its role.

    **This guards ROLES, not SEATS**, despite what "never averages" suggests on
    its own — the two roles here hold one seat each, so nothing in it exercises
    what happens when several seats merge into one role profile. That is the
    other half of the never-collapse claim and it has its own test:
    :func:`test_two_seats_cancelling_do_not_cancel_in_the_role_profile`.
    """
    freqs = _grid()
    on_axis = _flat(freqs)
    off_axis = _bump(freqs, _flat(freqs), (4000.0, 5040.0), -6.0)
    frames = {
        angle: GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ)
        for angle in (0.0, 20.0)
    }

    scored = score_prediction(
        {0.0: on_axis, 20.0: off_axis},
        freqs,
        role_by_angle={0.0: "onax", 20.0: "offax"},
        primary_role="onax",
        on_axis_deg=0.0,
        frame_by_angle=frames,
        crossover_region_hz=(1000.0, 4000.0),
    )

    onax = scored.view("onax")
    offax = scored.view("offax")
    assert onax is not None and offax is not None
    assert onax.residual_db == pytest.approx(0.0, abs=1e-6)
    assert offax.residual_db > 0.5
    # The defect is in the off-axis role's own profile, undiluted...
    assert offax.profile.worst_chunk_abs_db > 3.0
    # ...and it never leaked into the on-axis one.
    assert onax.profile.worst_chunk_abs_db == pytest.approx(0.0, abs=1e-6)
    # Every seat is reported on its own, which is what "never collapsed" means.
    assert len(scored.per_seat) == 2


# --------------------------------------------------------------------------
# The frozen reference
# --------------------------------------------------------------------------


def test_two_seats_cancelling_do_not_cancel_in_the_role_profile() -> None:
    """A role's worst chunk is its WORST SEAT, never an average of them.

    This is the module's whole reason for existing, one dimension in. The
    shipped pooled view power-averages the seat CURVES before any deviation is
    taken, so a chunk that is hot at one seat and equally cold at another
    reads as very nearly flat. A role profile that averaged its seats the same
    way would reproduce exactly that defect inside the tool built to expose it —
    and it would do so invisibly, because every OTHER number in the profile
    behaves correctly under that mutation.

    Both halves are asserted, because their difference is the finding:

    * the chunk's LEVEL cancels, to about +0.13 dB from a planted +/-3 dB — that
      is the trap, reproduced here on purpose rather than hidden;
    * the chunk's WORST does NOT, staying at the worst seat's full magnitude.

    Verified by mutation (2026-08-19): replacing the ``max``-by-magnitude with a
    per-seat mean moves this role's worst chunk from -2.92 dB to -0.04 dB and
    fails this test, while leaving all 52 others green — which is how the gap
    was found in the first place.
    """
    freqs = _grid()
    chunk_hz = (1000.0, 1260.0)
    hot = _bump(freqs, _flat(freqs), chunk_hz, +3.0)
    cold = _bump(freqs, _flat(freqs), chunk_hz, -3.0)
    frames = {
        angle: GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ)
        for angle in (-7.0, 7.0)
    }

    scored = score_prediction(
        {-7.0: hot, 7.0: cold},
        freqs,
        # ONE role, TWO seats — the case the sibling role test does not reach.
        role_by_angle={-7.0: "onax", 7.0: "onax"},
        primary_role="onax",
        on_axis_deg=-7.0,
        frame_by_angle=frames,
        crossover_region_hz=(1000.0, 4000.0),
    )
    view = scored.view("onax")
    assert view is not None

    seat_worsts = [
        next(c for c in seat.chunks if c.f_lo_hz <= 1100.0 <= c.f_hi_hz).worst_db
        for seat in scored.per_seat
    ]
    assert len(seat_worsts) == 2
    # The seats really do disagree in sign — otherwise there is nothing to
    # cancel and the assertions below would pass against any implementation.
    assert min(seat_worsts) < -2.5 < 2.5 < max(seat_worsts)
    would_be_mean = sum(seat_worsts) / len(seat_worsts)
    assert abs(would_be_mean) < 0.2

    role_chunk = next(
        c for c in view.profile.chunks if c.f_lo_hz <= 1100.0 <= c.f_hi_hz
    )
    # THE PIN: the worst survives at full magnitude...
    assert abs(role_chunk.worst_db) == pytest.approx(
        max(abs(w) for w in seat_worsts), rel=1e-12,
    )
    assert abs(role_chunk.worst_db) > 2.5
    # ...and is an order of magnitude away from the average that would hide it.
    assert abs(role_chunk.worst_db) > 10.0 * abs(would_be_mean)
    assert view.profile.worst_chunk_db == role_chunk.worst_db

    # The trap itself, made visible rather than assumed: the LEVEL does cancel.
    assert abs(role_chunk.level_db) < 0.5


def test_a_frame_from_another_floor_is_refused_not_reconciled() -> None:
    """Grading at one floor and profiling at another states two spans as one."""
    freqs = _grid()
    graded = _graded(freqs, _flat(freqs), floor_hz=FLOOR_HZ)

    matched = flatness_profile(
        graded,
        frame=GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ),
        view="probe",
    )
    mismatched = flatness_profile(
        graded,
        frame=GradingFrame.self_referenced(trusted_floor_hz=800.0),
        view="probe",
    )

    assert matched.evaluable
    assert mismatched.evaluable is False
    assert "trusted floor" in mismatched.not_evaluated_reason
    # "Not stated" cannot disagree with anything — the pre-#2551 rehydration
    # case, which must still profile rather than refuse.
    assert flatness_profile(
        graded,
        frame=GradingFrame.self_referenced(trusted_floor_hz=None),
        view="probe",
    ).evaluable


def test_a_curve_off_the_shared_grid_is_refused_by_name() -> None:
    """One grid for every angle, and a mismatch says so rather than mis-grading."""
    freqs = _grid(1024)
    frames = {
        angle: GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ)
        for angle in (0.0, 20.0)
    }

    scored = score_prediction(
        {0.0: _flat(freqs), 20.0: _flat(_grid(512))},
        freqs,
        role_by_angle={0.0: "onax", 20.0: "offax"},
        primary_role="onax",
        on_axis_deg=0.0,
        frame_by_angle=frames,
        crossover_region_hz=(1000.0, 4000.0),
    )

    assert scored.view("onax") is not None
    assert scored.view("offax") is None
    assert any(
        "shared frequency grid" in why for _view, why in scored.not_evaluated
    )


def test_the_freeze_adds_one_constant_to_every_deviation() -> None:
    """The S8.9 identity, asserted rather than assumed.

    ``deviation_frozen = deviation_shipped + (own_ref - base_ref)``. If that is
    ever false, the freeze has started changing the SHAPE of the grade rather
    than only its level frame, which is not the operation that was ratified.
    """
    freqs = _grid()
    baseline_curve = _flat(freqs, -20.0)
    later_curve = _bump(freqs, _flat(freqs, -20.0), (2000.0, 8000.0), -2.0)

    baseline = _graded(freqs, baseline_curve)
    later = _graded(freqs, later_curve)

    self_profile = flatness_profile(
        later,
        frame=GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ),
        view="probe",
    )
    frozen_profile = flatness_profile(
        later, frame=GradingFrame.frozen_to(baseline.report), view="probe",
    )

    assert self_profile.reference_policy == "self"
    assert frozen_profile.reference_policy == "frozen"
    delta = later.report.reference_db - baseline.report.reference_db
    assert delta != pytest.approx(0.0, abs=1e-6)
    for before, after in zip(self_profile.chunks, frozen_profile.chunks):
        assert after.f_lo_hz == before.f_lo_hz
        assert after.level_db == pytest.approx(before.level_db + delta, rel=1e-12)


def test_a_broadband_cut_scores_worse_frozen_than_self_referenced() -> None:
    """The flattery, pinned as a number rather than asserted as a risk.

    A cut lowers the curve's own power mean, so a self-referenced grade measures
    the cut against the level the cut itself produced and gives part of the loss
    back. The freeze removes exactly that degree of freedom, so the same cut
    must score WORSE frozen.
    """
    freqs = _grid()
    baseline = _graded(freqs, _flat(freqs, -20.0))
    cut = _graded(freqs, _bump(freqs, _flat(freqs, -20.0), (250.0, 8000.0), -1.5))

    self_referenced = flatness_profile(
        cut, frame=GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ),
        view="probe",
    )
    frozen = flatness_profile(
        cut, frame=GradingFrame.frozen_to(baseline.report), view="probe",
    )

    assert frozen.rms_db > self_referenced.rms_db
    assert frozen.worst_chunk_abs_db > self_referenced.worst_chunk_abs_db


def test_the_freeze_is_per_seat_and_a_frame_carries_its_own_floor() -> None:
    freqs = _grid()
    quiet = _graded(freqs, _flat(freqs, -30.0))
    loud = _graded(freqs, _flat(freqs, -10.0))

    quiet_frame = GradingFrame.frozen_to(quiet.report)
    loud_frame = GradingFrame.frozen_to(loud.report)

    assert quiet_frame.reference_db != loud_frame.reference_db
    assert quiet_frame.trusted_floor_hz == FLOOR_HZ
    # A single global freeze would state one seat's level in the other's frame,
    # which injects the directivity difference straight into the grade.
    assert loud_frame.reference_db - quiet_frame.reference_db == pytest.approx(
        20.0, abs=1e-6,
    )


# --------------------------------------------------------------------------
# Acceptance semantics
# --------------------------------------------------------------------------


def test_the_chunk_regression_bar_is_the_repos_own_material_bar() -> None:
    assert CHUNK_REGRESSION_BAR_DB == PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB


def test_a_candidate_that_trades_a_region_does_not_dominate() -> None:
    """No buying a better average by making one region worse."""
    freqs = _grid()
    incumbent = _profile(freqs, _bump(freqs, _flat(freqs), (700.0, 900.0), +2.0))
    traded = _bump(freqs, _flat(freqs), (700.0, 900.0), +0.2)
    traded = _bump(freqs, traded, (5000.0, 6300.0), -3.0)
    candidate = _profile(freqs, traded)

    verdict = dominates(
        candidate, incumbent,
        candidate_residual_db=0.10,
        incumbent_residual_db=2.00,
    )

    assert verdict.dominates is False
    assert verdict.refusal == "chunk_regressed_past_the_bar"
    assert verdict.worst_regression_db > CHUNK_REGRESSION_BAR_DB
    assert 4500.0 <= verdict.worst_regression_hz <= 6500.0


def test_regression_is_measured_on_the_worst_bin_not_on_the_chunks_average() -> None:
    """A chunk that develops a NOTCH has regressed, however its mean moves.

    ``dominates``' own docstring makes exactly this claim — "a chunk that
    develops a notch has regressed even if its mean and its RMS barely move,
    and the notch is what a listener finds" — and until this test nothing
    checked it. This is round-1's seat-axis finding one axis over: the same
    never-let-an-average-hide-structure rule, applied to the BINS inside one
    chunk rather than to the SEATS inside one role.

    The pair below is the discriminator. The chunk at 1000-1260 Hz starts
    uniformly +2.0 dB hot. The candidate flattens its MEAN to -0.9 dB — a
    1.1 dB improvement on that axis — and buys it by cutting a -2.8 dB notch
    into the same third-octave. On the mean the candidate looks like a win
    everywhere; on the worst bin it has traded a region away, which is the one
    thing acceptance may not be sold.

    Verified by mutation (2026-08-19): taking the metric on ``level_db`` makes
    the worst regression anywhere read -0.05 dB and ACCEPTS this candidate;
    on ``rms_db`` it reads -0.07 dB and ACCEPTS it; on ``worst_db`` it reads
    +0.80 dB and refuses. Under either mutant this test is the only one in the
    file — and in ``test_crossover_search.py`` — that fails.
    """
    step = 2.0 ** (1.0 / CHUNK_FRACTION)
    trap_hz = (1000.0, 1000.0 * step)
    calm_hz = (1000.0 * step, 1000.0 * step * step)

    incumbent_chunks = (
        _chunk(*trap_hz, level_db=+2.00, worst_db=+2.00, rms_db=2.00),
        _chunk(*calm_hz, level_db=+0.10, worst_db=+0.20, rms_db=0.15),
    )
    candidate_chunks = (
        _chunk(*trap_hz, level_db=-0.90, worst_db=-2.80, rms_db=1.50),
        _chunk(*calm_hz, level_db=+0.05, worst_db=+0.10, rms_db=0.08),
    )

    def worst_regression_on(field: str) -> float:
        """What the metric would report if it were taken on ``field``."""
        return max(
            abs(getattr(after, field)) - abs(getattr(before, field))
            for after, before in zip(candidate_chunks, incumbent_chunks)
        )

    # ``dominates`` matches chunks by their EDGES; the counterfactual above
    # matches by position. Identical edge lists is what makes the two the same
    # pairing, so the numbers below really are the ones the rule would see.
    assert [(c.f_lo_hz, c.f_hi_hz) for c in candidate_chunks] == [
        (c.f_lo_hz, c.f_hi_hz) for c in incumbent_chunks
    ]

    # NON-VACUITY, as numbers rather than as a claim: on the two axes the
    # docstring says are blind to a notch, this candidate improves EVERYWHERE
    # and clears the bar comfortably. If either of these ever went positive the
    # pair would have stopped discriminating and the pin below would be empty.
    assert worst_regression_on("level_db") == pytest.approx(-0.05)
    assert worst_regression_on("rms_db") == pytest.approx(-0.07)
    assert worst_regression_on("level_db") < CHUNK_REGRESSION_BAR_DB
    assert worst_regression_on("rms_db") < CHUNK_REGRESSION_BAR_DB
    # ...and in the trap chunk itself the two axes disagree in SIGN, not merely
    # in size: the mean improves by 1.1 dB while the worst bin regresses by 0.8.
    assert abs(candidate_chunks[0].level_db) - abs(
        incumbent_chunks[0].level_db
    ) == pytest.approx(-1.10)
    assert worst_regression_on("worst_db") == pytest.approx(+0.80)
    assert worst_regression_on("worst_db") > CHUNK_REGRESSION_BAR_DB

    verdict = dominates(
        _profile_of(candidate_chunks),
        _profile_of(incumbent_chunks),
        candidate_residual_db=0.80,
        incumbent_residual_db=2.00,
    )

    # The primary residual really does improve past its own bar, so the refusal
    # below is the CHUNK rule biting and not the primary rule standing in for it.
    assert verdict.primary_delta_db == pytest.approx(-1.20)
    assert verdict.primary_delta_db < -PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB

    # THE PIN: measured on the worst bin, refused, and localized to the chunk
    # that grew the notch rather than to the calm one.
    assert verdict.dominates is False
    assert verdict.refusal == "chunk_regressed_past_the_bar"
    assert verdict.worst_regression_db == pytest.approx(+0.80)
    assert verdict.worst_regression_hz == pytest.approx(candidate_chunks[0].worst_hz)


def test_a_candidate_that_helps_everywhere_dominates() -> None:
    """The positive control for the rule above."""
    freqs = _grid()
    incumbent = _profile(freqs, _bump(freqs, _flat(freqs), (700.0, 900.0), +2.0))
    candidate = _profile(freqs, _bump(freqs, _flat(freqs), (700.0, 900.0), +0.2))

    verdict = dominates(
        candidate, incumbent,
        candidate_residual_db=0.10,
        incumbent_residual_db=2.00,
    )

    assert verdict.dominates is True
    assert verdict.refusal == ""
    assert verdict.primary_delta_db == pytest.approx(-1.90)


def test_an_improvement_under_the_bar_is_not_material() -> None:
    freqs = _grid()
    profile = _profile(freqs, _flat(freqs))

    verdict = dominates(
        profile, profile,
        candidate_residual_db=1.00 - (PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB / 2.0),
        incumbent_residual_db=1.00,
    )

    assert verdict.dominates is False
    assert verdict.refusal == "primary_did_not_improve"


def test_incomparable_chunk_sets_refuse_rather_than_match_by_position() -> None:
    """Two chunkings are not two chunk-by-chunk comparable profiles."""
    freqs = _grid()
    fine = _profile(freqs, _flat(freqs), floor_hz=FLOOR_HZ)
    coarse = _profile(freqs, _flat(freqs), floor_hz=2000.0)

    verdict = dominates(
        fine, coarse, candidate_residual_db=0.1, incumbent_residual_db=5.0,
    )

    assert verdict.dominates is False
    assert verdict.refusal == "chunk_sets_are_not_comparable"


def test_an_unevaluable_profile_refuses_rather_than_wins() -> None:
    freqs = _grid()
    profile = _profile(freqs, _flat(freqs))

    verdict = dominates(
        profile, profile, candidate_residual_db=None, incumbent_residual_db=1.0,
    )

    assert verdict.dominates is False
    assert verdict.refusal == "a_profile_was_not_evaluable"


# --------------------------------------------------------------------------
# The DI-continuity term
# --------------------------------------------------------------------------


def test_a_smoothly_narrowing_beam_has_no_kink() -> None:
    """A steady off-axis rolloff is what a directional speaker does."""
    freqs = _grid()
    on_axis = _flat(freqs)
    taper = on_axis - 1.2 * np.log2(np.maximum(freqs, 1.0) / 1000.0)

    result = directivity_continuity(
        {0.0: on_axis, 20.0: taper}, freqs,
        on_axis_deg=0.0, region_hz=(1000.0, 4000.0),
    )

    assert result.evaluable
    assert result.kink_db == pytest.approx(0.0, abs=0.02)
    assert result.coverage == DI_COVERAGE_HORIZONTAL_ONLY


def test_a_lobe_reaimed_at_the_crossover_registers_as_a_kink() -> None:
    """The measured failure: on-axis flatness bought at off-axis cost."""
    freqs = _grid()
    on_axis = _flat(freqs)
    reaimed = _bump(freqs, _flat(freqs), (1700.0, 2400.0), -6.0)

    smooth = directivity_continuity(
        {0.0: on_axis, 20.0: on_axis - 1.2 * np.log2(np.maximum(freqs, 1.0) / 1000.0)},
        freqs, on_axis_deg=0.0, region_hz=(1000.0, 4000.0),
    )
    kinked = directivity_continuity(
        {0.0: on_axis, 20.0: reaimed}, freqs,
        on_axis_deg=0.0, region_hz=(1000.0, 4000.0),
    )

    assert kinked.kink_db > 1.0
    assert kinked.kink_db > 10.0 * smooth.kink_db


def test_opposite_kinks_at_two_angles_do_not_cancel() -> None:
    """The never-collapse rule, one dimension over from the seats."""
    freqs = _grid()
    on_axis = _flat(freqs)
    up = _bump(freqs, _flat(freqs), (1700.0, 2400.0), +5.0)
    down = _bump(freqs, _flat(freqs), (1700.0, 2400.0), -5.0)

    result = directivity_continuity(
        {0.0: on_axis, -20.0: up, 20.0: down}, freqs,
        on_axis_deg=0.0, region_hz=(1000.0, 4000.0),
    )

    assert result.kink_db > 1.0
    assert len(result.per_angle_db) == 2
    # Each angle carries its own magnitude, and both are large.
    assert all(kink > 1.0 for _angle, kink in result.per_angle_db)


def test_no_on_axis_reference_is_unevaluable_never_flat() -> None:
    freqs = _grid()
    result = directivity_continuity(
        {20.0: _flat(freqs)}, freqs, on_axis_deg=0.0, region_hz=(1000.0, 4000.0),
    )

    assert result.evaluable is False
    assert result.kink_db is None
    assert "on-axis" in result.not_evaluated_reason


# --------------------------------------------------------------------------
# The advisory stamp — what the weighted total is allowed to claim
# --------------------------------------------------------------------------


def test_the_weights_stamp_themselves_uncalibrated_on_every_score() -> None:
    """``calibrated`` is ``False``, and it rides the score a consumer reads.

    :class:`ObjectiveWeights` owns what the stamp means — that while it reads
    ``False`` the weighted total is "a SORT KEY and nothing else", and what
    would have to happen for it to read otherwise. Not restated here; this pins
    what it READS, which is the half a test can hold. It is an honesty stamp
    with the same job as the shortlist's ``may_rank``, and ``may_rank`` is
    pinned, so this one is too: a flag nothing asserts is a flag an edit can
    flip without ever meaning to claim calibration.

    Both halves matter — that it reads ``False``, and that it RIDES every scored
    result. The second is what makes it unmissable: a consumer reads ``total``
    off the score and finds the qualifier in the same place. A change that
    genuinely calibrates these weights comes through this test, not past it.
    """
    assert ObjectiveWeights().calibrated is False
    assert ADVISORY_WEIGHTS.calibrated is False
    assert ADVISORY_WEIGHTS.to_dict()["calibrated"] is False

    freqs = _grid()
    scored = score_prediction(
        {0.0: _flat(freqs), 20.0: _bump(freqs, _flat(freqs), (4000.0, 5040.0), -6.0)},
        freqs,
        role_by_angle={0.0: "onax", 20.0: "offax"},
        primary_role="onax",
        on_axis_deg=0.0,
        frame_by_angle={
            angle: GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ)
            for angle in (0.0, 20.0)
        },
        crossover_region_hz=(1000.0, 4000.0),
    )

    # There IS a total here — the stamp is qualifying a real number, not
    # standing beside a ``None`` that nobody could have ranked on anyway.
    assert scored.total is not None
    assert scored.weights is ADVISORY_WEIGHTS
    assert scored.weights.calibrated is False
    assert scored.to_dict()["weights"]["calibrated"] is False


# --------------------------------------------------------------------------
# Mutation control — a score that does not move is not a score
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("band_hz", "amount_db"),
    [
        ((1000.0, 1260.0), 0.30),
        ((5000.0, 6300.0), -0.30),
        ((400.0, 500.0), 0.30),
    ],
)
def test_a_small_perturbation_moves_every_headline_number(band_hz, amount_db) -> None:
    """A test that passes against a stubbed objective guards nothing."""
    freqs = _grid()
    base = _profile(freqs, _flat(freqs))
    moved = _profile(freqs, _bump(freqs, _flat(freqs), band_hz, amount_db))

    assert moved.rms_db > base.rms_db
    assert moved.worst_chunk_abs_db > base.worst_chunk_abs_db
