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
