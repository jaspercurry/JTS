# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.active_speaker.flat_spec_views.

Every numeric expectation here is either hand-computed in the test from the
formula in the module's docstring, or a structural invariant. The band counts
and edges the fixtures use are the 2026-08-18 jts3 arm-run's real ones
(1121 / 4096 / 5462 bins over 357.14-2000 / 2000-8000 / 8000-16000 Hz), so the
weighting ratios these tests pin are the ratios that corpus actually produced
— but no test reads the corpus, so none of them is gated on it being present.

Two promises the module makes that these tests exist to hold:

* **A view never grades.** No result type carries a pass/fail, and none is
  allowed to grow one — `test_no_view_publishes_a_verdict` walks the
  serialised output of all three and fails on a `passed` key.
* **Not-evaluated never collapses.** A missing residual is `None` with a
  reason attached, never `0.0` and never a pass, mirroring the first-class
  `CLAIM_NOT_EVALUATED` outcome issue #1868 keeps distinct from both.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from jasper.active_speaker.flat_spec import BandResult, ConvergenceResidual, FlatSpecReport
from jasper.active_speaker.flat_spec_views import (
    PositionCurve,
    directivity_table,
    log_pooled_residual,
    role_split_flatness,
)

ONAX = "onax"
OFFAX = "offax"

# The arm-run's own graded geometry: (f_lo, f_hi, graded_lo, n_bins).
CORPUS_BANDS = (
    (250.0, 2000.0, 357.14285714285717, 1121),
    (2000.0, 8000.0, 2000.0, 4096),
    (8000.0, 16000.0, 8000.0, 5462),
)
TRUSTED_FLOOR_HZ = 357.14285714285717


def _report(
    rms_per_band: tuple[float | None, ...],
    *,
    excluded: int = 0,
    intervals: tuple[tuple[float, float], ...] = (),
) -> FlatSpecReport:
    """A report carrying the corpus geometry and the given per-band RMS values.

    ``None`` for a band's RMS makes it unevaluable — the shape a band left with
    no non-excluded bin produces. ``intervals`` are the report's published
    exclusion spans, which the per-position views re-apply to each member
    curve.
    """
    bands = tuple(
        BandResult(
            f_lo_hz=f_lo,
            f_hi_hz=f_hi,
            tolerance_db=2.0,
            max_deviation_db=None if rms is None else rms,
            max_deviation_hz=None if rms is None else (f_lo + f_hi) / 2.0,
            rms_deviation_db=rms,
            n_bins=n_bins,
            n_excluded=excluded,
            evaluable=rms is not None,
            passed=None if rms is None else True,
            graded_lo_hz=graded_lo,
        )
        for (f_lo, f_hi, graded_lo, n_bins), rms in zip(CORPUS_BANDS, rms_per_band, strict=True)
    )
    return FlatSpecReport(
        reference_db=-24.0,
        bands=bands,
        overall_passed=False,
        excluded_intervals=intervals,
        best_effort_above_hz=16000.0,
        smoothing_fraction=3,
        trusted_floor_hz=TRUSTED_FLOOR_HZ,
        reference_band_hz=(TRUSTED_FLOOR_HZ, 8000.0),
    )


def _log_grid(n: int = 89, lo_hz: float = 142.8571) -> np.ndarray:
    """The corpus's own 1/12-octave grid shape: 89 points from ~143 Hz up."""
    return lo_hz * np.power(2.0, np.arange(n) / 12.0)


def _flat_curve(grid: np.ndarray, level_db: float = -24.0) -> np.ndarray:
    return np.full(len(grid), level_db, dtype=float)


