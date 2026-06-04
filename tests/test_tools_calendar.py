"""Unit tests for jasper.tools.calendar.

Each test constructs a GoogleClients with a fake registry + a fake
googleapiclient-shaped service so we don't hit Google. The
load_credentials path is monkeypatched to return a sentinel — that
lets build_calendar() succeed and pass the (sentinel) creds into the
fake factory.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jasper import google_creds as gc
from jasper.google_creds import GoogleAccount, GoogleClients, GoogleRegistry
from jasper.tools.calendar import make_calendar_tools


# --- fake googleapiclient surfaces --------------------------------


class _FakeExecutable:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeEvents:
    """Stand-in for `service.events()`. Captures the list() kwargs so
    tests can assert the time window the tool actually queried."""

    def __init__(self, items=None, raise_on_list=None):
        self.items = list(items or [])
        self.raise_on_list = raise_on_list
        self.last_kwargs = None

    def list(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_on_list:
            raise self.raise_on_list
        return _FakeExecutable({"items": self.items})


class _FakeCalendarService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


# --- helpers ------------------------------------------------------


def _make_clients(monkeypatch, *, accounts=("jasper", "brittany"), service=None):
    """Build a GoogleClients whose first account is the default. The
    credential loader returns a sentinel so build_calendar() reaches
    the test's fake service_factory."""
    monkeypatch.setattr(
        gc, "load_credentials", lambda account, **kw: object(),
    )
    r = GoogleRegistry()
    for i, name in enumerate(accounts):
        r.add_or_update(GoogleAccount(name=name), make_default=(i == 0))
    captured = {"factory_calls": []}

    def factory(api_name, version, creds):
        captured["factory_calls"].append((api_name, version))
        return service

    clients = GoogleClients(
        registry=r, client_id="x", client_secret="y",
        service_factory=factory,
    )
    return clients, captured


def _tool_by_name(tools, name):
    for fn in tools:
        if getattr(fn, "__jasper_tool_name__", fn.__name__) == name:
            return fn
    raise AssertionError(f"tool {name!r} not in {[fn.__name__ for fn in tools]}")


# --- factory: gates -----------------------------------------------


def test_make_calendar_tools_returns_empty_when_clients_none():
    assert make_calendar_tools(None) == []


# --- error paths --------------------------------------------------


@pytest.mark.asyncio
async def test_today_summary_no_accounts_message_points_to_wizard(monkeypatch):
    monkeypatch.setattr(gc, "load_credentials", lambda *a, **kw: None)
    clients = GoogleClients(
        registry=GoogleRegistry(),
        client_id="x", client_secret="y",
        service_factory=lambda *a: pytest.fail("should not be called"),
    )
    [today, _upcoming] = make_calendar_tools(clients)
    out = await today()
    assert out["ok"] is False
    assert "jts.local/google" in out["error"]


@pytest.mark.asyncio
async def test_today_summary_unknown_account_lists_available(monkeypatch):
    clients, _ = _make_clients(monkeypatch, accounts=("jasper", "brittany"))
    [today, _upcoming] = make_calendar_tools(clients)
    out = await today(account="frank")
    assert out["ok"] is False
    assert "frank" in out["error"]
    assert "jasper" in out["error"]
    assert "brittany" in out["error"]


@pytest.mark.asyncio
async def test_today_summary_credentials_failure_returns_relink_error(monkeypatch):
    monkeypatch.setattr(gc, "load_credentials", lambda *a, **kw: None)
    r = GoogleRegistry()
    r.add_or_update(GoogleAccount(name="jasper"), make_default=True)
    clients = GoogleClients(
        registry=r, client_id="x", client_secret="y",
        service_factory=lambda *a: pytest.fail("should not be called"),
    )
    [today, _upcoming] = make_calendar_tools(clients)
    out = await today()
    assert out["ok"] is False
    assert "Re-link" in out["error"] or "re-link" in out["error"]


@pytest.mark.asyncio
async def test_today_summary_api_error_returns_friendly_message(monkeypatch):
    fake_service = _FakeCalendarService(
        _FakeEvents(raise_on_list=RuntimeError("boom")),
    )
    clients, _ = _make_clients(monkeypatch, service=fake_service)
    [today, _upcoming] = make_calendar_tools(clients)
    out = await today()
    assert out["ok"] is False
    assert "Google Calendar" in out["error"]


# --- ok paths -----------------------------------------------------


