# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Decision-class + band-specific SNR gate (P1b).

Pins the split SNR policy from "Level control and SNR" in
docs/active-crossover-information-design.md:

  - :data:`CROSSOVER_SNR_BANDS_HZ`'s first four rows are :data:`SNR_BANDS_HZ`,
    the one room table, so the room and crossover tables cannot drift apart
    (pinned in ``test_audio_measurement_boundary_ssot.py``).
  - :func:`band_snr_verdicts` — magnitude/trim tiers at 25/20 dB (reusing
    ``QualityModel.snr_ok_db``/``snr_warn_db``), the stricter 35 dB alignment
    tier that rejects scalar-only evidence, and the worst-RELEVANT-band
    partial-pass rule (a bad octave outside the window a decision depends on
    must not veto it).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from jasper.audio_measurement import snr_policy, sweep
from jasper.audio_measurement.program_analysis.model import sweep_band_crest_factor_db
from jasper.audio_measurement.quality import dbfs
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.sweep_levels import sweep_band_levels

SR = 48000


@pytest.mark.parametrize("quiet_seconds", [0, 4])
def test_sweep_dwell_power_and_noise_use_the_same_units(quiet_seconds):
    signal, meta = sweep.synchronized_swept_sine(20, 200, 8, amplitude_dbfs=-20)
    rng = np.random.default_rng(91)
    quiet = rng.normal(0, 0.001, SR * quiet_seconds)
    capture = signal + rng.normal(0, 0.001, signal.size)
    rows = sweep_band_levels(capture, quiet, SR, meta, 0, [(10, 20), (50, 80), (200, 300)])
    assert len(rows) == 1
    row = rows[0]
    assert row["signal_plus_noise_dbfs"] == pytest.approx(-23.01, abs=0.3)
    if not quiet_seconds:
        assert row["noise_p10_p50_p90_dbfs"] is None
        assert row["estimated_snr_db"] is None
        assert row["quiet_windows"] == 0
        return
    noise_db = -60 + 10 * math.log10(30 / (SR / 2))
    assert row["noise_p10_p50_p90_dbfs"][1] == pytest.approx(noise_db, abs=1)
    assert row["estimated_snr_db"] == pytest.approx(-23.01 - noise_db, abs=2)
    assert row["quiet_windows"] > 1


def _bands(rows):
    """[(band_id, lo, hi, level_dbfs), ...] -> the correction-shape band list."""
    return [
        {"band_id": band_id, "band_hz": [lo, hi], "level_dbfs": level}
        for band_id, lo, hi, level in rows
    ]


# ---------- band_levels_dbfs / CROSSOVER_SNR_BANDS_HZ -----------------------


def test_band_levels_dbfs_reports_true_band_power():
    """D1 (#1838): a band's ``level_dbfs`` is what a band-pass + RMS meter
    reads, in real dBFS — the defect that made the #1829 level solve read the
    room 18-39 dB too quiet.

    Two independent known-power fixtures, both closed-form:

    * a -20 dBFS sine parked mid-band reads -20 dBFS in its own band;
    * white noise of known variance reads its exact band share,
      ``level = 20*log10(sigma) + 10*log10(bandwidth / nyquist)``.

    The pre-fix estimator (``sqrt(mean(power[mask])) / x.size``) returned a
    per-BIN mean instead, low by ``7.27 + 10*log10(n_bins)``: -62.8 for the
    sine and -111 for every one of the noise bands, saturating the evidence
    flat. Tolerances are tight on purpose — this is arithmetic with a right
    answer, not a heuristic.
    """
    # 1) Pure tone, well inside `mid` (2 kHz, clear of both band edges so no
    #    Hann skirt leaks across into `transition`).
    t = np.arange(SR) / SR
    tone = (10.0 ** (-20.0 / 20.0)) * np.sqrt(2.0) * np.sin(2 * np.pi * 2000.0 * t)
    by_id = {
        row["band_id"]: row["level_dbfs"]
        for row in snr_policy.band_levels_dbfs(
            tone, SR, snr_policy.CROSSOVER_SNR_BANDS_HZ
        )
    }
    assert by_id["mid"] == pytest.approx(-20.0, abs=0.1)
    # ...and the tone's energy is not double-counted into its neighbours.
    assert by_id["transition"] < -100.0
    assert by_id["treble"] < -100.0

    # 2) White noise: every band reads its own closed-form share. Four
    #    seconds so even the narrowest band (`sub_bass`, 60 Hz) averages
    #    enough bins for the chi-square spread of the estimate to sit well
    #    inside the tolerance.
    sigma = 0.001  # -60 dBFS broadband
    noise = np.random.default_rng(1838).normal(0.0, sigma, SR * 4)
    levels = {
        row["band_id"]: row["level_dbfs"]
        for row in snr_policy.band_levels_dbfs(
            noise, SR, snr_policy.CROSSOVER_SNR_BANDS_HZ
        )
    }
    for band_id, lo, hi in snr_policy.CROSSOVER_SNR_BANDS_HZ:
        expected = 20.0 * np.log10(sigma) + 10.0 * np.log10((hi - lo) / (SR / 2))
        assert levels[band_id] == pytest.approx(expected, abs=0.8), band_id
        # And nowhere near the floor the pre-fix estimator pinned them to.
        assert levels[band_id] > snr_policy.DBFS_FLOOR + 25.0


