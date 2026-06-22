# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Protocol for per-device-class post-pair behaviour."""
from __future__ import annotations

from typing import AsyncIterator, Protocol, TypedDict

from ..models import BluetoothDevice


class StatusEvent(TypedDict, total=False):
    """One status update emitted during post-pair work.

    `stage` is the user-facing step name (e.g. "connecting",
    "configuring routing", "ready"). `error` set indicates a hard
    failure for this handler — the engine surfaces it but the
    device stays paired and trusted (bluez doesn't need our
    permission to keep a pair record).
    """
    stage: str
    detail: str
    error: str


class BluetoothHandler(Protocol):
    """One device-class handler. Add a new class by implementing
    this and registering in handlers/__init__.py."""

    #: Stable id used in /var/lib/jasper/bt_roles.json so the right
    #: handler runs again on re-connect.
    id: str

    #: Short label for the UI ("HID accessory", "Audio sink", ...).
    label: str

    def applies_to(self, device: BluetoothDevice) -> bool:
        """Does this device match this handler? Inspect `device.uuids`,
        `device.class_of_device`, or `device.icon`. The DefaultHandler
        always returns True."""
        ...

    async def post_pair(
        self, device: BluetoothDevice,
    ) -> AsyncIterator[StatusEvent]:
        """Run after `Pair()` and `Trust=True` succeed. Yield status
        events that the SSE layer streams to the browser. Final yield
        should be `{"stage": "ready"}` on success or `{"error": ...}`
        on failure. Engine handles `Connect()` before this is called
        — handlers can assume a connected link if their device class
        supports a connect."""
        # Stub for Protocol typing; concrete impls override.
        if False:
            yield {}
