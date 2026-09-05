# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The assistant IPC wire at both widths (U2 PR-2, #2223).

Two bars, and they are not the same kind of claim:

* **NARROW IS FROZEN.** Every byte a narrow box emits must be what it emitted
  before this existed. That is pinned against a golden captured by running
  ``origin/main``'s ``jasper/`` tree over the same probe, not by re-deriving
  the code under test.
* **WIDE CARRIES MORE.** A signal below the S16 grid must reach the payload,
  and the assertion is a CONTRAST — what the narrow wire does with the same
  signal — because "the bits survived" means nothing on its own.

The width itself comes from ONE input, ``JASPER_FANIN_RING_WIRE_FORMAT``,
classified identically in both languages
(``tests/test_ring_wire_format_contract.py`` pins that half).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import socket
import struct
from pathlib import Path

import numpy as np
import pytest

from jasper import audio_io
from jasper.audio_io import (
    _OUTPUTD_AUDIO_FRAME_BYTES,
    _OUTPUTD_AUDIO_FRAME_BYTES_WIDE,
    _OUTPUTD_MAX_AUDIO_CHUNK_BYTES,
    _SPINE_SCALE,
    TtsPlayout,
    _outputd_audio_chunks,
    _OutputdStreamAdapter,
    _quantize_to_wire,
    tts_wire_is_wide,
)

from .fanin_env_fixtures import declare_fanin_env

_REPO = Path(__file__).resolve().parents[1]
_RESAMPLER_RS = _REPO / "rust" / "jasper-resampler" / "src" / "lib.rs"

# The emitted payload for `_probe_pcm()` on the NARROW wire, captured by
# running `git archive origin/main jasper/` through this same harness at
# 1caff2304 (2026-08-12). Not self-generated: the pre-change tree produced it.
_NARROW_GOLDEN_SHA256 = (
    "7499bf127f9d69eb1f82bf9449f148524a51a9c768dc5e8309b0b64d81654023"
)
_NARROW_GOLDEN_BYTES = 19_200
_NARROW_GOLDEN_HEAD = "00000000180a180a69176917b720b72054245424ba24ba246c236c23ca1fca1f"


