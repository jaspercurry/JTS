# SPDX-FileCopyrightText: 2026 Jasper Curry

#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop seat-SPL leveling: find the volume that measures the target.

Play the crossover session's own stimulus while a CALIBRATED measurement mic at
the listening seat watches, step the main volume until the seat SPL lands inside
the operator's band, and bank the volume that got there
(:mod:`seat_level_reference`) for
:func:`jasper.active_speaker.session_volume_plan.session_measurement_volume_db`.

Each step commands the remaining measured gap, saturated upward at one bite of
:data:`BITE_FRACTION` of ``ceiling - start``; downward steps are uncapped
because they reduce risk. No sample is discarded for being quiet, so "still
under the room" is a reason to bite again rather than a stall. The floor every
rise is judged against is ONLY ever measured with the speaker SILENT. A
reference is banked only from two consecutive settled readings that agree; every
refusal restores the household volume through the latch and persists nothing.
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
# owner (registered in ``mux.FANIN_TEST_OWNERS``, so ``TEST_RELEASE`` stays
# owner-scoped) AND jasper-control's volume-hold owner. It is what
# ``/state.measurement.owner`` and every ``event=measurement.hold_*`` line say
# while a leveling pass is running.
SEAT_LEVEL_GATE_OWNER = "seat-level"

# One WINDOW: the span whose median is one observation of the level. Half a
# second is about 12 samples from the wired meter's 2048-frame ALSA period
# (~42.7 ms at 48 kHz) — enough that the median is a level rather than a
# sample. NOT a lag model: nothing here claims to know how long this chain
# needs after a volume step, which is what :data:`SETTLED_AGREE_DB` measures.
MIC_WINDOW_S = 0.5

# When two consecutive windows are the SAME LEVEL, and so the whole definition
# of settled: the pass keeps reading until it sees that agreement, and banks
# the later of the two.
#
# Sized against a room's own wander at a settled step: a converged window
# varied 0.42 dB across its thirds while a window still climbing moved 6.03 dB,
# so 0.5 dB separates the two by an order of magnitude.
#
# It bounds a RATE, not a remaining distance — :func:`_settle_reading` states
# the residual that leaves and measures it against the chain's time constant.
#
# A deploy-time knob for the same reason :data:`MIC_RESPONSE_MIN_RISE_DB` is
# one: how still a room actually sits is a property only hardware knows.
# Widening it lowers the bar for "settled", so it is disclosed on the receipt
# (``ramp.settle_agree_db``) and on the start event.
SETTLED_AGREE_DB = 0.5

# How long ONE reading may spend proving it settled before the pass refuses it
# (:data:`REFUSE_LEVEL_UNSETTLED`). The default covers a chain settling with a
# time constant of about three seconds. A dead feed does NOT wait this out: a
# window with no finite sample in it is :data:`REFUSE_MIC_FEED_LOST` after one
# window.
#
# This number sets the pass's audible worst case, and a HEALTHY pass can pay
# it: agreement is tested before the timeout, so a chain that agrees just under
# the bar spends the whole timeout on a reading that CONVERGES. Audible time is
# bounded by ``walk_reading_budget`` readings at this timeout each, plus the
# pass's fade legs (:data:`FADE_LEGS_PER_PASS` at :func:`fade_seconds` apiece).
# Every sample is still under the commissioning stop.
#
# Env-overridable so an operator whose chain settles slower than this has a way
# forward that is not a redeploy; bounded so the knob cannot turn a leveling
# pass into an unbounded tone.
SETTLE_TIMEOUT_S = 8.0

# The quiet floor the climb starts from. Deliberately low and deliberately
# uninformed — we do not know what a stranger's amplifier does, so we start
# under anything that could hurt and let the bites find the level.
SEAT_LEVEL_START_DB = -50.0

# One bite, as a fraction of the run's OWN span (ceiling - start). The single
# dimensionless knob in the climb, and dimensionless on purpose: an unknown
# amplifier moves WHERE inside the span the speaker becomes audible, not how
# wide the span is, so 0.15 sweeps any chain in at most ceil(1 / 0.15) = 7
# bites whatever the gain.
#
# NOT the same question as calibration_level's AUDIBLE_RAMP_STEP_DB, which
# clamps ONE call to the calibration `set` endpoint at 10 dB. That is a
# per-request bound on an operator-driven jump; this is a climb bite sized to
# the span the ramp has to sweep.
BITE_FRACTION = 0.15

# The rise a reading must clear to count as the SPEAKER rather than the room —
# read by the runaway guard and by convergence, which is why it is one number.
#
# Measured against the AMBIENT floor and never against the first reading: a
# speaker that starts quieter than the room pins the mic at ambient for the
# first bites, so a 1:1 "track the commanded volume" test fires on a perfectly
# good mic in a normal room. Against ambient, such a chain simply has to emerge,
# while a mic pinned at a CONSTANT never does. That the floor is a level
# measured in SILENCE is what holds back a wrong-card mic hearing a room that
# wanders (:func:`_remeasure_silence` states how far that goes).
#
# A deploy-time knob (ramp.py's convention for every hardware-gated threshold)
# because the right rise depends on the room's floor, which only hardware knows.
MIC_RESPONSE_MIN_RISE_DB = 6.0

# How many steps that commanded the FULL remaining gap — no cap, no ceiling
# clamp — may land outside the band before the pass stops and reports the
# measured slope. One miss buys a correction from the new reading; a second
# says the chain did not answer its own measurement twice running.
MAX_MISSED_FULL_STEPS = 2

# Commanded volumes closer together than this are the same volume: the step
# arithmetic is dB and the fader is not infinitely resolved.
STEP_EPSILON_DB = 0.05

# How many samples of ONE window are retained for its trace. A production
# window holds about 12 (one sample per 2048-frame ALSA period, ~42.7 ms at
# 48 kHz), so this bounds a sample source that delivers faster than a sound card
# can. The cap is PER WINDOW and a reading keeps only its last window's trace.
# The sample that STOPPED a window is recorded outside the cap, so truncation
# can never lose it.
WINDOW_TRACE_MAX_SAMPLES = 256

# How many extra silent READINGS one pass can spend re-measuring a contradicted
# floor. ONE, and not a knob: a third reading would only ask the same question
# again with the same instrument, and the rise gate already reports a
# twice-contradicted floor as `mic_not_observing`.
#
# Readings, not windows: a silent re-measure settles the same way every other
# reading does, so what it costs the watchdog is one settle, not one window —
# plus the two fade legs that bracket its silence, which are priced separately
# because they are not readings (:data:`FADE_LEGS_PER_PASS`).
REMEASURE_READINGS = 1

