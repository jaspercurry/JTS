# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HID device handler.

Matches any device advertising the HID service UUID (0x1124). The
heavy lifting is already done by `jasper-input` (the evdev bridge
from Phase A) — once bluez `Connect()`s the device, the kernel
exposes it under `/dev/input/event*` and the bridge's pyudev watcher
opens it automatically. So this handler is mostly a no-op; it just
yields status events so the user sees progress in the UI.
"""
from __future__ import annotations

from typing import AsyncIterator

from ..models import UUID_HID, BluetoothDevice
from .base import StatusEvent


class HIDHandler:
    id = "hid"
    label = "HID accessory"

    def applies_to(self, device: BluetoothDevice) -> bool:
        uu = " ".join(device.uuids).lower()
        return UUID_HID in uu

    async def post_pair(
        self, device: BluetoothDevice,
    ) -> AsyncIterator[StatusEvent]:
        yield {
            "stage": "wiring",
            "detail": (
                "Adding to the accessory bridge. Rotate or click to "
                "drive the speaker."
            ),
        }
        # jasper-input watches /dev/input/event* via pyudev and picks
        # up the new node on its own — no IPC needed here. The user
        # will see knob.adjust / knob.action events in the journal
        # within ~1 s of pair completing.
        yield {"stage": "ready"}
