# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor integrations domain."""
from __future__ import annotations

import pytest

import jasper.citibike as citibike_mod
from jasper.cli import doctor
from jasper.config import Config

from .doctor_test_support import _fresh_cfg

# ------------------------------------------------------------ Google Routes

_ROUTES_VARS = (
    "GOOGLE_ROUTES_API_KEY",
    "JASPER_TRANSIT_LAT",
    "JASPER_TRANSIT_LON",
    "JASPER_TRANSIT_DISPLAY_NAME",
    "JASPER_TRAVEL_DEFAULT_MODE",
    "JASPER_HOSTNAME",
)

_ORIGIN = {"JASPER_TRANSIT_LAT": "40.758", "JASPER_TRANSIT_LON": "-73.985"}


def _routes_cfg(monkeypatch, **vars_) -> Config:
    for var in _ROUTES_VARS:
        monkeypatch.delenv(var, raising=False)
    return _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyTest", **vars_)


@pytest.mark.parametrize(
    "vars_, status, must_name",
    [
        ({}, "ok", "not configured"),
        ({"GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic"}, "warn", "speaker location"),
        (
            {
                "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic",
                **_ORIGIN,
                "JASPER_TRAVEL_DEFAULT_MODE": "hovercraft",
            },
            "warn",
            "JASPER_TRAVEL_DEFAULT_MODE",
        ),
        (
            {
                "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic",
                **_ORIGIN,
                "JASPER_TRAVEL_DEFAULT_MODE": "drive",
            },
            "ok",
            "configured for drive",
        ),
    ],
    ids=["unconfigured", "key-without-origin", "invalid-mode", "configured"],
)
def test_check_google_routes_verdicts(monkeypatch, vars_, status, must_name):
    cfg = _routes_cfg(monkeypatch, **vars_)

    r = doctor.check_google_routes(cfg)

    assert r.status == status
    assert must_name in r.detail


# ---------------------------------------------------------------- Citi Bike


def _citibike_cfg(monkeypatch, *, stations: str = "", ebike_only: str = "") -> Config:
    return _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIza-stub",
        JASPER_CITIBIKE_STATIONS=stations,
        JASPER_CITIBIKE_EBIKE_ONLY=ebike_only,
    )


def _gbfs(monkeypatch, station_ids):
    feed = {"data": {"stations": [{"station_id": s} for s in station_ids]}}
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: feed)


@pytest.mark.parametrize(
    "stations, live_ids, status, must_name",
    [
        ("", (), "ok", "not configured"),
        ("abc|9 Av,def|Atlantic", ("abc", "def"), "ok", "2 saved station"),
        # One station retired by Lyft: warn naming it, but the rest still work.
        ("abc|9 Av,def|Gone Station", ("abc",), "warn", "Gone Station"),
        # More than three missing: the detail stays scannable.
        ("a|A,b|B,c|C,d|D,e|E", (), "warn", "+2 more"),
    ],
    ids=["unconfigured", "all-resolve", "one-retired", "missing-list-capped"],
)
def test_check_citibike_verdicts(
    monkeypatch, stations, live_ids, status, must_name
):
    _gbfs(monkeypatch, live_ids)
    cfg = _citibike_cfg(monkeypatch, stations=stations)

    r = doctor.check_citibike(cfg)

    assert r.status == status
    assert must_name in r.detail


@pytest.mark.parametrize("ebike_only", ["", "1"], ids=["mixed", "ebike-only"])
def test_check_citibike_reports_the_ebike_only_mode(monkeypatch, ebike_only):
    _gbfs(monkeypatch, ("abc",))
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av", ebike_only=ebike_only)

    detail = doctor.check_citibike(cfg).detail

    assert ("e-bike-only mode" in detail) is bool(ebike_only)


def test_check_citibike_fails_when_gbfs_unreachable(monkeypatch):
    def _raise(url, ttl, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(citibike_mod, "fetch_feed", _raise)
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av")

    assert doctor.check_citibike(cfg).status == "fail"
