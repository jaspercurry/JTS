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
    # Dispatch is table-driven: do_GET/do_POST look the path up in the
    # _GET_ROUTES / _POST_ROUTES tables, whose keys are the route paths
    # (mapped to handler-method names). Parse those keys. Also pick up the
    # residual `self.path == "..."` literals that still live inside the
    # guard's /healthz special-case and the tuple handlers' internal
    # re-discrimination ladder, so a route declared either way is found.
    routes = set(re.findall(r'"([^"]+)":\s*"_(?:get|post)_[a-z_]+"', SERVER_SRC))
    routes |= set(re.findall(r'self\.path == "([^"]+)"', SERVER_SRC))
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


# ----------------------------------------------------------------------
# _connect_host — bind-address vs connect-address mapping
# ----------------------------------------------------------------------
#
# JASPER_CONTROL_HOST is primarily the *server bind* address (seeded
# 0.0.0.0 on installs so the dial can reach 8780 from the LAN). The
# client must connect via loopback instead: connecting to 0.0.0.0
# "works" on Linux but carries `Host: 0.0.0.0:8780`, which the
# management-host guard rejects — the 2026-06-11 regression where every
# /system/ dashboard poll 403ed.


def test_connect_host_maps_unspecified_bind_to_loopback():
    assert client._connect_host("0.0.0.0") == "127.0.0.1"
    assert client._connect_host("::") == "127.0.0.1"
    assert client._connect_host("[::]") == "127.0.0.1"
    assert client._connect_host("") == "127.0.0.1"
    assert client._connect_host(" 0.0.0.0 ") == "127.0.0.1"


def test_connect_host_preserves_explicit_overrides():
    assert client._connect_host("127.0.0.1") == "127.0.0.1"
    assert client._connect_host("192.168.1.40") == "192.168.1.40"
    assert client._connect_host("jts.local") == "jts.local"


def test_default_base_url_passes_management_host_guard():
    """Compose-guard for the 2026-06-11 regression: the Host header a
    request to DEFAULT_BASE_URL carries must be accepted by the
    management-read guard. Fails if the client ever again derives an
    unspecified connect host, or if the guard stops allowing loopback."""
    from urllib.parse import urlsplit

    from jasper.http_security import management_read_allowed

    host = urlsplit(client.DEFAULT_BASE_URL).netloc
    ok, reason = management_read_allowed({"Host": host})
    assert ok, f"guard rejected the client's own Host {host!r}: {reason}"


def test_guarded_server_accepts_client_built_from_bind_address():
    """End-to-end shape of the fixed bug: a server enforcing the
    management-read guard (as jasper-control does on every GET) must
    answer 200 to a client whose base URL was derived from the seeded
    bind value 0.0.0.0."""
    from jasper.http_security import management_read_allowed

    class _Guarded(_Echo):
        def do_GET(self):  # noqa: N802
            ok, reason = management_read_allowed(self.headers)
            if not ok:
                self._send({"error": reason}, status=403)
                return
            super().do_GET()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Guarded)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        base = f"http://{client._connect_host('0.0.0.0')}:{port}"
        resp = client.get("/healthz", base_url=base)
        assert resp.status == 200, resp.body
    finally:
        srv.shutdown()
        srv.server_close()


# ----------------------------------------------------------------------
# X-JTS-Token header forwarding (control-token gate).
#
# The web wizards proxy server-side, so a browser-supplied X-JTS-Token
# can only reach control if the client forwards it. _request must add
# caller `headers` (additive) without clobbering Content-Type.
# ----------------------------------------------------------------------


class _HeaderEcho(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence test server
        pass

    def _reply(self):
        payload = {
            "x_jts_token": self.headers.get("X-JTS-Token"),
            "content_type": self.headers.get("Content-Type"),
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._reply()

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._reply()


@pytest.fixture()
def header_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HeaderEcho)
    Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()
    srv.server_close()


def test_post_forwards_x_jts_token_header(header_server):
    resp = client.post(
        "/grouping/set", data=b"{}", base_url=header_server,
        headers={"X-JTS-Token": "the-token"},
    )
    echoed = resp.json()
    assert echoed["x_jts_token"] == "the-token"
    # A JSON body still carries Content-Type — the extra header is additive.
    assert echoed["content_type"] == "application/json"


def test_caller_header_cannot_clobber_content_type(header_server):
    resp = client.post(
        "/grouping/set", data=b"{}", base_url=header_server,
        headers={"Content-Type": "text/evil", "X-JTS-Token": "tok"},
    )
    echoed = resp.json()
    assert echoed["content_type"] == "application/json"  # not overridden
    assert echoed["x_jts_token"] == "tok"


def test_get_forwards_x_jts_token_header(header_server):
    resp = client.get(
        "/grouping", base_url=header_server,
        headers={"X-JTS-Token": "tok-g"},
    )
    assert resp.json()["x_jts_token"] == "tok-g"


def test_no_headers_sends_no_x_jts_token(header_server):
    resp = client.post("/grouping/set", data=b"{}", base_url=header_server)
    assert resp.json()["x_jts_token"] is None
