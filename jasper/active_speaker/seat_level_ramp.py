# SPDX-FileCopyrightText: 2026 Jasper Curry

#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop seat-SPL leveling: find the volume that measures the target.

Play the crossover session's own stimulus while a CALIBRATED measurement mic at the
listening seat watches, step the main volume until the seat SPL lands inside the
operator's band, and bank the volume that got there (:mod:`seat_level_reference`) for
:func:`jasper.active_speaker.session_volume_plan.session_measurement_volume_db`.

Each step commands the remaining measured gap, saturated upward at one bite of
:data:`BITE_FRACTION` of ``ceiling - start``; downward steps are uncapped. The floor
every rise is judged against is ONLY ever measured with the speaker SILENT. A reference
is banked only from two consecutive settled readings that agree; every refusal restores
the household volume through the latch and persists nothing.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from jasper.env_load import bounded_env_float
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.ramp import (
    HARD_CEILING_DBFS,
    RECOVERABLE_ERRORS,
    LevelSample,
    capped_gap_step_db,
)
from jasper.correction.coordinator import (
    MeasurementWindowError,
    measurement_window,
)
from jasper.log_event import log_event

from .seat_level_reference import (
    SeatLevelTarget,
    SeatLevelTargetError,
    StimulusProvenance,
    write_seat_level_reference,
)
from .session_volume_plan import (
    DEFAULT_SESSION_VOLUME_STATE_PATH,
    FaderVolumeDoor,
    SessionVolumeOpenResult,
    SessionVolumePlan,
    SessionVolumePlanError,
    SessionVolumeRestoreResult,
    live_measurement_session,
)
from .volume_latch import GetMainVolumeDb, SetMainVolumeDb

logger = logging.getLogger(__name__)

# This pass's identity in the shared measurement window: the mux diagnostic-gate
# owner (``mux.FANIN_TEST_OWNERS``) AND jasper-control's volume-hold owner,
# named on ``/state.measurement.owner`` and ``event=measurement.hold_*``.
SEAT_LEVEL_GATE_OWNER = "seat-level"

# One WINDOW: the span whose median is one observation of the level. 0.5 s is
# ~12 samples from the wired meter's 2048-frame ALSA period (~42.7 ms at 48
# kHz). NOT a lag model -- :data:`SETTLED_AGREE_DB` measures settling time.
MIC_WINDOW_S = 0.5

# Two consecutive windows at the SAME LEVEL define "settled"; the later of
# the two is banked. Sized against room wander at a settled step (converged
# window varied 0.42 dB across its thirds vs 6.03 dB still climbing). Bounds
# a RATE, not a remaining distance (:func:`_settle_reading` states the
# residual). Deploy-time knob, disclosed on the receipt (``ramp.settle_agree_db``).
SETTLED_AGREE_DB = 0.5

# How long ONE reading may spend proving it settled before
# :data:`REFUSE_LEVEL_UNSETTLED`. Default covers a ~3 s chain time constant.
# A dead feed does NOT wait this out: :data:`REFUSE_MIC_FEED_LOST` after one
# window. Env-overridable, bounded so it cannot turn the pass into an
# unbounded tone.
SETTLE_TIMEOUT_S = 8.0

# The quiet floor the climb starts from: deliberately low and uninformed,
# under anything that could hurt a stranger's amplifier.
SEAT_LEVEL_START_DB = -50.0

# One bite, as a fraction of the run's OWN span (ceiling - start).
# Dimensionless on purpose: sweeps any chain in at most ceil(1/0.15) = 7
# bites regardless of gain. Distinct from calibration_level's
# AUDIBLE_RAMP_STEP_DB, a per-request 10 dB bound on an operator jump.
BITE_FRACTION = 0.15

# Rise a reading must clear to count as the SPEAKER rather than the room.
# Measured against the AMBIENT floor, never the first reading (a speaker
# starting quieter than the room would otherwise false-fire on a good mic).
# Deploy-time knob: the room's floor is only known to hardware.
MIC_RESPONSE_MIN_RISE_DB = 6.0

# Steps that commanded the FULL remaining gap that may land outside the band
# before the pass stops and reports the measured slope. One miss buys a
# correction; a second says the chain did not answer its own measurement
# twice running.
MAX_MISSED_FULL_STEPS = 2

# Commanded volumes closer together than this are the same volume (dB step
# arithmetic, fader not infinitely resolved).
STEP_EPSILON_DB = 0.05

# Samples of ONE window retained for its trace. A production window holds
# ~12 (2048-frame ALSA period, ~42.7 ms at 48 kHz); the sample that STOPPED
# a window is recorded outside this cap so truncation never loses it.
WINDOW_TRACE_MAX_SAMPLES = 256

# Extra silent READINGS one pass can spend re-measuring a contradicted floor.
# ONE, not a knob: a third reading asks the same question again with the
# same instrument (a twice-contradicted floor is `mic_not_observing`).
REMEASURE_READINGS = 1

# Extra readings the BANK CONFIRM adds to the walk's budget. ADDED rather
# than taken from the miss budget, so a chain needing its whole budget to
# reach the band still has one left to confirm with.
BANK_CONFIRM_READINGS = 1

# Whole-operation watchdog slack on top of the pass's own priced worst case
# (:func:`_watchdog_seconds`); a backstop against a wedged awaitable, never a
# step governor.
WATCHDOG_SLACK_S = 15.0

# Fade the commanded volume down before the tone is killed, so a broadband
# stimulus never stops at full level into the DAC. Applies at every edge a
# measurement level starts/stops (:func:`_fade_and_stop`, both edges of
# :func:`_remeasure_silence`); shape owned by :func:`fade_steps`.
FADE_STEP_DB = 2.0
FADE_STEP_S = 0.03
FADE_FLOOR_DB = -50.0

# Fade legs one pass can walk INSIDE the watchdog scope (silent re-measure's
# two edges plus the end-of-run fade) -- a code-path count, not a tunable.
FADE_LEGS_PER_PASS = 3

# Times a teardown step may be re-awaited after the pass is cancelled. A
# plain ``finally: await ...`` survives ONE ``task.cancel()`` but not a
# REPEATED one (Ctrl-C twice, a re-cancelling supervisor); each attempt here
# shields the teardown and re-awaits it.
TEARDOWN_SHIELD_ATTEMPTS = 4

# Refusal codes: stable strings, the operator-facing reason and `event=` field.
REFUSE_MIC_NOT_OBSERVING = "mic_not_observing"
REFUSE_SPL_CEILING_EXCEEDED = "spl_ceiling_exceeded"
REFUSE_SPL_TARGET_UNREACHABLE = "spl_target_unreachable"
REFUSE_MIC_FEED_LOST = "mic_feed_lost"
REFUSE_MIC_CLIPPING = "mic_clipping"
REFUSE_RAMP_ERROR = "ramp_error"
REFUSE_SPL_TARGET_UNCAPTURABLE = "spl_target_uncapturable"
REFUSE_VOLUME_CEILING_TOO_LOW = "volume_ceiling_below_ramp_start"
REFUSE_VOLUME_LATCH_UNCONFIRMED = "volume_latch_unconfirmed"
# Two full-gap steps landed outside the band; carries the measured slope.
REFUSE_LEVEL_UNCONVERGED = "spl_level_unconverged"
# The level never stopped moving. One slug, two scales: consecutive WINDOWS
# disagreeing until SETTLE_TIMEOUT_S, or consecutive READINGS disagreeing
# until the walk's budget runs out. Distinct from `spl_level_unconverged`
# (failing to REACH the band at all).
REFUSE_LEVEL_UNSETTLED = "spl_level_unsettled"
# The whole-operation watchdog fired: something the pass awaits never returned.
REFUSE_WATCHDOG_EXPIRED = "seat_level_watchdog_expired"
# Ctrl-C or any other cancellation; recorded, then propagated, never swallowed.
REFUSE_INTERRUPTED = "seat_level_interrupted"
# Reuses jasper-angle-capture's slug verbatim: same door, same durable fact.
REFUSE_SESSION_ALREADY_LIVE = "measurement_session_already_live"
# The shared measurement window would not open. Distinct from
# REFUSE_SESSION_ALREADY_LIVE, the durable volume-latch fact read first.
REFUSE_ISOLATION_UNAVAILABLE = "measurement_isolation_unavailable"

# Shared wording for the two sample-domain stops (:func:`_window_reading`,
# :func:`_watched_fade`); the ceiling itself is owned by ``spl_ceiling_db_spl``.
CLIPPED_CAPTURE_DETAIL = "the capture clipped; no level can be read from it"