def _hand_power_mean_db(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))``, re-implemented fresh in this file.

    Never the module's own helper: the point is to catch a regression to the
    linear-dB-mean-vs-power-mean trap this repo has shipped at least three
    times, and a test calling the implementation it is checking would catch
    nothing. Same reason `tests/test_flat_spec.py` carries its own copy.
    """
    total = 0.0
    for value in values_db:
        total += 10.0 ** (float(value) / 10.0)
    return 10.0 * math.log10(total / len(values_db))


def _position(
    position_id: str,
    role: str,
    grid: np.ndarray,
    curve: np.ndarray,
    *,
    degrees: float | None = None,
) -> PositionCurve:
    return PositionCurve(
        position_id=position_id,
        role=role,
        freqs_hz=grid,
        magnitude_db=curve,
        smoothing_fraction=6,
        degrees=degrees,
        take_id=f"{position_id}_a01",
    )


# --------------------------------------------------------------------------
# (a) the linear grid's per-octave overweight, and the log view removing it
# --------------------------------------------------------------------------


def test_linear_grid_overweights_the_top_octave_about_twelve_to_one() -> None:
    """The skew, read straight off the band table.

    5,462 bins in the one octave 8-16 kHz against 1,121 bins in the 2.485
    graded octaves of 250 Hz-2 kHz is 5462 vs 451 bins per octave. That ratio
    is what makes the shipped pooled number a top-octave number.
    """
    view = log_pooled_residual(_report((0.5, 0.5, 0.5)))
    low, mid, high = view.bands
    assert low.octaves == pytest.approx(math.log2(2000.0 / TRUSTED_FLOOR_HZ), rel=1e-9)
    assert high.octaves == pytest.approx(1.0, rel=1e-9)
    # Direction and rough magnitude, not a brittle decimal.
    ratio = high.bins_per_octave / low.bins_per_octave
    assert 11.0 < ratio < 13.0
    # ...and the top octave outweighs the whole midrange outright on bin count,
    # which is the reading a household's "it says not flat" comes from.
    assert high.linear_share > low.linear_share * 4.0
    # The log view inverts that: the midrange, spanning the most octaves, gets
    # the most weight.
    assert low.log_share > high.log_share


def test_log_pooling_gives_every_octave_the_same_weight() -> None:
    """``log_share`` is proportional to octaves, exactly — the whole claim."""
    view = log_pooled_residual(_report((0.5, 0.5, 0.5)))
    per_octave = {band.log_share / band.octaves for band in view.bands}
    assert len(view.bands) == 3
    assert max(per_octave) == pytest.approx(min(per_octave), rel=1e-12)
    assert sum(band.log_share for band in view.bands) == pytest.approx(1.0, rel=1e-12)
    assert sum(band.linear_share for band in view.bands) == pytest.approx(1.0, rel=1e-12)


def test_a_top_octave_deviation_moves_the_linear_pool_about_twelve_times_harder() -> None:
    """The headline claim, per octave of spectrum deviated.

    Take a flat 0.5 dB baseline and raise ONE band to 1.5 dB. The pooled
    mean-square rises by that band's weight share times ``1.5**2 - 0.5**2``,
    so dividing the rise by the band's octave span gives the influence per
    octave deviated — the only fair comparison between a 1-octave band and a
    2.485-octave one.

    Linear pooling hands the top octave about 12x the influence per octave;
    log pooling hands both exactly the same, which is what "equal weight per
    octave" means.
    """
    baseline = _report((0.5, 0.5, 0.5))
    bumped_top = _report((0.5, 0.5, 1.5))
    bumped_mid = _report((1.5, 0.5, 0.5))

    def rise_per_octave(view_bumped, view_base, octaves: float) -> tuple[float, float]:
        linear = (view_bumped.linear_rms_db ** 2 - view_base.linear_rms_db ** 2) / octaves
        logged = (view_bumped.rms_db ** 2 - view_base.rms_db ** 2) / octaves
        return linear, logged

    base = log_pooled_residual(baseline)
    top = log_pooled_residual(bumped_top)
    mid = log_pooled_residual(bumped_mid)
    top_octaves = base.bands[2].octaves
    mid_octaves = base.bands[0].octaves

    top_linear, top_log = rise_per_octave(top, base, top_octaves)
    mid_linear, mid_log = rise_per_octave(mid, base, mid_octaves)

    # Both deviations are real and both move both numbers up.
    assert top_linear > 0 and mid_linear > 0 and top_log > 0 and mid_log > 0
    # Linear: the top octave dominates, by roughly the bins-per-octave ratio.
    assert 11.0 < top_linear / mid_linear < 13.0
    # Log: the same deviation per octave counts the same wherever it sits.
    assert top_log / mid_log == pytest.approx(1.0, rel=1e-9)


def test_the_shipped_number_is_lifted_not_recomputed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``linear_rms_db`` is whatever the ONE pooler says, not a second sum.

    Equality with the real `spec_convergence_residual` is necessary but not
    sufficient — a faithful re-implementation would satisfy it and still be a
    second owner. So the pooler is replaced with one returning a value no
    arithmetic over this report could produce: if `linear_rms_db` follows it,
    the view is genuinely *calling* the shipped pooler rather than agreeing
    with it. The per-octave figure must NOT follow, because that one is this
    module's own.
    """
    from jasper.active_speaker import flat_spec_views
    from jasper.active_speaker.flat_spec import spec_convergence_residual

    report = _report((0.7135, 0.5089, 0.9116))
    honest = log_pooled_residual(report)
    assert honest.linear_rms_db == spec_convergence_residual(report).rms_db

    monkeypatch.setattr(
        flat_spec_views,
        "spec_convergence_residual",
        lambda _report_arg: ConvergenceResidual(
            rms_db=1234.5, n_bins=99, n_excluded=7, evaluable=True,
        ),
    )
    patched = flat_spec_views.log_pooled_residual(report)
    assert patched.linear_rms_db == 1234.5
    # ...and the log pooling is untouched by it, being this module's own work.
    assert patched.rms_db == honest.rms_db


