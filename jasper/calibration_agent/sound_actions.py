# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Sound-profile executors for validated calibration-advisor actions."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from jasper.sound.profile import PROFILE_LIBRARY_PATH, PROFILE_PATH, SoundProfile

from .actions import ActionExecutor
from .response import ACTION_AUDITION


def build_sound_audition_executor(
    *,
    profile_path: str | Path = PROFILE_PATH,
    library_path: str | Path | None = PROFILE_LIBRARY_PATH,
    config_dir: str | Path | None = None,
    camilla_factory: Callable[[], Any] | None = None,
) -> ActionExecutor:
    """Return an executor that loads a validated profile as an audition.

    This is deliberately the reversible path. It writes and loads the
    existing ``sound_audition.yml`` substrate, does not persist a profile,
    and never accepts volume or raw CamillaDSP YAML from the advisor.
    """

    def _executor(action: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(
            audition_advisor_profile(
                action,
                profile_path=profile_path,
                library_path=library_path,
                config_dir=config_dir,
                camilla_factory=camilla_factory,
            )
        )

    return _executor


async def audition_advisor_profile(
    action: dict[str, Any],
    *,
    profile_path: str | Path = PROFILE_PATH,
    library_path: str | Path | None = PROFILE_LIBRARY_PATH,
    config_dir: str | Path | None = None,
    camilla_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Audition one validated advisor profile through the sound backend."""

    if action.get("type") != ACTION_AUDITION:
        raise ValueError("sound audition executor only accepts audition actions")
    profile_payload = action.get("profile")
    if not isinstance(profile_payload, dict):
        raise ValueError("validated audition action is missing profile")

    from jasper.web import sound_setup

    kwargs: dict[str, Any] = {}
    if camilla_factory is not None:
        kwargs["camilla_factory"] = camilla_factory
    payload = await sound_setup.audition_profile(
        SoundProfile.from_mapping(profile_payload),
        audition_mode="advisor",
        profile_path=profile_path,
        library_path=library_path,
        config_dir=config_dir or sound_setup.DEFAULT_CONFIG_DIR,
        **kwargs,
    )
    return _safe_audition_result(payload, action)


def _safe_audition_result(
    payload: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    active_config = payload.get("active_config_path")
    active_config_name = Path(str(active_config)).name if active_config else None
    return {
        "audition_mode": payload.get("audition_mode"),
        "active_config_name": active_config_name,
        "preserved_room_peqs": payload.get("preserved_room_peqs", 0),
        "output_trim_db": payload.get("output_trim_db", 0.0),
        "dsp_write_epoch": payload.get("dsp_write_epoch"),
        "sound_filter_count": action.get("sound_filter_count"),
        "headroom_db": action.get("headroom_db"),
        "profile": action.get("profile"),
    }
