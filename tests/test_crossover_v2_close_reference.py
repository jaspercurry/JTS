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
    ALIGNMENT_CONFIDENCE_FLOOR,
    DEFAULT_GATE_MS,
    GATE_SOURCE_CALLER,
    GATE_SOURCE_DECLARED,
    GATE_SOURCE_DEFAULT,
    REFUSE_UNREADABLE_ROUND,
    VERDICT_AGREEMENT,
    VERDICT_ROOM_DOMINATED,
    VERDICT_UNRESOLVED,
    UNRESOLVED_NO_CANCELLATION,
    cancellation_depth_db,
    compare_impulse_responses,
    declared_clean_window_ms,
    select_capture,
)
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry

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

#: The mismatched-window case: a far capture whose clean span is SHORT and a
#: close capture whose declared window is long, with one bounce sitting
#: between the two. The bounce is inside the close window and outside the far
#: one, which is the whole point.
SHORT_FAR_GATE_MS = 2.5
LONG_CLOSE_GATE_MS = 6.0
EARLY_BOUNCE_MS = 4.0
EARLY_BOUNCE_DB = -8.0


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


def _report(*, reflection: bool, **kwargs) -> dict:
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
        **kwargs,
    )


def _with_bounce(ir: np.ndarray, delay_ms: float, gain_db: float) -> np.ndarray:
    """``ir`` plus a delayed, attenuated copy of itself: one room arrival."""
    delay = int(round(delay_ms * 1e-3 * SAMPLE_RATE))
    echo = np.zeros_like(ir)
    echo[delay:] = ir[: ir.size - delay] * 10.0 ** (gain_db / 20.0)
    return ir + echo


def _geometry(distance_m: float = FAR_M) -> DeclaredGeometry:
    """The rig the synthetic IRs were built from, declared."""
    return DeclaredGeometry(
        speaker_height_m=SPEAKER_HEIGHT_M,
        mic_height_m=MIC_HEIGHT_M,
        distance_m=distance_m,
    )


def _graded(report: dict, name: str = "far_window") -> list[dict]:
    """Every band one window actually graded."""
    window = next(w for w in report["windows"] if w["name"] == name)
    return [row for row in window["bands"] if row["graded_band_hz"] is not None]


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


def test_alignment_is_cut_at_the_shorter_of_the_two_clean_windows():
    """Two unequal windows, one bounce between them: the SHORT one bounds.

    The far capture's clean window is 2.5 ms and the close capture's is 6 ms,
    which is the ordinary case — the first bounce's excess path grows as the
    mic nears the speaker. A -8 dB arrival sits at 4 ms, inside the close
    window and outside the far one. Cutting both alignment segments at the
    LONGER window would push the far segment past its own reflection-free
    span into that arrival, and the one lag it produces is then inherited by
    every band of the corrected close IR.
    """
    report = compare_impulse_responses(
        _with_bounce(
            _synthetic_ir(FAR_M, reflection=False), EARLY_BOUNCE_MS, EARLY_BOUNCE_DB
        ),
        _synthetic_ir(
            CLOSE_M, reflection=False, offset_samples=INJECTED_OFFSET_SAMPLES
        ),
        sample_rate=SAMPLE_RATE,
        far_m=FAR_M,
        close_m=CLOSE_M,
        fc_hz=FC_HZ,
        driver_diameter_m=DRIVER_DIAMETER_M,
        far_gate_ms=SHORT_FAR_GATE_MS,
        close_gate_ms=LONG_CLOSE_GATE_MS,
    )

    alignment = report["alignment"]
    assert alignment["alignment_gate_ms"] == SHORT_FAR_GATE_MS
    assert alignment["trusted"] is True
    # The lag is still the injected offset and nothing the room added.
    assert abs(alignment["measured_minus_geometric_us"] + INJECTED_OFFSET_US) < 5.0


def test_residual_matches_the_injected_reflection_level():
    """The residual IS the room contribution, to within a dB of what went in."""
    injected_db = 20.0 * math.log10((1.0 / _image_distance(FAR_M)) / (1.0 / FAR_M))
    for row in _graded(_report(reflection=True)):
        assert abs(row["residual_rel_direct_db"] - injected_db) < 1.0


