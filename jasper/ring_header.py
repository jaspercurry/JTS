# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Binary ABI, read-only header decoding, and liveness/flow interpretation for the jts_ring transport."""

from __future__ import annotations

import os
import time
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
#     fault. ``jasper.ring_header.ring_flow_state`` uses the split.
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
# ``tests/test_ring_header.py``.
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
    # on the same box; see :func:`jasper.ring_header.ring_stall_verdict`.
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


@dataclass(frozen=True)
class RingStallVerdict:
    """Is this ring being WRITTEN but not READ? The independent-observer alarm.

    A ring-local frozen dataclass rather than the ``severity``/``code`` dict
    shape ``jasper.output_topology`` uses: those warnings ride
    ``evaluate_output_topology``'s ``warnings`` list, which
    ``OutputTopology.to_dict`` embeds and three persisted fingerprints hash
    (issue #2500). A stall is RUNTIME state that changes second to second, so
    putting it there would make a topology's fingerprint vary with whether a
    daemon happened to be wedged when it was read.

    ``present`` is False when there is no coherent ring file to judge, or when
    the ring exists but nothing has stamped a writer heartbeat yet (an armed but
    idle ring). ``stalled`` is only meaningful when ``present``.

    WHAT IT DETECTS. The C ioplug demotes a reader whose heartbeat has gone
    stale and then FREE-RUNS, dropping the oldest slot per publish so the ring
    stays bounded. Audio is being lost and nobody reports it: the ioplug's
    ``published_slots`` / ``drop_no_reader`` / ``full_waits`` are process-local
    ``jts_ring_writer_t`` fields printed at close, not shared-header fields, and
    outputd — the reader — is exactly the process that is wedged, so its own
    STATUS is the least trustworthy witness. Hence a THIRD observer blocked in
    neither end.

    WHY THE READER'S HEARTBEAT AND NOT ``read_seq``. At demotion the writer
    advances ``read_seq`` on the absent reader's behalf, deliberately, so
    ``occupancy = write_seq - read_seq`` stays honest and ALSA's ``avail`` does
    not stick at 0 (``jts_ring_shm.c``, the ``atomic_store_explicit(&h->read_seq,
    rseq + 1, …)`` guarded by ``if (!reader_is_live(…))``; the Rust writer's
    ``free_run_drop_oldest`` does the same). A "``read_seq`` is flat" clause
    therefore holds only inside the pre-demotion grace and goes false exactly
    when the drops begin — an alarm that switches itself off at the onset of the
    fault it exists to catch. ``reader_heartbeat_ns`` has no such inversion:
    only a reader running its loop stamps it, it stays stale through and after
    demotion, and it is the same fact ``reader_is_live`` uses to demote. Attach
    resync (``read_seq = write_seq``) is a third way the sequence numbers lie;
    the heartbeat is unaffected by that too.

    RESIDUAL: there is no live DROP COUNT for this ring. This verdict says "the
    reader is gone while the writer runs", the condition under which drops
    occur, not a count of them.
    """

    present: bool
    stalled: bool = False
    writer_age_ns: int | None = None
    reader_age_ns: int | None = None
    detail: str = ""


def _heartbeat_age_ns(stamp: int, now_ns: int) -> int | None:
    """Saturating age of one heartbeat stamp, or None when never stamped.

    Saturating because a heartbeat stamped AFTER the observer sampled ``now_ns``
    would make the subtraction underflow and read as enormously stale — an alarm
    on a live ring. Shared by both judges below, so the arithmetic is one rule
    even where the predicates around it differ.
    """
    if stamp == 0:
        return None
    return now_ns - stamp if now_ns > stamp else 0


def _end_is_live(pid: int, heartbeat_ns: int, now_ns: int, timeout_ns: int) -> bool:
    """Is one end of a ring live, by the C ioplug's OWN predicate?

    ``reader_is_live`` / ``writer_is_live`` (``c/jts-ring-ioplug/jts_ring_shm.c``)
    both require **all three**: a non-zero pid, a non-zero heartbeat, and an age
    inside the liveness window. The pid clause must not be dropped:
    ``jts_ring_reader_close`` clears ``reader_pid`` but leaves the last
    heartbeat standing, so a cleanly-closed end stays heartbeat-fresh for a full
    window after the writer has already begun free-running and dropping.
    Judging on the heartbeat alone reports a ring as healthy for those two
    seconds while audio is being lost.
    """
    if pid == 0 or heartbeat_ns == 0:
        return False
    age = _heartbeat_age_ns(heartbeat_ns, now_ns)
    return age is not None and age < timeout_ns


