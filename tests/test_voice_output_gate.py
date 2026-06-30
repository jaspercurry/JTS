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
