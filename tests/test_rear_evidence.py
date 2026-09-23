# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pair-take figures on two ideal sources one delay apart."""
from __future__ import annotations

import json
import math
from importlib import import_module
from typing import Any

import numpy as np
import pytest

from jasper.audio_measurement import evidence_reasons, rear_evidence
from jasper.audio_measurement.alignment import DEFAULT_CONFIDENCE_THRESHOLD
from tests.test_seat_figures import COVERAGE_HZ, FREQS_HZ


@pytest.mark.parametrize("module_name, prefixes", [
    ('jasper.audio_measurement.rear_evidence', 'REASON_'),
    ('jasper.audio_measurement.interference_nulls', 'REASON_'),
    ('jasper.audio_measurement.room_limits', 'REASON_'),
    ('jasper.audio_measurement.timing_verification', 'REASON_'),
    ('jasper.active_speaker.crossover_v2.rear_views', ('REASON_', 'REFUSE_')),
    ('jasper.active_speaker.crossover_v2.feature_classifier', ('CAPTURE_', 'CAPTURES_', 'NO_ADMISSIBLE_', 'NO_FEATURES_', 'PROGRAM_MISSING', 'ROUND_SHAPE_')),
    ('jasper.active_speaker.crossover_v2.feature_classifier.captures', ('CAPTURE_', 'CAPTURES_', 'NO_ADMISSIBLE_', 'NO_FEATURES_', 'PROGRAM_MISSING', 'ROUND_SHAPE_')),
    ('jasper.active_speaker.crossover_v2.close_reference', ('REFUSE_', 'UNRESOLVED_', 'VERDICT_')),
    ('jasper.active_speaker.crossover_v2.evidence_packet.positions', 'REASON_'),
    ('jasper.active_speaker.round_verdicts', 'REASON_'),
    ('jasper.active_speaker.round_view_artifacts', 'REASON_'),
    ('jasper.cli.round_views._common', 'REASON_'),
    ('jasper.cli.round_views.repeat', 'REASON_'),
    ('jasper.active_speaker.crossover_v2.round_views.directivity', 'REASON_'),
])
def test_analysis_reason_constants_have_one_frozen_registry(module_name, prefixes):
    constants = {name: value for name, value in vars(evidence_reasons).items()
                 if name.isupper() and isinstance(value, str)}
    registry = evidence_reasons.EVIDENCE_REASONS
    assert len(constants) == len(set(constants.values())) == len(registry)
    assert set(constants.values()) == set(registry)
    assert all(isinstance(meaning, str) and meaning and "\n" not in meaning
               for meaning in registry.values())
    codes = [value for name, value in vars(import_module(module_name)).items()
             if name.startswith(prefixes) and isinstance(value, str)]
    assert codes and set(codes) <= registry.keys()
    with pytest.raises(TypeError):
        registry["new_reason"] = ""

#: The pair fixture: one arrival per woofer inside a window long enough to hold
#: both, on the take's own sample rate.
SAMPLE_RATE_HZ = 48000
PULSE_SAMPLES = 4096
FRONT_ARRIVAL_S = 0.005
#: The band a caller correlates the gap over when it names one itself.
GAP_BAND_HZ = (30.0, 800.0)


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
        band_hz=rear_evidence.ARRIVAL_GAP_BAND_HZ,
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


@pytest.mark.parametrize("band_hz,search_ms", [
    ((90.0, 91.0), None), ((90.0, 289.9), None),
    ((90.0, 290.0), 10.0), ((90.0, 315.0), 8.889),
])
def test_the_gap_requires_enough_bandwidth_for_a_bounded_search(band_hz, search_ms):
    gap = rear_evidence.arrival_gap_ms(
        [_pair(0.3)["impulses"]], sample_rate_hz=SAMPLE_RATE_HZ, band_hz=band_hz)

    assert gap["reason"] == (rear_evidence.REASON_COVERAGE_SHORT if search_ms is None else "")
    if search_ms is None:
        assert (gap["ms"], gap["search_ms"], gap["confidence"]) == (None, None, None)
        assert rear_evidence.confident_arrival_gap_s(gap) is None
    else:
        assert gap["ms"] is not None
        assert gap["search_ms"] == pytest.approx(search_ms, abs=0.001)


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