def stimulus_level_phrase(stimulus: StimulusProvenance | None) -> str:
    """What the SIGNAL measures, so an unreachable target reads as arithmetic (the fourth of
    four numbers, beside target SPL, mic sensitivity, and volume ceiling). Empty when
    the caller measured no stimulus.
    """
    if stimulus is None:
        return ""
    return (
        f"; the stimulus measures {stimulus.rms_dbfs:.1f} dBFS RMS at "
        f"{stimulus.peak_dbfs:.1f} dBFS peak "
        f"({stimulus.peak_dbfs - stimulus.rms_dbfs:.1f} dB crest), leaving "
        f"{-stimulus.peak_dbfs:.1f} dB of digital headroom unused"
    )


def _stimulus_event_fields(stimulus: StimulusProvenance | None) -> dict[str, Any]:
    """The same numbers as ``event=`` fields, absent when unmeasured -- for greppable
    cross-pass comparison, beside the prose rather than instead of it.
    """
    if stimulus is None:
        return {}
    return {
        "stimulus_rms_dbfs": f"{stimulus.rms_dbfs:.2f}",
        "stimulus_peak_dbfs": f"{stimulus.peak_dbfs:.2f}",
        "stimulus_sha256": stimulus.sha256,
    }


def reachable_target_db_spl(
    *, spl_ceiling_db_spl: float, trip_db_spl: float, settled_db_spl: float,
    tolerance_db: float,
) -> float:
    """The highest target this run's OWN excursion still fits under the stop.

    Both inputs are MEASURED at the same commanded volume, so no chain model
    is assumed::

        excursion = trip - settled
        band top  = ceiling - excursion
        target    = band top - tolerance
    """
    excursion_db = max(0.0, float(trip_db_spl) - float(settled_db_spl))
    return float(spl_ceiling_db_spl) - excursion_db - float(tolerance_db)


def over_ceiling_detail(*, observed_db_spl: float, spl_ceiling_db_spl: float) -> str:
    """What a sample above the profile's commissioning SPL stop reports."""
    return (
        f"measured {observed_db_spl:.1f} dB SPL, above the profile's "
        f"commissioning stop {spl_ceiling_db_spl:.1f} dB SPL"
    )


class SeatLevelRampError(RuntimeError):
    """The leveling step cannot form a safe ramp from these inputs."""


# An interrupted pass still owes its caller one fact: did the household get
# its volume back? The cancellation is RE-RAISED, so this rides on the
# propagating exception instead of a return value.
RESTORED_ATTR = "seat_level_restored"


def interrupted_restore_outcome(exc: BaseException) -> bool | None:
    """What the pass's teardown achieved before ``exc`` propagated. ``None`` when the exception
    never passed through a pass that had opened the latch -- must not be read as "volume
    was restored".
    """
    value = getattr(exc, RESTORED_ATTR, None)
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class SeatLevelResult:
    """The outcome of one leveling pass. ``status`` is ``"converged"`` or
    ``"refused"``; a refusal always carries a ``reason`` from ``REFUSE_*`` and
    persists nothing. ``ramp`` is this pass's telemetry, read by
    ``jasper-seat-level --json``. ``restored`` is ``None`` before anything
    moved, a MEASURED outcome after (the volume seam can reject a write).
    ``reachable_target_db_spl`` is :func:`reachable_target_db_spl`'s answer
    on a pass the commissioning stop ended, ``None`` elsewhere.
    """

    status: str
    reason: str | None = None
    detail: str | None = None
    reference_volume_db: float | None = None
    measured_db_spl: float | None = None
    restored: bool | None = None
    reachable_target_db_spl: float | None = None
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
            "restored": self.restored,
            "reachable_target_db_spl": self.reachable_target_db_spl,
            "ramp": self.ramp,
        }


SampleSource = Callable[[], Awaitable[list[LevelSample]]]


@dataclass(frozen=True)
class _WindowTrace:
    """What one settle window actually heard, sample by sample.

    ``samples`` is ``(offset_s, db_spl)`` per finite sample, offset from when
    the READING began, so one settling reading's windows form a single
    timeline. ``seen`` exceeds ``len(samples)`` only when
    :data:`WINDOW_TRACE_MAX_SAMPLES` truncated the series. ``trip`` is the
    sample that STOPPED the window, recorded outside that cap; ``None`` for a
    window that ended on its own deadline or a clipped capture.
    """

    samples: tuple[tuple[float, float], ...]
    seen: int
    trip: tuple[float, float] | None = None

    def series(self) -> str:
        """The retained samples as one ``offset:dB SPL`` line."""
        return " ".join(f"{at:.3f}:{level:.1f}" for at, level in self.samples)

    def summary(self) -> dict[str, Any]:
        """This window's facts, for the refusal receipt and its event line. ``retained`` equals
        ``samples`` unless truncated; ``trip_db_spl`` can legitimately exceed
        ``max_db_spl`` under truncation since it is recorded outside the cap.
        """
        levels = [level for _at, level in self.samples]
        return {
            "samples": self.seen,
            "retained": len(self.samples),
            "min_db_spl": round(min(levels), 2) if levels else None,
            "median_db_spl": round(statistics.median(levels), 2) if levels else None,
            "max_db_spl": round(max(levels), 2) if levels else None,
            "trip_db_spl": None if self.trip is None else round(self.trip[1], 2),
            "trip_offset_s": None if self.trip is None else round(self.trip[0], 3),
        }


def _window_event_fields(summary: dict[str, Any]) -> dict[str, Any]:
    """A refusal's last window as ``stopped_window_*`` fields. ``stopped_window_``, not
    ``window_``: ``seat_level_start`` already emits ``window_dbfs`` for the TARGET BAND,
    a different concept.
    """
    return {f"stopped_window_{key}": value for key, value in summary.items()}


def _window_phrase(summary: dict[str, Any], *, windows: int | None = None) -> str:
    """One sentence of a refusal's last window, for the operator's own terminal. The ``trip``
    clause is conditional: a sample-domain stop names the sample that ended it,
    :data:`REFUSE_LEVEL_UNSETTLED` has none. ``windows=0`` renames the noun to "fade
    leg" (:func:`_watched_fade` opens no window). Empty for a window that saw no finite
    sample.
    """
    if not summary["samples"]:
        return ""
    what = "fade leg" if windows == 0 else "window"
    phrase = (
        f"the {what} it stopped in saw {summary['samples']} samples spanning "
        f"{summary['min_db_spl']:.1f}-{summary['max_db_spl']:.1f} dB SPL, "
        f"median {summary['median_db_spl']:.1f}"
    )
    if summary["trip_db_spl"] is not None:
        phrase += (
            f", and stopped on the {summary['trip_db_spl']:.1f} dB SPL sample "
            f"{summary['trip_offset_s']:.3f} s in"
        )
    return phrase


@dataclass(frozen=True)
class _Reading:
    """One settled level reading, or the guard that stopped it mid-window.

    ``rms_dbfs`` is the median of EVERY finite sample in the window that
    settled it (no sample dropped for being quiet); ``None`` means no
    believable level (mic silent, sample-domain stop, or never settled).
    ``windows`` is how many windows the reading took to settle. A stop on a
    FADE leg (:func:`_watched_fade`) also comes back as one of these, with
    ``samples``/``windows`` at zero and ``trace`` as the leg, not a window.
    """

    rms_dbfs: float | None
    samples: int
    trace: _WindowTrace
    windows: int = 1
    refusal: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Unconfirmed:
    """Two readings that each qualified to bank and did not agree. Carries its OWN volume and
    ordinals, since by the time the budget runs out the walk has stepped elsewhere.
    """

    volume_db: float
    earlier_db_spl: float
    later_db_spl: float
    earlier_reading: int
    later_reading: int

    @property
    def moved_db(self) -> float:
        return self.later_db_spl - self.earlier_db_spl


