# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""BlueZ AVRCP helpers for receiver-side Bluetooth transport control."""
from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

BLUEZ_DEST = "org.bluez"
BLUEZ_PLAYER_IFACE = "org.bluez.MediaPlayer1"

_BLUEZ_PLAYER_PATH_RE = re.compile(
    rb"(/org/bluez/hci\d+/dev_[A-F0-9_]+/player\d+)"
)
_BLUEALSA_A2DP_DEVICE_RE = re.compile(
    rb"/org/bluealsa/(hci\d+/dev_[A-F0-9_]+)/a2dpsnk/source"
)
_BUSCTL_QUOTED_VALUE_RE = re.compile(r'"([^"]*)"')


async def bluetooth_active_device_path() -> str | None:
    """Return the BlueZ Device1 path for the active A2DP sink, if known.

    bluealsa-cli publishes paths under org.bluealsa; BlueZ publishes
    the matching AVRCP player under org.bluez. The hci/dev suffix is
    shared, so translate:
      /org/bluealsa/hci0/dev_AA_BB/... -> /org/bluez/hci0/dev_AA_BB
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluealsa-cli", "list-pcms",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        logger.debug("bluealsa-cli list-pcms failed: %s", e)
        return None
    if proc.returncode != 0:
        return None
    match = _BLUEALSA_A2DP_DEVICE_RE.search(stdout)
    if match is None:
        return None
    return f"/org/bluez/{match.group(1).decode('ascii')}"


async def bluetooth_player_paths() -> list[str]:
    """Return BlueZ AVRCP player object paths currently registered."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "tree", BLUEZ_DEST,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        logger.debug("bluez player tree failed: %s", e)
        return []
    if proc.returncode != 0:
        return []
    return sorted({
        match.group(1).decode("ascii")
        for match in _BLUEZ_PLAYER_PATH_RE.finditer(stdout)
    })


async def bluetooth_player_path() -> str | None:
    """Return the active device's AVRCP player path, or the first player."""
    active_device = await bluetooth_active_device_path()
    players = await bluetooth_player_paths()
    if active_device is not None:
        prefix = active_device + "/"
        for path in players:
            if path.startswith(prefix):
                return path
    return players[0] if players else None


async def bluetooth_player_status(path: str) -> str:
    """Read org.bluez.MediaPlayer1.Status, or empty string if unknown."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "get-property",
            BLUEZ_DEST, path, BLUEZ_PLAYER_IFACE, "Status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        logger.debug("bluez player status failed: %s", e)
        return ""
    if proc.returncode != 0:
        return ""
    match = _BUSCTL_QUOTED_VALUE_RE.search(stdout.decode("utf-8", "replace"))
    return match.group(1).lower() if match else ""


async def bluetooth_avrcp_call(method: str) -> None:
    """Invoke a no-arg AVRCP method on the active BlueZ MediaPlayer1."""
    path = await bluetooth_player_path()
    if path is None:
        raise RuntimeError("bluetooth AVRCP player not available")
    if method == "PlayPause":
        status = await bluetooth_player_status(path)
        method = "Pause" if status == "playing" else "Play"
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call",
            BLUEZ_DEST, path, BLUEZ_PLAYER_IFACE, method,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        raise RuntimeError(f"bluetooth {method} failed: {e}") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"bluetooth {method} failed: "
            f"{stderr.decode(errors='replace').strip()}"
        )
