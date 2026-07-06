# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`/state.renderers.usbsink` honesty on combo vs solo boxes.

On a USB *combo* box (jasper-fanin DIRECT-captures the gadget), the
jasper-usbsink bridge runs in standby and publishes frozen idle defaults —
``playing:false`` / ``rms_dbfs:-120`` — that describe nothing. The live audio
flows through fan-in's direct lane. These tests pin the aggregator's
``renderers.usbsink`` section to that fan-in truth (nulled, combo-flagged)
rather than the misleading standby-bridge values, and keep the solo path
byte-for-byte where the bridge's RMS-gated state IS the truth.
"""
from __future__ import annotations

import json

from jasper.control import state_aggregate


# A standby-bridge blob as seen live on a combo box (jts.local, 2026-07-06):
# playing/rms are frozen idle defaults, host_connected still valid via sysfs.
_STANDBY_BRIDGE = {
    "standby": True,
    "playing": False,
    "preempted": False,
    "host_connected": True,
    "rms_dbfs": -120.0,
    "updated_at": "2026-07-06T16:15:33.123Z",
}


def _fanin_status(usbsink_source: str, **usbsink_extra):
    """A fan-in STATUS snapshot with one non-USB lane plus the usbsink lane."""
    return {
        "inputs": [
            {"label": "spotify", "source": "lane"},
            {"label": "usbsink", "source": usbsink_source, **usbsink_extra},
        ]
    }


def test_combo_mode_detected_off_fanin_direct_lane():
    # source=="direct" is the authoritative "fan-in owns the live capture"
    # signal — the same one the route-latency harness keys its combo tap off.
    assert (
        state_aggregate._usbsink_in_combo_mode(
            _fanin_status("direct", frames_read=60768), {"playing": False}
        )
        is True
    )


def test_combo_mode_falls_back_to_bridge_standby_flag_when_fanin_absent():
    # If the fan-in STATUS is momentarily unreachable, the bridge's own
    # `standby` flag still tells us its playing/rms are meaningless — so a
    # transient fan-in outage can't resurrect the stale -120.
    assert state_aggregate._usbsink_in_combo_mode(None, {"standby": True}) is True


def test_solo_mode_when_lane_is_aloop_and_bridge_not_standby():
    assert (
        state_aggregate._usbsink_in_combo_mode(
            _fanin_status("lane"), {"playing": True}
        )
        is False
    )


def test_combo_renderer_state_nulls_stale_playing_and_rms():
    """The reported bug: standby bridge shows playing:false / rms:-120 while
    combo audio flows. The section must null those and flag combo, keeping the
    still-valid host_connected."""
    section = state_aggregate._build_usbsink_renderer_state(
        _STANDBY_BRIDGE, _fanin_status("direct", frames_read=60768)
    )
    assert section == {
        "combo": True,
        "playing": None,
        "preempted": False,
        "host_connected": True,
        "rms_dbfs": None,
        "updated_at": "2026-07-06T16:15:33.123Z",
    }


def test_combo_renderer_state_via_standby_fallback_matches_fanin_path():
    # Both combo detectors yield the same honest shape.
    via_standby = state_aggregate._build_usbsink_renderer_state(
        _STANDBY_BRIDGE, None
    )
    via_fanin = state_aggregate._build_usbsink_renderer_state(
        _STANDBY_BRIDGE, _fanin_status("direct")
    )
    assert via_standby == via_fanin
    assert via_standby["combo"] is True
    assert via_standby["playing"] is None
    assert via_standby["rms_dbfs"] is None


def test_solo_renderer_state_preserves_bridge_rms_truth():
    section = state_aggregate._build_usbsink_renderer_state(
        {
            "playing": True,
            "preempted": False,
            "host_connected": True,
            "rms_dbfs": -12.3,
            "updated_at": "2026-05-16T00:00:00+00:00",
        },
        _fanin_status("lane"),
    )
    assert section == {
        "combo": False,
        "playing": True,
        "preempted": False,
        "host_connected": True,
        "rms_dbfs": -12.3,
        "updated_at": "2026-05-16T00:00:00+00:00",
    }


def test_solo_renderer_state_scrubs_nonfinite_rms():
    # Legacy jasper-usbsink could write -Infinity; the section stays
    # allow_nan=False-clean.
    section = state_aggregate._build_usbsink_renderer_state(
        {"playing": False, "rms_dbfs": float("-inf"), "host_connected": True},
        None,
    )
    assert section["combo"] is False
    assert section["rms_dbfs"] is None
    json.dumps(section, allow_nan=False)


def test_combo_renderer_state_is_json_nan_clean():
    section = state_aggregate._build_usbsink_renderer_state(
        _STANDBY_BRIDGE, _fanin_status("direct")
    )
    json.dumps(section, allow_nan=False)


def test_unusable_blob_returns_none():
    # Feature off (no dict) → null, matching the "null == off" contract the
    # /system dashboard relies on. A non-dict JSON root (e.g. a list) must not
    # crash the aggregator.
    assert state_aggregate._build_usbsink_renderer_state(None, None) is None
    assert state_aggregate._build_usbsink_renderer_state(["nope"], None) is None
    assert state_aggregate._build_usbsink_renderer_state("nope", None) is None
