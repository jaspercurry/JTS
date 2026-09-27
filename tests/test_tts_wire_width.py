# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The assistant wire preserves resampled precision in bounded S32 frames."""

from __future__ import annotations

import asyncio
import math
import re
import socket
import struct
from pathlib import Path

import numpy as np
import pytest

from jasper import tts_playout
from jasper.assistant_loudness import UPSAMPLE_2X_CONTEXT, upsample_2x
from jasper.tts_playout import (
    _OUTPUTD_AUDIO_FRAME_BYTES,
    _OUTPUTD_MAX_AUDIO_CHUNK_BYTES,
    _SPINE_SCALE,
    TtsPlayout,
    _outputd_audio_chunks,
    _OutputdStreamAdapter,
    _quantize_to_wire,
)

from tests._playout import FakeOutputdStream

_REPO = Path(__file__).resolve().parents[1]
_RESAMPLER_RS = _REPO / "rust" / "jasper-resampler" / "src" / "lib.rs"


def _probe_pcm(n: int = 2_400) -> bytes:
    """24 kHz mono S16 with fine structure the 2x resampler can act on.

    The third term is deliberately tiny (~13 LSB peak): a component whose
    resampled values land between S16 codes and must survive quantization.
    """
    out = bytearray()
    for i in range(n):
        v = (
            0.62 * math.sin(2 * math.pi * 440.0 * i / 24_000.0)
            + 0.17 * math.sin(2 * math.pi * 3_271.0 * i / 24_000.0)
            + 0.0004 * math.sin(2 * math.pi * 91.0 * i / 24_000.0)
        )
        out += struct.pack("<h", max(-32_768, min(32_767, int(v * 30_000))))
    return bytes(out)


def _emit(pcm: bytes, **write_kwargs) -> bytes:
    tts = TtsPlayout(socket_path="/nonexistent.sock")
    rec = FakeOutputdStream()
    tts._stream = rec

    async def _ready():
        return rec

    tts._current_outputd_stream = _ready
    asyncio.run(tts.write_segment(pcm, segment_kind="cue", **write_kwargs))
    return b"".join(rec.writes)


def test_the_adapter_writes_the_audio32_header():
    ours, theirs = socket.socketpair()
    try:
        adapter = _OutputdStreamAdapter(ours)
        payload = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        adapter.write(payload)
        theirs.settimeout(2.0)
        got = theirs.recv(64)
    finally:
        ours.close()
        theirs.close()
    assert got == b"AUDIO32 8\n" + payload


# ---------------------------------------------------------------------------
# Wide: carries more, and the contrast says how much more.
# ---------------------------------------------------------------------------


def test_the_wide_wire_keeps_bits_the_narrow_wire_has_no_code_for():
    pcm = _probe_pcm()
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    context = 2 * UPSAMPLE_2X_CONTEXT
    resampled = upsample_2x(np.pad(samples, (context, 0)))[context:context + 2 * samples.size]
    narrow = np.repeat(np.clip(resampled, -32768, 32767).astype(np.int16), 2)
    wide = np.frombuffer(_emit(pcm), dtype="<i4")

    assert len(wide) == len(narrow), "same frames, twice the bytes"
    # Every wide sample describes the same signal at 2^16 times the scale, so
    # dividing back out lands within one narrow step of the narrow sample.
    delta = wide.astype(np.int64) - narrow.astype(np.int64) * _SPINE_SCALE
    assert np.all(np.abs(delta) <= _SPINE_SCALE), (
        "the two quantizations must describe the same signal at two scales"
    )
    # A sub-S16-LSB remainder proves signal detail survives, beyond frame width.
    remainder = wide.astype(np.int64) % _SPINE_SCALE
    carried = int(np.count_nonzero(remainder))
    assert carried > len(wide) // 2, (
        f"only {carried}/{len(wide)} wide samples carry sub-LSB detail; "
        "the probe is not exercising the precision this wire exists for"
    )


