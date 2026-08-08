# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Characterization tests for the extracted measurement kernel.

P1b moved the pure measurement primitives (sweep / deconv / analysis /
calibration / quality) out of ``jasper.correction`` into the shared
``jasper.audio_measurement`` package and parameterized the previously-forked
capture-quality thresholds into a :class:`QualityModel`. That move is meant to
be **behavior-preserving** — no threshold value changed, no DSP math changed.

These tests pin exactly that:

1. A fixed, RNG-free ``sweep → synthetic-room-convolution → deconv →
   magnitude_response → smooth`` pipeline yields the same scalars it yielded
   before extraction (golden values baked below). Any accidental change to the
   moved math moves a golden and fails here.
2. Each :class:`QualityModel` profile (``ROOM`` / ``DRIVER`` / ``RAMP``) carries
   exactly the pre-extraction threshold values, and the module-level aliases
   still consumed by ``acoustic_quality.py`` / ``driver_acoustics.py`` equal
   them.
3. ``assess_capture`` is byte-identical under the ROOM and DRIVER profiles for
   the same input — the driver capture path used room correction's
   ``assess_capture`` verbatim before extraction, so passing ``DRIVER`` must not
   change its output.

Companion module-level math coverage already lives in
``tests/test_correction_sweep_deconv.py`` (roundtrips) and
``tests/test_correction_quality.py`` (issue codes); this file adds the golden
freeze and the QualityModel value contract.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.audio_measurement import analysis, deconv, quality, sweep
from jasper.audio_measurement.quality_model import DRIVER, RAMP, ROOM, QualityModel

SR = 48000
SMOOTH_EQUIVALENCE_ATOL_DB = 2e-9


def _scalar_smooth_fractional_octave(freqs, magnitude_db, fraction=48):
    """Independent reference matching the shipped per-bin definition."""
    if fraction <= 0:
        raise ValueError(f"fraction must be positive, got {fraction}")
    if len(freqs) != len(magnitude_db):
        raise ValueError(
            f"length mismatch: freqs={len(freqs)} magnitude={len(magnitude_db)}"
        )
    power = 10.0 ** (magnitude_db / 10.0)
    factor = 2.0 ** (1.0 / (2.0 * fraction))
    smoothed = np.empty_like(power)
    n = len(freqs)
    for i in range(n):
        f = freqs[i]
        if f <= 0:
            smoothed[i] = power[i]
            continue
        lo_idx = int(np.searchsorted(freqs, f / factor, side="left"))
        hi_idx = int(np.searchsorted(freqs, f * factor, side="right"))
        lo_idx = max(0, lo_idx)
        hi_idx = max(lo_idx + 1, min(n, hi_idx))
        smoothed[i] = float(np.mean(power[lo_idx:hi_idx]))
    return 10.0 * np.log10(np.maximum(smoothed, 1e-12))


# ---------- 1. Golden pipeline freeze --------------------------------------


def _golden_pipeline():
    """Fully deterministic sweep → synthetic room → deconv → FR → smooth.

    No RNG: the synthetic IR is a fixed direct-arrival + one delayed
    reflection, so every scalar below is reproducible bit-for-bit on the same
    numpy/scipy. Returns everything the assertions probe.
    """
    sig, meta = sweep.synchronized_swept_sine(
        f1=50.0,
        f2=18000.0,
        duration_approx_s=1.0,
        sample_rate=SR,
        amplitude_dbfs=-12.0,
    )
    ir = np.zeros(400, dtype=np.float64)
    ir[10] = 1.0
    ir[120] = 0.4
    captured = fftconvolve(sig.astype(np.float64), ir, mode="full")
    recovered = deconv.deconvolve(captured, sig.astype(np.float64), sample_rate=SR)
    freqs, mag = deconv.magnitude_response(recovered, SR, normalize=True)
    smoothed = analysis.smooth_fractional_octave(freqs, mag, 24)
    return sig, meta, recovered, freqs, mag, smoothed


def test_sweep_generation_golden_shape():
    sig, meta, *_ = _golden_pipeline()
    assert len(sig) == 45205
    assert meta.n_samples == 45205
    assert meta.L == pytest.approx(0.16, abs=1e-12)
    # Whole-signal fingerprints — a change in the sweep formula moves these.
    assert float(np.sum(np.abs(sig))) == pytest.approx(7181.7509765625, rel=1e-9)
    assert float(np.sum(sig[::97].astype(np.float64))) == pytest.approx(
        -0.4176864121818653, rel=1e-9
    )