async def _window_reading(
    next_samples: SampleSource,
    *,
    sensitivity: MicSensitivity,
    spl_ceiling_db_spl: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    session_id: str,
    window: str,
    attempt: int,
    started: float,
    window_s: float,
) -> _Reading:
    """The median of ONE window, and the sample-domain stops that can end it.

    Both stops (clipped capture, over-ceiling sample) run on EVERY sample and
    abandon the window the moment one fires, checked before the median so the
    pass cannot sit at an over-ceiling level for the rest of a window.
    ``started`` is when the READING began; every trace offset is measured
    from it. Every window leaves a :class:`_WindowTrace` and emits one DEBUG
    line (``event=active_speaker.seat_level_window_samples``) when enabled.
    """
    readings: list[float] = []
    trace: list[tuple[float, float]] = []
    trip: tuple[float, float] | None = None
    seen = 0
    deadline = clock() + float(window_s)

    def _trace() -> _WindowTrace:
        return _WindowTrace(samples=tuple(trace), seen=seen, trip=trip)

    try:
        while clock() < deadline:
            for sample in await next_samples():
                if sample.clip:
                    return _Reading(
                        rms_dbfs=None,
                        samples=len(readings),
                        trace=_trace(),
                        refusal=REFUSE_MIC_CLIPPING,
                        detail=CLIPPED_CAPTURE_DETAIL,
                    )
                if not math.isfinite(sample.rms_dbfs):
                    continue
                observed_db_spl = sensitivity.db_spl_from_dbfs(sample.rms_dbfs)
                at = clock() - started
                seen += 1
                if len(trace) < WINDOW_TRACE_MAX_SAMPLES:
                    trace.append((at, observed_db_spl))
                if observed_db_spl > spl_ceiling_db_spl:
                    trip = (at, observed_db_spl)
                    return _Reading(
                        rms_dbfs=None,
                        samples=len(readings),
                        trace=_trace(),
                        refusal=REFUSE_SPL_CEILING_EXCEEDED,
                        detail=over_ceiling_detail(
                            observed_db_spl=observed_db_spl,
                            spl_ceiling_db_spl=spl_ceiling_db_spl,
                        ),
                    )
                readings.append(sample.rms_dbfs)
            await sleep(0.05)
        if not readings:
            return _Reading(
                rms_dbfs=None,
                samples=0,
                trace=_trace(),
                refusal=REFUSE_MIC_FEED_LOST,
            )
        return _Reading(
            rms_dbfs=statistics.median(readings),
            samples=len(readings),
            trace=_trace(),
        )
    finally:
        if logger.isEnabledFor(logging.DEBUG):
            recorded = _trace()
            log_event(
                logger,
                "active_speaker.seat_level_window_samples",
                level=logging.DEBUG,
                session=session_id,
                window=window,
                attempt=str(attempt),
                samples=str(recorded.seen),
                retained=str(len(recorded.samples)),
                db_spl=recorded.series(),
            )


async def _settle_reading(
    next_samples: SampleSource,
    *,
    sensitivity: MicSensitivity,
    spl_ceiling_db_spl: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    session_id: str,
    window: str,
    agree_db: float = SETTLED_AGREE_DB,
    timeout_s: float = SETTLE_TIMEOUT_S,
) -> _Reading:
    """Read windows until two consecutive ones agree, and return the later one.

    The LATER of the two agreeing windows is the reading. Three ways out other than
    agreement, none of which banks a number: a sample-domain stop or clipped capture
    (returned from the window that fired it); a window with no finite sample
    (:data:`REFUSE_MIC_FEED_LOST` after ONE window); or ``timeout_s`` elapsed still
    disagreeing (:data:`REFUSE_LEVEL_UNSETTLED`).

    Agreement bounds the RATE, never the remaining distance: ``residual ~= (agree_db /
    MIC_WINDOW_S) x tau``. Measured on the synthetic first-order rig at shipped
    defaults: tau=0.81s -> 0.28 dB under; tau=3s -> 2.11 dB under; tau=5s -> 4.16 dB
    under; tau=30s -> 19.46 dB under (minimum two windows). tau=3s is inside
    :data:`SETTLE_TIMEOUT_S`'s range, so ``windows == 2`` is not evidence of stillness.

    What the pass BANKS is bounded more tightly: two consecutive READINGS that agree
    (:data:`BANK_CONFIRM_READINGS`, the confirm in :func:`_walk_to_the_band`), a whole
    reading apart, catching the creep this residual describes.
    """
    started = clock()
    previous: float | None = None
    windows = 0
    while True:
        windows += 1
        reading = await _window_reading(
            next_samples,
            sensitivity=sensitivity,
            spl_ceiling_db_spl=spl_ceiling_db_spl,
            clock=clock,
            sleep=sleep,
            session_id=session_id,
            window=window,
            attempt=windows,
            started=started,
            window_s=MIC_WINDOW_S,
        )
        if reading.rms_dbfs is None:
            return replace(reading, windows=windows)
        if previous is not None:
            moved_db = reading.rms_dbfs - previous
            if abs(moved_db) <= float(agree_db):
                return replace(reading, windows=windows)
            if clock() - started >= float(timeout_s):
                was = sensitivity.db_spl_from_dbfs(previous)
                now = sensitivity.db_spl_from_dbfs(reading.rms_dbfs)
                return replace(
                    reading,
                    rms_dbfs=None,
                    windows=windows,
                    refusal=REFUSE_LEVEL_UNSETTLED,
                    detail=(
                        f"the level was still moving after {windows} windows of "
                        f"{MIC_WINDOW_S:g} s: the last two read "
                        f"{was:.1f} then {now:.1f} dB SPL ({moved_db:+.2f} dB "
                        f"apart, against a {float(agree_db):.1f} dB agreement "
                        f"bar), and the {float(timeout_s):.0f} s settle timeout "
                        "ran out; a level that has not settled is not banked"
                    ),
                )
        previous = reading.rms_dbfs


def bite_db(*, start_db: float, ceiling_db: float) -> float:
    """One bite: :data:`BITE_FRACTION` of the span this run has to sweep, computed once before
    the tone from the run's own start and ceiling.
    """
    return BITE_FRACTION * max(0.0, float(ceiling_db) - float(start_db))


def mic_is_not_observing(
    *, max_rise_db: float, min_rise_db: float, at_ceiling: bool
) -> bool:
    """At the ceiling with no reading ever clear of the room: nobody is listening.

    Without this, a mic hearing nothing (wrong card, in a bag, muted) reads
    as a quiet amplifier. The ceiling is the only non-arbitrary place to ask
    -- from a low start in a loud room, any fixed probe span fires on a
    chain that simply has not emerged yet. The measured SPL stop is
    deliberately NOT what bounds this: a non-observing mic's level does not
    move with volume, so it is structurally inert on this failure mode.
    """
    return max_rise_db < min_rise_db and at_ceiling


def fade_steps(*, from_db: float, to_db: float) -> int:
    """How many :data:`FADE_STEP_DB` writes a fade between two volumes makes. The single owner
    of a fade's shape (:func:`_fade_levels` walks it, :func:`_watchdog_seconds` prices
    it); direction-independent.
    """
    return math.ceil(abs(float(to_db) - float(from_db)) / FADE_STEP_DB)


def fade_seconds(*, from_db: float, to_db: float) -> float:
    """The wall-clock one fade leg spends sleeping between its writes."""
    return fade_steps(from_db=from_db, to_db=to_db) * FADE_STEP_S


def fade_quiet_db(from_db: float) -> float:
    """Where a fade-out walks TO: the floor, or ``from_db`` if already quieter. A fade only
    ever walks DOWN; the climb's downward steps are UNCAPPED, so a hot chain can already
    be below :data:`FADE_FLOOR_DB`, where fading "out" to the floor would RAISE the
    level. Below the floor, zero steps.
    """
    return min(float(from_db), FADE_FLOOR_DB)


def walk_reading_budget(*, start_db: float, ceiling_db: float) -> int:
    """How many settled readings one climb may spend, at most: one at the start volume, one per
    bite (``ceil(1/BITE_FRACTION)``, same for any chain), the allowed misses
    (:data:`MAX_MISSED_FULL_STEPS`), and the bank confirm
    (:data:`BANK_CONFIRM_READINGS`). Single owner of that count:
    :func:`_walk_to_the_band` spends it, :func:`_watchdog_seconds` prices it.
    """
    bite = bite_db(start_db=start_db, ceiling_db=ceiling_db)
    span_db = max(0.0, float(ceiling_db) - float(start_db))
    bites = math.ceil(span_db / bite) if bite > 0.0 else 0
    return 1 + bites + MAX_MISSED_FULL_STEPS + BANK_CONFIRM_READINGS