def test_log_pooling_is_hand_computable_from_the_band_table() -> None:
    """One fully hand-computed value, so the identity is pinned and not just
    self-consistent."""
    report = _report((0.7135, 0.5089, 0.9116))
    view = log_pooled_residual(report)
    w_low = math.log2(2000.0 / TRUSTED_FLOOR_HZ)
    expected = math.sqrt(
        (w_low * 0.7135 ** 2 + 2.0 * 0.5089 ** 2 + 1.0 * 0.9116 ** 2) / (w_low + 2.0 + 1.0),
    )
    assert view.rms_db == pytest.approx(expected, rel=1e-12)
    # ...and the shipped pooling over the same bands is a different number.
    assert view.linear_rms_db is not None
    assert abs(view.rms_db - view.linear_rms_db) > 0.05


def test_the_log_view_is_not_a_flattering_machine() -> None:
    """It can raise the residual, and must be allowed to.

    When the MIDRANGE is the worst band, giving the midrange its honest octave
    weight makes the number worse. A view that only ever improved the figure
    would be a marketing device; this one reports what the weighting says.
    """
    view = log_pooled_residual(_report((2.1209, 1.1023, 1.0329)))
    assert view.linear_rms_db is not None and view.rms_db is not None
    assert view.rms_db > view.linear_rms_db


# --------------------------------------------------------------------------
# (b) an off-axis-only deviation must not touch the on-axis headline
# --------------------------------------------------------------------------


def _cloud(
    grid: np.ndarray, offax_bump_db: float, *, with_angles: bool = False,
) -> tuple[PositionCurve, ...]:
    """Two on-axis positions (fixed) and two off-axis ones (perturbable)."""
    onax_a = _flat_curve(grid)
    onax_b = _flat_curve(grid) + 0.2
    offax = _flat_curve(grid).copy()
    # A ripple whose size the caller controls, so only the off-axis half moves.
    offax += offax_bump_db * np.sin(np.arange(len(grid), dtype=float))
    return (
        _position("p_on_a", ONAX, grid, onax_a, degrees=-7.0 if with_angles else None),
        _position("p_on_b", ONAX, grid, onax_b, degrees=7.0 if with_angles else None),
        _position("p_off_a", OFFAX, grid, offax, degrees=-22.0 if with_angles else None),
        _position("p_off_b", OFFAX, grid, offax - 0.1, degrees=22.0 if with_angles else None),
    )