def test_the_wide_quantizer_rounds_to_nearest_and_saturates():
    # 0.400008 -> 26214.92: the two rules disagree. 1e-05 -> 0.655: rounding
    # keeps the sample, truncation deletes it entirely.
    arr = np.array([0.400008, -0.400008, 1e-05, -1e-05, 40_000.0, -40_000.0],
                   dtype=np.float32)
    exact = arr.astype(np.float64) * _SPINE_SCALE
    assert any(
        np.trunc(x) != np.rint(x) for x in exact[:4]
    ), "the probe must reach a value the two rules disagree on"

    out = _quantize_to_wire(arr)
    assert out.dtype == np.int32
    assert out.tolist() == [
        int(np.rint(exact[0])),
        int(np.rint(exact[1])),
        int(np.rint(exact[2])),
        int(np.rint(exact[3])),
        2 ** 31 - 1,
        -(2 ** 31),
    ]
    # Spelled absolutely too, so an equality between two derived expressions
    # cannot go vacuous: 0.400008 at spine scale rounds UP past the truncation.
    assert out.tolist()[0] == 26_215
    assert out.tolist()[2] == 1, "truncation would have deleted this sample"


def test_a_wide_input_buffer_is_normalized_before_it_is_re_quantized():
    """Promoting an S16 input by 2^16 must preserve its output payload."""
    narrow_pcm = _probe_pcm(240)
    promoted = (
        np.frombuffer(narrow_pcm, dtype="<i2").astype(np.int32) * _SPINE_SCALE
    ).astype("<i4").tobytes()
    from_narrow = _emit(narrow_pcm)
    from_wide = _emit(promoted, pcm_wide=True)
    assert from_wide == from_narrow


# ---------------------------------------------------------------------------
# Framing, chunking, and the byte cap.
# ---------------------------------------------------------------------------


def test_the_chunker_keeps_whole_frames_under_the_byte_cap():
    data = b"\0" * (_OUTPUTD_AUDIO_FRAME_BYTES * 200_000)
    chunks = list(_outputd_audio_chunks(data))
    assert b"".join(chunks) == data
    for chunk in chunks:
        assert len(chunk) % _OUTPUTD_AUDIO_FRAME_BYTES == 0
        assert len(chunk) <= _OUTPUTD_MAX_AUDIO_CHUNK_BYTES
    with pytest.raises(ValueError):
        list(_outputd_audio_chunks(b"\0" * (_OUTPUTD_AUDIO_FRAME_BYTES + 1)))


def test_the_wire_frame_and_chunk_limit():
    assert _OUTPUTD_AUDIO_FRAME_BYTES == 8
    assert _OUTPUTD_MAX_AUDIO_CHUNK_BYTES == 48_000
    duration = _OUTPUTD_MAX_AUDIO_CHUNK_BYTES / (
        _OUTPUTD_AUDIO_FRAME_BYTES * TtsPlayout.OUTPUT_RATE
    )
    assert duration == 0.125


# ---------------------------------------------------------------------------
# The cross-language scale contract.
# ---------------------------------------------------------------------------


def test_the_spine_scale_is_the_shift_the_rust_primitive_applies():
    """`_SPINE_SCALE` is a CONTRACT with `widen_i16_to_i32`, not a constant.

    Read out of the Rust source rather than restated, so the two cannot drift:
    if that shift ever stops being 16, this fails instead of silently emitting
    a payload 96 dB off.
    """
    if not _RESAMPLER_RS.exists():
        pytest.skip(f"rust source not present: {_RESAMPLER_RS}")
    source = _RESAMPLER_RS.read_text(encoding="utf-8")
    match = re.search(
        r"pub fn widen_i16_to_i32\(sample: i16\) -> i32 \{\s*"
        r"i32::from\(sample\) << (\d+)\s*\}",
        source,
    )
    assert match, "could not locate widen_i16_to_i32 in the resampler crate"
    assert _SPINE_SCALE == 2 ** int(match.group(1))


def test_tts_module_is_the_one_the_worktree_owns():
    """Guard against a shared venv resolving `jasper` to another checkout."""
    assert Path(tts_playout.__file__).resolve().parent.parent == _REPO
