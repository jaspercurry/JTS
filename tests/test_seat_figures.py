# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""Listening-position figures on synthetic curves and impulses."""
from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pytest

from jasper.audio_measurement import seat_figures
from jasper.audio_measurement.analysis import CANONICAL_SHOULDER_RATIOS

#: Speed of sound, m/s, for the image model below.
SPEED_OF_SOUND_M_S = 343.0
#: About 1/100 octave — fine enough for the 1/6-octave figures.
FREQS_HZ = np.geomspace(20.0, 1000.0, 600)
COVERAGE_HZ = (30.0, 800.0)
CEILING_HZ = 500.0
#: Front-to-wall distance, m, and the hand-over the caller declares.
WALL_M = 0.35
HANDOVER_HZ = 90.0

def _image_curve(
    *,
    rho: float = 0.8,
    wall_m: float = WALL_M,
    offset_db: float = 0.0,
    notch_hz: float | None = None,
    notch_db: float = 0.0,
    notch_octaves: float = 0.12,
) -> np.ndarray:
    """``20*log10|1 + rho*exp(-j*2*pi*f*2x/c)|``: a front source plus its wall
    image, with an optional Gaussian notch (a hand-over hole) and a constant
    offset (lost output)."""
    phase = 2.0 * np.pi * FREQS_HZ * 2.0 * wall_m / SPEED_OF_SOUND_M_S
    level = 20.0 * np.log10(np.abs(1.0 + rho * np.exp(-1j * phase))) + offset_db
    if notch_hz is not None:
        level = level - notch_db * np.exp(
            -(np.log2(FREQS_HZ / notch_hz) ** 2) / (2.0 * notch_octaves**2)
        )
    return level


def _first_null_hz(wall_m: float = WALL_M) -> float:
    """Where the image cancels the direct sound: ``c / 4x``."""
    return SPEED_OF_SOUND_M_S / (4.0 * wall_m)


def _batch(reference_curve: np.ndarray) -> tuple[dict[str, Any], np.ndarray]:
    """The batch's frozen comparison band and its one reference curve."""
    band = seat_figures.comparison_band(
        coverage_hz=COVERAGE_HZ,
        ceiling_hz=CEILING_HZ,
        reference_take=(FREQS_HZ, reference_curve),
    )
    return band, seat_figures.reference_curve_db(FREQS_HZ, reference_curve)