def test_an_off_axis_deviation_leaves_the_on_axis_headline_untouched() -> None:
    grid = _log_grid()
    report = _report((0.7, 0.5, 0.9))
    quiet = role_split_flatness(report, _cloud(grid, 0.0), primary_role=ONAX)
    loud = role_split_flatness(report, _cloud(grid, 3.0), primary_role=ONAX)

    assert quiet.primary is not None and loud.primary is not None
    # The on-axis figure is IDENTICAL, not merely close: nothing off-axis feeds
    # into it at all.
    assert loud.primary.rms_db == quiet.primary.rms_db
    assert loud.primary.log_rms_db == quiet.primary.log_rms_db
    # ...while the off-axis figure moves a long way.
    quiet_off = quiet.others[0].log_rms_db
    loud_off = loud.others[0].log_rms_db
    assert quiet_off is not None and loud_off is not None
    assert loud_off > quiet_off + 1.0
    # And the primary is never a blend of the two roles.
    assert loud.primary.log_rms_db is not None
    assert loud.primary.log_rms_db < loud_off


def test_the_pooled_figure_travels_with_the_split_but_is_not_part_of_it() -> None:
    """The split carries the number it is explaining, lifted from the report."""
    report = _report((0.7135, 0.5089, 0.9116))
    split = role_split_flatness(report, _cloud(_log_grid(), 1.0), primary_role=ONAX)
    pooled = log_pooled_residual(report)
    assert split.pooled_rms_db == pooled.linear_rms_db
    assert split.pooled_log_rms_db == pooled.rms_db
    # The roles are separate entries, never summed or averaged into one.
    assert split.primary is not None
    assert [role.role for role in split.others] == [OFFAX]


def test_a_role_the_cloud_never_sampled_is_unsampled_not_flat() -> None:
    grid = _log_grid()
    onax_only = (_position("p_on", ONAX, grid, _flat_curve(grid)),)
    split = role_split_flatness(_report((0.7, 0.5, 0.9)), onax_only, primary_role="xovr")
    assert split.primary is None
    assert split.primary_role == "xovr"
    assert [role.role for role in split.others] == [ONAX]


# --------------------------------------------------------------------------
# (c) not-evaluated and missing data pass through visibly
# --------------------------------------------------------------------------


def test_a_band_with_no_residual_is_counted_not_pooled_as_zero() -> None:
    view = log_pooled_residual(_report((0.8, None, 0.8)))
    assert view.n_bands_not_evaluated == 1
    assert len(view.bands) == 2
    assert view.evaluable is True
    # Pooling two 0.8 dB bands gives 0.8, which a silently-zeroed third band
    # would have dragged down.
    assert view.rms_db == pytest.approx(0.8, rel=1e-12)


def test_a_report_with_no_evaluable_band_has_no_residual_not_a_zero_one() -> None:
    view = log_pooled_residual(_report((None, None, None)))
    assert view.rms_db is None
    assert view.evaluable is False
    assert view.n_bands_not_evaluated == 3
    assert view.bands == ()
    assert view.n_bins == 0


