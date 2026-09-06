# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jasper.voice.output_gate import (
        AssistantOutputEpisode,
        AssistantOutputGate,
        AssistantOutputKind,
    )


async def test_turn_preempts_stale_proactive_before_claiming_output() -> None:
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    proactive = await gate.begin_if_idle("proactive")
    assert proactive is not None

    turn_task = asyncio.create_task(gate.begin_turn())
    await asyncio.sleep(0)

    assert gate.active_kind == "proactive"
    assert not gate.is_current(proactive)
    assert not turn_task.done()

    await gate.end(proactive)
    turn = await asyncio.wait_for(turn_task, timeout=1.0)

    assert turn.kind == "turn"
    assert gate.active_kind == "turn"

    await gate.end_turn(turn)
    assert gate.active_kind is None


async def test_non_turn_episode_only_starts_when_idle() -> None:
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    turn = await gate.begin_turn()

    assert await gate.begin_if_idle("admin") is None

    await gate.end_turn(turn)
    admin = await gate.begin_if_idle("admin")
    assert admin is not None
    assert admin.kind == "admin"


# `wait_idle` — the bounded idle-wait the measurement window drains on
# (issue #1898). See tests/test_voice_daemon_measurement_inflight.py.


async def test_wait_idle_returns_true_immediately_when_idle() -> None:
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()

    # A zero budget still succeeds: an idle gate is never waited on, so
    # the caller pays nothing on the common path.
    assert await gate.wait_idle(0.0) is True


async def test_wait_idle_returns_true_once_the_episode_ends() -> None:
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    episode = await gate.begin_if_idle("proactive")
    assert episode is not None

    waiter = asyncio.create_task(gate.wait_idle(5.0))
    await asyncio.sleep(0)
    assert not waiter.done()

    await gate.end(episode)
    assert await asyncio.wait_for(waiter, timeout=1.0) is True


async def test_wait_idle_returns_false_when_the_bound_expires() -> None:
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    episode = await gate.begin_if_idle("admin")
    assert episode is not None

    assert await gate.wait_idle(0.01) is False

    # The waiter is a pure observer: the episode still owns output.
    assert gate.is_current(episode)


async def test_wait_idle_rechecks_after_each_wake() -> None:
    """`asyncio.Event` wakes every waiter the instant it is set, so a
    waiter can be resumed by an episode that ended and only be scheduled
    after the next one has begun. `wait_idle` must re-read the gate, not
    trust the wake."""
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    first = await gate.begin_if_idle("proactive")
    assert first is not None

    waiter = asyncio.create_task(gate.wait_idle(5.0))
    await asyncio.sleep(0)
    assert not waiter.done()

    # End then immediately re-take, with no await in between: the waiter
    # has been signalled but has not run yet.
    await gate.end(first)
    second = await gate.begin_if_idle("admin")
    assert second is not None

    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done()

    await gate.end(second)
    assert await asyncio.wait_for(waiter, timeout=1.0) is True
async def test_paused_admission_refuses_every_non_turn_output_kind() -> None:
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    assert await gate.pause_admission() is True
    assert await gate.pause_admission() is False

    for kind in ("proactive", "admin", "feedback"):
        assert await gate.begin_if_idle(kind) is None

    assert gate.admission_paused
    assert await gate.resume_admission() is True
    assert await gate.resume_admission() is False


async def test_turn_admission_waits_until_pause_is_resumed() -> None:
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    await gate.pause_admission()

    turn_task = asyncio.create_task(gate.begin_turn())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not turn_task.done()

    await gate.resume_admission()
    turn = await asyncio.wait_for(turn_task, timeout=1.0)
    assert turn.kind == "turn"


async def test_paused_gate_boundedly_drains_only_the_preexisting_episode() -> None:
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    episode = await gate.begin_if_idle("admin")
    assert episode is not None

    await gate.pause_admission()
    assert await gate.begin_if_idle("proactive") is None
    assert await gate.drain_paused(0.01) is False

    await gate.end(episode)
    assert await gate.drain_paused(0.0) is True
    assert await gate.begin_if_idle("feedback") is None
    await gate.resume_admission()
    assert await gate.begin_if_idle("feedback") is not None


