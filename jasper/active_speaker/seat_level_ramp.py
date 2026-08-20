# SPDX-FileCopyrightText: 2026 Jasper Curry

#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop seat-SPL leveling: find the volume that measures the target.

The crossover session's reference volume was a codified guess. This step makes
it an observation: roll the main volume slowly up from a quiet floor while a
CALIBRATED measurement mic at the listening seat watches, stop when the seat SPL
settles inside the operator's band, and bank the volume that got there
(:mod:`seat_level_reference`) for
:func:`jasper.active_speaker.session_volume_plan.session_measurement_volume_db`
to consume.

**Almost none of this is new machinery.** The ramp itself is
:class:`jasper.audio_measurement.ramp.RampController` — the settle-based
two-point level-match kernel that already owns the quiet start, the coarse
staircase, the stop-ahead pre-window, the settled jump, the confirm streak, the
clip abort, the trust floor, the feed-liveness abort, the derived safety
timeout, and the fade-before-tone-kill. The restore latch is
:class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan` (durable
intent before the first mutation, set-and-confirm, restore exactly once), given
its OWN statefile so it cannot collide with the crossover session's plan. This
module contributes exactly three things the kernel cannot know:

1. **The window is a dB SPL band, not a dBFS one.** The mic's calibration file
   carries the absolute reference
   (:class:`jasper.audio_measurement.calibration.MicSensitivity`); this module
   converts the operator's seat-SPL band into the mic-dBFS window the kernel
   ramps toward. No calibration means no absolute level, so the step REFUSES
   (``mic_calibration_unavailable``) rather than chasing an uncalibrated number.
2. **Two ceilings the kernel has no vocabulary for** — the driver-cap volume
   ceiling (``driver_cap_ceiling_db``, the same derivation the session volume is
   bounded by) as the ramp's cap, and the profile's
   ``max_commissioning_level_db_spl`` as a live, measured seat-SPL ceiling.
3. **The runaway guard.** A wired measurement mic that is plugged in but not
   observing the speaker — capturing the wrong card, in a bag, muted at the
   OS — keeps delivering samples at its noise floor, so neither the feed-
   liveness timeout (samples ARE arriving) nor the trust floor (they are simply
   dropped) stops the climb: the kernel would walk all the way to its cap.
   :class:`_MicObservationGuard` requires the observed level to actually RISE
   as the commanded volume rises, and aborts the moment it does not.

Every refusal restores the household volume through the latch and persists
nothing. Only a converged, in-window lock banks a reference.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.ramp import (
    HARD_CEILING_DBFS,
    LevelSample,
    MeasurementRamp,
    RampController,
    RampLockKind,
    RampState,
)
from jasper.log_event import log_event

from .seat_level_reference import (
    SeatLevelTarget,
    SeatLevelTargetError,
    write_seat_level_reference,
)
from .session_volume_plan import (
    SessionVolumeOpenResult,
    SessionVolumePlan,
    SessionVolumePlanError,
)
from .volume_latch import GetMainVolumeDb, SetMainVolumeDb

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_seat_level_volume.json")

# The quiet floor the climb starts from. The kernel's own default, and 30 dB
# below the codified -20 dB reference, so the first audible moment of a leveling
# pass is far under any level the session would hold.
SEAT_LEVEL_START_DB = -50.0

# Worst-case delay between commanding a volume and seeing it in a sample. A
# wired mic read in-process is chunk-bounded, an order of magnitude tighter than
# the phone relay's 2 s default; the kernel's overshoot invariant
# (``step + rate * latency < half the window``) is why this must be stated
# rather than inherited — the phone value would refuse a 5 dB-wide window.
SEAT_LEVEL_MAX_LOOP_LATENCY_S = 0.5

# Deliberately non-binding. The kernel's dynamic cap is
# ``min(original + cap_bump_db, cap_ceil_db)``, where ``original`` is whatever
# volume was live when the ramp started — and the latch has already parked that
# at SEAT_LEVEL_START_DB, so a household-relative bump would cap this ramp ~38 dB
# below any usable measurement level. The operative ceiling here is the
# driver-cap ceiling passed as ``max_main_volume_db``, backed by the live
# seat-SPL ceiling and the kernel's own 0 dB hard ceiling.
SEAT_LEVEL_CAP_BUMP_DB = 120.0

# The runaway guard. Once the commanded volume has climbed this far above the
# level at which the mic first reported, the observed level must have risen by
# at least MIC_RESPONSE_MIN_RISE_DB. The chain is LTI and main_volume adds in dB,
# so a mic that is genuinely listening rises ~1 dB per commanded dB; demanding
# half of it tolerates room noise, reading jitter and a compressed early chain,
# while a mic that is not listening (rise ~0) fails decisively. 12 dB is ~16
# staircase steps — reached long before the pre-window on any working chain, and
# ~6 s at the default cadence.
MIC_RESPONSE_PROBE_DB = 12.0
MIC_RESPONSE_MIN_RISE_DB = 6.0

# Refusal codes. Stable strings: they are the operator-facing reason and the
# `event=` field, so they are named once here.
REFUSE_MIC_NOT_OBSERVING = "mic_not_observing"
REFUSE_SPL_CEILING_EXCEEDED = "spl_ceiling_exceeded"
REFUSE_SPL_TARGET_UNREACHABLE = "spl_target_unreachable"
REFUSE_MIC_FEED_LOST = "mic_feed_lost"
REFUSE_MIC_CLIPPING = "mic_clipping"
REFUSE_RAMP_TIMEOUT = "ramp_timeout"
REFUSE_RAMP_ERROR = "ramp_error"
REFUSE_RAMP_CONFIG_INVALID = "ramp_config_invalid"
REFUSE_SPL_TARGET_UNCAPTURABLE = "spl_target_uncapturable"
REFUSE_VOLUME_CEILING_TOO_LOW = "volume_ceiling_below_ramp_start"
REFUSE_VOLUME_LATCH_UNCONFIRMED = "volume_latch_unconfirmed"


class SeatLevelRampError(RuntimeError):
    """The leveling step cannot form a safe ramp from these inputs."""


@dataclass(frozen=True)
class SeatLevelResult:
    """The outcome of one leveling pass.

    ``status`` is ``"converged"`` or ``"refused"``; a refusal always carries a
    ``reason`` from the ``REFUSE_*`` set and persisted nothing.
    """

    status: str
    reason: str | None = None
    reference_volume_db: float | None = None
    measured_db_spl: float | None = None
    ramp: dict[str, Any] = field(default_factory=dict)

    @property
    def converged(self) -> bool:
        return self.status == "converged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "reference_volume_db": self.reference_volume_db,
            "measured_db_spl": self.measured_db_spl,
            "ramp": self.ramp,
        }


SampleSource = Callable[[], Awaitable[list[LevelSample]]]


class _MicObservationGuard:
    """Wraps the sample source with the two SPL-domain aborts.

    Sits BEFORE the kernel's trust floor on purpose: an ambient-dominated sample
    is exactly the evidence the runaway guard needs, and the kernel drops those
    before the state machine ever sees them.

    Aborts by calling :meth:`RampController.cancel`, the kernel's own
    abort-and-restore path (fade down, kill the tone, return to the pre-ramp
    volume). The loop re-checks the cancel flag at the top of each tick and ticks
    every 10 ms, while it steps the volume only every ``step_interval_s``
    (0.5 s), so an abort lands with no further volume step.
    """

    def __init__(
        self,
        *,
        controller: RampController,
        source: SampleSource,
        sensitivity: MicSensitivity,
        spl_ceiling_db_spl: float,
    ) -> None:
        self._controller = controller
        self._source = source
        self._sensitivity = sensitivity
        self._spl_ceiling_db_spl = float(spl_ceiling_db_spl)
        self.refusal: str | None = None
        self.last_rms_dbfs: float | None = None
        self._first_rms_dbfs: float | None = None
        self._first_commanded_db: float | None = None
        self._max_rms_dbfs: float | None = None

    async def __call__(self) -> list[LevelSample]:
        batch = await self._source()
        if self.refusal is not None:
            return batch
        commanded = self._controller.data.current_main_volume_db
        for sample in batch:
            if not math.isfinite(sample.rms_dbfs):
                continue
            self.last_rms_dbfs = sample.rms_dbfs
            if self._first_rms_dbfs is None:
                self._first_rms_dbfs = sample.rms_dbfs
                self._first_commanded_db = commanded
            self._max_rms_dbfs = (
                sample.rms_dbfs
                if self._max_rms_dbfs is None
                else max(self._max_rms_dbfs, sample.rms_dbfs)
            )
            observed_db_spl = self._sensitivity.db_spl_from_dbfs(sample.rms_dbfs)
            if observed_db_spl > self._spl_ceiling_db_spl:
                await self._abort(
                    REFUSE_SPL_CEILING_EXCEEDED,
                    observed_db_spl=f"{observed_db_spl:.1f}",
                    ceiling_db_spl=f"{self._spl_ceiling_db_spl:.1f}",
                    commanded_db=f"{commanded:.2f}",
                )
                return batch
        if self._runaway(commanded):
            first_commanded = float(self._first_commanded_db or 0.0)
            first_rms = float(self._first_rms_dbfs or 0.0)
            await self._abort(
                REFUSE_MIC_NOT_OBSERVING,
                commanded_climb_db=f"{commanded - first_commanded:.2f}",
                observed_rise_db=f"{float(self._max_rms_dbfs or 0.0) - first_rms:.2f}",
                required_rise_db=f"{MIC_RESPONSE_MIN_RISE_DB:.2f}",
                commanded_db=f"{commanded:.2f}",
            )
        return batch

    def _runaway(self, commanded_db: float) -> bool:
        """True once the volume has climbed the probe span without a response."""
        if self._first_commanded_db is None or self._first_rms_dbfs is None:
            return False
        if commanded_db - self._first_commanded_db < MIC_RESPONSE_PROBE_DB:
            return False
        rise = float(self._max_rms_dbfs or self._first_rms_dbfs) - self._first_rms_dbfs
        return rise < MIC_RESPONSE_MIN_RISE_DB

    async def _abort(self, reason: str, **evidence: str) -> None:
        self.refusal = reason
        log_event(
            logger,
            "active_speaker.seat_level_abort",
            level=logging.WARNING,
            fields={"reason": reason, **evidence},
        )
        await self._controller.cancel()


def build_seat_level_ramp_config(
    *,
    target: SeatLevelTarget,
    sensitivity: MicSensitivity,
    max_main_volume_db: float,
) -> MeasurementRamp:
    """Translate a seat-SPL band into the kernel's mic-dBFS ramp config.

    The window is the band converted through the mic's own sensitivity; the cap
    is the driver-cap volume ceiling. Raises :class:`SeatLevelRampError` when the
    result would not be a ramp: a band the mic cannot capture without clipping,
    a ceiling at or below the quiet start, or a band too narrow for the kernel's
    overshoot invariant at this step/latency.
    """
    window_low = sensitivity.dbfs_from_db_spl(target.low_db_spl)
    window_high = sensitivity.dbfs_from_db_spl(target.high_db_spl)
    if window_high > HARD_CEILING_DBFS:
        raise SeatLevelRampError(
            f"{REFUSE_SPL_TARGET_UNCAPTURABLE}: {target.high_db_spl:g} dB SPL is "
            f"{window_high:.1f} dBFS at this mic — above digital full scale, so "
            "the band cannot be reached without clipping the capture"
        )
    ceiling = min(float(max_main_volume_db), HARD_CEILING_DBFS)
    if ceiling <= SEAT_LEVEL_START_DB:
        raise SeatLevelRampError(
            f"{REFUSE_VOLUME_CEILING_TOO_LOW}: the driver-cap ceiling "
            f"{ceiling:.1f} dB is at or below the {SEAT_LEVEL_START_DB:g} dB ramp "
            "start; there is no room to climb"
        )
    try:
        return MeasurementRamp.from_env(
            window_low_dbfs=window_low,
            window_high_dbfs=window_high,
            start_db=SEAT_LEVEL_START_DB,
            max_loop_latency_s=SEAT_LEVEL_MAX_LOOP_LATENCY_S,
            cap_bump_db=SEAT_LEVEL_CAP_BUMP_DB,
            cap_ceil_db=ceiling,
        )
    except ValueError as exc:
        raise SeatLevelRampError(f"{REFUSE_RAMP_CONFIG_INVALID}: {exc}") from exc


def _refusal_for(ramp_state: RampState, data: Any, guard_refusal: str | None) -> str:
    """Map a kernel terminal (plus any guard abort) onto one refusal code."""
    if guard_refusal is not None:
        return guard_refusal
    if ramp_state is RampState.MAXED_OUT:
        return REFUSE_SPL_TARGET_UNREACHABLE
    if ramp_state is RampState.LOCKED:
        # Not an in-window lock: a manual or bounded-low lock never proves the
        # requested band was reached, so it is a refusal here even though the
        # kernel calls it a lock.
        return REFUSE_SPL_TARGET_UNREACHABLE
    if ramp_state is RampState.ABORTED:
        # The kernel reaches ABORTED two ways and distinguishes them only in its
        # error sentence: a clip in a batch, or the feed going silent.
        error = str(getattr(data, "error", "") or "")
        return REFUSE_MIC_FEED_LOST if "feed lost" in error else REFUSE_MIC_CLIPPING
    if ramp_state is RampState.CANCELLED:
        return REFUSE_RAMP_TIMEOUT
    return REFUSE_RAMP_ERROR


async def run_seat_level_ramp(
    *,
    target: SeatLevelTarget,
    sensitivity: MicSensitivity,
    max_main_volume_db: float,
    spl_ceiling_db_spl: float,
    get_main_volume_db: GetMainVolumeDb,
    set_main_volume_db: SetMainVolumeDb,
    play_continuous_tone: Callable[[], Awaitable[Any]],
    cancel_tone: Callable[[], None],
    next_samples: SampleSource,
    noise_floor_dbfs: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session_id: str = "seat_level",
    volume_state_path: str | Path | None = DEFAULT_STATE_PATH,
    reference_state_path: str | Path | None = None,
) -> SeatLevelResult:
    """Ramp to the seat-SPL band and bank the volume that reached it.

    ``target`` must already be validated against ``spl_ceiling_db_spl`` by the
    caller (:meth:`SeatLevelTarget.validate`) — this function enforces the
    ceiling live, on measured samples, which is a different job from admitting
    the request.

    ``max_main_volume_db`` is the driver-cap volume ceiling from
    :func:`jasper.active_speaker.session_volume_plan.driver_cap_ceiling_db`. The
    ramp never commands above it.

    The household volume is snapshotted and restored through
    :class:`SessionVolumePlan` on EVERY exit path — converged, refused, or
    raised — and the plan holds its own statefile so it never contends with the
    crossover session's. A plan that cannot confirm its restore latches
    unresolved for the recovery path exactly as it does for a session.

    Persists a reference only on a converged, in-window lock.
    """
    config = build_seat_level_ramp_config(
        target=target,
        sensitivity=sensitivity,
        max_main_volume_db=max_main_volume_db,
    )
    plan = SessionVolumePlan(state_path=volume_state_path)
    if plan.needs_recovery:
        await plan.recover_unresolved(set_main_volume_db, get_main_volume_db)
        plan = SessionVolumePlan(state_path=volume_state_path)
    try:
        opened = await plan.open(
            SEAT_LEVEL_START_DB, set_main_volume_db, get_main_volume_db
        )
    except SessionVolumePlanError:
        # An undrainable prior latch. Nothing was mutated here; the recovery
        # path owns that state and must run before another pass.
        logger.exception("seat-level ramp could not open the volume latch")
        opened = SessionVolumeOpenResult.FAILED
    if opened is not SessionVolumeOpenResult.OPENED:
        log_event(
            logger,
            "active_speaker.seat_level_refused",
            level=logging.ERROR,
            reason=REFUSE_VOLUME_LATCH_UNCONFIRMED,
            open_result=opened.value,
        )
        return SeatLevelResult(
            status="refused", reason=REFUSE_VOLUME_LATCH_UNCONFIRMED
        )

    log_event(
        logger,
        "active_speaker.seat_level_start",
        session=session_id,
        target_db_spl=f"{target.target_db_spl:.1f}",
        band_db_spl=f"[{target.low_db_spl:.1f},{target.high_db_spl:.1f}]",
        window_dbfs=(
            f"[{config.window_low_dbfs:.1f},{config.window_high_dbfs:.1f}]"
        ),
        sens_factor_db=f"{sensitivity.sens_factor_db:.2f}",
        analog_gain_db=(
            "" if sensitivity.analog_gain_db is None
            else f"{sensitivity.analog_gain_db:.1f}"
        ),
        max_main_volume_db=f"{config.cap_ceil_db:.2f}",
        spl_ceiling_db_spl=f"{spl_ceiling_db_spl:.1f}",
        start_db=f"{config.start_db:.1f}",
        step_db=f"{config.step_db:.2f}",
    )

    controller = RampController(session_id=session_id, config=config)
    guard = _MicObservationGuard(
        controller=controller,
        source=next_samples,
        sensitivity=sensitivity,
        spl_ceiling_db_spl=spl_ceiling_db_spl,
    )
    try:
        data = await controller.run(
            get_main_volume_db=get_main_volume_db,
            set_main_volume_db=set_main_volume_db,
            play_continuous_tone=play_continuous_tone,
            cancel_tone=cancel_tone,
            next_samples=guard,
            noise_floor_dbfs=noise_floor_dbfs,
            clock=clock,
            sleep=sleep,
        )
        return _finish(
            data=data,
            guard=guard,
            target=target,
            sensitivity=sensitivity,
            config=config,
            session_id=session_id,
            reference_state_path=reference_state_path,
        )
    finally:
        # Restore-exactly-once, on converged / refused / raised alike. The
        # kernel's own restore returns to the latched START floor (that is the
        # volume it snapshotted); this returns the household's.
        await plan.close(
            set_main_volume_db, get_main_volume_db, reason="seat_level_complete"
        )


def _finish(
    *,
    data: Any,
    guard: _MicObservationGuard,
    target: SeatLevelTarget,
    sensitivity: MicSensitivity,
    config: MeasurementRamp,
    session_id: str,
    reference_state_path: str | Path | None,
) -> SeatLevelResult:
    """Classify the terminal and, only on convergence, bank the reference."""
    snapshot = data.snapshot()
    last_rms_dbfs = guard.last_rms_dbfs
    # ``guard.last_rms_dbfs is not None`` is part of convergence, not a
    # cosmetic detail: the banked reference is only meaningful next to the SPL
    # that was actually measured at it, and an in-window lock without a single
    # observed sample would mean the kernel and the guard disagree about what
    # the mic said. Refuse rather than bank a reference with a fabricated level.
    converged = (
        guard.refusal is None
        and data.state is RampState.LOCKED
        and data.lock_kind is RampLockKind.IN_WINDOW
        and data.locked_main_volume_db is not None
        and last_rms_dbfs is not None
    )
    if not converged:
        reason = _refusal_for(data.state, data, guard.refusal)
        log_event(
            logger,
            "active_speaker.seat_level_refused",
            level=logging.WARNING,
            session=session_id,
            reason=reason,
            ramp_state=data.state.value,
            at_db=f"{data.current_main_volume_db:.2f}",
            ceiling_db=f"{config.cap_ceil_db:.2f}",
            observed_db_spl=(
                ""
                if last_rms_dbfs is None
                else f"{sensitivity.db_spl_from_dbfs(last_rms_dbfs):.1f}"
            ),
        )
        return SeatLevelResult(status="refused", reason=reason, ramp=snapshot)

    reference_volume_db = float(data.locked_main_volume_db)
    assert last_rms_dbfs is not None  # part of `converged` above
    measured_db_spl = sensitivity.db_spl_from_dbfs(last_rms_dbfs)
    try:
        write_seat_level_reference(
            reference_volume_db=reference_volume_db,
            measured_db_spl=measured_db_spl,
            target=target,
            sensitivity=sensitivity.to_dict(),
            max_main_volume_db=float(config.cap_ceil_db),
            state_path=reference_state_path,
        )
    except (SeatLevelTargetError, OSError) as exc:
        log_event(
            logger,
            "active_speaker.seat_level_refused",
            level=logging.ERROR,
            session=session_id,
            reason=REFUSE_RAMP_ERROR,
            error=str(exc),
        )
        return SeatLevelResult(
            status="refused", reason=REFUSE_RAMP_ERROR, ramp=snapshot
        )
    log_event(
        logger,
        "active_speaker.seat_level_converged",
        session=session_id,
        reference_volume_db=f"{reference_volume_db:.2f}",
        measured_db_spl=f"{measured_db_spl:.1f}",
        band_db_spl=f"[{target.low_db_spl:.1f},{target.high_db_spl:.1f}]",
        gain_map_db=(
            "" if data.gain_map_db is None else f"{data.gain_map_db:.2f}"
        ),
    )
    return SeatLevelResult(
        status="converged",
        reference_volume_db=reference_volume_db,
        measured_db_spl=measured_db_spl,
        ramp=snapshot,
    )