# How many extra readings the BANK CONFIRM adds to the walk's budget. One: on a
# chain that has actually settled the confirm agrees the first time it is asked.
#
# It is ADDED rather than taken out of the miss budget on purpose. Folding it in
# would silently cost a chain one of its allowed misses, so a chain that needed
# its whole budget to reach the band would arrive there and then have nothing
# left to confirm with. A chain whose readings keep disagreeing still spends
# only the walk's own budget and then refuses; the wait is never unbounded.
BANK_CONFIRM_READINGS = 1

# Whole-operation watchdog slack, on top of the pass's own priced worst case
# (see :func:`_watchdog_seconds`). A backstop against a wedged awaitable — a
# volume setter or a sample source that never returns — never a step governor:
# this number does not bound the ramp's shape.
WATCHDOG_SLACK_S = 15.0

# Fade the commanded volume down before the tone is killed, so a broadband
# stimulus never stops at full level into the DAC.
#
# The rule is about EDGES, not about one call site, so every place this pass
# starts or stops the stimulus at a measurement level walks the fader through
# these steps: the end-of-run :func:`_fade_and_stop`, and both edges of the
# silent re-measure (:func:`_remeasure_silence`). A fade's shape — how many
# writes and therefore how many seconds — has one owner, :func:`fade_steps`, so
# :func:`_watchdog_seconds` prices exactly the fades the pass can spend.
FADE_STEP_DB = 2.0
FADE_STEP_S = 0.03
FADE_FLOOR_DB = -50.0

# How many fade legs one pass can walk INSIDE the watchdog scope, and so how
# many :func:`_watchdog_seconds` prices: the silent re-measure's down and up
# edges, and the end-of-run fade. A count of code paths, not a tunable. The
# pre-tone ambient read is outside the scope and moves no fader; a pass that
# never re-measures walks fewer than it was priced for, which is the safe
# direction for a backstop.
FADE_LEGS_PER_PASS = 3

# How many times a teardown step may be re-awaited after the pass is cancelled.
#
# A plain ``finally: await ...`` DOES complete after ONE ``task.cancel()`` (the
# CancelledError is delivered once); it is a REPEATED cancellation that strands
# it — an operator pressing Ctrl-C twice, or a supervising timeout that
# re-cancels. Each attempt here shields the teardown and re-awaits it.
#
# Bounded on purpose, and what that costs: ``cancel_tone`` is synchronous but
# runs in ``_fade_and_stop``'s own ``finally`` — AFTER the fade, not on the
# first shield attempt — so an abandoned teardown cuts the stimulus abruptly at
# whatever level the ramp had reached and leaves the fader there with the
# durable latch still on disk for the recovery machinery to drain. Bounded by
# the same headroom ceiling every other path is.
TEARDOWN_SHIELD_ATTEMPTS = 4

# Refusal codes. Stable strings: they are the operator-facing reason and the
# `event=` field, so they are named once here.
REFUSE_MIC_NOT_OBSERVING = "mic_not_observing"
REFUSE_SPL_CEILING_EXCEEDED = "spl_ceiling_exceeded"
REFUSE_SPL_TARGET_UNREACHABLE = "spl_target_unreachable"
REFUSE_MIC_FEED_LOST = "mic_feed_lost"
REFUSE_MIC_CLIPPING = "mic_clipping"
REFUSE_RAMP_ERROR = "ramp_error"
REFUSE_SPL_TARGET_UNCAPTURABLE = "spl_target_uncapturable"
REFUSE_VOLUME_CEILING_TOO_LOW = "volume_ceiling_below_ramp_start"
REFUSE_VOLUME_LATCH_UNCONFIRMED = "volume_latch_unconfirmed"
# Two steps commanded the full measured gap and neither landed in the band.
# The refusal carries the measured dB-per-dB slope so the operator sees WHY.
REFUSE_LEVEL_UNCONVERGED = "spl_level_unconverged"
# The level never stopped moving, so no number from it may be believed. ONE slug
# at two scales: consecutive WINDOWS disagreeing until SETTLE_TIMEOUT_S (a
# reading that cannot be believed at all), and consecutive READINGS disagreeing
# until the walk's budget runs out (a level that reaches the band and then
# creeps out from under it). The refusal's detail names which scale and quotes
# the two figures that disagreed. Distinct from `spl_level_unconverged` above,
# which is the ramp failing to REACH the band at all.
REFUSE_LEVEL_UNSETTLED = "spl_level_unsettled"
# The whole-operation watchdog fired: something the pass awaits never returned.
REFUSE_WATCHDOG_EXPIRED = "seat_level_watchdog_expired"
# Ctrl-C, SIGINT, or any other cancellation of the pass. Recorded, never
# swallowed: the cancellation still propagates once the teardown has run.
REFUSE_INTERRUPTED = "seat_level_interrupted"
# Reuses jasper-angle-capture's slug verbatim: same door, same durable fact.
REFUSE_SESSION_ALREADY_LIVE = "measurement_session_already_live"
# The shared measurement window would not open — another measurement holds the
# speaker, or mux could not prove household music is out of the mix. Distinct
# from REFUSE_SESSION_ALREADY_LIVE, which is the durable volume-latch fact read
# before anything is attempted.
REFUSE_ISOLATION_UNAVAILABLE = "measurement_isolation_unavailable"

# The two sample-domain stops' operator-facing wording, named once because two
# places run those stops on every sample: a measurement window
# (:func:`_window_reading`) and a fade leg (:func:`_watched_fade`). The CEILING
# itself already has one owner — the ``spl_ceiling_db_spl`` parameter threaded
# from the profile — and these keep the sentence reporting it from becoming a
# second one.
CLIPPED_CAPTURE_DETAIL = "the capture clipped; no level can be read from it"


def over_ceiling_detail(*, observed_db_spl: float, spl_ceiling_db_spl: float) -> str:
    """What a sample above the profile's commissioning SPL stop reports."""
    return (
        f"measured {observed_db_spl:.1f} dB SPL, above the profile's "
        f"commissioning stop {spl_ceiling_db_spl:.1f} dB SPL"
    )


class SeatLevelRampError(RuntimeError):
    """The leveling step cannot form a safe ramp from these inputs."""