def test_a_position_the_evaluator_declines_keeps_its_place_and_its_reason() -> None:
    """A position that cannot be graded is present, `None`, and explained.

    Its grid stops below the report's trusted floor, so the reference band is
    empty and the evaluator raises. That must become a visible not-evaluated
    outcome, never a dropped row and never a 0.0 dB residual that would read as
    a perfect position.
    """
    grid = _log_grid()
    stunted = np.linspace(80.0, 300.0, 40)
    positions = (
        _position("p_on", ONAX, grid, _flat_curve(grid)),
        _position("p_on_bad", ONAX, stunted, _flat_curve(stunted)),
    )
    split = role_split_flatness(_report((0.7, 0.5, 0.9)), positions, primary_role=ONAX)
    assert split.primary is not None
    assert split.primary.n_positions == 2
    assert split.primary.n_evaluated == 1
    assert split.n_not_evaluated == 1
    bad = next(p for p in split.primary.positions if p.position_id == "p_on_bad")
    assert bad.evaluable is False
    assert bad.rms_db is None and bad.log_rms_db is None
    # The distinction the whole rule is about: absent, not zero.
    assert bad.rms_db != 0.0
    assert bad.not_evaluated_reason != ""
    # ...and the role's own pooled figure is built only from what was measured.
    good = next(p for p in split.primary.positions if p.position_id == "p_on")
    assert split.primary.rms_db == pytest.approx(good.rms_db, rel=1e-12)


def test_a_role_whose_every_position_declined_has_no_figure() -> None:
    stunted = np.linspace(80.0, 300.0, 40)
    positions = (_position("p_on_bad", ONAX, stunted, _flat_curve(stunted)),)
    split = role_split_flatness(_report((0.7, 0.5, 0.9)), positions, primary_role=ONAX)
    assert split.primary is not None
    assert split.primary.evaluable is False
    assert split.primary.rms_db is None and split.primary.log_rms_db is None
    assert split.primary.n_positions == 1 and split.primary.n_evaluated == 0


def test_directivity_without_a_reference_role_keeps_every_row_and_says_why() -> None:
    grid = _log_grid()
    positions = (
        _position("p_off_a", OFFAX, grid, _flat_curve(grid)),
        _position("p_off_b", OFFAX, grid, _flat_curve(grid)),
    )
    table = directivity_table(_report((0.7, 0.5, 0.9)), positions, reference_role=ONAX)
    assert table.evaluable is False
    assert table.not_evaluated_reason != ""
    # Nothing is dropped: a consumer counting rows sees what it handed in.
    assert len(table.rows) == 2
    assert all(not row.evaluable and row.not_evaluated_reason for row in table.rows)
    assert all(row.level_offset_db is None for row in table.rows)


def test_a_position_off_the_shared_grid_is_reported_not_resampled() -> None:
    grid = _log_grid()
    other = _log_grid(n=64)
    positions = (
        _position("p_on", ONAX, grid, _flat_curve(grid)),
        _position("p_off", OFFAX, other, _flat_curve(other)),
    )
    table = directivity_table(_report((0.7, 0.5, 0.9)), positions, reference_role=ONAX)
    assert table.evaluable is True
    stray = next(row for row in table.rows if row.position_id == "p_off")
    assert stray.evaluable is False
    assert "grid" in stray.not_evaluated_reason
    assert stray.normalized_db == ()


def test_no_view_publishes_a_verdict() -> None:
    """None of the three carries a pass/fail, and none may grow one.

    The session has exactly one verdict and one producer of it. A view that
    published its own would be a second grader by the back door — the failure
    mode issue #1868 closed at claim level, guarded here at view level.
    """
    grid = _log_grid()
    report = _report((0.7, 0.5, 0.9))
    positions = _cloud(grid, 1.0)
    payloads = [
        log_pooled_residual(report).to_dict(),
        role_split_flatness(report, positions, primary_role=ONAX).to_dict(),
        directivity_table(report, positions, reference_role=ONAX).to_dict(),
    ]
    for payload in payloads:
        blob = json.dumps(payload)
        assert '"passed"' not in blob
        assert '"overall_passed"' not in blob
        assert '"tolerance_db"' not in blob


# --------------------------------------------------------------------------
# (d) angles are optional; without them the views degrade to role-only
# --------------------------------------------------------------------------


