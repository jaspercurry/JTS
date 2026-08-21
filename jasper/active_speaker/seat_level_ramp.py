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
intent before the first mutation, set-and-confirm, restore exactly once) on the
SAME durable statefile a measurement session uses, so the recovery machinery
that already watches that file watches a leveling pass too. This module
contributes exactly three things the kernel cannot know:

1. **The window is a dB SPL band, not a dBFS one.** The mic's calibration file
   carries the absolute reference
   (:class:`jasper.audio_measurement.calibration.MicSensitivity`); this module
   converts the operator's seat-SPL band into the mic-dBFS window the kernel
   ramps toward. No calibration means no absolute level, so the step REFUSES
   (``mic_calibration_unavailable``) rather than chasing an uncalibrated number.
2. **A ceiling the kernel has no vocabulary for** — the loudest main volume at
   which the ACTUAL stimulus admits for EVERY driver
   (``unsegmented_stimulus_ceiling_db``). It is derived from the excitation
   ledger and the stimulus bytes and contains no measured level, so a
   mis-calibrated microphone cannot move it. The measured seat-SPL ceiling
   (``max_commissioning_level_db_spl``) is a second, softer stop that shares the
   calibration's fate — which is exactly why it is not the one holding the line.
3. **The ambient floor, and the two rules that read it.** A wired measurement
   mic that is plugged in but not observing the speaker — capturing the wrong
   card, in a bag, muted at the OS — keeps delivering samples at its noise
   floor, so neither the feed-liveness timeout (samples ARE arriving) nor the
   trust floor (they are simply dropped) stops the climb. Measuring the room
   ONCE before the tone gives the kernel its trust threshold, gives
   :class:`_MicObservationGuard` a "did anything actually rise" test that does
   not false-abort a speaker quieter than the room, and gives convergence the
   same test so a stuck-constant mic can never bank a reference.

Every refusal restores the household volume through the latch and persists
nothing. Only a converged, in-window lock banks a reference.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from jasper.env_load import bounded_env_float
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.ramp import (
    HARD_CEILING_DBFS,
    LevelSample,
    MeasurementRamp,
    RampController,
    RampLockKind,
    RampState,
)
from jasper.correction.coordinator import (
    MeasurementWindowError,
    measurement_window,
)
from jasper.log_event import log_event

from .seat_level_reference import (
    SeatLevelTarget,
    SeatLevelTargetError,
    write_seat_level_reference,
)
from .session_volume_plan import (
    DEFAULT_SESSION_VOLUME_STATE_PATH,
    SessionVolumeOpenResult,
    SessionVolumePlan,
    SessionVolumePlanError,
    live_measurement_session,
)
from .volume_latch import GetMainVolumeDb, SetMainVolumeDb

logger = logging.getLogger(__name__)

# The quiet floor the climb starts from. The kernel's own default, and 30 dB
# below the codified -20 dB reference, so the first audible moment of a leveling
# pass is far under any level the session would hold.
SEAT_LEVEL_START_DB = -50.0

# This pass's identity in the shared measurement window: the mux diagnostic-gate
# owner (registered in ``mux.FANIN_TEST_OWNERS``, so ``TEST_RELEASE`` stays
# owner-scoped) AND jasper-control's volume-hold owner, because the window
# reuses one name for both. It is what ``/state.measurement.owner`` and every
# ``event=measurement.hold_*`` line say while a leveling pass is running.
SEAT_LEVEL_GATE_OWNER = "seat-level"

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

# How long the mic listens to a silent room before the tone starts. The measured
# ambient floor it produces is the ONE measurement three rules read: the kernel's
# trust threshold, the runaway guard's "did anything rise", and convergence's
# "did the reading rise at all". One second of a stationary room is plenty for a
# median; longer just delays the operator.
AMBIENT_WINDOW_S = 1.0

