# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Combo-mode USB liveness primitives.

On a USB combo box (``JASPER_FANIN_USB_DIRECT=enabled``) jasper-fanin is the
sole live ingress owner and DIRECT-captures the gadget. Mux infers temporal
liveness from fan-in's direct-lane telemetry.
"""
from __future__ import annotations

from jasper.source_state import (
    USBSINK_PLAYING_RMS_DBFS,
    usbsink_direct_audible,
    usbsink_direct_playing,
    usbsink_direct_rms_dbfs,
)


def _fanin_status(
    source: str, *, frames: int = 0, resampler_frames=None, rms_dbfs=None, **extra,
):
    lane = {"label": "usbsink", "source": source, "frames_read": frames, **extra}
    if resampler_frames is not None:
        lane["resampler"] = {"input_frames": resampler_frames}
    if rms_dbfs is not None:
        lane["rms_dbfs"] = rms_dbfs
    return {
        "inputs": [
            {"label": "spotify", "source": "lane", "frames_read": 999},
            lane,
        ],
    }


def test_direct_playing_requires_capturing_health_and_audible_level():
    assert (
        usbsink_direct_playing(
            _fanin_status(
                "direct",
                rms_dbfs=-12.0,
                direct={"health": "capturing"},
            ),
        )
        is True
    )
    assert (
        usbsink_direct_playing(
            _fanin_status(
                "direct",
                rms_dbfs=-90.0,
                direct={"health": "capturing"},
            ),
        )
        is False
    )
    assert (
        usbsink_direct_playing(
            _fanin_status(
                "direct",
                rms_dbfs=-12.0,
                direct={"health": "waiting"},
            ),
        )
        is False
    )


# ---- Per-lane level readers -------------------------------------------------


def test_direct_rms_reads_the_direct_lane_level():
    assert usbsink_direct_rms_dbfs(_fanin_status("direct", rms_dbfs=-6.5)) == -6.5


def test_direct_rms_none_for_aloop_lane():
    assert usbsink_direct_rms_dbfs(_fanin_status("lane", rms_dbfs=-6.5)) is None


def test_direct_rms_none_when_missing_or_non_numeric():
    assert usbsink_direct_rms_dbfs(_fanin_status("direct")) is None
    assert usbsink_direct_rms_dbfs(_fanin_status("direct", rms_dbfs="loud")) is None
    assert usbsink_direct_rms_dbfs(_fanin_status("direct", rms_dbfs=True)) is None
    assert usbsink_direct_rms_dbfs(_fanin_status("direct", rms_dbfs=float("-inf"))) is None
    assert usbsink_direct_rms_dbfs(None) is None


def test_direct_audible_gates_on_the_shared_threshold():
    assert usbsink_direct_audible(_fanin_status("direct", rms_dbfs=-12.0)) is True
    assert usbsink_direct_audible(_fanin_status("direct", rms_dbfs=-90.0)) is False
    # Exactly at the gate is NOT audible (strict >), matching the solo bridge.
    assert (
        usbsink_direct_audible(
            _fanin_status("direct", rms_dbfs=USBSINK_PLAYING_RMS_DBFS),
        )
        is False
    )
    # No level / no direct lane -> None (caller picks the fail-soft direction).
    assert usbsink_direct_audible(_fanin_status("direct")) is None
    assert usbsink_direct_audible(_fanin_status("lane", rms_dbfs=-6.0)) is None
