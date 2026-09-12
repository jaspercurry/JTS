# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Microphone-driven leveling; the caller owns placement, isolation and restore."""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from jasper.audio_measurement.ramp import CEILING_MARGIN_DB, HARD_CEILING_DBFS, MAX_STEP_DB, SPL_CEILING_EXCEEDED, LevelSample, capped_gap_step_db
from jasper.audio_measurement.wired_capture import WiredSplCeilingExceeded
from jasper.env_load import bounded_env_float

from .restore_wait import resilient_restore
from .volume_latch import read_fader_db, set_and_confirm_volume

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import MicSensitivity

MIC_WINDOW_S = 0.5
SETTLED_AGREE_DB = 0.5
SETTLE_TIMEOUT_S = 8.0
START_FADER_DB = -40.0
DAMPING = 0.9
MIC_RESPONSE_MIN_RISE_DB = 6.0
REFUSE_LEVEL_UNSETTLED = "spl_level_unsettled"
REFUSE_LEVEL_UNREACHABLE = "level_unreachable"
REFUSE_MIC_NOT_OBSERVING = "mic_not_observing"
REFUSE_AMBIENT_TOO_HIGH = "level_ambient_too_high"
SampleSource = Callable[[], Awaitable[list[LevelSample]]]


@dataclass
class LevelResult:
    status: str
    reason: str | None = None
    gain_db: float | None = None
    leveled_db_spl: float | None = None
    ambient_db_spl: float | None = None
    readings: list[tuple[float, float]] = field(default_factory=list)


class _Refused(Exception):
    def __init__(self, reason: str, observed: float | None = None) -> None:
        self.reason, self.observed = reason, observed


async def _window_reading(
    next_samples: SampleSource, *, sensitivity: MicSensitivity, spl_ceiling_db_spl: float,
) -> float:
    readings: list[float] = []
    deadline = time.monotonic() + MIC_WINDOW_S
    while time.monotonic() < deadline:
        try:
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                samples = await next_samples()
        except TimeoutError:
            raise _Refused("mic_feed_lost") from None
        for sample in samples:
            if not math.isfinite(sample.rms_dbfs):
                if sample.clip:
                    raise _Refused("mic_clipping")
                continue
            observed_db_spl = sensitivity.db_spl_from_dbfs(sample.rms_dbfs)
            if observed_db_spl > spl_ceiling_db_spl:
                raise _Refused(SPL_CEILING_EXCEEDED, observed_db_spl)
            if sample.clip:
                raise _Refused("mic_clipping", observed_db_spl)
            readings.append(sample.rms_dbfs)
        await asyncio.sleep(0.05)
    if not readings:
        raise _Refused("mic_feed_lost")
    return statistics.median(readings)


def _settled_agree_db() -> float:
    return bounded_env_float("JASPER_SEAT_LEVEL_SETTLED_AGREE_DB", SETTLED_AGREE_DB, lo=0.1, hi=3.0)


async def _settle_reading(
    next_samples: SampleSource, *, sensitivity: MicSensitivity, spl_ceiling_db_spl: float,
) -> float:
    started = time.monotonic()
    previous: float | None = None
    agree_db = _settled_agree_db()
    timeout_s = bounded_env_float("JASPER_SEAT_LEVEL_SETTLE_TIMEOUT_S", SETTLE_TIMEOUT_S, lo=2.0, hi=30.0)
    while True:
        reading = await _window_reading(
            next_samples, sensitivity=sensitivity, spl_ceiling_db_spl=spl_ceiling_db_spl,
        )
        if previous is not None:
            if abs(reading - previous) <= agree_db:
                return sensitivity.db_spl_from_dbfs(reading)
            if time.monotonic() - started >= timeout_s:
                raise _Refused(REFUSE_LEVEL_UNSETTLED)
        previous = reading


def mic_is_not_observing(*, max_rise_db: float, min_rise_db: float) -> bool:
    return max_rise_db < min_rise_db


