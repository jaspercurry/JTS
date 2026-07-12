# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Decision-class + band-specific SNR gate (P1b).

Pins the split SNR policy from "Level control and SNR" in
docs/active-crossover-information-design.md:

  - :func:`band_levels_dbfs` is the FFT band-power math moved verbatim out of
    ``jasper.correction.session._band_levels_dbfs`` (which now delegates
    here) — the delegation's output must stay byte-equal.
  - :data:`CROSSOVER_SNR_BANDS_HZ`'s first four rows equal
    ``session.SNR_BANDS_HZ`` so the shipped room-correction table and the new
    six-band crossover table never drift apart.
  - :func:`band_snr_verdicts` — magnitude/trim tiers at 25/20 dB (reusing
    ``QualityModel.snr_ok_db``/``snr_warn_db``), the stricter 35 dB alignment
    tier that rejects scalar-only evidence, and the worst-RELEVANT-band
    partial-pass rule (a bad octave outside the window a decision depends on
    must not veto it).
  - :func:`cap_null_depth_db` — a null of depth D needs D + 10 dB of overlap
    SNR to be provable; a deeper measured null reports capped at what the
    SNR can prove.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement import snr_policy
from jasper.audio_measurement.quality_model import DRIVER
from jasper.correction import session as correction_session

SR = 48000


def _bands(rows):
    """[(band_id, lo, hi, level_dbfs), ...] -> the correction-shape band list."""
    return [
        {"band_id": band_id, "band_hz": [lo, hi], "level_dbfs": level}
        for band_id, lo, hi, level in rows
    ]


# ---------- band_levels_dbfs / CROSSOVER_SNR_BANDS_HZ -----------------------


def test_crossover_bands_first_four_match_room_correction_table():
    assert snr_policy.CROSSOVER_SNR_BANDS_HZ[:4] == correction_session.SNR_BANDS_HZ


def test_band_levels_dbfs_matches_session_delegation():
    """session._band_levels_dbfs (now a thin delegation) is byte-equal to
    calling snr_policy.band_levels_dbfs directly with the same band table —
    pins that the delegation forwards samples/sample_rate/bands correctly and
    that the relocated math was not altered in the move."""
    rng = np.random.default_rng(20260711)
    # Broadband noise-like fixture so all four low bands carry real energy —
    # a band-boundary mistake in the moved table would show up as a level
    # shift, not just a missing band.
    samples = rng.normal(scale=0.2, size=SR).astype(np.float64)

    via_session = correction_session._band_levels_dbfs(samples, SR)
    via_policy = snr_policy.band_levels_dbfs(
        samples, SR, snr_policy.CROSSOVER_SNR_BANDS_HZ[:4]
    )
    assert via_session == via_policy
    assert len(via_session) == 4


def test_ambient_report_uses_upper_percentile_across_one_second_frames():
    rng = np.random.default_rng(91)
    quiet = rng.normal(0.0, 0.001, SR * 11)
    noisy = rng.normal(0.0, 0.02, SR)

    report = snr_policy.ambient_band_report(
        np.concatenate([quiet, noisy]),
        SR,
        (("wide", 20.0, 12000.0),),
    )
    quiet_report = snr_policy.ambient_band_report(
        quiet,
        SR,
        (("wide", 20.0, 12000.0),),
    )

    assert report["duration_s"] == 12.0
    assert report["method"] == "one_second_p95"
    assert (
        report["bands"][0]["level_dbfs"]
        > quiet_report["bands"][0]["level_dbfs"] + 10.0
    )


def test_paired_signal_window_deconvolution_is_trusted_for_alignment():
    noise = [{
        "band_id": "wide",
        "band_hz": [100.0, 8000.0],
        "level_dbfs": -80.0,
    }]
    capture = [{**noise[0], "level_dbfs": -40.0}]

    verdict = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
        capture_bands=capture,
        noise_bands=noise,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(100.0, 8000.0),
        model=DRIVER,
        band_method="paired_signal_window_deconvolution",
    )

    assert verdict["verdict"] == "ok"
    assert verdict["bands"][0]["method"] == (
        "paired_signal_window_deconvolution"
    )


# ---------- band_snr_verdicts: magnitude class -------------------------------


