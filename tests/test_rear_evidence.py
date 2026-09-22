# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the rear-stage evidence figures (issue #5330, slice 1).

The fixtures are a front source plus its wall image, and a pair of ideal
sources one delay apart: they prove the arithmetic of these figures and
nothing at all about a real speaker.
"""
from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pytest

from jasper.audio_measurement import rear_evidence
from jasper.audio_measurement.alignment import DEFAULT_CONFIDENCE_THRESHOLD
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

#: The pair fixture: one arrival per woofer inside a window long enough to hold
#: both, on the take's own sample rate.
SAMPLE_RATE_HZ = 48000
PULSE_SAMPLES = 4096
FRONT_ARRIVAL_S = 0.005
#: The band a caller correlates the gap over when it names one itself.
GAP_BAND_HZ = (30.0, 800.0)


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
         (170.0 / rear_evidence.DIP_SEARCH_WINDOW_RATIO, 170.0 * rear_evidence.DIP_SEARCH_WINDOW_RATIO)),
        ({"section_band_hz": (74.0, 344.0)}, _WALL_DIP_HZ, (74.0, 344.0)),
        ({"handover_hz": 66.0}, _WALL_DIP_HZ, (66.0, CEILING_HZ)),
        ({}, _ROOM_MODE_HZ, (COVERAGE_HZ[0], CEILING_HZ)),
    ],
)
def test_the_measured_dip_search_is_windowed_so_a_deeper_dip_elsewhere_loses(
    kwargs, expected_hz, search_hz,
):
    band = rear_evidence.comparison_band(
        coverage_hz=COVERAGE_HZ, ceiling_hz=CEILING_HZ,
        reference_take=(FREQS_HZ, _two_dip_curve()), **kwargs,
    )
    assert band["source"] == rear_evidence.BAND_SOURCE_MEASURED_DIP
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
    row = rear_evidence.position_figures(
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


def _pulse(arrival_s: float, *, gain: float = 1.0, inverted: bool = False) -> np.ndarray:
    """One arrival at ``arrival_s`` and nothing else in the window, built from
    linear phase so a fractional-sample arrival is exact."""
    freqs = np.fft.rfftfreq(PULSE_SAMPLES, d=1.0 / SAMPLE_RATE_HZ)
    pulse = np.fft.irfft(np.exp(-2j * np.pi * freqs * arrival_s), n=PULSE_SAMPLES)
    return pulse * (-gain if inverted else gain)


def _pair(
    gap_ms: float, *, level_gap_db: float = 0.0, inverted: bool = False, error_db: float = 0.0,
) -> dict[str, Any]:
    """Two ideal woofers ``gap_ms`` apart, as a pair take banks them: the three
    complex segments on one grid, plus the two solo impulses."""
    gain = 10.0 ** (level_gap_db / 20.0)
    front = np.ones_like(FREQS_HZ, dtype=np.complex128)
    rear = (-gain if inverted else gain) * np.exp(
        -2j * np.pi * FREQS_HZ * gap_ms / 1000.0)
    return {
        "front_tf": front, "rear_tf": rear,
        "pair_tf": (front + rear) * 10.0 ** (error_db / 20.0),
        "impulses": (_pulse(FRONT_ARRIVAL_S),
                     _pulse(FRONT_ARRIVAL_S + gap_ms / 1000.0, gain=gain, inverted=inverted),
                     0.0),
    }


def _pair_band_hz(pair: dict[str, Any]) -> list[float]:
    """The span of the third-octave bands the figures were read over."""
    bands = rear_evidence.pair_band_levels(
        FREQS_HZ, coverage_hz=COVERAGE_HZ,
        **{key: pair[key] for key in ("front_tf", "rear_tf", "pair_tf")})
    return [bands[0]["band_hz"][0], bands[-1]["band_hz"][1]]


@pytest.mark.parametrize(
    ("gap_ms", "level_gap_db", "inverted", "polarity"),
    [
        (0.8, 0.0, False, rear_evidence.POLARITY_SAME),
        (0.8, -4.0, False, rear_evidence.POLARITY_SAME),
        (0.8, 0.0, True, rear_evidence.POLARITY_INVERTED),
        (-0.5, 2.5, True, rear_evidence.POLARITY_INVERTED),
    ],
)
def test_a_synthetic_pair_recovers_its_gap_level_and_polarity(
    gap_ms, level_gap_db, inverted, polarity,
):
    pair = _pair(gap_ms, level_gap_db=level_gap_db, inverted=inverted)
    transfers = {key: pair[key] for key in ("front_tf", "rear_tf", "pair_tf")}

    bands = rear_evidence.pair_band_levels(FREQS_HZ, coverage_hz=COVERAGE_HZ, **transfers)
    gap = rear_evidence.arrival_gap_ms(
        [pair["impulses"]], sample_rate_hz=SAMPLE_RATE_HZ,
        band_hz=rear_evidence.shared_radiating_band_hz(
            FREQS_HZ, front_tf=pair["front_tf"], rear_tf=pair["rear_tf"],
            band_hz=(FREQS_HZ[0], FREQS_HZ[-1])),
    )
    read = rear_evidence.rear_polarity(
        FREQS_HZ, front_tf=pair["front_tf"], rear_tf=pair["rear_tf"],
        band_hz=_pair_band_hz(pair), arrival_gap=gap,
    )

    # Each woofer alone, their sum, and the level gap — every band, no ranking.
    assert [row["front_db"] for row in bands] == pytest.approx([0.0] * len(bands))
    assert [row["level_gap_db"] for row in bands] == pytest.approx([level_gap_db] * len(bands))
    assert [row["rear_db"] for row in bands] == pytest.approx([level_gap_db] * len(bands))
    assert gap["ms"] == pytest.approx(gap_ms, abs=0.02)
    assert (gap["at_edge"], gap["n_repeats"], gap["repeat_spread_us"]) == (False, 1, None)
    assert gap["confidence"] > DEFAULT_CONFIDENCE_THRESHOLD
    assert read["state"] == polarity
    assert json.loads(json.dumps({"bands": bands, "gap": gap, "polarity": read})) == {
        "bands": bands, "gap": gap, "polarity": read}


@pytest.mark.parametrize("band_hz,expected_ms,confidence_range,search_ms", [
    ((40.0, 3000.0), 1.14, (0.4, 0.7), 2.0),
    (rear_evidence.ARRIVAL_GAP_BAND_HZ, 1.7, (0.4, 1.0), 8.889),
])
def test_a_wall_image_shifts_the_gap_in_the_cancellation_band(
    band_hz, expected_ms, confidence_range, search_ms,
):
    front, rear, shift = _pair(1.14)["impulses"]
    rear += _pulse(FRONT_ARRIVAL_S + 0.00114 + 0.0012, gain=0.9)

    gap = rear_evidence.arrival_gap_ms(
        [(front, rear, shift)], sample_rate_hz=SAMPLE_RATE_HZ, band_hz=band_hz)

    assert gap["ms"] == pytest.approx(expected_ms, abs=0.03)
    assert confidence_range[0] < gap["confidence"] < confidence_range[1]
    assert gap["band_hz"] == list(band_hz)
    assert gap["search_ms"] == pytest.approx(search_ms, abs=0.001)
    assert rear_evidence.confident_arrival_gap_s(gap) == pytest.approx(expected_ms / 1e3, abs=3e-5)


@pytest.mark.parametrize("error_db", [0.0, 3.0, -2.0])
def test_the_trust_number_is_the_error_the_played_sum_carries(error_db):
    """``P == F + R`` reads zero; anything else reads exactly its own error."""
    pair = _pair(0.8, error_db=error_db)

    residual = rear_evidence.superposition_residual_db(
        FREQS_HZ, band_hz=_pair_band_hz(pair),
        **{key: pair[key] for key in ("front_tf", "rear_tf", "pair_tf")})

    assert residual == pytest.approx(abs(error_db), abs=1e-6)


@pytest.mark.parametrize("bins_per_band", [2, 3, 12])
def test_pair_band_residuals_disclose_local_error_and_sparse_bins(bins_per_band):
    freqs = np.concatenate([np.linspace(24.0, 26.0, bins_per_band),
                            np.linspace(30.0, 33.0, bins_per_band)])
    transfers = {"front_tf": np.ones_like(freqs), "rear_tf": np.ones_like(freqs),
                 "pair_tf": np.repeat([2.0, 2.0 * 10.0 ** (3.0 / 20.0)], bins_per_band)}

    rows = rear_evidence.pair_band_levels(freqs, coverage_hz=(22.0, 36.0), **transfers)

    assert len(rows) == 2
    assert [row["superposition_residual_db"] for row in rows] == (
        [None, None] if bins_per_band < 3 else pytest.approx([0.0, 3.0], abs=0.05))


def test_a_band_too_narrow_to_read_answers_empty_rather_than_a_figure():
    """Each pair figure has its own band gate, and each says nothing rather
    than reading one: no whole third-octave band, fewer than three bins for the
    trust number, fewer than two for the gap's band."""
    pair = _pair(0.8)
    transfers = {key: pair[key] for key in ("front_tf", "rear_tf", "pair_tf")}
    two_bins = (FREQS_HZ[100], FREQS_HZ[102])

    assert rear_evidence.pair_band_levels(
        FREQS_HZ, coverage_hz=(FREQS_HZ[0], 21.0), **transfers) == []
    assert rear_evidence.superposition_residual_db(
        FREQS_HZ, band_hz=two_bins, **transfers) is None
    assert rear_evidence.shared_radiating_band_hz(
        FREQS_HZ, front_tf=pair["front_tf"], rear_tf=pair["rear_tf"],
        band_hz=(FREQS_HZ[100], FREQS_HZ[101])) is None
    assert rear_evidence.gradient_residual_db(
        FREQS_HZ, pair["rear_tf"], 0.0008, (FREQS_HZ[0], FREQS_HZ[0])) is None


