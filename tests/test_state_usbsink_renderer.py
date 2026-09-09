# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Honest USB renderer activity from fan-in's DIRECT lane."""

from __future__ import annotations

import pytest

from jasper.control import state_aggregate


def _fanin(**usb):
    return {
        "inputs": [
            {"label": "spotify", "source": "lane", "rms_dbfs": -5.0},
            {"label": "usbsink", "source": "direct", **usb},
        ]
    }


@pytest.mark.parametrize(
    ("fanin_status", "expected"),
    [
        (_fanin(rms_dbfs=-8.5), True),
        (_fanin(rms_dbfs=-65.0), False),
        (_fanin(), False),
        (None, False),
        ({"inputs": [{"label": "usbsink", "source": "lane"}]}, False),
        ({"inputs": [{"label": "other", "source": "direct"}]}, False),
    ],
)
def test_usbsink_renderer_playing(fanin_status, expected):
    assert state_aggregate._usbsink_renderer_playing(fanin_status) is expected