def test_angles_change_the_label_and_nothing_else() -> None:
    """Every number is identical with and without a recorded angle.

    The corpus predates the cloud banking an angle, so the angle arrives (if at
    all) from a walk-log join that can fail. That must cost a label, never a
    measurement.
    """
    grid = _log_grid()
    report = _report((0.7, 0.5, 0.9))
    without = _cloud(grid, 1.0, with_angles=False)
    with_angles = _cloud(grid, 1.0, with_angles=True)

    split_without = role_split_flatness(report, without, primary_role=ONAX).to_dict()
    split_with = role_split_flatness(report, with_angles, primary_role=ONAX).to_dict()

    def _strip(payload: dict) -> dict:
        blob = json.loads(json.dumps(payload))
        for role in [blob["primary"], *blob["others"]]:
            for position in role["positions"]:
                position.pop("degrees")
        return blob

    assert _strip(split_without) == _strip(split_with)
    # ...and the angles really were absent in one and present in the other.
    assert all(
        p["degrees"] is None for p in split_without["primary"]["positions"]
    )
    assert all(
        p["degrees"] is not None for p in split_with["primary"]["positions"]
    )


def test_the_directivity_table_says_whether_its_angles_are_complete() -> None:
    grid = _log_grid()
    report = _report((0.7, 0.5, 0.9))
    full = directivity_table(report, _cloud(grid, 1.0, with_angles=True), reference_role=ONAX)
    none = directivity_table(report, _cloud(grid, 1.0, with_angles=False), reference_role=ONAX)
    assert full.angles_recorded is True
    assert none.angles_recorded is False
    # A partial join is not "recorded": one missing angle makes the table
    # role-labelled, so a consumer cannot read a `None` as 0 degrees.
    mixed = list(_cloud(grid, 1.0, with_angles=True))
    mixed[2] = _position("p_off_a", OFFAX, grid, mixed[2].magnitude_db, degrees=None)
    partial = directivity_table(report, tuple(mixed), reference_role=ONAX)
    assert partial.angles_recorded is False
    # The numbers are unaffected by the missing label.
    assert [row.level_offset_db for row in partial.rows] == [
        row.level_offset_db for row in full.rows
    ]


# --------------------------------------------------------------------------
# directivity arithmetic
# --------------------------------------------------------------------------


def test_a_lone_reference_position_normalises_to_exactly_zero() -> None:
    """With one on-axis position the reference IS that position."""
    grid = _log_grid()
    positions = (
        _position("p_on", ONAX, grid, _flat_curve(grid)),
        _position("p_off", OFFAX, grid, _flat_curve(grid) - 3.0),
    )
    table = directivity_table(_report((0.7, 0.5, 0.9)), positions, reference_role=ONAX)
    reference_row = next(row for row in table.rows if row.position_id == "p_on")
    assert reference_row.in_reference is True
    assert max(abs(v) for v in reference_row.normalized_db) == pytest.approx(0.0, abs=1e-12)
    assert reference_row.level_offset_db == pytest.approx(0.0, abs=1e-12)
    # A uniformly quieter off-axis position is all level and no shape.
    off_row = next(row for row in table.rows if row.position_id == "p_off")
    assert off_row.in_reference is False
    assert off_row.level_offset_db == pytest.approx(-3.0, abs=1e-9)
    for band in off_row.bands:
        assert band.evaluable is True
        assert band.level_offset_db == pytest.approx(-3.0, abs=1e-9)
        assert band.shape_rms_db == pytest.approx(0.0, abs=1e-9)
        assert band.shape_max_db == pytest.approx(0.0, abs=1e-9)


