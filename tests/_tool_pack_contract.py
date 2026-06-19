from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jasper.tools import ToolRegistry, dispatch_tool
from jasper.tools.bus import make_bus_tools
from jasper.tools.catalog import _CATALOG_HIDDEN
from jasper.tools.citibike import make_citibike_tools
from jasper.tools.packs import CapabilityPack, ToolDeps, register_packs
from jasper.tools.subway import make_subway_tools

LEGACY_PACK_ORDER = [
    "audio",
    "transport",
    "spotify",
    "weather",
    "transit",
    "home_assistant",
    "time",
    "timer",
    "research",
    "calendar",
    "gmail",
    "diagnostic",
]

EXPECTED_TOOL_NAMES = [
    "get_volume", "set_volume", "adjust_volume", "mute", "unmute",
    "next_track", "previous_track", "pause", "resume", "get_now_playing",
    "spotify_play", "spotify_play_latest_by_artist", "spotify_queue",
    "get_weather",
    "get_subway_arrivals", "get_bus_arrivals", "get_citibike_status",
    "home_assistant", "home_assistant_confirm",
    "get_current_time",
    "set_timer", "list_timers", "cancel_timer", "update_timer",
    "research",
    "calendar_today_summary", "calendar_upcoming",
    "gmail_unread_summary", "gmail_read_thread",
    "flag_recent_issue",
]

ALWAYS_ON_TOOL_NAMES = {
    "get_volume", "set_volume", "adjust_volume", "mute", "unmute",
    "next_track", "previous_track", "pause", "resume", "get_now_playing",
    "spotify_play", "spotify_play_latest_by_artist", "spotify_queue",
    "get_weather", "get_current_time",
}

GATED_TOOL_NAMES = {
    "get_subway_arrivals", "get_bus_arrivals", "get_citibike_status",
    "home_assistant", "home_assistant_confirm",
    "set_timer", "list_timers", "cancel_timer", "update_timer",
    "research",
    "calendar_today_summary", "calendar_upcoming",
    "gmail_unread_summary", "gmail_read_thread",
    "flag_recent_issue",
}

VISIBLE_TOOL_NAMES = (ALWAYS_ON_TOOL_NAMES | GATED_TOOL_NAMES) - _CATALOG_HIDDEN


@dataclass(frozen=True)
class DispatchCase:
    name: str
    args: dict[str, Any]
    expected: dict[str, Any]


def transit_tool_stubs() -> list[Any]:
    """The 3 shipped transit tools, built hardware-free with lazy stubs."""
    tools: list[Any] = []
    tools += list(make_subway_tools(object()))
    tools += list(make_bus_tools(types.SimpleNamespace(enabled=True)))
    tools += list(make_citibike_tools(types.SimpleNamespace(enabled=True)))
    return tools


def full_tool_deps(**overrides: Any) -> ToolDeps:
    """Gate-satisfying sentinel deps for the complete shipped registry.

    Tool factories capture deps lazily, so these stubs build the same
    definitions a live daemon would without touching hardware, network, or
    user state.
    """
    values = {
        "volume_coordinator": None,
        "renderer": None,
        "router": None,
        "weather": None,
        "spotify_device_name": "JTS",
        "spotify_setup_url": "",
        "transit_tools": transit_tool_stubs(),
        "ha": object(),
        "timer_scheduler": object(),
        "research_scheduler": object(),
        "google_clients": types.SimpleNamespace(
            list_account_names=lambda: ["jasper"],
        ),
        "wake_event_store": object(),
    }
    values.update(overrides)
    return ToolDeps(**values)


def minimal_tool_deps(**overrides: Any) -> ToolDeps:
    """Deps where only ungated always-on packs register."""
    values = {
        "volume_coordinator": None,
        "renderer": None,
        "router": None,
        "weather": None,
        "spotify_device_name": "JTS",
        "spotify_setup_url": "",
        "transit_tools": [],
        "ha": None,
        "timer_scheduler": None,
        "research_scheduler": None,
        "google_clients": types.SimpleNamespace(list_account_names=lambda: []),
        "wake_event_store": None,
    }
    values.update(overrides)
    return ToolDeps(**values)


def full_registry(
    *,
    disabled: frozenset[str] = frozenset(),
    disabled_packs: frozenset[str] = frozenset(),
    deps: ToolDeps | None = None,
) -> ToolRegistry:
    reg = ToolRegistry()
    register_packs(
        reg,
        deps or full_tool_deps(),
        disabled=disabled,
        disabled_packs=disabled_packs,
    )
    return reg


def minimal_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_packs(
        reg,
        minimal_tool_deps(),
        disabled=frozenset(),
        disabled_packs=frozenset(),
    )
    return reg


def manifest_by_name(registry: ToolRegistry) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in registry.to_manifest()}


def catalog_tools_by_name(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in catalog["tools"]}