def _watchdog_seconds(
    *, start_db: float, ceiling_db: float, settle_timeout_s: float = SETTLE_TIMEOUT_S
) -> float:
    """This pass's own worst case, priced as the readings and fades it takes.
    :func:`walk_reading_budget` readings plus the ONE silent re-measure, each at
    ``settle_timeout_s``; plus THREE fade legs, each at a fade from the ceiling to
    :data:`FADE_FLOOR_DB`; plus :data:`WATCHDOG_SLACK_S`. Priced at the ceiling because
    agreement is tested BEFORE the timeout. Scope excludes the pre-tone ambient read
    (nothing is mutated there).
    """
    readings = (
        walk_reading_budget(start_db=start_db, ceiling_db=ceiling_db)
        + REMEASURE_READINGS
    )
    # Priced through `fade_quiet_db`, not FADE_FLOOR_DB directly, so a ceiling
    # already under the floor prices ZERO here as it walks zero steps there.
    fades = FADE_LEGS_PER_PASS * fade_seconds(
        from_db=ceiling_db, to_db=fade_quiet_db(ceiling_db)
    )
    return readings * float(settle_timeout_s) + fades + WATCHDOG_SLACK_S


def seat_level_ceiling_db(max_main_volume_db: float) -> float:
    """The operative volume ceiling: the headroom bound under the hard rail."""
    return min(float(max_main_volume_db), HARD_CEILING_DBFS)


def validate_seat_level_window(
    *, target: SeatLevelTarget, sensitivity: MicSensitivity
) -> tuple[float, float]:
    """The band as a mic-dBFS window, refusing one the mic cannot capture."""
    window_low = sensitivity.dbfs_from_db_spl(target.low_db_spl)
    window_high = sensitivity.dbfs_from_db_spl(target.high_db_spl)
    if window_high > HARD_CEILING_DBFS:
        raise SeatLevelRampError(
            f"{REFUSE_SPL_TARGET_UNCAPTURABLE}: {target.high_db_spl:g} dB SPL is "
            f"{window_high:.1f} dBFS at this mic — above digital full scale, so "
            "the band cannot be reached without clipping the capture"
        )
    return window_low, window_high


async def run_teardown(what: str, coro: Awaitable[Any]) -> bool:
    """Run one teardown step to completion under REPEATED cancellation.

    A bare ``finally: await ...`` survives one cancellation but not a
    second (Ctrl-C twice, a re-cancelling supervisor). Shielding and
    re-awaiting finishes it, bounded by :data:`TEARDOWN_SHIELD_ATTEMPTS`;
    never re-raises. Returns whether the step actually completed.
    """
    task = asyncio.ensure_future(coro)
    for _ in range(TEARDOWN_SHIELD_ATTEMPTS):
        try:
            await asyncio.shield(task)
            return True
        except asyncio.CancelledError:
            if task.done():
                return not task.cancelled() and task.exception() is None
        except RECOVERABLE_ERRORS:
            # Everything the volume seam and the tone player can raise.
            logger.exception("seat-level teardown step %s failed", what)
            return False
    task.cancel()
    log_event(
        logger,
        "active_speaker.seat_level_teardown_abandoned",
        level=logging.ERROR,
        step=what,
        attempts=str(TEARDOWN_SHIELD_ATTEMPTS),
    )
    return False


def _fade_levels(*, from_db: float, to_db: float) -> tuple[float, ...]:
    """Every commanded volume one fade leg writes, in order, ending exactly at ``to_db`` (each
    step clamped at the destination; no float comparison decides when to stop).
    """
    level = float(from_db)
    to = float(to_db)
    levels: list[float] = []
    for _ in range(fade_steps(from_db=level, to_db=to)):
        level = (
            min(to, level + FADE_STEP_DB)
            if to > level
            else max(to, level - FADE_STEP_DB)
        )
        levels.append(level)
    return tuple(levels)


