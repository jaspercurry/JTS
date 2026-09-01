# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The close reference, judged on synthesized geometry whose answer is known.

The decisive test is the kill-test below: two mic distances from ONE declared
geometry, with the floor bounce present or absent and a speaker-side notch
present either way, so the ONLY difference between the two scenarios is the
room. The verb must flip its verdict on that difference alone, recover the
fractional-sample offset injected between the two captures, and put a number
on the room contribution that matches the level actually injected.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.close_reference import (
    REFUSE_UNREADABLE_ROUND,
    VERDICT_AGREEMENT,
    VERDICT_ROOM_DOMINATED,
    VERDICT_UNRESOLVED,
    UNRESOLVED_NO_CANCELLATION,
    CloseReferenceRefused,
    cancellation_depth_db,
    compare_impulse_responses,
    load_round_capture,
    recommended_distance,
)

SAMPLE_RATE = 48000
IR_LEN = 1 << 15
SOUND_SPEED = 343.0

# Declared geometry of the synthetic rig: speaker and mic at the same height,
# so the floor image sits 2 * height below the direct path.
SPEAKER_HEIGHT_M = 0.84
MIC_HEIGHT_M = 0.84
FAR_M = 1.0
CLOSE_M = 0.30
NOTCH_HZ = 900.0
FC_HZ = 6000.0
DRIVER_DIAMETER_M = 0.1397

#: Timing error injected between the two captures, on top of geometry: what a
#: pair of back-to-back runs with unknown interface latency actually costs.
INJECTED_OFFSET_SAMPLES = 0.37
INJECTED_OFFSET_US = INJECTED_OFFSET_SAMPLES / SAMPLE_RATE * 1e6


def _image_distance(distance_m: float) -> float:
    return math.hypot(distance_m, SPEAKER_HEIGHT_M + MIC_HEIGHT_M)


def _synthetic_ir(
    distance_m: float, *, reflection: bool, offset_samples: float = 0.0
) -> np.ndarray:
    """One mic position's IR: direct arrival, optional floor bounce, one notch.

    Built in the frequency domain so both the geometric delays and the injected
    offset are exactly fractional, then dithered with noise at -60 dB so an
    ``agreement`` verdict has to survive a difference between the captures
    rather than reading two byte-identical arrays.
    """
    freqs = np.fft.rfftfreq(IR_LEN, d=1.0 / SAMPLE_RATE)
    w = 2j * np.pi * freqs
    # 50 ms of pre-roll keeps the direct peak interior to the array.
    lead_s = 0.05 + offset_samples / SAMPLE_RATE
    spectrum = (1.0 / distance_m) * np.exp(-w * (distance_m / SOUND_SPEED + lead_s))
    if reflection:
        image = _image_distance(distance_m)
        spectrum = spectrum + (1.0 / image) * np.exp(
            -w * (image / SOUND_SPEED + lead_s)
        )
    # Speaker-side notch: identical at both distances, so it is the control.
    s = 1j * freqs / NOTCH_HZ
    spectrum = spectrum * (s**2 + 1.0) / (s**2 + s / 6.0 + 1.0)
    spectrum = spectrum * ((freqs > 25.0) & (freqs < 18000.0))
    ir = np.fft.irfft(spectrum, n=IR_LEN)
    rng = np.random.default_rng(int(distance_m * 1000) + int(reflection))
    return ir + rng.standard_normal(ir.size) * 1e-3 * float(np.max(np.abs(ir)))


def _report(*, reflection: bool) -> dict:
    return compare_impulse_responses(
        _synthetic_ir(FAR_M, reflection=reflection),
        _synthetic_ir(
            CLOSE_M, reflection=reflection, offset_samples=INJECTED_OFFSET_SAMPLES
        ),
        sample_rate=SAMPLE_RATE,
        far_m=FAR_M,
        close_m=CLOSE_M,
        fc_hz=FC_HZ,
        driver_diameter_m=DRIVER_DIAMETER_M,
    )


def _graded(report: dict) -> list[dict]:
    """Every band the far window actually graded."""
    far_window = next(w for w in report["windows"] if w["name"] == "far_window")
    return [row for row in far_window["bands"] if row["graded_band_hz"] is not None]