# An interrupted pass still owes its caller one fact: did the household get its
# volume back? The cancellation is RE-RAISED rather than swallowed, so there is
# no return value to carry it — it rides on the propagating exception, stamped
# by the teardown that measured it.
RESTORED_ATTR = "seat_level_restored"


def interrupted_restore_outcome(exc: BaseException) -> bool | None:
    """What the pass's teardown achieved before ``exc`` propagated out of it.

    ``None`` when the exception never passed through a pass that had opened the
    latch — which is the honest answer, not an optimistic one: a caller that
    gets ``None`` here must not claim the volume was restored.
    """
    value = getattr(exc, RESTORED_ATTR, None)
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class SeatLevelResult:
    """The outcome of one leveling pass.

    ``status`` is ``"converged"`` or ``"refused"``; a refusal always carries a
    ``reason`` from the ``REFUSE_*`` set and persisted nothing. ``ramp`` is this
    pass's own telemetry — the start, the ceiling, the measured ambient, and one
    entry per reading — read by ``jasper-seat-level --json``.

    ``restored`` says whether the household volume actually came back: ``None``
    before anything was moved, and a MEASURED outcome after, because the volume
    seam can reject a write (CamillaDSP down mid-pass).
    """

    status: str
    reason: str | None = None
    detail: str | None = None
    reference_volume_db: float | None = None
    measured_db_spl: float | None = None
    restored: bool | None = None
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
            "ramp": self.ramp,
        }


SampleSource = Callable[[], Awaitable[list[LevelSample]]]


@dataclass(frozen=True)
class _WindowTrace:
    """What one settle window actually heard, sample by sample.

    ``samples`` is ``(offset_s, db_spl)`` per finite sample, offset from the
    moment the READING began — for a climb reading, the moment the volume step
    was commanded — so every window of one settling reading reads as a single
    timeline rather than several that each restart at zero. That offset comes
    from the pass's OWN injected clock when the sample is processed, so it lags
    capture by at most one poll interval plus one chunk.

    ``seen`` is the true finite-sample count and exceeds ``len(samples)`` only
    when :data:`WINDOW_TRACE_MAX_SAMPLES` truncated the retained series.
    ``trip`` is the sample that STOPPED the window, recorded outside that cap;
    it is ``None`` for a window that ended on its own deadline — including
    :data:`REFUSE_LEVEL_UNSETTLED`, whose window has a median to read but no
    trip — and for a clipped capture, whose level is meaningless by definition.
    """

    samples: tuple[tuple[float, float], ...]
    seen: int
    trip: tuple[float, float] | None = None

    def series(self) -> str:
        """The retained samples as one ``offset:dB SPL`` line."""
        return " ".join(f"{at:.3f}:{level:.1f}" for at, level in self.samples)

    def summary(self) -> dict[str, Any]:
        """This window's facts, for the refusal receipt and its event line.

        ``retained`` is what min/median/max were computed over. It equals
        ``samples`` unless the cap truncated the series, and is published rather
        than implied so a truncated window cannot read as a whole one — the trip
        is recorded outside the cap, so under truncation ``trip_db_spl`` can
        legitimately exceed ``max_db_spl``.
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
    """A refusal's last window as ``stopped_window_*`` fields on its event line.

    ``stopped_window_``, not ``window_``: ``seat_level_start`` already emits
    ``window_dbfs`` for the TARGET BAND converted to mic dBFS, a different
    concept entirely. This is the one owner of the ``stopped_window_`` prefix,
    so the journal's vocabulary and the receipt's ``ramp.stopped_window`` object
    cannot drift into two names for one number.
    """
    return {f"stopped_window_{key}": value for key, value in summary.items()}


def _window_phrase(summary: dict[str, Any], *, windows: int | None = None) -> str:
    """One sentence of a refusal's last window, for the operator's own terminal.

    The ``trip`` clause is conditional rather than assumed: a sample-domain stop
    abandoned its window and names the sample that ended it, while
    :data:`REFUSE_LEVEL_UNSETTLED`'s window ran to its own deadline and has no
    trip to name.

    ``windows`` is the reading's own window count, and ``0`` renames the noun:
    :func:`_watched_fade` opens NO window, so a stop on a fade leg calling its
    samples "the window it stopped in" would claim a measurement that was never
    taken. ``None`` (the caller has no reading to ask) keeps the window wording.

    Empty for a window that saw no finite sample: the refusal that produces one
    (:data:`REFUSE_MIC_FEED_LOST`) already says exactly that in words.
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

    ``rms_dbfs`` is the median of EVERY finite sample in the window that settled
    it — no sample is dropped for being quiet, because "the speaker is still
    under the room" is evidence the step arithmetic uses. ``None`` means the
    reading produced no believable level: the mic delivered nothing finite, a
    sample-domain stop abandoned the window, or the level never settled.

    ``samples`` counts the finite samples behind that median. ``windows`` is how
    many windows the reading took to settle — this chain's own answer time.
    ``trace`` is the last window, or the abandoned one on a sample-domain stop.

    One shape here is not a reading at all: a sample-domain stop fired on a FADE
    leg (:func:`_watched_fade`) comes back as one of these so the walk has a
    single refusal vocabulary. It always carries ``rms_dbfs=None`` with
    ``samples`` and ``windows`` at zero, and its ``trace`` is the leg it stopped
    in rather than a window.
    """

    rms_dbfs: float | None
    samples: int
    trace: _WindowTrace
    windows: int = 1
    refusal: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Unconfirmed:
    """Two readings that each qualified to bank and did not agree with each other.

    The walk RETAINS the last of these to its tail, so the pair carries its OWN
    volume and its own ordinals in the receipt's ``steps`` array: by the time
    the budget runs out the walk has stepped somewhere else, and formatting the
    pair against the CURRENT commanded volume would attribute two real readings
    to a volume neither was taken at.
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

    Both stops run on EVERY sample seen and abandon the window the moment one
    fires: a clipped capture (the level is meaningless) and a sample above the
    profile's commissioning SPL stop. Both are checked before the median, so the
    pass cannot sit at an over-ceiling level for the rest of a window.

    ``started`` is the moment the READING began (the volume step, for a climb
    reading), and every offset on the trace is measured from it, so the windows
    of one settling reading form a single timeline. ``attempt`` is this window's
    1-based place in that reading.

    Every window — the ambient one and every climb one, stopped or not — leaves
    a :class:`_WindowTrace` on the reading and emits its per-sample series as
    ONE DEBUG line (``event=active_speaker.seat_level_window_samples``).
    ``window`` names which reading that line belongs to: ``"ambient"``,
    ``"silence"``, or the commanded volume the climb was sitting at. The line is
    built only when DEBUG is actually enabled.
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

    ``agree_db`` and ``timeout_s`` are run-scoped and operator-overridable,
    resolved once in :func:`run_seat_level_ramp`; :data:`MIC_WINDOW_S` is a fixed
    property of the meter and is applied HERE, so the receipt's
    ``settle_window_s`` has exactly one writer.

    The LATER of the two agreeing windows is the reading: the same level by
    construction, and on a level with any residual climb left in it, the louder.

    Three ways out other than agreement, and none of them banks a number:

    * a sample-domain stop or a clipped capture — returned straight from the
      window that fired it, with the window it abandoned attached;
    * a window with no finite sample in it — :data:`REFUSE_MIC_FEED_LOST`, after
      ONE window, so a dead feed never waits out the timeout;
    * ``timeout_s`` elapsed with the windows still disagreeing —
      :data:`REFUSE_LEVEL_UNSETTLED`, naming the last two and their distance.
      A reading always gets at least two windows before this can fire, because
      "two consecutive windows agree" cannot be answered with fewer.

    What agreement bounds is the RATE, never the remaining distance: banking
    happens once consecutive medians move less than ``agree_db`` per window, and
    what the level has LEFT to travel at that moment is that rate times the
    chain's own time constant::

        residual ~= (agree_db / MIC_WINDOW_S) x tau

    About 1 dB per second of tau at the shipped defaults, and UNBOUNDED in tau.
    Measured on ONE READING on the synthetic first-order rig, at those defaults::

        tau = 0.81 s  ->   0.28 dB under the level it is heading for
        tau = 3 s     ->   2.11 dB under
        tau = 5 s     ->   4.16 dB under
        tau = 30 s    ->  19.46 dB under, settling in the MINIMUM two windows

    tau = 3 s is inside the range :data:`SETTLE_TIMEOUT_S` covers, so this is a
    real operating region rather than a corner. A LOW window count is therefore
    not evidence of stillness: ``windows == 2`` means EITHER the level was
    genuinely still OR it was moving too slowly for consecutive medians to
    differ by ``agree_db`` at all, and the second case is where the error is
    largest. Read ``windows`` as this chain's answer time against its
    neighbours, never as a confidence score.

    Those are per-READING figures; what the pass BANKS is bounded more tightly.
    The banked volume outlives the pass and nothing downstream re-checks it, so
    banking takes two consecutive READINGS that agree
    (:data:`BANK_CONFIRM_READINGS`, and the confirm in
    :func:`_walk_to_the_band`) — a whole reading apart, so the same bar over
    that longer baseline catches the creep this residual describes.
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
    """One bite: :data:`BITE_FRACTION` of the span this run has to sweep.

    Computed once, before the tone, from the run's own start and ceiling, so the
    climb's step size is a property of the range in front of it.
    """
    return BITE_FRACTION * max(0.0, float(ceiling_db) - float(start_db))


