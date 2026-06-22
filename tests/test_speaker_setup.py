# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import socket
import threading
import urllib.request

import pytest

from jasper.speaker_name import read_state
from jasper.speaker_name_discovery import NameConflict
from jasper.web import speaker_setup

from ._web_test_helpers import post_with_csrf


@pytest.fixture
def server_with_state(tmp_path, monkeypatch):
    state_path = str(tmp_path / "speaker_name.env")
    apply_calls: list[str] = []
    monkeypatch.setattr(speaker_setup, "_apply_name", lambda name: apply_calls.append(name))
    monkeypatch.setattr(speaker_setup, "_find_conflicts", lambda name: [])

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    server = speaker_setup.make_server(("127.0.0.1", port), state_path=state_path)
    base_url = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield {
            "url": base_url,
            "state_path": state_path,
            "server": server,
            "apply_calls": apply_calls,
        }
    finally:
        server.shutdown()
        server.server_close()


def test_get_root_renders_default_name(server_with_state):
    resp = urllib.request.urlopen(server_with_state["url"] + "/")
    body = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "Speaker name" in body
    assert 'value="JTS"' in body
    assert "jts.local" in body


def test_save_writes_state_and_applies_name(server_with_state):
    post_with_csrf(
        server_with_state["url"],
        "/save",
        {"name": "Living Room"},
    )

    state = read_state(server_with_state["state_path"])
    assert state.name == "Living Room"
    assert server_with_state["apply_calls"] == ["Living Room"]


def test_save_rejects_invalid_name_before_apply(server_with_state):
    post_with_csrf(
        server_with_state["url"],
        "/save",
        {"name": "Kitchen/Bedroom"},
    )

    state = read_state(server_with_state["state_path"])
    assert state.name == "JTS"
    assert server_with_state["apply_calls"] == []


def test_save_blocks_visible_duplicate(server_with_state, monkeypatch):
    monkeypatch.setattr(
        speaker_setup,
        "_find_conflicts",
        lambda name: [NameConflict("Spotify Connect", name, "Living Room._spotify-connect._tcp.local.")],
    )
    post_with_csrf(
        server_with_state["url"],
        "/save",
        {"name": "Living Room"},
    )

    state = read_state(server_with_state["state_path"])
    assert state.name == "JTS"
    assert server_with_state["apply_calls"] == []


def test_save_same_name_is_noop(server_with_state):
    post_with_csrf(
        server_with_state["url"],
        "/save",
        {"name": "JTS"},
    )

    assert server_with_state["apply_calls"] == []
