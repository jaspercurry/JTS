# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /chat/ conversation-history dashboard server."""
from __future__ import annotations

import json
import re
import stat
import sys
import threading
import types
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO
from pathlib import Path

import pytest

from jasper.conversation_history import (
    CAPTURE_ALIAS_ENV,
    CAPTURE_ENABLED_ENV,
    ConversationStore,
    ConversationTurn,
    DB_PATH_ENV,
    make_turn_id,
    read_settings,
)
from jasper.web import chat_setup

if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")


def _turn(
    ts_utc: str,
    seq: int,
    *,
    user_text: str,
    assistant_text: str | None = None,
) -> ConversationTurn:
    return ConversationTurn(
        id=make_turn_id(ts_utc, seq),
        ts_utc=ts_utc,
        provider="gemini",
        user_text=user_text,
        assistant_text=assistant_text,
        tool_calls_json=None,
        data_json=None,
        session_id=seq,
    )


def _http_get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _http_get_full(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _http_post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str],
) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _csrf_headers(base: str) -> dict[str, str]:
    status, body, headers = _http_get_full(f"{base}/")
    assert status == 200
    match = re.search(r'name="jts-csrf" content="([^"]+)"', body.decode("utf-8"))
    assert match is not None
    cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
    return {
        "X-CSRF-Token": match.group(1),
        "Cookie": cookie,
    }


@pytest.mark.parametrize(
    ("body", "content_length", "expected"),
    (
        (b"", None, ({}, None)),
        (b"{}", "not-a-number", (None, "invalid content length")),
        (b"", "-1", (None, "request too large")),
        (b"", str(chat_setup.MAX_JSON_BYTES + 1), (None, "request too large")),
        (b"{", "1", (None, "invalid JSON body")),
        (b"{}", "3", (None, "invalid JSON body")),
        (b"[]", "2", (None, "JSON body must be an object")),
    ),
)
def test_chat_json_adapter_preserves_public_error_messages(
    body,
    content_length,
    expected,
):
    handler_cls = chat_setup._make_handler()
    handler = handler_cls.__new__(handler_cls)
    handler.headers = Message()
    if content_length is not None:
        handler.headers["Content-Length"] = content_length
    handler.rfile = BytesIO(body)

    assert handler._read_json() == expected


def test_chat_json_adapter_leaves_stream_oserror_distinct():
    class BrokenReader:
        def read(self, _length):
            raise OSError("socket reset")

    handler_cls = chat_setup._make_handler()
    handler = handler_cls.__new__(handler_cls)
    handler.headers = {"Content-Length": "1"}
    handler.rfile = BrokenReader()

    with pytest.raises(OSError, match="socket reset"):
        handler._read_json()


