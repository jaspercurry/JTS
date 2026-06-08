"""Tests for jasper.control.client — the typed jasper-control client.

Two things matter: (1) the client must not drift from the server's route
table (a method targeting a renamed/removed endpoint is a silent break), and
(2) the transport must behave — parse JSON, expose status, and raise
ControlError (not a raw OSError) when jasper-control is down.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from jasper.control import client

ROOT = Path(__file__).resolve().parent.parent
SERVER_SRC = (ROOT / "jasper" / "control" / "server.py").read_text()

# Every control path this client (and its callers) targets. Keep in sync with
# the client's methods + the migrated call sites; the test below proves each
# exists in the server.
CLIENT_PATHS = [
    "/state",
    "/dial/status",
    "/healthz",
    "/volume/adjust",
    "/volume/set",
    "/cue/play",
]


def test_client_paths_exist_in_server_route_table():
    """Contract guard: each path the client targets must be a real route in
    jasper/control/server.py, so renaming a server route fails this test
    instead of silently breaking a daemon at runtime."""
    routes = set(re.findall(r'self\.path == "([^"]+)"', SERVER_SRC))
    assert routes, "could not parse any routes from server.py"
    missing = [p for p in CLIENT_PATHS if p not in routes]
    assert not missing, f"client targets paths the server does not serve: {missing}"


class _Echo(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence test server
        pass

    def _send(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self._send({"ok": True})
        elif self.path == "/state":
            self._send({"voice": {"provider": "test"}})
        else:
            self._send({"error": "nope"}, status=404)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        self._send({"echo": body, "path": self.path})


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()
    srv.server_close()


def test_get_state_parses_json(server):
    state = client.get_state(base_url=server)
    assert state["voice"]["provider"] == "test"


def test_healthz_true_then_false(server):
    assert client.healthz(base_url=server) is True
    # Point at a closed port → ControlError swallowed → False (never raises).
    assert client.healthz(base_url="http://127.0.0.1:1", timeout=0.2) is False


def test_post_sends_json_body(server):
    resp = client.post("/volume/set", {"percent": 42, "source": "test"}, base_url=server)
    assert resp.ok
    assert resp.json()["echo"] == {"percent": 42, "source": "test"}


def test_post_raw_data_is_forwarded_verbatim(server):
    # The wizard proxy forwards pre-encoded JSON bytes via `data=`.
    raw = b'{"threshold": 0.4}'
    resp = client.post("/aec/threshold", data=raw, base_url=server)
    assert resp.ok
    assert resp.json()["echo"] == {"threshold": 0.4}


def test_non_2xx_is_not_an_error(server):
    # A 404 comes back as a ControlResponse, not a ControlError.
    resp = client.get("/nope", base_url=server)
    assert resp.status == 404
    assert resp.ok is False


def test_transport_failure_raises_control_error():
    with pytest.raises(client.ControlError):
        client.get("/state", base_url="http://127.0.0.1:1", timeout=0.2)


@pytest.mark.asyncio
async def test_async_client_adjust_and_set_volume(server):
    c = client.AsyncControlClient(server)
    r1 = await c.adjust_volume(5)
    assert r1.json()["echo"] == {"delta_percent": 5}
    r2 = await c.set_volume(30, source="usbsink")
    assert r2.json()["echo"] == {"percent": 30, "source": "usbsink"}


@pytest.mark.asyncio
async def test_async_client_raises_control_error_when_down():
    c = client.AsyncControlClient("http://127.0.0.1:1", timeout=0.2)
    with pytest.raises(client.ControlError):
        await c.get("/state")
