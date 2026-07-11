# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the route-latency harness's tap-transport selection + the fan-in tap wire.

There is ONE USB ingress tap now: the fan-in DIRECT-capture tap (control-UDS
``TAP_ARM`` verb). fan-in DIRECT-captures ``hw:UAC2Gadget`` and taps the impulse
there. The old usbsink-bridge HTTP tap (:8781) was removed with the aloop solo
capture path (2026-07-10), so ``--tap-transport`` always resolves to the fan-in
tap (``auto`` == ``fanin``). These tests pin:

* the transport resolution (``resolve_tap_transport`` / ``build_resolved_tap``)
  always targeting the fan-in tap + its ``FANIN_DEFAULT_TAP_PATH``;
* the ``FaninTapClient`` wire — ``TAP_ARM {json}`` / ``TAP_DISARM`` + plaintext
  ``OK …`` / ``ERR …`` replies — against a tiny in-process AF_UNIX stand-in for
  jasper-fanin's control socket (mirrors ``test_route_latency_status_socket.py``);
* the cross-language constants against the Rust the harness does NOT own
  (``rust/jasper-fanin/src/impulse_tap.rs`` default path, ``state.rs`` ``source``
  marker), so a Rust-side rename fails loudly here.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from jasper.cli import route_latency_harness as harness
from jasper.fanin.status import FANIN_INPUT_SOURCE_DIRECT
from jasper.route_latency import tap_transport as tt
from jasper.route_latency.pairing import MicDetection
from jasper.route_latency.status_socket import FANIN_STATUS_SOCKET
from jasper.route_latency.tap_client import (
    FANIN_CONTROL_SOCKET,
    FANIN_DEFAULT_TAP_PATH,
    FaninTapClient,
    TapArmer,
    TapArmParams,
    TapClientError,
    parse_tap_socket_reply,
)

_REPO = Path(__file__).resolve().parents[1]
_FANIN_IMPULSE_TAP_RS = _REPO / "rust" / "jasper-fanin" / "src" / "impulse_tap.rs"
_FANIN_STATE_RS = _REPO / "rust" / "jasper-fanin" / "src" / "state.rs"


# --------------------------------------------------------------------------
# In-process AF_UNIX stand-in for jasper-fanin's control socket
# --------------------------------------------------------------------------


@pytest.fixture()
def short_sock_path():
    """A Unix-socket path short enough for AF_UNIX's ~104-char limit.

    pytest's ``tmp_path`` is too deep on macOS; bind under a short mkdtemp dir.
    Mirrors the fixture in ``test_route_latency_status_socket.py``.
    """

    d = tempfile.mkdtemp(prefix="jts-tap-")
    path = os.path.join(d, "control.sock")
    try:
        yield path
    finally:
        for cleanup in (lambda: os.unlink(path), lambda: os.rmdir(d)):
            try:
                cleanup()
            except OSError:
                pass


class _FaninControlStub:
    """Accept one connection, capture the request line, reply one line + \\n.

    Exactly the shape jasper-fanin's ``handle_connection`` uses (read one line,
    write the reply, drop the stream)."""

    def __init__(self, sock_path: str, reply: str) -> None:
        self.received: str | None = None
        self._reply = reply
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.received = data.decode("utf-8")
                conn.sendall(self._reply.encode("utf-8") + b"\n")
        finally:
            self._srv.close()

    def join(self) -> None:
        self._thread.join(timeout=2.0)


# --------------------------------------------------------------------------
# parse_tap_socket_reply — the plaintext reply normalizer
# --------------------------------------------------------------------------


def test_parse_tap_socket_reply_arm_ok():
    parsed = parse_tap_socket_reply("OK armed path=/run/jasper-fanin/impulse-tap.jsonl\n")
    assert parsed["ok"] is True
    assert parsed["status"] == "armed"
    assert parsed["path"] == "/run/jasper-fanin/impulse-tap.jsonl"


def test_parse_tap_socket_reply_disarm_ok_coerces_counters_to_int():
    parsed = parse_tap_socket_reply("OK disarmed events_written=7 events_dropped=2")
    assert parsed["ok"] is True
    assert parsed["status"] == "disarmed"
    assert parsed["events_written"] == 7 and isinstance(parsed["events_written"], int)
    assert parsed["events_dropped"] == 2


def test_parse_tap_socket_reply_err_and_empty_are_not_ok():
    assert parse_tap_socket_reply("ERR bad arm params")["ok"] is False
    assert parse_tap_socket_reply("")["ok"] is False
    assert parse_tap_socket_reply("   ")["ok"] is False


# --------------------------------------------------------------------------
# FaninTapClient wire protocol
# --------------------------------------------------------------------------