@pytest.fixture
def chat_server(tmp_path, monkeypatch):
    db_path = tmp_path / "conversation_history.db"
    settings_path = tmp_path / "conversation_history.env"
    settings_path.write_text(
        f"{CAPTURE_ENABLED_ENV}=1\n{DB_PATH_ENV}={db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))

    store = ConversationStore(str(db_path))
    assert store.add(
        _turn(
            "2026-06-19T20:20:00Z",
            1,
            user_text="what is the next train",
            assistant_text="The next train is in four minutes.",
        ),
    )
    assert store.add(
        _turn(
            "2026-06-19T20:30:00Z",
            1,
            user_text="turn on the lights",
            assistant_text="Turning on the lights.",
        ),
    )
    store.close()

    srv = chat_setup.make_server(("127.0.0.1", 0))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield base, db_path, settings_path
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_root_serves_canonical_shell(chat_server) -> None:
    base, _db_path, _settings_path = chat_server
    status, body = _http_get(f"{base}/")

    assert status == 200
    text = body.decode("utf-8")
    assert "<!doctype html>" in text
    assert "/assets/app.css?v=" in text
    assert 'name="jts-csrf"' in text
    assert 'id="icon-back"' in text
    assert '/assets/chat/chat.css?v=' in text
    assert '<div id="app"' in text
    assert "Loading conversation history..." in text
    assert '<script type="module" src="/assets/chat/js/main.js">' in text
    assert "<style>" not in text


def test_chat_static_modules_follow_frontend_contract() -> None:
    asset_root = (
        Path(chat_setup.__file__).resolve().parents[2]
        / "deploy"
        / "assets"
        / "chat"
    )
    main = (asset_root / "js" / "main.js").read_text(encoding="utf-8")
    views = (asset_root / "js" / "views.js").read_text(encoding="utf-8")
    components = (asset_root / "js" / "components.js").read_text(encoding="utf-8")

    assert 'meta[name="jts-csrf"]' in main
    assert "function dataPath()" in main
    assert "getJSON(requestedPath)" in main
    assert 'from "/assets/shared/js/dialog.js"' in main
    assert 'JSON.parse(raw)' in views
    assert 'parsed.kind === "research"' in views
    assert 'parsed.kind !== "voice_turn"' in views
    assert "Transcript text is not available for this provider." in views
    assert 'Tool" : "Tools"' in views
    assert "chat-turns" in views
    assert "article.chat-turn-card" in views
    assert "User -> Assistant" not in views
    assert '"attr:aria-label": "Conversation capture"' in views
    assert "No transcript for this turn." in views

    combined = "\n".join([main, views, components])
    assert ".innerHTML" not in combined


def test_data_json_returns_recent_turns_with_limit_and_since(chat_server) -> None:
    base, _db_path, _settings_path = chat_server
    status, body = _http_get(
        f"{base}/data.json?limit=1&since=2026-06-19T20:20:00Z",
    )

    assert status == 200
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    assert payload["available"] is True
    assert payload["capture_enabled"] is True
    assert payload["limit"] == 1
    assert payload["since"] == "2026-06-19T20:20:00Z"
    assert [turn["user_text"] for turn in payload["turns"]] == [
        "turn on the lights",
    ]
    assert payload["turns"][0]["assistant_text"] == "Turning on the lights."


def test_data_json_invalid_limit_is_400(chat_server) -> None:
    base, _db_path, _settings_path = chat_server
    status, body = _http_get(f"{base}/data.json?limit=lots")

    assert status == 400
    assert json.loads(body)["error"] == "limit must be an integer"


def test_capture_enable_writes_settings_file_and_initializes_db(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "conversation_history.db"
    settings_path = tmp_path / "conversation_history.env"
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    srv = chat_setup.make_server(("127.0.0.1", 0))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        status, body = _http_post_json(
            f"{base}/capture",
            {"enabled": True},
            headers=_csrf_headers(base),
        )
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)

    assert status == 200
    payload = json.loads(body)
    assert payload["capture_enabled"] is True
    assert db_path.exists() is True
    assert stat.S_IMODE(db_path.stat().st_mode) & stat.S_IWGRP
    text = settings_path.read_text(encoding="utf-8")
    assert f"{CAPTURE_ALIAS_ENV}=1" in text
    assert f"{DB_PATH_ENV}={db_path}" in text
    assert read_settings(path=str(settings_path), environ={}).capture_enabled is True


def test_capture_enable_does_not_persist_when_db_initialization_fails(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "not-a-database"
    db_path.mkdir()
    settings_path = tmp_path / "conversation_history.env"
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    srv = chat_setup.make_server(("127.0.0.1", 0))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        status, body = _http_post_json(
            f"{base}/capture",
            {"enabled": True},
            headers=_csrf_headers(base),
        )
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)

    assert status == 500
    assert "could not be initialized" in json.loads(body)["error"]
    assert settings_path.exists() is False
    assert read_settings(
        path=str(settings_path),
        environ={DB_PATH_ENV: str(db_path)},
    ).capture_enabled is False


def test_capture_disable_stops_future_capture_without_clearing_rows(
    chat_server,
    monkeypatch,
) -> None:
    from jasper.voice_daemon import WakeLoop

    base, db_path, settings_path = chat_server
    status, body = _http_post_json(
        f"{base}/capture",
        {"enabled": False},
        headers=_csrf_headers(base),
    )

    assert status == 200
    assert json.loads(body)["capture_enabled"] is False
    assert read_settings(path=str(settings_path), environ={}).capture_enabled is False

    store = ConversationStore(str(db_path))
    try:
        assert len(store.recent(10)) == 2
        wl = WakeLoop.for_tests(conversation_store=store)
        monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))
        wl._record_conversation_turn("future command", "future answer")
        assert len(store.recent(10)) == 2
    finally:
        store.close()


def test_clear_history_removes_stored_turns(chat_server) -> None:
    base, db_path, _settings_path = chat_server
    status, body = _http_post_json(
        f"{base}/clear",
        {},
        headers=_csrf_headers(base),
    )

    assert status == 200
    assert json.loads(body)["deleted"] == 2
    store = ConversationStore(str(db_path), read_only=True)
    try:
        assert store.recent(10) == []
    finally:
        store.close()


def test_data_json_missing_db_is_unavailable_not_created(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "missing.db"
    settings_path = tmp_path / "conversation_history.env"
    settings_path.write_text(
        f"{CAPTURE_ENABLED_ENV}=1\n{DB_PATH_ENV}={db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))
    srv = chat_setup.make_server(("127.0.0.1", 0))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status, body = _http_get(f"http://127.0.0.1:{srv.server_port}/data.json")
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)

    assert status == 200
    payload = json.loads(body)
    assert payload["available"] is False
    assert payload["turns"] == []
    assert db_path.exists() is False


def test_unknown_route_404s_before_read_guard(chat_server) -> None:
    base, _db_path, _settings_path = chat_server
    status, _ = _http_get(f"{base}/nope", headers={"Host": "evil.example"})

    assert status == 404


def test_read_guard_rejects_bad_host(chat_server) -> None:
    base, _db_path, _settings_path = chat_server
    status, body = _http_get(f"{base}/", headers={"Host": "evil.example"})

    assert status == 403
    assert b"host_not_allowed" in body