def _probe_pcm(n: int = 2_400) -> bytes:
    """24 kHz mono S16 with fine structure the 2x resampler can act on.

    The third term is deliberately tiny (~13 LSB peak): a component whose
    resampled values land between S16 codes is what the wide wire keeps and
    the narrow one rounds away.
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


class _Recorder:
    """Stand-in for `_OutputdStreamAdapter` that keeps the emitted bytes."""

    def __init__(self) -> None:
        self.payload = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.payload += data

    def set_gain_db(self, db: float) -> None:
        pass

    def start_segment(self, **kwargs) -> None:
        pass

    def end_segment(self) -> None:
        pass


def _emit(pcm: bytes, *, wide: bool, **write_kwargs) -> bytes:
    tts = TtsPlayout(socket_path="/nonexistent.sock", wire_wide=wide)
    rec = _Recorder()
    tts._stream = rec

    async def _ready():
        return rec

    tts._current_outputd_stream = _ready
    asyncio.run(tts.write_segment(pcm, segment_kind="cue", **write_kwargs))
    return bytes(rec.payload)


# ---------------------------------------------------------------------------
# Narrow: frozen.
# ---------------------------------------------------------------------------


def test_the_narrow_wire_is_byte_identical_to_its_committed_golden():
    payload = _emit(_probe_pcm(), wide=False)
    assert len(payload) == _NARROW_GOLDEN_BYTES
    assert payload[:32].hex() == _NARROW_GOLDEN_HEAD
    assert hashlib.sha256(payload).hexdigest() == _NARROW_GOLDEN_SHA256


def test_the_narrow_quantizer_is_the_pre_change_expression():
    """Truncate-toward-zero, saturating, exactly as shipped.

    Spelled out here rather than only exercised end to end, because the
    difference between this and round-to-nearest is one LSB on roughly half
    the samples in the fleet.
    """
    arr = np.array([0.4, -0.4, 1.6, -1.6, 40_000.0, -40_000.0], dtype=np.float32)
    out = _quantize_to_wire(arr, wide=False)
    assert out.dtype == np.int16
    assert out.tolist() == [0, 0, 1, -1, 32_767, -32_768]


@pytest.mark.parametrize(
    ("wire_wide", "expected"),
    [(False, b"AUDIO 8\n"), (True, b"AUDIO32 8\n")],
)
def test_the_adapter_writes_the_header_its_wire_declares(wire_wide, expected):
    """The header line on the real socket, not a field read back.

    The reader dispatches on this verb, so it is the wire's declaration of its
    own sample width — the reason no format negotiation is needed.
    """
    ours, theirs = socket.socketpair()
    try:
        adapter = _OutputdStreamAdapter(ours, wire_wide=wire_wide)
        payload = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        adapter.write(payload)
        theirs.settimeout(2.0)
        got = theirs.recv(64)
    finally:
        ours.close()
        theirs.close()
    assert got == expected + payload


# ---------------------------------------------------------------------------
# Wide: carries more, and the contrast says how much more.
# ---------------------------------------------------------------------------


def test_the_wide_wire_keeps_bits_the_narrow_wire_has_no_code_for():
    pcm = _probe_pcm()
    narrow = np.frombuffer(_emit(pcm, wide=False), dtype="<i2")
    wide = np.frombuffer(_emit(pcm, wide=True), dtype="<i4")

    assert len(wide) == len(narrow), "same frames, twice the bytes"
    # Every wide sample describes the same signal at 2^16 times the scale, so
    # dividing back out lands within one narrow step of the narrow sample.
    delta = wide.astype(np.int64) - narrow.astype(np.int64) * _SPINE_SCALE
    assert np.all(np.abs(delta) <= _SPINE_SCALE), (
        "the two wires must describe the same signal at two scales"
    )
    # THE CONTRAST: a sub-S16-LSB remainder that the narrow wire simply has no
    # code for. Without this, "the wide wire is wider" would be a claim about
    # the container, not the signal.
    remainder = wide.astype(np.int64) % _SPINE_SCALE
    carried = int(np.count_nonzero(remainder))
    assert carried > len(wide) // 2, (
        f"only {carried}/{len(wide)} wide samples carry sub-LSB detail; "
        "the probe is not exercising the precision this wire exists for"
    )


def test_the_wide_quantizer_rounds_to_nearest_and_saturates():
    """A NEW edge, so it gets the house rule: round-to-nearest, no dither.

    The probe is asserted to DISTINGUISH rounding from truncation before it is
    used. An earlier revision of this test used 0.4, whose product with 2^16 is
    26214.4 — a value both rules map to 26214 — so it passed a truncating
    mutant. That is the half-guarded-site class, caught by mutation.
    """
    # 0.400008 -> 26214.92: the two rules disagree. 1e-05 -> 0.655: rounding
    # keeps the sample, truncation deletes it entirely.
    arr = np.array([0.400008, -0.400008, 1e-05, -1e-05, 40_000.0, -40_000.0],
                   dtype=np.float32)
    exact = arr.astype(np.float64) * _SPINE_SCALE
    assert any(
        np.trunc(x) != np.rint(x) for x in exact[:4]
    ), "the probe must reach a value the two rules disagree on"

    out = _quantize_to_wire(arr, wide=True)
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
    """An earcon baked wide and the same earcon baked narrow agree.

    The wide input is divided by the exact 2^16 on the way in and multiplied by
    it again on the way out, both exact, so a PROMOTED narrow buffer emits the
    identical wide payload the unpromoted one does.
    """
    narrow_pcm = _probe_pcm(240)
    promoted = (
        np.frombuffer(narrow_pcm, dtype="<i2").astype(np.int32) * _SPINE_SCALE
    ).astype("<i4").tobytes()
    from_narrow = _emit(narrow_pcm, wide=True)
    from_wide = _emit(promoted, wide=True, pcm_wide=True)
    assert from_wide == from_narrow


# ---------------------------------------------------------------------------
# Framing, chunking, and the byte cap.
# ---------------------------------------------------------------------------


def test_the_chunker_aligns_to_frames_at_both_widths_under_one_byte_cap():
    for frame_bytes in (_OUTPUTD_AUDIO_FRAME_BYTES, _OUTPUTD_AUDIO_FRAME_BYTES_WIDE):
        data = b"\0" * (frame_bytes * 200_000)
        chunks = list(_outputd_audio_chunks(data, frame_bytes))
        assert b"".join(chunks) == data
        for chunk in chunks:
            assert len(chunk) % frame_bytes == 0, "a torn frame would desync"
            # The cap is a BYTE cap on both wires — it bounds the allocation a
            # single command asks the daemon for, and must not double.
            assert len(chunk) <= _OUTPUTD_MAX_AUDIO_CHUNK_BYTES
        with pytest.raises(ValueError):
            list(_outputd_audio_chunks(b"\0" * (frame_bytes + 1), frame_bytes))


def test_the_wide_wire_frame_is_twice_the_narrow_one():
    assert _OUTPUTD_AUDIO_FRAME_BYTES == 4
    assert _OUTPUTD_AUDIO_FRAME_BYTES_WIDE == 8
    assert _OUTPUTD_AUDIO_FRAME_BYTES_WIDE == 2 * _OUTPUTD_AUDIO_FRAME_BYTES


# ---------------------------------------------------------------------------
# The width resolution: one input, one process-wide answer.
# ---------------------------------------------------------------------------


def _clear_cache():
    """`tests/conftest.py` clears this around every test; these calls are the
    WITHIN-test clears, because several of these tests deliberately resolve the
    width more than once with different declarations."""
    tts_wire_is_wide.cache_clear()


def _declare(monkeypatch, tmp_path, *, wire_format: str, coupling: str | None) -> None:
    """Declare BOTH halves of the box, at the read points the code actually uses.

    The FORMAT half is stubbed at its resolver. The TRANSPORT half is a REAL
    ``fanin.env`` the predicate opens, because that half's reader is a FILE door
    (``persisted_coupling_feeds_ring``) and a function stub is only as good as
    the caller still calling it: this helper used to stub
    ``coupling_reconcile.read_persisted_coupling``, which `assistant_wire_is_wide`
    stopped calling, and the ``loopback`` rows below silently began asserting
    against the runner's absent ``/var/lib/jasper/fanin.env`` instead — the
    vacuous-stub class #3644 fixed.

    ``coupling=None`` leaves the key out entirely: the UNDECLARED box.
    """
    import jasper.fanin_coupling as fc

    declare_fanin_env(
        monkeypatch,
        tmp_path,
        "" if coupling is None else f"JASPER_FANIN_CAMILLA_COUPLING={coupling}\n",
    )
    monkeypatch.setattr(fc, "read_declared_ring_wire_format", lambda: wire_format)
    _clear_cache()


# The verdict table, mirroring `the_assistant_width_needs_both_halves_of_the_box_declaration`
# in rust/jasper-tts-protocol/src/lib.rs. The (S32_LE, loopback) row is the one
# that matters: fan-in publishes through the ring and nothing else (ADR-0100),
# so a box that declared a wide format but never armed the ring is NARROW.
# The UNDECLARED rows are the other side of the same rule: `jasper-fanin` serves
# an absent key AS the ring (`coupling_is_shm_ring: true`), so naming nothing is
# WIDE at a wide format and the transport half only ever subtracts (#3655).
_VERDICTS = [
    ("S32_LE", "shm_ring", True),
    ("S32_LE", None, True),
    ("S32_LE", "", True),
    ("S32_LE", "loopback", False),
    ("S16_LE", "shm_ring", False),
    ("S16_LE", None, False),
    ("S16_LE", "loopback", False),
]


@pytest.mark.parametrize(("wire_format", "coupling", "expected"), _VERDICTS)
def test_the_width_needs_both_halves_of_the_box_declaration(
    monkeypatch, tmp_path, wire_format, coupling, expected
):
    _declare(monkeypatch, tmp_path, wire_format=wire_format, coupling=coupling)
    assert tts_wire_is_wide() is expected


def test_a_declared_wide_but_unarmed_box_speaks_the_narrow_verb(
    monkeypatch, tmp_path
):
    """The row an earlier revision of this PR got wrong, end to end.

    Keying on the format token alone made `jasper-voice` send AUDIO32 into a
    fan-in that mixes narrow — converted losslessly, but a standing
    disagreement. UNARMED is the RETIRED token here, not an absent key: fan-in
    refuses `loopback` at parse and parks, so that box's voice must speak the
    conservative verb.
    """
    _declare(monkeypatch, tmp_path, wire_format="S32_LE", coupling="loopback")
    tts = TtsPlayout(socket_path="/nonexistent.sock")
    assert tts._wire_wide is False
    assert tts._frame_bytes == _OUTPUTD_AUDIO_FRAME_BYTES


def test_a_declared_wide_undeclared_coupling_box_speaks_the_wide_verb(
    monkeypatch, tmp_path
):
    """The pair of the row above, and the one #3655 adds.

    A box the reconciler has not written names no coupling, and `jasper-fanin`
    runs it on the ring regardless (`Config::program_wire_is_wide` passes the
    transport half as a hard-coded `true`). Voice must speak AUDIO32 to it or
    every assistant payload takes a needless conversion at the mixer.
    """
    _declare(monkeypatch, tmp_path, wire_format="S32_LE", coupling=None)
    tts = TtsPlayout(socket_path="/nonexistent.sock")
    assert tts._wire_wide is True
    assert tts._frame_bytes == _OUTPUTD_AUDIO_FRAME_BYTES_WIDE


def test_an_unreadable_declaration_resolves_narrow_and_says_so(monkeypatch, caplog):
    """`jasper-fanin` owns the refusal; `jasper-voice` must not die twice.

    A bad token parks fan-in at exit 78. Re-raising here would also take down
    the daemon that plays the failure cues, so the fault is logged loudly and
    the conservative width is used.
    """
    import jasper.fanin_coupling as fc

    def _boom():
        raise ValueError("JASPER_FANIN_RING_WIRE_FORMAT='S24_3LE' unsupported")

    _clear_cache()
    monkeypatch.setattr(fc, "read_declared_ring_wire_format", _boom)
    with caplog.at_level("WARNING"):
        assert tts_wire_is_wide() is False
    assert "tts_wire.declaration_unreadable" in caplog.text
    _clear_cache()


def test_the_process_resolves_the_width_exactly_once(monkeypatch, tmp_path):
    """Two callers ask — the playout and the earcon bake. One answer, always."""
    import jasper.fanin_coupling as fc

    calls = []

    def _counted():
        calls.append(1)
        return "S32_LE"

    _declare(monkeypatch, tmp_path, wire_format="S32_LE", coupling="shm_ring")
    monkeypatch.setattr(fc, "read_declared_ring_wire_format", _counted)
    assert tts_wire_is_wide() is True
    assert tts_wire_is_wide() is True
    assert tts_wire_is_wide() is True
    assert len(calls) == 1, "a second file read could return a second answer"
    _clear_cache()


def test_the_playout_resolves_its_width_when_none_is_given(monkeypatch, tmp_path):
    _declare(monkeypatch, tmp_path, wire_format="S32_LE", coupling="shm_ring")
    tts = TtsPlayout(socket_path="/nonexistent.sock")
    assert tts._wire_wide is True
    assert tts._frame_bytes == _OUTPUTD_AUDIO_FRAME_BYTES_WIDE
    _declare(monkeypatch, tmp_path, wire_format="S16_LE", coupling="shm_ring")
    tts = TtsPlayout(socket_path="/nonexistent.sock")
    assert tts._wire_wide is False
    assert tts._frame_bytes == _OUTPUTD_AUDIO_FRAME_BYTES


def test_a_startup_line_names_the_resolved_width_and_where_it_came_from(
    monkeypatch, tmp_path, caplog
):
    """Item 4: a support read must not need journal archaeology.

    The mismatch warn fires at most once for the daemon's lifetime and may have
    scrolled away; this line is always there, on both sides of the socket.
    """
    _declare(monkeypatch, tmp_path, wire_format="S32_LE", coupling="shm_ring")
    with caplog.at_level("INFO"):
        TtsPlayout(socket_path="/nonexistent.sock")
    assert "event=tts_wire.resolved" in caplog.text
    assert "width=S32_LE" in caplog.text
    assert "verb=AUDIO32" in caplog.text
    assert "source=box_declaration" in caplog.text
    caplog.clear()
    with caplog.at_level("INFO"):
        TtsPlayout(socket_path="/nonexistent.sock", wire_wide=False)
    assert "width=S16_LE" in caplog.text
    assert "source=explicit" in caplog.text


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


def test_audio_io_module_is_the_one_the_worktree_owns():
    """Guard against a shared venv resolving `jasper` to another checkout."""
    assert Path(audio_io.__file__).resolve().parent.parent == _REPO
