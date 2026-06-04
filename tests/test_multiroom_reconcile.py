"""Unit tests for jasper.multiroom.reconcile.

The reconciler's decision (`plan`) and the argv builders are PURE, total
functions — no subprocess, no systemctl, no clock. These tests drive them
with synthetic GroupingConfigs and assert on the returned ReconcilePlan /
argv list. The I/O entrypoint (`main` / `_apply`) is intentionally NOT
exercised here (validated on hardware).

Mirrors the house style in tests/test_peering_state.py: synthetic inputs,
no I/O, plain asserts.
"""
from __future__ import annotations

from jasper.multiroom.config import DEFAULT_BUFFER_MS, DEFAULT_CODEC, GroupingConfig
from jasper.multiroom.reconcile import (
    SNAPCLIENT_UNIT,
    SNAPFIFO,
    SNAPSERVER_UNIT,
    ReconcilePlan,
    UnitIntent,
    plan,
    snapclient_argv,
    snapserver_argv,
)


# ---------- config builders ----------


def _disabled() -> GroupingConfig:
    return GroupingConfig(
        enabled=False,
        role="",
        channel="stereo",
        bond_id="",
        leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error=None,
    )


def _leader(*, channel="left", bond_id="living-room", buffer_ms=DEFAULT_BUFFER_MS,
            codec=DEFAULT_CODEC) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="leader",
        channel=channel,
        bond_id=bond_id,
        leader_addr="",
        buffer_ms=buffer_ms,
        codec=codec,
        error=None,
    )


def _follower(*, channel="right", bond_id="living-room", leader_addr="192.168.1.50",
              buffer_ms=DEFAULT_BUFFER_MS, codec=DEFAULT_CODEC) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="follower",
        channel=channel,
        bond_id=bond_id,
        leader_addr=leader_addr,
        buffer_ms=buffer_ms,
        codec=codec,
        error=None,
    )


def _invalid() -> GroupingConfig:
    """Enabled but carrying an error (the fail-LOUD state)."""
    return GroupingConfig(
        enabled=True,
        role="leader",
        channel="left",
        bond_id="",
        leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error="JASPER_GROUPING_BOND_ID is empty (grouping is on)",
    )


def _desired(plan_: ReconcilePlan, unit: str) -> str:
    """Pull the single desired state for a unit out of a plan."""
    matches = [i.desired for i in plan_.intents if i.unit == unit]
    assert len(matches) == 1, f"expected exactly one intent for {unit}, got {matches}"
    return matches[0]


# ---------- plan(): disabled => stop both ----------


def test_plan_disabled_stops_both():
    p = plan(_disabled())
    assert _desired(p, SNAPSERVER_UNIT) == "stop"
    assert _desired(p, SNAPCLIENT_UNIT) == "stop"
    assert "solo" in p.summary


def test_plan_disabled_has_exactly_two_intents():
    p = plan(_disabled())
    assert len(p.intents) == 2
    units = {i.unit for i in p.intents}
    assert units == {SNAPSERVER_UNIT, SNAPCLIENT_UNIT}


# ---------- plan(): enabled + error => stop both (never start a broken bond) ----------


def test_plan_enabled_but_invalid_stops_both():
    p = plan(_invalid())
    assert _desired(p, SNAPSERVER_UNIT) == "stop"
    assert _desired(p, SNAPCLIENT_UNIT) == "stop"


def test_plan_invalid_summary_surfaces_error_and_not_starting():
    p = plan(_invalid())
    assert "INVALID" in p.summary
    assert "BOND_ID" in p.summary
    assert "not starting" in p.summary


def test_plan_invalid_starts_nothing():
    """Fail-safe: a broken bond must never produce a start intent."""
    p = plan(_invalid())
    assert all(i.desired == "stop" for i in p.intents)


# ---------- plan(): leader => start server + start client ----------


def test_plan_leader_starts_server_and_client():
    p = plan(_leader())
    assert _desired(p, SNAPSERVER_UNIT) == "start"
    assert _desired(p, SNAPCLIENT_UNIT) == "start"


def test_plan_leader_summary_mentions_bond_and_channel():
    p = plan(_leader(channel="left", bond_id="living-room"))
    assert "living-room" in p.summary
    assert "left" in p.summary


