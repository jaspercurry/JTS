# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Google Routes travel-time regression scenarios.

Read-only, but paid: each test makes one realtime LLM turn and one Google
Routes API call when configured. Keep PASS_K at 1 unless a human explicitly
approves a higher cost.
"""
from __future__ import annotations

import os

import pytest


PASS_K = 1


def _require_routes_configured() -> None:
    if not (
        os.environ.get("GOOGLE_ROUTES_API_KEY", "").strip()
        and os.environ.get("JASPER_TRANSIT_LAT", "").strip()
        and os.environ.get("JASPER_TRANSIT_LON", "").strip()
    ):
        pytest.skip(
            "voice-eval: Google Routes not configured "
            "(GOOGLE_ROUTES_API_KEY + JASPER_TRANSIT_LAT/LON required)",
        )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_travel_time_default_mode(harness, trial: int) -> None:
    _require_routes_configured()

    result = await harness.ask("how long will it take me to get to 30 Rock?")

    call = result.tool_call("get_travel_routes")
    assert call is not None, (
        f"[trial {trial}] model did not call get_travel_routes. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    assert "30" in (call.args.get("destination") or "")
    assert (call.args.get("travel_mode") or "") in {"", "transit"}
    assert int(call.args.get("max_routes") or 1) <= 1


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_travel_time_drive_override(harness, trial: int) -> None:
    _require_routes_configured()

    result = await harness.ask("how long would it take me to drive to 30 Rock?")

    call = result.tool_call("get_travel_routes")
    assert call is not None, (
        f"[trial {trial}] model did not call get_travel_routes. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    assert "30" in (call.args.get("destination") or "")
    assert (call.args.get("travel_mode") or "").lower() in {"drive", "driving"}


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_travel_options_requests_two_routes(harness, trial: int) -> None:
    _require_routes_configured()

    result = await harness.ask("how can I get to 30 Rock?")

    call = result.tool_call("get_travel_routes")
    assert call is not None, (
        f"[trial {trial}] model did not call get_travel_routes. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    assert "30" in (call.args.get("destination") or "")
    assert int(call.args.get("max_routes") or 1) == 2
