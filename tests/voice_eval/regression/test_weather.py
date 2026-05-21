"""Weather regression scenarios.

Each scenario follows the same three-assertion shape as
`test_subway.py`:

  1. Trajectory: the model called the expected tool.
  2. Outcome: the tool returned the expected fields.
  3. Reality: the tool's data matches an independent ground-truth
     fetch within tolerance.

Read-only — no playback side-effects.

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


# Run each scenario 3× (pass^3). For a regression test where
# consistency matters, all three must pass — pytest fails the
# overall scenario if any trial fails.
PASS_K = 3


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_sunset_today(harness, trial: int) -> None:
    """Asks 'what time does the sun set today?' — the model should
    call `get_weather`, the tool response should include today's
    sunset timestamp, and that timestamp should match Open-Meteo's
    independent answer within 1 minute.

    **KNOWN FAILING (2026-05-21)**: the weather tool doesn't request
    `sunrise,sunset` from Open-Meteo today — the assertion on the
    `sunset` field will fail until the sunset fix lands. The
    failure is the bug, documented in test form. When the fix
    lands, this turns green and stays green.
    """
    if not harness.cfg.weather_default_location:
        pytest.skip(
            "voice-eval: JASPER_DEFAULT_LOCATION not set; sunset has no "
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

    truth = await oracles.weather_sunset(harness.cfg.weather_default_location)
    if truth is None:
        pytest.skip("voice-eval: Open-Meteo oracle returned no result; "
                    "transient — re-run")
    assert oracles.time_within_seconds(tool_sunset, truth, seconds=60), (
        f"[trial {trial}] tool sunset {tool_sunset} differs from "
        f"Open-Meteo {truth} by more than 1 minute. "
        f"See transcript: {result.transcript_path}"
    )

    # 4. Spoken reality — if the model spoke a time, it should be
    # within 5 minutes of the truth. Looser tolerance than the tool
    # assertion because the model might round ("around 8 PM" vs
    # 8:14 PM). Skips if no transcript captured.
    if result.spoken_text:
        spoken_time = harness.extract_time_from_text(result.spoken_text)
        if spoken_time is not None:
            from datetime import datetime
            truth_t = truth.time()
            spoken_dt = datetime.combine(truth.date(), spoken_time)
            truth_dt = datetime.combine(truth.date(), truth_t)
            assert oracles.time_within_seconds(spoken_dt, truth_dt, seconds=300), (
                f"[trial {trial}] model spoke {spoken_time} but actual "
                f"sunset is {truth_t}. Spoken text: {result.spoken_text!r}. "
                f"See transcript: {result.transcript_path}"
            )
