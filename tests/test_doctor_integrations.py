# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor integrations domain."""
from __future__ import annotations

import pytest

import jasper.citibike as citibike_mod
import jasper.home_assistant as ha_mod
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
        ({}, "ok", "REASON_GOOGLE_ROUTES_NOT_CONFIGURED"),
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
    assert r.reason == getattr(integrations, reason)


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
        ("", (), "ok", "REASON_CITIBIKE_NOT_CONFIGURED"),
        (
            "abc|9 Av,def|Atlantic", ("abc", "def"), "ok",
            "REASON_CITIBIKE_CONNECTED",
        ),
        # One station retired by Lyft: named, but the rest still work.
        (
            "abc|9 Av,def|Gone Station", ("abc",), "ok",
            "REASON_CITIBIKE_STATIONS_RETIRED",
        ),
        # More than three missing: still the same retirement reason.
        ("a|A,b|B,c|C,d|D,e|E", (), "ok", "REASON_CITIBIKE_STATIONS_RETIRED"),
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
    assert r.reason == getattr(integrations, reason)


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

    assert r.reason == getattr(integrations, reason)


def test_check_citibike_warns_when_gbfs_unreachable(monkeypatch):
    def _raise(url, ttl, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(citibike_mod, "fetch_feed", _raise)
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av")

    r = integrations.check_citibike(cfg)
    assert r.status == "warn"
    assert r.reason == integrations.REASON_CITIBIKE_GBFS_UNREACHABLE


# ----------------------------------------------------------- Home Assistant


def _ha_cfg(monkeypatch, *, url: str = "", token: str = "") -> Config:
    return _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIza-stub",
        JASPER_HA_URL=url,
        JASPER_HA_TOKEN=token,
    )


def _ha_probe(monkeypatch, result=None, raises: Exception | None = None):
    async def fake_probe(url, token, *, force=False, verify_ssl=True):
        if raises is not None:
            raise raises
        return dict(result or {}, url=url)

    monkeypatch.setattr(ha_mod, "probe_status", fake_probe)


_CONNECTED = {
    "configured": True, "connected": True,
    "instance_name": "Brooklyn House", "version": "2026.5.1", "error": None,
}
_UNREACHABLE = {
    "configured": True, "connected": False,
    "instance_name": None, "version": None, "error": "no route to host",
}


@pytest.mark.parametrize(
    "creds, probe, status, reason",
    [
        ({}, None, "ok", "REASON_HOME_ASSISTANT_NOT_CONFIGURED"),
        (
            {"url": "http://ha.local:8123", "token": "t"},
            {"result": _CONNECTED},
            "ok",
            "REASON_HOME_ASSISTANT_CONNECTED",
        ),
        (
            {"url": "http://ha.local:8123", "token": "t"},
            {"result": _UNREACHABLE},
            "warn",
            "REASON_HOME_ASSISTANT_UNREACHABLE",
        ),
        (
            {"url": "http://ha.local:8123", "token": "t"},
            {"raises": RuntimeError("network stack exploded")},
            "warn",
            "REASON_HOME_ASSISTANT_PROBE_RAISED",
        ),
    ],
    ids=["unconfigured", "connected", "unreachable", "probe-raised"],
)
def test_check_home_assistant_verdicts(
    monkeypatch, creds, probe, status, reason
):
    """An integration is not the speaker: an unreachable or exploding
    Home Assistant is a warn a healer can act on, and never configuring one
    is operator intent."""
    if probe is not None:
        _ha_probe(monkeypatch, **probe)

    r = integrations.check_home_assistant(_ha_cfg(monkeypatch, **creds))

    assert r.status == status
    assert r.reason == getattr(integrations, reason)


def test_check_home_assistant_probe_raised_redacts_credential_shaped_text(
    monkeypatch,
):
    """probe_status runs with the live ha_token; a raised exception's text
    must route through `_exception_detail` (redact + cap) like every other
    crash branch, not bare `{e}`."""
    _ha_probe(
        monkeypatch,
        raises=RuntimeError("token=abcdef0123456789 unreachable"),
    )
    creds = {"url": "http://ha.local:8123", "token": "t"}

    r = integrations.check_home_assistant(_ha_cfg(monkeypatch, **creds))

    assert r.status == "warn"
    assert r.reason == integrations.REASON_HOME_ASSISTANT_PROBE_RAISED
    assert "abcdef0123456789" not in r.detail
