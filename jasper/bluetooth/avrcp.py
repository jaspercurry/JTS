# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""BlueZ AVRCP transport control and the A2DP-sink presence probe.

One ObjectManager read answers both questions: whether a phone has an A2DP
transport to us (mux and the source-state poll), and which MediaPlayer1 to
drive (Play/Pause/Next).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .models import UUID_A2DP_SINK

if TYPE_CHECKING:
    from .adapter import BluezSession

logger = logging.getLogger(__name__)

BLUEZ_PLAYER_IFACE = "org.bluez.MediaPlayer1"
BLUEZ_TRANSPORT_IFACE = "org.bluez.MediaTransport1"

# BlueZ answers GetManagedObjects out of its own cache, so this bounds a
# wedged bus, not a slow one. Both callers are on hot paths: every mux BT
# transport command, and the per-tick source-state poll.
BLUEZ_PROBE_TIMEOUT_SEC = 2.0


async def _bluez_objects(session: BluezSession | None = None) -> dict[str, Any] | None:
    """One bounded object-tree read; None when BlueZ failed."""
    try:
        # lazy: import cost. dbus_next behind .adapter is ~10 MB RSS that the
        # resident mux/voice daemons importing this module must not hold for a
        # path used once per Bluetooth preempt (tests/test_lazy_imports.py).
        # Guarded: an absent dbus_next is an unreachable bus, not a raise into
        # mux's probe gather, and BLUEZ_ERRORS below is unbound if it fails.
        from .adapter import BLUEZ_ERRORS, managed_objects  # lazy
    except ImportError as exc:
        logger.debug("dbus_next unavailable: %s", exc)
        return None
    try:
        # asyncio.timeout(), NOT wait_for(): awaited directly from
        # cancellation-only poll loops (mux patrol, source-state tick), which
        # wait_for on 3.11 would keep immortal. See jasper/renderer.py.
        async with asyncio.timeout(BLUEZ_PROBE_TIMEOUT_SEC):
            return await managed_objects(session)
    # LookupError: managed_objects returns body[0] of the reply.
    except (*BLUEZ_ERRORS, LookupError, TimeoutError) as exc:
        logger.debug("bluez GetManagedObjects failed: %s", exc)
        return None


def _a2dp_sink_device(objects: dict[str, Any]) -> str | None:
    """The Device1 path owning the first A2DP-sink transport, in any state.

    Such a ``MediaTransport1`` exists exactly while a phone has the A2DP sink
    profile connected — the same fact the bluealsa PCM list used to report —
    so a connected-but-paused phone still counts.

    The UUID filter is load-bearing: bluez-alsa also runs an a2dp-source
    endpoint (JTS -> headset) and SCO/HFP transports surface on this same
    interface, and either would otherwise read as "a phone is playing to us"
    and preempt the current mux winner.
    """
    for ifaces in objects.values():
        transport = ifaces.get(BLUEZ_TRANSPORT_IFACE)
        if transport is None:
            continue
        uuid = getattr(transport.get("UUID"), "value", None)
        if not isinstance(uuid, str) or UUID_A2DP_SINK not in uuid.lower():
            continue
        device = getattr(transport.get("Device"), "value", None)
        if isinstance(device, str) and device:
            return device
    return None


def _player_path(objects: dict[str, Any]) -> str | None:
    """The active A2DP device's MediaPlayer1 path, else the first player."""
    players = sorted(path for path, ifaces in objects.items() if BLUEZ_PLAYER_IFACE in ifaces)
    device = _a2dp_sink_device(objects)
    if device is not None:
        for path in players:
            if path.startswith(device + "/"):
                return path
    return players[0] if players else None


def _player_status(objects: dict[str, Any], path: str) -> str:
    """``MediaPlayer1.Status`` lowercased, or empty when unknown."""
    status = objects.get(path, {}).get(BLUEZ_PLAYER_IFACE, {}).get("Status")
    value = getattr(status, "value", None)
    return value.lower() if isinstance(value, str) else ""


async def a2dp_sink_playing(session: BluezSession | None = None) -> bool | None:
    """True while a phone has an A2DP transport to us; None when BlueZ failed.

    The None is load-bearing: mux must not read an unreachable bus as
    "nothing playing".
    """
    objects = await _bluez_objects(session)
    if objects is None:
        return None
    return _a2dp_sink_device(objects) is not None


async def bluetooth_player_path(session: BluezSession | None = None) -> str | None:
    """The MediaPlayer1 path an AVRCP command should target, if any."""
    objects = await _bluez_objects(session)
    return None if objects is None else _player_path(objects)


async def bluetooth_avrcp_call(method: str, session: BluezSession | None = None) -> None:
    """Invoke a no-arg AVRCP method on the active BlueZ MediaPlayer1."""
    try:
        # Guarded for the same reason as in _bluez_objects: an absent
        # dbus_next is one failed transport command, not a ModuleNotFoundError
        # escaping into mux's preempt path.
        from dbus_next import Message  # type: ignore  # lazy: import cost

        from .adapter import BLUEZ_BUS, BLUEZ_ERRORS, bluez_session  # lazy: import cost
    except ImportError as exc:
        raise RuntimeError(f"bluetooth {method} failed: {exc}") from exc

    try:
        # The timeout also covers the connect: a bus that accepts the socket
        # but never answers Hello must not hang the mux preempt path.
        async with asyncio.timeout(BLUEZ_PROBE_TIMEOUT_SEC), bluez_session(session) as sess:
            objects = await _bluez_objects(sess)
            path = None if objects is None else _player_path(objects)
            if objects is None or path is None:
                raise RuntimeError("bluetooth AVRCP player not available")
            if method == "PlayPause":
                method = "Pause" if _player_status(objects, path) == "playing" else "Play"
            await sess.call(Message(
                destination=BLUEZ_BUS,
                path=path,
                interface=BLUEZ_PLAYER_IFACE,
                member=method,
            ))
    except (*BLUEZ_ERRORS, LookupError, TimeoutError) as exc:
        raise RuntimeError(f"bluetooth {method} failed: {exc}") from exc
