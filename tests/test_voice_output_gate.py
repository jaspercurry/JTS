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
