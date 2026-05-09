"""Unit tests for jasper.tools.gmail.

Same pattern as test_tools_calendar: build a GoogleClients with a
fake registry + a fake googleapiclient-shaped service, monkeypatch
load_credentials so build_gmail() reaches the fake factory.

Body decoding + relative-date formatting are tested directly against
the module-private helpers, since they're the most likely-to-break
parts of the surface.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest

from jasper import google_creds as gc
from jasper.google_creds import GoogleAccount, GoogleClients, GoogleRegistry
from jasper.tools import gmail as gmail_mod
from jasper.tools.gmail import make_gmail_tools


# --- fake gmail surface -------------------------------------------


class _FakeExecutable:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeMessages:
    """Stand-in for service.users().messages(). Captures list/get
    args; configured separately for `list` (returns id stubs) and
    `get` (looks up a per-id payload)."""

    def __init__(self, *, list_payload=None, get_payloads=None):
        self.list_payload = list_payload or {"messages": []}
        self.get_payloads = get_payloads or {}
        self.last_list_kwargs = None
        self.last_get_kwargs_list = []

    def list(self, **kwargs):
        self.last_list_kwargs = kwargs
        return _FakeExecutable(self.list_payload)

    def get(self, **kwargs):
        self.last_get_kwargs_list.append(kwargs)
        msg_id = kwargs.get("id")
        return _FakeExecutable(self.get_payloads.get(msg_id, {}))


class _FakeThreads:
    def __init__(self, payload):
        self.payload = payload
        self.last_kwargs = None

    def get(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeExecutable(self.payload)


class _FakeUsers:
    def __init__(self, *, messages=None, threads=None):
        self._messages = messages or _FakeMessages()
        self._threads = threads or _FakeThreads({})

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads


class _FakeGmailService:
    def __init__(self, *, messages=None, threads=None):
        self._users = _FakeUsers(messages=messages, threads=threads)

    def users(self):
        return self._users


# --- helpers ------------------------------------------------------


def _make_clients(monkeypatch, *, accounts=("jasper", "brittany"), service=None):
    monkeypatch.setattr(
        gc, "load_credentials", lambda account, **kw: object(),
    )
    r = GoogleRegistry()
    for i, name in enumerate(accounts):
        r.add_or_update(GoogleAccount(name=name), make_default=(i == 0))

    def factory(api_name, version, creds):
        return service

    return GoogleClients(
        registry=r, client_id="x", client_secret="y",
        service_factory=factory,
    )


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# --- gates --------------------------------------------------------


def test_make_gmail_tools_returns_empty_when_clients_none():
    assert make_gmail_tools(None) == []


# --- body decoding (pure helper) ----------------------------------


def test_decode_body_prefers_text_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("plain version")}},
            {"mimeType": "text/html", "body": {"data": _b64url("<p>html version</p>")}},
        ],
    }
    assert gmail_mod._decode_body(payload) == "plain version"


def test_decode_body_falls_back_to_html_strip():
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64url("<p>Hello <b>world</b></p>")},
    }
    out = gmail_mod._decode_body(payload)
    assert "Hello" in out
    assert "<" not in out
    assert "world" in out


def test_decode_body_caps_at_max_chars():
    big = "x" * 10_000
    payload = {"mimeType": "text/plain", "body": {"data": _b64url(big)}}
    out = gmail_mod._decode_body(payload)
    assert len(out) <= gmail_mod._MAX_BODY_CHARS + 5  # the "…" overhang
    assert out.endswith("…")


def test_decode_body_empty_payload_returns_empty_string():
    assert gmail_mod._decode_body({}) == ""


def test_decode_body_walks_nested_parts():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64url("inner plain")}},
                ],
            },
            {"mimeType": "image/png", "body": {"data": _b64url("ignored")}},
        ],
    }
    assert gmail_mod._decode_body(payload) == "inner plain"


# --- relative-date formatting (pure helper) -----------------------


def test_format_relative_date_today():
    now = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc).astimezone()
    target = datetime(2026, 5, 9, 9, 30, tzinfo=timezone.utc)
    out = gmail_mod._format_relative_date(target, now=now)
    assert "today" in out
    # Must contain a clock time
    assert "AM" in out or "PM" in out


def test_format_relative_date_yesterday():
    now = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc).astimezone()
    target = datetime(2026, 5, 8, 15, 14, tzinfo=timezone.utc)
    out = gmail_mod._format_relative_date(target, now=now)
    assert "yesterday" in out


def test_format_relative_date_within_week_uses_weekday():
    now = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc).astimezone()
    target = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)  # 3 days ago
    out = gmail_mod._format_relative_date(target, now=now)
    assert "at" in out  # has clock
    # Should be a day name (Mon-Sun)
    assert any(d in out for d in (
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    ))


def test_format_relative_date_older_uses_month_day():
    now = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc).astimezone()
    target = datetime(2026, 3, 5, 9, 0, tzinfo=timezone.utc)
    out = gmail_mod._format_relative_date(target, now=now)
    assert "March" in out
    # No year for current-year dates
    assert "2026" not in out


def test_format_relative_date_old_year_includes_year():
    now = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc).astimezone()
    target = datetime(2024, 3, 5, 9, 0, tzinfo=timezone.utc)
    out = gmail_mod._format_relative_date(target, now=now)
    assert "2024" in out


# --- error paths --------------------------------------------------


@pytest.mark.asyncio
async def test_unread_summary_no_accounts_points_to_wizard(monkeypatch):
    monkeypatch.setattr(gc, "load_credentials", lambda *a, **kw: None)
    clients = GoogleClients(
        registry=GoogleRegistry(),
        client_id="x", client_secret="y",
        service_factory=lambda *a: pytest.fail("should not be called"),
    )
    [unread, _read] = make_gmail_tools(clients)
    out = await unread()
    assert out["ok"] is False
    assert "jts.local/google" in out["error"]


@pytest.mark.asyncio
async def test_unread_summary_unknown_account_lists_available(monkeypatch):
    clients = _make_clients(monkeypatch, accounts=("jasper", "brittany"),
                            service=_FakeGmailService())
    [unread, _read] = make_gmail_tools(clients)
    out = await unread(account="frank")
    assert out["ok"] is False
    assert "frank" in out["error"]


@pytest.mark.asyncio
async def test_read_thread_requires_thread_id(monkeypatch):
    clients = _make_clients(monkeypatch, service=_FakeGmailService())
    [_unread, read_thread] = make_gmail_tools(clients)
    out = await read_thread(thread_id="")
    assert out["ok"] is False
    assert "thread_id" in out["error"]


# --- ok paths -----------------------------------------------------


@pytest.mark.asyncio
async def test_unread_summary_zero_messages(monkeypatch):
    msgs = _FakeMessages(list_payload={"messages": []})
    service = _FakeGmailService(messages=msgs)
    clients = _make_clients(monkeypatch, service=service)
    [unread, _read] = make_gmail_tools(clients)
    out = await unread()
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["messages"] == []


@pytest.mark.asyncio
async def test_unread_summary_returns_metadata(monkeypatch):
    msgs = _FakeMessages(
        list_payload={"messages": [
            {"id": "abc", "threadId": "t1"},
            {"id": "def", "threadId": "t2"},
        ]},
        get_payloads={
            "abc": {
                "id": "abc", "threadId": "t1",
                "snippet": "Lunch tomorrow?",
                "payload": {"headers": [
                    {"name": "From", "value": "Brittany <b@example.com>"},
                    {"name": "Subject", "value": "Lunch?"},
                    {"name": "Date", "value": "Thu, 8 May 2026 09:30:00 +0000"},
                ]},
            },
            "def": {
                "id": "def", "threadId": "t2",
                "snippet": "PR review needed",
                "payload": {"headers": [
                    {"name": "From", "value": "GitHub <noreply@github.com>"},
                    {"name": "Subject", "value": "[repo] PR #42"},
                ]},
            },
        },
    )
    service = _FakeGmailService(messages=msgs)
    clients = _make_clients(monkeypatch, service=service)
    [unread, _read] = make_gmail_tools(clients)
    out = await unread()
    assert out["ok"] is True
    assert out["count"] == 2
    msg_a = next(m for m in out["messages"] if m["id"] == "abc")
    assert msg_a["from"].startswith("Brittany")
    assert msg_a["subject"] == "Lunch?"
    assert msg_a["thread_id"] == "t1"
    assert "snippet" in msg_a
    assert "date" in msg_a  # parsed
    msg_d = next(m for m in out["messages"] if m["id"] == "def")
    assert msg_d["subject"] == "[repo] PR #42"
    # No Date header on this one — date field absent rather than crash
    assert "date" not in msg_d


@pytest.mark.asyncio
async def test_unread_summary_clamps_limit(monkeypatch):
    msgs = _FakeMessages(list_payload={"messages": []})
    service = _FakeGmailService(messages=msgs)
    clients = _make_clients(monkeypatch, service=service)
    [unread, _read] = make_gmail_tools(clients)
    # Way over the cap
    await unread(limit=999)
    assert msgs.last_list_kwargs["maxResults"] == gmail_mod._MAX_UNREAD
    # Way under the floor
    await unread(limit=-3)
    assert msgs.last_list_kwargs["maxResults"] == 1


@pytest.mark.asyncio
async def test_unread_summary_uses_inbox_filter(monkeypatch):
    msgs = _FakeMessages(list_payload={"messages": []})
    service = _FakeGmailService(messages=msgs)
    clients = _make_clients(monkeypatch, service=service)
    [unread, _read] = make_gmail_tools(clients)
    await unread()
    q = msgs.last_list_kwargs["q"]
    assert "is:unread" in q
    assert "in:inbox" in q
    assert "-category:promotions" in q


@pytest.mark.asyncio
async def test_read_thread_decodes_bodies(monkeypatch):
    threads = _FakeThreads({
        "messages": [
            {
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Alice <a@example.com>"},
                        {"name": "Subject", "value": "Trip plan"},
                        {"name": "Date", "value": "Thu, 8 May 2026 09:30:00 +0000"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Hi! Plan attached.")},
                },
            },
            {
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Jasper <j@example.com>"},
                        {"name": "Subject", "value": "Re: Trip plan"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Looks good. Let's go.")},
                },
            },
        ],
    })
    service = _FakeGmailService(threads=threads)
    clients = _make_clients(monkeypatch, service=service)
    [_unread, read_thread] = make_gmail_tools(clients)
    out = await read_thread(thread_id="t1")
    assert out["ok"] is True
    assert out["thread_id"] == "t1"
    assert out["subject"] == "Trip plan"
    assert out["message_count"] == 2
    assert out["messages"][0]["body"] == "Hi! Plan attached."
    assert out["messages"][1]["body"] == "Looks good. Let's go."
    # threads.get was called with the right thread_id
    assert threads.last_kwargs["id"] == "t1"


@pytest.mark.asyncio
async def test_read_thread_caps_at_10_messages(monkeypatch):
    # 15 messages — should be capped at _MAX_THREAD_MESSAGES (10)
    msgs = [
        {
            "payload": {
                "headers": [{"name": "From", "value": f"User{i}"}],
                "mimeType": "text/plain",
                "body": {"data": _b64url(f"body {i}")},
            },
        }
        for i in range(15)
    ]
    threads = _FakeThreads({"messages": msgs})
    service = _FakeGmailService(threads=threads)
    clients = _make_clients(monkeypatch, service=service)
    [_unread, read_thread] = make_gmail_tools(clients)
    out = await read_thread(thread_id="big")
    assert out["message_count"] == gmail_mod._MAX_THREAD_MESSAGES


@pytest.mark.asyncio
async def test_account_arg_routes_to_named_member(monkeypatch):
    msgs = _FakeMessages(list_payload={"messages": []})
    service = _FakeGmailService(messages=msgs)
    clients = _make_clients(monkeypatch, service=service)
    [unread, _read] = make_gmail_tools(clients)
    out = await unread(account="brittany")
    assert out["ok"] is True
    assert out["account"] == "brittany"