def test_sweep_meta_strict_persisted_round_trip() -> None:
    _signal, meta = sweep.synchronized_swept_sine(duration_approx_s=0.2)
    assert sweep.SweepMeta.from_dict(meta.to_dict()) == meta
    for field, invalid in (
        ("sample_rate", 48000.0),
        ("n_samples", True),
        ("amplitude_dbfs", "-12"),
        ("duration_s", meta.duration_s + 0.1),
    ):
        with pytest.raises(ValueError):
            sweep.SweepMeta.from_dict({**meta.to_dict(), field: invalid})
    with pytest.raises(ValueError, match="schema"):
        sweep.SweepMeta.from_dict({**meta.to_dict(), "extra": 1})


@pytest.mark.parametrize(
    "amplitude_dbfs",
    (3.0, float("nan"), float("inf"), True, "-12"),
)
def test_sweep_generation_rejects_invalid_amplitude(amplitude_dbfs) -> None:
    with pytest.raises(ValueError, match="finite non-positive"):
        sweep.synchronized_swept_sine(
            duration_approx_s=0.2,
            amplitude_dbfs=amplitude_dbfs,
        )


def test_deconvolution_golden_recovery():
    _, _, recovered, *_ = _golden_pipeline()
    assert len(recovered) == 24010
    # Direct arrival recovered at the injected offset (pre-arrival window = 5 ms
    # → 240 samples, so the peak sits at index 10 within the trimmed IR).
    assert int(np.argmax(np.abs(recovered))) == 10
    assert float(np.max(np.abs(recovered))) == pytest.approx(
        0.6224174499511719, rel=1e-6
    )


def test_magnitude_and_smoothing_golden():
    _, _, _, freqs, mag, smoothed = _golden_pipeline()
    assert len(mag) == 16385
    assert len(smoothed) == len(freqs) == 16385
    # Probe the smoothed magnitude at fixed frequencies. A change in the FFT
    # length policy, the deconv regularizer, or the smoothing power-mean moves
    # any of these.
    probes = {
        100.0: -1.936692,
        500.0: -0.75041,
        2000.0: -6.213585,
        8000.0: -4.565471,
    }
    for probe_hz, golden in probes.items():
        idx = int(np.argmin(np.abs(freqs - probe_hz)))
        assert float(smoothed[idx]) == pytest.approx(golden, abs=1e-4)
    assert float(np.mean(smoothed)) == pytest.approx(-11.391156, abs=1e-4)


@pytest.mark.parametrize("size", [1, 2, 31, 257, 2049])
@pytest.mark.parametrize("fraction", [2, 3, 6, 24, 48])
def test_fractional_octave_smoothing_matches_scalar_reference(size, fraction):
    """Prefix sums preserve every scalar window on linear FFT-like grids."""
    freqs = np.linspace(0.0, 24000.0, size, dtype=np.float64)
    magnitude_db = np.random.default_rng(size * 100 + fraction).uniform(
        -45.0, 12.0, size
    )
    expected = _scalar_smooth_fractional_octave(freqs, magnitude_db, fraction)
    actual = analysis.smooth_fractional_octave(freqs, magnitude_db, fraction)
    max_error_db = float(np.max(np.abs(actual - expected)))
    # Prefix accumulation changes floating-point addition order. 2e-9 dB is
    # tight enough to catch a boundary/mean-domain change while remaining far
    # below any acoustically meaningful or persisted display precision.
    assert max_error_db <= SMOOTH_EQUIVALENCE_ATOL_DB, max_error_db


def test_fractional_octave_smoothing_matches_incident_sized_reference():
    """The 2^19-point VERIFY FFT shape stays numerically equivalent."""
    freqs = np.linspace(0.0, 24000.0, 524_289, dtype=np.float64)
    magnitude_db = (
        -18.0
        + 7.0 * np.sin(2.0 * np.pi * freqs / 3179.0)
        + 2.0 * np.cos(2.0 * np.pi * freqs / 431.0)
    )
    expected = _scalar_smooth_fractional_octave(freqs, magnitude_db, 6)
    actual = analysis.smooth_fractional_octave(freqs, magnitude_db, 6)
    max_error_db = float(np.max(np.abs(actual - expected)))
    assert max_error_db <= SMOOTH_EQUIVALENCE_ATOL_DB, max_error_db


