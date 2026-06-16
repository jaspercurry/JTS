"""jasper.tool_catalog_view — the light read/overlay side of the catalog.

The wizard (and /state) read voice's /run/jasper/tools.json and overlay the
FRESH disabled-set so the UI converges on a toggle without waiting on — or
being raced by — a voice restart. These pin the overlay + the fail-safe reads
+ the pending computation.
"""
from __future__ import annotations

import json

from jasper import tool_catalog_view as view


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
    # needs_setup is config-derived; the disabled-set must not turn it on/off.
    cat = {"tools": [{"name": "a", "status": "needs_setup"}]}
    out = view.overlay(cat, frozenset({"a"}))
    assert out["tools"][0]["status"] == "needs_setup"
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


def test_summary_shape(tmp_path):
    cat = tmp_path / "tools.json"
    state = tmp_path / "state.env"
    _write(cat, {"schema_version": 1, "tools": [
        {"name": "a", "status": "active"}, {"name": "b", "status": "active"}]})
    state.write_text("JASPER_DISABLED_TOOLS=a\n")
    s = view.summary(str(cat), str(state))
    assert s == {
        "catalog_present": True, "count": 2,
        "disabled": ["a"], "disabled_count": 1, "pending": True,
    }


def test_summary_absent_catalog(tmp_path):
    s = view.summary(str(tmp_path / "absent.json"), str(tmp_path / "state.env"))
    assert s["catalog_present"] is False
    assert s["count"] == 0
    assert s["disabled"] == []
    assert s["pending"] is False
