"""Tests for the /chat/ conversation-history dashboard server."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from jasper.conversation_history import (
    CAPTURE_ENABLED_ENV,
    ConversationStore,
    ConversationTurn,
    DB_PATH_ENV,
    make_turn_id,
)
from jasper.web import chat_setup


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
        yield base, db_path
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_root_serves_canonical_shell(chat_server) -> None:
    base, _db_path = chat_server
    status, body = _http_get(f"{base}/")

    assert status == 200
    text = body.decode("utf-8")
    assert "<!doctype html>" in text
    assert "/assets/app.css?v=" in text
    assert 'name="jts-csrf"' in text
    assert 'id="icon-back"' in text
    assert '<div id="app"' in text
    assert "Loading conversation history..." in text
    assert '<script type="module" src="/assets/chat/js/main.js">' in text
    assert "<style>" not in text


def test_data_json_returns_recent_turns_with_limit_and_since(chat_server) -> None:
    base, _db_path = chat_server
    status, body = _http_get(
        f"{base}/data.json?limit=1&since=2026-06-19T20:20:00Z",
    )

    assert status == 200
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    assert payload["available"] is True
    assert payload["limit"] == 1
    assert payload["since"] == "2026-06-19T20:20:00Z"
    assert [turn["user_text"] for turn in payload["turns"]] == [
        "turn on the lights",
    ]
    assert payload["turns"][0]["assistant_text"] == "Turning on the lights."


def test_data_json_invalid_limit_is_400(chat_server) -> None:
    base, _db_path = chat_server
    status, body = _http_get(f"{base}/data.json?limit=lots")

    assert status == 400
    assert json.loads(body)["error"] == "limit must be an integer"


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
    base, _db_path = chat_server
    status, _ = _http_get(f"{base}/nope", headers={"Host": "evil.example"})

    assert status == 404


def test_read_guard_rejects_bad_host(chat_server) -> None:
    base, _db_path = chat_server
    status, body = _http_get(f"{base}/", headers={"Host": "evil.example"})

    assert status == 403
    assert b"host_not_allowed" in body
