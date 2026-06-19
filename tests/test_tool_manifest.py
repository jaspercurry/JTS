"""Slice 3 — the derived manifest is no-loss over every shipped tool.

`Tool.to_manifest_entry()` / `ToolRegistry.to_manifest()` build a stable,
provider-neutral record straight from existing Tool fields. This pins:
field-by-field equality with the source Tool, deterministic `providers`
ordering (frozenset has none), and that the manifest is in registration
order. Pure-additive — it must not change dispatch or the provider
serializers.
"""
from __future__ import annotations

from jasper.tools import MANIFEST_SCHEMA_VERSION, ToolRegistry
from tests._tool_pack_contract import full_registry


def _full_registry() -> ToolRegistry:
    return full_registry()


def test_manifest_covers_every_tool_in_order():
    reg = _full_registry()
    manifest = reg.to_manifest()
    assert len(manifest) == len(reg.tools) == 29
    assert [e["name"] for e in manifest] == list(reg.tools.keys())


def test_manifest_entries_are_no_loss():
    reg = _full_registry()
    by_name = {e["name"]: e for e in reg.to_manifest()}

    for name, t in reg.tools.items():
        entry = by_name[name]
        assert entry["schema_version"] == MANIFEST_SCHEMA_VERSION
        assert entry["name"] == t.name
        assert entry["description"] == t.model_facing_description()
        assert entry["input_schema"] == t.parameters
        expected_providers = sorted(t.providers) if t.providers else None
        assert entry["compatibility"]["providers"] == expected_providers
        assert entry["timeout"] == t.timeout
        assert entry["labels"] == list(t.labels)
        assert entry["risk_flags"] == {
            "untrusted_output": t.untrusted_output,
            "consequential": t.consequential,
        }


def test_manifest_providers_are_sorted_deterministically():
    """frozenset has no stable order; the manifest must sort providers so
    two runs produce identical output."""
    from jasper.tools import tool

    @tool(providers={"openai", "grok", "gemini"})
    def restricted() -> dict:
        """A provider-scoped tool."""
        return {}

    reg = ToolRegistry()
    reg.register(restricted)
    entry = reg.to_manifest()[0]
    assert entry["compatibility"]["providers"] == ["gemini", "grok", "openai"]


def test_manifest_providers_none_for_universal_tool():
    from jasper.tools import tool

    @tool()
    def universal() -> dict:
        """Visible to every provider."""
        return {}

    reg = ToolRegistry()
    reg.register(universal)
    entry = reg.to_manifest()[0]
    assert entry["compatibility"]["providers"] is None


def test_transit_tools_carry_city_and_mode_labels():
    """The transit city is a label on the tool (not a CityPack toggle) —
    the catalog will filter/sort on these. Declared order is preserved.
    See docs/tool-platform-plan.md."""
    by_name = {e["name"]: e for e in _full_registry().to_manifest()}
    assert by_name["get_subway_arrivals"]["labels"] == ["transit", "nyc", "subway"]
    assert by_name["get_bus_arrivals"]["labels"] == ["transit", "nyc", "bus"]
    assert by_name["get_citibike_status"]["labels"] == ["transit", "nyc", "bikeshare"]


def test_unlabeled_tool_emits_empty_labels():
    from jasper.tools import tool

    @tool()
    def plain() -> dict:
        """A tool with no catalog labels."""
        return {}

    reg = ToolRegistry()
    reg.register(plain)
    assert reg.to_manifest()[0]["labels"] == []