def test_band_levels_dbfs_is_independent_of_capture_length():
    """The pre-fix estimator was not a stable statistic: because its per-bin
    mean diluted with ``n_bins``, the SAME stationary noise read -111.4 /
    -114.4 / -117.1 dBFS over 1 / 2 / 4 s. That is why the SNR ratio only
    cancelled when both sides shared a window length — and room correction's
    ``capture_band_snr`` compares a full sweep capture against a separately
    recorded, differently-sized noise WAV. Band power is length-invariant.
    """
    rng = np.random.default_rng(4242)
    # One wide band, so the reading is dominated by the scaling under test
    # rather than by the chi-square spread of a 60-bin narrow-band estimate.
    wide = (("wide", 20.0, 12000.0),)
    levels = [
        snr_policy.band_levels_dbfs(
            rng.normal(0.0, 0.001, SR * seconds), SR, wide
        )[0]["level_dbfs"]
        for seconds in (1, 2, 4)
    ]
    # Pre-fix these spanned a full 10*log10(4) = 6 dB, monotonically
    # decreasing with duration; the true band level does not move at all.
    assert max(levels) - min(levels) < 0.5


# ---------- window="rectangular" for non-stationary sweep captures (#1847) --


def _room_correction_sweep() -> tuple[np.ndarray, float, float, float]:
    """Room correction's own sweep shape, rendered through the real generator.

    ``correction/session.py``'s ``SessionConfig`` defaults: 20 Hz-20 kHz,
    ~10 s @ 48 kHz. Returns ``(stimulus, peak_dbfs, f1, f2)`` — peak measured
    directly off the rendered signal (not assumed from ``amplitude_dbfs``),
    matching ``test_sweep_band_crest_factor_matches_the_rendered_sweep``'s
    own rigor.
    """
    f1, f2 = 20.0, 20000.0
    stimulus, _ = sweep.synchronized_swept_sine(
        f1=f1, f2=f2, duration_approx_s=10.0,
        sample_rate=SR, amplitude_dbfs=0.0,
    )
    stimulus = np.asarray(stimulus, dtype=np.float64)
    peak_dbfs = 20.0 * math.log10(float(np.max(np.abs(stimulus))))
    return stimulus, peak_dbfs, f1, f2


def test_band_levels_dbfs_rectangular_window_matches_the_sweep_law():
    """#1847: a chirp's band split, measured with ``window="rectangular"``,
    matches the closed-form dwell-time law within a tight tolerance — on
    room correction's OWN sweep shape and OWN four bands, both "near the
    edge" and "mid-sweep".

    ``sweep_band_crest_factor_db`` is the analytical peak-to-band-RMS law
    for an exponential sweep, already validated to within 0.03-0.7 dB
    against a RECTANGULAR-window measurement of the identical generator
    (``test_sweep_band_crest_factor_matches_the_rendered_sweep`` in
    ``tests/test_audio_measurement_program_analysis.py`` — see that test's
    own docstring for why rectangular, not Hann, is the correct window for
    a non-stationary sweep). This test applies the SAME check to
    ``band_levels_dbfs(..., window="rectangular")`` directly, on room
    correction's four capture-quality bands rather than a driver's SNR
    solve bands.

    ``sub_bass`` (20-80 Hz) sits at the very START of this low-to-high
    sweep — the edge Hann attenuates hardest, and where the reported
    #1847 bias was largest (~-10 dB). ``transition`` (350-1000 Hz) sits
    ``ln(1000/20)/ln(20000/20) ≈ 57%`` into the sweep's duration — close to
    Hann's PEAK gain, where the reported bias flipped positive (+4.0 dB).
    One sweep therefore already exercises both regimes without a second
    fixture.
    """
    stimulus, peak_dbfs, f1, f2 = _room_correction_sweep()
    levels = {
        row["band_id"]: row["level_dbfs"]
        for row in snr_policy.band_levels_dbfs(
            stimulus, SR, snr_policy.SNR_BANDS_HZ, window="rectangular",
        )
    }
    for band_id, lo, hi in snr_policy.SNR_BANDS_HZ:
        predicted = peak_dbfs - sweep_band_crest_factor_db(
            (f1, f2), (lo, hi)
        )
        assert levels[band_id] == pytest.approx(predicted, abs=0.3), band_id


