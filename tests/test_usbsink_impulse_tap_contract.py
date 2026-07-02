# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the Python-side contract with the Rust ingress tap
(`rust/jasper-usbsink-audio`) that this package does NOT own or edit.

The tap lives in Rust; this file's job is to pin the two halves of the
boundary this package's Python code actually consumes:

  * the JSONL event schema (`jasper.route_latency.tap_client.read_tap_events`
    must parse exactly the pinned shape, including malformed-tail
    tolerance for a file read mid-write by the Rust publisher thread), and
  * the HTTP arm/disarm/status request/response shapes
    (`jasper.route_latency.tap_client.TapClient` speaks exactly the pinned
    verbs/paths/bodies).

This is NOT a test of the Rust implementation (out of scope — the other
implementer owns `rust/jasper-usbsink-audio/**`); it is a test that OUR
side of the interface matches the documented contract, using a tiny local
HTTP stub to stand in for the real Rust listener.
"""
from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from jasper.cli.route_latency_harness import KNOWN_HEALTH_COUNTER_PATHS
from jasper.route_latency.tap_client import (
    DEFAULT_TAP_PATH,
    TapArmParams,
    TapClient,
    TapClientError,
    read_tap_events,
)

_REPO = Path(__file__).resolve().parents[1]
_USBSINK_MAIN_RS = _REPO / "rust" / "jasper-usbsink-audio" / "src" / "main.rs"


# --------------------------------------------------------------------------
# JSONL schema: {"monotonic_ns":..,"frame_index":..,"ring_fill_frames":..,
# "peak":..} — one per line.
# --------------------------------------------------------------------------


def test_read_tap_events_parses_byte_exact_rust_emitted_line(tmp_path):
    # This exact string is asserted verbatim by the Rust crate's own
    # `tap_event_jsonl_shape_is_stable` test
    # (rust/jasper-usbsink-audio/src/impulse_tap.rs) — a cross-language wire
    # fixture, not a paraphrase. If either side's serialization format ever
    # drifts (spacing, key order, float precision), one of the two pinned
    # tests should catch it.
    path = tmp_path / "impulse-tap.jsonl"
    path.write_text(
        '{"monotonic_ns":123456789012,"frame_index":4096,"ring_fill_frames":512,"peak":0.830000}\n',
        encoding="utf-8",
    )

    events = read_tap_events(path)

    assert len(events) == 1
    e = events[0]
    assert e.monotonic_ns == 123456789012
    assert e.frame_index == 4096
    assert e.ring_fill_frames == 512
    assert e.peak == pytest.approx(0.83)


def test_read_tap_events_parses_negative_i128_monotonic_ns(tmp_path):
    # Rust's monotonic_ns is i128 (can be negative in principle, e.g. very
    # early boot); Python's arbitrary-precision int must round-trip it
    # exactly. Fixture matches Rust's own
    # `tap_event_jsonl_round_trips_through_serde` test.
    path = tmp_path / "impulse-tap.jsonl"
    path.write_text(
        '{"monotonic_ns":-42,"frame_index":7,"ring_fill_frames":0,"peak":0.500000}\n',
        encoding="utf-8",
    )

    events = read_tap_events(path)

    assert events[0].monotonic_ns == -42


def test_read_tap_events_parses_pinned_schema_exactly(tmp_path):
    path = tmp_path / "impulse-tap.jsonl"
    path.write_text(
        '{"monotonic_ns": 123456789012, "frame_index": 4096, "ring_fill_frames": 512, "peak": 0.83}\n',
        encoding="utf-8",
    )

    events = read_tap_events(path)

    assert len(events) == 1
    e = events[0]
    assert e.monotonic_ns == 123456789012
    assert e.frame_index == 4096
    assert e.ring_fill_frames == 512
    assert e.peak == pytest.approx(0.83)


def test_read_tap_events_tolerates_truncated_final_line(tmp_path):
    # The Rust publisher thread appends lines periodically; a read that
    # races an in-progress append can see a partial final line. Every
    # earlier COMPLETE line remains valid evidence and must still parse.
    path = tmp_path / "impulse-tap.jsonl"
    path.write_text(
        '{"monotonic_ns": 1, "frame_index": 1, "ring_fill_frames": 1, "peak": 0.1}\n'
        '{"monotonic_ns": 2, "frame_index": 2, "ring_fill_frames": 2, "peak": 0.2}\n'
        '{"monotonic_ns": 3, "frame_index"',  # truncated
        encoding="utf-8",
    )

    events = read_tap_events(path)

    assert [e.monotonic_ns for e in events] == [1, 2]


def test_read_tap_events_skips_blank_lines(tmp_path):
    path = tmp_path / "impulse-tap.jsonl"
    path.write_text(
        '{"monotonic_ns": 1, "frame_index": 1, "ring_fill_frames": 1, "peak": 0.1}\n'
        "\n"
        '{"monotonic_ns": 2, "frame_index": 2, "ring_fill_frames": 2, "peak": 0.2}\n',
        encoding="utf-8",
    )

    events = read_tap_events(path)

    assert len(events) == 2


def test_read_tap_events_missing_file_returns_empty_list(tmp_path):
    events = read_tap_events(tmp_path / "does-not-exist.jsonl")

    assert events == []


def test_read_tap_events_skips_lines_missing_required_fields(tmp_path):
    path = tmp_path / "impulse-tap.jsonl"
    path.write_text(
        '{"monotonic_ns": 1, "frame_index": 1, "ring_fill_frames": 1, "peak": 0.1}\n'
        '{"monotonic_ns": 2}\n'  # missing required fields
        '{"monotonic_ns": 3, "frame_index": 3, "ring_fill_frames": 3, "peak": 0.3}\n',
        encoding="utf-8",
    )

    events = read_tap_events(path)

    assert [e.monotonic_ns for e in events] == [1, 3]


def test_default_tap_path_is_under_run_jasper_usbsink():
    # Pinned per the contract: tmpfs, same dir as state.json.
    assert DEFAULT_TAP_PATH == "/run/jasper-usbsink/impulse-tap.jsonl"


# --------------------------------------------------------------------------
# Health-counter names: the harness's route-health verdict
# (`RouteHealthReport.would_justify_route_health_ok`, which
# `--confirm-route-health-ok` gates on) reads specific counter paths out of
# the usbsink `state.json`. If the Rust `counters` block ever renamed one of
# those, `all_deltas.get(path, 0.0)` would return 0 forever and the flagged
# verdict would silently degrade to vacuous-true. Pin the leaf names against
# the Rust source so a rename fails loudly here — the same cross-language
# discipline used for the JSONL fixture and the raw0 wire constants.
# --------------------------------------------------------------------------


def test_known_health_counter_names_exist_in_rust_status_json():
    source = _USBSINK_MAIN_RS.read_text(encoding="utf-8")
    for path in KNOWN_HEALTH_COUNTER_PATHS:
        # Every pinned path is usbsink.counters.<leaf>; the leaf is the JSON
        # key the Rust status_json emits inside its "counters" object.
        assert path[:2] == ("usbsink", "counters"), (
            f"unexpected health-counter path shape {path!r}; the source "
            "cross-check below only validates usbsink.counters.* leaves"
        )
        leaf = path[-1]
        assert f'\\"{leaf}\\":' in source, (
            f"health counter {leaf!r} (in KNOWN_HEALTH_COUNTER_PATHS) is no "
            "longer emitted by jasper-usbsink-audio's status_json counters "
            "block — a Rust-side rename would silently make the harness's "
            "route-health verdict vacuous. Update both sides together."
        )


# --------------------------------------------------------------------------
# HTTP contract: POST /tap/arm, POST /tap/disarm, GET /tap — using a tiny
# local stub server standing in for the Rust listener's documented shapes.
# --------------------------------------------------------------------------


class _StubTapHandler(http.server.BaseHTTPRequestHandler):
    """Minimal stand-in for the Rust 8781 listener's /tap/* routes."""

    armed = False
    last_arm_body: dict | None = None

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        if self.path == "/tap/arm":
            _StubTapHandler.armed = True
            _StubTapHandler.last_arm_body = json.loads(raw) if raw else {}
            self._send_json(200, {"ok": True, "armed": True, "path": "/run/jasper-usbsink/impulse-tap.jsonl"})
        elif self.path == "/tap/disarm":
            _StubTapHandler.armed = False
            self._send_json(200, {"ok": True, "armed": False, "events_written": 42, "events_dropped": 1})
        else:
            self._send_json(404, {"ok": False})

    def do_GET(self):  # noqa: N802
        if self.path == "/tap":
            self._send_json(
                200,
                {
                    "armed": _StubTapHandler.armed,
                    "events_written": 7,
                    "events_dropped": 0,
                    "threshold": 0.2,
                    "refractory_ms": 250,
                    "max_events": 4000,
                    "path": "/run/jasper-usbsink/impulse-tap.jsonl",
                },
            )
        else:
            self._send_json(404, {"ok": False})

    def log_message(self, format, *args):  # noqa: A002 - stdlib override
        pass  # silence test-run noise