async def _hand_over_against_a_turn_queued_on_the_lock(
    gate: AssistantOutputGate,
) -> tuple[
    AssistantOutputEpisode,
    AssistantOutputEpisode | None,
    asyncio.Task[AssistantOutputEpisode],
]:
    """Run the handover with a `begin_turn` waiter already queued on the
    gate's own lock, and hand back what each of the three got.

    Nothing yields between an end and a begin while the lock is free, so an
    uncontended caller cannot tell one lock hold from two — which is why a
    test that does not contend the lock passes against both. A third task
    holds the lock, the handover and the waiter queue behind it in that
    order, and the lock's FIFO decides the rest: one hold keeps the waiter
    out until the cue owns output, two holds hand it the open gate in
    between and the sound that had to be heard is skipped (NN-6).
    """
    turn = await gate.begin_turn()

    held = asyncio.Event()
    release_holder = asyncio.Event()

    async def _hold_the_lock() -> None:
        async with gate._lock:
            held.set()
            await asyncio.wait_for(release_holder.wait(), timeout=5.0)

    holder = asyncio.create_task(_hold_the_lock())
    try:
        await asyncio.wait_for(held.wait(), timeout=1.0)
        # One step each is enough to reach — and park on — the lock.
        handover = asyncio.create_task(
            gate.hand_over_if_current(turn, "admin"),
        )
        await asyncio.sleep(0)
        waiter = asyncio.create_task(gate.begin_turn())
        await asyncio.sleep(0)

        release_holder.set()
        cue = await asyncio.wait_for(handover, timeout=1.0)
    finally:
        release_holder.set()
        await asyncio.wait_for(holder, timeout=1.0)
    return turn, cue, waiter


async def test_the_handover_beats_a_turn_waiter_queued_before_it() -> None:
    """A sound that MUST be heard cannot end its blocker and then ask for
    the gate: `end` releases the lock, and a `begin_turn` queued on it owns
    output before the ask lands — the sound is skipped (NN-6). The two
    halves therefore happen inside one hold of the gate's single lock."""
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    turn, cue, waiter = await _hand_over_against_a_turn_queued_on_the_lock(
        gate,
    )

    assert cue is not None
    assert cue.kind == "admin"
    assert not gate.is_current(turn)
    for _ in range(5):
        await asyncio.sleep(0)
    assert gate.active_kind == "admin"
    assert not waiter.done()

    # And the waiter is not lost — it takes its own, later episode once the
    # cue is done.
    await gate.end(cue)
    queued = await asyncio.wait_for(waiter, timeout=1.0)
    assert queued.kind == "turn"
    assert queued.id > cue.id


async def test_a_two_hold_handover_loses_the_gate_to_the_queued_turn() -> None:
    """Proof that the pin above discriminates. The same driver, against a
    gate whose handover ends and begins in two lock holds: the queued turn
    takes the gate in the gap, the cue is refused, and the wake it was
    answering goes unheard."""
    from jasper.voice.output_gate import AssistantOutputGate

    class _TwoHoldHandoverGate(AssistantOutputGate):
        async def hand_over_if_current(
            self,
            episode: AssistantOutputEpisode,
            kind: AssistantOutputKind,
        ) -> AssistantOutputEpisode | None:
            async with self._lock:
                if self._admission_paused or not self.is_current(episode):
                    return None
                self._end_locked(episode, kind=None)
            async with self._lock:
                return self._begin_if_idle_locked(kind)

    gate = _TwoHoldHandoverGate()
    turn, cue, waiter = await _hand_over_against_a_turn_queued_on_the_lock(
        gate,
    )

    assert cue is None
    stolen = await asyncio.wait_for(waiter, timeout=1.0)
    assert stolen.kind == "turn"
    assert stolen.id > turn.id
    assert gate.active_kind == "turn"


async def test_the_handover_refuses_without_ending_anything() -> None:
    """A refusal leaves the caller owning what it still owns: it is the
    caller's episode to finish, its duck to hand back and its gate to
    release. Ending first and asking afterwards would strand a caller that
    still believes it owns output — and would open the gate for a queued
    turn rather than for the sound the handover exists to let through."""
    from jasper.voice.output_gate import (
        AssistantOutputEpisode,
        AssistantOutputGate,
    )

    gate = AssistantOutputGate()
    turn = await gate.begin_turn()

    # Paused admission: nothing may be admitted, so nothing is ended.
    await gate.pause_admission()
    assert await gate.hand_over_if_current(turn, "admin") is None
    assert gate.is_current(turn)
    assert gate.active_kind == "turn"
    await gate.resume_admission()

    # Same id, earlier epoch: the caller was already preempted once.
    superseded = AssistantOutputEpisode(
        id=turn.id, kind="turn", epoch=turn.epoch - 1,
    )
    assert await gate.hand_over_if_current(superseded, "admin") is None
    assert gate.is_current(turn)
    assert gate.active_kind == "turn"

    # An episode this gate never had active.
    stale = AssistantOutputEpisode(
        id=turn.id + 1, kind="turn", epoch=turn.epoch,
    )
    assert await gate.hand_over_if_current(stale, "admin") is None
    assert gate.is_current(turn)
    assert gate.active_kind == "turn"
