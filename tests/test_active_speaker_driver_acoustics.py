# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Summed capture curves and validity-floor refusal."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile

from jasper.active_speaker import driver_acoustics as da
from jasper.audio_measurement import sweep as sweep_mod

SR = 48000


def _write_capture_slot(
    tmp_path,
    name,
    reference,
    *,
    gain,
    noise_sigma=0.001,
    pre_s=1.0,
    ambient_s=14.0,
    tail_s=0.6,
    pre_contamination=None,
    seed=17,
):
    """Write the real capture shape with a controlled 14 s paused interval."""

    rng = np.random.default_rng(seed)
    total = int(round((pre_s + ambient_s + tail_s) * SR)) + len(reference)
    full = rng.normal(0.0, noise_sigma, total)
    sweep_start = int(round((pre_s + ambient_s) * SR))
    if pre_contamination is not None:
        contamination = np.asarray(pre_contamination, dtype=np.float64)
        full[: min(sweep_start - int(ambient_s * SR), len(contamination))] += contamination[
            : min(sweep_start - int(ambient_s * SR), len(contamination))
        ]
    full[sweep_start:sweep_start + len(reference)] += gain * reference
    path = tmp_path / name
    wavfile.write(path, SR, full.astype(np.float32))
    return path


def test_summed_capture_curve_returns_a_readable_curve(tmp_path):
    """The capture half of a reverse null, and ONLY that half.

    It hands back the calibrated magnitude on the analysis grid. It computes no
    depth and reaches no verdict — the subtraction belongs to
    ``analysis.crossover_null_depth_db`` and the shoulders to
    ``analysis.shoulder_span``, so a door composing all three reads the same
    quantity a computed proposal does.
    """
    reference, sweep_meta = sweep_mod.synchronized_swept_sine(
        f1=200.0, f2=8000.0, duration_approx_s=1.0, sample_rate=SR,
        amplitude_dbfs=da.DEFAULT_AMPLITUDE_DBFS,
    )
    path = _write_capture_slot(tmp_path, "null-curve.wav", reference, gain=0.2)

    curve = da.summed_capture_curve(
        path, sweep_meta.to_dict(), crossover_fc_hz=2000.0,
        capture_geometry="near_field",
    )

    assert curve is not None
    assert len(curve.freqs) == len(curve.magnitude_db)
    assert curve.above_validity_floor is True
    # Dense enough either side of Fc for a shoulder to be placed — the property
    # the door's `shoulder_span` call depends on this grid for.
    assert int(np.count_nonzero(curve.freqs < 2000.0)) >= 2
    assert int(np.count_nonzero(curve.freqs > 2000.0)) >= 2


def test_summed_capture_curve_refuses_a_capture_below_its_validity_floor(
    tmp_path, monkeypatch
):
    """``None`` when the ROOM would supply the reference, not the crossover.

    A reference-axis capture whose low-frequency validity floor sits above the
    lower shoulder (``Fc/2``) cannot decide a null there. Refused rather than
    returned, because a depth read off contaminated data is a number a reader
    cannot tell from a measurement.
    """
    from jasper.audio_measurement import gating

    reference, sweep_meta = sweep_mod.synchronized_swept_sine(
        f1=200.0, f2=8000.0, duration_approx_s=1.0, sample_rate=SR,
        amplitude_dbfs=da.DEFAULT_AMPLITUDE_DBFS,
    )
    path = _write_capture_slot(tmp_path, "null-floor.wav", reference, gain=0.2)

    def _gate_with_high_floor(ir, sample_rate, **_kwargs):
        return ir, {
            "schema_version": 1,
            "direct_peak_ms": 5.0,
            "first_reflection_ms": 8.0,
            "window_ms": 8.0,
            "window": "half_hann_tail",
            # Above Fc/2 = 1000 Hz, so the lower shoulder is undecidable.
            "f_valid_floor_hz": 1500.0,
            "floor_source": "gate_window",
        }

    monkeypatch.setattr(gating, "gate_impulse_response", _gate_with_high_floor)

    assert da.summed_capture_curve(
        path, sweep_meta.to_dict(), crossover_fc_hz=2000.0,
        capture_geometry="reference_axis",
    ) is None
    with pytest.raises(da.SummedCaptureUnusable) as caught:
        da.summed_capture_curve(path, sweep_meta.to_dict(), crossover_fc_hz=2000,
                                capture_geometry="reference_axis", raise_on_unusable=True)
    assert caught.value.diagnostics["reason"] == "gate_excludes_lower_shoulder"
    assert caught.value.diagnostics["gating"]["f_valid_floor_hz"] == 1500
    assert caught.value.diagnostics["required_lower_shoulder_hz"] == 1000
    readable = da.summed_capture_curve(path, sweep_meta.to_dict(), crossover_fc_hz=2000,
                                      capture_geometry="reference_axis", overlap_hz=(1600, 4000))
    assert readable is not None
    assert readable.shoulders.used_hz[0] >= 1600
