# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the rear-stage evidence figures (issue #5330, slice 1).

The fixtures are a front source plus its wall image: they prove the
arithmetic of these figures and nothing at all about a real speaker.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from jasper.audio_measurement import rear_evidence
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
    band = rear_evidence.comparison_band(
        coverage_hz=COVERAGE_HZ,
        ceiling_hz=CEILING_HZ,
        reference_take=(FREQS_HZ, reference_curve),
    )
    return band, rear_evidence.reference_curve_db(FREQS_HZ, reference_curve)


def _figures(
    curve: np.ndarray,
    band: dict[str, Any],
    reference: np.ndarray,
    *,
    incumbent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return rear_evidence.position_figures(
        FREQS_HZ, curve, reference_db=reference, band_hz=band["band_hz"],
        coverage_hz=COVERAGE_HZ, handover_hz=HANDOVER_HZ, incumbent=incumbent,
    )


@pytest.mark.parametrize("wall_m", [0.25, WALL_M, 0.5])
def test_the_band_follows_the_measured_wall_dip(wall_m):
    band, _ = _batch(_image_curve(wall_m=wall_m))
    null_hz = _first_null_hz(wall_m)
    lo_ratio, hi_ratio = CANONICAL_SHOULDER_RATIOS
    assert band["source"] == rear_evidence.BAND_SOURCE_MEASURED_DIP
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
        ({"geometric_dip_hz": 200.0}, rear_evidence.BAND_SOURCE_DECLARED_GEOMETRY, (100.0, 400.0)),
        (
            {"geometric_dip_hz": 200.0, "section_band_hz": (60.0, 300.0)},
            rear_evidence.BAND_SOURCE_DECLARED_GEOMETRY,
            (100.0, 400.0),
        ),
        ({"section_band_hz": (60.0, 300.0)}, rear_evidence.BAND_SOURCE_SECTION_BAND, (60.0, 300.0)),
        ({}, rear_evidence.BAND_SOURCE_COVERAGE, (COVERAGE_HZ[0], CEILING_HZ)),
        # Wholly above the coverage: a disclosed no-band, never an inverted range.
        ({"section_band_hz": (1000.0, 1200.0)}, rear_evidence.BAND_SOURCE_SECTION_BAND, None),
    ],
)
def test_with_no_measurable_dip_the_band_falls_back_in_order(declared, source, band_hz):
    band = rear_evidence.comparison_band(
        coverage_hz=COVERAGE_HZ, ceiling_hz=CEILING_HZ,
        reference_take=(FREQS_HZ, _image_curve(rho=0.0)), **declared,
    )
    assert (band["source"], band["dip_hz"]) == (source, None)
    assert band["reason"] == ("" if band_hz else rear_evidence.REASON_COVERAGE_SHORT)
    assert band["band_hz"] == (None if band_hz is None else pytest.approx(list(band_hz)))


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
    summary = rear_evidence.across_positions(
        rows, incumbent_rows=incumbent_rows,
        spread_db=rear_evidence.repeat_spread([rows["front"], rows["front"]])["spread_db"],
    )
    worst = summary["worst_regression"]
    assert (worst["position"], worst["figure"]) == ("right", "handover.hole_db")
    assert worst["change_db"] > 8.0
    assert worst["exceeds_repeat_spread"] is True
    hole = summary["figures"]["handover.hole_db"]
    assert hole["worst_db"] > hole["median_db"] + 8.0
    assert (summary["positions"], summary["positions_unavailable"]) == (3, {})
    # A position this candidate was never measured at is disclosed, not dropped.
    partial = rear_evidence.across_positions(
        {key: row for key, row in rows.items() if key != "left"}, incumbent_rows=incumbent_rows)
    assert partial["positions"] == 3
    assert partial["positions_unavailable"] == {"left": rear_evidence.REASON_NO_ROW}


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
    spread = rear_evidence.repeat_spread(rows[:n_repeats])
    assert spread["n_repeats"] == n_repeats
    assert spread["reason"] == ("" if n_repeats > 1 else rear_evidence.REASON_NO_REPEATS)
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
    row = rear_evidence.position_figures(
        FREQS_HZ[keep], curve[keep], reference_db=reference[keep],
        band_hz=None if missing == "no_band" else band["band_hz"],
        coverage_hz=COVERAGE_HZ, handover_hz=HANDOVER_HZ,
    )
    assert row["reason"] == rear_evidence.REASON_COVERAGE_SHORT
    assert all(
        row[key] is None
        for key in ("dip", "dip_shift", "ripple_db", "handover", "low_bass", "band_level_db")
    )
    summary = rear_evidence.across_positions({"front": row}, incumbent_rows={"front": row})
    assert summary["positions_unavailable"] == {"front": rear_evidence.REASON_COVERAGE_SHORT}
    assert summary["worst_regression"] is None
