# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The grouping ring's observability surface (issue #2786).

Before the S0 pacing governor a stalled reader on ``jts_ring_grouping`` announced
itself by sheer volume — the writer free-ran orders of magnitude past real time
and the drop count ran away with it. The governor holds that same fault to
roughly nominal, which fixes the storm and destroys the signature: a stalled
reader and a healthy cold start now look alike in every raw magnitude. So the
SIGNAL has to come from classification rather than size, and these tests pin the
classifier and the two surfaces that carry it.

Three contracts:

**C-1 — the three states are told apart.** Healthy flow, startup transient, and a
stalled reader are three distinct answers from one 128-byte header read.
:func:`test_the_three_operator_states_are_distinguishable` is the bar the issue
sets, asserted directly rather than inferred from the per-state tests below it.

**C-2 — ``/state`` carries them, and only where it should.** The ``ring`` block
appears on a bonded box and is absent from a solo snapshot, preserving
:mod:`jasper.multiroom.state`'s zero-cost-when-N=1 contract, and a broken read
degrades to ``null`` instead of erroring the grouping section.

**C-3 — the doctor's device probe cannot disturb a live bond.** The check opens
and closes the PCM and never reaches ``prepare``, which is the only callback that
attaches the SHM ring. That claim is a claim about the C source, so it is pinned
against the C source — and it is also why the check has no busy case at all:
every ``-EBUSY`` the ring can produce comes from the two guards inside
``jts_ring_writer_open`` / ``jts_ring_reader_open``, which only ``prepare``
reaches. A ring busy with live audio and an idle one are the same answer here.
"""

from __future__ import annotations

import errno
import os
import re
import struct
import subprocess
from pathlib import Path

import pytest

from jasper import ring_assets
from jasper.ring_assets import (
    RING_FLOW_ABSENT,
    RING_FLOW_FLOWING,
    RING_FLOW_IDLE,
    RING_FLOW_PRIMING,
    RING_FLOW_READER_STALLED,
    RING_FLOW_UNREADABLE,
    RING_LIVENESS_TIMEOUT_NS,
    ring_flow_state,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_IOPLUG_C = _REPO_ROOT / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"

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

    Using ``ring_assets``' offsets rather than literals keeps this helper honest
    about one thing only — the VALUES — and leaves the offsets themselves to the
    cross-language pin in ``tests/test_ring_slot_ceiling_pin.py``.
    """
    head = bytearray(128)
    struct.pack_into("<I", head, ring_assets._RING_OFF_MAGIC, magic)
    struct.pack_into("<I", head, ring_assets._RING_OFF_VERSION, version)
    struct.pack_into("<I", head, ring_assets._RING_OFF_RATE, _RATE)
    struct.pack_into("<I", head, ring_assets._RING_OFF_CHANNELS, 2)
    struct.pack_into("<I", head, ring_assets._RING_OFF_SAMPLE_FORMAT, 1)
    struct.pack_into("<I", head, ring_assets._RING_OFF_PERIOD_FRAMES, _PERIOD_FRAMES)
    struct.pack_into("<I", head, ring_assets._RING_OFF_N_SLOTS, n_slots)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_WRITER_EPOCH, writer_epoch)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_WRITE_SEQ, write_seq)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_READ_SEQ, read_seq)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_WRITER_PID, writer_pid)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_READER_PID, reader_pid)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_WRITER_HEARTBEAT_NS, writer_hb)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_READER_HEARTBEAT_NS, reader_hb)
    return bytes(head)


def _ring(tmp_path: Path, **kwargs: object) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "grouping.ring"
    path.write_bytes(_header(**kwargs))  # type: ignore[arg-type]
    return str(path)


def _state(path: str) -> ring_assets.RingFlowState:
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
    assert ring_assets.ring_stall_verdict(closed, now_ns=_NOW_NS).stalled is False
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
    assert ring_assets.ring_stall_verdict(wedged, now_ns=_NOW_NS).stalled is True
    assert _state(wedged).state == RING_FLOW_READER_STALLED
    assert ring_assets.ring_stall_verdict(live, now_ns=_NOW_NS).stalled is False
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

    assert ring_assets.ring_stall_verdict(stalled, now_ns=_NOW_NS).stalled is True
    assert ring_assets.ring_stall_verdict(live, now_ns=_NOW_NS).stalled is False
    assert ring_assets.ring_stall_verdict(idle, now_ns=_NOW_NS).present is False
    # The narrow verdict still alarms where the rich one grants a startup grace:
    # its consumers judge four rings and were not re-scoped by this issue.
    young = _ring(tmp_path / "y", writer_pid=1, writer_hb=_FRESH, write_seq=3)
    assert ring_assets.ring_stall_verdict(young, now_ns=_NOW_NS).stalled is True
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

    monkeypatch.setattr(ring_assets, "open", _spy, raising=False)
    ring = _ring(tmp_path, writer_pid=1, reader_pid=2, writer_hb=_FRESH)

    ring_assets.read_ring_header(ring)
    _state(ring)

    assert modes and set(modes) == {"rb"}


