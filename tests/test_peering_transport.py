"""Unit tests for jasper.peering.transport — encoding / decoding only.

Socket-level tests (real multicast send/recv) would need network and
flake on CI. The encode/decode round-trip is the load-bearing
correctness contract — that's what we pin here. The MulticastTransport
class itself is exercised by integration tests on real Pis.
"""
from __future__ import annotations

import pytest

from jasper.peering.rank import WakeReport
from jasper.peering.transport import (
    IncomingClaim,
    IncomingEnd,
    IncomingHeartbeat,
    IncomingHello,
    IncomingWake,
    PROTO_VERSION,
    decode,
    encode_claim,
    encode_end,
    encode_heartbeat,
    encode_hello,
    encode_wake,
)


# ---------- Round-trip every message type ----------


def test_hello_roundtrip():
    raw = encode_hello(peer_id="alice", room="kitchen", primary=False, ts_ns=12345)
    msg = decode(raw)
    assert isinstance(msg, IncomingHello)
    assert msg.peer_id == "alice"
    assert msg.room == "kitchen"
    assert msg.primary is False
    assert msg.ts_ns == 12345


def test_wake_roundtrip():
    report = WakeReport(
        peer_id="alice", score=0.87, snr_db=18.5, rms_dbfs=-22.3,
        primary=True, can_serve=True,
    )
    raw = encode_wake(epoch="ep-1", report=report, ts_ns=99999)
    msg = decode(raw)
    assert isinstance(msg, IncomingWake)
    assert msg.epoch == "ep-1"
    assert msg.ts_ns == 99999
    assert msg.report.peer_id == "alice"
    assert msg.report.score == pytest.approx(0.87)
    assert msg.report.snr_db == pytest.approx(18.5)
    assert msg.report.rms_dbfs == pytest.approx(-22.3)
    assert msg.report.primary is True
    assert msg.report.can_serve is True


def test_wake_with_null_snr_rms():
    """Missing SNR/RMS should round-trip as None — not coerce to 0.0,
    which would be a legitimate-looking but wrong reading."""
    report = WakeReport(
        peer_id="alice", score=0.5, snr_db=None, rms_dbfs=None,
        primary=False, can_serve=True,
    )
    raw = encode_wake(epoch="ep", report=report, ts_ns=0)
    msg = decode(raw)
    assert isinstance(msg, IncomingWake)
    assert msg.report.snr_db is None
    assert msg.report.rms_dbfs is None


def test_claim_roundtrip():
    raw = encode_claim(epoch="ep-1", peer_id="alice", ts_ns=42)
    msg = decode(raw)
    assert isinstance(msg, IncomingClaim)
    assert msg.epoch == "ep-1"
    assert msg.peer_id == "alice"
    assert msg.ts_ns == 42


def test_heartbeat_roundtrip():
    raw = encode_heartbeat(epoch="ep-1", peer_id="alice", ts_ns=100)
    msg = decode(raw)
    assert isinstance(msg, IncomingHeartbeat)
    assert msg.epoch == "ep-1"
    assert msg.peer_id == "alice"


def test_end_roundtrip():
    raw = encode_end(epoch="ep-1", peer_id="alice", reason="silence", ts_ns=200)
    msg = decode(raw)
    assert isinstance(msg, IncomingEnd)
    assert msg.epoch == "ep-1"
    assert msg.peer_id == "alice"
    assert msg.reason == "silence"


def test_end_reason_truncated():
    """Reasons get capped to keep datagrams small. Truncation is
    silent so an over-eager caller doesn't crash arbitration."""
    long_reason = "x" * 500
    raw = encode_end(epoch="ep", peer_id="alice", reason=long_reason, ts_ns=0)
    msg = decode(raw)
    assert isinstance(msg, IncomingEnd)
    assert len(msg.reason) == 64


# ---------- Malformed input is silently dropped ----------


def test_garbage_bytes_returns_none():
    assert decode(b"this is not json") is None


def test_empty_bytes_returns_none():
    assert decode(b"") is None


def test_non_object_returns_none():
    assert decode(b'["array"]') is None
    assert decode(b'"string"') is None
    assert decode(b'42') is None


def test_wrong_proto_returns_none():
    """A future-version message arrives — we must drop politely, not
    crash. The protocol gets bumped infrequently and an old peer
    refusing to participate is correct."""
    import json
    raw = json.dumps({"t": "HELLO", "proto": 99, "peer": "alice"}).encode()
    assert decode(raw) is None


def test_missing_required_fields_returns_none():
    """A peer that omits e.g. score from a WAKE message must be
    rejected (we can't rank a missing score), not crash."""
    import json
    raw = json.dumps({"t": "WAKE", "proto": PROTO_VERSION, "epoch": "ep"}).encode()
    # No 'peer' or 'score' — KeyError caught and returns None.
    assert decode(raw) is None


def test_unknown_message_type_returns_none():
    import json
    raw = json.dumps({"t": "FUTURE_TYPE", "proto": PROTO_VERSION}).encode()
    assert decode(raw) is None


def test_wake_with_oob_score_clamped_at_decode():
    """Receiver-side defense: WakeReport's __post_init__ clamps, so a
    peer reporting score=2.0 still produces a valid (clamped) report."""
    import json
    raw = json.dumps({
        "t": "WAKE", "proto": PROTO_VERSION, "epoch": "ep",
        "peer": "alice", "score": 2.0,
        "snr_db": None, "rms_dbfs": None,
        "primary": 0, "can_serve": 1,
    }).encode()
    msg = decode(raw)
    assert isinstance(msg, IncomingWake)
    assert msg.report.score == 1.0  # clamped