def test_magnitude_class_28db_reads_ok():
    capture = _bands([("mid", 1000.0, 4000.0, -20.0)])
    noise = _bands([("mid", 1000.0, 4000.0, -48.0)])  # 28 dB SNR
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture,
        noise_bands=noise,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(1000.0, 4000.0),
        model=DRIVER,
    )
    band = out["bands"][0]
    assert band["estimated_snr_db"] == pytest.approx(28.0)
    assert band["verdict"] == "ok"
    assert band["shortfall_db"] is None
    assert band["method"] == "fft_band_power_difference"
    assert out["verdict"] == "ok"
    assert out["worst_relevant"]["band_id"] == "mid"


def test_magnitude_class_22db_reads_reduced_with_shortfall_against_ok():
    capture = _bands([("mid", 1000.0, 4000.0, -20.0)])
    noise = _bands([("mid", 1000.0, 4000.0, -42.0)])  # 22 dB SNR
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture,
        noise_bands=noise,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(1000.0, 4000.0),
        model=DRIVER,
    )
    band = out["bands"][0]
    assert band["verdict"] == "reduced"
    # 25 dB (snr_ok_db) - 22 dB = 3 dB short of the confident floor.
    assert band["shortfall_db"] == pytest.approx(3.0)
    assert out["verdict"] == "reduced"


def test_magnitude_class_17db_reads_insufficient_with_missing_db_report():
    # The design doc's own worked example ("17.4 dB SNR; 2.6 dB more needed"):
    # 20.0 dB (snr_warn_db) - 17.4 dB = 2.6 dB missing.
    capture = _bands([("mid", 1000.0, 4000.0, -20.0)])
    noise = _bands([("mid", 1000.0, 4000.0, -37.4)])  # 17.4 dB SNR
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture,
        noise_bands=noise,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(1000.0, 4000.0),
        model=DRIVER,
    )
    band = out["bands"][0]
    assert band["verdict"] == "insufficient"
    assert band["shortfall_db"] == pytest.approx(2.6)
    assert out["verdict"] == "insufficient"


def test_magnitude_class_no_noise_reads_unknown():
    capture = _bands([("mid", 1000.0, 4000.0, -20.0)])
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture,
        noise_bands=None,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(1000.0, 4000.0),
        model=DRIVER,
    )
    band = out["bands"][0]
    assert band["verdict"] == "unknown"
    assert band["estimated_snr_db"] is None
    assert band["shortfall_db"] is None
    assert band["method"] == "none"
    assert out["verdict"] == "unknown"
    assert out["worst_relevant"] is None


def test_partial_pass_worst_relevant_band_governs_overall_verdict():
    # Mirrors the design doc's own woofer example: good 150-800 Hz (upper_bass
    # + transition), reduced 80-150 Hz (bass), short below 80 Hz (sub_bass).
    capture = _bands([
        ("sub_bass", 20.0, 80.0, -20.0),
        ("bass", 80.0, 160.0, -20.0),
        ("upper_bass", 160.0, 350.0, -20.0),
        ("transition", 350.0, 1000.0, -20.0),
    ])
    noise = _bands([
        ("sub_bass", 20.0, 80.0, -35.0),      # 15 dB -> insufficient
        ("bass", 80.0, 160.0, -42.0),         # 22 dB -> reduced
        ("upper_bass", 160.0, 350.0, -48.0),  # 28 dB -> ok
        ("transition", 350.0, 1000.0, -48.0),  # 28 dB -> ok
    ])
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture,
        noise_bands=noise,
        noise_floor_dbfs_scalar=None,
        # sub_bass (the worst band in the WHOLE report) sits outside this
        # decision's relevant window.
        relevant_hz=(80.0, 1000.0),
        model=DRIVER,
    )
    verdicts = {b["band_id"]: b["verdict"] for b in out["bands"]}
    assert verdicts == {
        "sub_bass": "insufficient",
        "bass": "reduced",
        "upper_bass": "ok",
        "transition": "ok",
    }
    # The insufficient sub_bass band never vetoes: it's outside relevant_hz.
    assert out["verdict"] == "reduced"
    assert out["worst_relevant"]["band_id"] == "bass"


# ---------- band_snr_verdicts: alignment class -------------------------------


def test_alignment_class_40db_band_evidence_reads_ok():
    fc = 2000.0
    capture = _bands([("mid", 1000.0, 4000.0, -20.0)])
    noise = _bands([("mid", 1000.0, 4000.0, -60.0)])  # 40 dB SNR
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
        capture_bands=capture,
        noise_bands=noise,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(fc / 2.0, fc * 2.0),
        model=DRIVER,
    )
    band = out["bands"][0]
    assert band["estimated_snr_db"] == pytest.approx(40.0)
    assert band["verdict"] == "ok"
    assert band["shortfall_db"] is None
    assert band["method"] == "fft_band_power_difference"
    assert out["verdict"] == "ok"