def mic_is_not_observing(
    *, max_rise_db: float, min_rise_db: float, at_ceiling: bool
) -> bool:
    """At the ceiling with no reading ever clear of the room: nobody is listening.

    Without this, a mic that is open and delivering samples but hearing nothing
    (wrong card, in a bag, muted at the OS) is reported as a quiet amplifier,
    which sends an operator to the wrong part of the chain.

    The ceiling is the ONLY non-arbitrary place to ask: from a low start in a
    loud room, any fixed probe span fires on a perfectly good chain that simply
    has not emerged yet.

    What bounds the wait: ``max_main_volume_db`` is DIGITAL HEADROOM — the
    loudest volume at which no driver's branch reaches full scale, with the
    declared per-driver caps disclosed beside it on
    ``event=active_speaker.unsegmented_ceiling_bound`` rather than binding it.
    So a dead-mic walk is bounded by that headroom ceiling, by full scale
    itself, and by the graph's limiters.

    The measured SPL stop is deliberately NOT in that list, and cannot be: a mic
    that is not observing reports a level that does not move with the volume, so
    a -90 dBFS feed reads about 16 dB SPL against an 85 dB SPL stop the whole way
    up — structurally inert on the exact failure mode this predicate is about.
    """
    return max_rise_db < min_rise_db and at_ceiling


def fade_steps(*, from_db: float, to_db: float) -> int:
    """How many :data:`FADE_STEP_DB` writes a fade between two volumes makes.

    The single owner of a fade's shape: the legs are walked from this number
    (:func:`_fade_levels`) and PRICED from it (:func:`_watchdog_seconds`).
    Direction is not part of the answer — a fade up and the mirror fade down
    cost the same.
    """
    return math.ceil(abs(float(to_db) - float(from_db)) / FADE_STEP_DB)


def fade_seconds(*, from_db: float, to_db: float) -> float:
    """The wall-clock one fade leg spends sleeping between its writes."""
    return fade_steps(from_db=from_db, to_db=to_db) * FADE_STEP_S


def fade_quiet_db(from_db: float) -> float:
    """Where a fade-out walks TO: the floor, or ``from_db`` if already quieter.

    A fade only ever walks DOWN, and this is the one place that is decided. The
    climb's downward steps are UNCAPPED — ``capped_gap_step_db`` saturates
    upward only, because a downward step reduces risk — so a hot chain that
    reads over the target at :data:`SEAT_LEVEL_START_DB` steps to a commanded
    volume BELOW :data:`FADE_FLOOR_DB`, and a fade-out walked from there to the
    floor would RAISE the level with the stimulus playing.

    Below the floor there is nothing to fade and the answer is zero steps.
    """
    return min(float(from_db), FADE_FLOOR_DB)


def walk_reading_budget(*, start_db: float, ceiling_db: float) -> int:
    """How many settled readings one climb may spend, at most.

    One at the start volume, one per bite from there to the ceiling, the misses
    the chain is allowed (:data:`MAX_MISSED_FULL_STEPS`), and the bank confirm
    (:data:`BANK_CONFIRM_READINGS`). Because the bite is a fixed fraction of the
    span, the bite count is ``ceil(1 / BITE_FRACTION)`` for every chain, so this
    number is the same whatever the hardware.

    The single owner of that count: :func:`_walk_to_the_band` spends it and
    :func:`_watchdog_seconds` prices it, so adding a reading to the walk changes
    the budget here, once, and the price follows.
    """
    bite = bite_db(start_db=start_db, ceiling_db=ceiling_db)
    span_db = max(0.0, float(ceiling_db) - float(start_db))
    bites = math.ceil(span_db / bite) if bite > 0.0 else 0
    return 1 + bites + MAX_MISSED_FULL_STEPS + BANK_CONFIRM_READINGS