async def level_to(
    target_db_spl: float, *, tolerance_db: float, stop_db_spl: float,
    max_main_volume_db: float, sensitivity: MicSensitivity, next_samples: SampleSource,
    get_main_volume_db: Callable[[], Awaitable[float | None]],
    set_main_volume_db: Callable[[float], Awaitable[object]],
    play: Callable[[], Awaitable[object]], stop_playback: Callable[[], Awaitable[object]],
) -> LevelResult:
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

    async def write(value: float) -> None:
        nonlocal gain
        gain = min(value, cap)
        async with asyncio.timeout(SETTLE_TIMEOUT_S):
            if not await set_and_confirm_volume(gain, set_main_volume_db, get_main_volume_db):
                raise _Refused("volume_latch_unconfirmed")
        result.gain_db = gain

    async def stop() -> None:
        await stop_playback()

    async def start() -> None:
        async with asyncio.timeout(SETTLE_TIMEOUT_S):
            await play()

    try:
        await stop_playback()
        async with asyncio.timeout(SETTLE_TIMEOUT_S):
            current = await read_fader_db(get_main_volume_db)
        if current is None or not math.isfinite(current):
            raise _Refused("volume_latch_unconfirmed")
        await write(min(current, START_FADER_DB))
        result.ambient_db_spl = await _settle_reading(
            next_samples, sensitivity=sensitivity, spl_ceiling_db_spl=stop_db_spl,
        )
        if result.ambient_db_spl >= target_db_spl - min_rise:
            raise _Refused(REFUSE_AMBIENT_TOO_HIGH)
        await start()
        budget = 1
        in_band = 0
        max_rise = 0.0
        remeasured = False
        agree_db = _settled_agree_db()
        while len(result.readings) < budget:
            observed = await _settle_reading(
                next_samples, sensitivity=sensitivity, spl_ceiling_db_spl=stop_db_spl,
            )
            assert gain is not None
            result.readings.append((gain, observed))
            if len(result.readings) == 1:
                budget = math.ceil(abs(target_db_spl - observed) / MAX_STEP_DB) + 4
            if observed < result.ambient_db_spl and not remeasured:
                await stop_playback()
                result.ambient_db_spl = await _settle_reading(
                    next_samples, sensitivity=sensitivity, spl_ceiling_db_spl=stop_db_spl,
                )
                if result.ambient_db_spl >= target_db_spl - min_rise:
                    raise _Refused(REFUSE_AMBIENT_TOO_HIGH)
                remeasured = True
                await start()
                in_band = 0
                continue
            max_rise = max(max_rise, observed - result.ambient_db_spl)
            in_band = in_band + 1 if abs(observed - target_db_spl) <= tolerance_db and observed - result.ambient_db_spl >= min_rise else 0
            if in_band >= 2 and abs(observed - result.readings[-2][1]) <= agree_db:
                result.status, result.leveled_db_spl = "converged", observed
                return result
            if in_band:
                continue
            if gain == cap and observed < target_db_spl - tolerance_db:
                reason = (REFUSE_MIC_NOT_OBSERVING if mic_is_not_observing(
                    max_rise_db=max_rise, min_rise_db=min_rise,
                ) else REFUSE_LEVEL_UNREACHABLE)
                raise _Refused(reason)
            if len(result.readings) < budget:
                await write(gain + capped_gap_step_db(
                    measured_db=observed, target_db=(target_db_spl if observed > target_db_spl else observed + DAMPING * (target_db_spl - observed)), cap_db=MAX_STEP_DB,
                ))
        raise _Refused(REFUSE_LEVEL_UNREACHABLE)
    except WiredSplCeilingExceeded as exc:
        result.reason = SPL_CEILING_EXCEEDED
        if gain is not None:
            result.readings.append((gain, exc.observed_db_spl))
        return result
    except _Refused as exc:
        result.reason = exc.reason
        if exc.observed is not None and gain is not None:
            result.readings.append((gain, exc.observed))
        return result
    except TimeoutError:
        result.reason = "seat_level_watchdog_expired"
        return result
    finally:
        await resilient_restore(stop())
