# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Global sound-output settings (distinct from per-profile EQ).

These shape the single output gain stage shared by every preference
profile, so they live apart from :class:`~jasper.sound.profile.SoundProfile`:

- ``headroom_trim_db`` — a fixed digital attenuation the listener can dial
  in for clip safety when running JTS at full digital volume into an
  external amp (with headroom, boosts can't reach 0 dBFS and clip).
  ``0`` by default, so boosts apply at unity — how a consumer EQ behaves.
- ``match_loudness`` — when on, each profile is turned down by its
  loudness-weighted gain so switching profiles compares tone, not volume.
  ``False`` by default.
- ``volume_floor_db`` — the calibrated audible floor for the global
  1..100% speaker-volume curve. 0% remains a true mute; 1% maps to this
  floor. The default preserves the original shipped curve.

Single source of truth: ``/var/lib/jasper/sound_settings.json``,
wizard-owned (the ``/sound/`` page). Absence or corruption fails soft to
the defaults above — which are also the "change nothing" state — so a
missing or bad file can never silently alter the sound.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..atomic_io import atomic_write_text
from ..volume_curve import (
    DEFAULT_VOLUME_FLOOR_DB,
    VOLUME_FLOOR_MAX_DB,
    VOLUME_FLOOR_MIN_DB,
    normalize_volume_floor_db,
)
from .profile import SoundProfile, loudness_compensation_db

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_VOLUME_FLOOR_DB",
    "HEADROOM_TRIM_MAX_DB",
    "SETTINGS_PATH",
    "SoundSettings",
    "VOLUME_FLOOR_MAX_DB",
    "VOLUME_FLOOR_MIN_DB",
    "load_sound_settings",
    "output_trim_db",
    "save_sound_settings",
]

SETTINGS_PATH = "/var/lib/jasper/sound_settings.json"

# Bound on the manual headroom trim. ±12 dB mirrors the per-band EQ range;
# more than 12 dB of global attenuation is volume control, not headroom.
HEADROOM_TRIM_MAX_DB = 12.0


def _coerce_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return out


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(frozen=True)
class SoundSettings:
    """Global output settings shared by every preference profile."""

    headroom_trim_db: float = 0.0
    match_loudness: bool = False
    volume_floor_db: float = DEFAULT_VOLUME_FLOOR_DB

    @classmethod
    def from_mapping(cls, raw: Any) -> "SoundSettings":
        raw = raw if isinstance(raw, dict) else {}
        trim = _coerce_float(raw.get("headroom_trim_db"), 0.0)
        trim = min(HEADROOM_TRIM_MAX_DB, max(0.0, trim))
        return cls(
            headroom_trim_db=round(trim, 3),
            match_loudness=_coerce_bool(raw.get("match_loudness"), False),
            volume_floor_db=normalize_volume_floor_db(
                raw.get("volume_floor_db", DEFAULT_VOLUME_FLOOR_DB)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "headroom_trim_db": round(self.headroom_trim_db, 3),
            "match_loudness": self.match_loudness,
            "volume_floor_db": round(self.volume_floor_db, 3),
        }


def _settings_path(path: str | Path | None) -> Path:
    return Path(path or os.environ.get("JASPER_SOUND_SETTINGS_PATH", SETTINGS_PATH))


def load_sound_settings(path: str | Path | None = None) -> SoundSettings:
    settings_path = _settings_path(path)
    try:
        return SoundSettings.from_mapping(json.loads(settings_path.read_text()))
    except FileNotFoundError:
        return SoundSettings()
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read sound settings %s: %s", settings_path, e)
        return SoundSettings()


def save_sound_settings(
    settings: SoundSettings, path: str | Path | None = None
) -> None:
    settings_path = _settings_path(path)
    data = json.dumps(settings.to_dict(), indent=2, sort_keys=True) + "\n"
    # WS1 Phase 3b-2: 0640 group jasper (was 0600) so the now-non-root
    # jasper-control can read these (non-secret) sound settings for /state.
    atomic_write_text(settings_path, data, mode=0o640)


def output_trim_db(profile: SoundProfile, settings: SoundSettings) -> float:
    """Total post-EQ attenuation for ``profile`` under ``settings``: the manual
    headroom trim, plus the profile's loudness compensation when match-loudness
    is on. Both default to 0, so the default is no trim at all -- boosts boost.
    (Emitters additionally ignore any trim on a flat profile, which can't clip
    from EQ.) Shared by the ``/sound/`` apply path, jasper-control's ``/state``,
    and jasper-doctor so the policy lives in exactly one place."""
    trim = settings.headroom_trim_db
    if settings.match_loudness:
        trim += loudness_compensation_db(profile)
    return round(trim, 3)
