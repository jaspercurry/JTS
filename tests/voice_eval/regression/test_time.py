# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Time regression scenarios.

Each scenario follows the same three-assertion shape as
`test_subway.py`:

  1. Trajectory: the model called the expected tool.
  2. Outcome: the tool returned the expected fields.
  3. Reality: the tool's data matches an independent ground-truth
     fetch within tolerance.

Read-only — no playback side-effects.

Why this matters even though "the LLM knows the time": the
realtime models DON'T know the time mid-session. The system prompt
bakes a timestamp at connection-open and the connection stays open
for hours with idle-context-reset disabled (current default). With
no tool to refresh the clock, the model speaks the stale bake.

============================================================
COST NOTICE — read tests/voice_eval/harness.py top docstring
============================================================
Paid LLM API calls per turn. Read-only scenarios but the LLM
cost still applies. PASS_K = 3 turns per scenario function.
DO NOT loop or increase PASS_K without explicit human approval.
============================================================
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tests.voice_eval import oracles


PASS_K = 3


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_what_time_is_it(harness, trial: int) -> None:
    """Asks 'what time is it?' — the model should call
    `get_current_time` (or whatever the time tool ends up being
    named) and the response should be within 1 minute of
    `datetime.now()`.

    Without a time tool the model reads its stale system-prompt
    timestamp (baked at connection-open, potentially many hours old)
    and confidently speaks the wrong time; this test asserts the
    model instead calls a tool for fresh time on every question.

    Status (2026-06-15): the `get_current_time` tool has landed, is
    registered in `_build_test_registry`, and returns a
    minute-resolution `local_time` ISO string — exactly what
    assertion #2 reads. The earlier "no time tool exists yet"
    KNOWN-FAILING note is obsolete; this scenario is expected to
    pass. (Not re-run in the fix that corrected this note — confirm
    on the next paid eval pass.)"""
    result = await harness.ask("what time is it?")

    # 1. Trajectory — the model must call the time tool.
    call = result.tool_call("get_current_time")
    assert call is not None, (
        f"[trial {trial}] model did not call get_current_time. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — response includes a parseable local timestamp.
    #
    # The tool's exact response shape will be decided when the tool
    # is built. We assert on a `local_time` ISO-8601 field for now;
    # if the eventual tool uses a different field name, update this
    # assertion alongside the tool implementation. The shape is a
    # contract worth pinning early so the scenario keeps reading
    # the right field after the fix lands.
    result_dict = call.result or {}
    local_time_raw = result_dict.get("local_time")
    assert local_time_raw, (
        f"[trial {trial}] time response had no `local_time` field. "
        f"Result: {result_dict!r}. "
        f"See transcript: {result.transcript_path}"
    )
    try:
        tool_time = datetime.fromisoformat(local_time_raw)
    except (TypeError, ValueError):
        pytest.fail(
            f"[trial {trial}] local_time field is not ISO-8601: "
            f"{local_time_raw!r}. See transcript: {result.transcript_path}",
        )

    # 3. Reality — tool time matches wall-clock within tolerance.
    now = oracles.time_now_local()
    # Make both tz-aware for comparison (or strip tz on truth) —
    # the tool's emission is local-and-aware; oracles.time_now_local
    # is also aware.
    if tool_time.tzinfo is None:
        # Fall back to naive comparison against local naive now.
        now = now.replace(tzinfo=None)
    assert oracles.time_within_seconds(tool_time, now, seconds=60), (
        f"[trial {trial}] tool returned {tool_time} but wall-clock "
        f"is {now} — gap > 1 minute. "
        f"See transcript: {result.transcript_path}"
    )

    # 4. Spoken reality — the model's spoken time matches the wall
    # clock within 2 minutes (looser than the tool tolerance because
    # the model rounds to "10:14" vs "10:14:32"). Skips if no
    # transcript captured. Catches the stale-system-prompt bug
    # directly: pre-fix the model speaks whatever time was baked at
    # connection-open, often hours stale.
    if result.spoken_text:
        spoken_time = harness.extract_time_from_text(result.spoken_text)
        if spoken_time is not None:
            now_local = oracles.time_now_local()
            spoken_dt = datetime.combine(now_local.date(), spoken_time)
            now_naive = now_local.replace(tzinfo=None)
            assert oracles.time_within_seconds(
                spoken_dt, now_naive, seconds=120,
            ), (
                f"[trial {trial}] model spoke {spoken_time} but wall "
                f"clock is {now_naive.time()}. Spoken text: "
                f"{result.spoken_text!r}. "
                f"See transcript: {result.transcript_path}"
            )