@pytest.mark.parametrize("broken", ["nan_impulse", "inf_impulse", "nan_shift", "short_impulse"])
def test_an_unreadable_impulse_is_disclosed_rather_than_correlated(broken):
    """The whitening returns a lag for a non-finite bin instead of an error, so
    a corrupted repeat has to be refused before it reaches the correlator."""
    front, rear, shift = _pair(0.8)["impulses"]
    front, rear = np.asarray(front, dtype=np.float64), np.asarray(rear, dtype=np.float64).copy()
    if broken == "nan_impulse":
        rear[10] = np.nan
    elif broken == "inf_impulse":
        rear[10] = np.inf
    elif broken == "nan_shift":
        shift = np.nan
    else:
        front, rear = front[:1], rear[:1]

    gap = rear_evidence.arrival_gap_ms([(front, rear, shift)], sample_rate_hz=SAMPLE_RATE_HZ,
                                       band_hz=GAP_BAND_HZ)

    assert (gap["ms"], gap["n_repeats"]) == (None, 0)
    assert gap["reason"] == rear_evidence.REASON_NO_IMPULSE
    assert rear_evidence.confident_arrival_gap_s(gap) is None


def test_the_gap_pools_its_repeats_and_a_missing_segment_says_so():
    near, far = _pair(0.8), _pair(0.9)
    front, rear, _shift = near["impulses"]

    pooled = rear_evidence.arrival_gap_ms(
        [near["impulses"], far["impulses"]],
        sample_rate_hz=SAMPLE_RATE_HZ, band_hz=GAP_BAND_HZ)
    missing = rear_evidence.arrival_gap_ms((), sample_rate_hz=SAMPLE_RATE_HZ,
                                           band_hz=GAP_BAND_HZ)
    # A clock shift is the schedule's drift, not the pair's gap: it is removed.
    shifted = rear_evidence.arrival_gap_ms([(front, rear, 4.8)], sample_rate_hz=SAMPLE_RATE_HZ,
                                           band_hz=GAP_BAND_HZ)

    assert (pooled["n_repeats"], pooled["reason"]) == (2, "")
    assert pooled["ms"] == pytest.approx(0.85, abs=0.02)
    assert pooled["repeat_spread_us"] == pytest.approx(100.0, abs=20.0)
    assert shifted["ms"] == pytest.approx(0.8 - 4.8 / SAMPLE_RATE_HZ * 1e3, abs=0.02)
    assert (missing["ms"], missing["confidence"], missing["at_edge"]) == (None, None, None)
    assert missing["reason"] == rear_evidence.REASON_NO_IMPULSE
    assert rear_evidence.confident_arrival_gap_s(missing) is None
    # Without a gap to remove, the phase of R/F says nothing about polarity —
    # and the two ways a gap goes unusable keep their own reasons, because a
    # gap the correlator read but does not trust is not a missing segment.
    shy = {**pooled, "confidence": DEFAULT_CONFIDENCE_THRESHOLD / 2.0}
    for gap, reason in ((missing, rear_evidence.REASON_NO_IMPULSE),
                        (shy, rear_evidence.REASON_GAP_NOT_CONFIDENT)):
        unclear = rear_evidence.rear_polarity(
            FREQS_HZ, front_tf=near["front_tf"], rear_tf=near["rear_tf"],
            band_hz=(40.0, 200.0), arrival_gap=gap)
        assert (unclear["state"], unclear["phase_deg"]) == (rear_evidence.POLARITY_UNCLEAR, None)
        assert unclear["reason"] == reason


