# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring binary header decoding and generated ABI agreement."""

from __future__ import annotations

from jasper import ring_header
from jasper.fanin_coupling import RING_WIRE_FORMATS
from tests.ring_abi import ring_abi


def _write_ring_header(
    path,
    *,
    magic=0x4A52_494E,
    version=1,
    rate=48000,
    channels=2,
    sample_format=ring_header.RING_SAMPLE_FORMAT_S16LE,
    period=128,
    n_slots=2,
):
    import struct

    hdr = bytearray(ring_header._RING_HEADER_BYTES)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_MAGIC, magic)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_VERSION, version)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_RATE, rate)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_CHANNELS, channels)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_SAMPLE_FORMAT, sample_format)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_PERIOD_FRAMES, period)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_N_SLOTS, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)


def test_read_ring_header_reads_valid_geometry(tmp_path):
    ring = tmp_path / "program.ring"
    _write_ring_header(ring, period=128, n_slots=2)
    header = ring_header.read_ring_header(str(ring))
    assert header.valid is True
    assert header.version == 1
    assert header.period_frames == 128
    assert header.n_slots == 2


def test_read_ring_header_reads_every_declared_geometry_field(tmp_path):
    """All six fields round-trip, at DISTINCT values.

    The stale-file guards compare a header against what the writer will build,
    and the attach compares every field. A reader that only saw slots/period
    would report a coherent ring for a file that shears on rate, channels, or
    format — which is the shape that plays wrong audio instead of failing loud.
    Distinct values are what makes a swapped-offset bug fail here: with the
    shipped 2-channel / S32 pair, `channels` and `sample_format` are 2 and 2 at
    adjacent offsets — literally indistinguishable — so a swapped wiring would
    read straight through. (Before the ring-wire default flip the pair was 2 and
    1, which was already too close to catch several wrong wirings; it is now
    exactly equal.)
    """
    ring = tmp_path / "wide.ring"
    _write_ring_header(
        ring,
        rate=44100,
        channels=6,
        sample_format=ring_header.RING_SAMPLE_FORMAT_S32LE,
        period=256,
        n_slots=4,
    )
    header = ring_header.read_ring_header(str(ring))
    assert header.valid is True
    assert header.version == 1
    assert header.rate == 44100
    assert header.channels == 6
    assert header.sample_format == ring_header.RING_SAMPLE_FORMAT_S32LE
    assert header.sample_format_name == "S32_LE"
    assert header.period_frames == 256
    assert header.n_slots == 4


def test_read_ring_header_invalid_when_absent_short_or_magicless(tmp_path):
    # Absent file.
    assert ring_header.read_ring_header(str(tmp_path / "gone.ring")).valid is False
    # Too short for a header.
    short = tmp_path / "short.ring"
    short.write_bytes(b"\x00" * 32)
    assert ring_header.read_ring_header(str(short)).valid is False
    # Full-size but WRONG magic (a torn / foreign file) — must not be trusted.
    bad = tmp_path / "bad.ring"
    _write_ring_header(bad, magic=0xDEADBEEF, n_slots=2)
    header = ring_header.read_ring_header(str(bad))
    assert header.valid is False
    # The (untrusted) geometry fields are NOT surfaced when invalid.
    assert header.n_slots == 0


def test_read_ring_header_refuses_a_version_these_offsets_do_not_describe(tmp_path):
    """A magic-matching header at another layout version vouches for nothing.

    These offsets are the v1 layout's. Nothing compared `version` before, so a
    file announcing a different layout had its bytes read AS IF v1 and its
    "geometry" believed. The version is still reported, so a caller can name
    what it saw rather than say "no ring".
    """
    ring = tmp_path / "future.ring"
    _write_ring_header(ring, version=7, period=999, n_slots=9)
    header = ring_header.read_ring_header(str(ring))
    assert header.valid is False
    assert header.version == 7
    # Nothing else is surfaced from a layout we cannot parse.
    assert header.period_frames == 0
    assert header.n_slots == 0
    assert header.channels == 0
    assert header.sample_format == 0


def test_unknown_sample_format_id_is_named_honestly(tmp_path):
    # A header carrying an id outside the layout's two must not be printed as
    # one of them; the detail says exactly what the byte was.
    ring = tmp_path / "odd.ring"
    _write_ring_header(ring, sample_format=9)
    assert ring_header.read_ring_header(str(ring)).sample_format_name == "id=9"


# --- The generated ring ABI --------------------------------------------------
#
# `rust/jasper-ring/layout.json` is rendered from `jasper_ring::layout` (its
# `layout_dump` example) and OWNS every number the SHM header carries. Python is
# a third speller of them, in a language that can link neither the Rust const nor
# the C `#define`, and a drift here is silent in the worst possible way: the
# parser keeps returning a coherent-looking header with fields read out of the
# wrong words, so a live ring reads as idle or an idle one as stalled.
#
# The C half of the same pin lives in `c/jts-ring-ioplug/test_ring_core.c`.

RING_ABI = ring_abi()


def test_python_ring_constants_match_the_generated_abi():
    assert ring_header._RING_MAGIC == RING_ABI["magic"]
    assert ring_header._RING_HEADER_BYTES == RING_ABI["header_bytes"]
    assert ring_header._RING_HEADER_VERSION == RING_ABI["version"]
    assert (
        ring_header.RING_LIVENESS_TIMEOUT_NS == RING_ABI["writer_liveness_timeout_ns"]
    )
    # Header FIELD VALUES compared field-by-field at attach: Python names them
    # for a mismatch detail, so a drift would print the wrong format for a real
    # shear.
    assert ring_header.RING_SAMPLE_FORMAT_S16LE == RING_ABI["sample_format_s16le"]
    assert ring_header.RING_SAMPLE_FORMAT_S32LE == RING_ABI["sample_format_s32le"]
    # The names are the ALSA tokens the conf.d and every emitter spell.
    assert set(ring_header.RING_SAMPLE_FORMAT_NAMES.values()) == set(RING_WIRE_FORMATS)


def test_python_header_offsets_match_the_generated_abi():
    for field, offset in {
        "magic": ring_header._RING_OFF_MAGIC,
        "version": ring_header._RING_OFF_VERSION,
        "rate": ring_header._RING_OFF_RATE,
        "channels": ring_header._RING_OFF_CHANNELS,
        "sample_format": ring_header._RING_OFF_SAMPLE_FORMAT,
        "period_frames": ring_header._RING_OFF_PERIOD_FRAMES,
        "n_slots": ring_header._RING_OFF_N_SLOTS,
        "writer_epoch": ring_header._RING_OFF_WRITER_EPOCH,
        "write_seq": ring_header._RING_OFF_WRITE_SEQ,
        "read_seq": ring_header._RING_OFF_READ_SEQ,
        "writer_pid": ring_header._RING_OFF_WRITER_PID,
        "writer_heartbeat_ns": ring_header._RING_OFF_WRITER_HEARTBEAT_NS,
        "reader_pid": ring_header._RING_OFF_READER_PID,
        "reader_heartbeat_ns": ring_header._RING_OFF_READER_HEARTBEAT_NS,
    }.items():
        assert offset == RING_ABI[f"off_{field}"], field