def test_the_directivity_split_adds_back_up() -> None:
    """``normalized = level_offset + shape`` per bin, per band — the identity
    the level/shape split is only meaningful under."""
    grid = _log_grid()
    tilted = _flat_curve(grid) - np.linspace(0.0, 6.0, len(grid))
    positions = (
        _position("p_on", ONAX, grid, _flat_curve(grid)),
        _position("p_off", OFFAX, grid, tilted),
    )
    report = _report((0.7, 0.5, 0.9))
    table = directivity_table(report, positions, reference_role=ONAX)
    off_row = next(row for row in table.rows if row.position_id == "p_off")
    normalized = np.asarray(off_row.normalized_db)
    freqs = np.asarray(table.freqs_hz)
    for band, source in zip(off_row.bands, report.bands, strict=True):
        assert band.evaluable is True
        assert band.level_offset_db is not None and band.shape_rms_db is not None
        graded_lo = source.graded_lo_hz or source.f_lo_hz
        inside = (freqs >= graded_lo) & (freqs < source.f_hi_hz)
        shape = normalized[inside] - band.level_offset_db
        assert band.shape_rms_db == pytest.approx(
            float(np.sqrt(np.mean(shape ** 2))), rel=1e-12,
        )
        # A downward tilt reads as a real level drop, worse the higher it goes.
        assert band.level_offset_db < 0.0
    assert off_row.bands[0].level_offset_db > off_row.bands[-1].level_offset_db


def test_the_reference_is_a_power_mean_across_positions_not_a_db_average() -> None:
    """Two on-axis positions at UNEQUAL levels, which is the only case that
    can tell the two means apart.

    Energy-averaging −30 and −20 dB lands at −22.596 dB, pulled toward the
    louder one, because that one carries ten times the power. Averaging the
    dB numbers lands at −25.0, halfway. Every fixture whose reference
    positions sit at the same level makes those identical, which is how a
    plain-average regression hides.

    The asymmetry is the signature: against a power-mean reference the two
    positions' offsets are −7.40 and +2.60 dB and do NOT cancel; against a
    dB-average one they would be exactly ∓5.
    """
    grid = _log_grid()
    positions = (
        _position("p_quiet", ONAX, grid, _flat_curve(grid, -30.0)),
        _position("p_loud", ONAX, grid, _flat_curve(grid, -20.0)),
    )
    table = directivity_table(_report((0.7, 0.5, 0.9)), positions, reference_role=ONAX)
    expected = _hand_power_mean_db(np.array([-30.0, -20.0]))
    assert expected == pytest.approx(-22.5963731, abs=1e-6)
    assert min(table.reference_db) == pytest.approx(expected, abs=1e-9)
    assert max(table.reference_db) == pytest.approx(expected, abs=1e-9)
    # Unmistakably not the dB average, which is what the trap produces.
    assert abs(expected - (-25.0)) > 2.0

    quiet = next(row for row in table.rows if row.position_id == "p_quiet")
    loud = next(row for row in table.rows if row.position_id == "p_loud")
    assert quiet.level_offset_db == pytest.approx(-30.0 - expected, abs=1e-9)
    assert loud.level_offset_db == pytest.approx(-20.0 - expected, abs=1e-9)
    assert quiet.level_offset_db is not None and loud.level_offset_db is not None
    # They sum to -4.807 dB, not to zero: the reference sits nearer the louder
    # position, so the quiet one is further from it than the loud one is.
    assert quiet.level_offset_db + loud.level_offset_db == pytest.approx(-4.807, abs=1e-2)


def test_the_level_offset_is_a_power_mean_across_frequency_not_a_db_average() -> None:
    """A position whose level swings inside the band, which is the only case
    that can tell those two apart along the frequency axis.

    A curve alternating between −40 and −20 dB averages to −30 dB as dB
    numbers, and to about −23 dB as energy — the loud bins dominate. So a
    position that a dB average calls "level with on-axis" is really some
    7 dB hotter, and every flat fixture reports the same number either way.
    """
    grid = _log_grid()
    swinging = np.where(np.arange(len(grid)) % 2 == 0, -40.0, -20.0)
    report = _report((0.7, 0.5, 0.9))
    positions = (
        _position("p_on", ONAX, grid, _flat_curve(grid, -30.0)),
        _position("p_swing", OFFAX, grid, swinging),
    )
    table = directivity_table(report, positions, reference_role=ONAX)
    row = next(r for r in table.rows if r.position_id == "p_swing")

    ref_lo, ref_hi = report.reference_band_hz
    in_band = (grid >= ref_lo) & (grid < ref_hi)
    expected = _hand_power_mean_db(swinging[in_band]) - _hand_power_mean_db(
        np.full(int(in_band.sum()), -30.0),
    )
    assert row.level_offset_db == pytest.approx(expected, abs=1e-9)
    # A dB average over the same bins would report roughly nothing at all.
    assert abs(float(np.mean(swinging[in_band])) - (-30.0)) < 1.0
    assert row.level_offset_db is not None and row.level_offset_db > 5.0

    # The same helper drives every per-band offset, so pin one of those too.
    band = row.bands[0]
    source = report.bands[0]
    graded_lo = source.graded_lo_hz or source.f_lo_hz
    inside = (grid >= graded_lo) & (grid < source.f_hi_hz)
    band_expected = _hand_power_mean_db(swinging[inside]) - _hand_power_mean_db(
        np.full(int(inside.sum()), -30.0),
    )
    assert band.level_offset_db == pytest.approx(band_expected, abs=1e-9)
    assert band.level_offset_db is not None and band.level_offset_db > 5.0


