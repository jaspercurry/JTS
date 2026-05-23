"""Tool-dispatch tests for jasper.tools.citibike.

Confirms:
  - make_citibike_tools(None) and an empty/disabled client return []
    (model doesn't see the tool when Citi Bike isn't configured)
  - make_citibike_tools(<enabled client>) returns one tool named
    get_citibike_status
  - The tool dispatches to client.get_status, with station_label
    routed through to station_filter
  - The result shape is what the LLM expects: stations list,
    ebike_only_mode echoed, filter echoed, no_match populated
  - ebike_only_mode=True drops the classic_bikes field from each
    station's dict
  - TransitError from the client surfaces as {error: ...} (the LLM
    has been instructed to read it verbatim)
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from jasper.citibike import StationStatus
from jasper.tools import build_tool
from jasper.tools.citibike import make_citibike_tools
from jasper.transit.base import TransitError


# --- Stub CitiBikeClient ----------------------------------------------


@dataclass
class _FakeClient:
    """Minimal duck-typed substitute for CitiBikeClient — only the
    surface the tool actually touches (enabled, ebike_only, get_status).
    Avoids subclassing the real client so the test verifies the tool's
    contract, not its inheritance."""
    saved_count: int = 1
    ebike_only_flag: bool = False
    result: list[StationStatus] | None = None
    raise_on_call: BaseException | None = None
    calls: list[str] = None

    def __post_init__(self):
        if self.calls is None:
            self.calls = []
        if self.result is None:
            self.result = [_ok_station("abc", "9 Av")]

    @property
    def enabled(self) -> bool:
        return self.saved_count > 0

    @property
    def ebike_only(self) -> bool:
        return self.ebike_only_flag

    def get_status(self, *, station_filter: str = "") -> list[StationStatus]:
        self.calls.append(station_filter)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return list(self.result)


def _ok_station(sid: str, label: str, **kw) -> StationStatus:
    defaults = dict(
        station_id=sid, label=label,
        classic_bikes=5, ebikes=2, docks=10,
        status="ok", last_reported_age_seconds=20,
    )
    defaults.update(kw)
    return StationStatus(**defaults)


# --- Gating: empty list when not configured ---------------------------


def test_returns_empty_when_client_is_none():
    assert make_citibike_tools(None) == []


def test_returns_empty_when_no_stations_saved():
    fake = _FakeClient(saved_count=0)
    assert make_citibike_tools(fake) == []


# --- One tool registered when configured ------------------------------


def test_returns_one_tool_when_configured():
    fake = _FakeClient()
    assert len(make_citibike_tools(fake)) == 1


def test_tool_name_is_get_citibike_status():
    fake = _FakeClient()
    [fn] = make_citibike_tools(fake)
    built = build_tool(fn)
    assert built.name == "get_citibike_status"


def test_tool_schema_is_serializable():
    """The tool's schema must round-trip through build_tool. The HA tool
    test covers provider-specific schema serializers separately — here
    we just confirm the basic introspection doesn't blow up."""
    fake = _FakeClient()
    [fn] = make_citibike_tools(fake)
    built = build_tool(fn)
    assert built.parameters is not None
    # station_label argument with default ""
    assert "station_label" in built.parameters.get("properties", {})


# --- Dispatch + result shape ------------------------------------------


@pytest.mark.asyncio
async def test_tool_passes_label_as_station_filter():
    fake = _FakeClient()
    [fn] = make_citibike_tools(fake)
    await fn(station_label="9 Av")
    assert fake.calls == ["9 Av"]


@pytest.mark.asyncio
async def test_tool_empty_label_passes_empty_filter():
    fake = _FakeClient()
    [fn] = make_citibike_tools(fake)
    await fn()
    assert fake.calls == [""]


@pytest.mark.asyncio
async def test_tool_result_includes_required_top_level_keys():
    fake = _FakeClient()
    [fn] = make_citibike_tools(fake)
    result = await fn(station_label="")
    assert set(result.keys()) >= {
        "stations", "ebike_only_mode", "filter", "no_match",
    }