# ---------- plan(): follower => stop server + start client ----------


def test_plan_follower_stops_server_starts_client():
    p = plan(_follower())
    assert _desired(p, SNAPSERVER_UNIT) == "stop"
    assert _desired(p, SNAPCLIENT_UNIT) == "start"


def test_plan_follower_summary_mentions_leader_addr():
    p = plan(_follower(leader_addr="10.0.0.7"))
    assert "10.0.0.7" in p.summary


# ---------- plan(): stops-before-starts ordering ----------


def test_plan_intents_ordered_stops_before_starts_leader():
    """Leader has no stops, but the ordering invariant must still hold
    (no start precedes a stop) — trivially true here."""
    p = plan(_leader())
    desireds = [i.desired for i in p.intents]
    assert _stops_before_starts(desireds)


def test_plan_intents_ordered_stops_before_starts_follower():
    """Follower has one stop (server) and one start (client) — the stop
    must come first so a role flip tears down before bringing up."""
    p = plan(_follower())
    desireds = [i.desired for i in p.intents]
    assert _stops_before_starts(desireds)
    # Be explicit: the first intent is the server stop.
    assert p.intents[0].unit == SNAPSERVER_UNIT
    assert p.intents[0].desired == "stop"


def test_plan_intents_ordered_stops_before_starts_disabled():
    p = plan(_disabled())
    assert _stops_before_starts([i.desired for i in p.intents])


def test_plan_intents_ordered_stops_before_starts_invalid():
    p = plan(_invalid())
    assert _stops_before_starts([i.desired for i in p.intents])


def _stops_before_starts(desireds: list[str]) -> bool:
    """True iff no "start" appears before a "stop" in the sequence."""
    seen_start = False
    for d in desireds:
        if d == "start":
            seen_start = True
        elif d == "stop" and seen_start:
            return False
    return True


# ---------- plan(): returned types ----------


def test_plan_returns_reconcileplan_of_unitintents():
    p = plan(_leader())
    assert isinstance(p, ReconcilePlan)
    assert all(isinstance(i, UnitIntent) for i in p.intents)
    assert isinstance(p.summary, str) and p.summary


# ---------- snapserver_argv(): codec + buffer_ms flow into argv ----------


def test_snapserver_argv_includes_codec():
    argv = snapserver_argv(_leader(codec="opus"))
    joined = " ".join(argv)
    assert "codec=opus" in joined


def test_snapserver_argv_includes_buffer_ms():
    argv = snapserver_argv(_leader(buffer_ms=750))
    joined = " ".join(argv)
    assert "buffer_ms=750" in joined


def test_snapserver_argv_reads_the_fifo_source():
    argv = snapserver_argv(_leader())
    joined = " ".join(argv)
    assert SNAPFIFO in joined
    assert "pipe://" in joined


def test_snapserver_argv_starts_with_snapserver():
    argv = snapserver_argv(_leader())
    assert argv[0] == "snapserver"


def test_snapserver_argv_codec_passthrough_all_codecs():
    for codec in ("pcm", "flac", "opus"):
        argv = snapserver_argv(_leader(codec=codec))
        assert f"codec={codec}" in " ".join(argv)


# ---------- snapclient_argv(): host + buffer targeting ----------


def test_snapclient_argv_leader_targets_loopback():
    """The leader runs its own server, so its client targets 127.0.0.1."""
    argv = snapclient_argv(_leader())
    assert "127.0.0.1" in argv


def test_snapclient_argv_follower_targets_leader_addr():
    argv = snapclient_argv(_follower(leader_addr="192.168.1.50"))
    assert "192.168.1.50" in argv
    # And NOT the loopback.
    assert "127.0.0.1" not in argv


def test_snapclient_argv_latency_from_buffer_ms():
    argv = snapclient_argv(_follower(buffer_ms=600))
    assert "600" in argv


def test_snapclient_argv_starts_with_snapclient():
    argv = snapclient_argv(_leader())
    assert argv[0] == "snapclient"


def test_snapclient_argv_host_flag_present():
    argv = snapclient_argv(_follower(leader_addr="10.0.0.7"))
    assert "--host" in argv
    # The value immediately follows the flag.
    assert argv[argv.index("--host") + 1] == "10.0.0.7"