def test_band_levels_dbfs_hann_default_still_biases_a_sweep():
    """Contrast guard: the DEFAULT window is unchanged. A caller that does
    not opt into ``window="rectangular"`` still sees #1847's bias on a
    sweep — proving the fix is genuinely opt-in (the stationary-ambient
    callers of this function are untouched) and that the reported bias is
    real and reproducible on demand, not an artifact of one session's log.
    """
    stimulus, peak_dbfs, f1, f2 = _room_correction_sweep()
    predicted_sub_bass = peak_dbfs - sweep_band_crest_factor_db(
        (f1, f2), (20.0, 80.0)
    )
    hann_levels = {
        row["band_id"]: row["level_dbfs"]
        for row in snr_policy.band_levels_dbfs(
            stimulus, SR, snr_policy.SNR_BANDS_HZ,
        )  # default window="hann"
    }
    # The reported bias was ~-10 dB; assert at least half of that survives
    # under the unchanged default so this fails loudly if a future edit
    # flips the default and silently loses the ambient-side regression
    # coverage the Hann window still needs.
    assert hann_levels["sub_bass"] < predicted_sub_bass - 5.0


def test_band_levels_dbfs_rejects_an_unknown_window():
    with pytest.raises(ValueError):
        snr_policy.band_levels_dbfs(
            np.zeros(SR), SR, snr_policy.CROSSOVER_SNR_BANDS_HZ, window="boxcar",
        )