@pytest.mark.asyncio
async def test_tool_result_includes_classic_when_not_ebike_only():
    fake = _FakeClient(ebike_only_flag=False)
    [fn] = make_citibike_tools(fake)
    result = await fn()
    assert result["ebike_only_mode"] is False
    [s] = result["stations"]
    assert "classic_bikes" in s
    assert s["classic_bikes"] == 5
    assert s["ebikes"] == 2


@pytest.mark.asyncio
async def test_tool_result_drops_classic_when_ebike_only():
    fake = _FakeClient(ebike_only_flag=True)
    [fn] = make_citibike_tools(fake)
    result = await fn()
    assert result["ebike_only_mode"] is True
    [s] = result["stations"]
    assert "classic_bikes" not in s
    assert s["ebikes"] == 2
    assert s["docks"] == 10


@pytest.mark.asyncio
async def test_tool_no_match_true_when_filter_excludes_everything():
    fake = _FakeClient(result=[])  # client returns empty
    [fn] = make_citibike_tools(fake)
    result = await fn(station_label="ghost")
    assert result["no_match"] is True
    assert result["stations"] == []
    assert result["filter"] == "ghost"


@pytest.mark.asyncio
async def test_tool_no_match_false_when_no_filter_passed():
    fake = _FakeClient(result=[])  # client returns empty
    [fn] = make_citibike_tools(fake)
    result = await fn(station_label="")
    # Empty filter + empty result is NOT "no match" — it's "you have
    # no saved stations." The LLM gets ebike_only_mode + an empty
    # stations list; no_match=false signals "filter didn't exclude
    # anything; the absence is structural."
    assert result["no_match"] is False


@pytest.mark.asyncio
async def test_tool_no_match_false_when_filter_whitespace_only():
    fake = _FakeClient(result=[])
    [fn] = make_citibike_tools(fake)
    result = await fn(station_label="   ")
    # Whitespace doesn't count as a filter — strip() before the check.
    assert result["no_match"] is False


@pytest.mark.asyncio
async def test_tool_multi_station_returns_per_station_dicts():
    fake = _FakeClient(result=[
        _ok_station("abc", "9 Av"),
        _ok_station("def", "Atlantic", ebikes=4, classic_bikes=1, docks=20),
    ])
    [fn] = make_citibike_tools(fake)
    result = await fn()
    # Labels are normalized for speech in the tool response: "9 Av"
    # expands to "9th Avenue" so the LLM's TTS says "ninth avenue"
    # rather than "nine av." See normalize_station_name docstring.
    labels = [s["label"] for s in result["stations"]]
    assert labels == ["9th Avenue", "Atlantic"]
    assert result["stations"][1]["ebikes"] == 4
    assert result["stations"][1]["classic_bikes"] == 1


@pytest.mark.asyncio
async def test_tool_propagates_offline_and_missing_status():
    fake = _FakeClient(result=[
        _ok_station("abc", "9 Av"),
        _ok_station("off", "Off", status="offline"),
        _ok_station("ghost", "Ghost", status="missing", classic_bikes=0, ebikes=0),
    ])
    [fn] = make_citibike_tools(fake)
    result = await fn()
    statuses = {s["label"]: s["status"] for s in result["stations"]}
    # "9 Av" → "9th Avenue" via normalize_station_name; "Off" / "Ghost"
    # have no abbreviations so they pass through unchanged.
    assert statuses == {"9th Avenue": "ok", "Off": "offline", "Ghost": "missing"}


# --- Error handling ---------------------------------------------------


@pytest.mark.asyncio
async def test_tool_returns_error_dict_on_transit_error():
    fake = _FakeClient(raise_on_call=TransitError("GBFS unreachable"))
    [fn] = make_citibike_tools(fake)
    result = await fn()
    assert "error" in result
    assert "GBFS unreachable" in result["error"]
    # On error path we do NOT also return a stations list — the LLM is
    # instructed to speak {error} verbatim. Mixing both would be
    # confusing.
    assert "stations" not in result


@pytest.mark.asyncio
async def test_tool_propagates_unexpected_exception():
    """Programming-error exceptions (not TransitError) should bubble
    so the daemon's outer error handler logs and surfaces them. We
    don't want to silently mask bugs by catch-all."""
    fake = _FakeClient(raise_on_call=RuntimeError("bug"))
    [fn] = make_citibike_tools(fake)
    with pytest.raises(RuntimeError, match="bug"):
        await fn()
