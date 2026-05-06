"""Route-level tests for jasper.control.server.

Spins the ThreadingHTTPServer on a random port against a fake
CamillaProxy that records calls — so we can exercise the HTTP
surface end-to-end without needing a real CamillaDSP instance.
"""
from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from jasper.control.server import (
    VOLUME_MAX_DB,
    VOLUME_MIN_DB,
    _clamp_db,
    _db_to_percent,
    _make_handler,
)


class FakeCamilla:
    """Stand-in for CamillaProxy. Same sync interface, in-memory state."""

    def __init__(self, db: float = -25.0) -> None:
        self._db = db
        self.calls: list[tuple[str, float | None]] = []
        self.fail_next = False

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated camilla failure")

    def get_volume_db(self) -> float:
        self._maybe_fail()
        self.calls.append(("get", None))
        return self._db

    def set_volume_db(self, db: float) -> float:
        self._maybe_fail()
        clamped = _clamp_db(db)
        self._db = clamped
        self.calls.append(("set", clamped))
        return clamped

    def adjust_volume_db(self, delta_db: float) -> float:
        self._maybe_fail()
        target = _clamp_db(self._db + float(delta_db))
        self._db = target
        self.calls.append(("adjust", float(delta_db)))
        return target


@pytest.fixture
def server_with_camilla():
    """Start a ThreadingHTTPServer on a free port. Yields (base_url, fake)."""
    fake = FakeCamilla(db=-20.0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _maybe_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


def _post(url: str, body: dict | None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


# --- pure helpers ---


def test_clamp_db_endpoints():
    assert _clamp_db(-100.0) == VOLUME_MIN_DB
    assert _clamp_db(50.0) == VOLUME_MAX_DB
    assert _clamp_db(-10.0) == -10.0


def test_db_to_percent_endpoints():
    assert _db_to_percent(VOLUME_MIN_DB) == 0
    assert _db_to_percent(VOLUME_MAX_DB) == 100
    assert _db_to_percent((VOLUME_MIN_DB + VOLUME_MAX_DB) / 2) == 50


# --- routes ---


def test_healthz(server_with_camilla):
    base, _ = server_with_camilla
    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert body == {"ok": True}


def test_get_volume(server_with_camilla):
    base, fake = server_with_camilla
    status, body = _get(f"{base}/volume")
    assert status == 200
    assert body["db"] == -20.0
    assert body["percent"] == _db_to_percent(-20.0)
    assert fake.calls == [("get", None)]


def test_volume_adjust_relative(server_with_camilla):
    base, fake = server_with_camilla
    status, body = _post(f"{base}/volume/adjust", {"delta_db": -2.0})
    assert status == 200
    assert body["db"] == -22.0
    assert ("adjust", -2.0) in fake.calls


def test_volume_adjust_clamps_high(server_with_camilla):
    base, fake = server_with_camilla
    fake._db = -1.0
    status, body = _post(f"{base}/volume/adjust", {"delta_db": 10.0})
    assert status == 200
    assert body["db"] == VOLUME_MAX_DB
    assert body["percent"] == 100


def test_volume_adjust_clamps_low(server_with_camilla):
    base, fake = server_with_camilla
    fake._db = -49.0
    status, body = _post(f"{base}/volume/adjust", {"delta_db": -10.0})
    assert status == 200
    assert body["db"] == VOLUME_MIN_DB
    assert body["percent"] == 0


def test_volume_set_absolute(server_with_camilla):
    base, fake = server_with_camilla
    status, body = _post(f"{base}/volume/set", {"db": -8.0})
    assert status == 200
    assert body["db"] == -8.0
    assert ("set", -8.0) in fake.calls


def test_volume_set_clamps(server_with_camilla):
    base, _ = server_with_camilla
    status, body = _post(f"{base}/volume/set", {"db": 100.0})
    assert status == 200
    assert body["db"] == VOLUME_MAX_DB


def test_adjust_missing_field_400(server_with_camilla):
    base, _ = server_with_camilla
    status, body = _post(f"{base}/volume/adjust", {})
    assert status == 400
    assert "delta_db" in body["error"]


def test_adjust_non_numeric_400(server_with_camilla):
    base, _ = server_with_camilla
    status, body = _post(f"{base}/volume/adjust", {"delta_db": "loud"})
    assert status == 400


def test_set_missing_field_400(server_with_camilla):
    base, _ = server_with_camilla
    status, body = _post(f"{base}/volume/set", {})
    assert status == 400


def test_unknown_route_404(server_with_camilla):
    base, _ = server_with_camilla
    status, _ = _get(f"{base}/nope")
    assert status == 404


def test_camilla_failure_502(server_with_camilla):
    base, fake = server_with_camilla
    fake.fail_next = True
    status, body = _post(f"{base}/volume/adjust", {"delta_db": -2.0})
    assert status == 502
    assert "error" in body
