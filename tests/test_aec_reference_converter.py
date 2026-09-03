# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`_ReferenceFrameConverter` still emits the samples scipy used to emit.

The bridge's far-end reference is what AEC3 subtracts from the mic. A gain,
delay or band change here does not fail loudly — it quietly stops cancelling
echo, and the box answers itself. This pins the whole conversion (stereo fold,
resample, high-pass, gain, clip, int16 framing) against the `scipy.signal`
pipeline it replaced, and pins delivery-boundary continuity without scipy.

The last file in this module is the one remaining scipy seam: which resampler
the optional corpus USB mic gets, and therefore whether the resident daemon
imports scipy at all.
"""
from __future__ import annotations

import sys

import numpy as np
import pytest

from jasper.cli.aec_bridge import (
    FRAME_SAMPLES,
    REF_CHANNELS,
    REF_RATE,
    SAMPLE_RATE,
    _ReferenceFrameConverter,
    _usb_resampler,
)

REF_BLOCK = FRAME_SAMPLES * (REF_RATE // SAMPLE_RATE)
HPF_HZ = 125.0


def _interleaved(kind: str, blocks: int = 6) -> np.ndarray:
    n = REF_BLOCK * blocks
    t = np.arange(n) / REF_RATE
    if kind == "square":
        mono = 32767.0 * np.sign(np.sin(2.0 * np.pi * 220.0 * t))
    else:
        rng = np.random.default_rng(9)
        mono = (
            9000.0 * np.sin(2.0 * np.pi * 110.0 * t)
            + 4000.0 * np.sin(2.0 * np.pi * 1750.0 * t + 0.4)
            + 800.0 * rng.standard_normal(n)
        )
    out = np.empty(n * REF_CHANNELS, dtype=np.int16)
    out[0::REF_CHANNELS] = np.clip(mono, -32768, 32767).astype(np.int16)
    out[1::REF_CHANNELS] = np.clip(mono * 0.8, -32768, 32767).astype(np.int16)
    return out


def _frames(batch) -> np.ndarray:
    return np.concatenate(
        [np.frombuffer(f, dtype=np.int16) for f in batch.frames]
    ).astype(np.int64)


def _scipy_pipeline(interleaved: np.ndarray, gain_db: float) -> np.ndarray:
    """The conversion exactly as it read before jasper.dsp_numpy replaced it."""
    from scipy.signal import butter, resample_poly, sosfilt

    left = interleaved[0::REF_CHANNELS].astype(np.float32)
    right = interleaved[1::REF_CHANNELS].astype(np.float32)
    accum = (left + right) * 0.5
    sos = butter(2, HPF_HZ, btype="highpass", fs=SAMPLE_RATE, output="sos")
    zi = np.zeros((sos.shape[0], 2))
    gain = 10.0 ** (gain_db / 20.0)
    frames = []
    while accum.size >= REF_BLOCK:
        chunk, accum = accum[:REF_BLOCK], accum[REF_BLOCK:]
        mono16 = resample_poly(chunk, up=1, down=3)
        mono16, zi = sosfilt(sos, mono16, zi=zi)
        if gain != 1.0:
            mono16 = mono16 * gain
        frames.append(np.clip(mono16, -32768, 32767).astype(np.int16))
    return np.concatenate(frames).astype(np.int64)


@pytest.mark.parametrize("gain_db", [0.0, 12.0])
@pytest.mark.parametrize("kind", ["programme", "square"])
def test_reference_frames_match_the_scipy_pipeline(gain_db, kind):
    pytest.importorskip("scipy.signal")
    interleaved = _interleaved(kind)

    got = _frames(
        _ReferenceFrameConverter(ref_gain_db=gain_db, ref_hpf_hz=HPF_HZ)
        .feed(interleaved)
    )
    want = _scipy_pipeline(interleaved, gain_db)

    assert got.size == want.size
    # scipy resampled in float32 and this resamples in float64, so a handful
    # of samples land the other side of an int16 truncation boundary. One LSB
    # at -90 dBFS, ~80 dB under the mic's own noise floor.
    assert int(np.max(np.abs(got - want))) <= 1
    rms_error = float(np.sqrt(np.mean(np.square(got - want))))
    assert rms_error <= 1e-4 * float(np.sqrt(np.mean(np.square(want))))


@pytest.mark.parametrize("kind", ["programme", "square"])
def test_reference_frames_are_continuous_across_delivery_boundaries(kind):
    """Ragged UDP datagrams must fold into the same stream as one big one.

    Accumulator and high-pass state both have to survive a delivery that ends
    mid-block; a reset would stamp a filter transient into the reference every
    packet, which AEC3 cannot subtract out of the mic.
    """
    interleaved = _interleaved(kind)

    whole = _frames(
        _ReferenceFrameConverter(ref_gain_db=0.0, ref_hpf_hz=HPF_HZ)
        .feed(interleaved)
    )
    split = _ReferenceFrameConverter(ref_gain_db=0.0, ref_hpf_hz=HPF_HZ)
    pieces = []
    step = 2 * REF_CHANNELS * 137
    for start in range(0, interleaved.size, step):
        batch = split.feed(interleaved[start:start + step])
        if batch.frames:
            pieces.append(_frames(batch))

    assert np.array_equal(np.concatenate(pieces), whole)


def test_reference_frames_report_clipping_without_wrapping():
    """A hot reference clamps at full scale; int16 wrap would invert it."""
    interleaved = _interleaved("square", blocks=3)

    batch = (
        _ReferenceFrameConverter(ref_gain_db=6.0, ref_hpf_hz=HPF_HZ)
        .feed(interleaved)
    )

    samples = _frames(batch)
    assert batch.total_samples == samples.size
    assert 0 < batch.clipped_samples <= samples.size
    assert int(samples.min()) >= -32768 and int(samples.max()) <= 32767


def test_a_card_already_at_the_mic_rate_is_not_resampled():
    assert _usb_resampler(SAMPLE_RATE) == (None, 1, 1)


@pytest.mark.parametrize(
    ("usb_rate", "ratio", "wants_scipy"),
    [
        (8_000, (2, 1), False),
        (24_000, (2, 3), False),
        (32_000, (1, 2), False),
        (48_000, (1, 3), False),
        (96_000, (1, 6), False),
        (44_100, (160, 441), True),
        (22_050, (320, 441), True),
    ],
)
def test_only_the_44_1_khz_family_makes_the_bridge_import_scipy(
    usb_rate, ratio, wants_scipy,
):
    """Every integer-ratio card must stay on the numpy kernel.

scipy here is resident RSS in a `MemorySwapMax=0` slice
    (`jasper.dsp_numpy` owns the figure), paid for the life of the daemon, and
    the numpy kernel is measurably faster at these ratios. Only 44.1 kHz
    reduces to hundreds of polyphase branches, which numpy would run in a
    Python loop inside the capture callback.
    """
    resample, up, down = _usb_resampler(usb_rate)

    assert (up, down) == ratio
    assert resample.__module__.startswith("scipy") is wants_scipy


def test_a_44_1_khz_card_falls_back_to_numpy_when_scipy_is_absent(monkeypatch):
    """A missing scipy must slow the corpus leg down, not kill its thread."""
    monkeypatch.setitem(sys.modules, "scipy.signal", None)

    resample, up, down = _usb_resampler(44_100)

    assert (up, down) == (160, 441)
    assert resample.__module__ == "jasper.dsp_numpy"