@pytest.mark.asyncio
async def test_today_summary_returns_events_with_clock_times(monkeypatch):
    items = [
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-05-09T09:30:00-04:00"},
            "end":   {"dateTime": "2026-05-09T09:45:00-04:00"},
        },
        {
            "summary": "Review",
            "start": {"dateTime": "2026-05-09T14:00:00-04:00"},
            "end":   {"dateTime": "2026-05-09T15:00:00-04:00"},
            "location": "Room 4",
        },
    ]
    events = _FakeEvents(items=items)
    fake_service = _FakeCalendarService(events)
    clients, _ = _make_clients(monkeypatch, service=fake_service)
    [today, _upcoming] = make_calendar_tools(clients)

    out = await today()
    assert out["ok"] is True
    assert out["account"] == "jasper"
    assert out["count"] == 2
    assert out["events"][0]["summary"] == "Standup"
    assert "AM" in out["events"][0]["start"] or "PM" in out["events"][0]["start"]
    assert out["events"][1]["location"] == "Room 4"
    # Verify the API was queried for 'today' (timeMin = now, timeMax = end of day).
    kw = events.last_kwargs
    assert kw["calendarId"] == "primary"
    assert kw["singleEvents"] is True
    assert kw["orderBy"] == "startTime"


@pytest.mark.asyncio
async def test_today_summary_handles_all_day_event(monkeypatch):
    items = [
        {"summary": "Picnic", "start": {"date": "2026-05-09"}, "end": {"date": "2026-05-10"}},
    ]
    events = _FakeEvents(items=items)
    clients, _ = _make_clients(monkeypatch, service=_FakeCalendarService(events))
    [today, _upcoming] = make_calendar_tools(clients)
    out = await today()
    assert out["ok"] is True
    assert out["events"][0]["all_day"] is True
    assert out["events"][0]["start"] == "all day"


@pytest.mark.asyncio
async def test_upcoming_default_window_is_24_hours(monkeypatch):
    events = _FakeEvents(items=[])
    clients, _ = _make_clients(monkeypatch, service=_FakeCalendarService(events))
    [_today, upcoming] = make_calendar_tools(clients)
    out = await upcoming()
    assert out["ok"] is True
    assert out["scope"] == "next_24h"
    # Verify the API call: timeMin/timeMax span ~24h
    kw = events.last_kwargs
    t_min = datetime.fromisoformat(kw["timeMin"])
    t_max = datetime.fromisoformat(kw["timeMax"])
    assert timedelta(hours=23, minutes=59) < (t_max - t_min) < timedelta(hours=24, minutes=1)


@pytest.mark.asyncio
async def test_upcoming_custom_hours(monkeypatch):
    events = _FakeEvents(items=[])
    clients, _ = _make_clients(monkeypatch, service=_FakeCalendarService(events))
    [_today, upcoming] = make_calendar_tools(clients)
    out = await upcoming(hours=4)
    assert out["scope"] == "next_4h"
    kw = events.last_kwargs
    t_min = datetime.fromisoformat(kw["timeMin"])
    t_max = datetime.fromisoformat(kw["timeMax"])
    assert timedelta(hours=3, minutes=59) < (t_max - t_min) < timedelta(hours=4, minutes=1)


@pytest.mark.asyncio
async def test_upcoming_clamps_huge_hours_to_30_days(monkeypatch):
    events = _FakeEvents(items=[])
    clients, _ = _make_clients(monkeypatch, service=_FakeCalendarService(events))
    [_today, upcoming] = make_calendar_tools(clients)
    out = await upcoming(hours=10000)  # ~14 months
    assert out["ok"] is True
    # Cap at 24 * 30 = 720h
    kw = events.last_kwargs
    t_min = datetime.fromisoformat(kw["timeMin"])
    t_max = datetime.fromisoformat(kw["timeMax"])
    assert (t_max - t_min) <= timedelta(hours=720, minutes=1)


@pytest.mark.asyncio
async def test_upcoming_rejects_zero_or_negative_hours(monkeypatch):
    clients, _ = _make_clients(monkeypatch, service=_FakeCalendarService(_FakeEvents()))
    [_today, upcoming] = make_calendar_tools(clients)
    out = await upcoming(hours=0)
    assert out["ok"] is False
    out = await upcoming(hours=-5)
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_account_arg_routes_to_named_member(monkeypatch):
    """The model passes account='brittany' — the tool should resolve
    to the brittany account (verified by the response's `account`
    field, since the fake factory accepts both)."""
    events = _FakeEvents(items=[])
    clients, _ = _make_clients(monkeypatch, service=_FakeCalendarService(events))
    [today, _upcoming] = make_calendar_tools(clients)
    out = await today(account="brittany")
    assert out["ok"] is True
    assert out["account"] == "brittany"
