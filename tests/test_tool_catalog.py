"""jasper.tools.catalog — full catalog enumeration + status computation.

The catalog lists EVERY first-party tool (needs_setup ones via
gate-satisfying sentinel deps) and computes each tool's status from set
membership: live registry (configured + enabled), disabled-set, or
neither (needs_setup). Pins the /run JSON shape the /tools/ wizard reads.
"""
from __future__ import annotations

import json

from jasper.tools import Tool, ToolDefinition, ToolRegistry
from jasper.tools.catalog import (
    _CATALOG_HIDDEN,
    CATALOG_SCHEMA_VERSION,
    _build_pack_payloads,
    _full_catalog_registry,
    build_catalog,
    write_catalog,
)
from jasper.tool_state import ToolState
from jasper.tools.packs import CapabilityPack, CatalogPack, register_packs
from tests._tool_pack_contract import (
    ALWAYS_ON_TOOL_NAMES,
    GATED_TOOL_NAMES,
    VISIBLE_TOOL_NAMES,
    full_registry,
    minimal_registry,
)


def test_full_registry_empty_disabled_all_active():
    cat = build_catalog(full_registry(), frozenset())
    assert cat["schema_version"] == CATALOG_SCHEMA_VERSION
    assert len(cat["tools"]) == len(VISIBLE_TOOL_NAMES)
    assert all(t["status"] == "active" for t in cat["tools"])
    # Hidden companion tools never get a catalog card.
    names = {t["name"] for t in cat["tools"]}
    assert names == VISIBLE_TOOL_NAMES
    assert _CATALOG_HIDDEN and not (_CATALOG_HIDDEN & names)


def test_catalog_includes_display_metadata_for_pack_first_ui():
    cat = build_catalog(full_registry(), frozenset())
    by_name = {
        t["name"]: t
        for t in cat["tools"]
    }
    by_pack = {p["id"]: p for p in cat["packs"]}

    spotify = by_name["spotify_play"]
    assert spotify["category"] == "Music"
    assert spotify["pack"] == {
        "id": "spotify",
        "title": "Spotify",
        "summary": "Search, play, and queue music through configured Spotify accounts.",
        "setup_url": "/spotify/",
    }

    # Multiple internal registration packs can share one display pack.
    assert by_name["calendar_today_summary"]["pack"]["id"] == "google"
    assert by_name["gmail_unread_summary"]["pack"]["id"] == "google"

    # Single-tool capabilities still get a display pack so /tools/ can render
    # one stable top-level card per user-facing capability.
    assert by_name["get_weather"]["category"] == "Utilities"
    assert by_name["get_weather"]["pack"]["id"] == "weather"
    assert by_name["get_weather"]["pack"]["setup_url"] == "/weather/"
    assert by_name["get_current_time"]["category"] == "Utilities"
    assert by_name["get_current_time"]["pack"]["id"] == "time"

    assert by_pack["spotify"]["tool_count"] == 3
    assert by_pack["weather"]["tool_names"] == ["get_weather"]
    assert by_pack["time"]["tool_names"] == ["get_current_time"]

    assert by_name["get_weather"]["summary"]
    assert "\n" not in by_name["get_weather"]["summary"]
    assert len(by_name["get_weather"]["summary"]) <= 183  # 180 + "..."
    assert by_name["get_weather"]["details"]
    assert by_name["get_weather"]["default_description"]
    assert by_name["get_weather"]["prompt_customized"] is False


def test_explicit_capability_pack_flows_through_catalog():
    """A source-neutral pack reaches the catalog, not just dispatch."""
    class RecordingExecutor:
        async def execute(self, args):
            return {"echo": args["text"]}

    explicit = Tool(
        definition=ToolDefinition(
            name="contrib_echo",
            description="Echo contributor input.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            labels=("contrib", "example"),
            timeout=3.0,
        ),
        executor=RecordingExecutor(),
    )
    pack = CapabilityPack(
        "contrib_echo",
        lambda _d: [explicit],
        category="Examples",
        catalog_pack=CatalogPack(
            "contrib-echo",
            "Contributor Echo",
            "Example contributor capability.",
            setup_url="/contrib/",
            setup_required=True,
        ),
    )
    live = ToolRegistry()
    register_packs(
        live,
        object(),
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(pack,),
    )
    cat = build_catalog(live, frozenset(), packs=(pack,))

    row = cat["tools"][0]
    assert row["name"] == "contrib_echo"
    assert row["status"] == "active"
    assert row["category"] == "Examples"
    assert row["labels"] == ["contrib", "example"]
    assert row["timeout"] == 3.0
    assert row["pack"] == {
        "id": "contrib-echo",
        "title": "Contributor Echo",
        "summary": "Example contributor capability.",
        "setup_url": "/contrib/",
    }
    assert row["setup_url"] == "/contrib/"
    assert row["requires_setup"] is False
    assert cat["packs"][0]["id"] == "contrib-echo"

    needs_setup = build_catalog(ToolRegistry(), frozenset(), packs=(pack,))
    setup_row = needs_setup["tools"][0]
    assert setup_row["status"] == "needs_setup"
    assert setup_row["setup_url"] == "/contrib/"
    assert setup_row["requires_setup"] is True