@pytest.mark.parametrize(
    "reflection, expected",
    [(True, VERDICT_ROOM_DOMINATED), (False, VERDICT_AGREEMENT)],
)
def test_room_only_feature_and_speaker_side_feature_separate(reflection, expected):
    """The floor bounce is the only difference; the verdict must be too."""
    report = _report(reflection=reflection)
    graded = _graded(report)
    assert graded, report["validity"]
    assert {row["verdict"] for row in graded} == {expected}
    ungraded = [
        row
        for row in next(w for w in report["windows"] if w["name"] == "far_window")[
            "bands"
        ]
        if row["graded_band_hz"] is None
    ]
    assert {row["verdict"] for row in ungraded} == {VERDICT_UNRESOLVED}


@pytest.mark.parametrize("reflection", [True, False])
def test_injected_fractional_offset_is_recovered(reflection):
    """The sub-sample align finds the offset geometry does not explain."""
    alignment = _report(reflection=reflection)["alignment"]
    assert alignment["trusted"] is True
    assert abs(alignment["residual_lag_us"]) < 5.0
    assert (
        abs(alignment["measured_minus_geometric_us"] + INJECTED_OFFSET_US) < 5.0
    )
    # A budget priced at a zero residual would advertise infinite depth.
    assert all(
        math.isfinite(entry["depth_db"])
        for entry in alignment["cancellation_budget_db"]
    )


def test_residual_matches_the_injected_reflection_level():
    """The residual IS the room contribution, to within a dB of what went in."""
    injected_db = 20.0 * math.log10((1.0 / _image_distance(FAR_M)) / (1.0 / FAR_M))
    for row in _graded(_report(reflection=True)):
        assert abs(row["residual_rel_direct_db"] - injected_db) < 1.0


def test_frame_and_validity_are_published():
    report = _report(reflection=True)
    assert set(report["frame"]) >= {
        "window_kind", "taper_fraction", "gate_lead_ms", "smooth_fraction",
        "detrend_fraction", "grid_hz", "grid_points", "n_fft",
        "alignment_band_hz", "gcc_upsample", "sound_speed_m_s",
    }
    validity = report["validity"]
    assert validity["band_top_hz"] == pytest.approx(FC_HZ / 2.0)
    assert validity["comparison_band_hz"][1] == pytest.approx(FC_HZ / 2.0)
    # A close mic is near-field at HIGH frequencies: the criterion is a ceiling.
    assert validity["far_field_ceiling_hz"] > validity["comparison_band_hz"][1]


@pytest.mark.parametrize(
    "diameter_in, fc_hz, expected_in",
    [(5.5, 2500.0, 12.4), (12.0, 500.0, 25.3), (2.5, 2500.0, 5.3)],
)
def test_recommended_distance_lands_where_the_issue_says(
    diameter_in, fc_hz, expected_in
):
    record = recommended_distance(diameter_in * 0.0254, fc_hz)
    assert record["distance_in"] == pytest.approx(expected_in, abs=0.1)
    assert record["distance_m"] == pytest.approx(
        record["far_field_term_m"] + record["margin_term_m"]
    )
    assert record["far_field_ceiling_hz"] > record["band_top_hz"]


@pytest.mark.parametrize(
    "lag_us, f_hz, expected_db",
    # gate-research-results.md, document 2 section B3's own worked numbers.
    [(10.0, 1000.0, -24.0), (10.0, 200.0, -38.0), (1e6 / 48000, 1000.0, -18.0),
     (1.6, 1000.0, -40.0)],
)
def test_cancellation_budget_matches_the_banked_derivation(lag_us, f_hz, expected_db):
    assert cancellation_depth_db(f_hz, lag_us * 1e-6) == pytest.approx(
        expected_db, abs=0.5
    )


def test_a_round_that_is_not_a_directory_refuses_by_name(tmp_path):
    with pytest.raises(CloseReferenceRefused) as excinfo:
        load_round_capture(tmp_path / "absent")
    assert excinfo.value.reason == REFUSE_UNREADABLE_ROUND


def test_a_misdeclared_distance_does_not_read_as_agreement():
    """Shapes agree, levels do not: that is a finding about the declaration."""
    report = compare_impulse_responses(
        _synthetic_ir(FAR_M, reflection=False),
        _synthetic_ir(CLOSE_M, reflection=False),
        sample_rate=SAMPLE_RATE,
        far_m=FAR_M,
        close_m=0.9 * FAR_M,  # the mic was at CLOSE_M
        fc_hz=FC_HZ,
        driver_diameter_m=DRIVER_DIAMETER_M,
    )
    for row in _graded(report):
        assert row["verdict"] == VERDICT_UNRESOLVED
        assert row["unresolved_reason"] == UNRESOLVED_NO_CANCELLATION
