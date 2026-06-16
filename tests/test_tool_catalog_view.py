"""jasper.tool_catalog_view — the light read/overlay side of the catalog.

The wizard (and /state) read voice's /run/jasper/tools.json and overlay the
FRESH disabled-set so the UI converges on a toggle without waiting on — or
being raced by — a voice restart. These pin the overlay + the fail-safe reads
+ the pending computation.
"""
from __future__ import annotations

import json

from jasper import tool_catalog_view as view
from jasper.tool_state import ToolState


def _write(path, payload):
    path.write_text(json.dumps(payload))


# --- read_catalog_json fail-safe -------------------------------------------

def test_read_missing_is_unavailable(tmp_path):
    out = view.read_catalog_json(str(tmp_path / "absent.json"))
    assert out["unavailable"] is True
    assert out["tools"] == []


def test_read_malformed_json_is_unavailable(tmp_path):
    p = tmp_path / "tools.json"
    p.write_text("{not json")
    assert view.read_catalog_json(str(p))["unavailable"] is True


def test_read_non_utf8_is_unavailable(tmp_path):
    # A non-UTF-8 / corrupt file raises UnicodeDecodeError (a ValueError, not
    # OSError/JSONDecodeError) — it must resolve to unavailable, never crash
    # /state, the doctor, or the wizard's /catalog.json.
    p = tmp_path / "tools.json"
    p.write_bytes(b"\xff\xfe\x00bad bytes")
    out = view.read_catalog_json(str(p))
    assert out["unavailable"] is True
    assert out["tools"] == []
    # summary() (used by /state + doctor) must stay total over the same input.
    s = view.summary(str(p), str(tmp_path / "state.env"))
    assert s["catalog_present"] is False


def test_read_wrong_shape_is_unavailable(tmp_path):
    for bad in ({}, {"tools": None}, {"tools": "nope"}, [1, 2, 3]):
        p = tmp_path / "tools.json"
        _write(p, bad)
        out = view.read_catalog_json(str(p))
        assert out["unavailable"] is True, bad
        assert out["tools"] == []


def test_read_valid_passes_through(tmp_path):
    p = tmp_path / "tools.json"
    payload = {"schema_version": 1, "tools": [{"name": "x", "status": "active"}]}
    _write(p, payload)
    assert view.read_catalog_json(str(p))["tools"] == payload["tools"]


# --- overlay ---------------------------------------------------------------

def test_overlay_flips_active_to_off_when_disabled():
    cat = {"tools": [{"name": "a", "status": "active"},
                     {"name": "b", "status": "active"}]}
    out = view.overlay(cat, frozenset({"a"}))
    by = {t["name"]: t for t in out["tools"]}
    assert by["a"]["status"] == "off"
    assert by["b"]["status"] == "active"
    assert out["pending"] is True


def test_overlay_flips_off_to_active_when_not_disabled():
    # Voice baked it 'off' (it was disabled at the last restart); the user has
    # since re-enabled it — overlay reflects 'active' and marks pending.
    cat = {"tools": [{"name": "a", "status": "off"}]}
    out = view.overlay(cat, frozenset())
    assert out["tools"][0]["status"] == "active"
    assert out["pending"] is True


def test_overlay_never_flips_needs_setup():
    # needs_setup with no setup path is a degraded/unavailable state, not an
    # optional integration opt-in, so the disabled-set must not turn it on/off.
    cat = {"tools": [{"name": "a", "status": "needs_setup"}]}
    out = view.overlay(cat, frozenset({"a"}))
    assert out["tools"][0]["status"] == "needs_setup"
    assert out["pending"] is False


def test_overlay_setup_required_pack_defaults_off_until_user_opts_in():
    cat = {"tools": [{
        "name": "home_assistant",
        "status": "needs_setup",
        "setup_url": "/ha/",
        "requires_setup": True,
        "pack": {"id": "home-assistant", "title": "Home Assistant", "summary": ""},
        "category": "Smart Home",
    }]}
    out = view.overlay(cat, ToolState())
    tool = out["tools"][0]
    pack = out["packs"][0]
    assert tool["status"] == "off"
    assert tool["disabled_by_pack"] is True
    assert tool["setup_enabled"] is False
    assert pack["status"] == "off"
    assert pack["setup_required_count"] == 1
    assert out["pending"] is False


