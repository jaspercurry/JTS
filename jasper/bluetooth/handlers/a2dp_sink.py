# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A2DP-sink handler — Pi receives audio from a phone/tablet.

Matches anything advertising A2DP Sink (0x110b) or A2DP Source (0x110a)
or HFP Hands-Free (0x111e) — basically any audio-class device. The Pi
is set up as a BT speaker via `bluez-alsa` + `bluealsa-aplay`, which
auto-routes any connected A2DP source's PCM into the JTS loopback
(see deploy/install.sh bluealsa-aplay drop-in). So no explicit routing
work is needed here.
"""
from __future__ import annotations

from typing import AsyncIterator

from ..models import (
    BluetoothDevice,
    UUID_A2DP_SINK,
    UUID_A2DP_SOURCE,
    UUID_HFP_HF,
)
from .base import StatusEvent


class A2DPSinkHandler:
    id = "a2dp_sink"
    label = "Audio source"

    def applies_to(self, device: BluetoothDevice) -> bool:
        uu = " ".join(device.uuids).lower()
        return (
            UUID_A2DP_SINK in uu
            or UUID_A2DP_SOURCE in uu
            or UUID_HFP_HF in uu
        )

    async def post_pair(
        self, device: BluetoothDevice,
    ) -> AsyncIterator[StatusEvent]:
        yield {
            "stage": "ready",
            "detail": (
                "Audio source paired. Start playing music on the "
                "device to route through JTS."
            ),
        }