def _watchdog_seconds(
    *, start_db: float, ceiling_db: float, settle_timeout_s: float = SETTLE_TIMEOUT_S
) -> float:
    """This pass's own worst case, priced as the readings and fades it takes.

    :func:`walk_reading_budget` — every reading the climb may spend — plus the
    ONE silent re-measure a contradicted floor can cost
    (:func:`_remeasure_silence`), each priced at ``settle_timeout_s``, the most
    one reading can spend; plus the THREE fade legs the pass can walk inside
    this scope — the re-measure's down and up edges and the end-of-run
    :func:`_fade_and_stop` — each priced at its own worst case, a fade from the
    ceiling to :data:`FADE_FLOOR_DB`; plus :data:`WATCHDOG_SLACK_S`.

    Priced at the ceiling because the ceiling is reachable by a HEALTHY pass:
    agreement is tested BEFORE the timeout, so a chain that keeps disagreeing
    until just under the bar spends the whole timeout on a reading that
    CONVERGES, and a budget priced at "a second a reading" would fire the
    backstop on it. The fades are a term rather than a rounding error for the
    same reason — they are the only seconds inside this scope that are not a
    reading, so leaving them out would make the margin silently smaller than the
    constant that names it.

    Scope: the ambient read happens BEFORE the timeout scope opens, so this
    budget covers the tone-playing walk only, and the margin is
    :data:`WATCHDOG_SLACK_S` and nothing else. A feed that never returns during
    the ambient read is not bounded here — nothing has been mutated at that
    point (no tone, no latch, fader unmoved) and the operator's interrupt is the
    stop. Priced against the ACTUAL start and the ACTUAL bite.
    """
    readings = (
        walk_reading_budget(start_db=start_db, ceiling_db=ceiling_db)
        + REMEASURE_READINGS
    )
    # The loudest a fade leg can start (or end) is the ceiling, which the climb
    # never commands above. Priced through `fade_quiet_db` rather than against
    # FADE_FLOOR_DB directly, so a ceiling already under the floor prices ZERO
    # here for the same reason it walks zero steps there.
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

    A bare ``finally: await ...`` survives a single cancellation (CancelledError
    is delivered once) but not a second — Ctrl-C twice, or a re-cancelling
    supervisor: the restore's next await raises and the fader is left at a
    measurement level. Shielding the teardown and re-awaiting it is what
    finishes it. Bounded by :data:`TEARDOWN_SHIELD_ATTEMPTS`, and never
    re-raises: a failed teardown is logged, not allowed to mask the outcome that
    sent us here.

    Returns whether the step actually completed, so the pass can PUBLISH that
    rather than assume it.
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
    """Every commanded volume one fade leg writes, in order, ending exactly at
    ``to_db``.

    Terminating by construction: the count comes from :func:`fade_steps` and each
    step is clamped at the destination, so the last write IS ``to_db`` and no
    float comparison decides when to stop.
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

    ``cancel_tone`` is synchronous and sits in this function's own ``finally``,
    so the stimulus stops even when the fade itself cannot run.

    The pass's TEARDOWN edge, and the only fade that is not sample-watched: by
    the time it runs the outcome is already decided, so a stop it could see
    would have nowhere to be reported. :func:`_watched_fade` is the mid-pass one.

    The destination is :func:`fade_quiet_db` rather than :data:`FADE_FLOOR_DB`
    itself, because :func:`_fade_levels` walks in whichever direction it is
    pointed and a fade-to-stop only ever goes down.
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

    Returns the refusal a sample forced, or ``None`` when the leg completed and
    the fader is sitting exactly at ``to_db``.

    A fade is AUDIBLE seconds — the stimulus plays throughout — and the two legs
    of the silent re-measure are the only audible time the pass spends outside a
    measurement window WHILE ITS OUTCOME IS STILL UNDECIDED, so they run the
    same two stops a window does, from the same wording
    (:data:`CLIPPED_CAPTURE_DETAIL`, :func:`over_ceiling_detail`) against the
    same ``spl_ceiling_db_spl``. They bank no median: the levels a fade sweeps
    through are on their way somewhere by definition. The pass's THIRD leg, the
    end-of-run :func:`_fade_and_stop`, is audible and outside a window too and
    is deliberately NOT watched — its own docstring owns that reasoning.

    A refusal from here carries the leg's own samples as its trace, published
    with ``windows=0`` so :func:`_window_phrase` calls the thing a "fade leg"
    rather than a window it never opened. One
    ``event=active_speaker.seat_level_fade`` line per leg, not per write (a leg
    is up to 25 writes at the ceiling); ``direction`` names the way the FADER
    walks. No per-sample DEBUG series here, unlike :func:`_window_reading`: a
    leg's samples are checked and discarded, so there is no median to read them
    against, and a leg that STOPS publishes its trace on the refusal instead.

    A mid-leg failure of the injected volume or sample seam that belongs to the
    :data:`RECOVERABLE_ERRORS` family comes back as a :data:`REFUSE_RAMP_ERROR`
    refusal rather than propagating, so the caller's own ``finally`` still gets
    the household its volume back. That family and not "any failure": the
    production volume seam is ``CamillaController.set_volume_db``, which raises
    ``CamillaUnavailable``, and that derives from bare ``Exception``, so it is
    NOT in :data:`RECOVERABLE_ERRORS` and propagates past this guard to the CLI.
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
            # Zero WINDOWS, explicitly and not by default: no window was opened
            # here, and the one-window default would claim a measurement that
            # was never taken.
            windows=0,
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
                    # A fade is not a reading, so a feed that goes quiet across
                    # one is not `mic_feed_lost`: there is no median here to be
                    # missing, and the next window answers that question.
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
        # The traceback goes to the journal because `str(exc)` alone rarely
        # names the seam. The type-name fallback covers an exception whose
        # `str` is empty (a bare `ValueError()`), which would otherwise hand the
        # refusal path an empty detail and let its fallback prose blame the
        # microphone for a volume-seam failure.
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
    — the loudest volume at which the actual stimulus still has digital headroom
    in every driver's branch. The ramp never commands above it.

    The hold rides the crossover session's own volume plan, on the SAME durable
    statefile a measurement session uses, so the recovery machinery already
    watching that file sees a killed leveling process. It refuses to start under
    a live or unresolved session (:func:`live_measurement_session`).

    The whole pass runs inside the shared measurement window
    (:func:`jasper.correction.coordinator.measurement_window`), owned as
    ``seat-level``: that stops jasper-voice's ``VolumeCoordinator`` patrol
    reconciling the fader back toward the household level while the ramp climbs,
    keeps household music out of the mix through mux's diagnostic gate, and
    holds jasper-control off applying host-slider volume observations. It wraps
    ``plan.open`` too, since the latch's first write is a fader write like any
    other. A window that cannot open is a refusal
    (``measurement_isolation_unavailable``), and every lease it holds
    self-expires, so a killed pass frees the speaker with no operator step.

    Persists a reference only on a reading inside the band that rose clear of a
    floor THIS PASS MEASURED IN SILENCE — the first ambient window, or the one
    re-measured mid-climb when a reading contradicted it.
    """
    window_low_dbfs, window_high_dbfs = validate_seat_level_window(
        target=target, sensitivity=sensitivity
    )
    ceiling_db = seat_level_ceiling_db(max_main_volume_db)
    min_rise_db = bounded_env_float(
        "JASPER_SEAT_LEVEL_MIN_RISE_DB", MIC_RESPONSE_MIN_RISE_DB, lo=1.0, hi=20.0
    )
    # The two halves of "settled", read once here so one pass has one answer.
    # Disclosed on the receipt and the start event, because widening the first
    # one lowers the bar for banking.
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

    # Written by the teardown in the `finally` below and read by the caller
    # after it, so the published result can STATE the restore, not assume it.
    restored: dict[str, bool | None] = {"ok": None}

    async def _leveled_under_isolation() -> SeatLevelResult:
        """The whole pass, run with the measurement window already held."""
        # Ambient first, and deliberately BEFORE the latch: nothing is playing,
        # so the fader is irrelevant to it, and a process killed here mutated
        # nothing.
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
        # One comparison, two failures: a ceiling that leaves no room to climb,
        # and a non-finite one (every NaN comparison is False).
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
        # A killed leveling pass should surface far sooner than a measurement
        # session's 30-minute walked-away window.
        plan.set_wall_clock_ceiling_s(watchdog_s + 60.0)
        # THE DIRECT DOOR: this ramp writes the fader itself rather than through
        # a ranked claim, because the process it runs in has no owner to
        # arbitrate through -- `jasper.cli.seat_level` installs none, so
        # `jasper.volume_owner.volume_owner()` answers `None` here. A
        # single-purpose CLI holding the speaker alone has no second writer for
        # an owner to arbitrate against.
        volume_door = FaderVolumeDoor(set_main_volume_db, get_main_volume_db)
        try:
            opened = await plan.open(start_db, volume_door)
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
            # The absolute SPL is only as true as the capture gain the vendor's
            # sens factor assumes. An operator reads journal lines, not docstrings.
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
            # Recorded, then re-raised: an operator who pressed Ctrl-C gets the
            # honest reason in the journal, and the cancellation still reaches
            # the caller. The `finally` below has already put the volume back.
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
            # Restore-exactly-once, on converged / refused / interrupted alike,
            # and whether it SUCCEEDED is published rather than assumed. Two
            # ways it fails and both must read as "not restored": the teardown
            # never finished, or it finished and the latch reports it could not
            # put the level back.
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
            # An interrupt leaves by exception, so there is no result to stamp.
            # Stamp the exception instead: the CLI turns it into an honest
            # refusal.
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
            # The pass finished and already restored the household volume; only
            # teardown failed. Report the outcome that actually happened and
            # make the stuck isolation loud -- every lease self-expires within
            # ~2 minutes.
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

    Returns the new floor and the tone future the caller must now hold, because
    the old one is finished once its player has been cancelled.

    The tone has to actually STOP. The floor this returns feeds the guards that
    decide whether the mic is observing the speaker and whether a reading may
    bank a reference, and those are only answerable against a level measured
    while the speaker is silent — the anti-coincidence property that makes
    "rise" mean "responded to the speaker" rather than "happened to be louder".

    The residual: this window is anti-coincident with the SPEAKER but NOT
    independent of the trigger. It is taken BECAUSE a reading landed low, about
    a second later, and room lulls autocorrelate over seconds, so a lull still
    present when the silent window runs hands back the same low level. The
    observing/banking guard therefore fails on ``P(the first ambient window was
    low)`` PLUS ``P(the first was high AND the re-measure lands low inside the
    same lull)`` — the second term is ADDED, not traded. What the pass buys is
    on the other error type: a contaminated ambient window cannot disqualify
    GOOD readings for the rest of a run. The receipt's ``ambient_remeasured``
    and the event line's ``remeasured_delta_db`` are the operator's tell.

    Bounded and unconditional-once: exactly one extra :func:`_settle_reading`
    plus the two fade legs below, all three priced into
    :func:`_watchdog_seconds`, with no retry and no lag to tune. The stimulus's
    own decay tail after ``cancel_tone`` needs no drained delay: the tail IS a
    moving level, so the window that catches it disagrees with the one after it
    and the reading keeps going until the room is still. A room that never goes
    still refuses (:data:`REFUSE_LEVEL_UNSETTLED`).

    BOTH edges are faded, because the :data:`FADE_STEP_DB` rule is about EDGES:
    a broadband stimulus appearing instantly at a measurement level is the same
    step as one stopping there, with the sign reversed. Both legs turn at
    :func:`fade_quiet_db` and NOT at :data:`FADE_FLOOR_DB` itself — the climb's
    downward steps are uncapped, so a hot chain can already be below the floor,
    where fading "out" to the floor would walk the level UP with the stimulus
    playing. Below the floor both legs are zero steps.

    What a leg bounds is the COMMANDED edge, which is all this seam can promise:
    CamillaDSP ramps a volume write over roughly 400 ms, so a leg audibly dies
    somewhere above the last level it commanded, and a slow tone player lands
    its first sound part-way up the ramp. The silent window itself is unaffected
    by either leg — it opens after ``cancel_tone`` and closes before the tone
    restarts. A sample-domain stop can fire on either leg
    (:func:`_watched_fade`) and comes back as this function's reading.

    EVERY exit from here leaves the stimulus off except the one that returns a
    usable room. When the silent window itself failed the stimulus is NOT
    restarted: the fader is left at the floor and the pass's ``finally``
    restores the household volume through the latch. The legs are wrapped so a
    mid-leg cancellation (Ctrl-C, the whole-operation watchdog) or a seam
    failure cuts the tone on its way out too — without that the caller's
    teardown would run with the stimulus still commanded ON at a level BELOW
    where the climb thinks the fader is, and its first write, computed from the
    climb's number, would command the room UP.
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
        # A plain `finally`, because the normal path stops the tone here anyway:
        # this leg exists to walk the level down BEFORE the stimulus is cut.
        # What the wrapper covers is the abnormal exits -- a cancellation
        # delivered mid-leg, or an error `_watched_fade` could not turn into a
        # refusal -- which would otherwise leave the room playing.
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
    # The fader is already at the floor, so the stimulus starts quiet and the up
    # leg is the only write needed to put it back where the climb had it.
    restarted: asyncio.Future[Any] = asyncio.ensure_future(play_continuous_tone())
    # A flag rather than the down leg's plain `finally`, because this leg's
    # SUCCESS path deliberately hands the caller a playing stimulus to carry on
    # climbing with. What it has to cover is `CancelledError` -- the operator's
    # Ctrl-C and the whole-operation watchdog -- plus anything else
    # `_watched_fade` could not turn into a refusal; such an exit would
    # otherwise leave the stimulus commanded ON with the fader somewhere
    # between `quiet_db` and `volume_db`.
    #
    # A bare-`except BaseException` handler that re-raises is equivalent on
    # behaviour here (this block has no `return`, `break` or `continue`), but
    # `tests/test_lint_contracts.py` demands a literal BLE001 suppression
    # comment on any such line -- a text scan with no re-raise exemption -- AND
    # caps the tree-wide suppression-comment total, so no spelling of the
    # handler passes both guards. The flag has the same behaviour and costs
    # neither ratchet.
    #
    # `restarted` is cancelled here and nowhere else: every other exit hands it
    # back for the caller's own teardown, and an exit by exception hands back
    # nothing.
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
        # A stop on the way back UP is the one RETURNING path here that would
        # otherwise hand the caller a PLAYING stimulus at a partly-restored
        # fader, so this is what makes the invariant uniform: on every failure
        # path out of this function the stimulus is already off. It cuts the
        # room the INSTANT the stop fires, rather than leaving a descending
        # stimulus audible for the three quarters of a second the teardown's own
        # fade can take.
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
    # The climb's INTENT: the volume the walk believes it is measuring at. Every
    # piece of the walk's arithmetic -- the step, the gap, the slope, what a
    # `steps[]` entry was taken at, what gets banked -- is about this number, and
    # a fade leg must never move it.
    volume_db = start_db
    # Where the FADER actually is, as a LOWER BOUND, and a different fact: a
    # fade leg walks the fader down and back up without the climb's intent
    # changing, so anything asking "what is the room hearing right now" -- the
    # teardown's own fade, the refusal that quotes where the pass stopped --
    # reads this one instead. Maintained by `write_fader` below; `start_db` is
    # where `plan.open` confirmed the fader. See
    # docs/adr/0005-fader-bound-asymmetric-record-point.md.
    fader_db = start_db

    async def write_fader(level: float) -> None:
        """Move the fader and keep ``fader_db`` a LOWER BOUND on where it is.

        A write either lands or raises, so after attempting ``old -> L`` the true
        position is one of ``{old, L}``:

        * DOWNWARD (``L < old``): both candidates are ``>= L``, so recording
          ``L`` BEFORE the write keeps the bound valid even when the write
          raises.
        * UPWARD (``L > old``): both candidates are ``>= old``, so the bound must
          stay ``old`` until the write is known to have landed — recorded AFTER.

        Either fixed record point is unsafe on one of the two directions, hence
        the branch. See docs/adr/0005-fader-bound-asymmetric-record-point.md.

        The residual: on a FAILED downward write the bound sits below the true
        fader, so the teardown fades from too low and ``cancel_tone`` cuts a
        stimulus still higher than the teardown thinks — degraded, but never
        upward, which is the price of a bound that can never be too high.

        Every write made while ``fader_db`` still has a reader goes through here
        — the climb's own steps and both legs of :func:`_remeasure_silence`. The
        pass's own teardown fade is the one write that does not: it runs in the
        ``finally`` below, after the result it would inform is already built.
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
    # The BANK CONFIRM: the level of the previous reading that qualified to
    # bank, held so the next one can be checked against it. `None` whenever the
    # last reading did not qualify, so "consecutive" means what it says.
    candidate_dbfs: float | None = None
    # The most recent pair of qualifying readings that DISAGREED, for the
    # refusal that reports a level which reached the band and would not hold
    # still there. Carried with its own volume and ordinals so the sentence
    # quoting it cannot attribute it to wherever the walk ended up instead.
    disagreed: _Unconfirmed | None = None
    # The floor every rise is measured against. ALWAYS a window measured with
    # the speaker silent: the pre-tone ambient, or -- once, when a reading
    # contradicts it -- a second silent window measured mid-climb.
    floor_dbfs = ambient_dbfs
    remeasured_dbfs: float | None = None
    # Non-zero by construction: a span that would make the bite zero is a
    # ceiling at or below the start, which REFUSE_VOLUME_CEILING_TOO_LOW already
    # refused before the tone started.
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
            # The floor the rise gate used is `ambient_remeasured_db_spl` when
            # `ambient_remeasured`, else `ambient_db_spl`. One rule, so neither
            # number is a second writer of the other.
            "ambient_remeasured": remeasured_dbfs is not None,
            "ambient_remeasured_db_spl": (
                None
                if remeasured_dbfs is None
                else round(sensitivity.db_spl_from_dbfs(remeasured_dbfs), 2)
            ),
            "required_rise_db": round(min_rise_db, 2),
            # What "settled" meant for THIS run. `steps[].windows` cannot be
            # read without them, and two of the three are operator-overridable.
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

        Every detail closes with where the ramp stopped, the headroom ceiling it
        stopped against, and the level that produced it: an operator reading a
        refusal is usually not reading the journal. That level is the PRIOR
        window's settled median, so the prose names the volume it was taken at —
        without it the sentence reads as if the ramp had settled at the volume it
        stopped on, which is the one thing a sample-domain stop means it did not.

        "Where the ramp stopped" is ``fader_db`` — where the FADER is — not
        ``volume_db``, the climb's intent. They are the same number on every
        refusal the climb itself raises, and diverge on exactly the refusals a
        fade leg produces: the leg stopped part-way, so the room is quieter than
        the climb's number by up to the whole fade.

        ``trace`` is the window a sample-domain stop abandoned; its facts ride
        the receipt as ``ramp.stopped_window``, the event line as
        ``stopped_window_*``, and the operator's own prose. ``windows`` is that
        reading's window count, which :func:`_window_phrase` reads to name a
        fade leg as one.
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
        if summary is not None:
            window_fields.update(_window_event_fields(summary))
            # Only beside a window: this is the number the window has to be read
            # AGAINST, and on any other refusal the prose already carries it.
            if last is not None:
                window_fields["prior_db_spl"] = f"{last['observed_db_spl']:.1f}"
                window_fields["prior_volume_db"] = f"{last['volume_db']:.2f}"
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
            # A reading CONTRADICTS the floor when it lands below it: the tone is
            # playing, so this reading is the room plus the speaker and cannot be
            # quieter than the room. One of the two windows is wrong, so measure
            # the silence again rather than believe the reading.
            if remeasured_dbfs is None and reading.rms_dbfs < floor_dbfs:
                silent, tone = await _remeasure_silence(
                    tone=tone,
                    volume_db=volume_db,
                    sensitivity=sensitivity,
                    spl_ceiling_db_spl=spl_ceiling_db_spl,
                    # The legs write the fader through the walk's own setter, so
                    # `fader_db` tracks a fade exactly as it tracks a climb step
                    # -- which is what lets the teardown below fade from where
                    # the room actually is.
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
                        # `0` on a fade leg, which is what makes the prose call
                        # it one; the silent window's own refusals carry their
                        # real count and keep the window wording.
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
                    # A LARGE NEGATIVE delta is the residual's signature: the
                    # silent window agreed with the low reading that triggered
                    # it, which is what a room lull persisting across the ~1 s
                    # between them looks like, and is indistinguishable here
                    # from a mic that never responds (see `_remeasure_silence`).
                    # A POSITIVE delta means the floor went UP, so the trigger
                    # reading and everything under the new floor publish
                    # NEGATIVE rises -- conservative for banking.
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
                # How many windows this reading needed before two of them
                # agreed -- this chain's own answer time. NOT a confidence
                # score: see `_settle_reading` on the residual behind
                # `windows == 2`. Read it against its neighbours.
                "windows": reading.windows,
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

            # Convergence needs BOTH: inside the band, and clear of the room --
            # where "the room" is a level THIS PASS MEASURED IN SILENCE. A mic
            # that is not observing the speaker still lands in the band
            # sometimes, and the only thing separating that from a real answer
            # is whether the reading rose above a floor the speaker was NOT
            # contributing to. Measure the floor while the tone plays and the
            # separation is gone.
            in_band = target.low_db_spl <= observed_db_spl <= target.high_db_spl
            if in_band and rise_db >= min_rise_db:
                # THE BANK CONFIRM: a reference is banked only when two
                # consecutive READINGS agree -- the same rule a reading itself
                # is settled by, applied one level up, because window agreement
                # bounds the RATE and not the distance (`_settle_reading` states
                # the residual). The per-sample commissioning stop guards the
                # pass, while the banked number outlives it and every later
                # session consumes that volume with no equivalent stop. A
                # confirm that agrees banks immediately; only a MEASURED
                # disagreement costs another reading inside the same budget.
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
                        session_id=session_id,
                        reference_state_path=reference_state_path,
                        telemetry=telemetry(volume_db),
                    )
                if candidate_dbfs is not None:
                    # Both readings were taken HERE -- the candidate path
                    # continues without stepping -- so this volume, and these
                    # two ordinals in `steps`, are the pair's own identity.
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
                # The step that landed here made its prediction and was right --
                # it reached the band -- so it must not also be charged a miss
                # on the way back round when the confirm disagrees.
                last_step_was_full = False
                # Re-read at the SAME commanded volume: no step, which is what
                # makes the next reading a confirm of this one rather than a
                # measurement of somewhere else.
                continue
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
                # The rise is measured against the floor this pass last measured
                # IN SILENCE, so that is the number this sentence names: quoting
                # the first window beside a rise computed from a RE-MEASURED
                # floor would be two different rooms in one sentence.
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

            # The remaining gap IS the step, saturated upward by this run's own
            # bite. Downward moves are uncapped: they reduce risk.
            step_db = capped_gap_step_db(
                measured_db=observed_db_spl,
                target_db=target.target_db_spl,
                cap_db=bite,
            )
            next_db = min(volume_db + step_db, ceiling_db)
            if abs(next_db - volume_db) <= STEP_EPSILON_DB:
                # Two shapes reach here. At the ceiling the amplifier is the
                # actionable half; off the ceiling the ramp simply cannot move
                # (a reading pinned within STEP_EPSILON_DB of the target while
                # out of band), and telling that operator to turn up an
                # amplifier would send them the wrong way.
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
                    f"band and the ramp cannot climb further; {remedy}",
                    observed_db_spl=f"{observed_db_spl:.1f}",
                )
            # Only a step that commanded the WHOLE measured gap is a prediction
            # the chain can miss. A capped or ceiling-clamped step is a
            # deliberately truncated move, so it never spends the miss budget.
            last_step_was_full = abs(next_db - (volume_db + gap_db)) <= STEP_EPSILON_DB
            volume_db = next_db
            await write_fader(volume_db)

        if disagreed is not None:
            # It DID reach the band -- repeatedly -- and would not hold still
            # there, which is a different sentence from "never arrived". Named
            # by their own volume and ordinals, NOT as "the last two" at
            # wherever the walk stopped: this pair may be several readings back.
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
                # It DID land, on the very last reading it had, and a bank needs
                # one more to confirm with. Saying "without landing in the band"
                # there would send the operator after a level that was reached.
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
        # THE TEARDOWN FADES FROM WHERE THE ROOM IS, NOT FROM WHERE THE CLIMB
        # THINKS IT IS. `volume_db` is the climb's intent and stops being the
        # fader's position the moment a fade leg walks it: a leg stopped
        # part-way leaves the fader up to a whole fade below the climb's number,
        # and a teardown starting from the climb's number would command the
        # level UP as its first act. `fader_db` is a LOWER BOUND on the true
        # position, so the `min` is redundant by construction and kept anyway:
        # it makes "this fade only ever walks down" readable from the expression.
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
    )
    return SeatLevelResult(
        status="converged",
        reference_volume_db=reference_volume_db,
        measured_db_spl=measured_db_spl,
        ramp=telemetry,
    )