def test_a_declared_geometry_gates_each_window_at_its_own_first_bounce():
    """The close capture's clean window is LONGER, and the report says so.

    The declared floor bounce is the same reflection at both distances; its
    excess path over the direct one grows as the direct path shrinks, which
    is half of why a close capture can say what a far one cannot. An explicit
    gate still wins, because the operator may know something the declaration
    does not.
    """
    geometry = _geometry()
    far_ms = declared_clean_window_ms(geometry, FAR_M)
    close_ms = declared_clean_window_ms(geometry, CLOSE_M)
    assert far_ms is not None and close_ms is not None and close_ms > far_ms
    # Under the declaration's own 0.15 m floor there is no derived window.
    assert declared_clean_window_ms(geometry, 0.05) is None

    windows = {
        window["name"]: window
        for window in _report(reflection=True, geometry=geometry)["windows"]
    }
    assert windows["far_window"]["gate_ms"] == pytest.approx(far_ms)
    assert windows["close_window"]["gate_ms"] == pytest.approx(close_ms)
    assert {window["gate_source"] for window in windows.values()} == {
        GATE_SOURCE_DECLARED
    }

    overridden = _report(reflection=True, geometry=geometry, close_gate_ms=3.0)
    close_window = next(
        window for window in overridden["windows"] if window["name"] == "close_window"
    )
    assert close_window["gate_ms"] == 3.0
    assert close_window["gate_source"] == GATE_SOURCE_CALLER
    # The window it displaced is still published beside it.
    assert close_window["declared_clean_window_ms"] == pytest.approx(close_ms)


def test_the_close_windows_longer_gate_is_what_finds_the_room():
    """The payoff of the second capture, read at the two declared windows.

    The far window's own gate excludes the floor bounce from BOTH captures, so
    they agree — at that length the far read really was the speaker. The close
    window's longer gate lets the bounce into the far capture alone, because
    the close capture's own bounce is still later, and the subtraction reads
    the room that the shorter window could not see.
    """
    report = _report(reflection=True, geometry=_geometry())

    assert {row["verdict"] for row in _graded(report)} == {VERDICT_AGREEMENT}
    assert {row["verdict"] for row in _graded(report, "close_window")} == {
        VERDICT_ROOM_DOMINATED
    }


@pytest.mark.parametrize("reflection", [True, False])
def test_the_alignment_block_says_what_it_measured(reflection):
    """Every alignment number a reader acts on, against the fixture's own
    geometry: the close mic is nearer, so its direct arrives EARLIER, and the
    shift the correlator recovered is the geometric one less the offset the
    fixture injected."""
    alignment = _report(reflection=reflection)["alignment"]

    assert isinstance(alignment["far_direct_peak_ms"], float)
    assert isinstance(alignment["close_direct_peak_ms"], float)
    assert alignment["close_direct_peak_ms"] < alignment["far_direct_peak_ms"]
    assert alignment["measured_shift_us"] == pytest.approx(
        alignment["geometric_delay_us"] - INJECTED_OFFSET_US, abs=5.0
    )
    assert alignment["confidence"] >= ALIGNMENT_CONFIDENCE_FLOOR
    # The search bound is not what stopped the peak, so the lag is a reading
    # rather than a clamped artifact.
    assert alignment["at_search_edge"] is False


@pytest.mark.parametrize(
    "reflection, room_moves_the_worst_bin", [(True, True), (False, False)]
)
def test_each_band_names_its_worst_far_bin_and_what_the_close_read_there(
    reflection, room_moves_the_worst_bin
):
    """The three per-band numbers a reader argues from.

    The 900 Hz speaker-side notch is the control: it is in BOTH captures, so
    the far read's worst bin lands on it either way and reads as a dip. What
    changes is ``delta_at_worst_db`` — with the floor bounce present the close
    capture reads that bin materially shallower, and that gap is the room.
    """
    graded = _graded(_report(reflection=reflection))
    for row in graded:
        lo_hz, hi_hz = row["graded_band_hz"]
        assert lo_hz <= row["worst_far_bin_hz"] < hi_hz
        assert isinstance(row["worst_far_deviation_db"], float)
        assert isinstance(row["delta_at_worst_db"], float)
        assert (abs(row["delta_at_worst_db"]) > 1.0) is room_moves_the_worst_bin

    # The control: the notch is the low band's worst bin under both
    # scenarios, and it reads as a DIP.
    assert graded[0]["worst_far_bin_hz"] == pytest.approx(NOTCH_HZ, rel=0.05)
    assert graded[0]["worst_far_deviation_db"] < 0.0


def test_frame_and_validity_are_published():
    report = _report(reflection=True)
    for window in report["windows"]:
        assert window["gate_source"] == GATE_SOURCE_DEFAULT
        assert window["gate_ms"] == DEFAULT_GATE_MS
        assert window["declared_clean_window_ms"] is None
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
    with pytest.raises(RoundCapturesRefused) as excinfo:
        select_capture(tmp_path / "absent")
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
