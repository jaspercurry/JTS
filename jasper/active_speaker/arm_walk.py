# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drive the crossover-v2 position gate with the lab turntable arm.

The session publishes ``relay.position_pending`` on ``GET
/correction/crossover/status`` and holds every begin until something POSTs
``/correction/crossover/v2/position-ready``; the turntable adapter moves the
microphone. This module is the loop that joins them, kept as a contract
rather than a throwaway per-campaign script.

The browser/human flow is untouched and stays first-class; this is the
opt-in lab-arm path only (nothing starts it, no unit, no timer). The loop,
one turn per held capture::

    poll status -> power preflight -> move -> settle -> position-ready

A walk ends when its session does: the same poll carries ``relay.status``,
and the three terminal values (:data:`SESSION_ENDED_STATUSES`) are the cue
to stop (``complete`` a clean finish, ``stopped`` -> :data:`EXIT_SESSION_STOPPED`,
``failed`` -> :data:`EXIT_SESSION_FAILED`). A terminal status only counts
once this walk has seen its session LIVE, since the wizard keeps one relay
slot and a walk launches before its session opens.

Safety invariants, each a test in ``tests/test_arm_walk.py``: power is
re-read before every WALK move (any sign VOIDS attestation ->
:data:`EXIT_POWER_VOID`, park not re-checked -- the adapter's own preflight
still blocks a brownout move); the envelope is clamped to
:data:`~jasper.active_speaker.angle_capture.ARM_ENVELOPE_DEG` before any
move; the arm parks at 0 deg and is verified (``abs(offset) <
PARK_TOLERANCE_DEG``) on EVERY exit path, including :data:`PARK_ON_SIGNALS`;
``set-zero`` is never invoked (:data:`_TOOL_SUBCOMMANDS` is the complete
adapter-verb set); the settle never goes under :data:`SETTLE_FLOOR_S`,
validated at configure time and MEASURED per release.

Attestation, not a nanny: the adapter requires two ``--confirm-*`` flags per
move; the operator answers ONCE with ``--attest-rig-clear``, mapped to both
flags on every move. A power sign is the one thing that voids it, since
that is when the saved zero may no longer be the acoustic axis.
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from jasper.log_event import log_event

from .angle_capture import ARM_ENVELOPE_DEG
from .wizard_client import STATUS_PATH, WizardClient

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# the numbers
# --------------------------------------------------------------------------- #

#: The owner's stated minimum settle. A configured settle under this is
#: refused; a MEASURED settle under it ends the walk (the capture is not the
#: pose it says).
SETTLE_FLOOR_S = 10.0

#: The settle proven on hardware (2026-08-19): the value the graded night
#: ran, spending 5% of the gate's 600 s hold.
DEFAULT_SETTLE_S = 30.0

#: Settle after the park move, before reading the offset back.
PARK_SETTLE_S = 10.0

#: How close to zero a parked arm must read. The adapter reports hundredths.
PARK_TOLERANCE_DEG = 0.05

DEFAULT_POLL_S = 3.0

#: Nothing pending for this long: the session finished, never opened, or
#: does not need the arm.
DEFAULT_IDLE_CEILING_S = 1200.0

#: In flight, nothing pending or released for this long: the signature of a
#: REJECTED capture, which renders a "Try again" no unattended run can press
#: (#2506). Named rather than waited out.
DEFAULT_STUCK_ALARM_S = 300.0

#: Status endpoint unreadable for this long with no good read. A blip is
#: absorbed; a wrong ``--hostname``, a 403, or a stopped wizard never clears.
DEFAULT_UNREADABLE_CEILING_S = 60.0

#: The ``relay.status`` values meaning the session is OVER. Every other
#: value, INCLUDING an unrecognised one, reads as in flight (ending early
#: strands a round; waiting costs one poll). Pinned against the wizard's own
#: in-flight set by ``tests/test_arm_walk.py``.
SESSION_ENDED_STATUSES = frozenset({"complete", "stopped", "failed"})

#: Where ``install.sh`` puts the turntable adapter on a speaker.
DEFAULT_TOOL_PATH = Path("/opt/jasper/experiments/usb-turntable/jts_turntable.py")

#: Every adapter verb this module may emit. ``set-zero`` is deliberately
#: absent: no automated walk may redefine the saved acoustic-axis zero.
_TOOL_SUBCOMMANDS = frozenset({"power", "position", "offset"})


