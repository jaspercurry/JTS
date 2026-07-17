# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Decision-class + band-specific SNR gate (P1b).

Pins the split SNR policy from "Level control and SNR" in
docs/active-crossover-information-design.md:

  - :func:`band_levels_dbfs` is the FFT band-power math moved verbatim out of
    ``jasper.correction.session._band_levels_dbfs`` (which now delegates
    through ``jasper.correction.acoustic_quality``) — the delegation's output
    must stay byte-equal.
  - :data:`CROSSOVER_SNR_BANDS_HZ`'s first four rows equal
    ``acoustic_quality.SNR_BANDS_HZ`` so the shipped room-correction table and
    the new six-band crossover table never drift apart.
  - :func:`band_snr_verdicts` — magnitude/trim tiers at 25/20 dB (reusing
    ``QualityModel.snr_ok_db``/``snr_warn_db``), the stricter 35 dB alignment
    tier that rejects scalar-only evidence, and the worst-RELEVANT-band
    partial-pass rule (a bad octave outside the window a decision depends on
    must not veto it).
  - :func:`cap_null_depth_db` — a null of depth D needs D + 10 dB of overlap
    SNR to be provable; a deeper measured null reports capped at what the
    SNR can prove.
  - :func:`excitation_covered_bands` / :func:`apply_noise_band_fallback` —
    the phantom-noise-floor fix (docs/HANDOFF-audio-measurement-core.md
    "SNR"). A band the reference sweep never excited reads a Tikhonov
    regularization artifact from the deconvolved domain, not a real
    ambient-noise measurement; these two functions detect that case and
    substitute the raw (non-deconvolved) ambient reading instead. The
    ground-truth fixture rows are distilled from three real jts3 hardware
    captures (sessions 293cc36331f7, 70819cab996b, f44ecc33d071,
    2026-07-17) where this bug overstated ``sub_bass`` SNR by ~50 dB and
    drove a false "insufficient" verdict.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement import snr_policy
from jasper.audio_measurement.quality_model import DRIVER
from jasper.correction import acoustic_quality
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
    assert snr_policy.CROSSOVER_SNR_BANDS_HZ[:4] == (
        acoustic_quality.SNR_BANDS_HZ
    )
    assert correction_session.SNR_BANDS_HZ is acoustic_quality.SNR_BANDS_HZ


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

    via_owner = acoustic_quality.band_levels_dbfs(samples, SR)
    via_session = correction_session._band_levels_dbfs(samples, SR)
    via_policy = snr_policy.band_levels_dbfs(
        samples, SR, snr_policy.CROSSOVER_SNR_BANDS_HZ[:4]
    )
    assert via_owner == via_policy
    assert via_session == via_owner
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


@pytest.mark.parametrize(
    ("capture_level", "expected_snr", "expected_verdict"),
    [
        (-20.0000001, 20.0, "reduced"),
        (-20.1, 19.9, "insufficient"),
    ],
)
def test_magnitude_warn_boundary_uses_displayed_inclusive_precision(
    capture_level, expected_snr, expected_verdict
):
    out = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=_bands([("mid", 1000.0, 4000.0, capture_level)]),
        noise_bands=_bands([("mid", 1000.0, 4000.0, -40.0)]),
        noise_floor_dbfs_scalar=None,
        relevant_hz=(1000.0, 4000.0),
        model=DRIVER,
    )

    assert out["bands"][0]["estimated_snr_db"] == expected_snr
    assert out["bands"][0]["verdict"] == expected_verdict
    assert out["verdict"] == expected_verdict


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


# ---------- excitation_covered_bands / apply_noise_band_fallback ------------
#
# The phantom-noise-floor fix. jasper.active_speaker.commissioning_admission
# derives a per-driver reference sweep whose [f1, f2] is the CONFIRMED safe
# excitation band for that driver — a woofer's is routinely much narrower
# than the analysis' full [20, 12000] band table (e.g. the real hardware
# fixture below, f1=60/f2=4000). Outside that band the reference sweep
# carries no deliberate energy, so a regularized deconvolution divides by a
# near-zero reference spectrum there — a well-known Tikhonov "resonance"
# artifact, not a measurement. A signal capture usually swamps this artifact
# (a near-mic'd driver is loud); an ambient (noise-only) capture has nothing
# to swamp it with, so the reported noise floor was overstated by ~50 dB —
# see the ground-truth fixture below.

