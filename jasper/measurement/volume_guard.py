# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Volume snapshot/normalize/restore guards for measurement sessions."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

CALIBRATION_MAIN_VOLUME_DB = -12.0
CALIBRATION_SNAPCAST_PERCENT = 100


class VolumeGuardError(RuntimeError):
    """A calibration volume guard could not safely normalize the path."""


@dataclass(frozen=True)
class SnapcastClientSnapshot:
    channel: str
    name: str
    client_id: str
    group_id: str
    volume_percent: int
    muted: bool
    group_muted: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "name": self.name,
            "volume_percent": self.volume_percent,
            "muted": self.muted,
            "group_muted": self.group_muted,
        }


@dataclass(frozen=True)
class VolumeSnapshot:
    main_volume_db: float
    snapcast_clients: tuple[SnapcastClientSnapshot, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "main_volume_db": round(self.main_volume_db, 2),
            "snapcast_clients": [
                client.public_dict() for client in self.snapcast_clients
            ],
        }


@dataclass(frozen=True)
class VolumeGuardReport:
    snapshot: VolumeSnapshot
    calibration_main_volume_db: float
    calibration_snapcast_percent: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.public_dict(),
            "calibration_main_volume_db": round(
                self.calibration_main_volume_db, 2,
            ),
            "calibration_snapcast_percent": self.calibration_snapcast_percent,
        }


def _bare_hostname(value: str) -> str:
    text = str(value or "").strip().rstrip(".")
    if text.endswith(".local"):
        text = text[:-6]
    return text.split(".")[0]


def _candidate_names_for_member(
    channel: str,
    member: dict[str, Any],
    *,
    hostname: str,
) -> set[str]:
    if member.get("is_self"):
        return {
            _bare_hostname(member.get("snapcast_name", "")),
            _bare_hostname(hostname),
        } - {""}
    names = {
        _bare_hostname(member.get("snapcast_name", "")),
        _bare_hostname(member.get("label", "")),
        _bare_hostname(member.get("grouping", {}).get("peer_name", "")),
    }
    return {n for n in names if n}


def _resolve_snapcast_clients(
    rows: list[dict[str, Any]],
    members: dict[str, dict[str, Any]],
    *,
    hostname: str,
    want_stream: str,
) -> tuple[SnapcastClientSnapshot, ...]:
    by_name = {
        str(row.get("name") or ""): row
        for row in rows
        if str(row.get("name") or "")
    }
    snapshots: list[SnapcastClientSnapshot] = []
    missing: list[str] = []
    for channel, member in members.items():
        candidates = _candidate_names_for_member(
            channel, member, hostname=hostname,
        )
        row = next((by_name[name] for name in candidates if name in by_name), None)
        if row is None:
            missing.append(f"{channel}:{'/'.join(sorted(candidates)) or '?'}")
            continue
        if row.get("connected") is not True:
            raise VolumeGuardError(
                f"snapcast client {row.get('name') or '?'} is not connected"
            )
        if want_stream and row.get("stream_id") != want_stream:
            raise VolumeGuardError(
                f"snapcast client {row.get('name') or '?'} is bound to "
                f"{row.get('stream_id') or '(none)'} instead of {want_stream}"
            )
        client_id = str(row.get("client_id") or "")
        group_id = str(row.get("group_id") or "")
        if not client_id or not group_id:
            raise VolumeGuardError(
                f"snapcast client {row.get('name') or '?'} lacks an RPC id"
            )
        snapshots.append(SnapcastClientSnapshot(
            channel=channel,
            name=str(row.get("name") or ""),
            client_id=client_id,
            group_id=group_id,
            volume_percent=int(row.get("volume_percent", 100)),
            muted=bool(row.get("muted", False)),
            group_muted=bool(row.get("group_muted", False)),
        ))
    if missing:
        raise VolumeGuardError(
            "could not find snapcast client(s) for " + ", ".join(missing)
        )
    return tuple(snapshots)


