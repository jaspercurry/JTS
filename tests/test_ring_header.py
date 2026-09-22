# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring header decoding, ABI agreement, and liveness/flow interpretation."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from jasper import ring_header
from jasper.fanin_coupling import RING_WIRE_FORMATS
from jasper.ring_header import (
    RING_FLOW_ABSENT,
    RING_FLOW_FLOWING,
    RING_FLOW_IDLE,
    RING_FLOW_PRIMING,
    RING_FLOW_READER_STALLED,
    RING_FLOW_UNREADABLE,
    RING_LIVENESS_TIMEOUT_NS,
    ring_flow_state,
    ring_stall_verdict,
)
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


NOW = 100_000_000_000  # an arbitrary CLOCK_MONOTONIC "now", 100 s
FRESH = NOW - 5_000_000  # 5 ms behind — comfortably live
STALE = NOW - 3_000_000_000  # 3 s behind — past the 2 s window


def _ring_file(
    tmp_path: Path,
    *,
    writer_hb: int,
    reader_hb: int,
    read_seq: int = 0,
    write_seq: int = 0,
    magic: int = 0x4A52_494E,
    version: int = 1,
) -> str:
    """A synthetic 128-byte v1 ring header with the runtime fields set."""
    head = bytearray(ring_header._RING_HEADER_BYTES)
    struct.pack_into("<I", head, ring_header._RING_OFF_MAGIC, magic)
    struct.pack_into("<I", head, ring_header._RING_OFF_VERSION, version)
    struct.pack_into("<I", head, ring_header._RING_OFF_RATE, 48000)
    struct.pack_into("<I", head, ring_header._RING_OFF_CHANNELS, 4)
    struct.pack_into("<I", head, ring_header._RING_OFF_SAMPLE_FORMAT, 2)
    struct.pack_into("<I", head, ring_header._RING_OFF_PERIOD_FRAMES, 128)
    struct.pack_into("<I", head, ring_header._RING_OFF_N_SLOTS, 2)
    struct.pack_into("<Q", head, ring_header._RING_OFF_WRITE_SEQ, write_seq)
    struct.pack_into("<Q", head, ring_header._RING_OFF_READ_SEQ, read_seq)
    struct.pack_into("<Q", head, ring_header._RING_OFF_WRITER_HEARTBEAT_NS, writer_hb)
    struct.pack_into("<Q", head, ring_header._RING_OFF_READER_HEARTBEAT_NS, reader_hb)
    path = tmp_path / "active-content.ring"
    path.write_bytes(bytes(head))
    return str(path)


# --- the threshold is not ours to choose ------------------------------------


def test_the_liveness_window_is_the_rings_own_number():
    """SINGLE SOURCE OF TRUTH, across a language boundary.

    The observer must apply the SAME staleness window the C ioplug's
    ``reader_is_live`` applies when it decides to demote a reader and free-run.
    An observer with its own threshold would eventually disagree with the writer
    about who is alive — reporting a stall the writer is not acting on, or
    staying silent through one it is. Both ends take it from the generated ring
    ABI, which the C test pins its own ``#define`` against.
    """
    assert ring_header.RING_LIVENESS_TIMEOUT_NS == ring_abi()["writer_liveness_timeout_ns"]


# --- the conjunction ---------------------------------------------------------


def test_writer_live_and_reader_stale_is_the_alarm(tmp_path):
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=FRESH, reader_hb=STALE), now_ns=NOW
    )
    assert verdict.present is True
    assert verdict.stalled is True
    assert "free-running" in verdict.detail
    assert "Audio is being lost" in verdict.detail


def test_writer_live_and_reader_never_stamped_is_the_alarm(tmp_path):
    """A reader that attached and died before its first period — or never
    attached at all while the writer publishes."""
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=FRESH, reader_hb=0), now_ns=NOW
    )
    assert verdict.stalled is True
    assert "never stamped" in verdict.detail


def test_both_ends_live_is_silent(tmp_path):
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=FRESH, reader_hb=FRESH), now_ns=NOW
    )
    assert verdict.present is True
    assert verdict.stalled is False
    assert "both ends live" in verdict.detail


