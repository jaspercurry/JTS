# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cheap volume-policy diagnostics for `/state`.

This module is deliberately not a control path. The coordinator writes a
small volatile JSON snapshot when source-volume pushes, degraded guards,
or guard clears happen. jasper-control reads it while building `/state`.

The file lives under /run by default so diagnostics do not add SD-card
wear, and every operation is fail-soft: volume correctness must never
depend on this snapshot being writable.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_text
from .music_sources import Source, VolumeMode, volume_mode

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/run/jasper/volume_policy.json"
PUSH_GUARD_EPSILON_DB = 1.0

PUSH_OK = "ok"
PUSH_UNSUPPORTED = "unsupported"
PUSH_MISSING_ROUTER = "missing_router"
PUSH_NO_ACTIVE_DEVICE = "no_active_device"
PUSH_NO_ACTIVE_TRANSPORT = "no_active_transport"
PUSH_WRITE_FAILED = "write_failed"

GUARD_PUSH_WRITE_FAILED = "push_write_failed"
GUARD_SOURCE_HANDOFF_PUSH_FAILED = "source_handoff_push_failed"
GUARD_ACTIVE_SOURCE_PUSH_FAILED = "active_source_push_failed"
GUARD_PUSH_CONFIRMED = "push_confirmed"
GUARD_CLEAR_DEFERRED_DUCK_ACTIVE = "clear_deferred_duck_active"


def diagnostics_path(path: str | None = None) -> str:
    return path or os.environ.get("JASPER_VOLUME_DIAGNOSTICS_PATH", DEFAULT_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds",
    ).replace("+00:00", "Z")


def _source_value(source: Source | str) -> str:
    if isinstance(source, Source):
        return source.value
    return str(source)


def _read(path: str | None = None) -> dict[str, Any]:
    p = Path(diagnostics_path(path))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return {"version": 1}
    if not isinstance(data, dict):
        return {"version": 1}
    data["version"] = 1
    return data


def _write(snapshot: dict[str, Any], path: str | None = None) -> None:
    p = Path(diagnostics_path(path))
    try:
        body = json.dumps(snapshot, indent=2, sort_keys=True)
        # mode=0o600 preserves the mode the previous hand-rolled mkstemp
        # writer published (mkstemp creates 0600 and never chmod'd).
        atomic_write_text(p, body, mode=0o600)
    except OSError as e:
        logger.debug("volume diagnostics write failed for %s: %s", p, e)


def read_diagnostics(path: str | None = None) -> dict[str, Any]:
    """Return the latest volatile diagnostics snapshot.

    Shape is intentionally sparse: missing keys mean no event has been
    recorded since boot or since /run was cleared.
    """
    return _read(path)


def record_source_push(
    source: Source | str,
    *,
    level: int,
    ok: bool,
    reason: str,
    detail: str = "",
    path: str | None = None,
) -> None:
    snapshot = _read(path)
    snapshot["last_source_push_result"] = {
        "source": _source_value(source),
        "level": max(0, min(100, int(level))),
        "ok": bool(ok),
        "reason": reason,
        "detail": detail,
        "updated_at": _now_iso(),
    }
    _write(snapshot, path)


def record_push_guard(
    source: Source | str,
    *,
    level: int,
    guard_db: float,
    reason: str,
    context: str,
    previous_db: float | None = None,
    path: str | None = None,
) -> None:
    snapshot = _read(path)
    snapshot["push_guard"] = {
        "active": True,
        "source": _source_value(source),
        "level": max(0, min(100, int(level))),
        "guard_db": round(float(guard_db), 2),
        "previous_db": None if previous_db is None else round(float(previous_db), 2),
        "reason": reason,
        "context": context,
        "updated_at": _now_iso(),
    }
    _write(snapshot, path)


def record_push_guard_clear(
    source: Source | str,
    *,
    level: int,
    previous_db: float | None,
    reason: str = GUARD_PUSH_CONFIRMED,
    context: str,
    ok: bool = True,
    path: str | None = None,
) -> None:
    snapshot = _read(path)
    event = {
        "source": _source_value(source),
        "level": max(0, min(100, int(level))),
        "previous_db": None if previous_db is None else round(float(previous_db), 2),
        "reason": reason,
        "context": context,
        "ok": bool(ok),
        "updated_at": _now_iso(),
    }
    snapshot["last_clear_event"] = event
    if ok:
        snapshot["push_guard"] = {
            "active": False,
            "source": _source_value(source),
            "reason": reason,
            "context": context,
            "updated_at": event["updated_at"],
        }
    _write(snapshot, path)


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
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the read-only `/state.audio.volume_policy` block.

    The snapshot is derived from already-collected `/state` inputs plus
    the volatile diagnostics file. It performs no network, DBus, Camilla,
    or Spotify calls.
    """
    diagnostics = diagnostics or {}
    source = _source_from_state(active_source, mux_status)
    mode = volume_mode(source)
    persisted_guard_db = (
        persisted_main_volume_db
        if (
            persisted_main_volume_db is not None
            and float(persisted_main_volume_db) < -PUSH_GUARD_EPSILON_DB
        )
        else None
    )
    live_guard_db = (
        main_volume_db
        if (
            main_volume_db is not None
            and float(main_volume_db) < -PUSH_GUARD_EPSILON_DB
        )
        else None
    )
    guard_db = persisted_guard_db if persisted_guard_db is not None else live_guard_db
    push_guard_active = (
        mode == VolumeMode.PUSH
        and guard_db is not None
    )

    guard_detail = diagnostics.get("push_guard")
    if not isinstance(guard_detail, dict):
        guard_detail = {}
    guard_matches_source = guard_detail.get("source") == source.value

    guard_reason = None
    guard_context = None
    previous_db = None
    if push_guard_active:
        guard_reason = (
            guard_detail.get("reason")
            if guard_matches_source and isinstance(guard_detail.get("reason"), str)
            else (
                "derived_from_persisted_camilla_guard"
                if persisted_guard_db is not None
                else "derived_from_live_camilla_guard"
            )
        )
        guard_context = (
            guard_detail.get("context")
            if guard_matches_source and isinstance(guard_detail.get("context"), str)
            else None
        )
        raw_previous = guard_detail.get("previous_db") if guard_matches_source else None
        previous_db = raw_previous if isinstance(raw_previous, (int, float)) else None

    last_push = diagnostics.get("last_source_push_result")
    if not isinstance(last_push, dict):
        last_push = None
    last_clear = diagnostics.get("last_clear_event")
    if not isinstance(last_clear, dict):
        last_clear = None
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
        "guard_reason": guard_reason,
        "guard_context": guard_context,
        "previous_db": None if previous_db is None else round(float(previous_db), 2),
        "last_source_push_result": last_push,
        "last_clear_event": last_clear,
        "last_handoff": last_handoff,
    }
