# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.peering.state.

The state machine is the heart of arbitration. These tests drive it
with synthetic event sequences (no I/O, no real timers) and assert on
the Actions it returns. Each test is a short story — wake fires here,
peer claims there, what does our machine do?
"""
from __future__ import annotations

from jasper.peering.rank import WakeReport
from jasper.peering.state import (
    TIMER_ARB_WINDOW,
    TIMER_HEARTBEAT_SEND,
    TIMER_HEARTBEAT_TIMEOUT,
    BroadcastClaim,
    BroadcastEnd,
    BroadcastHeartbeat,
    BroadcastWake,
    CancelTimer,
    LocalWake,
    PeerClaim,
    PeerEnd,
    PeerHeartbeat,
    PeerState,
    PeerWake,
    PeeringStateMachine,
    ScheduleTimer,
    StandDown,
    StartSession,
    StateMachineParams,
    TimerFired,
    VoiceSessionEnded,
    VoiceSessionStarted,
)


# ---------- single-peer (alone on the network) ----------


def test_local_wake_when_alone_wins_immediately():
    """No peers, local wake fires. After arb window, we win our own
    arbitration (we're the only candidate)."""
    m = _make("alice")
    actions = m.handle(LocalWake(score=0.8, snr_db=20.0, rms_dbfs=-20.0, can_serve=True, now=1.0))
    # First action: broadcast WAKE so peers (if any) can join.
    assert any(isinstance(a, BroadcastWake) for a in actions)
    # Second action: schedule arb-window timer.
    sched = [a for a in actions if isinstance(a, ScheduleTimer)]
    assert sched and sched[0].timer_id == TIMER_ARB_WINDOW
    assert sched[0].at_monotonic == 1.15  # 1.0 + 150ms
    assert m.state is PeerState.CANDIDATE

    # Arb window elapses with no peer reports — we win.
    win_actions = m.handle(TimerFired(timer_id=TIMER_ARB_WINDOW, now=1.15))
    assert any(isinstance(a, BroadcastClaim) for a in win_actions)
    assert any(isinstance(a, StartSession) for a in win_actions)
    assert m.state is PeerState.WINNER


# ---------- two peers, we lose ----------


def test_local_wake_loses_to_higher_confidence_peer():
    m = _make("alice")
    m.handle(LocalWake(score=0.6, snr_db=10.0, rms_dbfs=-25.0, can_serve=True, now=1.0))
    # Peer bob reports a stronger wake for the same arbitration.
    bob_report = WakeReport(
        peer_id="bob", score=0.9, snr_db=20.0, rms_dbfs=-15.0,
        primary=False, can_serve=True,
    )
    # bob's PeerWake arrives with a different epoch — but our state
    # machine's _begin_candidate created OUR epoch and we collect bob
    # only if epochs match. Simulate the gossip-merge path: bob's epoch
    # could be lower, in which case we adopt it. Easier path for this
    # test: drive bob's wake AFTER ours, with the same epoch.
    our_epoch = m.current_epoch
    assert our_epoch is not None
    m.handle(PeerWake(epoch=our_epoch, report=bob_report, now=1.05))

    # Window closes — we should lose.
    actions = m.handle(TimerFired(timer_id=TIMER_ARB_WINDOW, now=1.15))
    assert any(isinstance(a, StandDown) for a in actions)
    assert m.state is PeerState.SUPPRESSED


# ---------- foreign peer claims first ----------


def test_foreign_claim_puts_us_in_suppressed():
    """We were IDLE; sibling bob CLAIMs a session. We go SUPPRESSED
    and schedule a heartbeat-timeout."""
    m = _make("alice")
    actions = m.handle(PeerClaim(epoch="ep1", peer_id="bob", now=10.0))
    assert m.state is PeerState.SUPPRESSED
    timers = [a for a in actions if isinstance(a, ScheduleTimer)]
    assert any(t.timer_id == TIMER_HEARTBEAT_TIMEOUT for t in timers)


def test_heartbeat_resets_timeout():
    m = _make("alice")
    m.handle(PeerClaim(epoch="ep1", peer_id="bob", now=10.0))
    actions = m.handle(PeerHeartbeat(epoch="ep1", peer_id="bob", now=10.8))
    # Reschedules the timeout.
    sched = [a for a in actions if isinstance(a, ScheduleTimer)]
    assert sched and sched[0].timer_id == TIMER_HEARTBEAT_TIMEOUT
    # 10.8 + 2.0 = 12.8
    assert sched[0].at_monotonic == 12.8


def test_session_end_clears_suppression():
    m = _make("alice")
    m.handle(PeerClaim(epoch="ep1", peer_id="bob", now=10.0))
    actions = m.handle(PeerEnd(epoch="ep1", peer_id="bob", reason="silence", now=15.0))
    assert m.state is PeerState.IDLE
    assert any(isinstance(a, CancelTimer) for a in actions)


def test_heartbeat_timeout_clears_suppression():
    """If a winner crashes mid-session, heartbeats stop arriving. After
    the timeout elapses, peers un-suppress and the next wake-word
    starts fresh arbitration."""
    m = _make("alice")
    m.handle(PeerClaim(epoch="ep1", peer_id="bob", now=10.0))
    # No heartbeats arrive — timer fires after timeout.
    m.handle(TimerFired(timer_id=TIMER_HEARTBEAT_TIMEOUT, now=12.1))
    assert m.state is PeerState.IDLE


def test_local_wake_below_break_threshold_ignored_when_suppressed():
    """A weak local wake during a foreign session is ignored. The
    user can walk to the active speaker or wait for end-of-session."""
    m = _make("alice", break_threshold=0.85)
    m.handle(PeerClaim(epoch="ep1", peer_id="bob", now=10.0))
    # Weak wake below threshold.
    actions = m.handle(LocalWake(
        score=0.6, snr_db=10.0, rms_dbfs=-25.0, can_serve=True, now=12.0,
    ))
    assert actions == []
    assert m.state is PeerState.SUPPRESSED


def test_strong_local_wake_breaks_suppression():
    """A strong local wake (above break_threshold) ends suppression and
    enters arbitration. The user can grab a non-primary speaker by
    speaking the wake word directly to it."""
    m = _make("alice", break_threshold=0.85)
    m.handle(PeerClaim(epoch="ep1", peer_id="bob", now=10.0))
    actions = m.handle(LocalWake(
        score=0.92, snr_db=22.0, rms_dbfs=-12.0, can_serve=True, now=12.0,
    ))
    # Should leave SUPPRESSED and enter CANDIDATE.
    assert m.state is PeerState.CANDIDATE
    # And should broadcast its WAKE.
    assert any(isinstance(a, BroadcastWake) for a in actions)


# ---------- session lifecycle (we won) ----------


def test_winner_transitions_to_active_on_voice_started():
    m = _make("alice")
    m.handle(LocalWake(score=0.8, snr_db=20.0, rms_dbfs=-20.0, can_serve=True, now=1.0))
    m.handle(TimerFired(timer_id=TIMER_ARB_WINDOW, now=1.15))
    assert m.state is PeerState.WINNER
    epoch = m.current_epoch
    assert epoch is not None

    actions = m.handle(VoiceSessionStarted(epoch=epoch, now=1.2))
    assert m.state is PeerState.ACTIVE
    # Should send the first heartbeat + schedule the next.
    assert any(isinstance(a, BroadcastHeartbeat) for a in actions)
    assert any(
        isinstance(a, ScheduleTimer) and a.timer_id == TIMER_HEARTBEAT_SEND
        for a in actions
    )


def test_active_sends_heartbeats_on_timer():
    m = _make("alice")
    m.handle(LocalWake(score=0.8, snr_db=20.0, rms_dbfs=-20.0, can_serve=True, now=1.0))
    m.handle(TimerFired(timer_id=TIMER_ARB_WINDOW, now=1.15))
    m.handle(VoiceSessionStarted(epoch=m.current_epoch, now=1.2))

    # Timer fires for next heartbeat.
    actions = m.handle(TimerFired(timer_id=TIMER_HEARTBEAT_SEND, now=2.2))
    assert any(isinstance(a, BroadcastHeartbeat) for a in actions)
    # And reschedules.
    sched = [a for a in actions if isinstance(a, ScheduleTimer)]
    assert any(t.timer_id == TIMER_HEARTBEAT_SEND for t in sched)


def test_session_ended_broadcasts_end():
    m = _make("alice")
    m.handle(LocalWake(score=0.8, snr_db=20.0, rms_dbfs=-20.0, can_serve=True, now=1.0))
    m.handle(TimerFired(timer_id=TIMER_ARB_WINDOW, now=1.15))
    epoch = m.current_epoch
    m.handle(VoiceSessionStarted(epoch=epoch, now=1.2))
    actions = m.handle(VoiceSessionEnded(epoch=epoch, reason="silence", now=10.0))
    assert any(
        isinstance(a, BroadcastEnd) and a.epoch == epoch and a.reason == "silence"
        for a in actions
    )
    assert m.state is PeerState.IDLE


# ---------- gossip dedup of duplicate epochs ----------


def test_smaller_foreign_epoch_replaces_local():
    """If we and bob both start arbitration nearly simultaneously,
    each picks our own epoch. The smaller UUID wins to converge —
    we adopt bob's epoch."""
    m = _make("alice")
    # Force a known-large epoch by mocking uuid generation.
    m.handle(LocalWake(score=0.8, snr_db=20.0, rms_dbfs=-20.0, can_serve=True, now=1.0))
    our_epoch = m.current_epoch
    assert our_epoch is not None
    # Construct a smaller epoch — UUIDs sort lexicographically, so
    # "00000000-..." is smaller than any randomly-generated one.
    smaller_epoch = "00000000-0000-0000-0000-000000000000"
    bob_report = WakeReport(
        peer_id="bob", score=0.7, snr_db=15.0, rms_dbfs=-22.0,
        primary=False, can_serve=True,
    )
    m.handle(PeerWake(epoch=smaller_epoch, report=bob_report, now=1.05))
    # We should have adopted the smaller epoch.
    assert m.current_epoch == smaller_epoch


# ---------- ACTIVE session must not be clobbered (DA-0021) ----------


def test_active_session_not_clobbered_by_unrelated_foreign_claim():
    """DA-0021: a foreign CLAIM for an *unrelated* epoch — a different
    wake elsewhere in the house, since the multicast group is shared
    household-wide — must NOT tear down our live ACTIVE session.

    Before the ACTIVE guard, ANY foreign claim forced SUPPRESSED. That
    silently clobbered the live session's bookkeeping: the heartbeat
    timer stopped rescheduling and the real session's END broadcast was
    dropped by _on_voice_ended's ACTIVE guard.
    """
    m = _make("alice")
    m.handle(LocalWake(score=0.8, snr_db=20.0, rms_dbfs=-20.0, can_serve=True, now=1.0))
    m.handle(TimerFired(timer_id=TIMER_ARB_WINDOW, now=1.15))
    epoch = m.current_epoch
    m.handle(VoiceSessionStarted(epoch=epoch, now=1.2))
    assert m.state is PeerState.ACTIVE

    # A second person wakes a different speaker in another room; that
    # speaker's CLAIM reaches us over the shared multicast group.
    actions = m.handle(PeerClaim(epoch="ep-other-room", peer_id="bob", now=2.0))
    assert actions == []                       # no stand-down, no timers
    assert m.state is PeerState.ACTIVE         # our session is untouched
    assert m.current_epoch == epoch            # still tracking our own

    # Our session later ends on its own terms — the END broadcast that
    # the old clobber silently dropped must still fire.
    end_actions = m.handle(VoiceSessionEnded(epoch=epoch, reason="silence", now=10.0))
    assert any(
        isinstance(a, BroadcastEnd) and a.epoch == epoch for a in end_actions
    )
    assert m.state is PeerState.IDLE


def test_active_session_keeps_heartbeating_through_unrelated_claim():
    """Companion to the above: the heartbeat-send loop keeps running
    through an unrelated foreign claim. The old clobber-to-SUPPRESSED
    made the next heartbeat tick hit _send_heartbeat_and_reschedule's
    `state is not ACTIVE` guard and silently stop the loop."""
    m = _make("alice")
    m.handle(LocalWake(score=0.8, snr_db=20.0, rms_dbfs=-20.0, can_serve=True, now=1.0))
    m.handle(TimerFired(timer_id=TIMER_ARB_WINDOW, now=1.15))
    epoch = m.current_epoch
    m.handle(VoiceSessionStarted(epoch=epoch, now=1.2))

    m.handle(PeerClaim(epoch="ep-other-room", peer_id="bob", now=2.0))

    hb = m.handle(TimerFired(timer_id=TIMER_HEARTBEAT_SEND, now=2.2))
    assert any(isinstance(a, BroadcastHeartbeat) for a in hb)
    assert any(
        isinstance(a, ScheduleTimer) and a.timer_id == TIMER_HEARTBEAT_SEND
        for a in hb
    )


# ---------- README invariant: exactly one winner per wake event ----------


def test_wake_propagation_picks_exactly_one_winner_and_suppresses_rest():
    """Guard for README's 'peering picks exactly one winner per wake
    event so they [don't] all answer at once.'

    Scenario: one speaker physically hears the wake and multicasts its
    WAKE; the other N-1 speakers adopt the foreign epoch from the
    multicast (they never local-waked) and, when their arb window
    closes, rank the single report and concede. Exactly one peer
    reaches WINNER; every other peer is SUPPRESSED.

    This is the *single physical waker* case, which the state machine
    enforces. The concurrent *multi-waker* race (several speakers
    local-wake on the same utterance) is a separate, reported
    enforcement gap — see the audit report / items_skipped — so it is
    deliberately NOT asserted here as green.
    """
    peers = {pid: _make(pid) for pid in ("alice", "bob", "carol", "dave")}
    t = 1.0

    # alice hears the wake and broadcasts it.
    acts = peers["alice"].handle(
        LocalWake(score=0.82, snr_db=20.0, rms_dbfs=-20.0, can_serve=True, now=t)
    )
    wake = next(a for a in acts if isinstance(a, BroadcastWake))

    # The other speakers see alice's WAKE over multicast while IDLE and
    # adopt the foreign epoch (no local report of their own).
    for pid in ("bob", "carol", "dave"):
        peers[pid].handle(PeerWake(epoch=wake.epoch, report=wake.report, now=t + 0.01))

    # Every arb window closes.
    claims: list[tuple[str, str]] = []
    for pid, m in peers.items():
        for a in m.handle(TimerFired(timer_id=TIMER_ARB_WINDOW, now=t + 0.15)):
            if isinstance(a, BroadcastClaim):
                claims.append((pid, a.epoch))

    # Exactly one peer claimed, and it is the one that heard the wake.
    assert claims == [("alice", wake.epoch)]

    # The winner's CLAIM reaches everyone (an IDLE peer that never saw
    # the WAKE would suppress on it too).
    for pid, m in peers.items():
        if pid != "alice":
            m.handle(PeerClaim(epoch=wake.epoch, peer_id="alice", now=t + 0.16))

    winners = [
        pid for pid, m in peers.items()
        if m.state in (PeerState.WINNER, PeerState.ACTIVE)
    ]
    suppressed = [pid for pid, m in peers.items() if m.state is PeerState.SUPPRESSED]
    assert winners == ["alice"]
    assert sorted(suppressed) == ["bob", "carol", "dave"]


# ---------- helpers ----------


def _make(
    peer_id: str,
    *,
    primary: bool = False,
    arb_window_sec: float = 0.15,
    break_threshold: float = 0.85,
) -> PeeringStateMachine:
    return PeeringStateMachine(StateMachineParams(
        peer_id=peer_id,
        primary=primary,
        arb_window_sec=arb_window_sec,
        break_threshold=break_threshold,
    ))
