# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from jasper.tools import PythonExecutor, Tool, ToolDefinition, ToolRegistry
from jasper.tools.catalog import build_catalog
from jasper.tools.packs import TOOL_PACKS, CapabilityPack, register_packs
from jasper.tools.weather import (
    WEATHER_TOOL_LABELS,
    WEATHER_TOOL_NAME,
    WEATHER_TOOL_TIMEOUT_SEC,
)
from jasper.tools.travel_routes import (
    TRAVEL_ROUTES_TOOL_LABELS,
    TRAVEL_ROUTES_TOOL_NAME,
    TRAVEL_ROUTES_TOOL_TIMEOUT_SEC,
)
from tests._tool_pack_contract import (
    DispatchCase,
    assert_capability_pack_contract,
    assert_dispatch_cases,
    full_registry,
    full_tool_deps,
    minimal_registry,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_starter_example():
    path = ROOT / "docs" / "examples" / "tool_pack_starter.py"
    spec = importlib.util.spec_from_file_location("tool_pack_starter", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pack(name: str) -> CapabilityPack:
    return next(pack for pack in TOOL_PACKS if pack.name == name)


@pytest.mark.asyncio
async def test_docs_starter_pack_uses_real_boundary_without_registering_production():
    starter = _load_starter_example()

    assert isinstance(starter.STARTER_PACK, CapabilityPack)
    assert starter.STARTER_PACK.name not in {pack.name for pack in TOOL_PACKS}
    assert starter.STARTER_PACK.catalog_pack is starter.STARTER_CATALOG_PACK

    tools = list(starter.STARTER_PACK.build(starter.StarterDeps()))
    assert len(tools) == 1
    built = tools[0]
    assert isinstance(built, Tool)
    assert isinstance(built.definition, ToolDefinition)
    assert isinstance(built.executor, PythonExecutor)
    assert built.name == starter.STARTER_TOOL_NAME
    assert built.llm_description
    assert built.labels == starter.STARTER_TOOL_LABELS
    assert built.timeout == starter.STARTER_TOOL_TIMEOUT_SEC
    assert built.untrusted_output is False
    assert built.consequential is False

    reg = ToolRegistry()
    outcomes = register_packs(
        reg,
        starter.StarterDeps(sender_name="Codex"),
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(starter.STARTER_PACK,),
    )
    assert [(o.name, o.status, o.tool_count) for o in outcomes] == [
        ("example_postcard", "registered", 1),
    ]
    assert list(reg.tools) == [starter.STARTER_TOOL_NAME]
    assert reg.tool_packs == {starter.STARTER_TOOL_NAME: starter.STARTER_PACK.name}

    manifest = reg.to_manifest()[0]
    assert manifest["description"] == built.model_facing_description()
    assert manifest["labels"] == list(starter.STARTER_TOOL_LABELS)
    assert manifest["timeout"] == starter.STARTER_TOOL_TIMEOUT_SEC
    assert manifest["risk_flags"] == {
        "untrusted_output": False,
        "consequential": False,
    }

    await assert_dispatch_cases(
        reg,
        [
            DispatchCase(
                starter.STARTER_TOOL_NAME,
                {"recipient": "Ada"},
                {
                    "recipient": "Ada",
                    "sender": "Codex",
                    "message": "Wish you were here, Ada.",
                },
            ),
        ],
    )


def test_docs_starter_pack_gate_is_part_of_the_copyable_deps_shape():
    starter = _load_starter_example()

    reg = ToolRegistry()
    outcomes = register_packs(
        reg,
        starter.StarterDeps(enabled=False),
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(starter.STARTER_PACK,),
    )

    assert [(o.name, o.status, o.tool_count) for o in outcomes] == [
        ("example_postcard", "skipped", 0),
    ]
    assert reg.tools == {}


def test_weather_pack_satisfies_rich_first_party_contract():
    pack = _pack("weather")
    reg = full_registry()
    catalog = build_catalog(reg, frozenset())

    assert_capability_pack_contract(
        pack=pack,
        registry=reg,
        catalog=catalog,
        expected_pack_name="weather",
        expected_category="Utilities",
        expected_catalog_pack={
            "id": "weather",
            "title": "Weather",
            "summary": (
                "Current conditions and forecast answers for the configured "
                "location."
            ),
            "setup_url": "/weather/",
        },
        expected_tool_names=[WEATHER_TOOL_NAME],
        expected_labels={WEATHER_TOOL_NAME: WEATHER_TOOL_LABELS},
        expected_timeouts={WEATHER_TOOL_NAME: WEATHER_TOOL_TIMEOUT_SEC},
        expected_risk_flags={
            WEATHER_TOOL_NAME: {
                "untrusted_output": False,
                "consequential": False,
            },
        },
    )


def test_weather_pack_setup_gate_story_is_keyless_and_pack_level():
    pack = _pack("weather")
    minimal = minimal_registry()
    catalog = build_catalog(minimal, frozenset())
    weather_tool = {
        tool["name"]: tool
        for tool in catalog["tools"]
    }[WEATHER_TOOL_NAME]

    assert pack.gate(full_tool_deps()) is True
    assert WEATHER_TOOL_NAME in minimal.tools
    assert weather_tool["status"] == "active"
    assert weather_tool["setup_url"] is None
    assert weather_tool["requires_setup"] is False
    assert {
        pack["id"]: pack
        for pack in catalog["packs"]
    }["weather"]["setup_url"] == "/weather/"


def test_travel_routes_pack_satisfies_rich_first_party_contract():
    pack = _pack("travel_routes")
    reg = full_registry()
    catalog = build_catalog(reg, frozenset())

    assert_capability_pack_contract(
        pack=pack,
        registry=reg,
        catalog=catalog,
        expected_pack_name="travel_routes",
        expected_category="Transit",
        expected_catalog_pack={
            "id": "travel-routes",
            "title": "Travel Time",
            "summary": (
                "Destination ETAs and route overviews from the speaker's "
                "saved location."
            ),
            "setup_url": "/transit/",
        },
        expected_tool_names=[TRAVEL_ROUTES_TOOL_NAME],
        expected_labels={TRAVEL_ROUTES_TOOL_NAME: TRAVEL_ROUTES_TOOL_LABELS},
        expected_timeouts={TRAVEL_ROUTES_TOOL_NAME: TRAVEL_ROUTES_TOOL_TIMEOUT_SEC},
        expected_risk_flags={
            TRAVEL_ROUTES_TOOL_NAME: {
                "untrusted_output": True,
                "consequential": False,
            },
        },
    )


@pytest.mark.asyncio
async def test_travel_routes_pack_dispatches_through_executor_boundary():
    class StubRoutes:
        def __init__(self) -> None:
            self.calls = []

        async def get_travel_routes(self, *, destination, travel_mode="", max_routes=1):
            self.calls.append({
                "destination": destination,
                "travel_mode": travel_mode,
                "max_routes": max_routes,
            })
            return {"ok": True, "routes": [{"duration_minutes": 12}]}

    routes = StubRoutes()
    reg = ToolRegistry()
    outcomes = register_packs(
        reg,
        full_tool_deps(google_routes=routes),
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(_pack("travel_routes"),),
    )

    assert [(o.name, o.status, o.tool_count) for o in outcomes] == [
        ("travel_routes", "registered", 1),
    ]
    await assert_dispatch_cases(
        reg,
        [
            DispatchCase(
                TRAVEL_ROUTES_TOOL_NAME,
                {
                    "destination": "30 Rock",
                    "travel_mode": "drive",
                    "max_routes": 1,
                },
                {"ok": True, "routes": [{"duration_minutes": 12}]},
            ),
        ],
    )
    assert routes.calls == [{
        "destination": "30 Rock",
        "travel_mode": "drive",
        "max_routes": 1,
    }]


def test_travel_routes_pack_is_needs_setup_without_client():
    pack = _pack("travel_routes")
    minimal = minimal_registry()
    catalog = build_catalog(minimal, frozenset())
    travel_tool = {
        tool["name"]: tool
        for tool in catalog["tools"]
    }[TRAVEL_ROUTES_TOOL_NAME]

    assert pack.gate(full_tool_deps(google_routes=None)) is True
    assert TRAVEL_ROUTES_TOOL_NAME not in minimal.tools
    assert travel_tool["status"] == "needs_setup"
    assert travel_tool["setup_url"] == "/transit/"
    assert travel_tool["requires_setup"] is True


@pytest.mark.asyncio
async def test_weather_pack_dispatches_through_executor_boundary():
    class StubWeather:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_weather(self, location: str = "") -> dict:
            self.calls.append(location)
            return {"location": location or "home", "now": {"temperature": 72}}

    weather = StubWeather()
    reg = ToolRegistry()
    outcomes = register_packs(
        reg,
        full_tool_deps(weather=weather),
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(_pack("weather"),),
    )

    assert [(o.name, o.status, o.tool_count) for o in outcomes] == [
        ("weather", "registered", 1),
    ]
    await assert_dispatch_cases(
        reg,
        [
            DispatchCase(
                WEATHER_TOOL_NAME,
                {"location": "Tampa, Florida"},
                {
                    "location": "Tampa, Florida",
                    "now": {"temperature": 72},
                },
            ),
        ],
    )
    assert weather.calls == ["Tampa, Florida"]
