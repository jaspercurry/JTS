# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Native-units <-> canonical listening_level maps for each push source.

`jasper.volume_coordinator` owns which attenuator carries the canonical
0-100 `listening_level`; observers and push writers share these conversions.
"""
from __future__ import annotations

from .music_sources import Source

# AirPlay's native map lives in shairport's volume hook,
# deploy/bin/jasper-airplay-volume (ADR-0206), not here.


def listening_level_to_spotify_percent(level: int) -> int:
    return max(0, min(100, int(level)))


def spotify_percent_to_listening_level(pct: int) -> int:
    return max(0, min(100, int(pct)))


# Bluetooth's MediaTransport1.Volume is uint16 0..127 (AVRCP 1.6
# absolute-volume scale).
BT_VOLUME_MAX = 127


def listening_level_to_bt_volume(level: int) -> int:
    p = max(0, min(100, int(level)))
    return round(p * BT_VOLUME_MAX / 100.0)


def bt_volume_to_listening_level(vol: int) -> int:
    v = max(0, min(BT_VOLUME_MAX, int(vol)))
    return round(v * 100.0 / BT_VOLUME_MAX)


def native_to_listening_level(source: Source, native_value: float | int) -> int | None:
    """Map an inbound observation to canonical listening_level units.

    Returns None for a source with no defined native mapping; the caller
    decides how to handle that (observe_source_volume declines it).
    """
    if source == Source.AIRPLAY:
        # shairport's volume hook (deploy/bin/jasper-airplay-volume,
        # ADR-0206) has already mapped AirPlay's dB scale onto this one
        # and dropped its mute sentinel, so AirPlay arrives in
        # listening-level units like USBSINK's. AirPlay is
        # camilla-master, so the carrier sync below moves the ramped
        # master fader; the sender's own slider is still never written
        # (ADR-0176).
        return max(0, min(100, int(native_value)))
    elif source == Source.SPOTIFY:
        return spotify_percent_to_listening_level(int(native_value))
    elif source == Source.BLUETOOTH:
        return bt_volume_to_listening_level(int(native_value))
    elif source == Source.USBSINK:
        # USB gadget volume_bridge POSTs percent directly. It has already
        # inverted macOS's observed square-root transfer from the gadget
        # mixer's 0-based step index (see volume_bridge._raw_to_pct). Map
        # identity to listening_level — the coordinator doesn't need to
        # know about ALSA mixer units, step indices, or the gadget's dB
        # range.
        return max(0, min(100, int(native_value)))
    else:
        return None