def test_disabled_pack_disables_all_child_tools():
    cat = build_catalog(
        full_registry(),
        frozenset(),
        disabled_packs=frozenset({"spotify"}),
    )
    by_name = {t["name"]: t for t in cat["tools"]}
    assert by_name["spotify_play"]["status"] == "off"
    assert by_name["spotify_queue"]["status"] == "off"
    assert by_name["spotify_play"]["disabled_by_pack"] is True
    by_pack = {p["id"]: p for p in cat["packs"]}
    assert by_pack["spotify"]["status"] == "off"


def test_full_catalog_registry_ignores_staged_disabled_packs(monkeypatch):
    """Full schema enumeration must keep disabled packs visible to re-enable."""
    import jasper.tool_state as tool_state

    monkeypatch.setattr(
        tool_state,
        "read_tool_state",
        lambda: ToolState(disabled_packs=frozenset({"weather"})),
    )

    reg = _full_catalog_registry()
    assert "get_weather" in reg.tools

    cat = build_catalog(
        full_registry(disabled_packs=frozenset({"weather"})),
        frozenset(),
        disabled_packs=frozenset({"weather"}),
    )
    by_name = {t["name"]: t for t in cat["tools"]}
    by_pack = {p["id"]: p for p in cat["packs"]}
    assert by_name["get_weather"]["status"] == "off"
    assert by_name["get_weather"]["disabled_by_pack"] is True
    assert by_pack["weather"]["status"] == "off"


def test_pack_payloads_synthesize_singleton_for_packless_tool():
    packs = _build_pack_payloads([{
        "name": "standalone_tool",
        "summary": "Standalone summary",
        "category": "Utilities",
        "status": "active",
        "setup_url": "/standalone/",
        "prompt_customized": True,
    }])
    assert packs == [{
        "id": "tool:standalone_tool",
        "title": "standalone_tool",
        "summary": "Standalone summary",
        "setup_url": "/standalone/",
        "category": "Utilities",
        "tool_names": ["standalone_tool"],
        "singleton_tool_name": "standalone_tool",
        "status": "active",
        "tool_count": 1,
        "active_count": 1,
        "off_count": 0,
        "needs_setup_count": 0,
        "setup_required_count": 0,
        "customized_count": 1,
    }]


def test_prompt_overrides_surface_with_reset_metadata():
    cat = build_catalog(
        full_registry(),
        frozenset(),
        prompt_overrides={"get_weather": "Use pirate weather."},
    )
    by_name = {t["name"]: t for t in cat["tools"]}
    assert by_name["get_weather"]["description"] == "Use pirate weather."
    assert by_name["get_weather"]["default_description"] != "Use pirate weather."
    assert by_name["get_weather"]["prompt_customized"] is True
    by_pack = {p["id"]: p for p in cat["packs"]}
    assert by_pack["weather"]["customized_count"] == 1


def test_visible_first_party_tools_have_search_labels():
    cat = build_catalog(full_registry(), frozenset())
    missing = [t["name"] for t in cat["tools"] if not t["labels"]]
    assert not missing


def test_minimal_registry_gated_tools_need_setup():
    cat = build_catalog(minimal_registry(), frozenset())
    by_name = {t["name"]: t for t in cat["tools"]}
    assert len(by_name) == len(VISIBLE_TOOL_NAMES)
    for name in GATED_TOOL_NAMES - _CATALOG_HIDDEN:
        assert by_name[name]["status"] == "needs_setup", name
        if by_name[name]["setup_url"]:
            assert by_name[name]["requires_setup"] is True
    for name in ALWAYS_ON_TOOL_NAMES:
        assert by_name[name]["status"] == "active", name
        assert by_name[name]["requires_setup"] is False