async def _fade_and_stop(
    *,
    from_db: float,
    set_main_volume_db: SetMainVolumeDb,
    cancel_tone: Callable[[], None],
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """Walk the commanded volume down, then kill the tone. Best effort.

    ``cancel_tone`` sits in this function's own ``finally``, so the stimulus
    stops even when the fade cannot run. The pass's TEARDOWN edge, and the
    only fade that is not sample-watched -- by the time it runs the outcome
    is already decided. :func:`_watched_fade` is the mid-pass one.
    """
    try:
        for level in _fade_levels(from_db=from_db, to_db=fade_quiet_db(from_db)):
            await set_main_volume_db(level)
            await sleep(FADE_STEP_S)
    except RECOVERABLE_ERRORS:
        logger.exception("seat-level fade-before-tone-kill failed")
    finally:
        cancel_tone()


async def _watched_fade(
    *,
    from_db: float,
    to_db: float,
    direction: str,
    sensitivity: MicSensitivity,
    spl_ceiling_db_spl: float,
    set_main_volume_db: SetMainVolumeDb,
    next_samples: SampleSource,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    session_id: str,
) -> _Reading | None:
    """Walk the fader between two levels with the sample-domain stops running.

    Returns the refusal a sample forced, or ``None`` when the leg completed with the
    fader exactly at ``to_db``. Runs the same two stops a window does
    (:data:`CLIPPED_CAPTURE_DETAIL`, :func:`over_ceiling_detail`), but banks no median;
    the end-of-run :func:`_fade_and_stop` leg is deliberately NOT watched. A refusal
    carries the leg's samples as its trace, with ``windows=0`` so :func:`_window_phrase`
    calls it a "fade leg". A mid-leg :data:`RECOVERABLE_ERRORS` failure comes back as
    :data:`REFUSE_RAMP_ERROR`; ``CamillaUnavailable`` is NOT in that family and still
    propagates to the CLI.
    """
    levels = _fade_levels(from_db=from_db, to_db=to_db)
    log_event(
        logger,
        "active_speaker.seat_level_fade",
        session=session_id,
        direction=direction,
        from_db=f"{float(from_db):.2f}",
        to_db=f"{float(to_db):.2f}",
        steps=str(len(levels)),
        seconds=f"{fade_seconds(from_db=from_db, to_db=to_db):.2f}",
    )
    trace: list[tuple[float, float]] = []
    trip: tuple[float, float] | None = None
    seen = 0
    started = clock()

    def _stopped(refusal: str, detail: str) -> _Reading:
        return _Reading(
            rms_dbfs=None,
            samples=0,
            windows=0,  # explicit: no window opened, unlike the 1-window default
            trace=_WindowTrace(samples=tuple(trace), seen=seen, trip=trip),
            refusal=refusal,
            detail=detail,
        )

    try:
        for level in levels:
            await set_main_volume_db(level)
            await sleep(FADE_STEP_S)
            for sample in await next_samples():
                if sample.clip:
                    return _stopped(REFUSE_MIC_CLIPPING, CLIPPED_CAPTURE_DETAIL)
                if not math.isfinite(sample.rms_dbfs):
                    # A fade is not a reading: no median here to go missing.
                    continue
                observed_db_spl = sensitivity.db_spl_from_dbfs(sample.rms_dbfs)
                at = clock() - started
                seen += 1
                if len(trace) < WINDOW_TRACE_MAX_SAMPLES:
                    trace.append((at, observed_db_spl))
                if observed_db_spl > spl_ceiling_db_spl:
                    trip = (at, observed_db_spl)
                    return _stopped(
                        REFUSE_SPL_CEILING_EXCEEDED,
                        over_ceiling_detail(
                            observed_db_spl=observed_db_spl,
                            spl_ceiling_db_spl=spl_ceiling_db_spl,
                        ),
                    )
    except RECOVERABLE_ERRORS as exc:
        # Type-name fallback covers an exception with empty `str` (bare
        # `ValueError()`), so the refusal never blames the mic by default.
        logger.exception("seat-level watched fade failed (%s leg)", direction)
        return _stopped(REFUSE_RAMP_ERROR, str(exc) or type(exc).__name__)
    return None


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
    stimulus: StimulusProvenance | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session_id: str = "seat_level",
    volume_state_path: str | Path | None = None,
    reference_state_path: str | Path | None = None,
) -> SeatLevelResult:
    """Ramp to the seat-SPL band and bank the volume that reached it.

    ``target`` must already be validated against ``spl_ceiling_db_spl`` by the caller
    (:meth:`SeatLevelTarget.validate`); this function enforces the ceiling live, on
    measured samples. ``max_main_volume_db`` is the mic-independent ceiling from
    :func:`jasper.active_speaker.session_volume_plan.unsegmented_stimulus_ceiling_db`;
    the ramp never commands above it. ``stimulus`` bounds nothing -- it is carried so
    :data:`REFUSE_SPL_TARGET_UNREACHABLE` can show its own arithmetic and a banked
    reference names its stimulus. The hold rides the crossover session's own volume plan
    on the same durable statefile, and refuses under a live or unresolved session
    (:func:`live_measurement_session`). Runs inside the shared measurement window
    (:func:`jasper.correction.coordinator.measurement_window`, owned as ``seat-level``);
    every lease self-expires. Persists a reference only on a reading that rose clear of
    a floor THIS PASS MEASURED IN SILENCE.
    """
    window_low_dbfs, window_high_dbfs = validate_seat_level_window(
        target=target, sensitivity=sensitivity
    )
    ceiling_db = seat_level_ceiling_db(max_main_volume_db)
    min_rise_db = bounded_env_float(
        "JASPER_SEAT_LEVEL_MIN_RISE_DB", MIC_RESPONSE_MIN_RISE_DB, lo=1.0, hi=20.0
    )
    # The two halves of "settled", read once so one pass has one answer.
    agree_db = bounded_env_float(
        "JASPER_SEAT_LEVEL_SETTLED_AGREE_DB", SETTLED_AGREE_DB, lo=0.1, hi=3.0
    )
    settle_timeout_s = bounded_env_float(
        "JASPER_SEAT_LEVEL_SETTLE_TIMEOUT_S", SETTLE_TIMEOUT_S, lo=2.0, hi=30.0
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

    # Written by the teardown's `finally` below, read by the caller after it.
    restored: dict[str, bool | None] = {"ok": None}

    async def _leveled_under_isolation() -> SeatLevelResult:
        """The whole pass, run with the measurement window already held."""
        # Ambient first, deliberately BEFORE the latch: nothing is playing, so
        # a process killed here mutated nothing.
        ambient = await _settle_reading(
            next_samples,
            sensitivity=sensitivity,
            spl_ceiling_db_spl=spl_ceiling_db_spl,
            clock=clock,
            sleep=sleep,
            session_id=session_id,
            window="ambient",
            agree_db=agree_db,
            timeout_s=settle_timeout_s,
        )
        if ambient.rms_dbfs is None:
            reason = ambient.refusal or REFUSE_MIC_FEED_LOST
            summary = ambient.trace.summary()
            phrase = _window_phrase(summary)
            detail = ambient.detail or (
                "the microphone delivered no finite sample before the tone"
            )
            log_event(
                logger,
                "active_speaker.seat_level_refused",
                level=logging.WARNING,
                session=session_id,
                reason=reason,
                detail=detail,
                **_window_event_fields(summary),
            )
            return SeatLevelResult(
                status="refused",
                reason=reason,
                detail=detail if not phrase else f"{detail} ({phrase})",
            )
        ambient_dbfs = ambient.rms_dbfs
        ambient_db_spl = sensitivity.db_spl_from_dbfs(ambient_dbfs)
        start_db = SEAT_LEVEL_START_DB
        # Covers a ceiling with no room to climb, and a non-finite one (NaN
        # comparisons are False).
        if not start_db < ceiling_db:
            raise SeatLevelRampError(
                f"{REFUSE_VOLUME_CEILING_TOO_LOW}: the headroom ceiling "
                f"{ceiling_db:.1f} dB is at or below the {start_db:g} dB "
                "start; there is no room to climb"
            )
        watchdog_s = _watchdog_seconds(
            start_db=start_db,
            ceiling_db=ceiling_db,
            settle_timeout_s=settle_timeout_s,
        )

        plan = SessionVolumePlan(
            state_path=(
                DEFAULT_SESSION_VOLUME_STATE_PATH
                if volume_state_path is None
                else volume_state_path
            )
        )
        # A killed leveling pass should surface sooner than a measurement
        # session's 30-minute walked-away window.
        plan.set_wall_clock_ceiling_s(watchdog_s + 60.0)
        # THE DIRECT DOOR: writes the fader itself, not through a ranked claim
        # -- `jasper.cli.seat_level` installs no volume owner to arbitrate through.
        volume_door = FaderVolumeDoor(set_main_volume_db, get_main_volume_db)
        try:
            opened = await plan.open(start_db, volume_door)
        except SessionVolumePlanError:
            # An undrainable prior latch; nothing mutated here, recovery owns it.
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
                status="refused",
                reason=REFUSE_VOLUME_LATCH_UNCONFIRMED,
                detail=(
                    f"the volume latch did not confirm the {start_db:.2f} dB "
                    f"start ({opened.value}); the speaker was not moved"
                ),
            )

        log_event(
            logger,
            "active_speaker.seat_level_start",
            session=session_id,
            target_db_spl=f"{target.target_db_spl:.1f}",
            band_db_spl=f"[{target.low_db_spl:.1f},{target.high_db_spl:.1f}]",
            window_dbfs=f"[{window_low_dbfs:.1f},{window_high_dbfs:.1f}]",
            sens_factor_db=f"{sensitivity.sens_factor_db:.2f}",
            analog_gain_db=(
                "" if sensitivity.analog_gain_db is None
                else f"{sensitivity.analog_gain_db:.1f}"
            ),
            max_main_volume_db=f"{ceiling_db:.2f}",
            spl_ceiling_db_spl=f"{spl_ceiling_db_spl:.1f}",
            ambient_dbfs=f"{ambient_dbfs:.1f}",
            ambient_db_spl=f"{ambient_db_spl:.1f}",
            start_db=f"{start_db:.2f}",
            bite_db=f"{bite_db(start_db=start_db, ceiling_db=ceiling_db):.2f}",
            bite_fraction=f"{BITE_FRACTION:g}",
            required_rise_db=f"{min_rise_db:.1f}",
            settle_window_s=f"{MIC_WINDOW_S:g}",
            settle_agree_db=f"{agree_db:.2f}",
            settle_timeout_s=f"{settle_timeout_s:.0f}",
            watchdog_s=f"{watchdog_s:.0f}",
            precondition="mic capture volume must be at maximum (amixer -c <card>)",
        )

        try:
            async with asyncio.timeout(watchdog_s):
                return await _walk_to_the_band(
                    target=target,
                    sensitivity=sensitivity,
                    spl_ceiling_db_spl=spl_ceiling_db_spl,
                    ambient_dbfs=ambient_dbfs,
                    ambient_db_spl=ambient_db_spl,
                    start_db=start_db,
                    ceiling_db=ceiling_db,
                    min_rise_db=min_rise_db,
                    agree_db=agree_db,
                    settle_timeout_s=settle_timeout_s,
                    watchdog_s=watchdog_s,
                    stimulus=stimulus,
                    set_main_volume_db=set_main_volume_db,
                    play_continuous_tone=play_continuous_tone,
                    cancel_tone=cancel_tone,
                    next_samples=next_samples,
                    clock=clock,
                    sleep=sleep,
                    session_id=session_id,
                    reference_state_path=reference_state_path,
                )
        except TimeoutError:
            detail = (
                f"the leveling pass exceeded its {watchdog_s:.0f} s whole-operation "
                "watchdog; something it awaited never returned"
            )
            log_event(
                logger,
                "active_speaker.seat_level_refused",
                level=logging.ERROR,
                session=session_id,
                reason=REFUSE_WATCHDOG_EXPIRED,
                watchdog_s=f"{watchdog_s:.0f}",
            )
            return SeatLevelResult(
                status="refused", reason=REFUSE_WATCHDOG_EXPIRED, detail=detail
            )
        except asyncio.CancelledError:
            # Recorded, then re-raised; the `finally` below has already put
            # the volume back.
            log_event(
                logger,
                "active_speaker.seat_level_refused",
                level=logging.WARNING,
                session=session_id,
                reason=REFUSE_INTERRUPTED,
                detail="the leveling pass was cancelled; nothing was banked",
            )
            raise
        finally:
            # Restore-exactly-once on every exit path; success is published,
            # not assumed.
            drained: dict[str, Any] = {}

            async def _drain() -> None:
                drained["result"] = await plan.close(
                    volume_door, reason="seat_level_complete",
                )

            restored["ok"] = await run_teardown("volume_restore", _drain()) and (
                drained.get("result")
                in (
                    SessionVolumeRestoreResult.EXACT_RESTORED,
                    SessionVolumeRestoreResult.ALREADY_RESOLVED,
                )
            )
            if not restored["ok"]:
                log_event(
                    logger,
                    "active_speaker.seat_level_restore_failed",
                    level=logging.ERROR,
                    session=session_id,
                    detail=(
                        "the household volume was NOT restored; the speaker is "
                        "parked at a measurement level"
                    ),
                )
            # An interrupt leaves by exception; stamp it instead of a result.
            in_flight = sys.exc_info()[1]
            if in_flight is not None:
                setattr(in_flight, RESTORED_ATTR, restored["ok"])

    def _stamped(result: SeatLevelResult) -> SeatLevelResult:
        """Publish the restore outcome the teardown recorded."""
        return replace(result, restored=restored["ok"])

    leveled: SeatLevelResult | None = None
    try:
        async with measurement_window(gate_owner=SEAT_LEVEL_GATE_OWNER):
            leveled = await _leveled_under_isolation()
        return _stamped(leveled)
    except MeasurementWindowError as exc:
        if leveled is not None:
            # The pass finished and already restored the household volume;
            # only teardown failed. Every lease self-expires within ~2 minutes.
            log_event(
                logger,
                "active_speaker.seat_level_isolation_cleanup_failed",
                level=logging.ERROR,
                session=session_id,
                error=str(exc),
            )
            return _stamped(leveled)
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