# --- the negatives that keep it off the whole fleet -------------------------


def test_an_idle_ring_is_not_a_stall(tmp_path):
    """THE LOAD-BEARING NEGATIVE. A ring nobody is writing is idle, not stalled.

    Without this the alarm would fire on every box carrying a leftover ring file
    with no daemon attached — which is most of the fleet most of the time.
    """
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=STALE, reader_hb=STALE), now_ns=NOW
    )
    assert verdict.present is False
    assert verdict.stalled is False
    assert "not a stall" in verdict.detail


def test_a_never_written_ring_is_not_a_stall(tmp_path):
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=0, reader_hb=0), now_ns=NOW
    )
    assert verdict.present is False
    assert "never been written" in verdict.detail


def test_an_absent_or_torn_ring_is_not_judged(tmp_path):
    assert ring_stall_verdict(str(tmp_path / "nope.ring")).present is False
    torn = _ring_file(tmp_path, writer_hb=FRESH, reader_hb=STALE, magic=0xDEAD_BEEF)
    assert ring_stall_verdict(torn, now_ns=NOW).present is False
    wrong_version = _ring_file(tmp_path, writer_hb=FRESH, reader_hb=STALE, version=99)
    assert ring_stall_verdict(wrong_version, now_ns=NOW).present is False


def test_a_heartbeat_from_the_future_clamps_instead_of_alarming(tmp_path):
    """Saturating subtraction, mirroring the C ``reader_is_live`` comment.

    The reader stamps concurrently, so a heartbeat taken AFTER this observer
    sampled ``now_ns`` would underflow an unsigned subtraction into an enormous
    age and spuriously alarm on a perfectly live ring. Python's ints do not
    wrap, but the same clamp is needed for the same reason.
    """
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=NOW + 1_000_000, reader_hb=NOW + 1_000_000),
        now_ns=NOW,
    )
    assert verdict.stalled is False
    assert verdict.reader_age_ns == 0
    assert verdict.writer_age_ns == 0


# --- D1: the discrimination argument, as a test ------------------------------


@pytest.mark.parametrize("write_seq", [0, 4096, 2**40])
@pytest.mark.parametrize("read_seq", [0, 1, 4096, 2**40])
def test_the_alarm_survives_the_read_seq_inversion(tmp_path, read_seq, write_seq):
    """THE D1 RULING. ``read_seq`` must not enter the verdict at ANY value.

    During the exact fault this alarm exists to catch, the writer advances
    ``read_seq`` on the absent reader's behalf so occupancy stays honest
    (``jts_ring_shm.c``'s ``atomic_store_explicit(&h->read_seq, rseq + 1, …)``
    under ``if (!reader_is_live(…))``; the Rust writer's
    ``free_run_drop_oldest`` does the same, and attach resync sets
    ``read_seq = write_seq`` outright). So a ``read_seq``-flat clause holds only
    inside the pre-demotion grace and goes FALSE exactly when the drops start —
    an alarm that switches itself off at the onset of its own fault.

    Sweeping BOTH sequence cursors across flat, advancing, and
    resynced-to-each-other while holding the heartbeats fixed proves the verdict
    is independent of either.
    """
    path = _ring_file(
        tmp_path,
        writer_hb=FRESH,
        reader_hb=STALE,
        read_seq=read_seq,
        write_seq=write_seq,
    )
    verdict = ring_stall_verdict(path, now_ns=NOW)
    assert verdict.stalled is True, (
        f"read_seq={read_seq} write_seq={write_seq} changed the verdict — the "
        "alarm has re-imported the self-silencing inversion D1 removed"
    )


#: The grouping ring's own wire, so the derived startup budget below is the one a
#: real box computes: 2 s x 48000 / 128 = 750 slots.
_RATE = 48_000
_PERIOD_FRAMES = 128
_PRIMING_BUDGET = (RING_LIVENESS_TIMEOUT_NS * _RATE) // (_PERIOD_FRAMES * 1_000_000_000)