def test_alignment_class_30db_reads_insufficient():
    fc = 2000.0
    capture = _bands([("mid", 1000.0, 4000.0, -20.0)])
    noise = _bands([("mid", 1000.0, 4000.0, -50.0)])  # 30 dB SNR
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
        capture_bands=capture,
        noise_bands=noise,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(fc / 2.0, fc * 2.0),
        model=DRIVER,
    )
    band = out["bands"][0]
    assert band["verdict"] == "insufficient"
    assert band["shortfall_db"] == pytest.approx(5.0)  # 35 dB - 30 dB
    assert out["verdict"] == "insufficient"


def test_alignment_class_scalar_only_reads_unknown():
    # A 1 kHz scalar level is explicitly NOT sufficient evidence for a
    # null/alignment decision, even though it computes a clean-looking number.
    fc = 2000.0
    capture = _bands([("mid", 1000.0, 4000.0, -20.0)])
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
        capture_bands=capture,
        noise_bands=None,
        noise_floor_dbfs_scalar=-60.0,  # would read as 40 dB "SNR" if trusted
        relevant_hz=(fc / 2.0, fc * 2.0),
        model=DRIVER,
    )
    band = out["bands"][0]
    assert band["method"] == "scalar_fallback"
    assert band["verdict"] == "unknown"
    assert out["verdict"] == "unknown"
    assert out["worst_relevant"] is None


def test_band_snr_verdicts_rejects_unknown_decision_class():
    with pytest.raises(ValueError):
        snr_policy.band_snr_verdicts(
            decision_class="bogus",
            capture_bands=[],
            noise_bands=None,
            noise_floor_dbfs_scalar=None,
            relevant_hz=(100.0, 200.0),
            model=DRIVER,
        )


# ---------- worst_band_verdict -----------------------------------------------


def test_worst_band_verdict_ignores_unknown_and_non_overlapping():
    bands = [
        {"band_id": "a", "band_hz": [100.0, 200.0], "verdict": "ok"},
        {"band_id": "b", "band_hz": [200.0, 300.0], "verdict": "insufficient"},
        {"band_id": "c", "band_hz": [900.0, 1000.0], "verdict": "insufficient"},
        {"band_id": "d", "band_hz": [150.0, 250.0], "verdict": "unknown"},
    ]
    worst = snr_policy.worst_band_verdict(bands, 100.0, 400.0)
    assert worst["band_id"] == "b"


def test_worst_band_verdict_none_when_nothing_covered():
    bands = [{"band_id": "a", "band_hz": [100.0, 200.0], "verdict": "unknown"}]
    assert snr_policy.worst_band_verdict(bands, 100.0, 200.0) is None
    assert snr_policy.worst_band_verdict([], 100.0, 200.0) is None
    assert snr_policy.worst_band_verdict(None, 100.0, 200.0) is None


# ---------- cap_null_depth_db -------------------------------------------------


def test_cap_null_depth_caps_to_snr_minus_margin():
    # overlap SNR 20 dB + measured 25 dB null -> reported 10 dB, capped.
    worst_relevant = {
        "band_id": "mid", "estimated_snr_db": 20.0, "verdict": "insufficient",
    }
    reported, capped = snr_policy.cap_null_depth_db(
        25.0, worst_relevant, DRIVER.null_cap_margin_db
    )
    assert reported == pytest.approx(10.0)
    assert capped is True


def test_cap_null_depth_uncapped_when_measured_within_proof():
    worst_relevant = {"band_id": "mid", "estimated_snr_db": 40.0, "verdict": "ok"}
    reported, capped = snr_policy.cap_null_depth_db(
        12.0, worst_relevant, DRIVER.null_cap_margin_db
    )
    assert reported == pytest.approx(12.0)
    assert capped is False


def test_cap_null_depth_floors_at_zero():
    worst_relevant = {
        "band_id": "mid", "estimated_snr_db": 5.0, "verdict": "insufficient",
    }
    reported, capped = snr_policy.cap_null_depth_db(
        8.0, worst_relevant, DRIVER.null_cap_margin_db
    )
    assert reported == 0.0
    assert capped is True


def test_cap_null_depth_unchanged_when_no_evidence():
    reported, capped = snr_policy.cap_null_depth_db(
        9.0, None, DRIVER.null_cap_margin_db
    )
    assert reported == 9.0
    assert capped is False
