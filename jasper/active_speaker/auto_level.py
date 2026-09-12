# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Sweep-driven leveling; the caller owns playback, isolation and restore."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from jasper.audio_measurement.ramp import CEILING_MARGIN_DB, HARD_CEILING_DBFS, MAX_STEP_DB, SPL_CEILING_EXCEEDED, capped_gap_step_db
from jasper.audio_measurement.wired_capture import WiredCaptureError, WiredSplCeilingExceeded
from jasper.env_load import bounded_env_float

from .volume_latch import read_fader_db, set_and_confirm_volume

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import MicSensitivity

AGREE_DB = 0.5  # Repeat sweeps at one fader must agree; see ADR-0308.
VOLUME_CONFIRM_TIMEOUT_S = 8.0
START_FADER_DB = -40.0
DAMPING = 0.9
MIC_RESPONSE_MIN_RISE_DB = 6.0
REFUSE_LEVEL_UNSETTLED = "spl_level_unsettled"
REFUSE_LEVEL_UNREACHABLE = "level_unreachable"
REFUSE_MIC_NOT_OBSERVING = "mic_not_observing"
REFUSE_AMBIENT_TOO_HIGH = "level_ambient_too_high"
LevelReader = Callable[[], Awaitable[float]]


def reading_budget(start: float, ceiling_db: float) -> int:
    return math.ceil((min(ceiling_db, HARD_CEILING_DBFS) - start) / MAX_STEP_DB) + 7


@dataclass
class LevelResult:
    status: str
    reason: str | None = None
    gain_db: float | None = None
    leveled_db_spl: float | None = None
    ambient_db_spl: float | None = None
    readings: list[tuple[float, float]] = field(default_factory=list)


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def mic_is_not_observing(*, max_rise_db: float, min_rise_db: float) -> bool:
    return max_rise_db < min_rise_db


async def level_to(
    target_db_spl: float, *, tolerance_db: float, stop_db_spl: float,
    max_main_volume_db: float, sensitivity: MicSensitivity,
    read_level: LevelReader, read_ambient: LevelReader,
    get_main_volume_db: Callable[[], Awaitable[float | None]],
    set_main_volume_db: Callable[[float], Awaitable[object]],
) -> LevelResult:
    """The watched reader enforces the SPL stop; an unwatched reader has no stop."""
    if not all(math.isfinite(value) for value in (
        target_db_spl, tolerance_db, stop_db_spl, max_main_volume_db, sensitivity.sens_factor_db,
    )) or tolerance_db <= 0 or target_db_spl + tolerance_db > stop_db_spl - MAX_STEP_DB - CEILING_MARGIN_DB:
        raise ValueError("Level target and tolerance must fit below the commissioning stop")
    cap = min(max_main_volume_db, HARD_CEILING_DBFS)
    result = LevelResult("refused")
    if sensitivity.dbfs_from_db_spl(target_db_spl + tolerance_db) > HARD_CEILING_DBFS:
        return LevelResult("refused", "spl_target_uncapturable")
    gain: float | None = None
    min_rise = bounded_env_float("JASPER_SEAT_LEVEL_MIN_RISE_DB", MIC_RESPONSE_MIN_RISE_DB, lo=1.0, hi=20.0)
    max_rise = 0.0

    async def write(value: float) -> None:
        nonlocal gain
        gain = min(value, cap)
        async with asyncio.timeout(VOLUME_CONFIRM_TIMEOUT_S):
            if not await set_and_confirm_volume(gain, set_main_volume_db, get_main_volume_db):
                raise _Refused("volume_latch_unconfirmed")
        result.gain_db = gain

    async def reading(reader: LevelReader) -> float:
        observed = await reader()
        if not math.isfinite(observed):
            raise _Refused("mic_feed_lost")
        return observed

    async def ambient() -> float:
        observed = await reading(read_ambient)
        result.ambient_db_spl = observed
        if observed >= target_db_spl - min_rise:
            raise _Refused(REFUSE_AMBIENT_TOO_HIGH)
        return observed

    def _cap_reason() -> str:
        return REFUSE_MIC_NOT_OBSERVING if mic_is_not_observing(
            max_rise_db=max_rise, min_rise_db=min_rise,
        ) else REFUSE_LEVEL_UNREACHABLE

    try:
        async with asyncio.timeout(VOLUME_CONFIRM_TIMEOUT_S):
            current = await read_fader_db(get_main_volume_db)
        if current is None or not math.isfinite(current):
            raise _Refused("volume_latch_unconfirmed")
        await write(min(current, START_FADER_DB))
        result.ambient_db_spl = await ambient()
        budget = 1
        in_band: float | None = None
        remeasured = ever_unsettled = last_buried = False
        agree_db = bounded_env_float("JASPER_SEAT_LEVEL_SETTLED_AGREE_DB", AGREE_DB, lo=0.1, hi=3.0)
        while len(result.readings) < budget:
            observed = await reading(read_level)
            assert gain is not None
            result.readings.append((gain, observed))
            if len(result.readings) == 1:
                budget = math.ceil(abs(target_db_spl - observed) / MAX_STEP_DB) + 4
            buried = observed < result.ambient_db_spl + min_rise
            if buried:
                # Room noise is a floor, not a level; allow another upward step.
                budget += 1
            if observed < result.ambient_db_spl and not remeasured:
                result.ambient_db_spl = await ambient()
                remeasured = True
                in_band = None
                if observed >= result.ambient_db_spl + min_rise:
                    continue
            last_buried = buried
            max_rise = max(max_rise, observed - result.ambient_db_spl)
            gap = target_db_spl - observed
            if not buried and abs(gap) <= tolerance_db:
                if in_band is not None:
                    if abs(observed - in_band) > agree_db:
                        raise _Refused(REFUSE_LEVEL_UNSETTLED)
                    result.status, result.leveled_db_spl = "converged", observed
                    return result
                in_band = observed
                continue
            unsettled = in_band is not None
            ever_unsettled |= unsettled
            in_band = None
            if gain >= cap - 1e-9 and (buried or unsettled or gap > tolerance_db):
                raise _Refused(_cap_reason())
            if len(result.readings) < budget:
                if buried or unsettled:
                    magnitude = min(MAX_STEP_DB, max(1.0, DAMPING * abs(gap)))
                    step_db = magnitude if gap >= 0 else -magnitude
                else:
                    step_db = capped_gap_step_db(
                        measured_db=observed,
                        target_db=target_db_spl if gap < 0 else observed + DAMPING * gap,
                        cap_db=MAX_STEP_DB,
                    )
                await write(gain + step_db)
        raise _Refused(REFUSE_LEVEL_UNSETTLED if ever_unsettled and not last_buried else _cap_reason())
    except WiredSplCeilingExceeded as exc:
        result.reason = SPL_CEILING_EXCEEDED
        if gain is not None:
            result.readings.append((gain, exc.observed_db_spl))
        return result
    except WiredCaptureError:
        result.reason = "mic_feed_lost"
        return result
    except _Refused as exc:
        result.reason = exc.reason
        return result
    except TimeoutError:
        result.reason = "seat_level_watchdog_expired"
        return result