def ring_stall_verdict(
    path: str,
    *,
    now_ns: int | None = None,
    timeout_ns: int = RING_LIVENESS_TIMEOUT_NS,
) -> RingStallVerdict:
    """Judge one ring file: writer heartbeat FRESH while reader heartbeat STALE.

    Single-sample, no sleep. Both heartbeats and ``time.monotonic_ns()`` are
    CLOCK_MONOTONIC on the same box (``jts_ring_monotonic_ns`` in C,
    ``monotonic_ns`` in ``jasper-ring``, ``clock_gettime(CLOCK_MONOTONIC)`` in
    CPython), so "advancing over a window" and "fresh right now" are the same
    predicate — and freshness needs one read where advancement would need two
    plus a window the caller could get wrong. Ages are saturating
    (:func:`_heartbeat_age_ns`): a future heartbeat clamps to age 0.

    ``present=False`` (never an alarm) for: an absent / torn / foreign / wrong
    version file; and a ring whose WRITER heartbeat is 0 or itself stale. That
    last one is load-bearing: a ring nobody is writing is idle, not stalled, and
    alarming on it would fire on every unarmed box. The alarm is specifically
    "audio is flowing IN and not OUT".
    """
    header = read_ring_header(path)
    if not header.valid:
        return RingStallVerdict(present=False, detail="no coherent ring header")
    if now_ns is None:
        now_ns = time.monotonic_ns()

    def _age(stamp: int) -> int | None:
        return _heartbeat_age_ns(stamp, now_ns)

    # NOTE (#2786): this judge is HEARTBEAT-ONLY on purpose. The C's own
    # `reader_is_live` also requires a non-zero pid, so a cleanly-closed reader
    # is dead to the writer instantly while its last heartbeat stays fresh for
    # one liveness window — during which this verdict still reads "both ends
    # live". :func:`ring_flow_state` applies the full pid-and-heartbeat
    # predicate; this alarm judges four rings and the divergence costs it at
    # most one window of late alarming on a ring whose reader left cleanly. If
    # that ever matters, change it here for all four rings at once rather than
    # letting the two drift; where they disagree is pinned by
    # `test_the_two_judges_diverge_only_on_a_cleanly_closed_end`.
    writer_age = _age(header.writer_heartbeat_ns)
    reader_age = _age(header.reader_heartbeat_ns)
    if writer_age is None:
        return RingStallVerdict(
            present=False, detail="ring has never been written (no writer heartbeat)"
        )
    if writer_age >= timeout_ns:
        return RingStallVerdict(
            present=False,
            writer_age_ns=writer_age,
            reader_age_ns=reader_age,
            detail=(
                f"writer heartbeat is itself stale ({writer_age / 1e6:.0f} ms) — "
                "an idle or stopped ring, not a stall"
            ),
        )
    # The writer is live. Now the reader half.
    if reader_age is not None and reader_age < timeout_ns:
        return RingStallVerdict(
            present=True,
            stalled=False,
            writer_age_ns=writer_age,
            reader_age_ns=reader_age,
            detail=(
                f"both ends live (writer {writer_age / 1e6:.0f} ms, reader "
                f"{reader_age / 1e6:.0f} ms behind)"
            ),
        )
    reader_desc = (
        "never stamped a heartbeat"
        if reader_age is None
        else f"{reader_age / 1e6:.0f} ms behind"
    )
    return RingStallVerdict(
        present=True,
        stalled=True,
        writer_age_ns=writer_age,
        reader_age_ns=reader_age,
        detail=(
            f"the writer is live ({writer_age / 1e6:.0f} ms behind) but the "
            f"reader {reader_desc}, past the {timeout_ns / 1e6:.0f} ms liveness "
            "window the ioplug itself uses — it has demoted the reader and is "
            "free-running, dropping the oldest slot per publish. Audio is being "
            "lost. Check the reader daemon (jasper-outputd for the content "
            "rings) and its journal"
        ),
    )


# The states :func:`ring_flow_state` classifies a ring into. They are published
# verbatim as the ``state`` of ``/state``'s grouping ``ring`` block
# (``jasper.multiroom.state``), so the vocabulary is consumed off the box.
# Named constants rather than bare literals: a misspelled literal compares
# false silently.
RING_FLOW_ABSENT = "absent"
RING_FLOW_UNREADABLE = "unreadable"
RING_FLOW_IDLE = "idle"
RING_FLOW_PRIMING = "priming"
RING_FLOW_READER_STALLED = "reader_stalled"
RING_FLOW_FLOWING = "flowing"


