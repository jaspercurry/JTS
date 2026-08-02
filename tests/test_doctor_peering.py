# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor peering domain."""

from pathlib import Path
from unittest.mock import patch


from jasper.cli import doctor
from jasper.config import Config


from .doctor_test_support import (
    _fresh_cfg,
)

# ---------------------------------------------------- peering doctor checks


def test_check_peering_mode_no_file_returns_ok_default(monkeypatch, tmp_path):
    """When /var/lib/jasper/peering.env doesn't exist, peering is off
    by design — the default. Doctor should return ok with a hint."""
    fake = tmp_path / "peering.env"  # does not exist
    with patch(
        "jasper.cli.doctor.peering.Path",
        side_effect=lambda p: fake if "peering.env" in p else Path(p),
    ):
        r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "off" in r.detail.lower()


def test_check_peering_mode_off_explicit(tmp_path, monkeypatch):
    """Explicit JASPER_PEERING=off — same ok status, slightly different
    message (operator made the choice deliberately)."""
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=off\n")
    monkeypatch.setattr(
        "jasper.cli.doctor.peering.Path",
        lambda p: env if "peering.env" in p else Path(p),
    )
    r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "off" in r.detail.lower()


def test_check_peering_mode_on(tmp_path, monkeypatch):
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=on\nJASPER_PEER_ROOM=kitchen\n")
    monkeypatch.setattr(
        "jasper.cli.doctor.peering.Path",
        lambda p: env if "peering.env" in p else Path(p),
    )
    r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "on" in r.detail.lower()


def test_check_peering_mode_garbage_warns(tmp_path, monkeypatch):
    """A malformed value warns the user — silent failure here would let
    a typo (JASPER_PEERING=onn) leave the user thinking peering is on
    when it actually resolved to off."""
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=banana\n")
    monkeypatch.setattr(
        "jasper.cli.doctor.peering.Path",
        lambda p: env if "peering.env" in p else Path(p),
    )
    r = doctor.check_peering_mode()
    assert r.status == "warn"
    assert "banana" in r.detail


def test_check_peering_discovery_no_peers(monkeypatch):
    """avahi-browse returns no peers — single-device mode (ok)."""
    fake_output = "+ eth0 IPv4 SomeOtherService _foo._tcp local\n"
    monkeypatch.setattr(
        "jasper.cli.doctor.shutil.which", lambda p: "/usr/bin/avahi-browse"
    )
    monkeypatch.setattr(
        "jasper.cli.doctor.peering._run",
        lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": fake_output})(),
    )
    r = doctor.check_peering_discovery()
    assert r.status == "ok"
    assert "0 sibling" in r.detail


def test_check_peering_discovery_sees_siblings(monkeypatch, tmp_path):
    """avahi-browse returns two siblings — count them, exclude self."""
    fake_output = (
        "+ eth0 IPv4 JTSpeer_alice _jasper-peer._udp local\n"
        "= eth0 IPv4 JTSpeer_alice _jasper-peer._udp local\n"
        "  hostname = [alice.local]\n"
        '  txt = ["peer_id=alice-uuid" "room=kitchen" "primary=1" "proto=1"]\n'
        "+ eth0 IPv4 JTSpeer_bob _jasper-peer._udp local\n"
        "= eth0 IPv4 JTSpeer_bob _jasper-peer._udp local\n"
        "  hostname = [bob.local]\n"
        '  txt = ["peer_id=bob-uuid" "room=bedroom" "primary=0" "proto=1"]\n'
    )
    monkeypatch.setattr(
        "jasper.cli.doctor.shutil.which", lambda p: "/usr/bin/avahi-browse"
    )
    monkeypatch.setattr(
        "jasper.cli.doctor.peering._run",
        lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": fake_output})(),
    )
    # Pretend we're alice — filter ourselves out.
    monkeypatch.setattr(
        "jasper.cli.doctor.peering._local_peer_id", lambda: "alice-uuid"
    )
    r = doctor.check_peering_discovery()
    assert r.status == "ok"
    assert "1 sibling" in r.detail
    assert "bob-uuid" in r.detail


def test_check_peering_discovery_no_avahi_browse_warns(monkeypatch):
    """Without avahi-browse we can't verify discovery — warn but
    don't fail (it's an optional dep)."""
    monkeypatch.setattr("jasper.cli.doctor.shutil.which", lambda p: None)
    r = doctor.check_peering_discovery()
    assert r.status == "warn"


# -------------------------------------------------- check_google_routes


