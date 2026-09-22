# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Binary ABI and read-only header decoding for the jts_ring transport."""

from __future__ import annotations

from dataclasses import dataclass

# Layout: rust/jasper-ring/src/layout.rs; pinned by tests/test_ring_header.py.
# Offsets are hand-duplicated because Python cannot link the Rust const;
# the Rust crate's golden layout test is the offset SSOT.
#
# ALL SIX declared geometry fields are read, not just the two the slot-count
# guard needs: the Rust/C attach compares every one of them field-by-field, so a
# Python guard reading only ``period_frames``/``n_slots`` would report a
# coherent ring where the ioplug attach will fail.
_RING_MAGIC = 0x4A52_494E  # "JRIN" little-endian (layout.rs MAGIC)
_RING_HEADER_BYTES = 128  # layout.rs HEADER_BYTES
# The one layout version this parser's offsets describe (layout.rs VERSION).
_RING_HEADER_VERSION = 1
_RING_OFF_MAGIC = 0  # u32
_RING_OFF_VERSION = 4  # u32
_RING_OFF_RATE = 8  # u32
_RING_OFF_CHANNELS = 12  # u32
_RING_OFF_SAMPLE_FORMAT = 16  # u32
_RING_OFF_PERIOD_FRAMES = 20  # u32
_RING_OFF_N_SLOTS = 24  # u32
# The two RUNTIME liveness fields, both little-endian u64 CLOCK_MONOTONIC
# nanoseconds (layout.rs OFF_WRITER_HEARTBEAT_NS / OFF_READER_HEARTBEAT_NS).
# Unlike the six geometry fields above these change every period, so they answer
# "is this ring moving", not "what shape is it".
_RING_OFF_WRITER_HEARTBEAT_NS = 64  # u64
_RING_OFF_READER_HEARTBEAT_NS = 80  # u64
# The remaining RUNTIME fields, all little-endian u64, inside the same 128 bytes.
# What each one buys an observer that the two heartbeats above do not:
#   - the two SEQUENCE cursors are the only cross-process trace of DROPS. When
#     the writer demotes an absent reader it advances ``read_seq`` on that
#     reader's behalf, one slot per dropped publish, so while nothing live is
#     stamping ``reader_heartbeat_ns`` a rising ``read_seq`` IS the drop cursor.
#     Their DIFFERENCE is the ring's occupancy.
#   - ``reader_pid`` separates "no reader has ever attached" (0 with a zero
#     heartbeat) from "a reader attached and stopped beating" (a pid with a
#     stale heartbeat). Both leave the reader not-live; only the second is a
#     fault. ``jasper.ring_assets.ring_flow_state`` uses the split.
#   - ``writer_epoch`` counts writer REATTACHES, so a flapping writer is legible
#     without differencing journal lines.
_RING_OFF_WRITER_EPOCH = 32  # u64
_RING_OFF_WRITE_SEQ = 40  # u64
_RING_OFF_READ_SEQ = 48  # u64
_RING_OFF_WRITER_PID = 56  # u64
_RING_OFF_READER_PID = 72  # u64

# The staleness window a heartbeat may fall behind before its stamper counts as
# gone. NOT a number chosen here: it is the C ioplug's own
# ``JTS_RING_WRITER_LIVENESS_TIMEOUT_NS`` (``jts_ring_shm.h``), the exact
# threshold ``reader_is_live`` applies to ``reader_heartbeat_ns`` when deciding
# whether to demote a reader and free-run. Spelling the same number makes an
# observer's verdict and the mechanism it reports agree by construction; an
# observer with its own threshold would eventually disagree with the writer
# about who is alive. Pinned against the C header by
# ``tests/test_ring_stall_alarm.py``.
RING_LIVENESS_TIMEOUT_NS = 2_000_000_000

# The ``sample_format`` header field's wire values (layout.rs
# SAMPLE_FORMAT_S16LE / SAMPLE_FORMAT_S32LE, mirrored by the C header's
# JTS_RING_SAMPLE_FORMAT_*). These ids are written into the shared header and
# compared field-by-field on attach, so they are a wire contract pinned against
# the generated ring ABI by ``tests/test_ring_header.py``.
RING_SAMPLE_FORMAT_S16LE = 1
RING_SAMPLE_FORMAT_S32LE = 2
# Header sample_format id -> the ALSA format token the conf.d and the emitters
# spell. ``jasper.fanin_coupling`` owns that token vocabulary; this map is how a
# header byte is named in a human-readable mismatch detail.
RING_SAMPLE_FORMAT_NAMES = {
    RING_SAMPLE_FORMAT_S16LE: "S16_LE",
    RING_SAMPLE_FORMAT_S32LE: "S32_LE",
}