@pytest.mark.parametrize(
    ("ratio", "expected_db"),
    [("ideal_gradient", None), ("no_rear", 0.0), ("front_copy", 20.0 * math.log10(2.0))],
)
def test_the_gradient_residual_reads_the_applied_ratio_against_the_gap(ratio, expected_db):
    """An ideal gradient is the rear playing the front inverted and delayed by
    the measured gap, so it cancels; no rear at all leaves the ideal itself."""
    gap_s = 0.0008
    delayed = np.exp(-2j * np.pi * FREQS_HZ * gap_s)
    applied = {"ideal_gradient": -delayed, "no_rear": np.zeros_like(delayed),
               "front_copy": delayed}[ratio]

    residual = rear_evidence.gradient_residual_db(FREQS_HZ, applied, gap_s, (40.0, 200.0))

    assert residual < -60.0 if expected_db is None else residual == pytest.approx(expected_db)
    assert rear_evidence.gradient_residual_db(
        FREQS_HZ, applied, -gap_s, (40.0, 200.0)) == pytest.approx(residual)
    assert rear_evidence.gradient_residual_db(FREQS_HZ, applied, None, (40.0, 200.0)) is None
    assert rear_evidence.gradient_residual_db(FREQS_HZ, applied, gap_s, None) is None


@pytest.mark.parametrize("output_db", [0.0, -30.0])
def test_the_gap_band_follows_where_both_woofers_radiate(output_db):
    """A rear that rolls off narrows the band the gap correlates over, so
    GCC-PHAT never whitens a bin the pair did not drive. The floor is each
    woofer's OWN peak, so a quiet take reads the same band as a loud one."""
    scale = 10.0 ** (output_db / 20.0)

    narrowed = rear_evidence.shared_radiating_band_hz(
        FREQS_HZ, front_tf=scale * np.ones_like(FREQS_HZ, dtype=np.complex128),
        rear_tf=scale / (1.0 + 1j * FREQS_HZ / 120.0) ** 4,
        band_hz=(FREQS_HZ[0], FREQS_HZ[-1]))

    assert narrowed[0] == pytest.approx(FREQS_HZ[0])
    assert 120.0 < narrowed[1] < 400.0


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


