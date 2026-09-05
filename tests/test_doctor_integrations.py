# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor integrations domain."""
from __future__ import annotations

import pytest

import jasper.citibike as citibike_mod
from jasper.cli import doctor
from jasper.cli.doctor import integrations
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
    "vars_, status, reason",
    [
        ({}, "skipped", "REASON_GOOGLE_ROUTES_NOT_CONFIGURED"),
        (
            {"GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic"},
            "warn",
            "REASON_GOOGLE_ROUTES_MISSING_SETUP",
        ),
        (
            {
                "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic",
                **_ORIGIN,
                "JASPER_TRAVEL_DEFAULT_MODE": "hovercraft",
            },
            "warn",
            "REASON_GOOGLE_ROUTES_INVALID_MODE",
        ),
        (
            {
                "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic",
                **_ORIGIN,
                "JASPER_TRAVEL_DEFAULT_MODE": "drive",
            },
            "ok",
            "REASON_GOOGLE_ROUTES_CONFIGURED",
        ),
    ],
    ids=["unconfigured", "key-without-origin", "invalid-mode", "configured"],
)
def test_check_google_routes_verdicts(monkeypatch, vars_, status, reason):
    cfg = _routes_cfg(monkeypatch, **vars_)

    r = integrations.check_google_routes(cfg)

    assert r.status == status
    assert r.reason == getattr(doctor.integrations, reason)


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
    "stations, live_ids, status, reason",
    [
        ("", (), "skipped", "REASON_CITIBIKE_NOT_CONFIGURED"),
        (
            "abc|9 Av,def|Atlantic", ("abc", "def"), "ok",
            "REASON_CITIBIKE_CONNECTED",
        ),
        # One station retired by Lyft: warn naming it, but the rest still work.
        (
            "abc|9 Av,def|Gone Station", ("abc",), "warn",
            "REASON_CITIBIKE_STATIONS_RETIRED",
        ),
        # More than three missing: still the same retirement reason.
        ("a|A,b|B,c|C,d|D,e|E", (), "warn", "REASON_CITIBIKE_STATIONS_RETIRED"),
    ],
    ids=["unconfigured", "all-resolve", "one-retired", "missing-list-capped"],
)
def test_check_citibike_verdicts(
    monkeypatch, stations, live_ids, status, reason
):
    _gbfs(monkeypatch, live_ids)
    cfg = _citibike_cfg(monkeypatch, stations=stations)

    r = integrations.check_citibike(cfg)

    assert r.status == status
    assert r.reason == getattr(doctor.integrations, reason)


@pytest.mark.parametrize(
    "ebike_only, reason",
    [
        ("", "REASON_CITIBIKE_CONNECTED"),
        ("1", "REASON_CITIBIKE_CONNECTED_EBIKE_ONLY"),
    ],
    ids=["mixed", "ebike-only"],
)
def test_check_citibike_reports_the_ebike_only_mode(monkeypatch, ebike_only, reason):
    _gbfs(monkeypatch, ("abc",))
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av", ebike_only=ebike_only)

    r = integrations.check_citibike(cfg)

    assert r.reason == getattr(doctor.integrations, reason)


def test_check_citibike_fails_when_gbfs_unreachable(monkeypatch):
    def _raise(url, ttl, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(citibike_mod, "fetch_feed", _raise)
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av")

    r = integrations.check_citibike(cfg)
    assert r.status == "fail"
    assert r.reason == doctor.integrations.REASON_CITIBIKE_GBFS_UNREACHABLE
