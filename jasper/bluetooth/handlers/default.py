# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Catch-all handler. Matches anything; runs last in the registry.

Used for BLE-only sensors, GATT peripherals, and any device we
don't have specialised behaviour for yet. Pair + Trust + Connect
already happened in the engine; this handler just declares "done".
"""
from __future__ import annotations

from typing import AsyncIterator

from ..models import BluetoothDevice
from .base import StatusEvent


class DefaultHandler:
    id = "default"
    label = "Generic"

    def applies_to(self, device: BluetoothDevice) -> bool:
        return True

    async def post_pair(
        self, device: BluetoothDevice,
    ) -> AsyncIterator[StatusEvent]:
        yield {
            "stage": "ready",
            "detail": (
                "Paired and trusted. No specific handler runs for this "
                "device class — its OS-level features (if any) are "
                "available."
            ),
        }