def test_band_snr_verdicts_are_unchanged_by_the_band_power_rescale():
    """The ratio consumers' cancellation, SHOWN rather than assumed (#1838).

    ``band_snr_verdicts`` subtracts a noise level from a capture level. The
    D1 rescale adds the SAME per-band offset to both sides whenever both are
    measured over equal-length windows, so every ``estimated_snr_db`` and
    every verdict must be byte-identical to what the pre-fix estimator
    produced. This replays the pre-fix math locally (the deleted expression,
    kept here as the reference) and asserts the verdict block matches.
    """
    rng = np.random.default_rng(770)
    capture = rng.normal(0.0, 0.05, SR)
    noise = rng.normal(0.0, 0.0008, SR)

    def _pre_fix_levels(x):
        window = np.hanning(x.size)
        power = np.abs(np.fft.rfft(x * window)) ** 2
        freqs = np.fft.rfftfreq(x.size, d=1.0 / SR)
        rows = []
        for band_id, lo, hi in snr_policy.CROSSOVER_SNR_BANDS_HZ:
            mask = (freqs >= lo) & (freqs < hi)
            rms_like = np.sqrt(float(np.mean(power[mask]))) / x.size
            rows.append({
                "band_id": band_id,
                "band_hz": [lo, hi],
                "level_dbfs": round(dbfs(float(rms_like), floor=snr_policy.DBFS_FLOOR), 2),
            })
        return rows

    def _verdicts(capture_bands, noise_bands):
        return snr_policy.band_snr_verdicts(
            decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
            capture_bands=capture_bands,
            noise_bands=noise_bands,
            noise_floor_dbfs_scalar=None,
            relevant_hz=(20.0, 12000.0),
            model=DRIVER,
        )

    fixed = _verdicts(
        snr_policy.band_levels_dbfs(capture, SR, snr_policy.CROSSOVER_SNR_BANDS_HZ),
        snr_policy.band_levels_dbfs(noise, SR, snr_policy.CROSSOVER_SNR_BANDS_HZ),
    )
    pre_fix = _verdicts(_pre_fix_levels(capture), _pre_fix_levels(noise))

    assert fixed["verdict"] == pre_fix["verdict"]
    assert fixed["worst_relevant"] == pre_fix["worst_relevant"]
    for fixed_band, pre_fix_band in zip(fixed["bands"], pre_fix["bands"], strict=True):
        assert fixed_band["band_id"] == pre_fix_band["band_id"]
        assert fixed_band["verdict"] == pre_fix_band["verdict"]
        # Rounding of the two levels can move the difference by one 0.1 step;
        # the SNR is otherwise identical.
        assert fixed_band["estimated_snr_db"] == pytest.approx(
            pre_fix_band["estimated_snr_db"], abs=0.2
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


# ---------- worst_band_verdict: the equal-verdict tie-break (issue #2026) -----
#
# Verdict rank still dominates (the refusal path is unchanged); among EQUAL
# verdicts the entry with the LOWEST estimated_snr_db wins, because that is
# the band that actually limits the measurement.


def test_worst_band_verdict_breaks_equal_verdict_ties_on_lowest_snr():
    """Issue #2026's reproduction: all-`ok` sweep bands, 17 dB apart.

    Table order would return `sweep_low` at 55 dB; the band that actually
    limits the measurement is `sweep_high` at 38 dB.
    """
    bands = [
        {"band_id": "sweep_low", "band_hz": [800.0, 1269.9],
         "estimated_snr_db": 55.0, "verdict": "ok"},
        {"band_id": "sweep_mid", "band_hz": [1269.9, 2015.9],
         "estimated_snr_db": 41.0, "verdict": "ok"},
        {"band_id": "sweep_high", "band_hz": [2015.9, 3200.0],
         "estimated_snr_db": 38.0, "verdict": "ok"},
    ]
    worst = snr_policy.worst_band_verdict(bands, 800.0, 3200.0)
    assert worst["band_id"] == "sweep_high"
    assert worst["estimated_snr_db"] == pytest.approx(38.0)


def test_worst_band_verdict_lowest_snr_wins_regardless_of_table_order():
    """The pick is by value, not position — reversing the table cannot move it.

    Guards the run-to-run instability reported on #2026, where which third
    landed first changed the reported SNR by ~5 dB on noise seed alone.
    """
    bands = [
        {"band_id": "sweep_low", "band_hz": [800.0, 1269.9],
         "estimated_snr_db": 38.0, "verdict": "ok"},
        {"band_id": "sweep_mid", "band_hz": [1269.9, 2015.9],
         "estimated_snr_db": 41.0, "verdict": "ok"},
        {"band_id": "sweep_high", "band_hz": [2015.9, 3200.0],
         "estimated_snr_db": 55.0, "verdict": "ok"},
    ]
    forward = snr_policy.worst_band_verdict(bands, 800.0, 3200.0)
    reverse = snr_policy.worst_band_verdict(list(reversed(bands)), 800.0, 3200.0)
    assert forward["band_id"] == reverse["band_id"] == "sweep_low"
    assert forward["estimated_snr_db"] == reverse["estimated_snr_db"] == 38.0


def test_worst_band_verdict_rank_still_dominates_snr():
    """No-change guard on the REFUSAL path.

    An `insufficient` band wins over every `ok` sibling even when its SNR is
    the HIGHEST in the window, and even when it is last in table order.
    """
    bands = [
        {"band_id": "sweep_low", "band_hz": [800.0, 1269.9],
         "estimated_snr_db": 5.0, "verdict": "ok"},
        {"band_id": "sweep_mid", "band_hz": [1269.9, 2015.9],
         "estimated_snr_db": 8.0, "verdict": "reduced"},
        {"band_id": "sweep_high", "band_hz": [2015.9, 3200.0],
         "estimated_snr_db": 34.9, "verdict": "insufficient"},
    ]
    worst = snr_policy.worst_band_verdict(bands, 800.0, 3200.0)
    assert worst["band_id"] == "sweep_high"
    assert worst["verdict"] == "insufficient"


def test_worst_band_verdict_prefers_numeric_evidence_over_missing_snr():
    """A same-verdict band with no usable number never displaces one that has
    a number: a consumer cannot grade against ``None``/NaN."""
    for absent in (None, float("nan"), "n/a"):
        bands = [
            {"band_id": "no_number", "band_hz": [800.0, 1600.0],
             "estimated_snr_db": absent, "verdict": "ok"},
            {"band_id": "numeric", "band_hz": [1600.0, 3200.0],
             "estimated_snr_db": 40.0, "verdict": "ok"},
        ]
        worst = snr_policy.worst_band_verdict(bands, 800.0, 3200.0)
        assert worst["band_id"] == "numeric", absent
        # ... and in the other table order too.
        worst = snr_policy.worst_band_verdict(
            list(reversed(bands)), 800.0, 3200.0
        )
        assert worst["band_id"] == "numeric", absent


def test_band_snr_verdicts_worst_relevant_is_the_lowest_snr_ok_band():
    """End-to-end through the builder: `worst_relevant` reports the
    lowest-SNR band of the window, not the first one."""
    capture_bands = [
        {"band_id": "sweep_low", "band_hz": [800.0, 1269.9], "level_dbfs": -10.0},
        {"band_id": "sweep_mid", "band_hz": [1269.9, 2015.9], "level_dbfs": -10.0},
        {"band_id": "sweep_high", "band_hz": [2015.9, 3200.0], "level_dbfs": -10.0},
    ]
    noise_bands = [
        {"band_id": "sweep_low", "level_dbfs": -65.0},   # 55 dB
        {"band_id": "sweep_mid", "level_dbfs": -51.0},   # 41 dB
        {"band_id": "sweep_high", "level_dbfs": -48.0},  # 38 dB
    ]
    block = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
        capture_bands=capture_bands,
        noise_bands=noise_bands,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(800.0, 3200.0),
        model=DRIVER,
        band_method="deconvolved_band_difference",
    )
    assert block["verdict"] == "ok"
    assert block["worst_relevant"]["band_id"] == "sweep_high"
    assert block["worst_relevant"]["estimated_snr_db"] == pytest.approx(38.0)