# --- C-2: the /state block -------------------------------------------------


#: A valid bonded follower — the role that actually reads the grouping ring.
_BONDED_FOLLOWER_ENV = (
    "JASPER_GROUPING=on\n"
    "JASPER_GROUPING_ROLE=follower\n"
    "JASPER_GROUPING_CHANNEL=left\n"
    "JASPER_GROUPING_BOND_ID=bond-1\n"
    "JASPER_GROUPING_LEADER_ADDR=10.0.0.9\n"
)


def _grouping_env(tmp_path: Path, body: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "grouping.env"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _bonded_snapshot(tmp_path, flow: ring_assets.RingFlowState) -> dict:
    from jasper.multiroom.state import read_grouping_state

    return read_grouping_state(
        _grouping_env(tmp_path, _BONDED_FOLLOWER_ENV),
        unit_state_reader=lambda units: {u: "active" for u in units},
        tap_path_reader=lambda: "/run/jasper-grouping/snapfifo",
        stream_clients_reader=lambda: [],
        endpoint_status_reader=lambda: {},
        ring_state_reader=lambda: flow,
    )


def test_a_bonded_snapshot_carries_the_ring_block(tmp_path):
    """Every key earns its place: ``state``/``detail`` are the answer, the two
    ages say which end and for how long, and the cursor pair is what a drop count
    would be derived from."""
    from jasper.multiroom.grouping_ring import GROUPING_RING_FILE, GROUPING_RING_PCM

    flow = ring_assets.RingFlowState(
        state=RING_FLOW_READER_STALLED,
        detail="reader pid 202 stopped stamping its heartbeat",
        writer_age_ns=5_000_000,
        reader_age_ns=47_000_000_000,
        write_seq=900_000,
        read_seq=899_984,
        occupancy_slots=16,
        writer_epoch=3,
    )
    ring = _bonded_snapshot(tmp_path, flow)["ring"]

    assert ring == {
        "pcm": GROUPING_RING_PCM,
        "path": GROUPING_RING_FILE,
        "state": RING_FLOW_READER_STALLED,
        "detail": "reader pid 202 stopped stamping its heartbeat",
        "writer_age_ms": 5,
        "reader_age_ms": 47_000,
        "write_seq": 900_000,
        "read_seq": 899_984,
        "occupancy_slots": 16,
        "writer_epoch": 3,
    }


def test_a_never_stamped_heartbeat_stays_null_rather_than_zero(tmp_path):
    """``None`` and ``0 ms`` mean opposite things — never attached versus beating
    right now. The projection must not collapse them."""
    flow = ring_assets.RingFlowState(state=RING_FLOW_PRIMING, reader_age_ns=None)
    ring = _bonded_snapshot(tmp_path, flow)["ring"]
    assert ring["reader_age_ms"] is None


def test_a_solo_snapshot_gains_no_ring_key_and_reads_no_ring(tmp_path):
    """The zero-cost-when-N=1 contract :mod:`jasper.multiroom.state` states for
    every other runtime block. A solo speaker is the overwhelming majority of the
    fleet and its snapshot must stay byte-for-byte what it was."""
    from jasper.multiroom.state import read_grouping_state

    def _must_not_run() -> ring_assets.RingFlowState:  # pragma: no cover
        raise AssertionError("a solo snapshot must not read the grouping ring")

    snapshot = read_grouping_state(
        _grouping_env(tmp_path, "JASPER_GROUPING=off\n"),
        ring_state_reader=_must_not_run,
    )
    assert "ring" not in snapshot
    assert snapshot["enabled"] is False


def test_a_failed_ring_read_nulls_only_its_own_block(tmp_path):
    """The aggregator's fail-soft rule, applied one level down: a dead source
    nulls its section and never errors the call, so a ring problem cannot take
    the grouping section — or the rest of ``/state`` — with it."""
    from jasper.multiroom.state import read_grouping_state

    def _boom() -> ring_assets.RingFlowState:
        raise OSError("shm gone")

    snapshot = read_grouping_state(
        _grouping_env(tmp_path, _BONDED_FOLLOWER_ENV),
        unit_state_reader=lambda units: {u: "active" for u in units},
        endpoint_status_reader=lambda: {},
        ring_state_reader=_boom,
    )
    assert snapshot["ring"] is None
    assert snapshot["runtime"]["health"] == "ok"


# --- C-3: the doctor's device probe ----------------------------------------


@pytest.fixture()
def _installed_confd(tmp_path, monkeypatch):
    """Point the check at a present conf.d file so the probe branch is reached."""
    from jasper.multiroom import grouping_ring

    conf = tmp_path / "62-jts-ring-grouping.conf"
    conf.write_text("pcm.jts_ring_grouping { type jts_ring }\n", encoding="utf-8")
    monkeypatch.setattr(grouping_ring, "GROUPING_RING_CONF_D", str(conf))
    return conf


def _check(monkeypatch, rc, reason="", *, bonded=False):
    from types import SimpleNamespace

    from jasper.cli.doctor import grouping as doctor_grouping
    from jasper.multiroom import config as grouping_config

    monkeypatch.setattr(
        doctor_grouping, "_probe_grouping_pcm", lambda pcm: (rc, reason)
    )
    # The check reads exactly one field off the config, so a stub carrying that
    # field keeps the test about severity rather than about config parsing.
    monkeypatch.setattr(
        grouping_config, "load_config", lambda *a, **k: SimpleNamespace(enabled=bonded)
    )
    return doctor_grouping.check_grouping_ring_device()


def test_a_resolving_pcm_is_ok(monkeypatch, _installed_confd):
    result = _check(monkeypatch, 0)
    assert result.status == "ok"


def test_the_check_has_no_busy_case_because_busy_cannot_happen():
    """The corollary of the attach-site pin below, asserted as ABSENCE.

    Every ``-EBUSY`` the ring can produce comes from the single-writer /
    single-reader guards inside ``jts_ring_writer_open`` /
    ``jts_ring_reader_open``, and both are reached only from the ``prepare``
    callbacks this probe never enters. So there is no busy state to handle, and
    handling one would mean writing a branch that can only ever be wrong about
    why it fired. This guards the deletion: re-adding EBUSY handling here means
    either the probe grew an attach (caught by the two pins below) or someone
    wrote dead code with a false explanation attached.
    """
    import inspect

    from jasper.cli.doctor import grouping as doctor_grouping

    source = inspect.getsource(doctor_grouping.check_grouping_ring_device)
    assert "EBUSY" not in source.split('"""')[-1], (
        "check_grouping_ring_device has no reachable busy case — see its docstring"
    )


def test_a_name_that_does_not_resolve_fails_a_bonded_box(monkeypatch, _installed_confd):
    from jasper.cli.doctor import grouping as doctor_grouping

    result = _check(monkeypatch, -errno.ENOENT, bonded=True)
    assert result.status == "fail"
    assert result.reason == doctor_grouping.REASON_RING_PCM_UNRESOLVED


def test_the_same_defect_is_a_warning_on_a_solo_box(monkeypatch, _installed_confd):
    """Weighed by what it costs THIS box, mirroring ``check_ring_platform_assets``:
    nothing opens the name on a solo speaker yet, so the defect is real but not
    load-bearing — and surfacing it here is the whole point, since it gets fixed
    before anyone tries to bond."""
    from jasper.cli.doctor import grouping as doctor_grouping

    result = _check(monkeypatch, -errno.ENOENT, bonded=False)
    assert result.status == "warn"
    assert result.reason == doctor_grouping.REASON_RING_PCM_UNRESOLVED


def test_a_missing_confd_fails_before_probing(monkeypatch, tmp_path):
    from jasper.cli.doctor import grouping as doctor_grouping
    from jasper.multiroom import grouping_ring

    monkeypatch.setattr(
        grouping_ring, "GROUPING_RING_CONF_D", str(tmp_path / "absent.conf")
    )

    def _must_not_probe(pcm):  # pragma: no cover
        raise AssertionError("no point probing a name that is not declared")

    monkeypatch.setattr(doctor_grouping, "_probe_grouping_pcm", _must_not_probe)
    result = doctor_grouping.check_grouping_ring_device()
    assert result.status == "fail"
    assert result.reason == doctor_grouping.REASON_RING_CONFD_MISSING


def test_an_unrunnable_probe_skips_rather_than_accusing_the_pcm(
    monkeypatch, _installed_confd
):
    """No libasound on this host is a fact about the host, not a verdict about
    the device — the probe never ran, so nothing was observed."""
    from jasper.cli.doctor import grouping as doctor_grouping

    result = _check(monkeypatch, None, "libasound.so.2 unavailable", bonded=True)
    assert result.status == "skipped"
    assert result.reason == doctor_grouping.REASON_RING_PROBE_UNAVAILABLE


def test_the_probe_child_opens_and_closes_and_does_nothing_else():
    """The snippet is the safety argument's other half: whatever the C source
    does at ``prepare``, this never calls it. Pinned so a later "let's just make
    it play a moment to be sure" cannot land quietly."""
    from jasper.cli.doctor.grouping import _GROUPING_PCM_PROBE

    assert "snd_pcm_open" in _GROUPING_PCM_PROBE
    assert "snd_pcm_close" in _GROUPING_PCM_PROBE
    for forbidden in (
        "snd_pcm_prepare",
        "snd_pcm_hw_params",
        "snd_pcm_writei",
        "snd_pcm_readi",
        "snd_pcm_start",
    ):
        assert forbidden not in _GROUPING_PCM_PROBE, (
            f"the probe must not call {forbidden} — that is the path that "
            "attaches the SHM ring and would perturb a live bond"
        )


def test_the_ring_is_attached_only_from_the_prepare_callbacks():
    """WHY OPEN-AND-CLOSE IS SAFE, pinned against the C source it is a claim about.

    ``snd_pcm_open`` runs the plugin's define-func, which parses the conf.d block
    and calls ``snd_pcm_ioplug_create`` — it never touches the ring path as a
    file. The SHM is create-or-attached in the ``prepare`` callbacks, which an
    open-then-close never reaches. Move either attach call into the define-func
    (or into ``hw_params``) and the doctor's probe would start creating ring
    files, stamping a writer pid, and taking the writer flock on a box carrying
    live bonded audio — so this fails there instead.
    """
    source = _IOPLUG_C.read_text(encoding="utf-8")
    enclosing: dict[str, str] = {}
    current = ""
    for line in source.splitlines():
        function = re.match(r"^static\s+[\w\s\*]+?(\w+)\s*\(", line)
        if function:
            current = function.group(1)
        for call in ("jts_ring_writer_open(", "jts_ring_reader_open("):
            if call in line:
                enclosing[call] = current

    assert enclosing == {
        "jts_ring_writer_open(": "jts_ring_prepare",
        "jts_ring_reader_open(": "jts_ring_capture_prepare",
    }, f"the ring is attached from unexpected callbacks: {enclosing}"


def test_the_probe_is_isolated_and_bounded():
    """A child interpreter with a timeout, matching the audio_runtime ring
    probe's isolation posture: ``snd_pcm_open`` dlopens plugin code, and one
    faulting check must not cost the operator every other check's result."""
    from jasper.cli.doctor import grouping as doctor_grouping

    calls: list[tuple[list[str], float]] = []

    def _fake_run(cmd, timeout=5.0):
        calls.append((cmd, timeout))
        return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")

    original = doctor_grouping._run
    doctor_grouping._run = _fake_run  # type: ignore[assignment]
    try:
        rc, reason = doctor_grouping._probe_grouping_pcm("jts_ring_grouping")
    finally:
        doctor_grouping._run = original  # type: ignore[assignment]

    assert (rc, reason) == (0, "")
    cmd, timeout = calls[0]
    assert cmd[-1] == "jts_ring_grouping"
    assert timeout == doctor_grouping._GROUPING_PCM_PROBE_TIMEOUT_SEC
    assert timeout > 0


def test_an_unparseable_probe_answer_is_not_read_as_success():
    from jasper.cli.doctor import grouping as doctor_grouping

    def _fake_run(cmd, timeout=5.0):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Traceback ...")

    original = doctor_grouping._run
    doctor_grouping._run = _fake_run  # type: ignore[assignment]
    try:
        rc, reason = doctor_grouping._probe_grouping_pcm("jts_ring_grouping")
    finally:
        doctor_grouping._run = original  # type: ignore[assignment]

    assert rc is None
    assert "no result" in reason