async def _remeasure_silence(
    *,
    tone: "asyncio.Future[Any]",
    volume_db: float,
    sensitivity: MicSensitivity,
    spl_ceiling_db_spl: float,
    set_main_volume_db: SetMainVolumeDb,
    play_continuous_tone: Callable[[], Awaitable[Any]],
    cancel_tone: Callable[[], None],
    next_samples: SampleSource,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    session_id: str,
    agree_db: float,
    settle_timeout_s: float,
) -> tuple[_Reading, "asyncio.Future[Any]"]:
    """Fade out, stop the tone, measure the room again, start it and fade in.

    Returns the new floor and the tone future the caller must now hold. The floor this
    returns feeds the guards deciding whether the mic is observing and whether a reading
    may bank -- both answerable only against a level measured with the speaker SILENT
    (the anti-coincidence property).

    Residual: this window is anti-coincident with the SPEAKER but not independent of the
    trigger (taken because a reading landed low, and room lulls autocorrelate over
    seconds); the failure probability is ADDED, not traded, but a contaminated ambient
    window cannot disqualify GOOD readings for the rest of a run. Receipt's
    ``ambient_remeasured`` and ``remeasured_delta_db`` are the operator's tell.

    Bounded and unconditional-once: exactly one extra :func:`_settle_reading` plus the
    two fade legs, all priced into :func:`_watchdog_seconds`, no retry. Both edges are
    faded, turning at :func:`fade_quiet_db` not :data:`FADE_FLOOR_DB` (a hot chain can
    already be below the floor, where fading "out" to it would raise the level). A
    sample-domain stop can fire on either leg (:func:`_watched_fade`) and comes back as
    this function's reading.

    EVERY exit leaves the stimulus off except the one returning a usable room. The legs
    are wrapped so a mid-leg cancellation or seam failure cuts the tone on its way out
    too.
    """
    quiet_db = fade_quiet_db(volume_db)
    try:
        faded = await _watched_fade(
            from_db=volume_db,
            to_db=quiet_db,
            direction="down",
            sensitivity=sensitivity,
            spl_ceiling_db_spl=spl_ceiling_db_spl,
            set_main_volume_db=set_main_volume_db,
            next_samples=next_samples,
            clock=clock,
            sleep=sleep,
            session_id=session_id,
        )
    finally:
        # Covers abnormal exits (mid-leg cancellation, an unturned error) that
        # would otherwise leave the room playing.
        cancel_tone()
        tone.cancel()
    if faded is not None:
        return faded, tone
    reading = await _settle_reading(
        next_samples,
        sensitivity=sensitivity,
        spl_ceiling_db_spl=spl_ceiling_db_spl,
        clock=clock,
        sleep=sleep,
        session_id=session_id,
        window="silence",
        agree_db=agree_db,
        timeout_s=settle_timeout_s,
    )
    if reading.rms_dbfs is None:
        return reading, tone
    # Fader is already at the floor, so the up leg is the only write needed.
    restarted: asyncio.Future[Any] = asyncio.ensure_future(play_continuous_tone())
    # A flag, not a plain `finally`: this leg's SUCCESS path hands the caller
    # a playing stimulus. Covers cancellation/error leaving it ON mid-fade.
    # A bare `except BaseException` re-raise is equivalent but trips
    # `tests/test_lint_contracts.py`'s suppression-comment guards.
    returning = False
    try:
        faded_in = await _watched_fade(
            from_db=quiet_db,
            to_db=volume_db,
            direction="up",
            sensitivity=sensitivity,
            spl_ceiling_db_spl=spl_ceiling_db_spl,
            set_main_volume_db=set_main_volume_db,
            next_samples=next_samples,
            clock=clock,
            sleep=sleep,
            session_id=session_id,
        )
        returning = True
    finally:
        if not returning:
            cancel_tone()
            restarted.cancel()
    if faded_in is not None:
        # A stop on the way back UP would otherwise hand the caller a PLAYING
        # stimulus; cuts the room the instant the stop fires instead.
        cancel_tone()
        return faded_in, restarted
    return reading, restarted