@pytest.fixture
def stub_tap_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubTapHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def test_tap_client_arm_posts_expected_body(stub_tap_server):
    host, port = stub_tap_server
    client = TapClient(host=host, port=port)

    response = client.arm(TapArmParams(threshold=0.3, hysteresis=0.1, refractory_ms=200, max_events=1000, auto_disarm_min=30))

    assert response["ok"] is True
    assert response["armed"] is True
    assert _StubTapHandler.last_arm_body == {
        "threshold": 0.3,
        "hysteresis": 0.1,
        "refractory_ms": 200,
        "max_events": 1000,
        "auto_disarm_min": 30,
    }


def test_tap_client_arm_with_no_params_sends_empty_body(stub_tap_server):
    host, port = stub_tap_server
    client = TapClient(host=host, port=port)

    client.arm()

    assert _StubTapHandler.last_arm_body == {}


def test_tap_client_disarm_returns_counters(stub_tap_server):
    host, port = stub_tap_server
    client = TapClient(host=host, port=port)

    response = client.disarm()

    assert response["armed"] is False
    assert response["events_written"] == 42
    assert response["events_dropped"] == 1


def test_tap_client_status_parses_get_tap_response(stub_tap_server):
    host, port = stub_tap_server
    client = TapClient(host=host, port=port)

    status = client.status()

    assert status.events_written == 7
    assert status.events_dropped == 0
    assert status.path == "/run/jasper-usbsink/impulse-tap.jsonl"