def test_fractional_octave_smoothing_preserves_zero_and_nonfinite_edges():
    all_zero = np.full(7, -np.inf)
    freqs = np.array([-2.0, 0.0, 1.0, 2.0, 4.0, 8.0, 16.0])
    assert np.array_equal(
        analysis.smooth_fractional_octave(freqs, all_zero, 3),
        np.full(7, -120.0),
    )

    # NaN/+inf use the scalar fallback because prefix subtraction would
    # contaminate later windows that do not contain the non-finite bin.
    magnitude_db = np.array([-np.inf, -12.0, np.inf, np.nan, -6.0, -3.0, 0.0])
    expected = _scalar_smooth_fractional_octave(freqs, magnitude_db, 3)
    actual = analysis.smooth_fractional_octave(freqs, magnitude_db, 3)
    assert np.array_equal(actual, expected, equal_nan=True)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_fractional_octave_smoothing_preserves_dtype_and_shape(dtype):
    freqs = np.linspace(0.0, 24000.0, 129, dtype=dtype)
    magnitude_db = np.linspace(-30.0, 6.0, 129, dtype=dtype)
    expected = _scalar_smooth_fractional_octave(freqs, magnitude_db, 6)
    actual = analysis.smooth_fractional_octave(freqs, magnitude_db, 6)
    assert actual.shape == magnitude_db.shape
    assert actual.dtype == expected.dtype
    assert np.allclose(actual, expected, rtol=2e-6, atol=2e-6)


def test_fractional_octave_smoothing_preserves_validation_and_empty_result():
    with pytest.raises(ValueError, match="fraction must be positive, got 0"):
        analysis.smooth_fractional_octave(np.arange(2.0), np.arange(2.0), 0)
    with pytest.raises(ValueError, match="length mismatch: freqs=2 magnitude=1"):
        analysis.smooth_fractional_octave(np.arange(2.0), np.arange(1.0))
    empty = analysis.smooth_fractional_octave(
        np.array([], dtype=np.float64), np.array([], dtype=np.float32)
    )
    assert empty.shape == (0,)
    assert empty.dtype == np.float32


def test_fractional_octave_smoothing_uses_bulk_prefix_operations(monkeypatch):
    """Finite input is O(N log N) boundary search plus one O(N) prefix sum."""
    freqs = np.linspace(0.0, 24000.0, 4097)
    magnitude_db = np.sin(freqs / 1000.0)
    search_shapes = []
    cumsum_shapes = []
    original_searchsorted = np.searchsorted
    original_cumsum = np.cumsum

    def counted_searchsorted(a, values, *args, **kwargs):
        search_shapes.append(np.shape(values))
        return original_searchsorted(a, values, *args, **kwargs)

    def counted_cumsum(a, *args, **kwargs):
        cumsum_shapes.append(np.shape(a))
        return original_cumsum(a, *args, **kwargs)

    monkeypatch.setattr(analysis.np, "searchsorted", counted_searchsorted)
    monkeypatch.setattr(analysis.np, "cumsum", counted_cumsum)
    analysis.smooth_fractional_octave(freqs, magnitude_db, 6)

    assert search_shapes == [(4096,), (4096,)]
    assert cumsum_shapes == [(4097,)]


# ---------- 2. QualityModel value contract ---------------------------------


def test_quality_model_profiles_carry_preextraction_values():
    """The forked constants became data; the data must equal the constants.

    Pre-extraction values (verbatim):
      quality.py           PEAK_TOO_LOW_DBFS=-45.0  RMS_TOO_LOW_DBFS=-65.0
                           CLIP_ABS_THRESHOLD=0.999 CLIP_FRACTION_FAIL=1e-4
                           DBFS_FLOOR=-120.0
      acoustic_quality.py  SNR_OK_DB=25.0  SNR_WARN_DB=20.0
      driver_acoustics.py  SILENT_PEAK_DBFS=-45.0  DEFAULT_NULL_THRESHOLD_DB=6.0
                           OVERLAP_MIN_BINS=4
    """
    # Structural (shared across all profiles — digital-full-scale facts).
    for model in (ROOM, DRIVER, RAMP):
        assert model.dbfs_floor == -120.0
        assert model.clip_abs_threshold == 0.999
        assert model.clip_fraction_fail == 1e-4

    # ROOM — the room-correction capture-quality + SNR thresholds. Pinned
    # here (not just retuned in place) because jasper.correction.acoustic_quality
    # reads exactly these two fields for its own SNR_OK_DB/SNR_WARN_DB aliases —
    # a future crossover-motivated retune of ROOM would silently move room
    # correction's thresholds too if this pin didn't exist.
    assert ROOM.peak_too_low_dbfs == -45.0
    assert ROOM.rms_too_low_dbfs == -65.0
    assert ROOM.snr_ok_db == 25.0
    assert ROOM.snr_warn_db == 20.0
    # The band-specific SNR gate's decision-class split (P1b, "Level control
    # and SNR") lives on every profile as new fields with pre-existing-behavior
    # defaults — no profile overrides them, so room correction (which does not
    # yet call jasper.audio_measurement.snr_policy) is unaffected.
    assert ROOM.alignment_snr_ok_db == 35.0
    assert ROOM.null_cap_margin_db == 10.0

    # DRIVER — the active-crossover verdict thresholds…
    assert DRIVER.silent_peak_dbfs == -45.0
    assert DRIVER.null_threshold_db == 6.0
    assert DRIVER.overlap_min_bins == 4
    # …and its capture-quality/SNR fields MUST equal ROOM's, because the driver
    # capture path called room correction's assess_capture verbatim.
    assert DRIVER.peak_too_low_dbfs == ROOM.peak_too_low_dbfs
    assert DRIVER.rms_too_low_dbfs == ROOM.rms_too_low_dbfs
    assert DRIVER.snr_ok_db == ROOM.snr_ok_db
    assert DRIVER.snr_warn_db == ROOM.snr_warn_db
    assert DRIVER.alignment_snr_ok_db == ROOM.alignment_snr_ok_db
    assert DRIVER.null_cap_margin_db == ROOM.null_cap_margin_db

    # RAMP is a documented placeholder that reuses ROOM's values for now.
    assert RAMP == ROOM