#: A plausible CLOCK_MONOTONIC sample (about 1000 s of uptime), big enough that
#: a 47-second-stale heartbeat is still a positive stamp.
_NOW_NS = 1_000_000_000_000
_FRESH = _NOW_NS - 5_000_000  # 5 ms behind: well inside the liveness window
_STALE = _NOW_NS - 47_000_000_000  # 47 s behind: long past it


def _header(
    *,
    magic: int = 0x4A52_494E,
    version: int = 1,
    n_slots: int = 16,
    writer_epoch: int = 1,
    write_seq: int = 0,
    read_seq: int = 0,
    writer_pid: int = 0,
    reader_pid: int = 0,
    writer_hb: int = 0,
    reader_hb: int = 0,
) -> bytes:
    """A 128-byte v1 ring header, built through the module's own offsets.

    Using ``ring_header``' offsets rather than literals keeps this helper honest
    about one thing only — the VALUES — and leaves the offsets themselves to the
    cross-language pin in ``tests/test_ring_header.py``.
    """
    head = bytearray(128)
    struct.pack_into("<I", head, ring_header._RING_OFF_MAGIC, magic)
    struct.pack_into("<I", head, ring_header._RING_OFF_VERSION, version)
    struct.pack_into("<I", head, ring_header._RING_OFF_RATE, _RATE)
    struct.pack_into("<I", head, ring_header._RING_OFF_CHANNELS, 2)
    struct.pack_into("<I", head, ring_header._RING_OFF_SAMPLE_FORMAT, 1)
    struct.pack_into("<I", head, ring_header._RING_OFF_PERIOD_FRAMES, _PERIOD_FRAMES)
    struct.pack_into("<I", head, ring_header._RING_OFF_N_SLOTS, n_slots)
    struct.pack_into("<Q", head, ring_header._RING_OFF_WRITER_EPOCH, writer_epoch)
    struct.pack_into("<Q", head, ring_header._RING_OFF_WRITE_SEQ, write_seq)
    struct.pack_into("<Q", head, ring_header._RING_OFF_READ_SEQ, read_seq)
    struct.pack_into("<Q", head, ring_header._RING_OFF_WRITER_PID, writer_pid)
    struct.pack_into("<Q", head, ring_header._RING_OFF_READER_PID, reader_pid)
    struct.pack_into("<Q", head, ring_header._RING_OFF_WRITER_HEARTBEAT_NS, writer_hb)
    struct.pack_into("<Q", head, ring_header._RING_OFF_READER_HEARTBEAT_NS, reader_hb)
    return bytes(head)


def _ring(tmp_path: Path, **kwargs: object) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "grouping.ring"
    path.write_bytes(_header(**kwargs))  # type: ignore[arg-type]
    return str(path)


def _state(path: str) -> ring_header.RingFlowState:
    return ring_flow_state(path, now_ns=_NOW_NS)


# --- C-1: the classifier ---------------------------------------------------


def test_the_three_operator_states_are_distinguishable(tmp_path):
    """THE BAR (#2786): healthy, startup transient, and reader-stalled are three
    different answers — from ``/state`` or doctor output alone, with no journal
    line differenced and no second sample taken.

    All three headers below carry a LIVE writer, which post-governor is where
    their similarity stops being incidental: the raw magnitudes no longer
    separate them. What does is (a) whether a reader is beating, and when it is
    not, (b) whether the ring is young enough for that to still be the cold
    start.
    """
    healthy = _state(
        _ring(
            tmp_path / "a",
            writer_pid=101,
            reader_pid=202,
            writer_hb=_FRESH,
            reader_hb=_FRESH,
            write_seq=900_000,
            read_seq=899_998,
        )
    )
    startup = _state(
        _ring(
            tmp_path / "b",
            writer_pid=101,
            writer_hb=_FRESH,
            write_seq=120,
            read_seq=104,
        )
    )
    stalled = _state(
        _ring(
            tmp_path / "c",
            writer_pid=101,
            reader_pid=202,
            writer_hb=_FRESH,
            reader_hb=_STALE,
            write_seq=900_000,
            read_seq=899_984,
        )
    )

    assert healthy.state == RING_FLOW_FLOWING
    assert startup.state == RING_FLOW_PRIMING
    assert stalled.state == RING_FLOW_READER_STALLED
    assert len({healthy.state, startup.state, stalled.state}) == 3

    # …and the stalled one carries its DURATION without a second sample, which
    # is the question an operator actually asks next.
    assert stalled.reader_age_ns == 47_000_000_000
    # …plus the cursor pair the drop count is derived from. `read_seq` is being
    # advanced by the WRITER here (nothing live is draining), so differencing it
    # across two polls bounds the drops between them.
    assert (stalled.write_seq, stalled.read_seq) == (900_000, 899_984)
    assert stalled.occupancy_slots == 16


def test_an_absent_ring_file_is_absent_not_unreadable(tmp_path):
    """The fleet's normal state, and it must not read as a permission problem:
    a ring file exists only once something opens the PCM."""
    flow = _state(str(tmp_path / "never-created.ring"))
    assert flow.state == RING_FLOW_ABSENT
    assert flow.write_seq is None


@pytest.mark.parametrize(
    "kwargs, why",
    [({"magic": 0xDEADBEEF}, "foreign magic"), ({"version": 7}, "unknown version")],
)
def test_an_incoherent_header_is_unreadable(tmp_path, kwargs, why):
    flow = _state(_ring(tmp_path, **kwargs))
    assert flow.state == RING_FLOW_UNREADABLE, why
    assert "coherent" in flow.detail


def test_a_short_file_is_unreadable(tmp_path):
    path = tmp_path / "grouping.ring"
    path.write_bytes(_header()[:64])
    assert _state(str(path)).state == RING_FLOW_UNREADABLE


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the DAC read check")
def test_a_ring_this_process_cannot_read_says_so(tmp_path):
    """The vantage question, and why it is not folded into ``absent``.

    A reader outside group ``jts-ring`` gets the same "no header" as a box with
    no ring at all, and reporting the two the same way would let a permission
    regression read as an idle speaker. What the detail must therefore carry is
    the REQUIREMENT — group-readability by ``jts-ring`` — and deliberately not a
    mode; the comment on the assertions below says why naming one would be
    wrong, and the last assertion pins that it is not named.
    """
    path = _ring(tmp_path, writer_pid=1, writer_hb=_FRESH)
    os.chmod(path, 0o000)
    try:
        flow = _state(path)
    finally:
        os.chmod(path, 0o600)
    assert flow.state == RING_FLOW_UNREADABLE
    assert "not readable by this process" in flow.detail
    # The REQUIREMENT, not a mode. A ring's mode follows its creating unit's
    # umask — 0660 under UMask=0007, 0640 under systemd's default — so naming a
    # number here would pin a claim that is false on half the fleet. Both grant
    # the group read, which is the fact the remediation needs to convey.
    assert "jts-ring" in flow.detail
    assert "group-readable" in flow.detail
    assert "0660" not in flow.detail


@pytest.mark.parametrize(
    "writer_hb, why",
    [(0, "never written"), (_STALE, "writer heartbeat itself stale")],
)
def test_a_ring_nobody_is_writing_is_idle(tmp_path, writer_hb, why):
    """Idle, never stalled. The alarm is specifically "audio flows IN and not
    OUT"; with no live writer there is no audio to lose, and a solo box's ring
    (if one exists at all) must never look broken."""
    flow = _state(_ring(tmp_path, writer_hb=writer_hb, reader_hb=_STALE))
    assert flow.state == RING_FLOW_IDLE, why