# --------------------------------------------------------------------------- #
# exit codes -- part of the contract, because the caller is a script
# --------------------------------------------------------------------------- #

EXIT_OK = 0
#: A power sign voided the attestation. The arm is parked; fix the supply.
EXIT_POWER_VOID = 3
#: A move was refused or did not complete. The arm is parked.
EXIT_MOVE_FAILED = 4
#: ``--complete-after`` fired and the session did not accept it.
EXIT_COMPLETE_FAILED = 5
#: In flight, nothing pending, no progress -- a capture is awaiting a human.
EXIT_STUCK = 6
#: The session itself reported ``status="failed"``; its error is on the line.
EXIT_SESSION_FAILED = 7
#: Nothing ever asked the arm to move before the idle ceiling.
EXIT_IDLE_CEILING = 8
#: ``--expect-angles`` was given, no session is in flight, and no walk is staged.
EXIT_WALK_NOT_STAGED = 9
#: The run ended without serving every expected angle -- refused at take
#: time, the session silently degraded to its ordinary shape.
EXIT_WALK_NOT_TAKEN = 10
#: The settle actually taken was under :data:`SETTLE_FLOOR_S`.
EXIT_SETTLE_FLOOR = 11
#: The run was configured in a way this module refuses before it moves anything.
EXIT_REFUSED = 12
#: The session did not accept a position-ready: the microphone is at the
#: angle named, but nothing was released, so the walk stops rather than
#: count a capture that never began.
EXIT_RELEASE_REJECTED = 13
#: The status endpoint could not be read for a whole
#: :data:`DEFAULT_UNREADABLE_CEILING_S`. Usually the wrong ``--hostname``.
EXIT_STATUS_UNREACHABLE = 14
#: The session this walk was serving was STOPPED. Not :data:`EXIT_OK` (a
#: stopped session measured fewer positions than planned); not
#: :data:`EXIT_SESSION_FAILED` (nothing failed, no error to report).
EXIT_SESSION_STOPPED = 15

#: Signals that stop a walk THROUGH its park rather than around it. SIGHUP is
#: the remote spelling (an ssh transport going away hangs up the process
#: group). :func:`jasper.cli.arm_walk._install_park_on_signals` installs
#: them and exits ``SIGNAL_EXIT_BASE + signum``.
PARK_ON_SIGNALS: tuple[signal.Signals, ...] = (
    signal.SIGHUP, signal.SIGINT, signal.SIGTERM,
)
SIGNAL_EXIT_BASE = 128
#: Ended by a signal, park RUN on the way out. Derived, not spelled
#: 129/130/143, so these and the handler's ``sys.exit`` can never disagree.
EXIT_HANGUP_PARKED = SIGNAL_EXIT_BASE + int(signal.SIGHUP)
EXIT_INTERRUPTED_PARKED = SIGNAL_EXIT_BASE + int(signal.SIGINT)
EXIT_TERMINATED_PARKED = SIGNAL_EXIT_BASE + int(signal.SIGTERM)

EXIT_NAMES: Mapping[int, str] = {
    EXIT_OK: "ok",
    EXIT_POWER_VOID: "power_void",
    EXIT_MOVE_FAILED: "move_failed",
    EXIT_COMPLETE_FAILED: "complete_failed",
    EXIT_STUCK: "stuck",
    EXIT_SESSION_FAILED: "session_failed",
    EXIT_IDLE_CEILING: "idle_ceiling",
    EXIT_WALK_NOT_STAGED: "walk_not_staged",
    EXIT_WALK_NOT_TAKEN: "walk_not_taken",
    EXIT_SETTLE_FLOOR: "settle_floor",
    EXIT_REFUSED: "refused",
    EXIT_RELEASE_REJECTED: "release_rejected",
    EXIT_STATUS_UNREACHABLE: "status_unreachable",
    EXIT_SESSION_STOPPED: "session_stopped",
    EXIT_HANGUP_PARKED: "hangup_parked",
    EXIT_INTERRUPTED_PARKED: "interrupted_parked",
    EXIT_TERMINATED_PARKED: "terminated_parked",
}


class ArmWalkRefused(Exception):
    """The run may not start as configured. Nothing has moved."""