@dataclass(frozen=True)
class RingFlowState:
    """What is happening on one ring right now, in one word plus its evidence.

    :func:`ring_stall_verdict` answers one narrow question — is this ring being
    written but not read — for the doctor's four-ring alarm. This is the
    OPERATOR-FACING classification over the same 128 bytes, and it differs from
    that alarm in two deliberate ways.

    **It judges liveness by the writer's own rule.** Both ends are tested with
    :func:`_end_is_live`, i.e. pid AND heartbeat AND window — the same
    conjunction ``reader_is_live`` / ``writer_is_live`` apply in
    ``c/jts-ring-ioplug/jts_ring_shm.c``. The stall alarm tests the heartbeat
    only, which reports a cleanly-closed end as live for one window after the
    writer has already started dropping; an operator surface cannot afford that
    gap.

    **The startup split.** A ring whose writer is live and whose reader has never
    stamped a heartbeat is the SAME instantaneous shape at second one of a cold
    start and at hour three of a wedged reader, because the pacing governor
    (``jts_ring_pace_apply`` in ``c/jts-ring-ioplug/jts_ring_shm.h``) holds the
    stalled case to roughly nominal rather than letting it storm. So the
    classifier separates them by ``write_seq``, the ring's own age: below one
    liveness window's worth of slots the ring is still
    :data:`RING_FLOW_PRIMING`, above it the reader is genuinely late and the
    state is :data:`RING_FLOW_READER_STALLED`. The budget is DERIVED from the
    header's own ``rate``/``period_frames`` and the ioplug's own demotion window,
    so no threshold is invented here. This splits only the NEVER-ATTACHED case:
    a ring that had a reader and lost it is never priming, however young.

    **The drop cursor.** There is no drop COUNT in the shared header (the
    writer's ``drop_no_reader`` is a process-local ``jts_ring_writer_t`` field —
    see :class:`RingStallVerdict`), and this module does not invent one: a
    counter accumulated across polls would depend on who polled and how often.
    What it publishes instead is the pair the count is derived FROM. While no
    reader is live the writer advances ``read_seq`` itself, one slot per dropped
    publish, so two reads of ``read_seq`` while ``state`` is
    :data:`RING_FLOW_READER_STALLED` bound the drops between them exactly, and
    ``reader_age_ns`` says how long that has been true without differencing
    anything.

    ``writer_age_ns`` / ``reader_age_ns`` are None when that end has never
    stamped a heartbeat. The sequence/epoch fields are None whenever the header
    could not be read at all.
    """

    state: str
    detail: str = ""
    writer_age_ns: int | None = None
    reader_age_ns: int | None = None
    write_seq: int | None = None
    read_seq: int | None = None
    occupancy_slots: int | None = None
    writer_epoch: int | None = None


def _priming_slot_budget(header: RingHeader, timeout_ns: int) -> int:
    """Slots a nominal writer publishes in one liveness window.

    The startup grace, in the ring's OWN units: ``rate``/``period_frames`` come
    off the header and ``timeout_ns`` is the ioplug's own demotion window, so a
    ring with a different period or a different window gets a budget that tracks
    it. :func:`read_ring_header` gates on magic and version but NOT on the
    geometry's range, so a zero in either field is reachable (a torn read, or a
    foreign file carrying the magic). That answers 0 — no grace, because a grace
    nobody can size must not be granted; the cost of the strict direction is one
    startup transient reported as a stall.
    """
    if header.rate <= 0 or header.period_frames <= 0:
        return 0
    return (timeout_ns * header.rate) // (header.period_frames * 1_000_000_000)


