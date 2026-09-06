# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio


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


async def test_the_swap_beats_a_turn_waiter_queued_before_it() -> None:
    """A sound that MUST be heard cannot end its blocker and then ask for
    the gate: `end` wakes every queued `begin_turn` waiter, and one of them
    owns output before the ask lands — the sound is skipped (NN-6). The two
    halves therefore happen inside one hold of the gate's single lock, so a
    waiter queued before the swap cannot observe the gap."""
    from jasper.voice.output_gate import AssistantOutputGate

    gate = AssistantOutputGate()
    blocker = await gate.begin_if_idle("proactive")
    assert blocker is not None

    # Queued while a non-turn episode owns output, so it is a real waiter
    # rather than a caller that joins an open turn.
    waiter = asyncio.create_task(gate.begin_turn())
    await asyncio.sleep(0)
    assert not waiter.done()

    # The blocker ends and the turn takes the gate with no yield in
    # between: the waiter has been signalled but has not run.
    await gate.end(blocker)
    turn = await gate.begin_turn()
    assert turn.kind == "turn"
    assert not waiter.done()

    cue = await gate.end_and_begin_if_idle(turn, "admin")
    assert cue is not None
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