def _routes_cfg(monkeypatch, **vars_) -> Config:
    for var in (
        "GOOGLE_ROUTES_API_KEY",
        "JASPER_TRANSIT_LAT",
        "JASPER_TRANSIT_LON",
        "JASPER_TRANSIT_DISPLAY_NAME",
        "JASPER_TRAVEL_DEFAULT_MODE",
        "JASPER_HOSTNAME",
    ):
        monkeypatch.delenv(var, raising=False)
    return _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyTest", **vars_)


def test_check_google_routes_skips_when_not_configured(monkeypatch):
    cfg = _routes_cfg(monkeypatch)
    r = doctor.check_google_routes(cfg)
    assert r.status == "ok"
    assert "not configured" in r.detail


def test_check_google_routes_warns_when_key_without_origin(monkeypatch):
    cfg = _routes_cfg(monkeypatch, GOOGLE_ROUTES_API_KEY="AIzaSySynthetic")
    r = doctor.check_google_routes(cfg)
    assert r.status == "warn"
    assert "saved speaker location is missing" in r.detail


def test_check_google_routes_warns_for_invalid_default_mode(monkeypatch):
    cfg = _routes_cfg(
        monkeypatch,
        GOOGLE_ROUTES_API_KEY="AIzaSySynthetic",
        JASPER_TRANSIT_LAT="40.758",
        JASPER_TRANSIT_LON="-73.985",
        JASPER_TRAVEL_DEFAULT_MODE="hovercraft",
    )
    r = doctor.check_google_routes(cfg)
    assert r.status == "warn"
    assert "JASPER_TRAVEL_DEFAULT_MODE is invalid" in r.detail


def test_check_google_routes_ok_when_configured(monkeypatch):
    cfg = _routes_cfg(
        monkeypatch,
        GOOGLE_ROUTES_API_KEY="AIzaSySynthetic",
        JASPER_TRANSIT_LAT="40.758",
        JASPER_TRANSIT_LON="-73.985",
        JASPER_TRAVEL_DEFAULT_MODE="drive",
    )
    r = doctor.check_google_routes(cfg)
    assert r.status == "ok"
    assert "configured for drive" in r.detail
    assert "live API probe skipped" in r.detail


# -------------------------------------------------- check_citibike


def _citibike_cfg(monkeypatch, *, stations: str = "", ebike_only: str = "") -> Config:
    """Build the standard fresh config with only Citi Bike values layered in."""
    return _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIza-stub",
        JASPER_CITIBIKE_STATIONS=stations,
        JASPER_CITIBIKE_EBIKE_ONLY=ebike_only,
    )


def test_check_citibike_skips_when_not_configured(monkeypatch):
    cfg = _citibike_cfg(monkeypatch)  # no stations saved
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "not configured" in r.detail


def test_check_citibike_ok_when_all_saved_ids_resolve(monkeypatch):
    """Saved stations all present in GBFS → ok with the count."""
    import jasper.citibike as citibike_mod

    info = {
        "data": {
            "stations": [
                {"station_id": "abc"},
                {"station_id": "def"},
            ]
        }
    }
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch,
        stations="abc|9 Av,def|Atlantic",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "2 saved station" in r.detail
    assert "e-bike-only mode" not in r.detail


def test_check_citibike_ok_renders_ebike_only_suffix(monkeypatch):
    import jasper.citibike as citibike_mod

    info = {"data": {"stations": [{"station_id": "abc"}]}}
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch,
        stations="abc|9 Av",
        ebike_only="1",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "e-bike-only mode" in r.detail


def test_check_citibike_warns_when_some_saved_ids_missing(monkeypatch):
    """One saved station retired by Lyft → warn naming the affected
    station, but don't fail (the OK ones still work)."""
    import jasper.citibike as citibike_mod

    info = {"data": {"stations": [{"station_id": "abc"}]}}  # def is gone
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch,
        stations="abc|9 Av,def|Gone Station",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "warn"
    assert "Gone Station" in r.detail
    assert "1/2" in r.detail


def test_check_citibike_fails_when_gbfs_unreachable(monkeypatch):
    import jasper.citibike as citibike_mod

    def _raise(url, ttl, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(citibike_mod, "fetch_feed", _raise)
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av")
    r = doctor.check_citibike(cfg)
    assert r.status == "fail"
    assert "GBFS unreachable" in r.detail


def test_check_citibike_caps_missing_list_at_three_with_suffix(monkeypatch):
    """When > 3 stations are missing, the detail names the first 3 and
    appends a '+N more' suffix so the line stays scannable."""
    import jasper.citibike as citibike_mod

    info = {"data": {"stations": []}}  # everything retired
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch,
        stations="a|A,b|B,c|C,d|D,e|E",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "warn"
    assert "+2 more" in r.detail