# --------------------------------------------------------------------------- #
# the seams
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PowerVerdict:
    """One power reading. ``clean`` is the only thing that admits a motion."""

    clean: bool
    detail: str


@dataclass(frozen=True)
class Pending:
    """What the session is waiting for -- ``relay.position_pending``, typed."""

    index: int
    attempt: int
    degrees: int
    role: str = ""

    @property
    def key(self) -> tuple[int, int]:
        """The gate's own identity for a hold: ``(index, attempt)``."""
        return (self.index, self.attempt)


@dataclass(frozen=True)
class Poll:
    """One read of the envelope.

    ``failed_error`` is the session's OWN error string on ``relay.status == "failed"``,
    turning a dead session into an immediate exit instead of a
    :data:`DEFAULT_STUCK_ALARM_S` wait; which SESSION it is attributed to is the walk's
    own decision (:meth:`ArmWalk._session_ended`).

    ``readable`` is whether the envelope was READ at all, separate from ``in_flight``
    since conflating them would blame issue #2506 for a URL typo. An unreadable poll
    still reports ``in_flight=True``; :data:`WalkConfig.unreadable_ceiling_s` ends an
    unbroken run of them.

    ``ended`` is the TERMINAL ``relay.status`` a finished session published
    (:data:`SESSION_ENDED_STATUSES`), or empty -- a third state, not the negation of
    ``in_flight``: no relay block at all means no session has opened yet.
    """

    pending: Pending | None
    in_flight: bool
    failed_error: str | None = None
    readable: bool = True
    ended: str = ""


class Mover(Protocol):
    """The positioner. Every method is a real hardware round trip."""

    def power(self) -> PowerVerdict: ...

    def move_to(self, degrees: int) -> bool:
        """Move to a signed absolute angle. ``True`` only on a confirmed ok."""

    def offset_deg(self) -> float | None:
        """The signed readback from the saved zero, or ``None`` if unreadable. Callers consume the
        MAGNITUDE only (see the module docstring).
        """


class Session(Protocol):
    """The measurement session, over HTTP."""

    def poll(self) -> Poll: ...

    def release(self, index: int) -> tuple[int, str]:
        """POST position-ready. Returns ``(http_status, body)``."""

    def complete(self) -> tuple[int, str]:
        """POST the wired all-spots-measured signal. Returns ``(status, body)``."""


# --------------------------------------------------------------------------- #
# the turntable adapter, as a Mover
# --------------------------------------------------------------------------- #


def parse_power(payload: Mapping[str, Any]) -> PowerVerdict:
    """The adapter's ``power`` JSON -> a verdict, failing closed. Stricter than the adapter's
    own motion gate (CURRENT flags only, right for an attended human): this walk is
    unattended, so a since-boot flag also voids it, since the saved zero may not have
    survived the sag.
    """
    status = payload.get("power", {}).get("status", {})
    if not isinstance(status, Mapping) or not status.get("available"):
        detail = "power status unavailable"
        if isinstance(status, Mapping) and status.get("detail"):
            detail = str(status["detail"])
        return PowerVerdict(False, detail)
    current = tuple(str(f) for f in status.get("current_flags") or ())
    history = tuple(str(f) for f in status.get("history_flags") or ())
    if current or history:
        return PowerVerdict(
            False,
            "power flags: current=[{}] since_boot=[{}]".format(
                ",".join(current) or "-", ",".join(history) or "-"
            ),
        )
    return PowerVerdict(True, str(status.get("raw") or "throttled=0x0"))