# Ground-truth fixture, distilled from three real jts3 hardware captures
# (sessions 293cc36331f7, 70819cab996b, f44ecc33d071; driver=main woofer,
# f1=60.0 Hz, f2=4000.0 Hz — /var/lib/jasper/active_speaker/sessions/*/
# captures/driver_main_woofer_*.{wav,json}, 2026-07-17). Each row is
# (band_id, lo_hz, hi_hz, deconvolved_level_dbfs, raw_robust_p95_dbfs,
# raw_baseline_p50_dbfs) — deconvolved_level_dbfs is the noise IR's OWN
# magnitude-band reading (pre robust-delta adjustment); raw_robust/baseline
# are non-deconvolved, percentile-framed reads of the same quiet window
# (:func:`framed_ambient_band_report`). No WAV/binary fixtures are checked
# in — these six numbers per band are everything the fix's logic consumes.
_GROUND_TRUTH_SESSIONS = {
    "293cc36331f7": [
        ("sub_bass", 20.0, 80.0, -27.20, -73.20, -75.31),
        ("bass", 80.0, 160.0, -50.97, -85.62, -89.22),
        ("upper_bass", 160.0, 350.0, -52.10, -90.19, -91.15),
        ("transition", 350.0, 1000.0, -57.94, -95.85, -98.76),
        ("mid", 1000.0, 4000.0, -66.91, -111.19, -112.37),
        ("treble", 4000.0, 12000.0, -88.62, -120.0, -120.0),
    ],
    "70819cab996b": [
        ("sub_bass", 20.0, 80.0, -25.87, -72.82, -75.30),
        ("bass", 80.0, 160.0, -48.60, -90.36, -91.40),
        ("upper_bass", 160.0, 350.0, -53.67, -93.60, -94.37),
        ("transition", 350.0, 1000.0, -60.30, -101.82, -102.64),
        ("mid", 1000.0, 4000.0, -68.34, -112.60, -114.19),
        ("treble", 4000.0, 12000.0, -84.82, -120.0, -120.0),
    ],
    "f44ecc33d071": [
        ("sub_bass", 20.0, 80.0, -26.32, -74.25, -75.48),
        ("bass", 80.0, 160.0, -49.47, -87.77, -90.98),
        ("upper_bass", 160.0, 350.0, -54.90, -94.25, -94.88),
        ("transition", 350.0, 1000.0, -63.25, -102.75, -105.22),
        ("mid", 1000.0, 4000.0, -70.81, -114.57, -117.41),
        ("treble", 4000.0, 12000.0, -88.31, -120.0, -120.0),
    ],
}
# The pre-fix reported ambient["bands"] level for each session (deconvolved
# + robust-delta, byte-identical to what apply_noise_band_fallback returns
# today for a COVERED band) — pins the "must not regress" half of the fix
# alongside the ground-truth SNR jump for sub_bass.
_GROUND_TRUTH_PRE_FIX_LEVELS = {
    "293cc36331f7": {
        "sub_bass": -25.09, "bass": -47.37, "upper_bass": -51.14,
        "transition": -55.03, "mid": -65.73, "treble": -88.62,
    },
    "70819cab996b": {
        "sub_bass": -23.39, "bass": -47.56, "upper_bass": -52.90,
        "transition": -59.48, "mid": -66.75, "treble": -84.82,
    },
    "f44ecc33d071": {
        "sub_bass": -25.09, "bass": -46.26, "upper_bass": -54.27,
        "transition": -60.78, "mid": -67.97, "treble": -88.31,
    },
}
# Ground-truth SNR: the real driver signal level (deconvolved, unaffected by
# this fix) minus the pre-fix and post-fix noise levels, rounded the way
# band_snr_verdicts rounds it. Independent FFT analysis of the raw WAVs
# (scratch forensics, 2026-07-17) put the room's TRUE sub_bass ambient at
# about -75 dBFS across all three sessions — i.e. true SNR is roughly
# 63-66 dB. The acceptance bar is a conservative >=40 dB floor, not an exact
# match to that number.
_GROUND_TRUTH_SIGNAL_LEVEL = {
    "293cc36331f7": -9.49,
    "70819cab996b": -10.18,
    "f44ecc33d071": -11.71,
}


@pytest.mark.parametrize("session_id", sorted(_GROUND_TRUTH_SESSIONS))
def test_apply_noise_band_fallback_matches_ground_truth_hardware_captures(
    session_id,
):
    """sub_bass SNR clears >=40 dB post-fix; every other band is untouched.

    This is the acceptance bar from the phantom-noise-floor investigation:
    on all three real captures, sub_bass estimated SNR was 13-16 dB
    ("insufficient") pre-fix and must read >=40 dB post-fix (conservative;
    true SNR is ~63-66 dB), while bass/upper_bass/transition/mid/treble must
    stay within the pre-fix numbers (bit-for-bit here, since none of them
    change basis).
    """

    rows = _GROUND_TRUTH_SESSIONS[session_id]
    noise_bands = [
        {"band_id": r[0], "band_hz": [r[1], r[2]], "level_dbfs": r[3]}
        for r in rows
    ]
    robust_bands = [
        {"band_id": r[0], "band_hz": [r[1], r[2]], "level_dbfs": r[4]}
        for r in rows
    ]
    baseline_bands = [
        {"band_id": r[0], "band_hz": [r[1], r[2]], "level_dbfs": r[5]}
        for r in rows
    ]
    covered = snr_policy.excitation_covered_bands(
        snr_policy.CROSSOVER_SNR_BANDS_HZ, f1_hz=60.0, f2_hz=4000.0
    )
    assert covered == {
        "sub_bass": False, "bass": True, "upper_bass": True,
        "transition": True, "mid": True, "treble": False,
    }

    adjusted = snr_policy.apply_noise_band_fallback(
        noise_bands,
        robust_bands=robust_bands,
        baseline_bands=baseline_bands,
        covered=covered,
    )
    by_id = {item["band_id"]: item for item in adjusted}

    # sub_bass: only uncovered band with a non-floor-clamped raw reading ->
    # raw_ambient_fallback, reporting the raw robust (p95) level directly.
    assert by_id["sub_bass"]["basis"] == "raw_ambient_fallback"
    expected_sub_bass_raw = [r for r in rows if r[0] == "sub_bass"][0][4]
    assert by_id["sub_bass"]["level_dbfs"] == pytest.approx(
        expected_sub_bass_raw, abs=0.01
    )
    signal_level = _GROUND_TRUTH_SIGNAL_LEVEL[session_id]
    sub_bass_snr = round(signal_level - by_id["sub_bass"]["level_dbfs"], 1)
    assert sub_bass_snr >= 40.0, (
        f"{session_id}: sub_bass SNR {sub_bass_snr} dB did not clear the "
        "conservative 40 dB acceptance floor"
    )

    # treble: also uncovered, but its raw robust reading is floor-clamped
    # (the phone mic's real noise floor at 4-12 kHz reads as pure digital
    # silence) -- no real precision to trust, so it keeps the pre-fix
    # deconvolved+delta value untouched.
    assert by_id["treble"]["basis"] == "deconvolved"

    # Every band's reported level matches the pre-fix number exactly EXCEPT
    # sub_bass -- that's the "must not regress" half of the acceptance bar.
    pre_fix = _GROUND_TRUTH_PRE_FIX_LEVELS[session_id]
    for band_id, expected_pre_fix in pre_fix.items():
        if band_id == "sub_bass":
            continue
        assert by_id[band_id]["basis"] == "deconvolved"
        assert by_id[band_id]["level_dbfs"] == pytest.approx(
            expected_pre_fix, abs=0.01
        ), f"{session_id}/{band_id} regressed from its pre-fix reading"


def test_excitation_covered_bands_flags_the_narrow_woofer_sweep_hardware_shape():
    """f1=60/f2=4000 (the real production woofer sweep) covers the four
    middle bands but not sub_bass (mostly below f1) or treble (entirely
    above f2)."""

    covered = snr_policy.excitation_covered_bands(
        snr_policy.CROSSOVER_SNR_BANDS_HZ, f1_hz=60.0, f2_hz=4000.0
    )
    assert covered == {
        "sub_bass": False, "bass": True, "upper_bass": True,
        "transition": True, "mid": True, "treble": False,
    }


def test_excitation_covered_bands_full_range_sweep_covers_everything():
    """The DEFAULT single-driver sweep (20 Hz-20 kHz) never triggers the
    fallback -- every canonical band is fully covered."""

    covered = snr_policy.excitation_covered_bands(
        snr_policy.CROSSOVER_SNR_BANDS_HZ, f1_hz=20.0, f2_hz=20000.0
    )
    assert all(covered.values())


def test_excitation_covered_bands_boundary_is_inclusive():
    """A band edge that lands exactly on f1/f2 still counts as covered --
    matches the real "mid" band (hi=4000.0) against the real woofer sweep's
    f2=4000.0."""

    covered = snr_policy.excitation_covered_bands(
        [("exact_edge", 60.0, 4000.0)], f1_hz=60.0, f2_hz=4000.0
    )
    assert covered == {"exact_edge": True}


def test_excitation_covered_bands_no_margin_around_the_edge():
    """Deliberately exact: a band starting just above f1 (bass, which reads
    "sensible" today) is covered, but one straddling f1 (sub_bass) is not --
    proves the check isn't accidentally over-wide."""

    covered = snr_policy.excitation_covered_bands(
        [("straddles", 20.0, 80.0), ("clears", 80.0, 160.0)],
        f1_hz=60.0,
        f2_hz=4000.0,
    )
    assert covered == {"straddles": False, "clears": True}


def test_apply_noise_band_fallback_leaves_covered_bands_untouched_regardless_of_raw_level():
    """A covered band always takes the deconvolved+delta path -- even when
    the raw ambient reading would be far LOUDER, "fixing" a covered band was
    never in scope and must never happen as a side effect."""

    noise_bands = [{"band_id": "bass", "band_hz": [80.0, 160.0], "level_dbfs": -50.0}]
    robust_bands = [{"band_id": "bass", "band_hz": [80.0, 160.0], "level_dbfs": -10.0}]
    baseline_bands = [{"band_id": "bass", "band_hz": [80.0, 160.0], "level_dbfs": -20.0}]

    adjusted = snr_policy.apply_noise_band_fallback(
        noise_bands,
        robust_bands=robust_bands,
        baseline_bands=baseline_bands,
        covered={"bass": True},
    )
    assert adjusted[0]["basis"] == "deconvolved"
    # -50.0 (deconvolved) + (-10.0 - -20.0) (robust-baseline delta) = -40.0
    assert adjusted[0]["level_dbfs"] == pytest.approx(-40.0)


def test_apply_noise_band_fallback_does_not_launder_a_genuinely_noisy_uncovered_band():
    """Protective-power check: an uncovered band whose room really IS noisy
    (raw robust reads loud, not floor-clamped) reports that loud level
    verbatim -- the fallback is not a blanket "always quiet" shortcut, it
    reports what the room actually measured."""

    noise_bands = [
        {"band_id": "sub_bass", "band_hz": [20.0, 80.0], "level_dbfs": -25.0}
    ]
    robust_bands = [
        {"band_id": "sub_bass", "band_hz": [20.0, 80.0], "level_dbfs": -30.0}
    ]
    baseline_bands = [
        {"band_id": "sub_bass", "band_hz": [20.0, 80.0], "level_dbfs": -35.0}
    ]

    adjusted = snr_policy.apply_noise_band_fallback(
        noise_bands,
        robust_bands=robust_bands,
        baseline_bands=baseline_bands,
        covered={"sub_bass": False},
    )
    assert adjusted[0]["basis"] == "raw_ambient_fallback"
    assert adjusted[0]["level_dbfs"] == pytest.approx(-30.0)


def test_apply_noise_band_fallback_keeps_deconvolved_value_when_raw_is_floor_clamped():
    """An uncovered band whose raw reading hit DBFS_FLOOR carries no real
    precision (see snr_policy.DBFS_FLOOR) -- keep the pre-fix deconvolved
    value rather than reporting a clamped number as if it were a genuine
    measurement. Mirrors the real "treble" hardware shape (f2=4000 Hz sweep,
    phone-mic self-noise below 4 kHz reads as pure digital silence)."""

    noise_bands = [
        {"band_id": "treble", "band_hz": [4000.0, 12000.0], "level_dbfs": -88.62}
    ]
    robust_bands = [
        {
            "band_id": "treble", "band_hz": [4000.0, 12000.0],
            "level_dbfs": snr_policy.DBFS_FLOOR,
        }
    ]
    baseline_bands = [
        {
            "band_id": "treble", "band_hz": [4000.0, 12000.0],
            "level_dbfs": snr_policy.DBFS_FLOOR,
        }
    ]

    adjusted = snr_policy.apply_noise_band_fallback(
        noise_bands,
        robust_bands=robust_bands,
        baseline_bands=baseline_bands,
        covered={"treble": False},
    )
    assert adjusted[0]["basis"] == "deconvolved"
    assert adjusted[0]["level_dbfs"] == pytest.approx(-88.62)