def test_fanin_tap_client_arm_sends_tap_arm_verb_and_parses_ok(short_sock_path):
    stub = _FaninControlStub(short_sock_path, "OK armed path=/run/jasper-fanin/impulse-tap.jsonl")
    client = FaninTapClient(socket_path=short_sock_path)

    response = client.arm(
        TapArmParams(threshold=0.2, refractory_ms=250, path=FANIN_DEFAULT_TAP_PATH)
    )
    stub.join()

    # Wire: `TAP_ARM {json}\n` — verb + space + a single-line JSON body.
    assert stub.received is not None
    assert stub.received.startswith("TAP_ARM ")
    assert stub.received.endswith("\n")
    body = json.loads(stub.received[len("TAP_ARM "):].strip())
    # The integer knob serializes as an int (round), not a float — the Rust
    # `as_u64()` parser rejects a JSON float (the B1 cross-language contract).
    assert body["refractory_ms"] == 250 and isinstance(body["refractory_ms"], int)
    assert body["path"] == FANIN_DEFAULT_TAP_PATH
    assert response["ok"] is True and response["status"] == "armed"


def test_fanin_tap_client_disarm_sends_tap_disarm_and_parses_counters(short_sock_path):
    stub = _FaninControlStub(short_sock_path, "OK disarmed events_written=3 events_dropped=1")
    client = FaninTapClient(socket_path=short_sock_path)

    response = client.disarm()
    stub.join()

    assert stub.received == "TAP_DISARM\n"
    assert response["events_written"] == 3
    assert response["events_dropped"] == 1


def test_fanin_tap_client_raises_on_err_reply(short_sock_path):
    _FaninControlStub(short_sock_path, "ERR bad arm params")
    client = FaninTapClient(socket_path=short_sock_path)
    with pytest.raises(TapClientError, match="bad arm params"):
        client.arm(TapArmParams(threshold=0.2))


def test_fanin_tap_client_raises_when_socket_absent(short_sock_path):
    # Nothing bound at the path — connect() fails, surfaced as TapClientError.
    client = FaninTapClient(socket_path=short_sock_path)
    with pytest.raises(TapClientError, match="is jasper-fanin running"):
        client.disarm()


def test_empty_arm_params_serialize_to_empty_body(short_sock_path):
    # `TAP_ARM {}` → the Rust side parses `{}` onto its documented defaults.
    stub = _FaninControlStub(short_sock_path, "OK armed path=/run/jasper-fanin/impulse-tap.jsonl")
    FaninTapClient(socket_path=short_sock_path).arm()
    stub.join()
    assert stub.received == "TAP_ARM {}\n"


def test_fanin_tap_client_satisfies_tap_armer_protocol():
    assert isinstance(FaninTapClient(), TapArmer)


# --------------------------------------------------------------------------
# Transport resolution — fan-in only
# --------------------------------------------------------------------------


def test_resolve_tap_transport_always_fanin():
    # There is one USB ingress tap now; auto/fanin/unknown all resolve to it.
    assert tt.resolve_tap_transport("auto") == tt.TAP_TRANSPORT_FANIN
    assert tt.resolve_tap_transport("fanin") == tt.TAP_TRANSPORT_FANIN
    assert tt.resolve_tap_transport("anything-else") == tt.TAP_TRANSPORT_FANIN


def test_transports_are_auto_and_fanin_only():
    # The usbsink transport option was removed with the :8781 bridge tap.
    assert tt.TAP_TRANSPORTS == (tt.TAP_TRANSPORT_AUTO, tt.TAP_TRANSPORT_FANIN)


def test_build_resolved_tap_always_targets_the_fanin_default_path():
    for choice in (tt.TAP_TRANSPORT_AUTO, tt.TAP_TRANSPORT_FANIN):
        resolved = tt.build_resolved_tap(transport_choice=choice, explicit_tap_path=None)
        assert resolved.transport == tt.TAP_TRANSPORT_FANIN
        assert isinstance(resolved.client, FaninTapClient)
        assert resolved.tap_path == FANIN_DEFAULT_TAP_PATH


def test_explicit_tap_path_override_honored():
    override = "/run/jasper-fanin/run-7.jsonl"
    resolved = tt.build_resolved_tap(transport_choice="fanin", explicit_tap_path=override)
    assert resolved.tap_path == override


# --------------------------------------------------------------------------
# Cross-language / cross-module constant pins
# --------------------------------------------------------------------------


def test_fanin_tap_path_matches_rust_impulse_tap_default():
    # rust/jasper-fanin/src/impulse_tap.rs owns the JSONL the fan-in tap writes;
    # our client MUST read back exactly that path.
    rust = _FANIN_IMPULSE_TAP_RS.read_text(encoding="utf-8")
    assert f'pub const DEFAULT_TAP_PATH: &str = "{FANIN_DEFAULT_TAP_PATH}";' in rust


