# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Volume-policy state derived from the collected `/state` inputs."""
from __future__ import annotations

from typing import Any

from .music_sources import Source, VolumeMode, volume_mode
from .volume_floor import RECONCILE_DRIFT_DB


def _source_from_state(
    active_source: str | None,
    mux_status: dict[str, Any] | None,
) -> Source:
    for raw in (
        active_source,
        (mux_status or {}).get("active_source"),
        (mux_status or {}).get("selected_source"),
        (mux_status or {}).get("winner"),
    ):
        if not isinstance(raw, str):
            continue
        try:
            return Source(raw)
        except ValueError:
            continue
    return Source.IDLE


def build_volume_policy_snapshot(
    *,
    active_source: str | None,
    listening_level: int | None,
    main_volume_db: float | None,
    persisted_main_volume_db: float | None,
    mux_status: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build `/state.audio.volume_policy` without further I/O."""
    source = _source_from_state(active_source, mux_status)
    mode = volume_mode(source)
    persisted_guard_db = (
        persisted_main_volume_db
        if (
            persisted_main_volume_db is not None
            and float(persisted_main_volume_db) < -RECONCILE_DRIFT_DB
        )
        else None
    )
    live_guard_db = (
        main_volume_db
        if (
            main_volume_db is not None
            and float(main_volume_db) < -RECONCILE_DRIFT_DB
        )
        else None
    )
    guard_db = persisted_guard_db if persisted_guard_db is not None else live_guard_db
    push_guard_active = (
        mode == VolumeMode.PUSH
        and guard_db is not None
    )

    last_handoff = None
    if (
        isinstance(mux_status, dict)
        and isinstance(mux_status.get("last_handoff"), dict)
    ):
        last_handoff = mux_status.get("last_handoff")

    if mode == VolumeMode.PUSH:
        carrier = "camilla_guard" if push_guard_active else "source"
    else:
        carrier = "camilla"

    return {
        "active_source": active_source,
        "source": source.value,
        "volume_mode": mode.value,
        "carrier": carrier,
        "listening_level_percent": listening_level,
        "main_volume_db": main_volume_db,
        "persisted_main_volume_db": persisted_main_volume_db,
        "push_guard_active": push_guard_active,
        "guard_db": round(float(guard_db), 2) if push_guard_active else None,
        "last_handoff": last_handoff,
    }