def test_magnitude_worst_relevant_is_the_lowest_of_equal_insufficient_bands():
    """The LIVE magnitude route, which is where the tie-break actually bites.

    ``program_analysis.response.driver_response`` uses :func:`band_snr_verdicts` with
    ``decision_class="magnitude"``. A noisy room can put every band in the
    driver's window at the same ``insufficient`` verdict — the exact shape
    ``jasper.web.correction_crossover_backend``'s completion-time correction
    reads (its own comment cites hardware run 19: 16.3 / 13.4 / 7.8 dB).

    `worst_relevant` there is not a display value: the backend subtracts it
    from the solver's requirement to size a level shortfall, so grading against
    the first band in table order UNDERSTATES it and solves too quiet.
    """
    capture_bands = [
        {"band_id": "sub_bass", "band_hz": [20.0, 80.0], "level_dbfs": -30.0},
        {"band_id": "bass", "band_hz": [80.0, 160.0], "level_dbfs": -30.0},
        {"band_id": "upper_bass", "band_hz": [160.0, 350.0], "level_dbfs": -30.0},
        {"band_id": "transition", "band_hz": [350.0, 1000.0], "level_dbfs": -30.0},
        {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -30.0},
        {"band_id": "treble", "band_hz": [4000.0, 12000.0], "level_dbfs": -30.0},
    ]
    noise_bands = [
        {"band_id": "sub_bass", "level_dbfs": -44.2},    # 14.2 dB, first in table
        {"band_id": "bass", "level_dbfs": -43.7},        # 13.7 dB
        {"band_id": "upper_bass", "level_dbfs": -37.8},  #  7.8 dB <- the true worst
        {"band_id": "transition", "level_dbfs": -43.2},  # 13.2 dB
        {"band_id": "mid", "level_dbfs": -60.0},         # 30.0 dB, outside window
        {"band_id": "treble", "level_dbfs": -60.0},      # 30.0 dB, outside window
    ]
    block = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture_bands,
        noise_bands=noise_bands,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(40.0, 400.0),  # a woofer passband
        model=DRIVER,
        band_method="deconvolved_band_difference",
    )

    # The refusal signal is unchanged — every in-window band is insufficient.
    assert block["verdict"] == "insufficient"
    assert {
        b["band_id"] for b in block["bands"]
        if b["band_hz"][1] > 40.0 and b["band_hz"][0] < 400.0
    } == {"sub_bass", "bass", "upper_bass", "transition"}
    assert all(
        b["verdict"] == "insufficient" for b in block["bands"]
        if b["band_hz"][1] > 40.0 and b["band_hz"][0] < 400.0
    )

    # ...but the graded number is the lowest, not `sub_bass`'s 14.2 dB.
    assert block["worst_relevant"]["band_id"] == "upper_bass"
    assert block["worst_relevant"]["estimated_snr_db"] == pytest.approx(7.8)