def test_fanin_control_socket_is_the_same_socket_as_status():
    # One socket serves both STATUS (combo detection) and TAP_ARM (the arm). If
    # they ever diverged, a reader would read one socket and arm another.
    assert FANIN_CONTROL_SOCKET == FANIN_STATUS_SOCKET
    # …and it lives in the Rust tap's allowed dir (TAP_PATH_DIR).
    rust = _FANIN_IMPULSE_TAP_RS.read_text(encoding="utf-8")
    tap_dir = str(Path(FANIN_DEFAULT_TAP_PATH).parent)
    assert f'pub const TAP_PATH_DIR: &str = "{tap_dir}";' in rust
    assert str(Path(FANIN_CONTROL_SOCKET).parent) == tap_dir


def test_fanin_direct_source_marker_matches_rust_state_serializer():
    # state.rs renders `source:"direct"` on the USB DIRECT lane; the combo
    # detection (jasper.fanin.status) keys off exactly that literal.
    rust = _FANIN_STATE_RS.read_text(encoding="utf-8")
    assert '"source"' in rust
    assert f'if input.is_direct {{ "{FANIN_INPUT_SOURCE_DIRECT}" }} else {{ "lane" }}' in rust


# --------------------------------------------------------------------------
# Harness integration: `run` measures the shipping route (the fan-in tap)
# --------------------------------------------------------------------------


def test_run_reads_back_the_resolved_fanin_tap_path(tmp_path, monkeypatch):
    """End-to-end wiring guard: `run` points analyze at the resolved fan-in tap
    path. Tap events are written ONLY to a tmp fan-in path; a written samples file
    proves `run` read back the resolved path."""

    schedule = harness.click_track.build_schedule("quick", seed=3)
    schedule_path = tmp_path / "schedule.json"
    harness.click_track.write_schedule_json(schedule, schedule_path)

    # Tap JSONL at a tmp path standing in for the fan-in DEFAULT_TAP_PATH.
    fanin_tap = tmp_path / "fanin-impulse-tap.jsonl"
    n = 60
    fanin_tap.write_text(
        "\n".join(
            json.dumps({"monotonic_ns": i * 1_500_000_000, "frame_index": i * 256, "ring_fill_frames": 0, "peak": 0.8})
            for i in range(n)
        )
        + "\n",
        encoding="utf-8",
    )

    class _StubTapClient:
        def arm(self, _params):
            return {"ok": True, "status": "armed"}

        def disarm(self):
            return {"ok": True, "status": "disarmed"}

    # Resolve to the fan-in transport with the tmp tap path.
    monkeypatch.setattr(
        harness,
        "build_resolved_tap",
        lambda **_kwargs: harness.ResolvedTap(
            transport="fanin",
            client=_StubTapClient(),
            tap_path=str(fanin_tap),
            reason="fan-in tap (stubbed)",
        ),
    )
    monkeypatch.setattr(harness, "snapshot_route_health", lambda: {})
    monkeypatch.setattr(
        harness,
        "capture_mic_detections",
        lambda *a, **k: harness.MicCaptureResult(
            detections=tuple(
                MicDetection(monotonic_ns=i * 1_500_000_000 + 30_000_000, peak=0.5) for i in range(n)
            ),
            stopped_early=False,
            elapsed_seconds=schedule.duration_seconds,
            requested_seconds=schedule.duration_seconds,
        ),
    )

    rc = harness.main(["run", str(schedule_path), "--out-dir", str(tmp_path)])

    assert rc == 0
    samples = tmp_path / "latency-samples.json"
    assert samples.exists()
    values = json.loads(samples.read_text(encoding="utf-8"))
    # 30 ms raw tap→mic delta on every paired impulse.
    assert len(values) == n
    assert all(v == pytest.approx(30.0) for v in values)


def test_resolve_tap_is_cached_and_announces_transport(tmp_path, monkeypatch, capsys):
    calls = {"n": 0}

    def _fake_build(**_kwargs):
        calls["n"] += 1
        return harness.ResolvedTap(
            transport="fanin",
            client=object(),
            tap_path=FANIN_DEFAULT_TAP_PATH,
            reason="fan-in tap (stubbed)",
        )

    monkeypatch.setattr(harness, "build_resolved_tap", _fake_build)

    import argparse

    args = argparse.Namespace(
        tap_transport="auto",
        tap_socket=FANIN_CONTROL_SOCKET, tap_path=None,
    )
    first = harness._resolve_tap(args)
    second = harness._resolve_tap(args)

    assert first is second  # cached — resolved exactly once
    assert calls["n"] == 1
    out = capsys.readouterr().out
    assert "transport=fanin" in out
    assert FANIN_DEFAULT_TAP_PATH in out