def test_the_reports_exclusion_intervals_are_applied_to_every_member_curve() -> None:
    """A mid-band excluded span must remove bins from the per-position views.

    The honesty screen's result is published as intervals, and the per-role
    numbers are only comparable to the pooled one if they are graded over the
    same region of spectrum. A view that ignored the intervals would grade the
    interference-dominated bins the pooled figure dropped, silently, and the
    two numbers would answer different questions.

    1-2 kHz sits inside the 250 Hz-2 kHz band, so excluding it must show up as
    a non-zero excluded count AND move the residual.
    """
    grid = _log_grid()
    # A curve that is flat except for a large bump exactly where the mask bites,
    # so removing those bins cannot fail to change the answer.
    bumped = _flat_curve(grid)
    bumped[(grid >= 1000.0) & (grid < 2000.0)] += 8.0
    positions = (_position("p_on", ONAX, grid, bumped),)

    masked = role_split_flatness(
        _report((0.7, 0.5, 0.9), intervals=((1000.0, 2000.0),)), positions, primary_role=ONAX,
    )
    unmasked = role_split_flatness(_report((0.7, 0.5, 0.9)), positions, primary_role=ONAX)
    assert masked.primary is not None and unmasked.primary is not None
    masked_position = masked.primary.positions[0]
    unmasked_position = unmasked.primary.positions[0]

    assert masked_position.n_excluded > 0
    assert unmasked_position.n_excluded == 0
    assert masked_position.n_bins < unmasked_position.n_bins
    assert masked_position.rms_db is not None and unmasked_position.rms_db is not None
    # The bump was the dominant deviation; dropping it must move the number a
    # long way, not by a rounding error.
    assert masked_position.rms_db < unmasked_position.rms_db - 1.0

    # The directivity table applies the same intervals, so its band bin counts
    # must shrink by the same evidence.
    table_masked = directivity_table(
        _report((0.7, 0.5, 0.9), intervals=((1000.0, 2000.0),)), positions, reference_role=ONAX,
    )
    table_unmasked = directivity_table(_report((0.7, 0.5, 0.9)), positions, reference_role=ONAX)
    assert table_masked.rows[0].bands[0].n_bins < table_unmasked.rows[0].bands[0].n_bins


def test_the_table_is_json_serialisable() -> None:
    """A prescriber consumes `to_dict()`; it must contain no numpy."""
    grid = _log_grid()
    table = directivity_table(
        _report((0.7, 0.5, 0.9)), _cloud(grid, 1.0, with_angles=True), reference_role=ONAX,
    )
    blob = json.dumps(table.to_dict())
    restored = json.loads(blob)
    assert len(restored["freqs_hz"]) == len(grid)
    assert len(restored["reference_db"]) == len(grid)
    assert len(restored["rows"]) == 4
    assert restored["reference_position_ids"] == ["p_on_a", "p_on_b"]
    assert len(restored["rows"][0]["normalized_db"]) == len(grid)
