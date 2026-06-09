"""Unit tests for jasper.multiroom.reconcile.

The reconciler's decision (`plan`), the argv builders, and the args
assembly (`_assemble_args`) are PURE, total functions — no subprocess, no
systemctl, no clock. These tests drive them with synthetic GroupingConfigs
and assert on the returned ReconcilePlan / argv list / env-key values. The
args-file writer (`_write_args_file`) is exercised against a tmp path, and
`main()` is exercised with `_apply` mocked and the args file redirected to
tmp — so the assemble+persist round-trip is asserted without real
systemctl or touching /run. The real systemctl path (`_apply` itself) is
still validated on hardware.

Mirrors the house style in tests/test_peering_state.py: synthetic inputs,
plain asserts; file I/O goes to pytest's tmp_path.
"""
from __future__ import annotations

import os

from jasper.multiroom.config import DEFAULT_BUFFER_MS, DEFAULT_CODEC, GroupingConfig
from jasper.multiroom import reconcile as reconcile_mod
from jasper.multiroom.reconcile import (
    OUTPUTD_UNIT,
    SNAPCLIENT_UNIT,
    SNAPFIFO,
    SNAPSERVER_UNIT,
    ReconcilePlan,
    UnitIntent,
    _assemble_args,
    _read_outputd_snapfifo_path,
    _write_args_file,
    _write_outputd_snapfifo_env,
    desired_snapfifo_path,
    main,
    outputd_tap_action,
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


# ---------- _assemble_args(): pure derivation of the two env keys ----------
#
# These mirror the snap*_argv tests but assert on the env-key VALUES the
# units read (argv[0] stripped, space-joined), still without any I/O.


SERVER_KEY = "JASPER_SNAPSERVER_ARGS"
CLIENT_KEY = "JASPER_SNAPCLIENT_ARGS"


def test_assemble_args_returns_both_keys_always():
    for cfg in (_disabled(), _invalid(), _leader(), _follower()):
        d = _assemble_args(cfg)
        assert set(d) == {SERVER_KEY, CLIENT_KEY}


def test_assemble_args_leader_sets_both_keys():
    d = _assemble_args(_leader())
    assert d[SERVER_KEY]  # non-empty
    assert d[CLIENT_KEY]  # non-empty


def test_assemble_args_leader_strips_binary_name_from_server():
    """The persisted value is argv AFTER argv[0] — the binary is already
    in the unit's ExecStart, so it must not be duplicated."""
    d = _assemble_args(_leader())
    # snapserver_argv[0] == "snapserver"; the joined value must NOT start
    # with the binary token.
    assert not d[SERVER_KEY].split()[0] == "snapserver"
    assert d[SERVER_KEY] == " ".join(snapserver_argv(_leader())[1:])


def test_assemble_args_leader_strips_binary_name_from_client():
    d = _assemble_args(_leader())
    assert not d[CLIENT_KEY].split()[0] == "snapclient"
    assert d[CLIENT_KEY] == " ".join(snapclient_argv(_leader())[1:])


def test_assemble_args_leader_server_carries_the_fifo_source():
    d = _assemble_args(_leader())
    assert SNAPFIFO in d[SERVER_KEY]
    assert "pipe://" in d[SERVER_KEY]


def test_assemble_args_follower_server_empty_client_set():
    d = _assemble_args(_follower(leader_addr="192.168.1.50"))
    assert d[SERVER_KEY] == ""          # a follower runs no server
    assert d[CLIENT_KEY]                # but does run a client
    assert "--host 192.168.1.50" in d[CLIENT_KEY]


def test_assemble_args_disabled_clears_both():
    d = _assemble_args(_disabled())
    assert d[SERVER_KEY] == ""
    assert d[CLIENT_KEY] == ""


def test_assemble_args_invalid_clears_both():
    """Fail-safe: an enabled-but-broken bond derives empty args so a
    started unit can never pick up stale values."""
    d = _assemble_args(_invalid())
    assert d[SERVER_KEY] == ""
    assert d[CLIENT_KEY] == ""


def test_assemble_args_codec_and_buffer_flow_into_server():
    d = _assemble_args(_leader(codec="opus", buffer_ms=750))
    assert "codec=opus" in d[SERVER_KEY]
    assert "buffer_ms=750" in d[SERVER_KEY]


# ---------- _write_args_file(): atomic, mode 0644, fail-soft ----------


def test_write_args_file_round_trips_keys(tmp_path, monkeypatch):
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    keys = {SERVER_KEY: "--stream.source pipe://x", CLIENT_KEY: "--host 127.0.0.1 --latency 400"}
    assert _write_args_file(keys, path=str(target)) is True
    text = target.read_text()
    assert f"{SERVER_KEY}=--stream.source pipe://x\n" in text
    assert f"{CLIENT_KEY}=--host 127.0.0.1 --latency 400\n" in text


def test_write_args_file_empty_values_writes_bare_keys(tmp_path, monkeypatch):
    """The disabled/invalid case: both keys present but empty — clears
    any stale args rather than leaving the prior value live."""
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    assert _write_args_file({SERVER_KEY: "", CLIENT_KEY: ""}, path=str(target)) is True
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_write_args_file_mode_is_0644(tmp_path, monkeypatch):
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    _write_args_file({SERVER_KEY: "x", CLIENT_KEY: "y"}, path=str(target))
    assert (os.stat(target).st_mode & 0o777) == 0o644


def test_write_args_file_makedirs_parent(tmp_path, monkeypatch):
    """The reconciler os.makedirs the dir (it is NOT a unit
    RuntimeDirectory) — a missing parent must be created, not error."""
    sub = tmp_path / "jasper-grouping"
    target = sub / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(sub))
    assert not sub.exists()
    assert _write_args_file({SERVER_KEY: "x", CLIENT_KEY: ""}, path=str(target)) is True
    assert sub.is_dir()
    assert target.exists()


