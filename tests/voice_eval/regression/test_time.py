# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Paid, read-only time scenarios.

The connection's prompt timestamp can become stale; answers need fresh tool time.
PASS_K = 3 turns per scenario. Announce the count and estimated cost before
running. Never loop or auto-retry.
Increase PASS_K only with explicit approval.
See tests/voice_eval/README.md for run rules and evidence limits.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tests.voice_eval import oracles


PASS_K = 3


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_what_time_is_it(harness, trial: int) -> None:
    """Require fresh tool time and compare tool and spoken times with the clock."""
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
    if tool_time.tzinfo is None:
        # Fall back to naive comparison against local naive now.
        now = now.replace(tzinfo=None)
    assert oracles.time_within_seconds(tool_time, now, seconds=60), (
        f"[trial {trial}] tool returned {tool_time} but wall-clock "
        f"is {now} — gap > 1 minute. "
        f"See transcript: {result.transcript_path}"
    )

    result.require_spoken_text()
    spoken_time = harness.extract_time_from_text(result.spoken_text)
    assert spoken_time is not None, result.transcript_path
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
