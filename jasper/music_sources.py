# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical music-source metadata.

All music/content sources enter the speaker through jasper-fanin and
then CamillaDSP. This module keeps the cross-cutting source facts in
one place so adding a source is a declaration plus the source-specific
probe/control hooks, not a scavenger hunt across mux, volume, and UI
code.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Source(str, Enum):
    AIRPLAY = "airplay"
    SPOTIFY = "spotify"
    BLUETOOTH = "bluetooth"
    USBSINK = "usbsink"
    IDLE = "idle"


class VolumeMode(str, Enum):
    CAMILLA_MASTER = "camilla_master"
    PUSH = "push"


@dataclass(frozen=True)
class MusicSourceSpec:
    id: Source
    fanin_label: str
    renderer_active_key: str
    wizard_key: str
    volume_mode: VolumeMode
    display_name: str


MUSIC_SOURCE_SPECS: tuple[MusicSourceSpec, ...] = (
    MusicSourceSpec(
        id=Source.SPOTIFY,
        fanin_label="spotify",
        renderer_active_key="spotactive",
        wizard_key="spotify_connect",
        volume_mode=VolumeMode.PUSH,
        display_name="Spotify",
    ),
    MusicSourceSpec(
        id=Source.AIRPLAY,
        fanin_label="airplay",
        renderer_active_key="aplactive",
        wizard_key="airplay",
        volume_mode=VolumeMode.CAMILLA_MASTER,
        display_name="AirPlay",
    ),
    MusicSourceSpec(
        id=Source.BLUETOOTH,
        fanin_label="bluealsa",
        renderer_active_key="btactive",
        wizard_key="bluetooth",
        volume_mode=VolumeMode.PUSH,
        display_name="Bluetooth",
    ),
    MusicSourceSpec(
        id=Source.USBSINK,
        fanin_label="usbsink",
        renderer_active_key="usbsinkactive",
        wizard_key="usbsink",
        volume_mode=VolumeMode.CAMILLA_MASTER,
        display_name="USB Audio",
    ),
)

MUSIC_SOURCES: tuple[Source, ...] = tuple(spec.id for spec in MUSIC_SOURCE_SPECS)
SOURCE_SPECS: dict[Source, MusicSourceSpec] = {
    spec.id: spec for spec in MUSIC_SOURCE_SPECS
}
SOURCE_TO_FANIN_LABEL: dict[Source, str] = {
    spec.id: spec.fanin_label for spec in MUSIC_SOURCE_SPECS
}
SOURCE_TO_ACTIVE_KEY: dict[Source, str] = {
    spec.id: spec.renderer_active_key for spec in MUSIC_SOURCE_SPECS
}


def is_music_source(source: Source) -> bool:
    return source in SOURCE_SPECS


def volume_mode(source: Source) -> VolumeMode:
    if source == Source.IDLE:
        return VolumeMode.CAMILLA_MASTER
    return SOURCE_SPECS[source].volume_mode
