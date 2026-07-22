# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the Python-side contract with the Rust ingress tap that this package
does NOT own or edit.

Since the single-USB-pipeline convergence (2026-07-10) the ONE ingress tap lives
in `jasper-fanin` (`rust/jasper-fanin/src/impulse_tap.rs`): fan-in DIRECT-captures
`hw:UAC2Gadget` and taps the impulse there. The old usbsink-bridge HTTP tap
(`127.0.0.1:8781`) was removed with the aloop solo capture path, so its
`TapClient` + arm/disarm HTTP contract are gone.

This file pins the two halves of the boundary this package's Python code still
consumes:

  * the JSONL event schema (`jasper.route_latency.tap_client.read_tap_events`
    must parse exactly the pinned shape — the SAME shape the fan-in tap emits,
    including malformed-tail tolerance for a file read mid-write), and
  * the health-counter leaf names the route-health verdict reads out of the
    fanin + outputd Rust status serializers (a Rust-side rename must
    fail loudly here, not silently make the verdict vacuous).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.cli.route_latency_harness import (
    KNOWN_HEALTH_COUNTER_PATHS,
    KNOWN_HEALTH_COUNTER_SUFFIXES,
)
from jasper.route_latency.tap_client import (
    TapArmParams,
    read_tap_events,
)

_REPO = Path(__file__).resolve().parents[1]
_FANIN_STATE_RS = _REPO / "rust" / "jasper-fanin" / "src" / "state.rs"
_OUTPUTD_STATE_RS = _REPO / "rust" / "jasper-outputd" / "src" / "state.rs"


# --------------------------------------------------------------------------
# JSONL schema: {"monotonic_ns":..,"frame_index":..,"ring_fill_frames":..,
# "peak":..} — one per line.
# --------------------------------------------------------------------------


def test_read_tap_events_parses_byte_exact_rust_emitted_line(tmp_path):
    # This exact string is asserted verbatim by the Rust crate's own
    # `tap_event_jsonl_shape_is_stable` test
    # (rust/jasper-fanin/src/impulse_tap.rs) — a cross-language wire
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
    # `tap_event_jsonl_round_trips_through_serde` test
    # (rust/jasper-fanin/src/impulse_tap.rs).
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


# --------------------------------------------------------------------------
# Health-counter names: the harness's route-health verdict
# (`RouteHealthReport.would_justify_route_health_ok`, which
# `--confirm-route-health-ok` gates on) reads specific counter paths out of
# fan-in/outputd STATUS snapshots. If a Rust serializer renamed one of those,
# a stable-path lookup could stop observing the real counter and the flagged
# verdict would silently degrade to vacuous-true. Pin the leaf names against
# the Rust source so a rename fails loudly here — the same cross-language
# discipline used for the JSONL fixture and the raw0 wire constants.
# --------------------------------------------------------------------------


def _rust_status_emits_leaf(source: str, leaf: str) -> bool:
    """True iff `source` emits `leaf` as a JSON key.

    Fan-in/outputd currently use ``push_kv_*`` helpers with plain string
    literals. Accept the escaped form too so the check stays insensitive to
    serializer construction style.
    """

    return f'\\"{leaf}\\":' in source or f'"{leaf}"' in source


# The route-health surfaces and the Rust source that serializes each. A
# pinned counter's leaf must still be emitted by the surface it lives on, so a
# Rust-side rename fails loudly here instead of silently degrading the verdict.
_SURFACE_SOURCES = {
    "fanin": _FANIN_STATE_RS,
    "outputd": _OUTPUTD_STATE_RS,
}


def test_known_health_counter_names_exist_in_rust_status_json():
    sources = {name: path.read_text(encoding="utf-8") for name, path in _SURFACE_SOURCES.items()}
    for path in KNOWN_HEALTH_COUNTER_PATHS:
        surface = path[0]
        assert surface in sources, (
            f"health-counter path {path!r} names an unknown surface {surface!r}; "
            f"add its Rust source to _SURFACE_SOURCES to cross-check it"
        )
        leaf = path[-1]
        assert _rust_status_emits_leaf(sources[surface], leaf), (
            f"health counter {leaf!r} (in KNOWN_HEALTH_COUNTER_PATHS path "
            f"{path!r}) is no longer emitted by {surface}'s status serializer — "
            "a Rust-side rename would silently make the harness's route-health "
            "verdict vacuous. Update both sides together."
        )


def test_known_health_counter_suffixes_exist_in_fanin_status_json():
    # The array-indexed fan-in-input lane counters (per-lane xrun + the USB
    # resampler unlock/silence/overrun) are matched by dotted-path SUFFIX
    # because the lane index is not stable. Their leaf names must still exist in
    # the fan-in status serializer, or a rename makes the suffix match — and the
    # verdict for the route's own resampler/xrun health — vacuous.
    fanin_src = _FANIN_STATE_RS.read_text(encoding="utf-8")
    for suffix in KNOWN_HEALTH_COUNTER_SUFFIXES:
        assert suffix[:2] == ("fanin", "inputs"), (
            f"unexpected suffix shape {suffix!r}; the fan-in cross-check only "
            "validates fanin.inputs.* per-lane counters"
        )
        leaf = suffix[-1]
        assert _rust_status_emits_leaf(fanin_src, leaf), (
            f"per-lane health counter {leaf!r} (in KNOWN_HEALTH_COUNTER_SUFFIXES "
            f"{suffix!r}) is no longer emitted by jasper-fanin's state.rs status "
            "serializer — a Rust-side rename would silently make the harness's "
            "per-lane route-health verdict vacuous. Update both sides together."
        )


# --------------------------------------------------------------------------
# TapArmParams body serialization (the arm-request shape both the Rust fan-in
# tap and the harness agree on). The usbsink HTTP TapClient tests were deleted
# with the :8781 listener.
# --------------------------------------------------------------------------


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

_FANIN_IMPULSE_TAP_RS = _REPO / "rust" / "jasper-fanin" / "src" / "impulse_tap.rs"


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
    rust_src = _FANIN_IMPULSE_TAP_RS.read_text(encoding="utf-8")
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