def ring_flow_state(
    path: str,
    *,
    now_ns: int | None = None,
    timeout_ns: int = RING_LIVENESS_TIMEOUT_NS,
) -> RingFlowState:
    """Classify one ring file into a single operator-facing state.

    ONE read of the first 128 header bytes, read-only, no mmap, no ALSA, no lock
    — so it can be called against a ring carrying live audio without perturbing
    it. Bounded and non-blocking: the ring lives on tmpfs and the read is a fixed
    128 bytes.

    Total: every failure resolves to a state, never an exception.
    :data:`RING_FLOW_ABSENT` when no file is there (a ring file exists only once
    something opens the PCM), and :data:`RING_FLOW_UNREADABLE` when a file IS
    there but this process cannot read it or it carries no coherent v1 ``JRIN``
    header. Those two are kept apart deliberately: collapsing "nothing has opened
    this device" into "I am not allowed to look" would let a permission problem
    read as an idle speaker.
    """
    header = read_ring_header(path)
    if not header.valid:
        if not os.path.exists(path):
            return RingFlowState(
                state=RING_FLOW_ABSENT,
                detail="no ring file — nothing has opened this PCM",
            )
        if not os.access(path, os.R_OK):
            # The requirement, not a mode: ring files are group `jts-ring` by the
            # setgid directory (deploy/tmpfiles/jts-ring.conf), but their MODE is
            # the creating unit's umask — 0660 under UMask=0007, 0640 under
            # systemd's default. Both grant the group read, which is all an
            # observer needs, so naming one mode here would be false on half the
            # boxes.
            return RingFlowState(
                state=RING_FLOW_UNREADABLE,
                detail=(
                    f"{path} exists but is not readable by this process — ring "
                    "files are group-readable by `jts-ring`; this process is not "
                    "in that group"
                ),
            )
        return RingFlowState(
            state=RING_FLOW_UNREADABLE,
            detail="ring file carries no coherent v1 JRIN header",
        )

    if now_ns is None:
        now_ns = time.monotonic_ns()
    writer_age = _heartbeat_age_ns(header.writer_heartbeat_ns, now_ns)
    reader_age = _heartbeat_age_ns(header.reader_heartbeat_ns, now_ns)
    # Occupancy is only meaningful when the cursor pair is coherent. Over-range
    # (the writer lapped a wedged reader, or a torn read) and inverted are both
    # published as None rather than as a number: the C resolves an out-of-range
    # W - R by resyncing to the tip, so the raw difference is not an occupancy
    # anybody would act on. The raw cursors stay published, so nothing is hidden.
    raw_occupancy = header.write_seq - header.read_seq
    occupancy: int | None = (
        raw_occupancy if 0 <= raw_occupancy <= header.n_slots else None
    )
    evidence = {
        "writer_age_ns": writer_age,
        "reader_age_ns": reader_age,
        "write_seq": header.write_seq,
        "read_seq": header.read_seq,
        "occupancy_slots": occupancy,
        "writer_epoch": header.writer_epoch,
    }

    writer_live = _end_is_live(
        header.writer_pid, header.writer_heartbeat_ns, now_ns, timeout_ns
    )
    reader_live = _end_is_live(
        header.reader_pid, header.reader_heartbeat_ns, now_ns, timeout_ns
    )

    if not writer_live:
        if header.writer_heartbeat_ns == 0:
            why = "ring has never been written (no writer heartbeat)"
        elif header.writer_pid == 0:
            why = "the writer has closed the ring (writer pid cleared)"
        else:
            why = (
                f"writer heartbeat is itself stale ({(writer_age or 0) / 1e6:.0f} ms)"
            )
        return RingFlowState(
            state=RING_FLOW_IDLE,
            detail=f"{why} — an idle or stopped ring, not a stall",
            **evidence,
        )
    if reader_live:
        return RingFlowState(
            state=RING_FLOW_FLOWING,
            detail=(
                f"both ends live (writer {(writer_age or 0) / 1e6:.0f} ms, reader "
                f"{(reader_age or 0) / 1e6:.0f} ms behind)"
            ),
            **evidence,
        )

    # The writer is live and no reader is. Startup transient or a real stall?
    never_attached = header.reader_pid == 0 and header.reader_heartbeat_ns == 0
    budget = _priming_slot_budget(header, timeout_ns)
    if never_attached and header.write_seq <= budget:
        return RingFlowState(
            state=RING_FLOW_PRIMING,
            detail=(
                f"writer live, no reader attached yet — {header.write_seq} slot(s) "
                f"published, inside the {budget}-slot startup window"
            ),
            **evidence,
        )
    if never_attached:
        why = f"no reader has ever attached ({header.write_seq} slots published)"
    elif header.reader_pid == 0:
        # Reached the instant a reader closes cleanly, not only after it wedges:
        # the writer stops honouring a pid-less reader immediately, so the drops
        # start immediately too.
        why = "the reader closed the ring and has not come back"
    else:
        why = (
            f"reader pid {header.reader_pid} stopped stamping its heartbeat "
            f"({(reader_age or 0) / 1e6:.0f} ms behind)"
        )
    return RingFlowState(
        state=RING_FLOW_READER_STALLED,
        detail=(
            f"{why} while the writer is live — the ioplug has demoted the reader "
            "and is free-running, dropping the oldest slot per publish. read_seq "
            "is being advanced by the WRITER, so its rise is the drop cursor"
        ),
        **evidence,
    )