def test_the_startup_window_is_derived_from_the_ioplugs_own_liveness_window(tmp_path):
    """The priming grace is not a number chosen here.

    It is one liveness window's worth of slots at the ring's own rate and period
    — the same window ``reader_is_live`` applies before it demotes a reader — so
    a ring with a different geometry gets a budget that tracks it instead of a
    constant somebody has to remember to update.
    """
    assert _PRIMING_BUDGET == 750

    at_the_edge = _state(
        _ring(tmp_path / "in", writer_pid=1, writer_hb=_FRESH, write_seq=_PRIMING_BUDGET)
    )
    just_past = _state(
        _ring(
            tmp_path / "out",
            writer_pid=1,
            writer_hb=_FRESH,
            write_seq=_PRIMING_BUDGET + 1,
        )
    )
    assert at_the_edge.state == RING_FLOW_PRIMING
    assert just_past.state == RING_FLOW_READER_STALLED
    assert "no reader has ever attached" in just_past.detail


def test_a_cleanly_closed_reader_is_a_stall_the_instant_it_closes(tmp_path):
    """THE PID CLAUSE, and the bug it exists to prevent.

    ``jts_ring_reader_close`` clears ``reader_pid`` and leaves the last heartbeat
    standing, so for one whole liveness window a closed reader looks fresh. The C
    is not fooled — ``reader_is_live`` requires pid AND heartbeat AND window, so
    the writer demotes that reader and starts dropping *immediately*. An observer
    that judged on the heartbeat alone would report a healthy ring for two
    seconds while audio was being lost.

    The heartbeat here is FRESH on purpose: with a stale one this passes even
    without the pid clause, which is exactly how the gap survived the first
    round. The startup grace must not apply either — this ring had a reader.
    """
    flow = _state(
        _ring(
            tmp_path,
            writer_pid=1,
            reader_pid=0,
            writer_hb=_FRESH,
            reader_hb=_FRESH,
            write_seq=5,
        )
    )
    assert flow.state == RING_FLOW_READER_STALLED
    assert "closed the ring" in flow.detail


def test_a_cleanly_closed_writer_is_idle_not_flowing(tmp_path):
    """The symmetric half. ``writer_is_live`` has the same pid clause, so a
    writer that closed is gone even while its heartbeat is fresh — and a ring
    with no live writer is idle, whatever the reader is doing."""
    flow = _state(
        _ring(
            tmp_path,
            writer_pid=0,
            reader_pid=2,
            writer_hb=_FRESH,
            reader_hb=_FRESH,
            write_seq=900,
        )
    )
    assert flow.state == RING_FLOW_IDLE
    assert "closed the ring" in flow.detail


def test_a_wedged_reader_is_named_differently_from_a_closed_one(tmp_path):
    """Same state, different cause: a pid still stamped with a stale heartbeat is
    a reader that stopped running its loop, not one that left. The state word is
    what an operator acts on; the detail is what they read next."""
    flow = _state(
        _ring(
            tmp_path,
            writer_pid=1,
            reader_pid=202,
            writer_hb=_FRESH,
            reader_hb=_STALE,
            write_seq=900_000,
        )
    )
    assert flow.state == RING_FLOW_READER_STALLED
    assert "202" in flow.detail
    assert "stopped stamping" in flow.detail


def test_the_two_judges_diverge_only_on_a_cleanly_closed_end(tmp_path):
    """The divergence between this classifier and the four-ring stall alarm,
    pinned where it is rather than left to be discovered.

    ``ring_stall_verdict`` is heartbeat-only and was deliberately not re-scoped
    (it judges four rings; #2786 owns one). So on a cleanly-closed reader it
    still reads "both ends live" for one window while ``ring_flow_state``
    already says the writer is dropping. Naming the exact input where they
    disagree means a future change to either one has to come here and decide,
    instead of the two drifting apart quietly.
    """
    closed = _ring(
        tmp_path / "closed",
        writer_pid=1,
        reader_pid=0,
        writer_hb=_FRESH,
        reader_hb=_FRESH,
    )
    assert ring_header.ring_stall_verdict(closed, now_ns=_NOW_NS).stalled is False
    assert _state(closed).state == RING_FLOW_READER_STALLED

    # …and they agree everywhere else, so the divergence really is that one case.
    wedged = _ring(
        tmp_path / "wedged",
        writer_pid=1,
        reader_pid=2,
        writer_hb=_FRESH,
        reader_hb=_STALE,
    )
    live = _ring(
        tmp_path / "live", writer_pid=1, reader_pid=2, writer_hb=_FRESH, reader_hb=_FRESH
    )
    assert ring_header.ring_stall_verdict(wedged, now_ns=_NOW_NS).stalled is True
    assert _state(wedged).state == RING_FLOW_READER_STALLED
    assert ring_header.ring_stall_verdict(live, now_ns=_NOW_NS).stalled is False
    assert _state(live).state == RING_FLOW_FLOWING