def test_impulse_energy_windows_follow_the_own_peak():
    impulse = np.zeros(4800)
    impulse[480], impulse[1440] = 1.0, 0.5

    assert rear_evidence.impulse_energy_figures(impulse, sample_rate_hz=48000) == pytest.approx({
        "t0_ms": 10.0, "early_late_db": 10 * math.log10(1 / 0.25),
        "centroid_ms": 4.0, "energy_db": 10 * math.log10(1.25),
    }, abs=1e-6)


def test_impulse_late_energy_windows_follow_the_own_peak():
    impulse = np.zeros(48000)
    impulse[14400], impulse[15360] = 1.0, 0.5

    figures = rear_evidence.impulse_late_energy(impulse, sample_rate_hz=48000)

    # Band-pass ringing crosses the peak and both windows; unfiltered pulse ratios do not apply.
    assert figures == pytest.approx({
        "t0_ms": 4.9792, "early_late_db": 2.2304,
        "energy_db": -21.8354, "centroid_ms": 6.5617,
    }, abs=1e-4)


def test_band_limited_impulse_preserves_the_in_band_transfer():
    freqs = np.fft.rfftfreq(32768, 1 / 48000)
    band = rear_evidence.LATE_ENERGY_BAND_HZ
    transfer = ((freqs >= band[0]) & (freqs <= band[1])).astype(complex)

    impulse = rear_evidence.band_limited_impulse(freqs, transfer, band)

    assert np.fft.rfft(impulse) == pytest.approx(transfer, abs=1e-12)


@pytest.mark.parametrize("candidate_count,reference_count", [(3, 3), (0, 3), (3, 0), (0, 0)])
def test_late_energy_change_uses_each_sides_median(candidate_count, reference_count):
    candidate = [{"early_late_db": early, "energy_db": energy, "centroid_ms": centroid}
                 for early, energy, centroid in [(5, -10, 3), (7, -40, 4), (90, -12, 20)]]
    reference = [{"early_late_db": early, "energy_db": energy, "centroid_ms": centroid}
                 for early, energy, centroid in [(1, -14, 5), (4, -50, 6), (30, -16, 40)]]

    row = rear_evidence.late_energy_change(candidate[:candidate_count], reference[:reference_count])

    assert row == {
        "early_late_change_db": 3.0 if candidate_count and reference_count else None,
        "band_energy_change_db": 4.0 if candidate_count and reference_count else None,
        "arrival_shift_ms": -2.0 if candidate_count and reference_count else None,
        "repeats": [candidate_count, reference_count],
        "reason": "" if candidate_count and reference_count else rear_evidence.REASON_NO_COMPARISON,
    }


@pytest.mark.parametrize("ceiling,bands", [
    (5000, [[350.0, 700.0], [700.0, 1500.0], [1500.0, 5000.0]]),
    (1000, [[350.0, 700.0]]), (350, []),
])
def test_upper_band_levels_compare_only_wholly_covered_bands(ceiling, bands):
    freqs = np.geomspace(20, 5000, 600)

    rows = rear_evidence.band_level_changes(
        freqs, np.full_like(freqs, 2.0), reference_db=np.zeros_like(freqs), coverage_hz=(20, ceiling),
    )

    assert [row["band_hz"] for row in rows] == bands
    for row in rows:
        assert {key: row[key] for key in ("level_db", "reference_db", "change_db")} == pytest.approx(
            {"level_db": 2.0, "reference_db": 0.0, "change_db": 2.0})