# The runaway guard, and the same rise convergence requires. Once the commanded
# volume has climbed MIC_RESPONSE_PROBE_DB above the ramp's start, the observed
# level must sit at least MIC_RESPONSE_MIN_RISE_DB ABOVE the measured ambient
# floor.
#
# Measuring rise against AMBIENT rather than against the first reading is what
# keeps a real chain from being falsely aborted: a speaker that starts quieter
# than the room pins the mic at ambient for the first several steps, so a 1:1
# "track the commanded volume" test fires on a perfectly good mic in a normal
# room (chain 80 dB / ambient 45 dB, and 85/42, both do this). Against ambient,
# such a chain simply has to emerge — which it does, long before the ceiling —
# while a mic that is not listening never emerges at all, because ITS ambient
# reading and ITS signal reading are the same number.
#
# 20 dB of probe span is ~27 staircase steps (~13 s), sized so a chain starting
# below the room floor has room to climb through it. Both are deploy-time knobs
# (ramp.py's convention for every hardware-gated threshold) because the right
# span depends on the room's floor and the amp's gain, which only hardware
# knows.
MIC_RESPONSE_PROBE_DB = 20.0
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
# Reuses jasper-angle-capture's slug verbatim: same door, same durable fact.
REFUSE_SESSION_ALREADY_LIVE = "measurement_session_already_live"
# The shared measurement window would not open — another measurement holds the
# speaker, or mux could not prove household music is out of the mix. Distinct
# from REFUSE_SESSION_ALREADY_LIVE, which is the durable volume-latch fact read
# before anything is attempted.
REFUSE_ISOLATION_UNAVAILABLE = "measurement_isolation_unavailable"


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
    detail: str | None = None
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
            "detail": self.detail,
            "reference_volume_db": self.reference_volume_db,
            "measured_db_spl": self.measured_db_spl,
            "ramp": self.ramp,
        }


SampleSource = Callable[[], Awaitable[list[LevelSample]]]