def test_consumed_module_level_aliases_match_profiles():
    """Aliases still consumed across package boundaries stay profile-backed."""
    assert quality.DBFS_FLOOR == ROOM.dbfs_floor

    from jasper.correction import acoustic_quality

    assert acoustic_quality.SNR_OK_DB == ROOM.snr_ok_db
    assert acoustic_quality.SNR_WARN_DB == ROOM.snr_warn_db

    from jasper.active_speaker import driver_acoustics

    assert driver_acoustics.SILENT_PEAK_DBFS == DRIVER.silent_peak_dbfs
    assert driver_acoustics.DEFAULT_NULL_THRESHOLD_DB == DRIVER.null_threshold_db
    assert driver_acoustics.OVERLAP_MIN_BINS == DRIVER.overlap_min_bins


def test_default_quality_model_is_room():
    """assess_capture defaults to ROOM, so pre-extraction call sites (which
    passed no profile) are unaffected."""
    import inspect

    sig = inspect.signature(quality.assess_capture)
    assert sig.parameters["quality_model"].default is ROOM


# ---------- 3. assess_capture behavior parity ------------------------------


@pytest.mark.parametrize(
    "captured",
    [
        np.full(60000, 0.02, dtype=np.float32),  # well-formed, mid level
        np.full(60000, 3e-4, dtype=np.float32),  # low peak+rms → warnings fire
        np.concatenate(  # a clip event → fail path
            [np.ones(50, dtype=np.float32), np.zeros(59950, dtype=np.float32)]
        ),
    ],
)
def test_assess_capture_room_and_driver_identical(captured):
    """The driver profile must produce byte-identical assessment to ROOM for the
    same input — the driver path did not have its own capture-quality thresholds
    before extraction; parameterizing must not change its verdicts."""
    common = dict(
        sample_rate=SR,
        expected_sample_rate=SR,
        sweep_n_samples=48000,
        has_mic_calibration=True,
    )
    room = quality.assess_capture(captured, quality_model=ROOM, **common)
    driver = quality.assess_capture(captured, quality_model=DRIVER, **common)
    default = quality.assess_capture(captured, **common)  # default == ROOM
    assert room.to_dict() == driver.to_dict()
    assert room.to_dict() == default.to_dict()


def test_assess_capture_golden_dict():
    """Freeze the full assess_capture output for a fixed input so a change to
    the gate (thresholds, dBFS math, clip counting) is caught."""
    captured = np.full(60000, 0.02, dtype=np.float32)
    report = quality.assess_capture(
        captured,
        sample_rate=SR,
        expected_sample_rate=SR,
        sweep_n_samples=48000,
        has_mic_calibration=True,
    )
    assert report.to_dict() == {
        "sample_rate": 48000,
        "duration_s": 1.25,
        "peak_dbfs": pytest.approx(-33.97940028086514),
        "rms_dbfs": pytest.approx(-33.97940028086514),
        "clipped_fraction": 0.0,
        "failed": False,
        "warning_count": 0,
        "issues": [],
    }


def test_quality_model_is_frozen():
    """A profile is immutable data — accidental mutation would silently retune a
    layer at runtime."""
    with pytest.raises((AttributeError, TypeError)):
        ROOM.peak_too_low_dbfs = 0.0  # type: ignore[misc]
    # Default-constructed model equals ROOM (the room profile is the defaults).
    assert QualityModel() == ROOM