async def _snapshot(
    *,
    hostname: str,
    members: dict[str, dict[str, Any]],
    camilla: Any,
) -> VolumeSnapshot:
    from jasper.multiroom.reconcile import SNAP_STREAM_ID
    from jasper.multiroom.snapcast_rpc import read_stream_clients

    main_volume = await camilla.get_volume_db(best_effort=False)
    if main_volume is None:
        raise VolumeGuardError("CamillaDSP did not report main_volume")
    rows = await asyncio.to_thread(read_stream_clients)
    if rows is None:
        raise VolumeGuardError("snapserver RPC unreachable")
    clients = _resolve_snapcast_clients(
        rows,
        members,
        hostname=hostname,
        want_stream=SNAP_STREAM_ID,
    )
    return VolumeSnapshot(
        main_volume_db=float(main_volume),
        snapcast_clients=clients,
    )


async def _set_snapcast_snapshot(
    clients: tuple[SnapcastClientSnapshot, ...],
    *,
    percent: int | None = None,
    muted: bool | None = None,
    group_muted: bool | None = None,
) -> None:
    from jasper.multiroom.snapcast_rpc import set_client_volume, set_group_mute

    for client in clients:
        want_group_muted = (
            client.group_muted if group_muted is None else bool(group_muted)
        )
        ok = await asyncio.to_thread(
            set_group_mute, client.group_id, want_group_muted,
        )
        if not ok:
            raise VolumeGuardError(
                f"could not set snapcast group mute for {client.name}"
            )
        want_percent = (
            client.volume_percent if percent is None else int(percent)
        )
        want_muted = client.muted if muted is None else bool(muted)
        ok = await asyncio.to_thread(
            set_client_volume,
            client.client_id,
            percent=want_percent,
            muted=want_muted,
        )
        if not ok:
            raise VolumeGuardError(
                f"could not set snapcast volume for {client.name}"
            )


def _camilla() -> Any:
    from jasper.camilla import CamillaController

    return CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )


@asynccontextmanager
async def normalized_pair_volumes(
    *,
    hostname: str,
    members: dict[str, dict[str, Any]],
    camilla: Any | None = None,
    calibration_main_volume_db: float = CALIBRATION_MAIN_VOLUME_DB,
    calibration_snapcast_percent: int = CALIBRATION_SNAPCAST_PERCENT,
) -> AsyncIterator[VolumeGuardReport]:
    """Normalize owned volume controls for a pair measurement, then restore.

    The caller should enter this inside ``measurement_window()`` so normal
    renderers are already paused before hidden mixers are brought to unity.
    """
    cam = camilla or _camilla()
    snapshot = await _snapshot(hostname=hostname, members=members, camilla=cam)
    normalized = False
    try:
        await cam.set_volume_db(calibration_main_volume_db, best_effort=False)
        await _set_snapcast_snapshot(
            snapshot.snapcast_clients,
            percent=calibration_snapcast_percent,
            muted=False,
            group_muted=False,
        )
        normalized = True
        report = VolumeGuardReport(
            snapshot=snapshot,
            calibration_main_volume_db=float(calibration_main_volume_db),
            calibration_snapcast_percent=int(calibration_snapcast_percent),
        )
        log_event(
            logger,
            "measurement.volume_normalized",
            main_volume_db=f"{calibration_main_volume_db:.1f}",
            snapcast_percent=calibration_snapcast_percent,
            snapcast_clients=",".join(
                c.name for c in snapshot.snapcast_clients
            ),
        )
        yield report
    finally:
        restore_errors: list[str] = []
        try:
            await _set_snapcast_snapshot(snapshot.snapcast_clients)
        except (OSError, RuntimeError, TimeoutError, ValueError) as e:
            restore_errors.append(f"snapcast:{e}")
        try:
            await cam.set_volume_db(snapshot.main_volume_db, best_effort=False)
        except (OSError, RuntimeError, TimeoutError, ValueError) as e:
            restore_errors.append(f"camilla:{e}")
        level = logging.ERROR if restore_errors else logging.INFO
        log_event(
            logger,
            "measurement.volume_restored",
            normalized=normalized,
            errors=";".join(restore_errors),
            level=level,
        )
        if restore_errors:
            logger.error(
                "measurement volume restore incomplete: %s",
                "; ".join(restore_errors),
            )
