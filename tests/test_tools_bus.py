# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tool-dispatch tests for jasper.tools.bus.

Confirms:
  - make_bus_tools(None) and a disabled client return [] (model
    doesn't see the tool when buses aren't configured)
  - make_bus_tools(<enabled client>) returns one tool named
    get_bus_arrivals
  - The tool dispatches to client.get_arrivals, routing `route`
    through and echoing stops_queried
  - A reachable feed with no upcoming buses returns an empty
    arrivals list (the LLM narrates "no buses"), NOT an error
  - A total BusTime outage (every stop failed, no cache) raises
    TransitError from the client and surfaces as {error: ...} so
    the LLM speaks the error verbatim — the bug this fix closes

Companion to tests/test_bus.py, which exercises BusClient against a
mocked httpx transport. Here we duck-type the client so the test
verifies the tool's contract, not the HTTP layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from jasper.bus import BusArrival
from jasper.tools import build_tool
from jasper.tools.bus import make_bus_tools
from jasper.transit.base import TransitError


# --- Stub BusClient ---------------------------------------------------


@dataclass
class _FakeClient:
    """Minimal duck-typed substitute for BusClient — only the surface
    the tool touches (enabled, stop_ids, get_arrivals). Avoids
    subclassing the real client so the test pins the tool's contract."""

    stop_count: int = 1
    result: list[BusArrival] | None = None
    raise_on_call: BaseException | None = None
    calls: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.result is None:
            self.result = [_arrival("B35", 3)]

    @property
    def enabled(self) -> bool:
        return self.stop_count > 0

    @property
    def stop_ids(self) -> tuple[str, ...]:
        return tuple(f"30268{i}" for i in range(self.stop_count))

    async def get_arrivals(self, route: str = ""):
        self.calls.append(route)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return list(self.result)


def _arrival(route: str, minutes: int, **kw) -> BusArrival:
    defaults = dict(
        route=route,
        destination="BROWNSVILLE",
        minutes_from_now=minutes,
        presentable_distance="approaching",
        stops_from_call=0,
        stop_id="302680",
        stop_label="4 Av/39 St eastbound",
    )
    defaults.update(kw)
    return BusArrival(**defaults)


# --- Gating: empty list when not configured ---------------------------


def test_returns_empty_when_client_is_none():
    assert make_bus_tools(None) == []


def test_returns_empty_when_no_stops_configured():
    fake = _FakeClient(stop_count=0)
    assert make_bus_tools(fake) == []


# --- One tool registered when configured ------------------------------


def test_returns_one_tool_when_configured():
    fake = _FakeClient()
    assert len(make_bus_tools(fake)) == 1


def test_tool_name_is_get_bus_arrivals():
    fake = _FakeClient()
    [fn] = make_bus_tools(fake)
    built = build_tool(fn)
    assert built.name == "get_bus_arrivals"


def test_tool_schema_is_serializable():
    fake = _FakeClient()
    [fn] = make_bus_tools(fake)
    built = build_tool(fn)
    assert built.parameters is not None
    assert "route" in built.parameters.get("properties", {})


# --- Dispatch + result shape ------------------------------------------


@pytest.mark.asyncio
async def test_tool_passes_route_through():
    fake = _FakeClient()
    [fn] = make_bus_tools(fake)
    await fn(route="B70")
    assert fake.calls == ["B70"]


@pytest.mark.asyncio
async def test_tool_empty_route_passes_empty_string():
    fake = _FakeClient()
    [fn] = make_bus_tools(fake)
    await fn()
    assert fake.calls == [""]


@pytest.mark.asyncio
async def test_tool_result_shape_on_success():
    fake = _FakeClient(result=[_arrival("B35", 3), _arrival("B70", 7)])
    [fn] = make_bus_tools(fake)
    result = await fn()
    assert set(result.keys()) == {"stops_queried", "arrivals"}
    assert result["stops_queried"] == ["302680"]
    assert [a["route"] for a in result["arrivals"]] == ["B35", "B70"]
    assert result["arrivals"][0]["minutes_from_now"] == 3


@pytest.mark.asyncio
async def test_tool_empty_arrivals_is_not_an_error():
    """A reachable feed with nothing coming returns an empty arrivals
    list — the LLM narrates 'no buses', NOT the outage error. This is
    the genuinely-empty case the fix preserves."""
    fake = _FakeClient(result=[])
    [fn] = make_bus_tools(fake)
    result = await fn()
    assert "error" not in result
    assert result["arrivals"] == []
    assert result["stops_queried"] == ["302680"]


# --- Error handling: total outage -------------------------------------


@pytest.mark.asyncio
async def test_tool_returns_error_dict_on_total_outage():
    """Every configured stop failed AND no cache served → the client
    raises TransitError → the tool returns {error: ...} so the LLM
    speaks it verbatim instead of confidently saying 'no buses'. This
    is the bug this fix closes."""
    fake = _FakeClient(raise_on_call=TransitError("the MTA bus feed is unreachable"))
    [fn] = make_bus_tools(fake)
    result = await fn()
    assert "error" in result
    assert "MTA bus feed" in result["error"]
    # On the error path we do NOT also return an arrivals list — the
    # LLM is instructed to speak {error} verbatim. Mixing both would be
    # confusing (a "no buses" + error contradiction).
    assert "arrivals" not in result
    assert "stops_queried" not in result


@pytest.mark.asyncio
async def test_tool_propagates_unexpected_exception():
    """Programming-error exceptions (not TransitError) should bubble so
    the daemon's outer error handler logs and surfaces them. We don't
    want to silently mask bugs with a catch-all."""
    fake = _FakeClient(raise_on_call=RuntimeError("bug"))
    [fn] = make_bus_tools(fake)
    with pytest.raises(RuntimeError, match="bug"):
        await fn()
