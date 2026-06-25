# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Live multiroom pair-balance application.

The grouping reconciler remains the owner for structural changes: role,
channel, crossover, snapcast, and outputd wiring. Pair balance is different: it
is a scalar gain on the already-running endpoint path, and the UI needs that to
move at slider speed. This module owns only that live gain boundary; persisted
truth still lives in ``grouping.env``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from jasper.log_event import log_event

from .config import GROUPING_ENV_FILE, GroupingConfig, TRIM_DB_MAX, TRIM_DB_MIN, load_config

logger = logging.getLogger(__name__)

PAIR_BALANCE_FILTER = "pair_balance_trim"
OUTPUTD_CONTROL_SOCKET = "/run/jasper-outputd/control.sock"

ApplyMode = Literal["active_camilla", "outputd", "not_bonded"]


@dataclass(frozen=True)
class LiveTrimApplyResult:
    applied: bool
    mode: ApplyMode
    trim_db: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "applied": self.applied,
            "mode": self.mode,
            "trim_db": self.trim_db,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


def coerce_trim_db(trim_db: float) -> float:
    try:
        value = float(trim_db)
    except (TypeError, ValueError) as exc:
        raise ValueError("trim_db must be a number") from exc
    if not math.isfinite(value):
        raise ValueError("trim_db must be finite")
    value = round(value, 1)
    if not (TRIM_DB_MIN <= value <= TRIM_DB_MAX):
        raise ValueError(
            f"trim_db={value} must be between {TRIM_DB_MIN} and {TRIM_DB_MAX}"
        )
    return value


def active_endpoint(cfg: GroupingConfig, *, active_box_reader: Callable[[], bool]) -> bool:
    """Return true when this member's pair trim lives in CamillaDSP."""
    return (
        cfg.enabled
        and cfg.error is None
        and cfg.role in {"leader", "follower"}
        and active_box_reader()
    )


def camilla_patch_for_trim(trim_db: float) -> dict[str, Any]:
    """Patch only the named pair-balance gain stage."""
    trim = coerce_trim_db(trim_db)
    return {
        "filters": {
            PAIR_BALANCE_FILTER: {
                "parameters": {
                    "gain": trim,
                    "inverted": False,
                    "mute": False,
                }
            }
        }
    }


def _active_endpoint_camilla(cfg: GroupingConfig):
    if cfg.role == "leader":
        from jasper.camilla import crossover_controller

        return crossover_controller()

    from jasper.camilla import CamillaController

    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


async def _outputd_command(
    command: str,
    *,
    socket_path: str = OUTPUTD_CONTROL_SOCKET,
    timeout: float = 1.0,
) -> dict[str, Any]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path),
        timeout=timeout,
    )
    try:
        writer.write((command + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass
    if not line:
        raise RuntimeError("jasper-outputd returned no response")
    payload = json.loads(line.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("jasper-outputd returned non-object JSON")
    return payload


async def apply_local_trim(
    trim_db: float,
    *,
    cfg: GroupingConfig | None = None,
    active_box_reader: Callable[[], bool] | None = None,
    camilla_factory: Callable[[GroupingConfig], Any] | None = None,
    outputd_command: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
) -> LiveTrimApplyResult:
    """Apply this member's persisted pair trim to the currently running path."""
    trim = coerce_trim_db(trim_db)
    cfg = cfg or load_config(GROUPING_ENV_FILE)
    if not (cfg.enabled and cfg.error is None):
        return LiveTrimApplyResult(False, "not_bonded", trim, "grouping is not active")

    if active_box_reader is None:
        from .reconcile import is_active_speaker_box

        active_box_reader = is_active_speaker_box

    if active_endpoint(cfg, active_box_reader=active_box_reader):
        camilla = camilla_factory(cfg) if camilla_factory else _active_endpoint_camilla(cfg)
        try:
            ok = bool(await camilla.patch_config(camilla_patch_for_trim(trim), best_effort=True))
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            log_event(
                logger,
                "multiroom.balance.live_apply_failed",
                mode="active_camilla",
                trim=f"{trim:.1f}",
                error=str(exc),
                level=logging.WARNING,
            )
            return LiveTrimApplyResult(False, "active_camilla", trim, str(exc))
        log_event(
            logger,
            "multiroom.balance.live_apply",
            mode="active_camilla",
            trim=f"{trim:.1f}",
            ok=ok,
        )
        return LiveTrimApplyResult(
            ok,
            "active_camilla",
            trim,
            "" if ok else "CamillaDSP patch was not applied",
        )

    command = f"SET_DAC_CONTENT_TRIM_DB {trim:.1f}"
    try:
        payload = await (outputd_command or _outputd_command)(command)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        log_event(
            logger,
            "multiroom.balance.live_apply_failed",
            mode="outputd",
            trim=f"{trim:.1f}",
            error=str(exc),
            level=logging.WARNING,
        )
        return LiveTrimApplyResult(False, "outputd", trim, str(exc))
    if "error" in payload:
        detail = str(payload["error"])
        log_event(
            logger,
            "multiroom.balance.live_apply_failed",
            mode="outputd",
            trim=f"{trim:.1f}",
            error=detail,
            level=logging.WARNING,
        )
        return LiveTrimApplyResult(False, "outputd", trim, detail)

    applied = bool(payload.get("ok"))
    log_event(
        logger,
        "multiroom.balance.live_apply",
        mode="outputd",
        trim=f"{trim:.1f}",
        ok=applied,
    )
    return LiveTrimApplyResult(
        applied,
        "outputd",
        trim,
        "" if applied else "jasper-outputd did not acknowledge trim update",
    )
