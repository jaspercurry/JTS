# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-fanin STATUS interpretation predicates (`jasper.fanin.status`).

The single owner of the ``source=="direct"`` combo-mode contract, shared by
jasper-control's /state aggregator, jasper-doctor, and (future) the route-latency
harness / mux — so the magic string isn't copied per caller.
"""
from __future__ import annotations

from jasper.fanin.status import (
    DIRECT_HEALTH_CAPTURING,
    extract_direct_sample,
    fanin_usbsink_lane_is_direct,
)


def _status(usbsink_source: str) -> dict:
    return {
        "inputs": [
            {"label": "spotify", "source": "lane"},
            {"label": "usbsink", "source": usbsink_source},
        ]
    }


def test_direct_usbsink_lane_is_combo():
    assert fanin_usbsink_lane_is_direct(_status("direct")) is True


def test_aloop_usbsink_lane_is_not_combo():
    assert fanin_usbsink_lane_is_direct(_status("lane")) is False


def test_missing_usbsink_lane_is_false():
    status = {"inputs": [{"label": "spotify", "source": "lane"}]}
    assert fanin_usbsink_lane_is_direct(status) is False


def test_direct_on_a_non_usbsink_lane_does_not_count():
    # Only the usbsink lane's source is load-bearing; a hypothetical direct
    # source on another label must not read as USB combo mode.
    status = {"inputs": [{"label": "spotify", "source": "direct"}]}
    assert fanin_usbsink_lane_is_direct(status) is False


def test_malformed_status_is_false():
    assert fanin_usbsink_lane_is_direct(None) is False
    assert fanin_usbsink_lane_is_direct({}) is False
    assert fanin_usbsink_lane_is_direct({"inputs": "nope"}) is False
    assert fanin_usbsink_lane_is_direct({"inputs": [None, "x", {}, {"label": "usbsink"}]}) is False


def test_direct_sample_keeps_reopen_counters_as_observability():
    """Successful reopen churn is telemetry, not USB lifecycle authorization."""

    status = _status("direct")
    usb = status["inputs"][1]
    usb["frames_read"] = 4_534_765
    usb["direct"] = {
        "present": True,
        "health": "capturing",
        "reopens": 3,
        "card_gen_reopens": 1,
    }

    sample = extract_direct_sample(status)

    assert sample is not None
    assert sample.present is True
    assert sample.health == DIRECT_HEALTH_CAPTURING
    assert sample.reopens == 3
    assert sample.card_gen_reopens == 1
    assert sample.frames_read == 4_534_765


def test_direct_sample_rejects_non_direct_and_malformed_lanes():
    assert extract_direct_sample(_status("lane")) is None
    assert extract_direct_sample(None) is None
    assert extract_direct_sample({"inputs": "bad"}) is None
