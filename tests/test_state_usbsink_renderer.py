# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Honest USB renderer projection from kernel + fan-in owners."""

from __future__ import annotations

import json

from jasper.control import state_aggregate


def _fanin(**usb):
    return {
        "inputs": [
            {"label": "spotify", "source": "lane", "rms_dbfs": -5.0},
            {"label": "usbsink", "source": "direct", **usb},
        ]
    }


def test_renderer_projects_direct_lane_and_udc_connection():
    state = state_aggregate._build_usbsink_renderer_state(
        _fanin(rms_dbfs=-8.5, muted=False),
        host_connected=True,
    )

    assert state == {
        "combo": True,
        "playing": True,
        "preempted": False,
        "muted": False,
        "host_connected": True,
        "rms_dbfs": -8.5,
        "updated_at": None,
    }


def test_renderer_keeps_activity_separate_from_mix_mute():
    state = state_aggregate._build_usbsink_renderer_state(
        _fanin(rms_dbfs=-8.5, muted=True),
        host_connected=False,
    )

    assert state is not None
    assert state["playing"] is True
    assert state["muted"] is True
    assert state["host_connected"] is False


def test_renderer_fails_closed_without_identity_bound_direct_lane():
    assert (
        state_aggregate._build_usbsink_renderer_state(
            None,
            host_connected=True,
        )
        is None
    )
    assert (
        state_aggregate._build_usbsink_renderer_state(
            {"inputs": [{"label": "usbsink", "source": "lane"}]},
            host_connected=True,
        )
        is None
    )
    assert (
        state_aggregate._build_usbsink_renderer_state(
            {"inputs": [{"label": "other", "source": "direct"}]},
            host_connected=True,
        )
        is None
    )


def test_renderer_missing_level_is_null_and_json_clean():
    state = state_aggregate._build_usbsink_renderer_state(
        _fanin(),
        host_connected=True,
    )

    assert state is not None
    assert state["playing"] is None
    assert state["rms_dbfs"] is None
    assert state["muted"] is None
    json.dumps(state, allow_nan=False)