@dataclass
class TurntableMover:
    """The installed ``jts_turntable.py`` adapter, driven as a subprocess. Never an import:
    ``experiments/`` is not a dependency, and each command gets a clean serial session
    and its documented one-retry recovery.
    """

    tool_path: Path = DEFAULT_TOOL_PATH
    attest_rig_clear: bool = False
    timeout_s: float = 300.0
    python: str = field(default_factory=lambda: sys.executable)
    run: Callable[..., Any] = subprocess.run

    def _invoke(self, subcommand: str, *rest: str) -> tuple[int, Mapping[str, Any]]:
        if subcommand not in _TOOL_SUBCOMMANDS:
            raise AssertionError(f"not an allowed adapter verb: {subcommand!r}")
        argv = [self.python, str(self.tool_path), "--json", subcommand, *rest]
        try:
            proc = self.run(
                argv, capture_output=True, text=True, timeout=self.timeout_s
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            payload = json.loads(str(getattr(proc, "stdout", "") or ""))
        except ValueError:
            payload = {"ok": False, "error": "adapter did not answer JSON"}
        if not isinstance(payload, Mapping):
            payload = {"ok": False, "error": "adapter did not answer an object"}
        return int(getattr(proc, "returncode", 1)), payload

    def power(self) -> PowerVerdict:
        _, payload = self._invoke("power")
        return parse_power(payload)

    def move_to(self, degrees: int) -> bool:
        if not self.attest_rig_clear:
            # Unreachable via the CLI; kept since this class is directly
            # constructible and confirmations must never be forged by default.
            raise ArmWalkRefused("moving needs an explicit rig-clear attestation")
        _, payload = self._invoke(
            "position",
            str(int(degrees)),
            "--confirm-rig-clear",
            "--confirm-zero-valid",
        )
        return bool(payload.get("ok"))

    def offset_deg(self) -> float | None:
        _, payload = self._invoke("offset")
        result = payload.get("result")
        if not payload.get("ok") or not isinstance(result, Mapping):
            return None
        try:
            return float(result["offset_degrees"])
        except (KeyError, TypeError, ValueError):
            return None


# --------------------------------------------------------------------------- #
# the correction wizard, as a Session
# --------------------------------------------------------------------------- #

#: The position gate's two POSTs; transport and status read are
#: :mod:`jasper.active_speaker.wizard_client`.
POSITION_READY_PATH = "/correction/crossover/v2/position-ready"
COMPLETE_PATH = "/correction/crossover/v2/complete"



class LoopbackSession(WizardClient):
    """The position gate's three verbs, over the wizard reached on loopback. A
    :class:`WizardClient` pointed at ``127.0.0.1`` plus the :class:`Session` protocol,
    so the laptop round runner reaches the SAME wizard across the LAN through the same
    transport rather than a second copy of the Host/CSRF rules.
    """

    def poll(self) -> Poll:
        status, body = self.open(STATUS_PATH)
        if status != 200:
            logger.warning("status read returned %s", status)
            return poll_from_status(None)
        try:
            payload = json.loads(body)
        except ValueError:
            logger.warning("status was not JSON")
            return poll_from_status(None)
        return poll_from_status(payload if isinstance(payload, Mapping) else None)

    def release(self, index: int) -> tuple[int, str]:
        return self.post(POSITION_READY_PATH, {"index": int(index)})

    def complete(self) -> tuple[int, str]:
        return self.post(COMPLETE_PATH, {})


# --------------------------------------------------------------------------- #
# the trail
# --------------------------------------------------------------------------- #


class Trail:
    """One call, two surfaces: an ``event=arm_walk.*`` line and a JSONL row.

    Both carry the SAME fields from the same call site, so the journal and the
    trail can never disagree about a number, and a run is reconstructable
    mechanically from either.
    """

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time):
        self._handle = open(path, "a", buffering=1, encoding="utf-8") if path else None
        self._clock = clock

    def emit(self, action: str, *, level: int = logging.INFO, **fields: Any) -> None:
        log_event(logger, f"arm_walk.{action}", level=level, **fields)
        if self._handle is not None:
            row = {"t": round(self._clock(), 3), "event": action, **fields}
            self._handle.write(json.dumps(row, default=str) + "\n")

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


# --------------------------------------------------------------------------- #
# the walk
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WalkConfig:
    """Everything the loop's timing and expectations are stated in."""

    settle_s: float = DEFAULT_SETTLE_S
    poll_s: float = DEFAULT_POLL_S
    idle_ceiling_s: float = DEFAULT_IDLE_CEILING_S
    stuck_alarm_s: float = DEFAULT_STUCK_ALARM_S
    unreadable_ceiling_s: float = DEFAULT_UNREADABLE_CEILING_S
    complete_after: int | None = None
    #: The non-zero angles the staged walk contributes; a refused staged walk
    #: degrades to design-axis captures, so a stated angle never becoming
    #: pending is how this loop detects the silent degrade. Empty means
    #: "drive whatever the gate asks for".
    expect_angles: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.settle_s < SETTLE_FLOOR_S:
            raise ArmWalkRefused(
                f"settle {self.settle_s:.1f}s is under the {SETTLE_FLOOR_S:.0f}s "
                "floor a landed arm needs"
            )
        if self.poll_s <= 0:
            raise ArmWalkRefused("poll interval must be above zero")
        if self.complete_after is not None and self.complete_after < 1:
            raise ArmWalkRefused("--complete-after counts releases, so it is >= 1")
        outside = tuple(
            deg for deg in self.expect_angles if abs(deg) > ARM_ENVELOPE_DEG
        )
        if outside:
            raise ArmWalkRefused(
                f"the arm travels +/-{ARM_ENVELOPE_DEG} deg, so it cannot reach "
                + ", ".join(f"{deg:+d}" for deg in outside)
                + " deg"
            )


