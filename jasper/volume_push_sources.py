# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Push-mode source writers: Spotify Web API and Bluetooth AVRCP.

Spotify and Bluetooth carry `listening_level` on their own protocol
sliders rather than CamillaDSP (see `volume_coordinator`'s module
docstring). The coordinator retains `_set_spotify`/`_set_bluetooth` because
`tests/test_volume_coordinator.py` overrides them, and owns echo stamps.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

from . import busctl, volume_diagnostics
from .bluealsa_probe import active_transport_path
from .music_sources import Source
from .spotify_router import DEVICES_TIMEOUT_SEC
from .volume_scales import (
    listening_level_to_bt_volume,
    listening_level_to_spotify_percent,
)

if TYPE_CHECKING:
    from .spotify_router import Router

logger = logging.getLogger(__name__)
_bluez_alsa_active_transport_path = partial(active_transport_path, logger)


async def push_spotify_volume(
    router: Router | None,
    device_name: str,
    level: int,
) -> bool:
    """Set Spotify volume via Spotify Web API.

    librespot 0.8.0 has no local control HTTP — to change Spotify's
    volume we go through Spotify's cloud, which propagates back to
    librespot via spirc AND updates every Spotify client UI (your
    phone slider visibly moves). Latency ~200-800ms typical.

    We try every authorized account until one successfully claims
    the JTS device. On failure (no router configured, no account
    has the JTS device active, or all accounts return errors),
    log and no-op."""
    pct = listening_level_to_spotify_percent(level)
    if router is None or not getattr(router, "clients", {}):
        volume_diagnostics.record_source_push(
            Source.SPOTIFY,
            level=level,
            ok=False,
            reason=volume_diagnostics.PUSH_MISSING_ROUTER,
        )
        logger.warning(
            "spotify volume set: no Web API router configured; "
            "voice/remote volume can't propagate to Spotify (set "
            "SPOTIFY_CLIENT_ID/SECRET and authorize at least one "
            "account via /spotify)",
        )
        return False
    matches = await router.devices_named(device_name)
    saw_device = bool(matches)
    write_failed = False
    for ac, d in matches:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    ac.sp.volume, pct, device_id=d.get("id"),
                ),
                timeout=DEVICES_TIMEOUT_SEC,
            )
            volume_diagnostics.record_source_push(
                Source.SPOTIFY,
                level=level,
                ok=True,
                reason=volume_diagnostics.PUSH_OK,
                detail="device_visible",
            )
            logger.info(
                "spotify volume set: %d%% (account=%s)",
                pct, ac.account.name,
            )
            return True
        except Exception as e:  # noqa: BLE001
            write_failed = True
            logger.debug(
                "spotify volume() failed for %s: %s",
                ac.account.name, e,
            )
            continue
    reason = (
        volume_diagnostics.PUSH_WRITE_FAILED
        if write_failed
        else volume_diagnostics.PUSH_NO_ACTIVE_DEVICE
    )
    volume_diagnostics.record_source_push(
        Source.SPOTIFY,
        level=level,
        ok=False,
        reason=reason,
        detail="device_visible" if saw_device else "device_not_visible",
    )
    logger.warning(
        "spotify volume set FAILED: %d%% — no account could write "
        "to device '%s' (is JTS still selected in Spotify?)",
        pct, device_name,
    )
    return False


async def push_bluetooth_volume(level: int) -> bool:
    vol = listening_level_to_bt_volume(level)
    # bluez-alsa exposes one MediaTransport1 path per active
    # transport; we have to find it before we can set the
    # property. Empty list = no active BT transport (caller
    # invoked us during a brief BT-active window that closed).
    path = await _bluez_alsa_active_transport_path()
    if path is None:
        volume_diagnostics.record_source_push(
            Source.BLUETOOTH,
            level=level,
            ok=False,
            reason=volume_diagnostics.PUSH_NO_ACTIVE_TRANSPORT,
        )
        logger.debug(
            "bluetooth volume set: no active transport, skipping",
        )
        return False
    ok = await busctl.set_property(
        "org.bluealsa", path,
        "org.bluez.MediaTransport1",
        "Volume",
        "q",
        str(vol),
        bus="--system",
    )
    if ok:
        volume_diagnostics.record_source_push(
            Source.BLUETOOTH,
            level=level,
            ok=True,
            reason=volume_diagnostics.PUSH_OK,
            detail="transport_present",
        )
        logger.info("bluetooth volume set: %d%% (uint16=%d)", level, vol)
        return True
    else:
        volume_diagnostics.record_source_push(
            Source.BLUETOOTH,
            level=level,
            ok=False,
            reason=volume_diagnostics.PUSH_WRITE_FAILED,
            detail="transport_present",
        )
        logger.warning(
            "bluetooth volume set FAILED: %d%% (uint16=%d)", level, vol,
        )
        return False