def test_tap_client_raises_on_unreachable_host():
    # Port 1 is privileged/unlikely-bound; a genuinely closed port makes
    # this deterministic across CI hosts without relying on timing.
    client = TapClient(host="127.0.0.1", port=1, timeout_seconds=0.5)

    with pytest.raises(TapClientError, match="8781 listener"):
        client.arm()


def test_tap_arm_params_to_body_omits_unset_fields():
    params = TapArmParams(threshold=0.25)

    assert params.to_body() == {"threshold": 0.25}


def test_tap_arm_params_to_body_empty_when_all_unset():
    assert TapArmParams().to_body() == {}


# --------------------------------------------------------------------------
# B1 cross-language round-trip: a `--tap-refractory-ms` (argparse type=float)
# arrives at TapArmParams as a Python float (300.0). Before the fix, to_body()
# passed it through and json.dumps emitted "refractory_ms":300.0 — which the
# Rust `positive_u64` (serde_json `as_u64()`) rejects for any float, so every
# invocation of the flag 400'd the arm for the whole measurement window. These
# tests pin BOTH halves of the fix: the Python body must serialize integer
# knobs as JSON integers, and that exact byte-string must be one the Rust
# fixtures below (arm_body_overrides_all_fields / _accepts_integral_float_*)
# accept. We can't call the Rust parser from Python, so we cross-check that the
# CLI-produced JSON string matches the shape the Rust source's own tests pin.
# --------------------------------------------------------------------------