async def measure_ambient_dbfs(
    next_samples: SampleSource,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    window_s: float = AMBIENT_WINDOW_S,
) -> float | None:
    """Median mic level over a silent window — the ONE measurement three rules read.

    Called after the latch has parked the volume at the quiet floor and before
    the tone starts, so the room is as silent as this session will see it.
    Returns ``None`` when the mic delivered nothing finite; the caller refuses
    rather than assuming a floor.
    """
    readings: list[float] = []
    deadline = clock() + float(window_s)
    while clock() < deadline:
        for sample in await next_samples():
            if math.isfinite(sample.rms_dbfs):
                readings.append(sample.rms_dbfs)
        await sleep(0.05)
    return statistics.median(readings) if readings else None


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

    :attr:`rise_db` — how far the loudest observed reading sits above the
    measured ambient floor — is the guard's whole state, and convergence reads
    the same number so a stuck-constant mic cannot bank a reference.
    """

    def __init__(
        self,
        *,
        controller: RampController,
        source: SampleSource,
        sensitivity: MicSensitivity,
        spl_ceiling_db_spl: float,
        ambient_dbfs: float,
        start_volume_db: float,
        probe_db: float,
        min_rise_db: float,
    ) -> None:
        self._controller = controller
        self._source = source
        self._sensitivity = sensitivity
        self._spl_ceiling_db_spl = float(spl_ceiling_db_spl)
        self._ambient_dbfs = float(ambient_dbfs)
        self._start_volume_db = float(start_volume_db)
        self._probe_db = float(probe_db)
        self._min_rise_db = float(min_rise_db)
        self.refusal: str | None = None
        self.last_rms_dbfs: float | None = None
        self._max_rms_dbfs: float | None = None

    @property
    def min_rise_db(self) -> float:
        """The rise the guard demands — convergence demands the same one."""
        return self._min_rise_db

    @property
    def rise_db(self) -> float:
        """Loudest observed reading minus the ambient floor. Never negative.

        A non-finite feed never raises ``_max_rms_dbfs``, so NaN/inf samples
        score as no rise instead of defeating the guard by leaving it unarmed.
        """
        if self._max_rms_dbfs is None:
            return 0.0
        return max(0.0, self._max_rms_dbfs - self._ambient_dbfs)

    async def __call__(self) -> list[LevelSample]:
        batch = await self._source()
        if self.refusal is not None:
            return batch
        commanded = self._controller.data.current_main_volume_db
        for sample in batch:
            if not math.isfinite(sample.rms_dbfs):
                continue
            self.last_rms_dbfs = sample.rms_dbfs
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
            await self._abort(
                REFUSE_MIC_NOT_OBSERVING,
                commanded_climb_db=f"{commanded - self._start_volume_db:.2f}",
                ambient_dbfs=f"{self._ambient_dbfs:.1f}",
                observed_rise_db=f"{self.rise_db:.2f}",
                required_rise_db=f"{self._min_rise_db:.2f}",
                commanded_db=f"{commanded:.2f}",
            )
        return batch

    def _runaway(self, commanded_db: float) -> bool:
        """True once the volume climbed the probe span without emerging from ambient."""
        if commanded_db - self._start_volume_db < self._probe_db:
            return False
        return self.rise_db < self._min_rise_db

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
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session_id: str = "seat_level",
    volume_state_path: str | Path | None = None,
    reference_state_path: str | Path | None = None,
) -> SeatLevelResult:
    """Ramp to the seat-SPL band and bank the volume that reached it.

    ``target`` must already be validated against ``spl_ceiling_db_spl`` by the
    caller (:meth:`SeatLevelTarget.validate`) — this function enforces the
    ceiling live, on measured samples, which is a different job from admitting
    the request.

    ``max_main_volume_db`` is the mic-independent ceiling from
    :func:`jasper.active_speaker.session_volume_plan.unsegmented_stimulus_ceiling_db`
    — the loudest volume at which the actual stimulus admits for EVERY driver.
    The ramp never commands above it.

    **The hold rides the crossover session's own volume plan, on its own
    statefile.** A leveling pass holds the speaker exactly as a session does, so
    it joins the family the recovery machinery already watches: the volume
    recovery screen, ``/state.crossover_v2.needs_recovery``, and the flow's
    force-drain all see a killed leveling process, and it refuses to start under
    a live or unresolved session for the same reason
    ``jasper-angle-capture stage`` does (:func:`live_measurement_session`). A
    private statefile would have been invisible to every one of them.

    **The whole pass runs inside the shared measurement window**
    (:func:`jasper.correction.coordinator.measurement_window`), owned as
    ``seat-level``. That is what stops jasper-voice's ``VolumeCoordinator``
    patrol reconciling the fader back toward the household level once a second
    while the ramp climbs, keeps household music out of the mix through mux's
    diagnostic gate, and holds jasper-control off applying host-slider volume
    observations. The window wraps ``plan.open`` too, since the latch's first
    write is a fader write like any other. A window that cannot open is a
    refusal (``measurement_isolation_unavailable``), and every lease it holds
    self-expires, so a killed pass frees the speaker with no operator step.

    Persists a reference only on a converged, in-window lock whose reading rose
    clear of the measured ambient floor.
    """
    config = build_seat_level_ramp_config(
        target=target,
        sensitivity=sensitivity,
        max_main_volume_db=max_main_volume_db,
    )
    probe_db = bounded_env_float(
        "JASPER_SEAT_LEVEL_PROBE_DB", MIC_RESPONSE_PROBE_DB, lo=3.0, hi=40.0
    )
    min_rise_db = bounded_env_float(
        "JASPER_SEAT_LEVEL_MIN_RISE_DB", MIC_RESPONSE_MIN_RISE_DB, lo=1.0, hi=20.0
    )
    busy = live_measurement_session(
        state_path=None if volume_state_path is None else Path(volume_state_path),
        action="leveling the seat SPL",
    )
    if busy is not None:
        log_event(
            logger,
            "active_speaker.seat_level_refused",
            level=logging.WARNING,
            reason=REFUSE_SESSION_ALREADY_LIVE,
            detail=busy,
        )
        return SeatLevelResult(
            status="refused", reason=REFUSE_SESSION_ALREADY_LIVE, detail=busy
        )

    async def _leveled_under_isolation() -> SeatLevelResult:
        """The whole pass, run with the measurement window already held."""
        plan = SessionVolumePlan(
            state_path=(
                DEFAULT_SESSION_VOLUME_STATE_PATH
                if volume_state_path is None
                else volume_state_path
            )
        )
        # A leveling pass is bounded by the kernel's own derived safety timeout, so
        # a killed process should surface far sooner than a measurement session's
        # 30-minute walked-away window.
        plan.set_wall_clock_ceiling_s(config.safety_timeout + 60.0)
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
            # The absolute SPL is only as true as the capture gain the vendor's
            # sens factor assumes. An operator reads journal lines, not docstrings.
            precondition="mic capture volume must be at maximum (amixer -c <card>)",
        )

        try:
            ambient_dbfs = await measure_ambient_dbfs(
                next_samples, clock=clock, sleep=sleep
            )
            if ambient_dbfs is None:
                log_event(
                    logger,
                    "active_speaker.seat_level_refused",
                    level=logging.WARNING,
                    session=session_id,
                    reason=REFUSE_MIC_FEED_LOST,
                    detail="the microphone delivered no finite sample before the tone",
                )
                return SeatLevelResult(status="refused", reason=REFUSE_MIC_FEED_LOST)
            log_event(
                logger,
                "active_speaker.seat_level_ambient",
                session=session_id,
                ambient_dbfs=f"{ambient_dbfs:.1f}",
                ambient_db_spl=f"{sensitivity.db_spl_from_dbfs(ambient_dbfs):.1f}",
                probe_db=f"{probe_db:.1f}",
                required_rise_db=f"{min_rise_db:.1f}",
            )
            controller = RampController(session_id=session_id, config=config)
            guard = _MicObservationGuard(
                controller=controller,
                source=next_samples,
                sensitivity=sensitivity,
                spl_ceiling_db_spl=spl_ceiling_db_spl,
                ambient_dbfs=ambient_dbfs,
                start_volume_db=config.start_db,
                probe_db=probe_db,
                min_rise_db=min_rise_db,
            )
            data = await controller.run(
                get_main_volume_db=get_main_volume_db,
                set_main_volume_db=set_main_volume_db,
                play_continuous_tone=play_continuous_tone,
                cancel_tone=cancel_tone,
                next_samples=guard,
                noise_floor_dbfs=ambient_dbfs,
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

    # Everything that touches the fader runs inside the shared measurement
    # window (jasper.correction.coordinator). Without it, jasper-voice's
    # VolumeCoordinator patrol keeps reconciling the main volume back toward
    # the household level once a second and fights the ramp the whole way up
    # -- the jts3 writer war (journal: `event=volume.reconciled source=idle
    # ... drift_db=+9.35`). The window ALSO holds jasper-control's
    # volume-observation hold, so a host slider cannot walk the level either.
    # It wraps `plan.open` as well as the ramp: the latch's own first write
    # is a fader write like any other.
    leveled: SeatLevelResult | None = None
    try:
        async with measurement_window(gate_owner=SEAT_LEVEL_GATE_OWNER):
            leveled = await _leveled_under_isolation()
        return leveled
    except MeasurementWindowError as exc:
        if leveled is not None:
            # The pass finished and already restored the household volume;
            # only teardown failed. Report the outcome that actually
            # happened and make the stuck isolation loud -- every lease
            # self-expires within ~2 minutes, so this is observable, not
            # actionable-or-bust.
            log_event(
                logger,
                "active_speaker.seat_level_isolation_cleanup_failed",
                level=logging.ERROR,
                session=session_id,
                error=str(exc),
            )
            return leveled
        log_event(
            logger,
            "active_speaker.seat_level_refused",
            level=logging.WARNING,
            session=session_id,
            reason=REFUSE_ISOLATION_UNAVAILABLE,
            detail=str(exc),
        )
        return SeatLevelResult(
            status="refused",
            reason=REFUSE_ISOLATION_UNAVAILABLE,
            detail=str(exc),
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
        # The SAME rise the runaway guard measures. A mic stuck at a constant
        # that happens to sit inside the target window would otherwise settle,
        # confirm, and bank a reference for a level nothing produced -- and
        # because its ambient reading IS its signal reading, its rise is zero.
        and guard.rise_db >= guard.min_rise_db
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