def test_write_args_file_is_fail_soft(tmp_path, monkeypatch):
    """A write failure (e.g. makedirs raising) returns False and NEVER
    raises — a lost args write must not crash the reconcile."""
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reconcile_mod.os, "makedirs", _boom)
    # Must not raise; must report failure.
    assert _write_args_file({SERVER_KEY: "x", CLIENT_KEY: "y"}) is False


def test_write_args_file_no_partial_file_on_inner_failure(tmp_path, monkeypatch):
    """If the write/rename fails after mkstemp, the temp file is cleaned
    up and no target file is published."""
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise OSError("rename failed")

    monkeypatch.setattr(reconcile_mod.os, "replace", _boom)
    assert _write_args_file({SERVER_KEY: "x", CLIENT_KEY: "y"}, path=str(target)) is False
    assert not target.exists()
    # No leftover temp files in the dir.
    assert list(tmp_path.glob(".snapcast-args.*")) == []


# ---------- main(): assembles + writes args BEFORE applying the plan ----------
#
# main() is the I/O entrypoint; here we stub out the real systemctl calls
# (_apply) and the config load, and redirect the args file to a tmp path,
# so we can assert the args-write happens (and is ordered before _apply)
# without touching the host.


def _patch_main_io(monkeypatch, tmp_path, cfg):
    """Redirect main()'s side effects to a tmp dir + record _apply order."""
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    monkeypatch.setattr(reconcile_mod, "ARGS_FILE", str(target))
    monkeypatch.setattr(reconcile_mod, "load_config", lambda *a, **k: cfg)

    order: list[str] = []
    real_write = reconcile_mod._write_args_file

    def _spy_write(keys, *, path=str(target)):
        order.append("write")
        return real_write(keys, path=path)

    def _fake_apply(plan_):
        order.append("apply")
        # Assert the args file already exists when _apply runs.
        assert target.exists(), "args file must be written BEFORE _apply"
        return 0

    monkeypatch.setattr(reconcile_mod, "_write_args_file", _spy_write)
    monkeypatch.setattr(reconcile_mod, "_apply", _fake_apply)
    # The outputd-tap reconcile is its own concern (tested separately);
    # stub it out so these args/apply tests never touch /run or systemctl.
    monkeypatch.setattr(reconcile_mod, "_reconcile_outputd_tap", lambda *a, **k: 0)
    return target, order


def test_main_leader_writes_both_keys_then_applies(tmp_path, monkeypatch):
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    rc = main([])
    assert rc == 0
    assert order == ["write", "apply"]  # args persisted before units start
    text = target.read_text()
    assert text.startswith(f"{SERVER_KEY}=")
    assert SNAPFIFO in text                       # server reads the fifo
    assert f"\n{CLIENT_KEY}=--host 127.0.0.1" in text  # leader client → loopback


def test_main_follower_writes_client_only(tmp_path, monkeypatch):
    target, order = _patch_main_io(
        monkeypatch, tmp_path, _follower(leader_addr="192.168.1.50")
    )
    rc = main([])
    assert rc == 0
    text = target.read_text()
    assert f"{SERVER_KEY}=\n" in text             # follower: server empty
    assert f"{CLIENT_KEY}=--host 192.168.1.50" in text


def test_main_disabled_writes_empty_args(tmp_path, monkeypatch):
    target, _order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    rc = main([])
    assert rc == 0
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_main_invalid_writes_empty_args(tmp_path, monkeypatch):
    target, _order = _patch_main_io(monkeypatch, tmp_path, _invalid())
    rc = main([])
    assert rc == 0
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_main_survives_args_write_failure(tmp_path, monkeypatch):
    """A failed args write is fail-soft: main() still applies the plan and
    returns its rc, never crashes."""
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    monkeypatch.setattr(reconcile_mod, "ARGS_FILE", str(tmp_path / "x.env"))
    monkeypatch.setattr(reconcile_mod, "load_config", lambda *a, **k: _leader())
    monkeypatch.setattr(reconcile_mod, "_write_args_file", lambda *a, **k: False)
    monkeypatch.setattr(reconcile_mod, "_reconcile_outputd_tap", lambda *a, **k: 0)

    applied = []
    monkeypatch.setattr(
        reconcile_mod, "_apply", lambda plan_: applied.append(True) or 0
    )
    rc = main([])
    assert rc == 0
    assert applied == [True]  # plan still applied despite the write failure


# ---------- outputd snapfifo tap (leader only) ----------


def test_desired_snapfifo_path_leader_taps():
    assert desired_snapfifo_path(_leader()) == SNAPFIFO


def test_desired_snapfifo_path_follower_does_not_tap():
    assert desired_snapfifo_path(_follower()) == ""


def test_desired_snapfifo_path_disabled_does_not_tap():
    assert desired_snapfifo_path(_disabled()) == ""


def test_desired_snapfifo_path_invalid_leader_does_not_tap():
    # enabled + role=leader BUT carrying an error => never tap a broken bond.
    assert desired_snapfifo_path(_invalid()) == ""


def test_outputd_tap_action_no_change_is_false():
    assert outputd_tap_action(SNAPFIFO, SNAPFIFO) is False
    assert outputd_tap_action("", "") is False


def test_outputd_tap_action_change_is_true():
    assert outputd_tap_action(SNAPFIFO, "") is True   # became a leader
    assert outputd_tap_action("", SNAPFIFO) is True   # stopped being a leader


def test_outputd_tap_env_write_read_round_trip(tmp_path):
    p = str(tmp_path / "outputd-snapfifo.env")
    assert _write_outputd_snapfifo_env(SNAPFIFO, path=p) is True
    assert _read_outputd_snapfifo_path(p) == SNAPFIFO
    # clearing writes an EMPTY file => no tap key on the next outputd start.
    assert _write_outputd_snapfifo_env("", path=p) is True
    assert _read_outputd_snapfifo_path(p) == ""


def test_read_outputd_snapfifo_path_absent_file_is_empty(tmp_path):
    assert _read_outputd_snapfifo_path(str(tmp_path / "nope.env")) == ""


def test_reconcile_outputd_tap_no_touch_when_unchanged(monkeypatch):
    # Steady state (current == desired): the final-output owner is NEVER
    # touched — no write, no try-restart. This is the StartLimit-safe gate.
    writes, restarts = [], []
    monkeypatch.setattr(
        reconcile_mod, "_read_outputd_snapfifo_path", lambda *a, **k: SNAPFIFO
    )

    def _fake_write(desired, **k):
        writes.append(desired)
        return True

    def _fake_restart(unit, **k):
        restarts.append(unit)
        return 0

    monkeypatch.setattr(reconcile_mod, "_write_outputd_snapfifo_env", _fake_write)
    monkeypatch.setattr(reconcile_mod, "_try_restart", _fake_restart)

    rc = reconcile_mod._reconcile_outputd_tap(_leader())  # desired == current
    assert rc == 0
    assert writes == []
    assert restarts == []


def test_reconcile_outputd_tap_writes_and_restarts_on_transition(monkeypatch):
    # current="" (not tapping) -> desired=SNAPFIFO (became leader).
    writes, restarts = [], []
    monkeypatch.setattr(
        reconcile_mod, "_read_outputd_snapfifo_path", lambda *a, **k: ""
    )

    def _fake_write(desired, **k):
        writes.append(desired)
        return True

    def _fake_restart(unit, **k):
        restarts.append(unit)
        return 0

    monkeypatch.setattr(reconcile_mod, "_write_outputd_snapfifo_env", _fake_write)
    monkeypatch.setattr(reconcile_mod, "_try_restart", _fake_restart)

    rc = reconcile_mod._reconcile_outputd_tap(_leader())
    assert rc == 0
    assert writes == [SNAPFIFO]
    assert restarts == [OUTPUTD_UNIT]


def test_reconcile_outputd_tap_no_restart_when_write_fails(monkeypatch):
    # A failed env write must NOT restart outputd — that would blip the
    # final-output owner for nothing (it'd re-read the same stale env).
    restarts = []
    monkeypatch.setattr(
        reconcile_mod, "_read_outputd_snapfifo_path", lambda *a, **k: ""
    )
    monkeypatch.setattr(
        reconcile_mod, "_write_outputd_snapfifo_env", lambda *a, **k: False
    )
    monkeypatch.setattr(
        reconcile_mod, "_try_restart", lambda unit, **k: restarts.append(unit) or 0
    )
    rc = reconcile_mod._reconcile_outputd_tap(_leader())
    assert rc == 1          # surfaced
    assert restarts == []   # outputd NOT touched on a failed write