class ArmWalk:
    """One walk. Foreground, one run, parks whatever happens. Constructed with its three seams
    (``mover``, ``session``, clock/sleep) so the whole loop is exercised without a Pi, a
    turntable, or a socket.
    """

    def __init__(
        self,
        mover: Mover,
        session: Session,
        config: WalkConfig,
        *,
        trail: Trail | None = None,
        walk_staged: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._mover = mover
        self._session = session
        self._config = config
        self._trail = trail or Trail()
        self._walk_staged = walk_staged
        self._clock = clock
        self._sleep = sleep
        self._parked = False
        self._saw_session = False
        self._served: set[tuple[int, int]] = set()
        self._served_angles: set[int] = set()
        self._releases: list[tuple[int, int, float]] = []

    # -- the public entry point -------------------------------------------- #

    def run(self) -> int:
        """Walk, then park. The park runs on every path out of this method (a ``finally``, so it
        covers a clean return, any exception, or the CLI's SIGTERM ``SystemExit``). The
        aborted line reads the exception off ``sys.exc_info`` rather than binding it,
        keeping it on its way out.
        """
        code = EXIT_OK
        verdict_reached = False
        try:
            code = self._walk()
            verdict_reached = True
        finally:
            if not verdict_reached:
                exc = sys.exc_info()[1]
                self._trail.emit(
                    "aborted",
                    level=logging.ERROR,
                    error=f"{type(exc).__name__}: {exc}" if exc else "no verdict",
                )
            self._park()
        return self._final_code(code)

    # -- the loop ----------------------------------------------------------- #

    def _poll(self) -> Poll:
        """One read of the envelope, and the ``_saw_session`` latch. EVERY read goes through here;
        an UNREADABLE poll proves nothing about a session and never opens it (keeps a
        403 from being diagnosed as a stuck capture).
        """
        poll = self._session.poll()
        if poll.in_flight and poll.readable:
            self._saw_session = True
        return poll

    def _walk(self) -> int:
        cfg = self._config
        self._trail.emit(
            "up",
            settle_s=cfg.settle_s,
            settle_floor_s=SETTLE_FLOOR_S,
            envelope_deg=ARM_ENVELOPE_DEG,
            idle_ceiling_s=cfg.idle_ceiling_s,
            stuck_alarm_s=cfg.stuck_alarm_s,
            unreadable_ceiling_s=cfg.unreadable_ceiling_s,
            complete_after=cfg.complete_after,
            expect_angles=",".join(f"{a:+d}" for a in cfg.expect_angles) or "-",
        )
        verdict = self._mover.power()
        if not verdict.clean:
            self._trail.emit("power_void", level=logging.ERROR,
                             when="start", detail=verdict.detail)
            return EXIT_POWER_VOID
        self._trail.emit("start_offset", offset_deg=self._mover.offset_deg())

        first = self._poll()
        staged = self._staged_walk_check(first)
        if staged is not None:
            return staged

        now = self._clock()
        idle_since = last_progress = now
        unreadable_since: float | None = None if first.readable else now
        while True:
            poll = self._poll()
            if poll.ended and self._saw_session:
                return self._session_ended(poll)

            if poll.readable:
                unreadable_since = None
            else:
                if unreadable_since is None:
                    unreadable_since = self._clock()
                unreadable_for = self._clock() - unreadable_since
                if unreadable_for > self._config.unreadable_ceiling_s:
                    self._trail.emit(
                        "status_unreachable",
                        level=logging.ERROR,
                        unreadable_s=round(unreadable_for, 1),
                        released=len(self._served),
                        detail=(
                            "the status endpoint has not answered once -- check "
                            "--hostname and --base-url before suspecting the "
                            "session"
                        ),
                    )
                    return EXIT_STATUS_UNREACHABLE

            if poll.pending is not None:
                # An already-served hold falls through deliberately: not
                # progress, resets no clock, ends on the idle ceiling if it
                # somehow persists.
                if poll.pending.key not in self._served:
                    code = self._serve(poll.pending)
                    if code is not None:
                        return code
                    idle_since = last_progress = self._clock()
                    if self._complete_due():
                        return self._post_complete()
            elif (
                poll.readable
                and poll.in_flight
                and self._clock() - last_progress > self._config.stuck_alarm_s
            ):
                # ``readable`` is load-bearing: without it a 403 reaches the
                # operator as "a capture is awaiting a human", blaming #2506
                # for a typo.
                self._trail.emit(
                    "stuck",
                    level=logging.ERROR,
                    quiet_s=round(self._clock() - last_progress, 1),
                    released=len(self._served),
                    detail=(
                        "the session is in flight, nothing is pending, and "
                        "nothing has been released since -- either a rejected "
                        "capture is waiting on a human (#2506), or a session "
                        "went quiet without publishing a terminal status"
                    ),
                )
                return EXIT_STUCK

            if self._clock() - idle_since > self._config.idle_ceiling_s:
                self._trail.emit(
                    "idle_ceiling",
                    level=logging.INFO if self._served else logging.ERROR,
                    idle_s=round(self._clock() - idle_since, 1),
                    released=len(self._served),
                )
                return EXIT_OK if self._served else EXIT_IDLE_CEILING
            self._sleep(self._config.poll_s)

    def _staged_walk_check(self, first: Poll) -> int | None:
        """The one pre-motion evidence that a stated walk will actually run.

        A walk the session refuses at take time leaves NO trace on the envelope, so this
        asks the only thing answerable before anything moves: is one still staged,
        waiting for a session not yet open. Once a session IS in flight the question is
        unanswerable, and the loop falls back to :data:`EXIT_WALK_NOT_TAKEN` at the end.
        The two skip reasons are reported SEPARATELY: "already open" is a fact off the
        envelope, "could not be read" is the absence of any fact.
        """
        if not self._config.expect_angles or self._walk_staged is None:
            return None
        if not first.readable:
            self._trail.emit("staged_check_skipped", reason="status_unreadable")
            return None
        if first.in_flight:
            self._trail.emit("staged_check_skipped", reason="session_in_flight")
            return None
        if self._walk_staged():
            self._trail.emit("staged_check_ok")
            return None
        self._trail.emit(
            "walk_not_staged",
            level=logging.ERROR,
            expect_angles=",".join(f"{a:+d}" for a in self._config.expect_angles),
        )
        return EXIT_WALK_NOT_STAGED

    def _serve(self, pending: Pending) -> int | None:
        """Move, settle, release. ``None`` means the walk continues."""
        self._trail.emit(
            "pending",
            index=pending.index,
            attempt=pending.attempt,
            degrees=pending.degrees,
            role=pending.role or "-",
        )
        if abs(pending.degrees) > ARM_ENVELOPE_DEG:
            self._trail.emit("move_refused", level=logging.ERROR,
                             degrees=pending.degrees,
                             envelope_deg=ARM_ENVELOPE_DEG, reason="envelope")
            return EXIT_MOVE_FAILED
        verdict = self._mover.power()
        if not verdict.clean:
            self._trail.emit("power_void", level=logging.ERROR,
                             when="before_move", degrees=pending.degrees,
                             detail=verdict.detail)
            return EXIT_POWER_VOID
        if not self._mover.move_to(pending.degrees):
            self._trail.emit("move_failed", level=logging.ERROR,
                             index=pending.index, degrees=pending.degrees)
            return EXIT_MOVE_FAILED
        moved_at = self._clock()
        self._trail.emit("moved", index=pending.index, degrees=pending.degrees,
                         settle_s=self._config.settle_s)
        self._sleep(self._config.settle_s)
        settled = self._clock() - moved_at
        if settled < SETTLE_FLOOR_S:
            self._trail.emit("settle_floor", level=logging.ERROR,
                             index=pending.index, settled_s=round(settled, 1),
                             floor_s=SETTLE_FLOOR_S)
            return EXIT_SETTLE_FLOOR
        status, body = self._session.release(pending.index)
        if status != 200:
            # A release is a REQUEST the session may refuse (409/403/400/0);
            # none of those began a capture, so counting it as served would
            # satisfy --expect-angles/--complete-after on nothing measured.
            self._trail.emit(
                "release_rejected",
                level=logging.ERROR,
                index=pending.index,
                attempt=pending.attempt,
                degrees=pending.degrees,
                http=status,
                body=body[:300],
            )
            return EXIT_RELEASE_REJECTED
        self._trail.emit(
            "released",
            index=pending.index,
            attempt=pending.attempt,
            degrees=pending.degrees,
            settled_s=round(settled, 1),
            http=status,
            body=body[:300],
        )
        self._served.add(pending.key)
        self._served_angles.add(pending.degrees)
        self._releases.append((pending.index, pending.degrees, settled))
        return None

    def _complete_due(self) -> bool:
        after = self._config.complete_after
        return after is not None and len(self._served) >= after

    def _post_complete(self) -> int:
        status, body = self._session.complete()
        self._trail.emit(
            "complete",
            level=logging.INFO if status == 200 else logging.ERROR,
            released=len(self._served),
            http=status,
            body=body[:300],
        )
        return EXIT_OK if status == 200 else EXIT_COMPLETE_FAILED

    def _session_ended(self, poll: Poll) -> int:
        """The session published a terminal status. Its verdict becomes ours.

        Reached only once ``_saw_session`` (a LIVE, readable poll was seen),
        load-bearing rather than defensive: the wizard keeps ONE relay slot with the
        FINISHED session's block in it, and a walk launches BEFORE its session opens, so
        round N+1's first polls read round N's terminal block without the latch.

        ``failed`` carries the session's OWN error; ``stopped`` is not :data:`EXIT_OK`
        (fewer positions measured than planned); ``complete`` returns cleanly, leaving
        the ``--expect-angles`` check to :meth:`_final_code`.
        """
        if poll.failed_error is not None:
            self._trail.emit("session_failed", level=logging.ERROR,
                             error=poll.failed_error)
            return EXIT_SESSION_FAILED
        stopped = poll.ended == "stopped"
        self._trail.emit(
            "session_ended",
            level=logging.ERROR if stopped else logging.INFO,
            status=poll.ended,
            released=len(self._served),
        )
        return EXIT_SESSION_STOPPED if stopped else EXIT_OK

    # -- the exits ---------------------------------------------------------- #

    def _final_code(self, code: int) -> int:
        """An otherwise-clean walk still fails if a stated angle never came. Checked after the
        park, so a degraded session leaves the arm home and the operator a named code,
        not a silent zero.
        """
        if code != EXIT_OK:
            return code
        missing = tuple(
            deg for deg in self._config.expect_angles
            if deg not in self._served_angles
        )
        if not missing:
            return EXIT_OK
        self._trail.emit(
            "walk_not_taken",
            level=logging.ERROR,
            missing_angles=",".join(f"{deg:+d}" for deg in missing),
            served_angles=",".join(f"{deg:+d}" for deg in sorted(self._served_angles))
            or "-",
        )
        return EXIT_WALK_NOT_TAKEN

    def _park(self) -> None:
        """Home the arm and verify the MAGNITUDE. Idempotent; never raises."""
        if self._parked:
            return
        self._parked = True
        try:
            moved = self._mover.move_to(0)
            self._sleep(PARK_SETTLE_S)
            offset = self._mover.offset_deg()
        except (ArmWalkRefused, OSError, RuntimeError, ValueError,
                subprocess.SubprocessError) as exc:
            # Reported, not raised: this runs in the walk's own `finally`, and
            # raising would replace the walk's verdict with the park's.
            self._trail.emit("parked", level=logging.ERROR, ok=False,
                             error=f"{type(exc).__name__}: {exc}")
            return
        if offset is None:
            self._trail.emit("parked", level=logging.ERROR, ok=False,
                             moved=moved, offset_deg="unreadable",
                             detail="park could not be verified -- check the rig")
            return
        # MAGNITUDE only: the command sign is the truth and the readback's is
        # negated upstream, so consuming the sign here would invent a disagreement.
        at_zero = abs(offset) < PARK_TOLERANCE_DEG
        self._trail.emit(
            "parked",
            level=logging.INFO if at_zero and moved else logging.ERROR,
            ok=bool(at_zero and moved),
            moved=moved,
            offset_deg=round(offset, 3),
            tolerance_deg=PARK_TOLERANCE_DEG,
            releases=len(self._releases),
        )

    # -- what the operator reads at the end --------------------------------- #

    def summary(self) -> str:
        if not self._releases:
            return "no positions were released"
        return f"{len(self._releases)} releases: " + ", ".join(
            f"#{index}@{deg:+d}deg/{settled:.0f}s"
            for index, deg, settled in self._releases
        )


def pending_from_relay(relay: Mapping[str, Any]) -> Pending | None:
    """``relay.position_pending`` -> :class:`Pending`, or ``None``.

    Absent, malformed, or missing its index all read the same way: nothing to
    serve. A hold this loop cannot parse is one it must not move for.
    """
    raw = relay.get("position_pending")
    if not isinstance(raw, Mapping) or "index" not in raw or "degrees" not in raw:
        return None
    try:
        return Pending(
            index=int(raw["index"]),
            attempt=int(raw.get("attempt", 1)),
            degrees=int(raw["degrees"]),
            role=str(raw.get("role") or ""),
        )
    except (TypeError, ValueError):
        return None


def poll_from_status(status: Mapping[str, Any] | None) -> Poll:
    """``GET /correction/crossover/status`` -> one :class:`Poll`.

    ``None`` is an UNREADABLE envelope: ``in_flight=True`` so a transient
    read failure is not mistaken for "session ended", ``readable=False`` so
    an unbroken run of them is named for what it is. A relay block is not by
    itself a live session -- the wizard keeps the FINISHED session's block
    in its one relay slot, so a terminal ``status`` must be read, not merely
    the block's presence (measured 2026-08-21: a closed jts3 round was
    reported stuck 300 s later because the walk had no way to see the close).
    """
    if status is None:
        return Poll(None, True, None, readable=False)
    relay = status.get("relay")
    if not isinstance(relay, Mapping):
        return Poll(None, False, None)
    state = str(relay.get("status") or "")
    failed = None
    if state == "failed":
        failed = str(relay.get("error") or "(the session supplied no error)")
    ended = state if state in SESSION_ENDED_STATUSES else ""
    return Poll(pending_from_relay(relay), not ended, failed, ended=ended)


def staged_walk_pending() -> bool:
    """Is an angle walk waiting for the next session to take it? Delegates to the spool's own
    predicate, imported lazily so this module stays importable and testable without the
    crossover flow behind it.
    """
    from .angle_capture_spool import staged_angle_request_pending

    return staged_angle_request_pending()


__all__: Sequence[str] = (
    "ARM_ENVELOPE_DEG",
    "COMPLETE_PATH",
    "POSITION_READY_PATH",
    "ArmWalk",
    "ArmWalkRefused",
    "LoopbackSession",
    "DEFAULT_IDLE_CEILING_S",
    "DEFAULT_POLL_S",
    "DEFAULT_SETTLE_S",
    "DEFAULT_STUCK_ALARM_S",
    "DEFAULT_TOOL_PATH",
    "EXIT_COMPLETE_FAILED",
    "EXIT_HANGUP_PARKED",
    "EXIT_IDLE_CEILING",
    "EXIT_INTERRUPTED_PARKED",
    "EXIT_MOVE_FAILED",
    "EXIT_NAMES",
    "EXIT_OK",
    "EXIT_POWER_VOID",
    "EXIT_REFUSED",
    "EXIT_SESSION_FAILED",
    "EXIT_SESSION_STOPPED",
    "EXIT_SETTLE_FLOOR",
    "EXIT_STUCK",
    "EXIT_TERMINATED_PARKED",
    "EXIT_WALK_NOT_STAGED",
    "EXIT_WALK_NOT_TAKEN",
    "PARK_ON_SIGNALS",
    "PARK_SETTLE_S",
    "PARK_TOLERANCE_DEG",
    "SESSION_ENDED_STATUSES",
    "SETTLE_FLOOR_S",
    "SIGNAL_EXIT_BASE",
    "Mover",
    "Pending",
    "Poll",
    "PowerVerdict",
    "Session",
    "Trail",
    "TurntableMover",
    "WalkConfig",
    "parse_power",
    "pending_from_relay",
    "poll_from_status",
    "staged_walk_pending",
)
