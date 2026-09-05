# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Numeric-equivalence pins for jasper.dsp_numpy.

These kernels replaced live `scipy.signal` calls on the AEC bridge's
reference path and the assistant's TTS playout path, so "close enough"
is not a verdict anyone can hear their way to. Bit-exactness is pinned
against the textbook recursion spelled out in this module, which is the
kernel's own contract; scipy is a second oracle, for correctness rather than
for float order, and a drifting one, because it rounds that same recursion
its own way from release to release, so it is held to float rounding and not
to the bit. The scipy comparisons run whenever scipy is importable; the
textbook-recursion, shape, gain, stability and chunk-continuity checks run
always, because the boxes this ships to have no scipy at all.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.dsp_numpy import butter2_highpass_sos, resample_poly, sosfilt

REF_RATE = 48_000
MIC_RATE = 16_000
#: One 20 ms AEC reference block at 48 kHz: what `ReferenceFrameConverter`
#: resamples in one go (`FRAME_SAMPLES * REF_RATE // SAMPLE_RATE`).
REF_BLOCK = 960


def _speechlike(n: int, rate: float, seed: int = 5) -> np.ndarray:
    """Voiced fundamental + a formant + broadband noise, at int16 scale."""
    t = np.arange(n) / rate
    rng = np.random.default_rng(seed)
    return (
        7000.0 * np.sin(2.0 * np.pi * 180.0 * t)
        + 2500.0 * np.sin(2.0 * np.pi * 2600.0 * t + 0.7)
        + 400.0 * rng.standard_normal(n)
    )


def _full_scale_square(n: int, rate: float, hz: float = 220.0) -> np.ndarray:
    return 32767.0 * np.sign(np.sin(2.0 * np.pi * hz * np.arange(n) / rate))


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x))))


def _lag_of_peak_correlation(a: np.ndarray, b: np.ndarray) -> int:
    """Sample lag maximising cross-correlation; 0 means aligned."""
    a = a - a.mean()
    b = b - b.mean()
    corr = np.correlate(a, b, mode="full")
    return int(np.argmax(corr)) - (b.size - 1)


# --- resample_poly -------------------------------------------------------


@pytest.mark.parametrize(
    ("up", "down", "rate", "n"),
    [
        pytest.param(1, 3, REF_RATE, REF_BLOCK, id="ref-48k-to-16k"),
        pytest.param(2, 1, 24_000, 1_200, id="tts-24k-to-48k"),
    ],
)
@pytest.mark.parametrize("signal", ["speechlike", "square"])
def test_resample_poly_matches_scipy(up, down, rate, n, signal):
    scipy_signal = pytest.importorskip("scipy.signal")
    source = (
        _speechlike(n, rate) if signal == "speechlike"
        else _full_scale_square(n, rate)
    )

    out = resample_poly(source, up, down)
    reference = scipy_signal.resample_poly(source, up=up, down=down)

    assert out.shape == reference.shape
    # -280 dB relative: the taps and the output phase are the reference's, so
    # only float64 rounding is left.
    assert _rms(out - reference) <= 1e-14 * _rms(reference)
    assert float(np.max(np.abs(out - reference))) <= 1e-12 * float(
        np.max(np.abs(reference))
    )
    assert _lag_of_peak_correlation(out, reference) == 0


def test_resample_poly_matches_scipy_on_the_live_float32_reference():
    """The bridge feeds float32; scipy filters in float32, we filter in float64."""
    scipy_signal = pytest.importorskip("scipy.signal")
    source = _speechlike(REF_BLOCK, REF_RATE).astype(np.float32)

    out = resample_poly(source, 1, 3)
    reference = scipy_signal.resample_poly(source, up=1, down=3)

    # float32 rounding inside scipy dominates: -100 dB relative, ~1e-3 of one
    # int16 LSB on a signal that is about to be rounded to int16 anyway.
    assert _rms(out - reference) <= 1e-5 * _rms(reference)
    assert _lag_of_peak_correlation(out, reference) == 0


@pytest.mark.parametrize(
    ("up", "down", "n", "expected"),
    [(1, 3, REF_BLOCK, 320), (1, 3, 961, 321), (2, 1, 1_200, 2_400),
     (2, 1, 1, 2), (2, 1, 0, 0), (4, 6, 90, 60)],
)
def test_resample_poly_output_length(up, down, n, expected):
    assert resample_poly(np.zeros(n), up, down).size == expected


@pytest.mark.parametrize(("up", "down"), [(1, 3), (2, 1), (3, 2)])
def test_resample_poly_holds_dc_gain(up, down):
    """Unity passband: resampling must not shift the AEC reference's level.

    0.1%: the interpolating phases do not each carry exactly 1/up of the DC
    gain, so a constant comes back with a little per-phase ripple. scipy's
    taps have the same ripple — this pins the level, not the ripple.
    """
    out = resample_poly(np.full(2_400, 1_000.0), up, down)

    interior = out[64:-64]
    assert np.allclose(interior, 1_000.0, rtol=1e-3, atol=0.0)


# --- butter2_highpass_sos ------------------------------------------------


@pytest.mark.parametrize("cutoff", [40.0, 125.0, 300.0, 1_000.0])
def test_butter2_highpass_sos_matches_scipy(cutoff):
    scipy_signal = pytest.importorskip("scipy.signal")

    out = butter2_highpass_sos(cutoff, MIC_RATE)
    reference = scipy_signal.butter(
        2, cutoff, btype="highpass", fs=MIC_RATE, output="sos",
    )

    assert out.shape == reference.shape
    assert float(np.max(np.abs(out - reference))) <= 1e-12