async def _walk_to_the_band(
    *,
    target: SeatLevelTarget,
    sensitivity: MicSensitivity,
    spl_ceiling_db_spl: float,
    ambient_dbfs: float,
    ambient_db_spl: float,
    start_db: float,
    ceiling_db: float,
    min_rise_db: float,
    agree_db: float,
    settle_timeout_s: float,
    watchdog_s: float,
    stimulus: StimulusProvenance | None,
    set_main_volume_db: SetMainVolumeDb,
    play_continuous_tone: Callable[[], Awaitable[Any]],
    cancel_tone: Callable[[], None],
    next_samples: SampleSource,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    session_id: str,
    reference_state_path: str | Path | None,
) -> SeatLevelResult:
    """Play the stimulus and step toward the band, one measured gap at a time."""
    # The climb's INTENT: the volume the walk believes it is measuring at. A
    # fade leg must never move it.
    volume_db = start_db
    # Where the FADER actually is, as a LOWER BOUND -- distinct from
    # `volume_db` because a fade leg walks the fader down and back up without
    # the climb's intent changing. Maintained by `write_fader` below. See
    # docs/adr/0005-fader-bound-asymmetric-record-point.md.
    fader_db = start_db

    async def write_fader(level: float) -> None:
        """Move the fader and keep ``fader_db`` a LOWER BOUND on where it is. A write either lands
        or raises, so after ``old -> L`` the true position is ``old`` or ``L``. DOWNWARD
        (``L < old``): record ``L`` BEFORE the write. UPWARD (``L > old``): record
        ``old`` until the write lands. See
        docs/adr/0005-fader-bound-asymmetric-record-point.md.
        """
        nonlocal fader_db
        target = float(level)
        if target < fader_db:
            fader_db = target
            await set_main_volume_db(target)
            return
        await set_main_volume_db(target)
        fader_db = target

    steps: list[dict[str, Any]] = []
    max_rise_db = 0.0
    missed_full_steps = 0
    last_step_was_full = False
    previous: tuple[float, float] | None = None
    slope_db_per_db: float | None = None
    # BANK CONFIRM: level of the previous qualifying reading, `None` when the
    # last did not qualify, so "consecutive" means what it says.
    candidate_dbfs: float | None = None
    # Most recent pair of qualifying readings that DISAGREED, for the refusal
    # naming a level that reached the band and would not hold still there.
    disagreed: _Unconfirmed | None = None
    # Floor every rise is measured against: ALWAYS a window measured with the
    # speaker silent (the pre-tone ambient, or a mid-climb re-measure).
    floor_dbfs = ambient_dbfs
    remeasured_dbfs: float | None = None
    # Non-zero by construction: REFUSE_VOLUME_CEILING_TOO_LOW already refused
    # a ceiling at or below the start.
    bite = bite_db(start_db=start_db, ceiling_db=ceiling_db)
    max_readings = walk_reading_budget(start_db=start_db, ceiling_db=ceiling_db)

    def telemetry(
        final_volume_db: float, *, window: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "start_db": round(start_db, 2),
            "ceiling_db": round(ceiling_db, 2),
            "bite_db": round(bite, 2),
            "bite_fraction": BITE_FRACTION,
            "ambient_dbfs": round(ambient_dbfs, 2),
            "ambient_db_spl": round(ambient_db_spl, 2),
            "ambient_remeasured": remeasured_dbfs is not None,
            "ambient_remeasured_db_spl": (
                None
                if remeasured_dbfs is None
                else round(sensitivity.db_spl_from_dbfs(remeasured_dbfs), 2)
            ),
            "required_rise_db": round(min_rise_db, 2),
            # What "settled" meant for THIS run; two of three are overridable.
            "settle_window_s": MIC_WINDOW_S,
            "settle_agree_db": round(agree_db, 2),
            "settle_timeout_s": round(settle_timeout_s, 1),
            "watchdog_s": round(watchdog_s, 1),
            "final_volume_db": round(final_volume_db, 2),
            "slope_db_per_db": (
                None if slope_db_per_db is None else round(slope_db_per_db, 3)
            ),
            "steps": steps,
        }
        if window is not None:
            payload["stopped_window"] = window
        return payload

    def refuse(
        reason: str,
        detail: str,
        *,
        trace: _WindowTrace | None = None,
        windows: int | None = None,
        **evidence: Any,
    ) -> SeatLevelResult:
        """Log the refusal and hand the operator the same facts on stdout.

        Every detail closes with where the ramp stopped (``fader_db``, not
        ``volume_db`` -- they diverge exactly on a fade-leg refusal), the
        headroom ceiling, and the PRIOR window's settled median. ``trace`` is
        the window a sample-domain stop abandoned, riding the receipt as
        ``ramp.stopped_window`` and the event line as ``stopped_window_*``.
        """
        last = steps[-1] if steps else None
        stopped = (
            f"stopped at {fader_db:.2f} dB against the {ceiling_db:.2f} dB "
            "headroom ceiling"
            + (
                f", reading {last['observed_db_spl']:.1f} dB SPL at "
                f"{last['volume_db']:.2f} dB"
                if last
                else ""
            )
        )
        summary = None if trace is None else trace.summary()
        phrase = (
            "" if summary is None else _window_phrase(summary, windows=windows)
        )
        window_fields: dict[str, Any] = {}
        reachable = None
        if summary is not None:
            window_fields.update(_window_event_fields(summary))
            if last is not None:
                window_fields["prior_db_spl"] = f"{last['observed_db_spl']:.1f}"
                window_fields["prior_volume_db"] = f"{last['volume_db']:.2f}"
                # A trip is recorded only by the SPL stop; only when the prior
                # reading was at the SAME commanded volume is the difference
                # an EXCURSION rather than a volume step.
                if summary["trip_db_spl"] is not None and (
                    abs(fader_db - last["volume_db"]) <= STEP_EPSILON_DB
                ):
                    reachable = reachable_target_db_spl(
                        spl_ceiling_db_spl=spl_ceiling_db_spl,
                        trip_db_spl=summary["trip_db_spl"],
                        settled_db_spl=last["observed_db_spl"],
                        tolerance_db=target.tolerance_db,
                    )
                    window_fields["reachable_target_db_spl"] = f"{reachable:.1f}"
        log_event(
            logger,
            "active_speaker.seat_level_refused",
            level=logging.WARNING,
            session=session_id,
            reason=reason,
            at_db=f"{fader_db:.2f}",
            ceiling_db=f"{ceiling_db:.2f}",
            readings=str(len(steps)),
            **evidence,
            **window_fields,
        )
        return SeatLevelResult(
            status="refused",
            reason=reason,
            detail=f"{detail} ({stopped}{'' if not phrase else f'; {phrase}'})",
            reachable_target_db_spl=reachable,
            ramp=telemetry(fader_db, window=summary),
        )

    tone: asyncio.Future[Any] = asyncio.ensure_future(play_continuous_tone())
    try:
        for _ in range(max_readings):
            reading = await _settle_reading(
                next_samples,
                sensitivity=sensitivity,
                spl_ceiling_db_spl=spl_ceiling_db_spl,
                clock=clock,
                sleep=sleep,
                session_id=session_id,
                window=f"{volume_db:.2f}",
                agree_db=agree_db,
                timeout_s=settle_timeout_s,
            )
            if reading.rms_dbfs is None:
                return refuse(
                    reading.refusal or REFUSE_MIC_FEED_LOST,
                    reading.detail
                    or "the microphone stopped delivering finite samples",
                    trace=reading.trace,
                    windows=reading.windows,
                )
            observed_db_spl = sensitivity.db_spl_from_dbfs(reading.rms_dbfs)
            # A reading CONTRADICTS the floor when it lands below it (the tone
            # is playing, so it cannot be quieter than the room): measure the
            # silence again rather than believe the reading.
            if remeasured_dbfs is None and reading.rms_dbfs < floor_dbfs:
                silent, tone = await _remeasure_silence(
                    tone=tone,
                    volume_db=volume_db,
                    sensitivity=sensitivity,
                    spl_ceiling_db_spl=spl_ceiling_db_spl,
                    # Legs write the fader through the walk's own setter, so
                    # `fader_db` tracks a fade exactly as it tracks a climb step.
                    set_main_volume_db=write_fader,
                    play_continuous_tone=play_continuous_tone,
                    cancel_tone=cancel_tone,
                    next_samples=next_samples,
                    clock=clock,
                    sleep=sleep,
                    session_id=session_id,
                    agree_db=agree_db,
                    settle_timeout_s=settle_timeout_s,
                )
                if silent.rms_dbfs is None:
                    return refuse(
                        silent.refusal or REFUSE_MIC_FEED_LOST,
                        silent.detail
                        or (
                            "the microphone delivered no finite sample while the "
                            "room was re-measured in silence"
                        ),
                        trace=silent.trace,
                        # `0` on a fade leg is what makes the prose call it one.
                        windows=silent.windows,
                    )
                remeasured_dbfs = silent.rms_dbfs
                floor_dbfs = remeasured_dbfs
                remeasured_db_spl = sensitivity.db_spl_from_dbfs(remeasured_dbfs)
                log_event(
                    logger,
                    "active_speaker.seat_level_ambient_remeasured",
                    session=session_id,
                    at_db=f"{volume_db:.2f}",
                    contradicted_by_db_spl=f"{observed_db_spl:.2f}",
                    measured_ambient_db_spl=f"{ambient_db_spl:.2f}",
                    remeasured_ambient_db_spl=f"{remeasured_db_spl:.2f}",
                    # A large NEGATIVE delta is the residual's signature (a
                    # room lull persisting, indistinguishable from a mic that
                    # never responds); a POSITIVE delta means the floor went
                    # UP, so later rises publish NEGATIVE -- conservative.
                    remeasured_delta_db=f"{remeasured_db_spl - ambient_db_spl:+.2f}",
                    rise_after_remeasure_db=(
                        f"{observed_db_spl - remeasured_db_spl:+.2f}"
                    ),
                )
            rise_db = reading.rms_dbfs - floor_dbfs
            max_rise_db = max(max_rise_db, rise_db)
            gap_db = target.target_db_spl - observed_db_spl
            if previous is not None:
                delta_volume = volume_db - previous[0]
                if abs(delta_volume) > STEP_EPSILON_DB:
                    slope_db_per_db = (observed_db_spl - previous[1]) / delta_volume
            previous = (volume_db, observed_db_spl)
            steps.append({
                "volume_db": round(volume_db, 2),
                "observed_dbfs": round(reading.rms_dbfs, 2),
                "observed_db_spl": round(observed_db_spl, 2),
                "rise_db": round(rise_db, 2),
                "gap_db": round(gap_db, 2),
                "samples": reading.samples,
                "windows": reading.windows,  # answer time, not a confidence score
            })
            log_event(
                logger,
                "active_speaker.seat_level_reading",
                session=session_id,
                at_db=f"{volume_db:.2f}",
                observed_db_spl=f"{observed_db_spl:.1f}",
                rise_db=f"{rise_db:.1f}",
                gap_db=f"{gap_db:.1f}",
                samples=str(reading.samples),
                windows=str(reading.windows),
            )

            # Convergence needs BOTH: inside the band, and clear of a floor
            # THIS PASS MEASURED IN SILENCE -- a non-observing mic can still
            # land in the band by chance.
            in_band = target.low_db_spl <= observed_db_spl <= target.high_db_spl
            if in_band and rise_db >= min_rise_db:
                # THE BANK CONFIRM: a reference is banked only when two
                # consecutive READINGS agree -- window agreement (`_settle_reading`)
                # applied one level up, since the banked number outlives the
                # pass with no equivalent stop.
                if (
                    candidate_dbfs is not None
                    and abs(reading.rms_dbfs - candidate_dbfs) <= agree_db
                ):
                    return _bank(
                        reference_volume_db=volume_db,
                        measured_db_spl=observed_db_spl,
                        target=target,
                        sensitivity=sensitivity,
                        ceiling_db=ceiling_db,
                        stimulus=stimulus,
                        session_id=session_id,
                        reference_state_path=reference_state_path,
                        telemetry=telemetry(volume_db),
                    )
                if candidate_dbfs is not None:
                    # Both readings taken HERE (no step between); this volume
                    # and these ordinals are the pair's own identity.
                    disagreed = _Unconfirmed(
                        volume_db=volume_db,
                        earlier_db_spl=sensitivity.db_spl_from_dbfs(candidate_dbfs),
                        later_db_spl=observed_db_spl,
                        earlier_reading=len(steps) - 1,
                        later_reading=len(steps),
                    )
                    log_event(
                        logger,
                        "active_speaker.seat_level_bank_unconfirmed",
                        level=logging.WARNING,
                        session=session_id,
                        at_db=f"{disagreed.volume_db:.2f}",
                        readings=(
                            f"{disagreed.earlier_reading}-"
                            f"{disagreed.later_reading}"
                        ),
                        was_db_spl=f"{disagreed.earlier_db_spl:.2f}",
                        now_db_spl=f"{disagreed.later_db_spl:.2f}",
                        moved_db=f"{disagreed.moved_db:+.2f}",
                        agree_db=f"{agree_db:.2f}",
                    )
                candidate_dbfs = reading.rms_dbfs
                # This step reached the band, so it must not also be charged a
                # miss if the confirm later disagrees.
                last_step_was_full = False
                continue  # re-read at the SAME commanded volume: a confirm, not a step
            candidate_dbfs = None

            if last_step_was_full:
                missed_full_steps += 1
                if missed_full_steps >= MAX_MISSED_FULL_STEPS:
                    slope = (
                        "unmeasured" if slope_db_per_db is None
                        else f"{slope_db_per_db:.2f} dB per commanded dB"
                    )
                    return refuse(
                        REFUSE_LEVEL_UNCONVERGED,
                        f"two steps commanded the full measured gap and neither "
                        f"landed in the band; the chain measured {slope} across "
                        f"the last two readings (last reading "
                        f"{observed_db_spl:.1f} dB SPL at {volume_db:.2f} dB, "
                        f"band [{target.low_db_spl:.1f},{target.high_db_spl:.1f}])",
                        slope_db_per_db=(
                            "" if slope_db_per_db is None
                            else f"{slope_db_per_db:.3f}"
                        ),
                        observed_db_spl=f"{observed_db_spl:.1f}",
                    )

            at_ceiling = volume_db >= ceiling_db - STEP_EPSILON_DB
            if mic_is_not_observing(
                max_rise_db=max_rise_db,
                min_rise_db=min_rise_db,
                at_ceiling=at_ceiling,
            ):
                # Names the floor this pass last measured IN SILENCE, matching
                # the room the rise was actually computed against.
                floor_db_spl = sensitivity.db_spl_from_dbfs(floor_dbfs)
                return refuse(
                    REFUSE_MIC_NOT_OBSERVING,
                    f"the volume climbed {volume_db - start_db:.1f} dB to the "
                    f"ceiling and the mic never rose more than "
                    f"{max_rise_db:.1f} dB above the {floor_db_spl:.1f} dB SPL "
                    "room; check that the mic is capturing the right card and "
                    "is not muted",
                    commanded_climb_db=f"{volume_db - start_db:.2f}",
                    ambient_dbfs=f"{ambient_dbfs:.1f}",
                    floor_dbfs=f"{floor_dbfs:.1f}",
                    observed_rise_db=f"{max_rise_db:.2f}",
                    required_rise_db=f"{min_rise_db:.2f}",
                )

            # Remaining gap IS the step, saturated upward by this run's own
            # bite; downward moves are uncapped.
            step_db = capped_gap_step_db(
                measured_db=observed_db_spl,
                target_db=target.target_db_spl,
                cap_db=bite,
            )
            next_db = min(volume_db + step_db, ceiling_db)
            if abs(next_db - volume_db) <= STEP_EPSILON_DB:
                # At the ceiling the amplifier is actionable; off the ceiling
                # the ramp cannot move for another reason, and "raise the
                # amplifier" would send the operator the wrong way.
                remedy = (
                    "raise the external amplifier and retry"
                    if at_ceiling
                    else "the reading did not move with the volume; check the "
                    "microphone and the signal path"
                )
                return refuse(
                    REFUSE_SPL_TARGET_UNREACHABLE,
                    f"{observed_db_spl:.1f} dB SPL is short of the "
                    f"[{target.low_db_spl:.1f},{target.high_db_spl:.1f}] dB SPL "
                    f"band and the ramp cannot climb further; {remedy}"
                    + stimulus_level_phrase(stimulus),
                    observed_db_spl=f"{observed_db_spl:.1f}",
                    **_stimulus_event_fields(stimulus),
                )
            # Only a step commanding the WHOLE measured gap is a prediction the
            # chain can miss; a capped/clamped step never spends the miss budget.
            last_step_was_full = abs(next_db - (volume_db + gap_db)) <= STEP_EPSILON_DB
            volume_db = next_db
            await write_fader(volume_db)

        if disagreed is not None:
            # Reached the band, repeatedly, and would not hold still there --
            # different from "never arrived". Named by its own volume and
            # ordinals: this pair may be several readings back.
            return refuse(
                REFUSE_LEVEL_UNSETTLED,
                f"the level reached the band and would not hold still in it: "
                f"readings {disagreed.earlier_reading} and "
                f"{disagreed.later_reading} at {disagreed.volume_db:.2f} dB "
                f"read {disagreed.earlier_db_spl:.1f} then "
                f"{disagreed.later_db_spl:.1f} dB SPL "
                f"({disagreed.moved_db:+.2f} dB apart, against a "
                f"{agree_db:.1f} dB agreement bar) and the "
                f"{max_readings}-reading budget ran out; a reference is banked "
                "only from two readings that agree",
                pair_at_db=f"{disagreed.volume_db:.2f}",
                pair_readings=(
                    f"{disagreed.earlier_reading}-{disagreed.later_reading}"
                ),
                was_db_spl=f"{disagreed.earlier_db_spl:.1f}",
                now_db_spl=f"{disagreed.later_db_spl:.1f}",
                agree_db=f"{agree_db:.2f}",
            )
        slope = (
            "unmeasured" if slope_db_per_db is None
            else f"{slope_db_per_db:.2f} dB per commanded dB"
        )
        return refuse(
            REFUSE_LEVEL_UNCONVERGED,
            (
                # It DID land, on the very last reading, with none left to confirm.
                f"the ramp reached the band on the last of its "
                f"{max_readings} readings, with none left to confirm it; a "
                "reference is banked only from two readings that agree"
                if candidate_dbfs is not None
                else f"the ramp took its whole {max_readings}-reading budget "
                f"without landing in the band; the chain measured {slope} "
                "across the last two readings"
            ),
            slope_db_per_db=(
                "" if slope_db_per_db is None else f"{slope_db_per_db:.3f}"
            ),
        )
    finally:
        # Teardown fades from where the room IS, not the climb's intent:
        # `fader_db` is a LOWER BOUND on the true position (`min` is
        # redundant by construction, kept for readability).
        await run_teardown(
            "fade_and_stop",
            _fade_and_stop(
                from_db=min(volume_db, fader_db),
                set_main_volume_db=set_main_volume_db,
                cancel_tone=cancel_tone,
                sleep=sleep,
            ),
        )
        tone.cancel()


