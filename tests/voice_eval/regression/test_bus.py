"""Bus regression scenarios.

`test_next_bus_default_stop` — same three-assertion shape as the
other scenarios:
  1. Trajectory: did the model call get_bus_arrivals?
  2. Outcome: did the tool return a non-empty arrivals list?
  3. Reality: do the tool's routes match the MTA's BusTime SIRI
     response within tolerance?

`test_bus_outage_speaks_error` — the regression for the
narrate-an-outage-as-"no buses" bug. Forces a total BusTime outage
(every stop fetch fails) and asserts the tool surfaces {error} and
the model speaks it, NOT a confident "no buses in the next half
hour." Without the fix the tool returned an empty arrivals list on
a dead feed and the model narrated the empty list as "no buses."

Read-only — no playback side-effects.

============================================================
COST NOTICE — read tests/voice_eval/harness.py top docstring
============================================================
Paid LLM API calls per turn. Read-only scenarios but the LLM
cost still applies. PASS_K = 3 turns per scenario function;
the outage scenario uses PASS_K_OUTAGE = 1 (one paid turn) — its
contract is binary (error vs no-error narration), so one trial is
enough and keeps the bill down. DO NOT loop or increase either
PASS_K without explicit human approval.
============================================================
"""
from __future__ import annotations

import pytest

from jasper.bus import BusClient
from tests.voice_eval import oracles


PASS_K = 3
# The outage scenario's assertion is binary (the model either speaks
# the error or it narrates "no buses"); a single paid turn settles it.
PASS_K_OUTAGE = 1


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_next_bus_default_stop(harness, trial: int) -> None:
    """Asks 'when's the next bus?' — at the speaker's configured stop
    with multiple routes (e.g. B35 + B70 at 4 Av/39 St eastbound),
    the model should call `get_bus_arrivals` with an empty route
    string, get back arrivals for both, and speak the next few.

    Three assertions:
      1. Trajectory — model called get_bus_arrivals (NOT
         get_subway_arrivals — we're not on a train question)
      2. Outcome — tool returned at least one arrival (or, if MTA
         honestly has no buses coming, the scenario skips rather
         than fails)
      3. Reality — the routes the tool returned match the routes
         MTA's SIRI API returned just now, in the same order"""
    if not harness.cfg.bus_enabled:
        pytest.skip(
            "voice-eval: bus not configured (JASPER_MTA_BUSTIME_KEY + "
            "JASPER_BUS_STOPS required) — set them to run this scenario",
        )

    result = await harness.ask("when's the next bus?")

    # 1. Trajectory
    call = result.tool_call("get_bus_arrivals")
    assert call is not None, (
        f"[trial {trial}] model did not call get_bus_arrivals. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — got arrivals back
    tool_arrivals = (call.result or {}).get("arrivals") or []
    truth = await oracles.bus_arrivals(
        stop_id=harness.cfg.bus_stop_id,
        api_key=harness.cfg.mta_bustime_key,
        routes=list(harness.cfg.bus_routes) or None,
    )
    if not truth:
        pytest.skip(
            f"[trial {trial}] MTA reports no upcoming buses at the "
            f"configured stop right now. Re-run when the route is "
            f"in service."
        )
    assert tool_arrivals, (
        f"[trial {trial}] tool returned no arrivals while MTA shows "
        f"{[(a['route'], a['minutes_from_now']) for a in truth]}. "
        f"See transcript: {result.transcript_path}"
    )

    # 3. Reality — routes match in order. (We don't compare minute
    # values exactly because BusTime updates every ~30s and the two
    # fetches can be seconds apart, with ETAs swinging more than a
    # minute under traffic.)
    tool_routes = [a.get("route") for a in tool_arrivals]
    truth_routes = [a.get("route") for a in truth]
    # Tool may return more than oracle if oracle limits are tighter;
    # compare the head.
    n = min(len(tool_routes), len(truth_routes))
    assert tool_routes[:n] == truth_routes[:n], (
        f"[trial {trial}] tool routes {tool_routes[:n]} != MTA routes "
        f"{truth_routes[:n]}. See transcript: {result.transcript_path}"
    )


@pytest.mark.parametrize("trial", range(PASS_K_OUTAGE))
async def test_bus_outage_speaks_error(harness, trial: int, monkeypatch) -> None:
    """Asks 'when's the next bus?' during a total BusTime outage. Every
    per-stop fetch is forced to fail (no cache), so the client raises
    TransitError and `get_bus_arrivals` returns {error: ...}. The model
    must speak that error — NOT a confident 'no buses in the next half
    hour'.

    Regression for the narrate-an-outage-as-"no buses" bug: pre-fix the
    tool returned {stops_queried, arrivals: []} on a dead feed, so the
    model heard an empty list and narrated it as "no buses." This is a
    resilience defect — a dead API looked identical to a quiet stop.

    Three assertions:
      1. Trajectory — model called get_bus_arrivals.
      2. Outcome — the tool returned {error}, not an arrivals list.
      3. Reality (spoken) — the model did NOT say "no buses" / "no
         more buses"; it surfaced trouble reaching the feed.

    No real MTA call happens — the fetch is monkeypatched to fail — so
    this scenario doesn't depend on live BusTime weather/service."""
    if not harness.cfg.bus_enabled:
        pytest.skip(
            "voice-eval: bus not configured (JASPER_MTA_BUSTIME_KEY + "
            "JASPER_BUS_STOPS required) — set them to run this scenario",
        )

    # Force a total outage: every per-stop fetch returns None (the
    # failure sentinel), which `get_arrivals` turns into a TransitError
    # when no stop has a cache. Patched at the class level before the
    # harness builds its BusClient inside ask().
    async def _always_fail(self, stop_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(BusClient, "_fetch_for_stop", _always_fail)

    result = await harness.ask("when's the next bus?")

    # 1. Trajectory
    call = result.tool_call("get_bus_arrivals")
    assert call is not None, (
        f"[trial {trial}] model did not call get_bus_arrivals. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised instead of returning {{error}}: "
            f"{call.error}. See transcript: {result.transcript_path}",
        )

    # 2. Outcome — the tool returned an error payload, not arrivals.
    payload = call.result or {}
    assert "error" in payload, (
        f"[trial {trial}] tool returned {payload!r} on a total outage; "
        f"expected an {{error}} payload so the model speaks it instead of "
        f"narrating an empty list. See transcript: {result.transcript_path}"
    )
    assert "arrivals" not in payload, (
        f"[trial {trial}] error payload also carried arrivals "
        f"({payload!r}) — the LLM is told to speak {{error}} verbatim, so "
        f"mixing both is a contradiction. See {result.transcript_path}"
    )

    # 3. Reality (spoken) — the model must not narrate the outage as
    # "no buses." Skips gracefully if no transcript was captured.
    if result.spoken_text:
        spoken = result.spoken_text.lower()
        assert "no bus" not in spoken and "no more bus" not in spoken, (
            f"[trial {trial}] model narrated the outage as 'no buses': "
            f"{result.spoken_text!r}. The tool returned {{error}}; the "
            f"model should surface trouble reaching the feed. "
            f"See transcript: {result.transcript_path}"
        )
