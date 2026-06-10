"""jasper.transit.state.read_state — fresh SSOT read for /state surfaces.

The load-bearing property is that this reader agrees with the voice daemon's
enabled_pack_ids on every case: absent key -> all packs (legacy default),
present -> exactly listed, present-but-empty -> none. A /state surface that
disagreed with the running daemon would show the dashboard a lie.
"""
from __future__ import annotations

from jasper.transit.state import read_state


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "transit.env"
    p.write_text(body)
    return str(p)


def test_absent_key_all_packs_enabled(tmp_path):
    # No JASPER_TRANSIT_CITIES line -> absent -> all packs on (non-breaking).
    state = read_state(_write(tmp_path, "JASPER_TRANSIT_LAT=40.6\n"))
    nyc = next(p for p in state["packs"] if p["id"] == "nyc")
    assert nyc["enabled"] is True
    assert nyc["label"] == "New York City"


def test_explicit_pack_enabled(tmp_path):
    state = read_state(_write(tmp_path, "JASPER_TRANSIT_CITIES=nyc\n"))
    nyc = next(p for p in state["packs"] if p["id"] == "nyc")
    assert nyc["enabled"] is True


def test_present_but_empty_means_none(tmp_path):
    # The case that must match the daemon: present-but-empty -> nothing on,
    # NOT the absent-key "all" fallback.
    state = read_state(_write(tmp_path, "JASPER_TRANSIT_CITIES=\n"))
    assert state["packs"]
    assert all(p["enabled"] is False for p in state["packs"])


def test_unknown_pack_id_ignored(tmp_path):
    state = read_state(_write(tmp_path, "JASPER_TRANSIT_CITIES=berlin\n"))
    assert all(p["enabled"] is False for p in state["packs"])


def test_missing_file_is_total_and_defaults_to_all(tmp_path):
    # Missing file -> {} -> absent default (all enabled). Never raises.
    state = read_state(str(tmp_path / "nope.env"))
    assert state["packs"]
    assert all(p["enabled"] for p in state["packs"])


def test_shape_is_stable(tmp_path):
    state = read_state(_write(tmp_path, "JASPER_TRANSIT_CITIES=nyc\n"))
    assert set(state) == {"packs"}
    for row in state["packs"]:
        assert set(row) == {"id", "label", "enabled"}
        assert isinstance(row["enabled"], bool)