def _bank(
    *,
    reference_volume_db: float,
    measured_db_spl: float,
    target: SeatLevelTarget,
    sensitivity: MicSensitivity,
    ceiling_db: float,
    stimulus: StimulusProvenance | None,
    session_id: str,
    reference_state_path: str | Path | None,
    telemetry: dict[str, Any],
) -> SeatLevelResult:
    """Publish the converged reference. Called only from inside the band."""
    try:
        write_seat_level_reference(
            reference_volume_db=reference_volume_db,
            measured_db_spl=measured_db_spl,
            target=target,
            sensitivity=sensitivity.to_dict(),
            max_main_volume_db=float(ceiling_db),
            stimulus=stimulus,
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
            status="refused",
            reason=REFUSE_RAMP_ERROR,
            detail=str(exc),
            ramp=telemetry,
        )
    log_event(
        logger,
        "active_speaker.seat_level_converged",
        session=session_id,
        reference_volume_db=f"{reference_volume_db:.2f}",
        measured_db_spl=f"{measured_db_spl:.1f}",
        band_db_spl=f"[{target.low_db_spl:.1f},{target.high_db_spl:.1f}]",
        readings=str(len(telemetry.get("steps", []))),
        **_stimulus_event_fields(stimulus),
    )
    return SeatLevelResult(
        status="converged",
        reference_volume_db=reference_volume_db,
        measured_db_spl=measured_db_spl,
        ramp=telemetry,
    )