def test_needs_setup_setup_urls_map_to_right_wizard():
    cat = build_catalog(minimal_registry(), frozenset())
    by_name = {t["name"]: t for t in cat["tools"]}
    by_pack = {p["id"]: p for p in cat["packs"]}
    assert by_name["gmail_unread_summary"]["setup_url"] == "/google/"
    assert by_name["calendar_today_summary"]["setup_url"] == "/google/"
    assert by_name["home_assistant"]["setup_url"] == "/ha/"
    assert by_name["get_subway_arrivals"]["setup_url"] == "/transit/"
    assert by_name["get_bus_arrivals"]["setup_url"] == "/transit/"
    assert by_name["get_citibike_status"]["setup_url"] == "/transit/"
    # Weather is active even without a default (explicit place names still work);
    # its Configure page is pack-level metadata for bare-location defaults.
    assert by_pack["weather"]["setup_url"] == "/weather/"
    # Other core tools carry no setup wizard.
    assert by_name["get_current_time"]["setup_url"] is None
    assert by_name["set_timer"]["setup_url"] is None
    assert by_name["get_volume"]["setup_url"] is None


def test_setup_required_state_is_owned_by_pack_metadata():
    cat = build_catalog(minimal_registry(), frozenset())
    by_name = {t["name"]: t for t in cat["tools"]}

    # A future contributor should set CatalogPack(setup_required=True,
    # setup_url=...) once, not add every child tool to a central table.
    for name in (
        "gmail_unread_summary",
        "calendar_today_summary",
        "home_assistant",
        "get_subway_arrivals",
        "get_bus_arrivals",
        "get_citibike_status",
    ):
        pack = by_name[name]["pack"]
        assert by_name[name]["setup_url"] == pack["setup_url"]
        assert by_name[name]["requires_setup"] is True


def test_configured_but_disabled_renders_off():
    disabled = frozenset({"get_weather"})
    by_name = {
        t["name"]: t
        for t in build_catalog(full_registry(), disabled)["tools"]
    }
    assert by_name["get_weather"]["status"] == "off"
    assert by_name["spotify_play"]["status"] == "active"


def test_unconfigured_and_disabled_renders_off_edge_case():
    """A tool BOTH unconfigured (not in live registry) AND in the
    disabled-set renders 'off' — documented edge case."""
    by_name = {
        t["name"]: t
        for t in build_catalog(minimal_registry(), frozenset({"home_assistant"}))[
            "tools"
        ]
    }
    assert by_name["home_assistant"]["status"] == "off"


def test_full_catalog_registry_enumerates_all_tools():
    # The registry holds EVERY tool (incl. hidden companions); the catalog
    # UI is what hides some. So the registry count = ALWAYS_ON + GATED, even
    # though build_catalog emits fewer cards.
    reg = _full_catalog_registry()
    assert len(reg.tools) == len(ALWAYS_ON_TOOL_NAMES) + len(GATED_TOOL_NAMES)
    for hidden in _CATALOG_HIDDEN:
        assert hidden in reg.tools, hidden


def test_hidden_tools_are_in_registry_but_not_the_catalog():
    reg = _full_catalog_registry()
    cat_names = {
        t["name"]
        for t in build_catalog(full_registry(), frozenset())["tools"]
    }
    for hidden in _CATALOG_HIDDEN:
        assert hidden in reg.tools, f"{hidden} must stay a real registry tool"
        assert hidden not in cat_names, f"{hidden} must be hidden from the catalog"


def test_providers_none_for_universal_and_sorted_for_restricted():
    cat = build_catalog(full_registry(), frozenset())
    for t in cat["tools"]:
        # No shipped tool is provider-restricted today, so all are None;
        # but the shape must be `sorted(...)` (a list) or None, never a
        # set (unstable order, not JSON-serializable).
        assert t["providers"] is None or t["providers"] == sorted(t["providers"])


def test_write_catalog_round_trips_to_build_catalog(tmp_path):
    reg = full_registry()
    disabled = frozenset({"get_weather"})
    path = tmp_path / "tools.json"
    write_catalog(reg, disabled, path=str(path))
    on_disk = json.loads(path.read_text())
    assert on_disk == build_catalog(reg, disabled)


def test_write_catalog_fail_soft_on_unwritable_path(caplog):
    """A write error must NOT raise — the daemon must boot even if /run
    isn't writable in a dev environment."""
    reg = minimal_registry()
    bad = "/nonexistent-root-dir-xyz/tools.json"
    with caplog.at_level("WARNING"):
        write_catalog(reg, frozenset(), path=bad)  # must not raise
    assert any("tool_catalog.write_failed" in r.message for r in caplog.records)


def test_pack_setup_required_metadata_has_setup_urls():
    """A setup-required pack without a URL would strand needs_setup tools."""
    from jasper.tools.packs import TOOL_PACKS

    broken = [
        p.name
        for p in TOOL_PACKS
        if p.catalog_pack is not None
        and p.catalog_pack.setup_required
        and not p.catalog_pack.setup_url
    ]
    assert not broken
