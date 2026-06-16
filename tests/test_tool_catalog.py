"""jasper.tools.catalog — full catalog enumeration + status computation.

The catalog lists EVERY first-party tool (needs_setup ones via
gate-satisfying sentinel deps) and computes each tool's status from set
membership: live registry (configured + enabled), disabled-set, or
neither (needs_setup). Pins the /run JSON shape the /tools/ wizard reads.
"""
from __future__ import annotations

import json
import types

from jasper.tools import ToolRegistry
from jasper.tools.bus import make_bus_tools
from jasper.tools.catalog import (
    _CATALOG_HIDDEN,
    CATALOG_SCHEMA_VERSION,
    _SETUP_URLS,
    _full_catalog_registry,
    build_catalog,
    write_catalog,
)
from jasper.tools.citibike import make_citibike_tools
from jasper.tools.packs import ToolDeps, register_packs
from jasper.tools.subway import make_subway_tools

# The always-on tools (no backend gate) — audio + transport + spotify +
# weather + time. Mirrors test_tool_packs_registry's un-gated survivors.
ALWAYS_ON = {
    "get_volume", "set_volume", "adjust_volume", "mute", "unmute",
    "next_track", "previous_track", "pause", "resume", "get_now_playing",
    "spotify_play", "spotify_play_latest_by_artist", "spotify_queue",
    "get_weather", "get_current_time",
}

# Tools that only register when their backend is configured.
GATED = {
    "get_subway_arrivals", "get_bus_arrivals", "get_citibike_status",
    "home_assistant", "home_assistant_confirm",
    "set_timer", "list_timers", "cancel_timer", "update_timer",
    "calendar_today_summary", "calendar_upcoming",
    "gmail_unread_summary", "gmail_read_thread",
    "flag_recent_issue",
}

# Every registered tool, minus the ones hidden from the catalog UI
# (home_assistant_confirm is an internal companion — see _CATALOG_HIDDEN).
VISIBLE = (ALWAYS_ON | GATED) - _CATALOG_HIDDEN


def _full_live_registry() -> ToolRegistry:
    """Every tool, configured + enabled (mirrors _full_catalog_registry's
    sentinel deps)."""
    transit = []
    transit += list(make_subway_tools(object()))
    transit += list(make_bus_tools(types.SimpleNamespace(enabled=True)))
    transit += list(make_citibike_tools(types.SimpleNamespace(enabled=True)))
    deps = ToolDeps(
        volume_coordinator=None, renderer=None, router=None, weather=None,
        spotify_device_name="JTS", spotify_setup_url="",
        transit_tools=transit, ha=object(), timer_scheduler=object(),
        google_clients=types.SimpleNamespace(list_account_names=lambda: ["seed"]),
        wake_event_store=object(),
    )
    reg = ToolRegistry()
    register_packs(reg, deps, disabled=frozenset())
    return reg


def _minimal_live_registry() -> ToolRegistry:
    """Only the always-on tools register — every gated backend absent."""
    deps = ToolDeps(
        volume_coordinator=None, renderer=None, router=None, weather=None,
        spotify_device_name="JTS", spotify_setup_url="",
        transit_tools=[], ha=None, timer_scheduler=None,
        google_clients=types.SimpleNamespace(list_account_names=lambda: []),
        wake_event_store=None,
    )
    reg = ToolRegistry()
    register_packs(reg, deps, disabled=frozenset())
    return reg


def test_full_registry_empty_disabled_all_active():
    cat = build_catalog(_full_live_registry(), frozenset())
    assert cat["schema_version"] == CATALOG_SCHEMA_VERSION
    assert len(cat["tools"]) == len(VISIBLE)
    assert all(t["status"] == "active" for t in cat["tools"])
    # Hidden companion tools never get a catalog card.
    names = {t["name"] for t in cat["tools"]}
    assert names == VISIBLE
    assert _CATALOG_HIDDEN and not (_CATALOG_HIDDEN & names)


def test_catalog_includes_display_metadata_without_forcing_packs():
    by_name = {
        t["name"]: t
        for t in build_catalog(_full_live_registry(), frozenset())["tools"]
    }

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

    # Standalone tools deliberately have no pack; the UI groups them directly
    # under their category rather than inventing a fake display pack.
    assert by_name["get_weather"]["category"] == "Utilities"
    assert by_name["get_weather"]["pack"] is None
    assert by_name["get_current_time"]["category"] == "Utilities"
    assert by_name["get_current_time"]["pack"] is None

    assert by_name["get_weather"]["summary"]
    assert "\n" not in by_name["get_weather"]["summary"]
    assert len(by_name["get_weather"]["summary"]) <= 183  # 180 + "..."
    assert by_name["get_weather"]["details"]