def catalog_packs_by_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {pack["id"]: pack for pack in catalog["packs"]}


def _pack_tool_names(registry: ToolRegistry, pack: CapabilityPack) -> list[str]:
    return [
        name
        for name, pack_name in registry.tool_packs.items()
        if pack_name == pack.name
    ]


def assert_capability_pack_contract(
    *,
    pack: CapabilityPack,
    registry: ToolRegistry,
    catalog: dict[str, Any],
    expected_pack_name: str,
    expected_category: str,
    expected_catalog_pack: dict[str, Any],
    expected_tool_names: list[str],
    expected_labels: Mapping[str, tuple[str, ...]],
    expected_timeouts: Mapping[str, float],
    expected_risk_flags: Mapping[str, dict[str, bool]],
) -> None:
    """Checklist for the source-neutral pack boundary.

    This intentionally stays close to the public surfaces: internal pack
    metadata, ToolDefinition-derived manifest entries, catalog payloads, and
    registry pack indexes. It should make a pack test easier to scan, not hide
    the behavior behind a bespoke fixture language.
    """
    assert pack.name == expected_pack_name
    assert pack.category == expected_category
    assert pack.catalog_pack is not None
    assert {
        "id": pack.catalog_pack.id,
        "title": pack.catalog_pack.title,
        "summary": pack.catalog_pack.summary,
        "setup_url": pack.catalog_pack.setup_url,
    } == expected_catalog_pack

    assert _pack_tool_names(registry, pack) == expected_tool_names

    manifest = manifest_by_name(registry)
    catalog_tools = catalog_tools_by_name(catalog)
    catalog_packs = catalog_packs_by_id(catalog)

    assert expected_catalog_pack["id"] in catalog_packs
    assert catalog_packs[expected_catalog_pack["id"]]["tool_names"] == (
        expected_tool_names
    )
    assert catalog_packs[expected_catalog_pack["id"]]["category"] == (
        expected_category
    )

    for name in expected_tool_names:
        tool = registry.tools[name]
        entry = manifest[name]
        cat_tool = catalog_tools[name]

        assert entry["name"] == tool.name == name
        assert entry["description"] == tool.model_facing_description()
        assert entry["input_schema"] == tool.parameters
        assert entry["compatibility"]["providers"] == (
            sorted(tool.providers) if tool.providers else None
        )
        assert entry["labels"] == list(expected_labels[name])
        assert entry["timeout"] == expected_timeouts[name]
        assert entry["risk_flags"] == expected_risk_flags[name]

        assert cat_tool["category"] == expected_category
        assert cat_tool["pack"] == expected_catalog_pack
        assert cat_tool["labels"] == list(expected_labels[name])
        assert cat_tool["timeout"] == expected_timeouts[name]
        assert cat_tool["untrusted_output"] == (
            expected_risk_flags[name]["untrusted_output"]
        )
        assert cat_tool["consequential"] == (
            expected_risk_flags[name]["consequential"]
        )
        assert cat_tool["parameters"] == tool.parameters
        assert cat_tool["description"] == tool.model_facing_description()
        assert cat_tool["default_description"] == (
            tool.default_model_facing_description()
        )


async def assert_dispatch_cases(
    registry: ToolRegistry,
    cases: list[DispatchCase],
) -> None:
    for case in cases:
        assert await dispatch_tool(registry, case.name, case.args) == case.expected


def assert_duplicate_pack_fails_without_partial_registration(
    *,
    pack: CapabilityPack,
    deps: Any,
    expected_rolled_back_names: set[str],
) -> None:
    reg = ToolRegistry()
    outcomes = register_packs(
        reg,
        deps,
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(pack,),
    )

    assert len(outcomes) == 1
    assert outcomes[0].name == pack.name
    assert outcomes[0].status == "failed"
    assert "duplicate tool name" in (outcomes[0].error or "")
    assert expected_rolled_back_names.isdisjoint(reg.tools)
    assert expected_rolled_back_names.isdisjoint(reg.tool_packs)


def assert_duplicate_second_pack_fails_without_rolling_back_first(
    *,
    first_pack: CapabilityPack,
    duplicate_pack: CapabilityPack,
    deps: Any,
    expected_remaining_names: set[str],
    expected_rolled_back_names: set[str],
) -> None:
    reg = ToolRegistry()
    outcomes = register_packs(
        reg,
        deps,
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(first_pack, duplicate_pack),
    )

    assert [(o.name, o.status) for o in outcomes] == [
        (first_pack.name, "registered"),
        (duplicate_pack.name, "failed"),
    ]
    assert "duplicate tool name" in (outcomes[1].error or "")
    assert set(reg.tools) == expected_remaining_names
    assert expected_rolled_back_names.isdisjoint(reg.tools)
    assert expected_rolled_back_names.isdisjoint(reg.tool_packs)
    for name in expected_remaining_names:
        assert reg.tool_packs[name] == first_pack.name
