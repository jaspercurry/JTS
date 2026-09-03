# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`ReferenceFrameConverter` still emits the samples scipy used to emit.

The bridge's far-end reference is what AEC3 subtracts from the mic. A gain,
delay or band change here does not fail loudly — it quietly stops cancelling
echo, and the box answers itself. This pins the whole conversion (stereo fold,
resample, high-pass, gain, clip, int16 framing) against the `scipy.signal`
pipeline it replaced, and pins delivery-boundary continuity without scipy.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.cli.aec_bridge_engines import FRAME_SAMPLES, SAMPLE_RATE
from jasper.cli.aec_bridge_reference import (
    REF_CHANNELS,
    REF_RATE,
    ReferenceFrameConverter,
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
        ReferenceFrameConverter(ref_gain_db=gain_db, ref_hpf_hz=HPF_HZ)
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

    whole_batch = ReferenceFrameConverter(
        ref_gain_db=0.0, ref_hpf_hz=HPF_HZ,
    ).feed(interleaved)
    whole = _frames(whole_batch)
    split = ReferenceFrameConverter(ref_gain_db=0.0, ref_hpf_hz=HPF_HZ)
    pieces = []
    step = 2 * REF_CHANNELS * 137
    for start in range(0, interleaved.size, step):
        batch = split.feed(interleaved[start:start + step])
        if batch.frames:
            pieces.append(_frames(batch))

    assert np.array_equal(np.concatenate(pieces), whole)
    # AEC3 rejects a reference block that is not the mic's own length.
    assert all(len(f) == FRAME_SAMPLES * 2 for f in whole_batch.frames)


def test_reference_frames_report_clipping_without_wrapping():
    """A hot reference clamps at full scale; int16 wrap would invert it."""
    interleaved = _interleaved("square", blocks=3)

    batch = (
        ReferenceFrameConverter(ref_gain_db=6.0, ref_hpf_hz=HPF_HZ)
        .feed(interleaved)
    )

    samples = _frames(batch)
    assert batch.total_samples == samples.size
    assert 0 < batch.clipped_samples <= samples.size
    assert int(samples.min()) >= -32768 and int(samples.max()) <= 32767