def test_visible_first_party_tools_have_search_labels():
    cat = build_catalog(_full_live_registry(), frozenset())
    missing = [t["name"] for t in cat["tools"] if not t["labels"]]
    assert not missing


def test_minimal_registry_gated_tools_need_setup():
    cat = build_catalog(_minimal_live_registry(), frozenset())
    by_name = {t["name"]: t for t in cat["tools"]}
    assert len(by_name) == len(VISIBLE)
    for name in GATED - _CATALOG_HIDDEN:
        assert by_name[name]["status"] == "needs_setup", name
    for name in ALWAYS_ON:
        assert by_name[name]["status"] == "active", name


def test_needs_setup_setup_urls_map_to_right_wizard():
    by_name = {t["name"]: t for t in build_catalog(_minimal_live_registry(), frozenset())["tools"]}
    assert by_name["gmail_unread_summary"]["setup_url"] == "/google/"
    assert by_name["calendar_today_summary"]["setup_url"] == "/google/"
    assert by_name["home_assistant"]["setup_url"] == "/ha/"
    assert by_name["get_subway_arrivals"]["setup_url"] == "/transit/"
    assert by_name["get_bus_arrivals"]["setup_url"] == "/transit/"
    assert by_name["get_citibike_status"]["setup_url"] == "/transit/"
    # Core tools carry no setup wizard.
    assert by_name["get_current_time"]["setup_url"] is None
    assert by_name["set_timer"]["setup_url"] is None
    assert by_name["get_volume"]["setup_url"] is None


def test_configured_but_disabled_renders_off():
    disabled = frozenset({"get_weather"})
    by_name = {t["name"]: t for t in build_catalog(_full_live_registry(), disabled)["tools"]}
    assert by_name["get_weather"]["status"] == "off"
    assert by_name["spotify_play"]["status"] == "active"


def test_unconfigured_and_disabled_renders_off_edge_case():
    """A tool BOTH unconfigured (not in live registry) AND in the
    disabled-set renders 'off' — documented edge case."""
    by_name = {
        t["name"]: t
        for t in build_catalog(_minimal_live_registry(), frozenset({"home_assistant"}))["tools"]
    }
    assert by_name["home_assistant"]["status"] == "off"


def test_full_catalog_registry_enumerates_all_tools():
    # The registry holds EVERY tool (incl. hidden companions); the catalog
    # UI is what hides some. So the registry count = ALWAYS_ON + GATED, even
    # though build_catalog emits fewer cards.
    reg = _full_catalog_registry()
    assert len(reg.tools) == len(ALWAYS_ON) + len(GATED)
    for hidden in _CATALOG_HIDDEN:
        assert hidden in reg.tools, hidden


def test_hidden_tools_are_in_registry_but_not_the_catalog():
    reg = _full_catalog_registry()
    cat_names = {t["name"] for t in build_catalog(_full_live_registry(), frozenset())["tools"]}
    for hidden in _CATALOG_HIDDEN:
        assert hidden in reg.tools, f"{hidden} must stay a real registry tool"
        assert hidden not in cat_names, f"{hidden} must be hidden from the catalog"


def test_providers_none_for_universal_and_sorted_for_restricted():
    cat = build_catalog(_full_live_registry(), frozenset())
    for t in cat["tools"]:
        # No shipped tool is provider-restricted today, so all are None;
        # but the shape must be `sorted(...)` (a list) or None, never a
        # set (unstable order, not JSON-serializable).
        assert t["providers"] is None or t["providers"] == sorted(t["providers"])


def test_write_catalog_round_trips_to_build_catalog(tmp_path):
    reg = _full_live_registry()
    disabled = frozenset({"get_weather"})
    path = tmp_path / "tools.json"
    write_catalog(reg, disabled, path=str(path))
    on_disk = json.loads(path.read_text())
    assert on_disk == build_catalog(reg, disabled)


def test_write_catalog_fail_soft_on_unwritable_path(caplog):
    """A write error must NOT raise — the daemon must boot even if /run
    isn't writable in a dev environment."""
    reg = _minimal_live_registry()
    bad = "/nonexistent-root-dir-xyz/tools.json"
    with caplog.at_level("WARNING"):
        write_catalog(reg, frozenset(), path=bad)  # must not raise
    assert any("tool_catalog.write_failed" in r.message for r in caplog.records)


def test_setup_url_keys_are_real_tool_names():
    """Guard against a tool rename leaving a stale _SETUP_URLS key."""
    catalog_names = set(_full_catalog_registry().tools.keys())
    stale = set(_SETUP_URLS) - catalog_names
    assert not stale, f"_SETUP_URLS keys not in the catalog: {sorted(stale)}"