_USBSINK_IMPULSE_TAP_RS = _REPO / "rust" / "jasper-usbsink-audio" / "src" / "impulse_tap.rs"


def test_arm_body_serializes_float_typed_int_knobs_as_json_integers():
    # Mirrors exactly what jasper-route-latency-harness builds from a
    # `--tap-refractory-ms 300` / `--tap-max-events`-style float-typed CLI arg:
    # the value reaches TapArmParams as a float.
    params = TapArmParams(
        threshold=0.4,
        hysteresis=0.1,
        refractory_ms=300.0,
        max_events=10.0,
        auto_disarm_min=5.0,
        path="/run/jasper-usbsink/x.jsonl",
    )
    body = params.to_body()

    # The three integer knobs must be Python ints (so json.dumps emits `300`,
    # not `300.0`). The float knobs stay floats.
    assert body["refractory_ms"] == 300 and isinstance(body["refractory_ms"], int)
    assert body["max_events"] == 10 and isinstance(body["max_events"], int)
    assert body["auto_disarm_min"] == 5 and isinstance(body["auto_disarm_min"], int)
    assert isinstance(body["threshold"], float)

    serialized = json.dumps(body)
    # The exact wire bytes the daemon receives — no `.0` on any integer knob.
    assert "300.0" not in serialized
    assert "10.0" not in serialized
    assert "5.0" not in serialized
    assert '"refractory_ms": 300' in serialized


def test_arm_body_rounds_fractional_int_knobs_to_nearest():
    # A fractional ms is rounded to the nearest int rather than truncated, so an
    # operator's 250.7 lands on 251, not 250. (Rust rejects a non-integral float
    # outright — the Python round() is what keeps a fractional CLI value usable.)
    params = TapArmParams(refractory_ms=250.7, max_events=9.4, auto_disarm_min=5.5)
    body = params.to_body()

    assert body["refractory_ms"] == 251
    assert body["max_events"] == 9
    assert body["auto_disarm_min"] == 6  # round-half-to-even: 5.5 -> 6


def test_cli_produced_arm_body_matches_rust_integral_float_fixture():
    # Cross-language: the Rust source pins acceptance of BOTH a native-int body
    # (arm_body_overrides_all_fields) AND an integral-float body
    # (arm_body_accepts_integral_float_u64_knobs). The CLI now emits the
    # native-int shape; assert the Rust fixture that accepts it still exists, so
    # a Rust-side regression that dropped either acceptance path fails here too.
    rust_src = _USBSINK_IMPULSE_TAP_RS.read_text(encoding="utf-8")
    assert "fn arm_body_overrides_all_fields" in rust_src, (
        "Rust fixture pinning acceptance of the CLI's native-int arm body is "
        "gone — the B1 wire contract is unpinned on the Rust side."
    )
    assert "fn arm_body_accepts_integral_float_u64_knobs" in rust_src, (
        "Rust fixture pinning the integral-float defense (300.0) is gone — a "
        "client that emits float-encoded int knobs would silently 400 again."
    )
    # The CLI's serialized body uses these exact JSON integer knobs; the Rust
    # native-int fixture body must carry the same keys as integer literals.
    body = TapArmParams(refractory_ms=300.0, max_events=10.0, auto_disarm_min=5.0).to_body()
    serialized = json.dumps(body, separators=(",", ":"))
    assert '"refractory_ms":300' in serialized
    assert '"max_events":10' in serialized
    assert '"auto_disarm_min":5' in serialized
