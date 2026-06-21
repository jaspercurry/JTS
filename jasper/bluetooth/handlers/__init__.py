# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-device-class post-pair handlers.

Adding a new device class is one file: implement the `BluetoothHandler`
protocol from `base.py` and append it to `REGISTRY`. The engine walks
REGISTRY in order and picks the first whose `applies_to(device)`
returns True. The default handler at the tail always applies.

Today's handlers:
  - HID — wires the device into jasper-input (the accessories bridge
    from Phase A) so kernel-exposed evdev events drive volume etc.
  - A2DP-sink — Pi-as-speaker source; bluez-alsa picks the device
    up automatically once connected, so this is largely a no-op.
  - Default — trust + connect, no extra routing. Catches GATT-only
    devices and anything we don't have specialised behaviour for.
"""
from __future__ import annotations

from .base import BluetoothHandler, StatusEvent  # noqa: F401
from .hid import HIDHandler
from .a2dp_sink import A2DPSinkHandler
from .default import DefaultHandler

# Order matters — first match wins. DefaultHandler is the catch-all
# and must stay last.
REGISTRY: list[BluetoothHandler] = [
    HIDHandler(),
    A2DPSinkHandler(),
    DefaultHandler(),
]


def pick(device) -> BluetoothHandler:  # noqa: ANN001
    """First registered handler whose `applies_to` returns True.
    DefaultHandler always applies, so this never raises."""
    for h in REGISTRY:
        if h.applies_to(device):
            return h
    return REGISTRY[-1]  # DefaultHandler, by construction