@dataclass(frozen=True)
class RingHeader:
    """The geometry fields read from an on-disk ring SHM file header.

    ``valid`` is False when the file is absent, too small for a header, does not
    carry the ``JRIN`` magic (a torn / partially-initialized / foreign file), or
    declares a layout ``version`` this parser does not describe. A
    ``valid=False`` header is NOT trusted for a geometry comparison — the caller
    treats it as "no coherent ring present".

    The version gate is a PARSER property, not a policy one: these offsets are
    the v1 layout's, so a file announcing another version may put different
    meanings at them and its "geometry" would be fiction. ``version`` is still
    reported so a caller can name what it saw.
    """

    valid: bool
    magic: int = 0
    version: int = 0
    rate: int = 0
    channels: int = 0
    sample_format: int = 0
    period_frames: int = 0
    n_slots: int = 0
    # RUNTIME, not geometry: CLOCK_MONOTONIC ns, 0 when never stamped. The
    # writer stamps its own every publish/wait tick; the reader stamps its own
    # every DAC period, filled or not. The observer uses ``time.monotonic_ns()``
    # on the same box; see :func:`jasper.ring_assets.ring_stall_verdict`.
    # Freshness needs one sample, without a sampling window.
    writer_heartbeat_ns: int = 0
    reader_heartbeat_ns: int = 0
    # RUNTIME, not geometry (see the offset block above). ``write_seq`` /
    # ``read_seq`` are monotonic slot cursors; ``writer_pid`` / ``reader_pid``
    # are 0 when that end is not attached; ``writer_epoch`` increments on every
    # writer reattach. All 0 on a never-used ring.
    writer_epoch: int = 0
    write_seq: int = 0
    read_seq: int = 0
    writer_pid: int = 0
    reader_pid: int = 0

    @property
    def sample_format_name(self) -> str:
        """The header's ``sample_format`` as an ALSA token, or ``id=N`` for one
        outside the two the layout defines."""
        return RING_SAMPLE_FORMAT_NAMES.get(
            self.sample_format, f"id={self.sample_format}"
        )


def read_ring_header(path: str) -> RingHeader:
    """Read the geometry fields from a ring SHM file header (little-endian u32s).

    Pure filesystem read of the first :data:`_RING_HEADER_BYTES` bytes — no mmap,
    no ALSA, no writer disturbance (read-only open). Returns ``RingHeader(valid=
    False)`` for an absent/short/magic-less file, and for one whose ``version``
    is not :data:`_RING_HEADER_VERSION`. The magic gate matters: the Rust writer
    publishes ``JRIN`` LAST (a Release store), so a header without it is not yet
    a coherent ring and must not drive a delete/mismatch decision on its (zero)
    geometry fields.
    """
    import struct

    try:
        with open(path, "rb") as fh:
            head = fh.read(_RING_HEADER_BYTES)
    except OSError:
        return RingHeader(valid=False)
    if len(head) < _RING_HEADER_BYTES:
        return RingHeader(valid=False)
    magic = struct.unpack_from("<I", head, _RING_OFF_MAGIC)[0]
    if magic != _RING_MAGIC:
        return RingHeader(valid=False)
    version = struct.unpack_from("<I", head, _RING_OFF_VERSION)[0]
    if version != _RING_HEADER_VERSION:
        # Magic matched but the layout is not the one these offsets describe:
        # report the version, vouch for nothing else.
        return RingHeader(valid=False, magic=magic, version=version)
    return RingHeader(
        valid=True,
        magic=magic,
        version=version,
        rate=struct.unpack_from("<I", head, _RING_OFF_RATE)[0],
        channels=struct.unpack_from("<I", head, _RING_OFF_CHANNELS)[0],
        sample_format=struct.unpack_from("<I", head, _RING_OFF_SAMPLE_FORMAT)[0],
        period_frames=struct.unpack_from("<I", head, _RING_OFF_PERIOD_FRAMES)[0],
        n_slots=struct.unpack_from("<I", head, _RING_OFF_N_SLOTS)[0],
        writer_heartbeat_ns=struct.unpack_from(
            "<Q", head, _RING_OFF_WRITER_HEARTBEAT_NS
        )[0],
        reader_heartbeat_ns=struct.unpack_from(
            "<Q", head, _RING_OFF_READER_HEARTBEAT_NS
        )[0],
        writer_epoch=struct.unpack_from("<Q", head, _RING_OFF_WRITER_EPOCH)[0],
        write_seq=struct.unpack_from("<Q", head, _RING_OFF_WRITE_SEQ)[0],
        read_seq=struct.unpack_from("<Q", head, _RING_OFF_READ_SEQ)[0],
        writer_pid=struct.unpack_from("<Q", head, _RING_OFF_WRITER_PID)[0],
        reader_pid=struct.unpack_from("<Q", head, _RING_OFF_READER_PID)[0],
    )