def _figures(
    curve: np.ndarray,
    band: dict[str, Any],
    reference: np.ndarray,
    *,
    incumbent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return seat_figures.position_figures(
        FREQS_HZ, curve, reference_db=reference, band_hz=band["band_hz"],
        coverage_hz=COVERAGE_HZ, handover_hz=HANDOVER_HZ, incumbent=incumbent,
    )


@pytest.mark.parametrize("wall_m", [0.25, WALL_M, 0.5])
def test_the_band_follows_the_measured_wall_dip(wall_m):
    band, _ = _batch(_image_curve(wall_m=wall_m))
    null_hz = _first_null_hz(wall_m)
    lo_ratio, hi_ratio = CANONICAL_SHOULDER_RATIOS
    assert band["source"] == seat_figures.BAND_SOURCE_MEASURED_DIP
    assert band["dip_hz"] == pytest.approx(null_hz, rel=0.1)
    assert band["band_hz"][0] == pytest.approx(null_hz * lo_ratio, rel=0.1)
    assert band["band_hz"][1] == pytest.approx(min(null_hz * hi_ratio, CEILING_HZ), rel=0.1)


def test_a_weaker_reflection_reads_a_shallower_dip():
    depths = []
    for rho in (0.8, 0.6, 0.4):
        curve = _image_curve(rho=rho)
        band, reference = _batch(curve)
        dip = _figures(curve, band, reference)["dip"]
        assert dip["hz"] == pytest.approx(_first_null_hz(), rel=0.1)
        depths.append(dip["depth_db"])
    assert depths == sorted(depths, reverse=True)
    assert depths[0] > depths[-1] + 2.0


@pytest.mark.parametrize(
    ("declared", "source", "band_hz"),
    [
        ({"geometric_dip_hz": 200.0}, seat_figures.BAND_SOURCE_DECLARED_GEOMETRY, (100.0, 400.0)),
        (
            {"geometric_dip_hz": 200.0, "section_band_hz": (60.0, 300.0)},
            seat_figures.BAND_SOURCE_DECLARED_GEOMETRY,
            (100.0, 400.0),
        ),
        ({"section_band_hz": (60.0, 300.0)}, seat_figures.BAND_SOURCE_SECTION_BAND, (60.0, 300.0)),
        ({}, seat_figures.BAND_SOURCE_COVERAGE, (COVERAGE_HZ[0], CEILING_HZ)),
        # Wholly above the coverage: a disclosed no-band, never an inverted range.
        ({"section_band_hz": (1000.0, 1200.0)}, seat_figures.BAND_SOURCE_SECTION_BAND, None),
    ],
)
def test_with_no_measurable_dip_the_band_falls_back_in_order(declared, source, band_hz):
    band = seat_figures.comparison_band(
        coverage_hz=COVERAGE_HZ, ceiling_hz=CEILING_HZ,
        reference_take=(FREQS_HZ, _image_curve(rho=0.0)), **declared,
    )
    assert (band["source"], band["dip_hz"]) == (source, None)
    assert band["reason"] == ("" if band_hz else seat_figures.REASON_COVERAGE_SHORT)
    assert band["band_hz"] == (None if band_hz is None else pytest.approx(list(band_hz)))


#: Two dips on one reference curve — issue #5330's own jts3 round: an
#: unwindowed search let a 12 dB room feature at 37.6 Hz outrank the
#: shallower dip near the true wall distance (~170 Hz).
_ROOM_MODE_HZ = 38.0
_WALL_DIP_HZ = 165.0


def _two_dip_curve(*, room_db: float = 12.0, wall_db: float = 5.0) -> np.ndarray:
    """No wall image (``rho=0``), just the two notches ``_image_curve``
    already knows how to cut, summed onto one flat curve."""
    return (_image_curve(rho=0.0, notch_hz=_ROOM_MODE_HZ, notch_db=room_db)
            + _image_curve(rho=0.0, notch_hz=_WALL_DIP_HZ, notch_db=wall_db))


@pytest.mark.parametrize(
    ("kwargs", "expected_hz", "search_hz"),
    [
        ({"geometric_dip_hz": 170.0}, _WALL_DIP_HZ,
         (170.0 / seat_figures.DIP_SEARCH_WINDOW_RATIO, 170.0 * seat_figures.DIP_SEARCH_WINDOW_RATIO)),
        ({"section_band_hz": (74.0, 344.0)}, _WALL_DIP_HZ, (74.0, 344.0)),
        ({"handover_hz": 66.0}, _WALL_DIP_HZ, (66.0, CEILING_HZ)),
        ({}, _ROOM_MODE_HZ, (COVERAGE_HZ[0], CEILING_HZ)),
    ],
)
def test_the_measured_dip_search_is_windowed_so_a_deeper_dip_elsewhere_loses(
    kwargs, expected_hz, search_hz,
):
    band = seat_figures.comparison_band(
        coverage_hz=COVERAGE_HZ, ceiling_hz=CEILING_HZ,
        reference_take=(FREQS_HZ, _two_dip_curve()), **kwargs,
    )
    assert band["source"] == seat_figures.BAND_SOURCE_MEASURED_DIP
    assert band["dip_hz"] == pytest.approx(expected_hz, rel=0.05)
    assert band["search_hz"] == pytest.approx(list(search_hz))


def test_filling_the_dip_while_digging_a_handover_hole_shows_both():
    band, reference = _batch(_image_curve(rho=0.8))
    incumbent = _figures(_image_curve(rho=0.8), band, reference)
    variant = _figures(
        _image_curve(rho=0.25, notch_hz=HANDOVER_HZ, notch_db=9.0),
        band, reference, incumbent=incumbent,
    )
    assert variant["dip"]["depth_db"] < incumbent["dip"]["depth_db"] - 3.0
    assert variant["handover"]["hole_db"] > incumbent["handover"]["hole_db"] + 8.0
    assert variant["handover"]["hole_hz"] == pytest.approx(HANDOVER_HZ, rel=0.1)
    # A packet embeds the row as it stands.
    assert json.loads(json.dumps(variant)) == variant


def test_a_quieter_candidate_keeps_its_shape_and_loses_band_level():
    band, reference = _batch(_image_curve())
    loud = _figures(_image_curve(), band, reference)
    quiet = _figures(_image_curve(offset_db=-3.0), band, reference, incumbent=loud)
    assert quiet["dip"] == pytest.approx(loud["dip"])
    assert quiet["ripple_db"] == pytest.approx(loud["ripple_db"])
    assert quiet["handover"]["hole_db"] == pytest.approx(loud["handover"]["hole_db"])
    assert quiet["band_level_db"] == pytest.approx(loud["band_level_db"] - 3.0)
    assert quiet["low_bass"]["change_db"] == pytest.approx(-3.0)


@pytest.mark.parametrize("notch_at", ["inside", "band_low_edge", "band_high_edge"])
def test_a_dip_at_another_frequency_is_reported_as_shifted(notch_at):
    band, reference = _batch(_image_curve(rho=0.8))
    incumbent = _figures(_image_curve(rho=0.8), band, reference)
    lo_hz, hi_hz = band["band_hz"]
    # A dip ON the band's own first or last SAMPLE is still a dip.
    in_band = FREQS_HZ[(FREQS_HZ >= lo_hz) & (FREQS_HZ < hi_hz)]
    shifted_hz = {"inside": incumbent["dip"]["hz"] * 1.5,
                  "band_low_edge": in_band[0], "band_high_edge": in_band[-1]}[notch_at]
    variant = _figures(
        _image_curve(rho=0.8, notch_hz=shifted_hz, notch_db=12.0),
        band, reference, incumbent=incumbent,
    )
    assert variant["dip"]["hz"] == pytest.approx(shifted_hz, rel=0.02)
    assert variant["dip_shift"]["hz"] == pytest.approx(shifted_hz, rel=0.02)
    assert variant["dip_shift"]["depth_db"] == pytest.approx(variant["dip"]["depth_db"])
    assert _figures(_image_curve(rho=0.8), band, reference, incumbent=incumbent)["dip_shift"] is None


def test_a_curve_that_only_slopes_toward_the_band_edge_has_no_dip():
    """The reviewer-style case issue #5330's wrong band exposed: a trend
    that merely falls toward a band edge, and keeps falling past it, is not
    a local minimum there — unlike the genuine edge dip above, the sample
    one bin outside the edge must show a rise for the edge to count."""
    reference = np.zeros_like(FREQS_HZ)
    curve = -12.0 * np.log2(FREQS_HZ / FREQS_HZ[0])
    row = seat_figures.position_figures(
        FREQS_HZ, curve, reference_db=reference, band_hz=(100.0, 300.0),
        coverage_hz=COVERAGE_HZ, handover_hz=HANDOVER_HZ,
    )
    assert row["dip"] is None


def test_one_bad_position_is_the_reported_worst_regression():
    band, reference = _batch(_image_curve(rho=0.8))
    positions = ("front", "left", "right")
    incumbent_rows = {key: _figures(_image_curve(rho=0.8), band, reference) for key in positions}
    curves = {
        key: _image_curve(rho=0.3, notch_hz=HANDOVER_HZ if key == "right" else None, notch_db=12.0)
        for key in positions
    }
    rows = {
        key: _figures(curves[key], band, reference, incumbent=incumbent_rows[key])
        for key in positions
    }
    summary = seat_figures.across_positions(
        rows, incumbent_rows=incumbent_rows,
        spread_db=seat_figures.repeat_spread([rows["front"], rows["front"]])["spread_db"],
    )
    worst = summary["worst_regression"]
    assert (worst["position"], worst["figure"]) == ("right", "handover.hole_db")
    assert worst["change_db"] > 8.0
    assert worst["exceeds_repeat_spread"] is True
    hole = summary["figures"]["handover.hole_db"]
    assert hole["worst_db"] > hole["median_db"] + 8.0
    assert (summary["positions"], summary["positions_unavailable"]) == (3, {})
    # A position this candidate was never measured at is disclosed, not dropped.
    partial = seat_figures.across_positions(
        {key: row for key, row in rows.items() if key != "left"}, incumbent_rows=incumbent_rows)
    assert partial["positions"] == 3
    assert partial["positions_unavailable"] == {"left": seat_figures.REASON_NO_ROW}


@pytest.mark.parametrize(
    ("n_repeats", "offset_db", "expected_level_spread_db"),
    [(0, 0.0, None), (1, 0.0, None), (2, 0.0, 0.0), (2, -1.0, 1.0)],
)
def test_repeat_spread_comes_only_from_repeats(n_repeats, offset_db, expected_level_spread_db):
    band, reference = _batch(_image_curve())
    rows = [
        _figures(_image_curve(), band, reference),
        _figures(_image_curve(offset_db=offset_db), band, reference),
    ]
    spread = seat_figures.repeat_spread(rows[:n_repeats])
    assert spread["n_repeats"] == n_repeats
    assert spread["reason"] == ("" if n_repeats > 1 else seat_figures.REASON_NO_REPEATS)
    if expected_level_spread_db is None:
        assert set(spread["spread_db"].values()) == {None}
    else:
        assert spread["spread_db"]["band_level_db"] == pytest.approx(expected_level_spread_db)
        assert spread["spread_db"]["ripple_db"] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("missing", ["short_grid", "no_band"])
def test_a_curve_that_misses_the_band_reports_coverage_short(missing):
    curve = _image_curve()
    band, reference = _batch(curve)
    keep = FREQS_HZ < band["band_hz"][1] if missing == "short_grid" else FREQS_HZ > 0.0
    row = seat_figures.position_figures(
        FREQS_HZ[keep], curve[keep], reference_db=reference[keep],
        band_hz=None if missing == "no_band" else band["band_hz"],
        coverage_hz=COVERAGE_HZ, handover_hz=HANDOVER_HZ,
    )
    assert row["reason"] == seat_figures.REASON_COVERAGE_SHORT
    assert all(
        row[key] is None
        for key in ("dip", "dip_shift", "ripple_db", "own_trend_ripple_db", "handover", "low_bass", "band_level_db")
    )
    summary = seat_figures.across_positions({"front": row}, incumbent_rows={"front": row})
    assert summary["positions_unavailable"] == {"front": seat_figures.REASON_COVERAGE_SHORT}
    assert summary["worst_regression"] is None


def test_impulse_energy_windows_follow_the_own_peak():
    impulse = np.zeros(4800)
    impulse[480], impulse[1440] = 1.0, 0.5

    assert seat_figures.impulse_energy_figures(impulse, sample_rate_hz=48000) == pytest.approx({
        "t0_ms": 10.0, "early_late_db": 10 * math.log10(1 / 0.25),
        "centroid_ms": 4.0, "energy_db": 10 * math.log10(1.25),
    }, abs=1e-6)


def test_impulse_late_energy_windows_follow_the_own_peak():
    impulse = np.zeros(48000)
    impulse[14400], impulse[15360] = 1.0, 0.5

    figures = seat_figures.impulse_late_energy(impulse, sample_rate_hz=48000)

    # Band-pass ringing crosses the peak and both windows; unfiltered pulse ratios do not apply.
    assert figures == pytest.approx({
        "t0_ms": 4.9792, "early_late_db": 2.2304,
        "energy_db": -21.8354, "centroid_ms": 6.5617,
    }, abs=1e-4)


def test_band_limited_impulse_preserves_the_in_band_transfer():
    freqs = np.fft.rfftfreq(32768, 1 / 48000)
    band = seat_figures.LATE_ENERGY_BAND_HZ
    transfer = ((freqs >= band[0]) & (freqs <= band[1])).astype(complex)

    impulse = seat_figures.band_limited_impulse(freqs, transfer, band)

    assert np.fft.rfft(impulse) == pytest.approx(transfer, abs=1e-12)


@pytest.mark.parametrize("candidate_count,reference_count", [(3, 3), (0, 3), (3, 0), (0, 0)])
def test_late_energy_change_uses_each_sides_median(candidate_count, reference_count):
    candidate = [{"early_late_db": early, "energy_db": energy, "centroid_ms": centroid}
                 for early, energy, centroid in [(5, -10, 3), (7, -40, 4), (90, -12, 20)]]
    reference = [{"early_late_db": early, "energy_db": energy, "centroid_ms": centroid}
                 for early, energy, centroid in [(1, -14, 5), (4, -50, 6), (30, -16, 40)]]

    row = seat_figures.late_energy_change(candidate[:candidate_count], reference[:reference_count])

    assert row == {
        "early_late_change_db": 3.0 if candidate_count and reference_count else None,
        "band_energy_change_db": 4.0 if candidate_count and reference_count else None,
        "arrival_shift_ms": -2.0 if candidate_count and reference_count else None,
        "repeats": [candidate_count, reference_count],
        "reason": "" if candidate_count and reference_count else seat_figures.REASON_NO_COMPARISON,
    }


@pytest.mark.parametrize("ceiling,bands", [
    (5000, [[350.0, 700.0], [700.0, 1500.0], [1500.0, 5000.0]]),
    (1000, [[350.0, 700.0]]), (350, []),
])
def test_upper_band_levels_compare_only_wholly_covered_bands(ceiling, bands):
    freqs = np.geomspace(20, 5000, 600)

    rows = seat_figures.band_level_changes(
        freqs, np.full_like(freqs, 2.0), reference_db=np.zeros_like(freqs), coverage_hz=(20, ceiling),
    )

    assert [row["band_hz"] for row in rows] == bands
    for row in rows:
        assert {key: row[key] for key in ("level_db", "reference_db", "change_db")} == pytest.approx(
            {"level_db": 2.0, "reference_db": 0.0, "change_db": 2.0})


@pytest.mark.parametrize("tilt_db_per_decade,amplitude_db", [(-3.0, 0.8), (3.0, 0.8), (0.0, 0.0)])
def test_own_trend_ripple_separates_roughness_from_broad_tilt(tilt_db_per_decade, amplitude_db):
    freqs = np.geomspace(10.0, 20000.0, 1800)
    curve = amplitude_db * np.sin(2 * np.pi * np.log2(freqs / 100.0))
    band = (40.0, 8000.0)
    tilted = curve + tilt_db_per_decade * np.log10(freqs / 100.0)
    own = seat_figures.own_trend_ripple_db(freqs, curve, band_hz=band)
    after = seat_figures.own_trend_ripple_db(freqs, tilted, band_hz=band)
    rows = [seat_figures.position_figures(
        freqs, candidate, reference_db=np.zeros_like(freqs), band_hz=band,
        coverage_hz=(freqs[0], freqs[-1]),
    ) for candidate in (curve, tilted)]

    assert [row["own_trend_ripple_db"] for row in rows] == [own, after]
    assert after == pytest.approx(own, abs=0.05)
    if tilt_db_per_decade:
        assert rows[1]["ripple_db"] - rows[0]["ripple_db"] > 0.5
    else:
        assert own == after == rows[0]["ripple_db"] == 0.0


def test_spread_rms_uses_the_per_bin_population_spread_over_the_band():
    stack = np.array([[-99, -2, -3, -99, -99], [0, 0, 0, 0, 0], [99, 2, 3, 99, 99]])
    freqs = [20.0, 40.0, 80.0, 160.0, 320.0]
    spread = np.std(stack, axis=0)

    assert seat_figures.spread_rms_db(spread, freqs, band_hz=(40.0, 160.0)) == pytest.approx(math.sqrt(13 / 3))
    assert seat_figures.spread_rms_db(None, freqs, band_hz=(40.0, 160.0)) is None