@pytest.mark.parametrize("cutoff", [40.0, 125.0, 1_000.0])
def test_butter2_highpass_sos_is_stable_and_blocks_dc(cutoff):
    sos = butter2_highpass_sos(cutoff, MIC_RATE)

    poles = np.roots(np.concatenate(([1.0], sos[0, 4:])))
    assert float(np.max(np.abs(poles))) < 1.0
    # A high-pass has a numerator zero of order 2 at DC.
    assert abs(float(np.sum(sos[0, :3]))) <= 1e-12


@pytest.mark.parametrize("cutoff", [0.0, -1.0, 8_000.0, 9_000.0])
def test_butter2_highpass_sos_rejects_a_cutoff_outside_the_band(cutoff):
    with pytest.raises(ValueError):
        butter2_highpass_sos(cutoff, MIC_RATE)


# --- sosfilt -------------------------------------------------------------


def _df2t(sos, x, zi):
    """Textbook transposed direct form II, cascaded; each `a0` (column 3) is 1."""
    sections = [[float(v) for v in row] for row in sos]
    state = [[float(v) for v in row] for row in zi]
    out = []
    for sample in x:
        carried = float(sample)
        for i, (b0, b1, b2, _a0, a1, a2) in enumerate(sections):
            value = b0 * carried + state[i][0]
            state[i][0] = b1 * carried - a1 * value + state[i][1]
            state[i][1] = b2 * carried - a2 * value
            carried = value
        out.append(carried)
    return np.array(out), np.array(state)


def _sosfilt_fixture(signal):
    sos = butter2_highpass_sos(125.0, MIC_RATE)
    source = (
        _speechlike(1_280, MIC_RATE) if signal == "speechlike"
        else _full_scale_square(1_280, MIC_RATE)
    )
    zi = np.zeros((sos.shape[0], 2))
    return sos, source, zi


@pytest.mark.parametrize("signal", ["speechlike", "square"])
def test_sosfilt_matches_the_textbook_recursion(signal):
    sos, source, zi = _sosfilt_fixture(signal)

    out, state = sosfilt(sos, source, zi=zi)
    textbook, textbook_state = _df2t(sos, source, zi)

    assert out.shape == textbook.shape
    # The recursion and the float order it is evaluated in are the contract.
    assert float(np.max(np.abs(out - textbook))) == 0.0
    assert float(np.max(np.abs(state - textbook_state))) == 0.0


@pytest.mark.parametrize("signal", ["speechlike", "square"])
def test_sosfilt_agrees_with_scipy_to_float_rounding(signal):
    scipy_signal = pytest.importorskip("scipy.signal")
    sos, source, zi = _sosfilt_fixture(signal)

    out, state = sosfilt(sos, source, zi=zi)
    reference, ref_state = scipy_signal.sosfilt(sos, source, zi=zi)

    assert out.shape == reference.shape
    # scipy computes the same recursion with less rounding than plain float64,
    # so it pins correctness only, to a tolerance a hundredth of an int16 LSB.
    np.testing.assert_allclose(out, reference, rtol=0.0, atol=1e-8)
    np.testing.assert_allclose(state, ref_state, rtol=0.0, atol=1e-8)
    assert _lag_of_peak_correlation(out, reference) == 0


def test_sosfilt_carries_state_across_chunk_boundaries():
    """Filtering in chunks must equal filtering the stream whole.

    The AEC reference arrives one UDP datagram at a time; a dropped `zi` would
    put a settling transient at every block edge, which the AEC3 delay
    estimator sees as reference noise.
    """
    sos = butter2_highpass_sos(125.0, MIC_RATE)
    source = _speechlike(1_280, MIC_RATE)
    zi = np.zeros((sos.shape[0], 2))

    whole, _ = sosfilt(sos, source, zi=zi)
    chunked = []
    state = zi
    for start in range(0, source.size, 160):
        part, state = sosfilt(sos, source[start:start + 160], zi=state)
        chunked.append(part)

    assert float(np.max(np.abs(np.concatenate(chunked) - whole))) == 0.0


def test_sosfilt_blocks_dc_and_passes_the_voice_band():
    sos = butter2_highpass_sos(125.0, MIC_RATE)
    zi = np.zeros((sos.shape[0], 2))
    n = 8_000
    t = np.arange(n) / MIC_RATE

    dc, _ = sosfilt(sos, np.full(n, 5_000.0), zi=zi)
    voice, _ = sosfilt(sos, 5_000.0 * np.sin(2.0 * np.pi * 1_000.0 * t), zi=zi)

    assert _rms(dc[n // 2:]) <= 1e-6 * 5_000.0
    assert _rms(voice[n // 2:]) == pytest.approx(5_000.0 / np.sqrt(2.0), rel=0.02)


def test_sosfilt_settles_rather_than_diverging_on_a_full_scale_square():
    sos = butter2_highpass_sos(125.0, MIC_RATE)
    source = _full_scale_square(16_000, MIC_RATE, hz=50.0)

    out, state = sosfilt(sos, source, zi=np.zeros((sos.shape[0], 2)))

    assert np.all(np.isfinite(out))
    assert np.all(np.isfinite(state))
    assert float(np.max(np.abs(out))) <= 2.0 * 32767.0
