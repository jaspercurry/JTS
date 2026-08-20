# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for ``jasper-arm-walk``.

Every safety sentence the harness's docstrings make is a test here: power before
every motion, the envelope clamp, the park-and-verify on each exit path, the
never-``set-zero`` rule, and the settle floor.
"""

from __future__ import annotations

import importlib.util
import json
import signal
import sys
from pathlib import Path

import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import arm_walk as aw
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
    """Time only moves when something sleeps, so a measured settle is exact."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
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


def _pending(index: int, degrees: int, attempt: int = 1) -> aw.Poll:
    return aw.Poll(aw.Pending(index, attempt, degrees, "onax"), True, None)


_QUIET = aw.Poll(None, False, None)
_IN_FLIGHT_QUIET = aw.Poll(None, True, None)


def _walk(mover, session, *, clock=None, trail=None, walk_staged=None, **cfg):
    clock = clock or FakeClock()
    config = aw.WalkConfig(**{
        "settle_s": 30.0, "poll_s": 3.0, "idle_ceiling_s": 60.0,
        "stuck_alarm_s": 300.0, **cfg,
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
# power is read before EVERY motion
# --------------------------------------------------------------------------- #


def test_power_is_read_before_every_move():
    mover = FakeMover()
    session = FakeSession([_pending(1, 0), _pending(2, 22), _QUIET])
    assert _walk(mover, session, idle_ceiling_s=10.0).run() == aw.EXIT_OK
    # start preflight, then one power read immediately before each served move;
    # the park's own move is the trailing one.
    order = [c[0] for c in mover.calls if c[0] in {"power", "move_to"}]
    assert order == ["power", "power", "move_to", "power", "move_to", "move_to"]
    assert mover.moves == [0, 22, 0]


def test_a_power_sign_at_the_start_moves_nothing():
    mover = FakeMover(power=[aw.PowerVerdict(False, "current=[under_voltage]")])
    session = FakeSession([_pending(1, 22)])
    assert _walk(mover, session).run() == aw.EXIT_POWER_VOID
    assert session.released == []
    # Parked anyway: the arm may be anywhere, and home is the only safe place.
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
    void = trail.one("power_void")
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
    refused = trail.one("move_refused")
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
    assert off_trail.one("parked")["ok"] is False


def test_an_unreadable_park_is_reported_not_assumed():
    mover = FakeMover(offset=None)
    trail = _RecordingTrail()
    _walk(mover, FakeSession([_QUIET]), trail=trail, idle_ceiling_s=10.0).run()
    parked = trail.one("parked")
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
    assert "RuntimeError" in trail.one("aborted")["error"]


def test_a_park_that_fails_is_reported_and_never_masks_the_verdict():
    """The park runs inside the walk's ``finally``; raising there would swap
    the walk's own outcome for the park's."""
    class Unparkable(FakeMover):
        def move_to(self, degrees: int) -> bool:
            super().move_to(degrees)
            raise OSError("the controller is gone")

    trail = _RecordingTrail()
    session = FakeSession([aw.Poll(None, True, "the session failed")])
    walk = _walk(Unparkable(), session, trail=trail)
    assert walk.run() == aw.EXIT_SESSION_FAILED
    assert "OSError" in trail.one("parked")["error"]


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
    previous = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        cli._install_park_on_signals()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as excinfo:
            handler(signal.SIGTERM, None)
        assert excinfo.value.code == 143
    finally:
        signal.signal(signal.SIGTERM, previous[0])
        signal.signal(signal.SIGINT, previous[1])


def test_the_park_runs_once():
    mover = FakeMover()
    walk = _walk(mover, FakeSession([_QUIET]), idle_ceiling_s=10.0)
    walk.run()
    walk._park()
    assert mover.moves == [0]


# --------------------------------------------------------------------------- #
# set-zero is unreachable
# --------------------------------------------------------------------------- #


def test_the_adapter_is_a_subprocess_and_never_an_import():
    """``experiments/`` is not a package product code may depend on.

    A subprocess is also what gives each command a clean serial session and the
    adapter's own documented one-retry recovery.
    """
    import ast

    tree = ast.parse((ROOT / "jasper/active_speaker/arm_walk.py").read_text())
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
    assert trail.one("settle_floor")["floor_s"] == aw.SETTLE_FLOOR_S


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
    session = FakeSession([aw.Poll(None, True, "the fit refused: level too low")])
    trail = _RecordingTrail()
    assert _walk(mover, session, trail=trail).run() == aw.EXIT_SESSION_FAILED
    assert trail.one("session_failed")["error"] == "the fit refused: level too low"
    assert mover.moves == [0]


def test_a_stuck_capture_is_named_rather_than_waited_out():
    trail = _RecordingTrail()
    walk = _walk(FakeMover(), FakeSession([_IN_FLIGHT_QUIET]), trail=trail,
                 stuck_alarm_s=10.0, idle_ceiling_s=600.0)
    assert walk.run() == aw.EXIT_STUCK
    assert trail.one("stuck")["released"] == 0


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


def test_a_failed_move_stops_the_walk():
    session = FakeSession([_pending(1, 7), _QUIET])
    assert _walk(FakeMover(move_ok=False), session).run() == aw.EXIT_MOVE_FAILED
    assert session.released == []


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
    assert trail.one("walk_not_taken")["missing_angles"] == "+7,-7"


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


def test_a_pending_is_read_off_the_relay_block():
    poll = aw.poll_from_status({"relay": {"status": "running", "position_pending": {
        "index": 3, "attempt": 2, "degrees": -22, "role": "offax"}}})
    assert poll.pending == aw.Pending(3, 2, -22, "offax")
    assert poll.in_flight and poll.failed_error is None


@pytest.mark.parametrize("pending", [
    None, {}, {"index": 3}, {"degrees": 7}, {"index": "x", "degrees": 7},
    "not-a-mapping",
])
def test_a_hold_that_cannot_be_parsed_is_never_moved_for(pending):
    assert aw.pending_from_relay({"position_pending": pending}) is None


def test_a_failed_relay_carries_its_own_error():
    poll = aw.poll_from_status({"relay": {"status": "failed", "error": "boom"}})
    assert poll.failed_error == "boom"
    bare = aw.poll_from_status({"relay": {"status": "failed"}})
    assert bare.failed_error and "no error" in bare.failed_error


def test_an_unreadable_status_is_in_flight_not_finished():
    """Otherwise a transient read failure would trip the idle ceiling mid-walk."""
    poll = aw.poll_from_status(None)
    assert poll.in_flight and poll.pending is None and poll.failed_error is None


def test_no_relay_block_is_no_session():
    assert aw.poll_from_status({"relay": None}) == aw.Poll(None, False, None)


# --------------------------------------------------------------------------- #
# the HTTP session
# --------------------------------------------------------------------------- #


class _FakeOpener:
    def __init__(self, pages) -> None:
        self.pages = pages
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        body = self.pages.get(request.selector if hasattr(request, "selector")
                              else request.full_url, "")
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


def _loopback(pages):
    opener = _FakeOpener(pages)
    return aw.LoopbackSession(host_header="jts3.local", opener=opener), opener


def test_every_request_carries_the_speakers_own_host():
    session, opener = _loopback({
        aw.STATUS_PATH: json.dumps({"relay": {"status": "running"}}),
        aw.CSRF_PAGE_PATH: '<meta name="jts-csrf" content="tok123">',
        aw.POSITION_READY_PATH: '{"ok": true}',
    })
    session.poll()
    session.release(4)
    assert opener.requests
    for request in opener.requests:
        assert request.get_header("Host") == "jts3.local"


def test_a_release_carries_the_double_submit_token_and_the_index():
    session, opener = _loopback({
        aw.CSRF_PAGE_PATH: '<meta name="jts-csrf" content="tok123">',
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
               if r.full_url.endswith(aw.CSRF_PAGE_PATH)) == 1


def test_the_endpoints_are_the_products_own():
    from jasper.web.correction_crossover_v2 import POSITION_READY_ENDPOINT

    assert aw.POSITION_READY_PATH == POSITION_READY_ENDPOINT
    assert aw.STATUS_PATH == "/correction/crossover/status"
    assert aw.COMPLETE_PATH == "/correction/crossover/v2/complete"


def test_a_status_read_that_is_not_json_is_unreadable_not_finished():
    session, _ = _loopback({aw.STATUS_PATH: "<html>nope"})
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
    """A trail that keeps its rows in memory instead of a file."""

    def __init__(self) -> None:
        super().__init__(None)
        self.rows: list[dict] = []

    def emit(self, action: str, *, level=None, **fields) -> None:
        self.rows.append({"event": action, **fields})

    def one(self, action: str) -> dict:
        matches = [row for row in self.rows if row["event"] == action]
        assert len(matches) == 1, f"{action}: {len(matches)} rows"
        return matches[0]


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode
