# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for ``jasper-arm-walk``.

Every safety sentence the harness's docstrings make is a test here: power before
every walk move, the envelope clamp, the park-and-verify on each exit path, the
never-``set-zero`` rule, and the settle floor.

Two harness-level rules the individual tests lean on: :class:`FakeClock` bounds
the sleeps, so a loop that stops terminating FAILS rather than hangs; and
:meth:`_RecordingTrail.error` records the log LEVEL, so a failure that quietly
logged at INFO — invisible to an operator grepping the journal for trouble — is
a test failure rather than a passing assertion about its fields.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
from pathlib import Path

import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import arm_walk as aw
from jasper.active_speaker import wizard_client as wc
from jasper.cli import arm_walk as cli

ROOT = Path(__file__).resolve().parents[1]
TURNTABLE_SCRIPT = ROOT / "experiments" / "usb-turntable" / "jts_turntable.py"


def _load_turntable():
    spec = importlib.util.spec_from_file_location("jts_turntable_for_arm_walk",
                                                  TURNTABLE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# fakes -- the three seams
# --------------------------------------------------------------------------- #


class FakeClock:
    """Time only moves when something sleeps, so a measured settle is exact.

    ``max_sleeps`` is the bounded-waits rule: a loop that stops terminating
    FAILS here, with a sentence, instead of hanging the suite until a timeout
    kills it. Every scenario below finishes in tens of sleeps.
    """

    def __init__(self, max_sleeps: int = 500) -> None:
        self.t = 1000.0
        self._sleeps_left = max_sleeps

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self._sleeps_left -= 1
        if self._sleeps_left < 0:
            raise AssertionError(
                "the walk loop slept past its bound without terminating"
            )
        self.t += seconds


class FakeMover:
    def __init__(self, *, power=None, move_ok=True, offset=0.0) -> None:
        # A list is consumed one verdict per read, then the last one repeats.
        self._power = list(power or [aw.PowerVerdict(True, "throttled=0x0")])
        self._move_ok = move_ok
        self._offset = offset
        self.calls: list[tuple] = []

    def power(self) -> aw.PowerVerdict:
        verdict = self._power[0] if len(self._power) == 1 else self._power.pop(0)
        self.calls.append(("power", verdict.clean))
        return verdict

    def move_to(self, degrees: int) -> bool:
        self.calls.append(("move_to", degrees))
        return self._move_ok

    def offset_deg(self) -> float | None:
        self.calls.append(("offset_deg",))
        return self._offset

    @property
    def moves(self) -> list[int]:
        return [c[1] for c in self.calls if c[0] == "move_to"]


class FakeSession:
    """A queue of polls that advances on RELEASE, exactly as the gate does.

    The real gate republishes the same hold on every read until the driver
    reports the position, so a fake that popped per poll would let a walk skip a
    position no live session ever skips. The final entry repeats forever.
    """

    def __init__(self, polls, *, release=(200, '{"ok": true}'),
                 complete=(200, '{"ok": true}')) -> None:
        self._queue = list(polls)
        self._release = release
        self._complete = complete
        self.released: list[int] = []
        self.completes = 0

    def poll(self) -> aw.Poll:
        return self._queue[0]

    def release(self, index: int) -> tuple[int, str]:
        self.released.append(index)
        if len(self._queue) > 1:
            self._queue.pop(0)
        return self._release

    def complete(self) -> tuple[int, str]:
        self.completes += 1
        return self._complete


@contextlib.contextmanager
def _own_signals():
    """Install handlers for the block, then always give the process back.

    A test that left SIGTERM on ``SIG_IGN`` would make every later test — and
    pytest's own shutdown — unkillable.

    Keyed off ``PARK_ON_SIGNALS`` rather than a hand-listed pair: a signal
    added to the park set is one this fixture must hand back, and a list that
    fell behind would leave it disarmed for the rest of the session.
    """
    previous = {sig: signal.getsignal(sig) for sig in aw.PARK_ON_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _pending(index: int, degrees: int, attempt: int = 1) -> aw.Poll:
    return aw.Poll(aw.Pending(index, attempt, degrees, "onax"), True, None)


_QUIET = aw.Poll(None, False, None)
_IN_FLIGHT_QUIET = aw.Poll(None, True, None)
#: What an unreachable, 403ing, or non-JSON status endpoint reads as.
_UNREADABLE = aw.Poll(None, True, None, readable=False)
#: The three terminal blocks a finished session leaves in the capture slot, in
#: the shape ``poll_from_status`` really mints them (``in_flight`` is FALSE --
#: the block is the outcome the status page renders, not a live session).
_COMPLETE = aw.Poll(None, False, None, ended="complete")
_STOPPED = aw.Poll(None, False, None, ended="stopped")


def _failed(error: str = "the fit refused: level too low") -> aw.Poll:
    return aw.Poll(None, False, error, ended="failed")


class LiveThen:
    """One LIVE poll, then the given poll forever.

    A terminal status is only this walk's verdict once the walk has read its
    session live, so a double that answers terminal from the first poll is
    replaying the PREVIOUS round's outcome, not this walk's session. Every test
    about a session ENDING therefore has to let it begin.
    """

    def __init__(self, terminal: aw.Poll) -> None:
        self._terminal = terminal
        self._polls = 0

    def poll(self) -> aw.Poll:
        self._polls += 1
        return _IN_FLIGHT_QUIET if self._polls == 1 else self._terminal

    def release(self, index: int) -> tuple[int, str]:  # pragma: no cover
        raise AssertionError("nothing is ever pending in this double")

    def complete(self) -> tuple[int, str]:  # pragma: no cover
        raise AssertionError("this double never completes a stage")


def _walk(mover, session, *, clock=None, trail=None, walk_staged=None, **cfg):
    clock = clock or FakeClock()
    config = aw.WalkConfig(**{
        "settle_s": 30.0, "poll_s": 3.0, "idle_ceiling_s": 60.0,
        "stuck_alarm_s": 300.0, "unreadable_ceiling_s": 60.0, **cfg,
    })
    return aw.ArmWalk(
        mover, session, config,
        trail=trail, walk_staged=walk_staged,
        clock=clock.now, sleep=clock.sleep,
    )


# --------------------------------------------------------------------------- #
# the bounds SSOT
# --------------------------------------------------------------------------- #


def test_the_arm_envelope_is_the_adapters_own_bound():
    """A second copy of +/-45 that drifted would refuse or admit the wrong walk."""
    turntable = _load_turntable()
    assert float(aw.ARM_ENVELOPE_DEG) == turntable.MEASUREMENT_MAX_DEGREES
    assert float(-aw.ARM_ENVELOPE_DEG) == turntable.MEASUREMENT_MIN_DEGREES
    assert ac.MOVER_MAX_ANGLE_DEG[ac.MOVER_ARM] == aw.ARM_ENVELOPE_DEG


def test_an_arm_walk_past_the_envelope_is_refused_where_it_is_stated():
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.per_driver_at([7, 60], mover=ac.MOVER_ARM)
    assert excinfo.value.reason == ac.WALK_OVER_MOVER_ENVELOPE
    assert "+60" in excinfo.value.detail
    assert "45" in excinfo.value.detail
    assert ac.WALK_OVER_MOVER_ENVELOPE in ac.WALK_REFUSAL_REASONS


def test_the_same_angle_is_fine_for_a_person():
    """The bound is the ARM's reach, not the geometry's -- a person still walks."""
    request = ac.per_driver_at([60, -60], mover=ac.MOVER_HUMAN)
    assert [stop.angle_deg for stop in request.stops] == [60, -60]


def test_the_geometry_ceiling_still_refuses_both_movers():
    for mover in ac.MOVERS:
        with pytest.raises(ac.CrossoverV2FlowError):
            ac.per_driver_at([ac.MAX_ANGLE_DEG + 1], mover=mover)


# --------------------------------------------------------------------------- #
# power is read before every WALK move
# --------------------------------------------------------------------------- #


def test_power_is_read_before_every_walk_move():
    """One read immediately before each move the WALK makes.

    The trailing park move is deliberately not preceded by one here — see
    ``test_the_park_move_is_attempted_without_a_fresh_power_read`` for why that
    is the safe direction and what gates it instead.
    """
    mover = FakeMover()
    session = FakeSession([_pending(1, 0), _pending(2, 22), _QUIET])
    assert _walk(mover, session, idle_ceiling_s=10.0).run() == aw.EXIT_OK
    order = [c[0] for c in mover.calls if c[0] in {"power", "move_to"}]
    assert order == ["power", "power", "move_to", "power", "move_to", "move_to"]
    assert mover.moves == [0, 22, 0]


def test_the_park_move_is_attempted_without_a_fresh_power_read():
    """A walk parking BECAUSE of a power sign must still be allowed home.

    Re-checking here would refuse the park in exactly the case it matters most
    and strand the arm at the last measured angle. The adapter's own preflight
    still blocks a motion under a CURRENT flag, so a park during a live brownout
    is refused there and reported, not silently attempted.
    """
    mover = FakeMover(power=[aw.PowerVerdict(False, "current=[under_voltage]")])
    assert _walk(mover, FakeSession([_pending(1, 22)])).run() == aw.EXIT_POWER_VOID
    # The void is the FIRST call; the park move follows it with no second read.
    assert [c[0] for c in mover.calls if c[0] in {"power", "move_to"}] == [
        "power", "move_to",
    ]


def test_a_power_sign_at_the_start_moves_nothing():
    mover = FakeMover(power=[aw.PowerVerdict(False, "current=[under_voltage]")])
    session = FakeSession([_pending(1, 22)])
    assert _walk(mover, session).run() == aw.EXIT_POWER_VOID
    assert session.released == []
    # No WALK move was issued. The single move is the park, which is attempted
    # unconditionally; whether it succeeds is the adapter's call, and under a
    # current flag like this one it will refuse and the park reports ok=false.
    assert mover.moves == [0]


def test_a_power_sign_mid_walk_stops_parks_and_reports():
    mover = FakeMover(power=[
        aw.PowerVerdict(True, "throttled=0x0"),   # start preflight
        aw.PowerVerdict(True, "throttled=0x0"),   # before the first move
        aw.PowerVerdict(False, "since_boot=[under_voltage]"),  # voids the run
    ])
    session = FakeSession([_pending(1, 7), _pending(2, 22), _QUIET])
    trail = _RecordingTrail()
    assert _walk(mover, session, trail=trail).run() == aw.EXIT_POWER_VOID
    assert session.released == [1]
    assert mover.moves == [7, 0]
    void = trail.error("power_void")
    assert void["when"] == "before_move" and void["degrees"] == 22
    assert "under_voltage" in void["detail"]


def test_a_since_boot_flag_is_a_power_sign():
    """Stricter than the adapter's own gate, and the docstring says why."""
    clean = aw.parse_power({"power": {"status": {
        "available": True, "current_flags": [], "history_flags": [],
        "raw": "0x0"}}})
    assert clean.clean
    historic = aw.parse_power({"power": {"status": {
        "available": True, "current_flags": [], "history_flags": ["under_voltage"],
        "raw": "0x50000"}}})
    assert not historic.clean and "under_voltage" in historic.detail


def test_an_unreadable_power_reading_fails_closed():
    verdict = aw.parse_power({"power": {"status": {
        "available": False, "detail": "vcgencmd exited 1"}}})
    assert not verdict.clean and "vcgencmd" in verdict.detail
    assert not aw.parse_power({}).clean


# --------------------------------------------------------------------------- #
# the envelope clamp
# --------------------------------------------------------------------------- #


def test_a_pending_outside_the_envelope_is_refused_before_any_move():
    mover = FakeMover()
    session = FakeSession([_pending(1, aw.ARM_ENVELOPE_DEG + 1), _QUIET])
    trail = _RecordingTrail()
    assert _walk(mover, session, trail=trail).run() == aw.EXIT_MOVE_FAILED
    assert session.released == []
    assert mover.moves == [0]  # the park, and nothing else
    refused = trail.error("move_refused")
    assert refused["reason"] == "envelope"
    assert refused["envelope_deg"] == aw.ARM_ENVELOPE_DEG


def test_the_configured_expectations_are_clamped_too():
    with pytest.raises(aw.ArmWalkRefused, match="45"):
        aw.WalkConfig(expect_angles=(7, 60))


# --------------------------------------------------------------------------- #
# park, on every exit path
# --------------------------------------------------------------------------- #


def test_a_clean_finish_parks_and_verifies_the_magnitude():
    mover = FakeMover(offset=0.0)
    trail = _RecordingTrail()
    assert _walk(mover, FakeSession([_pending(1, 7), _QUIET]), trail=trail,
                 idle_ceiling_s=10.0).run() == aw.EXIT_OK
    parked = trail.one("parked")
    assert parked["ok"] is True and parked["offset_deg"] == 0.0
    assert mover.moves[-1] == 0


def test_the_park_readbacks_sign_is_never_consumed():
    """Command sign is truth; the readback's is negated upstream.

    A negative reading well inside tolerance is a PARKED arm, and a check that
    looked at the sign would call it a failure.
    """
    mover = FakeMover(offset=-0.01)
    trail = _RecordingTrail()
    assert _walk(mover, FakeSession([_QUIET]), trail=trail,
                 idle_ceiling_s=10.0).run() == aw.EXIT_IDLE_CEILING
    assert trail.one("parked")["ok"] is True

    off = FakeMover(offset=-0.9)
    off_trail = _RecordingTrail()
    _walk(off, FakeSession([_QUIET]), trail=off_trail, idle_ceiling_s=10.0).run()
    assert off_trail.error("parked")["ok"] is False


def test_an_unreadable_park_is_reported_not_assumed():
    mover = FakeMover(offset=None)
    trail = _RecordingTrail()
    _walk(mover, FakeSession([_QUIET]), trail=trail, idle_ceiling_s=10.0).run()
    parked = trail.error("parked")
    assert parked["ok"] is False and parked["offset_deg"] == "unreadable"


def test_an_exception_out_of_the_loop_still_parks():
    class Exploding(FakeSession):
        def poll(self):
            raise RuntimeError("the wizard fell over")

    mover = FakeMover()
    trail = _RecordingTrail()
    with pytest.raises(RuntimeError):
        _walk(mover, Exploding([_QUIET]), trail=trail).run()
    assert mover.moves == [0]
    assert trail.one("parked")["ok"] is True
    assert "RuntimeError" in trail.error("aborted")["error"]


def test_a_park_that_fails_is_reported_and_never_masks_the_verdict():
    """The park runs inside the walk's ``finally``; raising there would swap
    the walk's own outcome for the park's."""
    class Unparkable(FakeMover):
        def move_to(self, degrees: int) -> bool:
            super().move_to(degrees)
            raise OSError("the controller is gone")

    trail = _RecordingTrail()
    walk = _walk(Unparkable(), LiveThen(_failed("the session failed")), trail=trail)
    assert walk.run() == aw.EXIT_SESSION_FAILED
    assert "OSError" in trail.error("parked")["error"]


def test_a_sigterm_still_parks():
    """SIGTERM becomes SystemExit, which the run's park ``finally`` catches."""
    class Terminated(FakeSession):
        def poll(self):
            raise SystemExit(143)

    mover = FakeMover()
    with pytest.raises(SystemExit):
        _walk(mover, Terminated([_QUIET])).run()
    assert mover.moves == [0]


def test_the_cli_turns_sigterm_into_that_systemexit():
    with _own_signals():
        cli._install_park_on_signals()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as excinfo:
            handler(signal.SIGTERM, None)
        assert excinfo.value.code == 143


def test_the_first_signal_disarms_both_so_a_second_cannot_land():
    with _own_signals():
        cli._install_park_on_signals()
        with pytest.raises(SystemExit):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        for sig in aw.PARK_ON_SIGNALS:
            assert signal.getsignal(sig) is signal.SIG_IGN


def test_every_park_signal_is_installed_and_named_by_its_own_exit_code():
    """One rule, both ends: the handler exits 128+signum and EXIT_NAMES says so.

    SIGHUP is the load-bearing member. A remote walk is stopped by its ssh
    transport going away, which hangs up this process group — and Python's
    DEFAULT for SIGHUP is death with no unwinding, so before it joined this set
    every dropped link left the arm wherever it stood.
    """
    with _own_signals():
        cli._install_park_on_signals()
        for sig in aw.PARK_ON_SIGNALS:
            handler = signal.getsignal(sig)
            assert callable(handler), sig
            with pytest.raises(SystemExit) as excinfo:
                handler(sig, None)
            assert excinfo.value.code == aw.SIGNAL_EXIT_BASE + int(sig)
            assert aw.EXIT_NAMES[aw.SIGNAL_EXIT_BASE + int(sig)].endswith("_parked")
            # Re-arm for the next iteration: the first fire disarms them all.
            cli._install_park_on_signals()
    assert signal.SIGHUP in aw.PARK_ON_SIGNALS


def test_a_real_sighup_runs_the_park_rather_than_killing_the_process():
    """A DELIVERED SIGHUP, in a real process — not a hand-called handler.

    The bug this pins is Python's default disposition, so calling the handler
    in-process cannot see it: that path is already unwinding. Only an actual
    kill(SIGHUP) against a process that installed the handlers proves the
    ``finally`` still runs, which on a speaker is the park.
    """
    child = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from jasper.cli.arm_walk import _install_park_on_signals
        _install_park_on_signals()
        try:
            open(sys.argv[1], "w").write("ready")
            while True:
                time.sleep(0.02)
        finally:
            open(sys.argv[2], "w").write("parked")
    """)
    with tempfile.TemporaryDirectory() as tmp:
        ready, parked = Path(tmp) / "ready", Path(tmp) / "parked"
        proc = subprocess.Popen([sys.executable, "-c", child, str(ready), str(parked)])
        try:
            deadline = time.monotonic() + 60
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists(), "the child never installed its handlers"
            proc.send_signal(signal.SIGHUP)
            rc = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # Inside the temporary directory's lifetime, or the marker is gone
        # before it is read and the assertion below fails for the wrong reason.
        assert parked.exists(), "SIGHUP killed the process without running the park"
        assert rc == aw.SIGNAL_EXIT_BASE + int(signal.SIGHUP) == 129


def test_a_second_signal_during_the_park_cannot_abandon_the_arm():
    """The worst timing there is: SIGTERM while the adapter is mid-travel.

    With the handler still armed it raises a SECOND ``SystemExit`` inside the
    walk's own ``finally``, and the arm stops at an unknown angle, unverified —
    the state one signal was meant to avoid. The park's completion is what this
    asserts; ``trail.one("parked")`` is absent entirely if the park was cut off.
    """
    class Signalling(FakeMover):
        def move_to(self, degrees: int) -> bool:
            super().move_to(degrees)
            os.kill(os.getpid(), signal.SIGTERM)  # delivered DURING the park
            return True

    class Terminating(FakeSession):
        def poll(self) -> aw.Poll:
            os.kill(os.getpid(), signal.SIGTERM)  # the first signal
            return _QUIET

    mover = Signalling()
    trail = _RecordingTrail()
    with _own_signals():
        cli._install_park_on_signals()
        with pytest.raises(SystemExit):
            _walk(mover, Terminating([_QUIET]), trail=trail).run()
    assert mover.moves == [0]
    assert trail.one("parked")["ok"] is True


def test_the_park_runs_once():
    mover = FakeMover()
    walk = _walk(mover, FakeSession([_QUIET]), idle_ceiling_s=10.0)
    walk.run()
    walk._park()
    assert mover.moves == [0]


# --------------------------------------------------------------------------- #
# set-zero is unreachable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module", [
    "jasper/active_speaker/arm_walk.py",
    # The CLI is checked HERE and only here. It is allowlisted in the turntable
    # product-surface guard (it cites the stop unit for the `User=pi` identity),
    # so that guard can no longer notice a leak in it -- and ``ast.walk`` reaches
    # a function-body import, which is exactly the shape a lazy import would
    # take.
    "jasper/cli/arm_walk.py",
])
def test_the_adapter_is_a_subprocess_and_never_an_import(module):
    """``experiments/`` is not a package product code may depend on.

    A subprocess is also what gives each command a clean serial session and the
    adapter's own documented one-retry recovery.
    """
    import ast

    tree = ast.parse((ROOT / module).read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [name for name in imported
                if "turntable" in name or "experiment" in name]


def test_no_adapter_verb_this_module_emits_can_redefine_zero():
    assert "set-zero" not in aw._TOOL_SUBCOMMANDS
    with pytest.raises(AssertionError):
        aw.TurntableMover(attest_rig_clear=True)._invoke("set-zero")


def test_a_whole_walk_emits_only_the_allowed_verbs():
    recorded: list[list[str]] = []

    def fake_run(argv, **_):
        recorded.append(list(argv))
        verb = argv[3]
        if verb == "power":
            payload = {"ok": True, "power": {"status": {
                "available": True, "current_flags": [], "history_flags": [],
                "raw": "0x0"}}}
        elif verb == "offset":
            payload = {"ok": True, "result": {"offset_degrees": 0.0}}
        else:
            payload = {"ok": True, "result": {}}
        return _Proc(json.dumps(payload))

    mover = aw.TurntableMover(attest_rig_clear=True, run=fake_run)
    session = FakeSession([_pending(1, 22), _QUIET])
    assert _walk(mover, session, idle_ceiling_s=10.0).run() == aw.EXIT_OK
    verbs = {argv[3] for argv in recorded}
    assert verbs <= aw._TOOL_SUBCOMMANDS
    flat = " ".join(" ".join(argv) for argv in recorded)
    assert "set-zero" not in flat and "--confirm-redefine-zero" not in flat


# --------------------------------------------------------------------------- #
# the attestation
# --------------------------------------------------------------------------- #


def test_the_attestation_becomes_both_adapter_confirmations():
    recorded: list[list[str]] = []

    def fake_run(argv, **_):
        recorded.append(list(argv))
        return _Proc(json.dumps({"ok": True, "result": {}}))

    assert aw.TurntableMover(attest_rig_clear=True, run=fake_run).move_to(-22)
    argv = recorded[0]
    assert argv[3:5] == ["position", "-22"]
    assert "--confirm-rig-clear" in argv and "--confirm-zero-valid" in argv


def test_every_emitted_argv_is_one_the_adapter_actually_accepts():
    """The argv is a contract with a tool this module never imports.

    Parsed by the ADAPTER's own parser, so a flag rename or a reordered global
    option fails here rather than at 2 a.m. on a rig.
    """
    recorded: list[list[str]] = []

    def fake_run(argv, **_):
        recorded.append(list(argv))
        return _Proc(json.dumps({"ok": True, "result": {"offset_degrees": 0.0}}))

    mover = aw.TurntableMover(attest_rig_clear=True, run=fake_run)
    mover.move_to(-22)
    mover.power()
    mover.offset_deg()

    parser = _load_turntable().build_parser()
    parsed = [parser.parse_args(argv[2:]) for argv in recorded]
    assert [p.command for p in parsed] == ["position", "power", "offset"]
    assert all(p.json for p in parsed)
    assert parsed[0].degrees == -22.0
    assert parsed[0].confirm_rig_clear and parsed[0].confirm_zero_valid


def test_without_the_attestation_nothing_moves():
    def fake_run(argv, **_):  # pragma: no cover - must never be reached
        raise AssertionError("a move was issued without an attestation")

    with pytest.raises(aw.ArmWalkRefused):
        aw.TurntableMover(run=fake_run).move_to(7)


def test_the_cli_requires_the_attestation():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--hostname", "jts3.local"])


def test_an_adapter_that_does_not_answer_json_is_a_failed_move():
    mover = aw.TurntableMover(attest_rig_clear=True,
                              run=lambda argv, **_: _Proc("not json", 1))
    assert mover.move_to(7) is False
    assert mover.offset_deg() is None
    assert not mover.power().clean


def test_an_adapter_that_cannot_be_launched_is_a_failed_move():
    def boom(argv, **_):
        raise OSError("no such file")

    mover = aw.TurntableMover(attest_rig_clear=True, run=boom)
    assert mover.move_to(7) is False
    assert not mover.power().clean


# --------------------------------------------------------------------------- #
# the settle floor
# --------------------------------------------------------------------------- #


def test_a_settle_under_the_floor_is_refused_before_anything_runs():
    with pytest.raises(aw.ArmWalkRefused, match="floor"):
        aw.WalkConfig(settle_s=aw.SETTLE_FLOOR_S - 0.1)
    assert aw.WalkConfig(settle_s=aw.SETTLE_FLOOR_S).settle_s == aw.SETTLE_FLOOR_S
    assert aw.DEFAULT_SETTLE_S >= aw.SETTLE_FLOOR_S


def test_a_measured_settle_under_the_floor_ends_the_walk():
    """The floor is checked against the settle TAKEN, not the one configured."""
    class FrozenClock(FakeClock):
        def sleep(self, seconds: float) -> None:
            self.t += 1.0  # a sleep that did not sleep

    mover = FakeMover()
    session = FakeSession([_pending(1, 7), _QUIET])
    trail = _RecordingTrail()
    walk = _walk(mover, session, clock=FrozenClock(), trail=trail)
    assert walk.run() == aw.EXIT_SETTLE_FLOOR
    assert session.released == []
    assert trail.error("settle_floor")["floor_s"] == aw.SETTLE_FLOOR_S


def test_the_settle_actually_taken_is_what_the_trail_states():
    trail = _RecordingTrail()
    session = FakeSession([_pending(1, 7), _QUIET])
    assert _walk(FakeMover(), session, trail=trail, settle_s=12.0,
                 idle_ceiling_s=10.0).run() == aw.EXIT_OK
    assert trail.one("released")["settled_s"] == 12.0


# --------------------------------------------------------------------------- #
# the loop's own exits
# --------------------------------------------------------------------------- #


def test_a_failed_session_exits_immediately_with_its_own_error():
    mover = FakeMover()
    trail = _RecordingTrail()
    walk = _walk(mover, LiveThen(_failed()), trail=trail, idle_ceiling_s=600.0)
    assert walk.run() == aw.EXIT_SESSION_FAILED
    assert trail.error("session_failed")["error"] == "the fit refused: level too low"
    assert mover.moves == [0]


def test_a_stuck_capture_is_named_rather_than_waited_out():
    trail = _RecordingTrail()
    walk = _walk(FakeMover(), FakeSession([_IN_FLIGHT_QUIET]), trail=trail,
                 stuck_alarm_s=10.0, idle_ceiling_s=600.0)
    assert walk.run() == aw.EXIT_STUCK
    assert trail.error("stuck")["released"] == 0


def test_an_unreadable_status_is_never_diagnosed_as_a_stuck_capture():
    """The mis-attribution S4 names: a wrong --hostname blamed on #2506.

    A 403ing endpoint has no pending and no verdict, which looks exactly like a
    session sitting on a rejected capture. It must be reported for what it is.
    """
    trail = _RecordingTrail()
    walk = _walk(FakeMover(), FakeSession([_UNREADABLE]), trail=trail,
                 unreadable_ceiling_s=30.0, stuck_alarm_s=10.0,
                 idle_ceiling_s=600.0)
    assert walk.run() == aw.EXIT_STATUS_UNREACHABLE
    assert not [row for row in trail.rows if row["event"] == "stuck"]
    unreachable = trail.error("status_unreachable")
    assert unreachable["unreadable_s"] >= 30.0
    assert "--hostname" in unreachable["detail"]


def test_one_good_read_clears_an_unreadable_run():
    """A blip is absorbed; only an UNBROKEN run of them ends the walk."""
    class Flapping(FakeSession):
        def __init__(self) -> None:
            super().__init__([_IN_FLIGHT_QUIET])
            self.reads = 0

        def poll(self) -> aw.Poll:
            self.reads += 1
            # Two unreadable, one good, forever — never an unbroken ceiling.
            return _UNREADABLE if self.reads % 3 else _IN_FLIGHT_QUIET

    walk = _walk(FakeMover(), Flapping(), unreadable_ceiling_s=10.0,
                 stuck_alarm_s=600.0, idle_ceiling_s=30.0)
    assert walk.run() == aw.EXIT_IDLE_CEILING


def test_an_unreadable_first_poll_does_not_claim_a_session_is_open():
    """Two skip reasons, told apart: a read fact and the absence of one."""
    trail = _RecordingTrail()
    walk = _walk(FakeMover(), FakeSession([_UNREADABLE]), trail=trail,
                 unreadable_ceiling_s=10.0, expect_angles=(7,),
                 walk_staged=lambda: False)
    assert walk.run() == aw.EXIT_STATUS_UNREACHABLE
    assert trail.one("staged_check_skipped")["reason"] == "status_unreadable"


def test_nothing_ever_pending_is_a_failure_not_a_quiet_zero():
    assert _walk(FakeMover(), FakeSession([_QUIET]),
                 idle_ceiling_s=10.0).run() == aw.EXIT_IDLE_CEILING


def test_a_walk_that_ran_and_then_went_quiet_is_a_success():
    session = FakeSession([_pending(1, 7), _QUIET])
    assert _walk(FakeMover(), session, idle_ceiling_s=10.0).run() == aw.EXIT_OK
    assert session.released == [1]


def test_a_repeated_pending_is_served_once_and_still_terminates():
    """A hold the gate has not cleared yet is not progress, and not a new move."""
    session = FakeSession([_pending(4, 22), _pending(4, 22)])
    assert _walk(FakeMover(), session, idle_ceiling_s=10.0).run() == aw.EXIT_OK
    assert session.released == [4]


def test_a_retake_of_the_same_index_re_gates():
    """The gate keys holds on ``(index, attempt)``; so does the served set."""
    session = FakeSession([_pending(4, 22, 1), _pending(4, 22, 2), _QUIET])
    assert _walk(FakeMover(), session, idle_ceiling_s=10.0).run() == aw.EXIT_OK
    assert session.released == [4, 4]


# --------------------------------------------------------------------------- #
# a walk ends when its session does
# --------------------------------------------------------------------------- #


def test_a_session_that_finished_is_never_diagnosed_as_a_stuck_capture():
    """The 2026-08-21 jts3 incident, as a test.

    A stage-1 measure round served every planned position, the session closed
    itself cleanly, and the walk -- which read only the PRESENCE of the capture
    block, never its terminal ``status`` -- called that "in flight with nothing
    pending" and fired the stuck alarm 300 s later. rc 6 on a round that had
    measured everything it was asked for, and the laptop runner skipped banking.
    """
    trail = _RecordingTrail()
    session = FakeSession([_pending(1, 7), _COMPLETE])
    walk = _walk(FakeMover(), session, trail=trail,
                 stuck_alarm_s=10.0, idle_ceiling_s=600.0)
    assert walk.run() == aw.EXIT_OK
    assert not [row for row in trail.rows if row["event"] == "stuck"]
    ended = trail.one("session_ended")
    assert ended["status"] == "complete" and ended["released"] == 1


def test_a_finished_session_ends_the_walk_rather_than_the_idle_ceiling():
    """Waiting the ceiling out would reach the same rc twenty minutes later.

    ``DEFAULT_IDLE_CEILING_S`` is 1200 s and the round runner is blocked on this
    process, so "the session said it is done" has to be read, not waited out.
    """
    clock = FakeClock()
    started = clock.now()
    session = FakeSession([_pending(1, 7), _COMPLETE])
    walk = _walk(FakeMover(), session, clock=clock, idle_ceiling_s=1200.0)
    assert walk.run() == aw.EXIT_OK
    # The settle (30 s) + the park settle (10 s) + a poll or two -- nowhere near
    # the ceiling this used to have to reach.
    assert clock.now() - started < 120.0


def test_a_stopped_session_is_not_a_clean_zero():
    """Stop measured fewer positions than the plan named; a zero would bank it."""
    trail = _RecordingTrail()
    session = FakeSession([_pending(1, 7), _STOPPED])
    walk = _walk(FakeMover(), session, trail=trail, idle_ceiling_s=600.0)
    assert walk.run() == aw.EXIT_SESSION_STOPPED
    assert trail.error("session_ended")["status"] == "stopped"


def test_a_session_that_ends_parks_the_arm_like_every_other_exit():
    mover = FakeMover()
    assert _walk(mover, FakeSession([_pending(1, 7), _COMPLETE]),
                 idle_ceiling_s=600.0).run() == aw.EXIT_OK
    assert mover.moves == [7, 0]


@pytest.mark.parametrize("residue", [_COMPLETE, _STOPPED, _failed("last round")])
def test_the_previous_rounds_outcome_never_ends_a_fresh_walk(residue):
    """The wizard keeps ONE capture slot, and a walk is launched BEFORE its
    session opens -- so the first polls of round N+1 read round N's terminal
    block. Ending on it would report the previous round's verdict as this
    walk's, and after the first round of a night nothing would ever measure."""
    class ResidueThenLive:
        def __init__(self) -> None:
            self._polls = 0
            self.released: list[int] = []

        def poll(self) -> aw.Poll:
            self._polls += 1
            # Two polls of the previous round's block, then this walk's own
            # session opens and asks for a position.
            return residue if self._polls <= 2 else _pending(1, 7)

        def release(self, index: int) -> tuple[int, str]:
            self.released.append(index)
            return 200, '{"ok": true}'

        def complete(self) -> tuple[int, str]:
            return 200, '{"ok": true}'

    session = ResidueThenLive()
    # ``--complete-after`` only so the walk has an end; what is under test is
    # that it reached the position at all instead of ending on the residue.
    walk = _walk(FakeMover(), session, idle_ceiling_s=600.0, complete_after=1)
    assert walk.run() == aw.EXIT_OK
    assert session.released == [1]


def test_residue_does_not_skip_the_staged_walk_check_either():
    """``in_flight`` gates that check too: a terminal block read as a live
    session made the one pre-motion evidence unanswerable, so a walk the
    session would refuse got moved for anyway."""
    trail = _RecordingTrail()
    walk = _walk(FakeMover(), FakeSession([_COMPLETE]), trail=trail,
                 expect_angles=(7,), walk_staged=lambda: False)
    assert walk.run() == aw.EXIT_WALK_NOT_STAGED
    assert not [r for r in trail.rows if r["event"] == "staged_check_skipped"]


def test_a_session_that_finished_without_the_stated_angles_still_fails():
    """The end-of-session arm hands the last word to ``_final_code``."""
    session = FakeSession([_pending(1, 7), _COMPLETE])
    walk = _walk(FakeMover(), session, idle_ceiling_s=600.0,
                 expect_angles=(7, -7))
    assert walk.run() == aw.EXIT_WALK_NOT_TAKEN


def test_ending_on_the_sessions_own_close_never_posts_a_second_one():
    """The two closes are exclusive, and neither can double-fire.

    ``--complete-after`` is how a WALK closes a wired stage's held set, and it
    returns the moment its POST is accepted. A session that closed ITSELF needs
    no second close -- the slot dropped the completion signal with the gate, so
    a POST could only 409 -- and the terminal block repeats to every later poll,
    so reading it more than once has to stay a no-op. Here the walk is still
    four releases short of its ``--complete-after`` when the session ends.
    """
    session = FakeSession([_pending(1, 7), _COMPLETE])
    walk = _walk(FakeMover(), session, idle_ceiling_s=600.0, complete_after=5)
    assert walk.run() == aw.EXIT_OK
    assert session.completes == 0
    assert session.released == [1]


def test_a_failed_move_stops_the_walk():
    session = FakeSession([_pending(1, 7), _QUIET])
    assert _walk(FakeMover(move_ok=False), session).run() == aw.EXIT_MOVE_FAILED
    assert session.released == []


@pytest.mark.parametrize("status,body", [
    (409, '{"error": "measurement 5 is waiting, not 4"}'),
    (403, "forbidden"),
    (400, '{"error": "index must be an integer"}'),
    (0, "URLError: connection refused"),
])
def test_a_release_the_session_did_not_accept_stops_the_walk(status, body):
    """No capture began, so nothing may be counted as measured.

    Left ungated, a 409 (the gate moved on), a 403 (bad CSRF pair), a 400, or a
    POST that never arrived would each mark the position served, satisfy
    ``--expect-angles``, advance ``--complete-after``, and exit 0 on a walk that
    measured nothing.
    """
    trail = _RecordingTrail()
    session = FakeSession([_pending(4, 22), _QUIET], release=(status, body))
    walk = _walk(FakeMover(), session, trail=trail, expect_angles=(22,))
    assert walk.run() == aw.EXIT_RELEASE_REJECTED
    rejected = trail.error("release_rejected")
    assert rejected["http"] == status and rejected["index"] == 4
    assert not [row for row in trail.rows if row["event"] == "released"]
    assert walk.summary() == "no positions were released"


def test_a_rejected_release_never_satisfies_an_expectation():
    """The stated angle stays unserved, so the run cannot end 0 either way."""
    session = FakeSession([_pending(1, 7), _QUIET], release=(409, "conflict"))
    walk = _walk(FakeMover(), session, expect_angles=(7,))
    assert walk.run() == aw.EXIT_RELEASE_REJECTED


def test_a_rejected_release_never_advances_the_wired_completion():
    session = FakeSession([_pending(1, 7), _QUIET], release=(403, "forbidden"))
    assert _walk(FakeMover(), session,
                 complete_after=1).run() == aw.EXIT_RELEASE_REJECTED
    assert session.completes == 0


def test_complete_after_closes_the_wired_stage():
    session = FakeSession([_pending(1, 7), _pending(2, -7), _QUIET])
    assert _walk(FakeMover(), session, complete_after=2).run() == aw.EXIT_OK
    assert session.completes == 1 and session.released == [1, 2]


def test_a_complete_that_is_not_accepted_is_left_for_triage():
    session = FakeSession([_pending(1, 7), _QUIET], complete=(409, "no session"))
    assert _walk(FakeMover(), session,
                 complete_after=1).run() == aw.EXIT_COMPLETE_FAILED


def test_complete_after_must_count_at_least_one_release():
    with pytest.raises(aw.ArmWalkRefused):
        aw.WalkConfig(complete_after=0)


def test_every_exit_code_is_distinct_and_named():
    codes = [value for name, value in vars(aw).items() if name.startswith("EXIT_")
             and isinstance(value, int)]
    assert len(codes) == len(set(codes))
    assert set(codes) == set(aw.EXIT_NAMES)


# --------------------------------------------------------------------------- #
# the walk-taken checks
# --------------------------------------------------------------------------- #


def test_a_stated_walk_that_never_arrives_is_a_named_failure():
    """A session that refused the staged walk runs its ordinary, all-0 shape."""
    session = FakeSession([_pending(1, 0), _pending(2, 0), _QUIET])
    trail = _RecordingTrail()
    walk = _walk(FakeMover(), session, trail=trail, idle_ceiling_s=10.0,
                 expect_angles=(7, -7))
    assert walk.run() == aw.EXIT_WALK_NOT_TAKEN
    assert trail.error("walk_not_taken")["missing_angles"] == "+7,-7"


def test_a_stated_walk_that_arrives_is_a_success():
    session = FakeSession([_pending(1, 0), _pending(2, 7), _pending(3, -7), _QUIET])
    walk = _walk(FakeMover(), session, idle_ceiling_s=10.0, expect_angles=(7, -7))
    assert walk.run() == aw.EXIT_OK


def test_nothing_staged_and_no_session_refuses_before_it_moves():
    mover = FakeMover()
    walk = _walk(mover, FakeSession([_QUIET]), expect_angles=(7,),
                 walk_staged=lambda: False)
    assert walk.run() == aw.EXIT_WALK_NOT_STAGED
    assert mover.moves == [0]  # only the park


def test_a_session_already_in_flight_skips_the_unanswerable_check():
    trail = _RecordingTrail()
    session = FakeSession([_pending(1, 7), _QUIET])
    walk = _walk(FakeMover(), session, trail=trail, idle_ceiling_s=10.0,
                 expect_angles=(7,), walk_staged=lambda: False)
    assert walk.run() == aw.EXIT_OK
    assert trail.one("staged_check_skipped")["reason"] == "session_in_flight"


def test_no_expectation_means_no_staged_check():
    walk = _walk(FakeMover(), FakeSession([_QUIET]), idle_ceiling_s=10.0,
                 walk_staged=lambda: False)
    assert walk.run() == aw.EXIT_IDLE_CEILING


# --------------------------------------------------------------------------- #
# reading the envelope
# --------------------------------------------------------------------------- #


def test_a_pending_is_read_off_the_capture_block():
    poll = aw.poll_from_status({"capture": {"status": "running", "position_pending": {
        "index": 3, "attempt": 2, "degrees": -22, "role": "offax"}}})
    assert poll.pending == aw.Pending(3, 2, -22, "offax")
    assert poll.in_flight and poll.failed_error is None


@pytest.mark.parametrize("pending", [
    None, {}, {"index": 3}, {"degrees": 7}, {"index": "x", "degrees": 7},
    "not-a-mapping",
])
def test_a_hold_that_cannot_be_parsed_is_never_moved_for(pending):
    assert aw.pending_from_capture({"position_pending": pending}) is None


def test_a_failed_capture_carries_its_own_error():
    poll = aw.poll_from_status({"capture": {"status": "failed", "error": "boom"}})
    assert poll.failed_error == "boom"
    bare = aw.poll_from_status({"capture": {"status": "failed"}})
    assert bare.failed_error and "no error" in bare.failed_error


def test_an_unreadable_status_is_in_flight_not_finished():
    """Otherwise a transient read failure would trip the idle ceiling mid-walk."""
    poll = aw.poll_from_status(None)
    assert poll.in_flight and poll.pending is None and poll.failed_error is None


def test_no_capture_block_is_no_session():
    assert aw.poll_from_status({"capture": None}) == aw.Poll(None, False, None)


@pytest.mark.parametrize("status", sorted(aw.SESSION_ENDED_STATUSES))
def test_a_terminal_capture_block_is_not_a_live_session(status):
    """The block the status page renders the OUTCOME from is not a session.

    Reading its presence as "in flight" is what turned a finished round into
    :data:`EXIT_STUCK` (2026-08-21, jts3).
    """
    poll = aw.poll_from_status({"capture": {"status": status}})
    assert poll.ended == status
    assert not poll.in_flight and poll.readable


@pytest.mark.parametrize("status", [
    "starting", "awaiting_capture", "committing", "finishing", "stopping",
    "a_status_this_module_has_never_heard_of", "",
])
def test_anything_not_terminal_reads_as_in_flight(status):
    """The fail-safe direction: a status this module cannot name keeps the walk
    waiting. Ending early strands a round; one more poll costs one poll."""
    poll = aw.poll_from_status({"capture": {"status": status}})
    assert poll.in_flight and not poll.ended


def test_the_walks_terminal_statuses_are_the_wizards_own_three():
    """One vocabulary, two modules -- pinned rather than trusted.

    ``jasper.web.correction_setup`` owns the capture slot: ``_run_capture``
    writes every terminal status a session can end on, and
    ``_CAPTURE_IN_FLIGHT_STATUSES`` names the rest. A fourth terminal arm added
    there without a matching name here would leave the walk reading a finished
    session as live again -- which is exactly the defect this pins.
    """
    from jasper.web import correction_setup

    terminal = set(re.findall(
        r'"status": "([a-z_]+)"',
        inspect.getsource(correction_setup._run_capture),
    ))
    assert terminal == set(aw.SESSION_ENDED_STATUSES)
    assert aw.SESSION_ENDED_STATUSES.isdisjoint(
        correction_setup._CAPTURE_IN_FLIGHT_STATUSES
    )


# --------------------------------------------------------------------------- #
# the HTTP session
# --------------------------------------------------------------------------- #


class _FakeOpener:
    """``status`` other than 200 raises the ``HTTPError`` urllib really raises."""

    def __init__(self, pages, *, status: int = 200) -> None:
        self.pages = pages
        self.status = status
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.status != 200:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "refused", {},
                io.BytesIO(b"this JTS management page is only available from "
                           b"the speaker's trusted LAN hostname"),
            )
        body = ""
        for path, page in self.pages.items():
            if request.full_url.endswith(path):
                body = page
                break
        return _FakeResponse(body)


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _loopback(pages, *, status: int = 200):
    opener = _FakeOpener(pages, status=status)
    return aw.LoopbackSession(host_header="jts3.local", opener=opener), opener


def test_every_request_carries_the_speakers_own_host():
    session, opener = _loopback({
        wc.STATUS_PATH: json.dumps({"capture": {"status": "running"}}),
        wc.CSRF_PAGE_PATH: '<meta name="jts-csrf" content="tok123">',
        aw.POSITION_READY_PATH: '{"ok": true}',
    })
    session.poll()
    session.release(4)
    assert opener.requests
    for request in opener.requests:
        assert request.get_header("Host") == "jts3.local"


def test_a_release_carries_the_double_submit_token_and_the_index():
    session, opener = _loopback({
        wc.CSRF_PAGE_PATH: '<meta name="jts-csrf" content="tok123">',
        aw.POSITION_READY_PATH: '{"ok": true}',
    })
    assert session.release(4) == (200, '{"ok": true}')
    post = opener.requests[-1]
    assert post.get_method() == "POST"
    assert post.get_header("X-csrf-token") == "tok123"
    assert json.loads(post.data.decode()) == {"index": 4}
    # The token is minted once and reused, not re-fetched per POST.
    session.complete()
    assert sum(1 for r in opener.requests
               if r.full_url.endswith(wc.CSRF_PAGE_PATH)) == 1


def test_a_failed_first_mint_is_retried_rather_than_cached_forever():
    """Caching ``""`` would 403 every POST for the rest of the run, silently."""
    pages = {wc.CSRF_PAGE_PATH: "<html>the wizard is still starting</html>",
             aw.POSITION_READY_PATH: '{"ok": true}'}
    session, opener = _loopback(pages)
    session.release(1)
    assert opener.requests[-1].get_header("X-csrf-token") == ""

    pages[wc.CSRF_PAGE_PATH] = '<meta name="jts-csrf" content="tok456">'
    session.release(1)
    assert opener.requests[-1].get_header("X-csrf-token") == "tok456"
    # …and once a REAL token is in hand it is cached, not re-fetched.
    minted_before = sum(1 for r in opener.requests
                        if r.full_url.endswith(wc.CSRF_PAGE_PATH))
    session.complete()
    assert sum(1 for r in opener.requests
               if r.full_url.endswith(wc.CSRF_PAGE_PATH)) == minted_before


def test_a_status_endpoint_that_refuses_reads_as_unreadable():
    """A 403 — the shape a wrong --hostname produces — is not "no session"."""
    session, _ = _loopback({}, status=403)
    poll = session.poll()
    assert poll.readable is False and poll.in_flight is True
    assert poll.pending is None and poll.failed_error is None


def test_the_endpoints_are_the_products_own():
    from jasper.web.correction_crossover_v2 import POSITION_READY_ENDPOINT

    assert aw.POSITION_READY_PATH == POSITION_READY_ENDPOINT
    assert wc.STATUS_PATH == "/correction/crossover/status"
    assert aw.COMPLETE_PATH == "/correction/crossover/v2/complete"


def test_a_status_read_that_is_not_json_is_unreadable_not_finished():
    session, _ = _loopback({wc.STATUS_PATH: "<html>nope"})
    assert session.poll().in_flight is True


# --------------------------------------------------------------------------- #
# the trail
# --------------------------------------------------------------------------- #


def test_the_trail_file_reconstructs_the_walk(tmp_path):
    path = tmp_path / "walk.jsonl"
    trail = aw.Trail(path)
    session = FakeSession([_pending(1, 7), _QUIET])
    assert _walk(FakeMover(), session, trail=trail,
                 idle_ceiling_s=10.0).run() == aw.EXIT_OK
    trail.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    events = [row["event"] for row in rows]
    assert events[0] == "up" and events[-1] == "parked"
    released = next(row for row in rows if row["event"] == "released")
    assert released["index"] == 1 and released["degrees"] == 7
    assert all("t" in row for row in rows)


def test_the_trail_levels_separate_progress_from_trouble():
    """Otherwise ``_RecordingTrail.error`` would be a vacuous assertion."""
    trail = _RecordingTrail()
    session = FakeSession([_pending(1, 7), _QUIET])
    assert _walk(FakeMover(), session, trail=trail,
                 idle_ceiling_s=10.0).run() == aw.EXIT_OK
    for event in ("up", "pending", "moved", "released", "parked"):
        assert trail.one(event)["level"] == logging.INFO

    failing = _RecordingTrail()
    _walk(FakeMover(move_ok=False), FakeSession([_pending(1, 7), _QUIET]),
          trail=failing).run()
    failing.error("move_failed")


def test_the_summary_names_every_release():
    session = FakeSession([_pending(1, 7), _pending(2, -22), _QUIET])
    walk = _walk(FakeMover(), session, idle_ceiling_s=10.0)
    walk.run()
    assert walk.summary() == "2 releases: #1@+7deg/30s, #2@-22deg/30s"
    assert _walk(FakeMover(), FakeSession([_QUIET]),
                 idle_ceiling_s=10.0).summary() == "no positions were released"


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #


def test_the_cli_refuses_a_settle_under_the_floor(capsys):
    assert cli.main([
        "--attest-rig-clear", "--hostname", "jts3.local", "--settle-s", "5",
    ]) == aw.EXIT_REFUSED
    assert "floor" in capsys.readouterr().err


@pytest.mark.parametrize("raw,expected", [
    ("7,-7,22,-22", (7, -7, 22, -22)),
    ("0", (0,)),
    (" 7 , -7 ", (7, -7)),
    ("", ()),
])
def test_the_cli_parses_whole_degrees(raw, expected):
    assert cli._angles(raw) == expected


@pytest.mark.parametrize("raw", ["7.5", "0.4", "seven"])
def test_the_cli_never_rounds_an_angle(raw):
    with pytest.raises(Exception):
        cli._angles(raw)


def test_the_console_script_is_installed():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'jasper-arm-walk = "jasper.cli.arm_walk:main"' in text


# --------------------------------------------------------------------------- #
# helpers used above
# --------------------------------------------------------------------------- #


class _RecordingTrail(aw.Trail):
    """A trail that keeps its rows in memory instead of a file.

    ``level`` is recorded like any other field -- it is the severity an operator
    greps the journal by, so a failure that logged at INFO would be a real
    defect this double used to be blind to. :meth:`error` is how a failure
    assertion states that expectation in one place.
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.rows: list[dict] = []

    def emit(self, action: str, *, level: int = logging.INFO, **fields) -> None:
        self.rows.append({"event": action, "level": level, **fields})

    def one(self, action: str) -> dict:
        matches = [row for row in self.rows if row["event"] == action]
        assert len(matches) == 1, f"{action}: {len(matches)} rows"
        return matches[0]

    def error(self, action: str) -> dict:
        """The one row for ``action``, which must have logged at ERROR."""
        row = self.one(action)
        assert row["level"] == logging.ERROR, (
            f"{action} logged at {logging.getLevelName(row['level'])}, "
            "so it would not surface as a failure in the journal"
        )
        return row


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode
