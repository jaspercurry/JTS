# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Paid, read-only weather scenarios.

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
async def test_sunset_today(harness, trial: int) -> None:
    """Compare tool and spoken sunset times with the oracle for the same location."""
    if not harness.cfg.weather_prompt_location:
        pytest.skip(
            "voice-eval: weather default location not set; sunset has no "
            "location to resolve against",
        )

    result = await harness.ask("what time does the sun set today?")

    # 1. Trajectory — the model must call the weather tool.
    call = result.tool_call("get_weather")
    assert call is not None, (
        f"[trial {trial}] model did not call get_weather. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — response includes sunset on the today summary.
    today = ((call.result or {}).get("today") or {})
    sunset_raw = today.get("sunset")
    assert sunset_raw, (
        f"[trial {trial}] weather response had no `today.sunset` "
        f"field. Available keys: {list(today.keys())!r}. "
        f"See transcript: {result.transcript_path}"
    )

    # 3. Reality — tool's sunset matches Open-Meteo's independent
    # answer for the same location. ISO-8601 naive local time.
    try:
        tool_sunset = datetime.fromisoformat(sunset_raw)
    except (TypeError, ValueError):
        pytest.fail(
            f"[trial {trial}] sunset field is not ISO-8601: "
            f"{sunset_raw!r}. See transcript: {result.transcript_path}",
        )

    truth = await oracles.weather_sunset(harness.cfg.weather_prompt_location)
    if truth is None:
        pytest.skip("voice-eval: Open-Meteo oracle returned no result; "
                    "transient — re-run")
    assert oracles.time_within_seconds(tool_sunset, truth, seconds=60), (
        f"[trial {trial}] tool sunset {tool_sunset} differs from "
        f"Open-Meteo {truth} by more than 1 minute. "
        f"See transcript: {result.transcript_path}"
    )

    result.require_spoken_text()
    spoken_time = harness.extract_time_from_text(result.spoken_text)
    assert spoken_time is not None, result.transcript_path
    truth_t = truth.time()
    spoken_dt = datetime.combine(truth.date(), spoken_time)
    truth_dt = datetime.combine(truth.date(), truth_t)
    assert oracles.time_within_seconds(spoken_dt, truth_dt, seconds=300), (
        f"[trial {trial}] model spoke {spoken_time} but actual "
        f"sunset is {truth_t}. Spoken text: {result.spoken_text!r}. "
        f"See transcript: {result.transcript_path}"
    )