def test_an_over_range_occupancy_is_not_published_as_fact(tmp_path):
    """A writer that lapped a wedged reader (or a torn read) can present a
    ``write_seq - read_seq`` far past ``n_slots``. The C resolves that by
    resyncing to the tip, so the raw difference is not an occupancy anyone would
    act on — publishing it would put "900000 slots" in a 16-slot ring on a
    dashboard. The raw cursors stay published, so nothing is hidden."""
    flow = _state(
        _ring(
            tmp_path,
            writer_pid=1,
            reader_pid=2,
            writer_hb=_FRESH,
            reader_hb=_FRESH,
            write_seq=900_000,
            read_seq=0,
            n_slots=16,
        )
    )
    assert flow.occupancy_slots is None
    assert (flow.write_seq, flow.read_seq) == (900_000, 0)


def test_a_torn_sequence_pair_does_not_invent_a_negative_occupancy(tmp_path):
    """A single unlocked read of a live header can in principle catch the two
    cursors mid-update. An impossible pair reports ``None`` rather than a
    nonsense occupancy that a dashboard would render as fact."""
    flow = _state(
        _ring(
            tmp_path,
            writer_pid=1,
            reader_pid=2,
            writer_hb=_FRESH,
            reader_hb=_FRESH,
            write_seq=10,
            read_seq=99,
        )
    )
    assert flow.occupancy_slots is None
    assert flow.state == RING_FLOW_FLOWING


def test_the_narrow_stall_verdict_keeps_its_original_behaviour(tmp_path):
    """:func:`ring_stall_verdict` is the doctor's standing alarm over FOUR rings.
    #2786 owns one of them, so this issue changed none of its verdicts — a
    refactor that shifted it would move alarms on three rings nobody reviewed.
    """
    stalled = _ring(
        tmp_path / "s", writer_pid=1, reader_pid=2, writer_hb=_FRESH, reader_hb=_STALE
    )
    live = _ring(
        tmp_path / "l", writer_pid=1, reader_pid=2, writer_hb=_FRESH, reader_hb=_FRESH
    )
    idle = _ring(tmp_path / "i", writer_hb=_STALE)

    assert ring_header.ring_stall_verdict(stalled, now_ns=_NOW_NS).stalled is True
    assert ring_header.ring_stall_verdict(live, now_ns=_NOW_NS).stalled is False
    assert ring_header.ring_stall_verdict(idle, now_ns=_NOW_NS).present is False
    # The narrow verdict still alarms where the rich one grants a startup grace:
    # its consumers judge four rings and were not re-scoped by this issue.
    young = _ring(tmp_path / "y", writer_pid=1, writer_hb=_FRESH, write_seq=3)
    assert ring_header.ring_stall_verdict(young, now_ns=_NOW_NS).stalled is True
    assert _state(young).state == RING_FLOW_PRIMING


def test_reading_a_ring_header_never_opens_it_for_writing(tmp_path, monkeypatch):
    """jasper-control is in the ``jts-ring`` group so it can read this header,
    and that group grants WRITE on every ring on the box. Every open the read
    path performs, directly or through the classifier, is mode ``"rb"``.
    """
    modes: list[str] = []
    real_open = open

    def _spy(path, mode="r", *args, **kwargs):
        modes.append(mode)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(ring_header, "open", _spy, raising=False)
    ring = _ring(tmp_path, writer_pid=1, reader_pid=2, writer_hb=_FRESH)

    ring_header.read_ring_header(ring)
    _state(ring)

    assert modes and set(modes) == {"rb"}