def test_overlay_setup_required_pack_shows_needs_setup_after_opt_in():
    cat = {"tools": [{
        "name": "home_assistant",
        "status": "needs_setup",
        "setup_url": "/ha/",
        "requires_setup": True,
        "pack": {"id": "home-assistant", "title": "Home Assistant", "summary": ""},
        "category": "Smart Home",
    }]}
    out = view.overlay(
        cat,
        ToolState(setup_enabled_packs=frozenset({"home-assistant"})),
    )
    assert out["tools"][0]["status"] == "needs_setup"
    assert out["tools"][0]["setup_enabled"] is True
    assert out["packs"][0]["status"] == "needs_setup"
    assert out["pending"] is False


def test_overlay_not_pending_when_desired_matches_live():
    cat = {"tools": [{"name": "a", "status": "off"}]}
    out = view.overlay(cat, frozenset({"a"}))  # still disabled -> matches
    assert out["tools"][0]["status"] == "off"
    assert out["pending"] is False


def test_overlay_skips_non_dict_and_statusless_entries():
    cat = {"tools": ["junk", {"name": "a"}, {"status": "active"}]}
    out = view.overlay(cat, frozenset({"a"}))
    # "junk" dropped; statusless/nameless entries preserved untouched.
    assert "junk" not in out["tools"]
    assert {"name": "a"} in out["tools"]
    assert out["pending"] is False


def test_overlay_preserves_unavailable_flag():
    out = view.overlay({"tools": [], "unavailable": True}, frozenset())
    assert out["unavailable"] is True
    assert out["pending"] is False


# --- catalog_view + summary ------------------------------------------------

def test_catalog_view_reads_both_files(tmp_path):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write(cat, {"schema_version": 1, "tools": [{"name": "a", "status": "active"}]})
    state.write_text("JASPER_DISABLED_TOOLS=a\n")
    out = view.catalog_view(str(cat), str(state))
    assert out["tools"][0]["status"] == "off"
    assert out["pending"] is True


def test_overlay_pack_disable_updates_child_and_pack_status():
    cat = {"tools": [{
        "name": "spotify_play",
        "status": "active",
        "pack": {"id": "spotify", "title": "Spotify", "summary": ""},
        "category": "Music",
    }]}
    out = view.overlay(cat, ToolState(disabled_packs=frozenset({"spotify"})))
    assert out["tools"][0]["status"] == "off"
    assert out["tools"][0]["disabled_by_pack"] is True
    assert out["packs"][0]["id"] == "spotify"
    assert out["packs"][0]["status"] == "off"
    assert out["pending"] is True


def test_overlay_synthesizes_singleton_pack_for_packless_tool():
    cat = {"tools": [{
        "name": "standalone_tool",
        "status": "active",
        "summary": "Standalone summary",
        "category": "Utilities",
        "setup_url": "/standalone/",
    }]}
    out = view.overlay(cat, frozenset())
    assert out["packs"] == [{
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
        "customized_count": 0,
    }]


def test_overlay_prompt_override_updates_prompt_and_pending():
    cat = {"tools": [{
        "name": "get_weather",
        "status": "active",
        "description": "Default prompt",
        "default_description": "Default prompt",
        "pack": {"id": "weather", "title": "Weather", "summary": ""},
    }]}
    out = view.overlay(cat, frozenset(), {"get_weather": "Custom prompt"})
    assert out["tools"][0]["description"] == "Custom prompt"
    assert out["tools"][0]["prompt_customized"] is True
    assert out["packs"][0]["customized_count"] == 1
    assert out["pending"] is True


def test_summary_shape(tmp_path):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write(cat, {"schema_version": 1, "tools": [
        {"name": "a", "status": "active"}, {"name": "b", "status": "active"}]})
    state.write_text("JASPER_DISABLED_TOOLS=a\n")
    s = view.summary(str(cat), str(state))
    assert s == {
        "catalog_present": True, "count": 2,
        "pack_count": 2,
        "disabled": ["a"], "disabled_count": 1,
        "disabled_packs": [], "disabled_pack_count": 0,
        "setup_enabled_packs": [], "setup_enabled_pack_count": 0,
        "prompt_overrides": [], "prompt_override_count": 0,
        "pending": True,
    }


def test_summary_absent_catalog(tmp_path):
    s = view.summary(str(tmp_path / "absent.json"), str(tmp_path / "state.env"))
    assert s["catalog_present"] is False
    assert s["count"] == 0
    assert s["disabled"] == []
    assert s["pending"] is False
