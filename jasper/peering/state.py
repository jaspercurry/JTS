"""Pure state machine for multi-device wake arbitration.

The state machine has zero I/O. It accepts timestamped events
(local wake, peer message received, timer tick) and emits a list
of Actions describing what the caller should do (broadcast a
WAKE message, send a CLAIM, ask voice to begin a session, etc.).

This split — pure state, side-effectful caller — makes the machine
testable with synthetic event sequences (see test_peering_state.py)
and lets us iterate on the policy without touching socket code.

States (one PeerState enum):

  IDLE        steady state; no in-flight wake. Ready to receive a
              local wake or see a peer take one.
  CANDIDATE   local wake just fired; collecting peer WAKE messages
              for `arb_window_ms`. Once the window closes (timer
              tick), apply the ranking function and transition to
              either WINNER or LOSER.
  WINNER      we won the arbitration. Caller should send CLAIM and
              tell voice to begin the turn. Transitions to ACTIVE
              when voice confirms the session opened.
  ACTIVE      a session is live. Send HEART every 1 s; on session
              end, send END and return to IDLE.
  SUPPRESSED  a foreign peer is active (we saw their CLAIM or
              SESSION_HEARTBEAT). Local wakes below the break
              threshold are ignored. Returns to IDLE when the
              foreign session ends or the heartbeat times out.

Design notes:

  - All time is monotonic-seconds (float). The caller passes `now`
    on every event so the state machine never reads the clock
    itself — tests can drive time without monkey-patching.
  - The state machine doesn't know about Python asyncio. It returns
    Actions; the daemon code translates them into multicast sends,
    UDS commands, and timer scheduling.
  - There is no leader election. There is no consensus protocol.
    Each peer runs this same state machine independently against
    the same multicast stream and reaches the same outcome.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .rank import WakeReport, rank

logger = logging.getLogger(__name__)


class PeerState(str, Enum):
    IDLE = "idle"
    CANDIDATE = "candidate"
    WINNER = "winner"
    ACTIVE = "active"
    SUPPRESSED = "suppressed"


# ---------- Actions returned to caller ----------

@dataclass(frozen=True)
class BroadcastWake:
    """Caller should multicast a WAKE message with these fields."""
    epoch: str
    report: WakeReport


@dataclass(frozen=True)
class BroadcastClaim:
    """Caller should multicast a CLAIM message (winner announcement)."""
    epoch: str


@dataclass(frozen=True)
class BroadcastHeartbeat:
    """Caller should multicast a HEART (winner is alive)."""
    epoch: str


@dataclass(frozen=True)
class BroadcastEnd:
    """Caller should multicast an END (session over)."""
    epoch: str
    reason: str


@dataclass(frozen=True)
class StartSession:
    """Caller should ask jasper-voice to begin a turn.

    The caller (peering UDS server) is responding to a `WAKE_PROPOSE`
    request from voice. This action means "respond with WIN to that
    pending request" — voice then proceeds with its existing
    begin-turn flow.
    """
    epoch: str


@dataclass(frozen=True)
class StandDown:
    """Caller should ask jasper-voice to abort the pending wake."""
    epoch: str


@dataclass(frozen=True)
class ScheduleTimer:
    """Caller should fire `on_timer(timer_id)` at `at_monotonic`."""
    timer_id: str
    at_monotonic: float


@dataclass(frozen=True)
class CancelTimer:
    """Caller should cancel a previously-scheduled timer."""
    timer_id: str


Action = (
    BroadcastWake | BroadcastClaim | BroadcastHeartbeat | BroadcastEnd
    | StartSession | StandDown
    | ScheduleTimer | CancelTimer
)


# ---------- Events accepted ----------

@dataclass(frozen=True)
class LocalWake:
    """jasper-voice fired wake locally; asking us to arbitrate."""
    score: float
    snr_db: float | None
    rms_dbfs: float | None
    can_serve: bool
    now: float          # monotonic seconds


@dataclass(frozen=True)
class PeerWake:
    """Received a WAKE message from a sibling peer."""
    epoch: str
    report: WakeReport
    now: float


@dataclass(frozen=True)
class PeerClaim:
    """Received a CLAIM message — sibling claims it won."""
    epoch: str
    peer_id: str
    now: float


@dataclass(frozen=True)
class PeerHeartbeat:
    """Received a HEART message — sibling is still active."""
    epoch: str
    peer_id: str
    now: float


@dataclass(frozen=True)
class PeerEnd:
    """Received an END message — sibling session is over."""
    epoch: str
    peer_id: str
    reason: str
    now: float


@dataclass(frozen=True)
class TimerFired:
    """Previously-scheduled timer fired."""
    timer_id: str
    now: float


@dataclass(frozen=True)
class VoiceSessionStarted:
    """jasper-voice notified us its turn opened."""
    epoch: str
    now: float


@dataclass(frozen=True)
class VoiceSessionEnded:
    """jasper-voice notified us its turn closed."""
    epoch: str
    reason: str
    now: float


Event = (
    LocalWake | PeerWake | PeerClaim | PeerHeartbeat | PeerEnd
    | TimerFired | VoiceSessionStarted | VoiceSessionEnded
)


# ---------- Configuration the state machine reads ----------

@dataclass(frozen=True)
class StateMachineParams:
    peer_id: str
    primary: bool
    arb_window_sec: float
    break_threshold: float
    heartbeat_interval_sec: float = 1.0
    heartbeat_timeout_sec: float = 2.0


# ---------- Timer IDs used internally ----------

TIMER_ARB_WINDOW = "arb_window"
TIMER_HEARTBEAT_SEND = "heart_send"
TIMER_HEARTBEAT_TIMEOUT = "heart_timeout"


# ---------- The state machine ----------

@dataclass
class _Epoch:
    """Mutable per-arbitration scratchpad."""
    epoch: str
    reports: dict[str, WakeReport] = field(default_factory=dict)
    started_at: float = 0.0


class PeeringStateMachine:
    """Pure event-driven state machine for one peer.

    `handle(event)` returns a list of Actions the caller must execute.
    `state` is the current PeerState. All other internal state is
    transient and meant to be opaque to callers.
    """

    def __init__(self, params: StateMachineParams) -> None:
        self._p = params
        self._state = PeerState.IDLE
        # The epoch currently being arbitrated (if state == CANDIDATE)
        # or held during WINNER/ACTIVE. None in IDLE/SUPPRESSED unless
        # we're tracking a foreign session.
        self._epoch: Optional[_Epoch] = None
        # When SUPPRESSED, remember the foreign session's epoch + peer
        # so we know which heartbeats are relevant.
        self._foreign_epoch: Optional[str] = None
        self._foreign_peer: Optional[str] = None
        self._foreign_last_heartbeat: float = 0.0

    # ---- public observers ----

    @property
    def state(self) -> PeerState:
        return self._state

    @property
    def current_epoch(self) -> Optional[str]:
        if self._epoch is not None:
            return self._epoch.epoch
        return self._foreign_epoch

    # ---- main dispatcher ----

    def handle(self, event: Event) -> list[Action]:
        if isinstance(event, LocalWake):
            return self._on_local_wake(event)
        if isinstance(event, PeerWake):
            return self._on_peer_wake(event)
        if isinstance(event, PeerClaim):
            return self._on_peer_claim(event)
        if isinstance(event, PeerHeartbeat):
            return self._on_peer_heartbeat(event)
        if isinstance(event, PeerEnd):
            return self._on_peer_end(event)
        if isinstance(event, TimerFired):
            return self._on_timer(event)
        if isinstance(event, VoiceSessionStarted):
            return self._on_voice_started(event)
        if isinstance(event, VoiceSessionEnded):
            return self._on_voice_ended(event)
        return []

    # ---- event handlers ----

    def _on_local_wake(self, ev: LocalWake) -> list[Action]:
        # In SUPPRESSED, only a strong wake breaks the suppression.
        # Below the break threshold, swallow silently (the user can
        # walk to the active speaker or wait for the session to end).
        if self._state is PeerState.SUPPRESSED:
            if ev.score < self._p.break_threshold:
                logger.debug(
                    "local wake (score=%.2f) ignored: suppressed by peer %s",
                    ev.score, self._foreign_peer,
                )
                return []
            # Strong wake — break suppression, enter arbitration.
            logger.info(
                "event=peering.suppression.broken score=%.2f threshold=%.2f",
                ev.score, self._p.break_threshold,
            )
            self._clear_foreign_session()

        # IDLE or freshly-broken SUPPRESSED → start new arbitration.
        if self._state in (PeerState.IDLE, PeerState.SUPPRESSED):
            return self._begin_candidate(ev)

        # CANDIDATE (already arbitrating a previous wake): ignore the
        # spurious second wake from voice. The detector's own
        # refractory period should prevent this in practice; this is
        # belt-and-braces.
        # WINNER / ACTIVE: also ignore — we're already handling a
        # session.
        return []

    def _on_peer_wake(self, ev: PeerWake) -> list[Action]:
        # Peer is reporting their own wake. Three cases:
        #
        # 1. We're IDLE and haven't seen this epoch yet → enter
        #    CANDIDATE on the foreign epoch (so our local detector
        #    can join when it fires). We don't bid ourselves yet.
        # 2. We're CANDIDATE: collect their report into the current
        #    arbitration.
        # 3. We're WINNER/ACTIVE/SUPPRESSED: ignore — out of band.
        if self._state is PeerState.IDLE:
            # Adopt foreign epoch as a candidate; we have no local
            # report yet but we're tracking. Schedule the arb-window
            # timer based on when we saw THIS peer's WAKE — best-effort
            # approximation of when the foreign peer started arbitration.
            self._state = PeerState.CANDIDATE
            self._epoch = _Epoch(epoch=ev.epoch, started_at=ev.now)
            self._epoch.reports[ev.report.peer_id] = ev.report
            return [ScheduleTimer(
                timer_id=TIMER_ARB_WINDOW,
                at_monotonic=ev.now + self._p.arb_window_sec,
            )]

        if self._state is PeerState.CANDIDATE and self._epoch is not None:
            if ev.epoch == self._epoch.epoch:
                self._epoch.reports[ev.report.peer_id] = ev.report
            # Different epoch — duplicate utterance race. Two epochs
            # were created near-simultaneously; gossip-dedup by epoch
            # value (lower UUID wins). We side with the smaller epoch
            # to converge.
            elif ev.epoch < self._epoch.epoch:
                logger.debug(
                    "epoch race: replacing %s with smaller %s",
                    self._epoch.epoch, ev.epoch,
                )
                old_reports = self._epoch.reports
                self._epoch = _Epoch(epoch=ev.epoch, started_at=ev.now)
                self._epoch.reports[ev.report.peer_id] = ev.report
                # Carry forward any reports from the abandoned epoch
                # under the new key — they were heard for the same
                # utterance.
                for r in old_reports.values():
                    self._epoch.reports.setdefault(r.peer_id, r)
            return []

        return []

    def _on_peer_claim(self, ev: PeerClaim) -> list[Action]:
        # Some peer is claiming the session for epoch ev.epoch. If we
        # were arbitrating that epoch, concede; either way, enter
        # SUPPRESSED unless it's our own claim (loopback).
        if ev.peer_id == self._p.peer_id:
            return []  # our own multicast loopback — already in WINNER

        actions: list[Action] = []

        # If we were in WINNER for the same epoch, we lost a race —
        # the other peer also concluded it won. Concede.
        if self._state is PeerState.WINNER and self._epoch and self._epoch.epoch == ev.epoch:
            logger.info(
                "event=peering.winner.conceding to=%s epoch=%s",
                ev.peer_id, ev.epoch,
            )
            actions.append(StandDown(epoch=ev.epoch))
            actions.append(CancelTimer(timer_id=TIMER_HEARTBEAT_SEND))
            self._reset_epoch()

        # Cancel any pending arb-window timer + tell voice to stand
        # down. Without the StandDown, the pending ARBITRATE RPC would
        # hang until its hard timeout.
        if self._state is PeerState.CANDIDATE:
            if self._epoch and self._epoch.epoch == ev.epoch:
                actions.append(StandDown(epoch=ev.epoch))
            actions.append(CancelTimer(timer_id=TIMER_ARB_WINDOW))
            self._reset_epoch()

        # Enter SUPPRESSED for the foreign session
        self._state = PeerState.SUPPRESSED
        self._foreign_epoch = ev.epoch
        self._foreign_peer = ev.peer_id
        self._foreign_last_heartbeat = ev.now
        actions.append(ScheduleTimer(
            timer_id=TIMER_HEARTBEAT_TIMEOUT,
            at_monotonic=ev.now + self._p.heartbeat_timeout_sec,
        ))
        return actions

    def _on_peer_heartbeat(self, ev: PeerHeartbeat) -> list[Action]:
        # Only relevant in SUPPRESSED for the matching foreign session.
        if (
            self._state is PeerState.SUPPRESSED
            and ev.epoch == self._foreign_epoch
            and ev.peer_id == self._foreign_peer
        ):
            self._foreign_last_heartbeat = ev.now
            # Reschedule the timeout for `heartbeat_timeout_sec` from
            # the most recent heartbeat.
            return [ScheduleTimer(
                timer_id=TIMER_HEARTBEAT_TIMEOUT,
                at_monotonic=ev.now + self._p.heartbeat_timeout_sec,
            )]
        return []

    def _on_peer_end(self, ev: PeerEnd) -> list[Action]:
        if (
            self._state is PeerState.SUPPRESSED
            and ev.epoch == self._foreign_epoch
        ):
            logger.info(
                "event=peering.foreign.ended peer=%s reason=%s",
                ev.peer_id, ev.reason,
            )
            self._clear_foreign_session()
            self._state = PeerState.IDLE
            return [CancelTimer(timer_id=TIMER_HEARTBEAT_TIMEOUT)]
        return []

    def _on_timer(self, ev: TimerFired) -> list[Action]:
        if ev.timer_id == TIMER_ARB_WINDOW:
            return self._close_arbitration(ev.now)
        if ev.timer_id == TIMER_HEARTBEAT_SEND:
            return self._send_heartbeat_and_reschedule(ev.now)
        if ev.timer_id == TIMER_HEARTBEAT_TIMEOUT:
            return self._on_heartbeat_timeout(ev.now)
        return []

    def _on_voice_started(self, ev: VoiceSessionStarted) -> list[Action]:
        # voice confirms it opened a session for our winning epoch.
        # Begin heartbeating.
        if self._state is PeerState.WINNER and self._epoch and self._epoch.epoch == ev.epoch:
            self._state = PeerState.ACTIVE
            return self._send_heartbeat_and_reschedule(ev.now)
        return []

    def _on_voice_ended(self, ev: VoiceSessionEnded) -> list[Action]:
        # Session ended cleanly. Broadcast END, cancel timers, return
        # to IDLE.
        if (
            self._state is PeerState.ACTIVE
            and self._epoch is not None
            and self._epoch.epoch == ev.epoch
        ):
            actions: list[Action] = [
                BroadcastEnd(epoch=ev.epoch, reason=ev.reason),
                CancelTimer(timer_id=TIMER_HEARTBEAT_SEND),
            ]
            self._reset_epoch()
            self._state = PeerState.IDLE
            return actions
        return []

    # ---- internal helpers ----

    def _begin_candidate(self, ev: LocalWake) -> list[Action]:
        """Local wake fired in IDLE — start a new arbitration."""
        import uuid as _uuid
        epoch = str(_uuid.uuid4())
        own_report = WakeReport(
            peer_id=self._p.peer_id,
            score=ev.score,
            snr_db=ev.snr_db,
            rms_dbfs=ev.rms_dbfs,
            primary=self._p.primary,
            can_serve=ev.can_serve,
        )
        self._epoch = _Epoch(epoch=epoch, started_at=ev.now)
        self._epoch.reports[self._p.peer_id] = own_report
        self._state = PeerState.CANDIDATE
        return [
            BroadcastWake(epoch=epoch, report=own_report),
            ScheduleTimer(
                timer_id=TIMER_ARB_WINDOW,
                at_monotonic=ev.now + self._p.arb_window_sec,
            ),
        ]

    def _close_arbitration(self, now: float) -> list[Action]:
        """Arb window elapsed — apply ranking, decide winner."""
        if self._state is not PeerState.CANDIDATE or self._epoch is None:
            return []
        reports = list(self._epoch.reports.values())
        if not reports:
            # No reports at all (we tracked a foreign epoch but the
            # peer never sent its WAKE, and we never local-waked). Just
            # back to IDLE.
            self._reset_epoch()
            self._state = PeerState.IDLE
            return []
        winner_id = rank(reports)
        epoch = self._epoch.epoch
        if winner_id == self._p.peer_id:
            self._state = PeerState.WINNER
            logger.info(
                "event=peering.wake.won epoch=%s reports=%d",
                epoch, len(reports),
            )
            return [
                BroadcastClaim(epoch=epoch),
                StartSession(epoch=epoch),
            ]
        else:
            # We lost. Find the winner's score for log clarity.
            winner = next(r for r in reports if r.peer_id == winner_id)
            my_score = self._epoch.reports.get(
                self._p.peer_id, None,
            )
            logger.info(
                "event=peering.wake.lost epoch=%s winner=%s "
                "winner_score=%.2f my_score=%s",
                epoch, winner_id, winner.score,
                f"{my_score.score:.2f}" if my_score else "n/a",
            )
            actions: list[Action] = [StandDown(epoch=epoch)]
            # Enter SUPPRESSED tracking the winner.
            self._reset_epoch()
            self._state = PeerState.SUPPRESSED
            self._foreign_epoch = epoch
            self._foreign_peer = winner_id
            self._foreign_last_heartbeat = now
            actions.append(ScheduleTimer(
                timer_id=TIMER_HEARTBEAT_TIMEOUT,
                at_monotonic=now + self._p.heartbeat_timeout_sec,
            ))
            return actions

    def _send_heartbeat_and_reschedule(self, now: float) -> list[Action]:
        if self._state is not PeerState.ACTIVE or self._epoch is None:
            return []
        return [
            BroadcastHeartbeat(epoch=self._epoch.epoch),
            ScheduleTimer(
                timer_id=TIMER_HEARTBEAT_SEND,
                at_monotonic=now + self._p.heartbeat_interval_sec,
            ),
        ]

    def _on_heartbeat_timeout(self, now: float) -> list[Action]:
        if self._state is not PeerState.SUPPRESSED:
            return []
        gap = now - self._foreign_last_heartbeat
        if gap >= self._p.heartbeat_timeout_sec:
            logger.info(
                "event=peering.session.heartbeat_missed peer=%s after_ms=%d",
                self._foreign_peer, int(gap * 1000),
            )
            self._clear_foreign_session()
            self._state = PeerState.IDLE
        # else: a heartbeat snuck in between scheduling and firing —
        # the receiver already rescheduled, do nothing here.
        return []

    def _reset_epoch(self) -> None:
        self._epoch = None

    def _clear_foreign_session(self) -> None:
        self._foreign_epoch = None
        self._foreign_peer = None
        self._foreign_last_heartbeat = 0.0
