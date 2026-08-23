# SPDX-FileCopyrightText: 2026 Jasper Curry

#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop seat-SPL leveling: find the volume that measures the target.

The crossover session's reference volume was a codified guess. This step makes
it an observation: play the session's own stimulus while a CALIBRATED
measurement mic at the listening seat watches, ramp the main volume until the
seat SPL lands inside the operator's band, and bank the volume that got there
(:mod:`seat_level_reference`) for
:func:`jasper.active_speaker.session_volume_plan.session_measurement_volume_db`
to consume.

**How the ramp moves: the remaining gap IS the step.** Each reading measures
the seat SPL; the next step is ``target_db_spl - measured_db_spl``, saturated
upward at one BITE — and the bite is sized as a fraction of THIS run's own span,
:data:`BITE_FRACTION` of ``ceiling - start``, computed once before the tone.

**Why a fraction of the range and not a number of dB.** This ships to anyone's
hardware and nobody here knows what their amplifier does; an unknown gain only
changes WHERE inside the span the speaker becomes audible, never how wide the
span is. So a range-fraction sweeps any chain in at most ``ceil(1 /
BITE_FRACTION)`` bites, while a fixed dB constant is a guess about a stranger's
amp that is too slow on a quiet one and too coarse on a hot one. Start low —
because we do not know what will get us to the right volume — and keep taking
the same bite until audio appears.

So the ramp takes full bites while it is far away and naturally shrinking ones as
it closes: on jts3 five bites carry it across a 61 dB SPL room and two more,
aimed at the measured gap, land a 75 dB SPL target — seven readings, about seven
audible seconds, where the retired ladder spent 51 s and never arrived.

This assumes only that the chain is LOCALLY monotone in dB — not that it is
globally linear. Every step re-measures, so a chain that answers a 10 dB
command with 7 dB simply takes one more step; the cap bounds how far any single
surprise can carry the room. Downward steps are uncapped because they reduce
risk, exactly as ``calibration_level``'s own contract says.

**Where the ramp starts: low, and it does not matter much.** Bites this big
cross a 61 dB SPL room from :data:`SEAT_LEVEL_START_DB` in five of them, so a
start derived from the measured room was tried and dropped — it bought a second
and cost a helper, a constant, and a refusal. What broke the
retired ladder was never the start on its own — it was a 0.75 dB rung behind a
noise gate, which turned "still under the room" into a stall instead of a
reason to bite again.

**Almost none of the rest is new machinery.** The restore latch is
:class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan` (durable
intent before the first mutation, set-and-confirm, restore exactly once) on the
SAME durable statefile a measurement session uses, so the recovery machinery
that already watches that file watches a leveling pass too. What this module
owns, and nothing else can know:

1. **The window is a dB SPL band, not a dBFS one.** The mic's calibration file
   carries the absolute reference
   (:class:`jasper.audio_measurement.calibration.MicSensitivity`). No
   calibration means no absolute level, so the step REFUSES
   (``mic_calibration_unavailable``, raised by the CLI) rather than chasing an
   uncalibrated number.
2. **Two ceilings.** The loudest main volume at which the ACTUAL stimulus
   still has digital headroom in every driver's branch
   (``unsegmented_stimulus_ceiling_db``) is derived from full scale and the
   stimulus bytes and contains no measured level, so a mis-calibrated
   microphone cannot move it. The measured seat-SPL
   ceiling (``max_commissioning_level_db_spl``) is a second, softer stop that
   shares the calibration's fate — which is exactly why it is not the one
   holding the line. Neither moved in this rework.
3. **The ambient floor, and the rules that read it.** A wired measurement mic
   that is plugged in but not observing the speaker — capturing the wrong card,
   in a bag, muted at the OS — keeps delivering samples at its noise floor.
   Measuring the room ONCE before the tone gives the runaway guard a "did
   anything actually rise" test that does not false-abort a speaker quieter
   than the room, and gives convergence the same test so a stuck-constant mic
   can never bank a reference. That single half-second median is also the
   denominator of every rise, so a transient inside it would become "the room"
   for the whole run — :func:`reconciled_ambient_dbfs` lets the climb's own
   readings put the floor back down when they contradict it, and the pass
   publishes both numbers rather than rewriting what it measured.
4. **What the refusal path saw.** Every settle window leaves a
   :class:`_WindowTrace`, so a sample-domain stop publishes the window it
   abandoned — sample count, min/median/max dB SPL, and the sample that tripped
   with its offset from the volume step — on the receipt, the event line, and
   the operator's own terminal, with the whole per-sample series one DEBUG line
   behind ``--verbose``. Without it, a stop reports a slug and a number that
   cannot be told apart from a level that rose and stayed.

**No sample is discarded for being quiet, so the climb can never stall.** A
reading is the median of every finite sample in its window. A window with
nothing above the room in it still produces a number — the room's own — and the
gap from there to the target is large, so the one rule saturates at the cap and
the ramp takes another big bite. That is the whole behavioural flip: the retired
staircase ran behind a noise gate that dropped ambient-dominated samples before
its state machine saw them, so in a 61 dB SPL room 1138 of 1194 samples were
thrown away and the walk starved (``captures/new-horn-2026-08``). "Still under
the room" is a reason to bite again, never a reason to wait.

Every refusal restores the household volume through the latch and persists
nothing. Only a reading inside the band, clear of the measured ambient, banks a
reference.
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
# owner-scoped) AND jasper-control's volume-hold owner, because the window
# reuses one name for both. It is what ``/state.measurement.owner`` and every
# ``event=measurement.hold_*`` line say while a leveling pass is running.
SEAT_LEVEL_GATE_OWNER = "seat-level"

# The ONE timescale in this pass: how long until the mic reflects a step
# (owner's number, 2026-08-23). It is drained and discarded after each volume
# change so a median never mixes the level before a step with the level after
# it, and the median window that follows is the same length — a wired mic read
# in-process is chunk-bounded, an order of magnitude tighter than the phone
# relay's 2 s, and nothing else in the pass runs on a different clock.
MIC_SETTLE_S = 0.5

# The quiet floor the climb starts from. Deliberately low and deliberately
# uninformed — we do not know what a stranger's amplifier does, so we start
# under anything that could hurt and let the bites find the level. A start
# derived from the measured room was built and dropped: it bought a second and
# cost a helper, a constant, and a refusal.
SEAT_LEVEL_START_DB = -50.0

# One bite, as a fraction of the run's OWN span (ceiling - start). The single
# dimensionless knob in the climb, and dimensionless on purpose: it is the only
# shape that is hardware-independent. An unknown amplifier moves WHERE inside
# the span the speaker becomes audible; it does not change the span, so 0.15
# sweeps any chain in at most ceil(1 / 0.15) = 7 bites whatever the gain. A
# fixed dB bite would instead be a guess about someone else's amp — too slow on
# a quiet one, too coarse on a hot one.
#
# NOT the same question as calibration_level's AUDIBLE_RAMP_STEP_DB, which
# clamps ONE call to the calibration `set` endpoint at 10 dB. That is a
# per-request bound on an operator-driven jump; this is a climb bite sized to
# the span the ramp has to sweep. Two questions, two vocabularies, deliberately.
BITE_FRACTION = 0.15

# The rise a reading must clear to count as the SPEAKER rather than the room —
# read by the runaway guard and by convergence, which is why it is one number.
#
# Measuring rise against AMBIENT rather than against the first reading is what
# keeps a real chain from being falsely aborted: a speaker that starts quieter
# than the room pins the mic at ambient for the first bites, so a 1:1 "track the
# commanded volume" test fires on a perfectly good mic in a normal room. Against
# ambient, such a chain simply has to emerge — which it does, long before the
# ceiling — while a mic that is not listening never emerges at all, because ITS
# ambient reading and ITS signal reading are the same number.
#
# A deploy-time knob (ramp.py's convention for every hardware-gated threshold)
# because the right rise depends on the room's floor, which only hardware knows.
MIC_RESPONSE_MIN_RISE_DB = 6.0

# How many steps that commanded the FULL remaining gap — no cap, no ceiling
# clamp — may land outside the band before the pass stops and reports the
# measured slope. One miss buys a correction from the new reading; a second
# says the chain did not answer its own measurement twice running, which is an
# instrument answer worth printing, not a nanny.
MAX_MISSED_FULL_STEPS = 2

# Commanded volumes closer together than this are the same volume: the step
# arithmetic is dB and the fader is not infinitely resolved.
STEP_EPSILON_DB = 0.05

# How many samples of ONE settle window are retained for its trace. A window is
# MIC_SETTLE_S of drain plus MIC_SETTLE_S of median, and the wired meter
# delivers one sample per 2048-frame ALSA period (~42.7 ms at 48 kHz), so a
# production window holds about 24. This bounds a sample source that delivers
# faster than a sound card can, so neither the retained list nor the single
# DEBUG line built from it can grow without limit. The sample that STOPPED a
# window is recorded outside the cap, so truncation can never lose it.
WINDOW_TRACE_MAX_SAMPLES = 256

# Whole-operation watchdog slack, on top of the pass's own honestly-priced
# worst case (see :func:`_watchdog_seconds`). A backstop against a wedged
# awaitable — a volume setter or a sample source that never returns — never a
# step governor: the retired staircase died to a budget that priced a
# continuous climb while the walk was gated, and no reader should have to
# reason about whether this number bounds the ramp's shape. It does not.
WATCHDOG_SLACK_S = 15.0

# Fade the commanded volume down before the tone is killed, so a broadband
# stimulus never stops at full level into the DAC. Preserved verbatim from the
# retired kernel's fade-before-tone-kill.
FADE_STEP_DB = 2.0
FADE_STEP_S = 0.03
FADE_FLOOR_DB = -50.0

# How many times a teardown step may be re-awaited after the pass is cancelled.
#
# Measured, not assumed: a plain ``finally: await ...`` DOES complete after ONE
# ``task.cancel()`` — the CancelledError is delivered once. It is a REPEATED
# cancellation that strands it, which is exactly what an operator pressing
# Ctrl-C twice, or a supervising timeout that re-cancels, produces. Each attempt
# here shields the teardown and re-awaits it, so the household volume comes back
# under repeated cancellation too.
#
# The count is bounded on purpose: past this many cancellations the operator is
# leaning on the key and gets out. What that costs, stated rather than implied:
# ``cancel_tone`` is synchronous but runs in ``_fade_and_stop``'s own ``finally``
# — i.e. AFTER the fade, not on the first shield attempt — so an abandoned
# teardown cuts the stimulus abruptly at whatever level the ramp had reached,
# and leaves the fader there with the durable latch still on disk for the
# recovery machinery to drain. Abrupt at a measurement level is the deliberate
# trade for always being able to stop; it is bounded by the same headroom
# ceiling every other path is, and the recovery screen sees the latch.
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


class SeatLevelRampError(RuntimeError):
    """The leveling step cannot form a safe ramp from these inputs."""


# An interrupted pass still owes its caller one fact: did the household get its
# volume back? The cancellation is RE-RAISED rather than swallowed (swallowing
# would break task-cancellation semantics for every caller), so there is no
# return value to carry it — it rides on the exception that is already
# propagating, stamped by the teardown that measured it.
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

    ``restored`` says whether the household volume actually came back. ``None``
    before anything was moved, and a MEASURED outcome after: the volume seam can
    reject a write (CamillaDSP down mid-pass), and a pass that left the fader at
    a measurement level must say so rather than let silence read as success.
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

    The refusal path's discriminator, and the reason it exists: before it, an
    aborted window's sample count was computed and then DROPPED, and no
    per-sample SPL was written anywhere — so nothing the pass emitted could
    separate "one tail sample crossed the stop" from "the level rose and stayed
    there". On jts3 (2026-08-23) two 75 dB SPL runs refused on a ~+6.5-7 dB
    excursion appearing ~0.95 s after a volume step, against settled medians of
    64.6 and 64.3 dB SPL, and the mechanism could not be located from any
    artifact the build produced.

    ``samples`` is ``(offset_s, db_spl)`` per finite sample, offset from the
    moment the settle began — which, for a climb window, is the moment the
    volume step was commanded. That offset is read from the pass's OWN injected
    clock when the sample is processed, so it lags capture by at most one drain
    interval plus one chunk; this pass runs on one clock (see
    :data:`MIC_SETTLE_S`) and a second time base carried on the sample would not
    survive the fake clock the tests inject.

    ``seen`` is the true finite-sample count and exceeds ``len(samples)`` only
    when :data:`WINDOW_TRACE_MAX_SAMPLES` truncated the retained series.
    ``trip`` is the sample that STOPPED the window, recorded outside that cap;
    it is ``None`` for a window that ended on its own deadline and for a clipped
    capture, whose level is meaningless by definition.
    """

    samples: tuple[tuple[float, float], ...]
    seen: int
    trip: tuple[float, float] | None = None

    def series(self) -> str:
        """The retained samples as one ``offset:dB SPL`` line."""
        return " ".join(f"{at:.3f}:{level:.1f}" for at, level in self.samples)

    def summary(self) -> dict[str, Any]:
        """This window's facts, for the refusal receipt and its event line.

        The PRIOR window's settled median is deliberately not here: the receipt
        already owns it as ``ramp.steps[-1]``, and a second copy beside it would
        be a second writer of one fact. The refusal's own prose and event line
        carry it because neither of those has the steps array to read.

        ``retained`` is what min/median/max were computed over. It equals
        ``samples`` unless the cap truncated the series, and it is published
        rather than implied so a truncated window cannot read as a whole one —
        the trip is recorded outside the cap, so under truncation
        ``trip_db_spl`` can legitimately exceed ``max_db_spl``.
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
    """An aborted window's facts as ``window_*`` fields on a refusal event.

    One owner of that prefix, so the journal's vocabulary and the receipt's
    ``ramp.window`` object cannot drift apart into two names for one number.
    """
    return {f"window_{key}": value for key, value in summary.items()}


def _window_phrase(summary: dict[str, Any]) -> str:
    """One sentence of an aborted window, for the operator's own terminal.

    Empty for a window that saw no finite sample: the refusal that produces one
    (:data:`REFUSE_MIC_FEED_LOST`) already says exactly that in words, and
    "saw 0 samples" beside it is the same fact twice.
    """
    if not summary["samples"]:
        return ""
    phrase = (
        f"the window it stopped in saw {summary['samples']} samples spanning "
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

    ``rms_dbfs`` is the median of EVERY finite sample in the window — no sample
    is dropped for being quiet, because "the speaker is still under the room" is
    evidence the step arithmetic uses. ``None`` means the mic delivered nothing
    finite at all, which is a lost feed rather than a quiet room.

    ``samples`` counts the finite samples that reached the MEDIAN — the drain's
    are excluded, which is what the converged receipt's ``steps[].samples`` has
    always meant. ``trace`` is the whole window, drain included, and is the only
    place the drain's samples survive.
    """

    rms_dbfs: float | None
    samples: int
    trace: _WindowTrace
    refusal: str | None = None
    detail: str | None = None


async def _settle_reading(
    next_samples: SampleSource,
    *,
    sensitivity: MicSensitivity,
    spl_ceiling_db_spl: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    session_id: str,
    window: str,
    window_s: float = MIC_SETTLE_S,
    latency_s: float = MIC_SETTLE_S,
) -> _Reading:
    """Drain the transport delay, then take the median of one window.

    The two sample-domain stops run on EVERY sample seen, drain included, and
    abandon the window the moment one fires: a clipped capture (the level is
    meaningless) and a reading above the profile's commissioning SPL stop (the
    hard-stop list's measured ceiling). Both are checked before the median so
    the pass cannot sit at an over-ceiling level for the rest of a window.

    Every window — the ambient one and every climb one, stopped or not — leaves
    a :class:`_WindowTrace` on the reading and emits its per-sample series as
    ONE DEBUG line (``event=active_speaker.seat_level_window_samples``, one line
    per window rather than one per sample). ``window`` names which window that
    line describes: ``"ambient"``, or the commanded volume the climb was sitting
    at. The line is built only when DEBUG is actually enabled, so an ordinary
    run pays nothing for it.
    """
    readings: list[float] = []
    trace: list[tuple[float, float]] = []
    trip: tuple[float, float] | None = None
    seen = 0
    started = clock()
    deadline = started + float(latency_s) + float(window_s)

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
                        detail="the capture clipped; no level can be read from it",
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
                        detail=(
                            f"measured {observed_db_spl:.1f} dB SPL, above the "
                            f"profile's commissioning stop "
                            f"{spl_ceiling_db_spl:.1f} dB SPL"
                        ),
                    )
                if at >= float(latency_s):
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
                samples=str(recorded.seen),
                retained=str(len(recorded.samples)),
                db_spl=recorded.series(),
            )


def bite_db(*, start_db: float, ceiling_db: float) -> float:
    """One bite: :data:`BITE_FRACTION` of the span this run has to sweep.

    Computed once, before the tone, from the run's own start and ceiling — so
    the climb's step size is a property of the range in front of it rather than
    a constant chosen against hardware nobody here has seen.
    """
    return BITE_FRACTION * max(0.0, float(ceiling_db) - float(start_db))


def mic_is_not_observing(
    *, max_rise_db: float, min_rise_db: float, at_ceiling: bool
) -> bool:
    """At the ceiling with no reading ever clear of the room: nobody is listening.

    Without this, a mic that is open and delivering samples but hearing nothing
    (wrong card, in a bag, muted at the OS) is reported as a quiet amplifier,
    which sends an operator to the wrong part of the chain.

    The ceiling is the ONLY non-arbitrary place to ask. A fixed "probe span"
    used to arm it earlier, and from a low start in a loud room it fired on a
    perfectly good chain that simply had not emerged yet — jts3's 61 dB SPL room
    tripped it 20 dB into a 44 dB climb, refusing the very incident this pass
    exists to fix.

    **What bounds the wait, stated exactly** (#2910 merged the vocabulary this
    has to use): ``max_main_volume_db`` is DIGITAL HEADROOM — the loudest volume
    at which no driver's branch reaches full scale — with the declared per-driver
    caps disclosed beside it on
    ``event=active_speaker.unsegmented_ceiling_bound`` rather than binding it. So
    the three things that bound a dead-mic walk are that headroom ceiling (the
    loudest volume this run may reach, by construction), full scale itself, and
    the graph's limiters.

    The measured SPL stop is deliberately NOT in that list. It cannot be: a mic
    that is not observing reports a level that does not move with the volume, so
    its reading never approaches the stop however far the ramp climbs — a
    -90 dBFS feed reads about 16 dB SPL against an 85 dB SPL stop the whole way
    up. Citing it here would be citing a guard that is structurally inert on the
    exact failure mode this predicate is about.

    Model-derived cost of the wait (synthetic mic, jts3 rig): the walk tops out
    AT the ceiling and never above it, 8.86 audible seconds of which 1.0 s sits
    at the ceiling, refusal ``mic_not_observing``, household volume restored —
    23.2 dB louder than the retired 20 dB span reached before refusing. An
    on-metal dead-mic run is on the bench checklist; these numbers are a model.
    """
    return max_rise_db < min_rise_db and at_ceiling


def reconciled_ambient_dbfs(
    *, ambient_dbfs: float, quietest_reading_dbfs: float
) -> float:
    """The room floor the rise gate uses: the quietest level this mic reported.

    The ambient window is ONE median of ONE half-second, taken once before the
    tone, and it is then the denominator of every rise the pass computes. So a
    transient inside that half-second becomes "the room" for the whole run. On
    jts3 (2026-08-23) a pass measured ``ambient_db_spl=57.18`` and published
    rises of ``-7.0, -6.3, -3.7`` — tone readings BELOW the measured room — while
    its own -50.00 dB reading (50.21 dB SPL) and the previous pass's (50.59)
    put the real floor near 49.7. ``required_rise_db`` gates trust on
    ``observed - ambient``, so an inflated floor silently disqualifies good
    readings.

    The reconciliation is the physics: the tone is PLAYING, so a climb reading is
    the room plus the speaker and cannot be quieter than the room. A reading
    below the measured ambient is therefore proof the ambient window over-read,
    from the same instrument seconds later — and the floor moves down to it.

    **Why this cannot weaken the guard it feeds.** The failure
    :func:`mic_is_not_observing` exists for is a mic that delivers samples which
    never respond to the speaker; it reports a CONSTANT, so its ambient reading
    IS its signal reading and this ``min`` returns the ambient unchanged. The
    correction is structurally inert on the exact failure mode the rise gate
    guards, and bounded everywhere else by the quietest level the mic actually
    reported during the pass.

    Only the floor is reconciled. The measured ambient is still published as
    measured (``ramp.ambient_db_spl``), beside the floor that was used
    (``ramp.ambient_effective_db_spl``) and whether they differ
    (``ramp.ambient_corrected``) — the pass discloses the correction rather than
    quietly rewriting what it measured.
    """
    return min(float(ambient_dbfs), float(quietest_reading_dbfs))


def _watchdog_seconds(*, start_db: float, ceiling_db: float) -> float:
    """This pass's own worst case, priced as the readings it actually takes.

    One ambient reading, one reading per bite from the start to the ceiling, and
    one per allowed miss — each costing a settle drain plus a median window —
    plus :data:`WATCHDOG_SLACK_S`.

    Scope, because the budget is wider than the thing it guards: the ambient read
    happens BEFORE the timeout scope opens (its result is what the pass logs and
    what the guards read), so this budget covers the tone-playing walk only. The
    ambient window is priced in anyway, which makes the budget generous by one
    reading rather than tight by one — the safe direction for a backstop. A feed
    that never returns during the ambient read is not bounded here — nothing has
    been mutated at that point (no tone, no latch, fader unmoved) and the
    operator's interrupt is the stop. Priced against the ACTUAL start and the
    ACTUAL bite, so it cannot repeat the retired kernel's mistake of budgeting a
    continuous climb for a walk that does not climb continuously. Because the
    bite is a fixed fraction of the span, the bite count is the same
    ``ceil(1 / BITE_FRACTION)`` for every chain.
    """
    bite = bite_db(start_db=start_db, ceiling_db=ceiling_db)
    span_db = max(0.0, float(ceiling_db) - float(start_db))
    bites = math.ceil(span_db / bite) if bite > 0.0 else 0
    readings = 1 + bites + MAX_MISSED_FULL_STEPS
    per_reading = MIC_SETTLE_S + MIC_SETTLE_S
    return MIC_SETTLE_S + readings * per_reading + WATCHDOG_SLACK_S


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

    Stopping the ramp — Ctrl-C, SIGINT, the watchdog, a refusal — must always
    stop the stimulus and hand the household its volume back. A bare
    ``finally: await ...`` survives a single cancellation (CancelledError is
    delivered once) but not a second one, which is what pressing Ctrl-C twice
    or a re-cancelling supervisor produces: the restore's next await raises and
    the fader is left at a measurement level. Shielding the teardown and
    re-awaiting it is what finishes it. Bounded by
    :data:`TEARDOWN_SHIELD_ATTEMPTS`, and never re-raises: a failed teardown is
    logged, not allowed to mask the outcome that sent us here.

    Returns whether the step actually completed, so the pass can PUBLISH that
    rather than assume it. A restore that silently failed and a restore that
    happened must not look the same in the result.
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
            # The kernel's own teardown vocabulary: everything the volume seam
            # and the tone player can raise. A failed teardown is reported, not
            # allowed to mask the outcome that sent us here.
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
    """
    level = float(from_db)
    try:
        while level > FADE_FLOOR_DB:
            level = max(FADE_FLOOR_DB, level - FADE_STEP_DB)
            await set_main_volume_db(level)
            await sleep(FADE_STEP_S)
    except RECOVERABLE_ERRORS:
        logger.exception("seat-level fade-before-tone-kill failed")
    finally:
        cancel_tone()


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

    Persists a reference only on a reading inside the band that rose clear of
    the measured ambient floor.
    """
    window_low_dbfs, window_high_dbfs = validate_seat_level_window(
        target=target, sensitivity=sensitivity
    )
    ceiling_db = seat_level_ceiling_db(max_main_volume_db)
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

    # Written by the teardown in the `finally` below and read by the caller
    # after it, so the published result can STATE the restore, not assume it.
    restored: dict[str, bool | None] = {"ok": None}

    async def _leveled_under_isolation() -> SeatLevelResult:
        """The whole pass, run with the measurement window already held."""
        # Ambient first, and deliberately BEFORE the latch: nothing is playing,
        # so the fader is irrelevant to it, and the start the latch parks at is
        # derived from this very measurement. A process killed here mutated
        # nothing.
        ambient = await _settle_reading(
            next_samples,
            sensitivity=sensitivity,
            spl_ceiling_db_spl=spl_ceiling_db_spl,
            clock=clock,
            sleep=sleep,
            session_id=session_id,
            window="ambient",
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
        watchdog_s = _watchdog_seconds(start_db=start_db, ceiling_db=ceiling_db)

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
        try:
            opened = await plan.open(start_db, set_main_volume_db, get_main_volume_db)
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
            # Restore-exactly-once, on converged / refused / interrupted alike.
            # The latch returns the HOUSEHOLD volume, so a stopped pass never
            # leaves the speaker parked at a measurement level -- and whether it
            # SUCCEEDED is published, not assumed.
            # Two ways this fails, and both must read as "not restored": the
            # teardown never finished, or it finished and the latch reports it
            # could not put the level back (the volume seam raises when
            # CamillaDSP rejects a write, and `close` handles that internally).
            drained: dict[str, Any] = {}

            async def _drain() -> None:
                drained["result"] = await plan.close(
                    set_main_volume_db,
                    get_main_volume_db,
                    reason="seat_level_complete",
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
            # refusal, and `restored: null` beside prose claiming a restore is
            # exactly the dishonesty this field exists to prevent.
            in_flight = sys.exc_info()[1]
            if in_flight is not None:
                setattr(in_flight, RESTORED_ATTR, restored["ok"])

    # Everything that touches the fader runs inside the shared measurement
    # window (jasper.correction.coordinator). Without it, jasper-voice's
    # VolumeCoordinator patrol keeps reconciling the main volume back toward
    # the household level once a second and fights the ramp the whole way up
    # -- the jts3 writer war (journal: `event=volume.reconciled source=idle
    # ... drift_db=+9.35`). The window ALSO holds jasper-control's
    # volume-observation hold, so a host slider cannot walk the level either.
    # It wraps `plan.open` as well as the ramp: the latch's own first write
    # is a fader write like any other.
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
    volume_db = start_db
    steps: list[dict[str, Any]] = []
    max_rise_db = 0.0
    missed_full_steps = 0
    last_step_was_full = False
    previous: tuple[float, float] | None = None
    slope_db_per_db: float | None = None
    # The floor every rise is measured against. It starts as the ambient window
    # read it and can only ever move DOWN -- see `reconciled_ambient_dbfs`.
    effective_ambient_dbfs = ambient_dbfs
    # Non-zero by construction: a span that would make the bite zero is a
    # ceiling at or below the start, which REFUSE_VOLUME_CEILING_TOO_LOW already
    # refused before the tone started.
    bite = bite_db(start_db=start_db, ceiling_db=ceiling_db)
    max_readings = 1 + math.ceil(
        max(0.0, ceiling_db - start_db) / bite
    ) + MAX_MISSED_FULL_STEPS

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
            "ambient_effective_db_spl": round(
                sensitivity.db_spl_from_dbfs(effective_ambient_dbfs), 2
            ),
            "ambient_corrected": effective_ambient_dbfs < ambient_dbfs,
            "required_rise_db": round(min_rise_db, 2),
            "watchdog_s": round(watchdog_s, 1),
            "final_volume_db": round(final_volume_db, 2),
            "slope_db_per_db": (
                None if slope_db_per_db is None else round(slope_db_per_db, 3)
            ),
            "steps": steps,
        }
        if window is not None:
            payload["window"] = window
        return payload

    def refuse(
        reason: str,
        detail: str,
        *,
        trace: _WindowTrace | None = None,
        **evidence: Any,
    ) -> SeatLevelResult:
        """Log the refusal and hand the operator the same facts on stdout.

        Every detail closes with where the ramp stopped, the headroom ceiling it
        stopped against, and the level that produced (#2910): an operator reading
        a refusal is usually not reading the journal, and a bare slug is
        unactionable without those three numbers. That level is the PRIOR
        window's settled median, so the prose names the volume it was taken at —
        without it the sentence reads as if the ramp had settled at the volume it
        stopped on, which is the one thing a sample-domain stop means it did not.

        ``trace`` is the window a sample-domain stop abandoned. Its facts ride
        the receipt as ``ramp.window``, the event line as ``window_*``, and the
        operator's own prose, because separating "one tail sample crossed" from
        "the level rose and stayed" needs all three of those readers to have it.
        """
        last = steps[-1] if steps else None
        stopped = (
            f"stopped at {volume_db:.2f} dB against the {ceiling_db:.2f} dB "
            "headroom ceiling"
            + (
                f", reading {last['observed_db_spl']:.1f} dB SPL at "
                f"{last['volume_db']:.2f} dB"
                if last
                else ""
            )
        )
        summary = None if trace is None else trace.summary()
        phrase = "" if summary is None else _window_phrase(summary)
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
            at_db=f"{volume_db:.2f}",
            ceiling_db=f"{ceiling_db:.2f}",
            readings=str(len(steps)),
            **evidence,
            **window_fields,
        )
        return SeatLevelResult(
            status="refused",
            reason=reason,
            detail=f"{detail} ({stopped}{'' if not phrase else f'; {phrase}'})",
            ramp=telemetry(volume_db, window=summary),
        )

    tone = asyncio.ensure_future(play_continuous_tone())
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
            )
            if reading.rms_dbfs is None:
                return refuse(
                    reading.refusal or REFUSE_MIC_FEED_LOST,
                    reading.detail
                    or "the microphone stopped delivering finite samples",
                    trace=reading.trace,
                )
            observed_db_spl = sensitivity.db_spl_from_dbfs(reading.rms_dbfs)
            reconciled = reconciled_ambient_dbfs(
                ambient_dbfs=effective_ambient_dbfs,
                quietest_reading_dbfs=reading.rms_dbfs,
            )
            if reconciled < effective_ambient_dbfs:
                effective_ambient_dbfs = reconciled
                log_event(
                    logger,
                    "active_speaker.seat_level_ambient_corrected",
                    session=session_id,
                    at_db=f"{volume_db:.2f}",
                    measured_ambient_db_spl=f"{ambient_db_spl:.2f}",
                    effective_ambient_db_spl=f"{observed_db_spl:.2f}",
                )
            rise_db = reading.rms_dbfs - effective_ambient_dbfs
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
            )

            # Convergence needs BOTH: inside the band, and clear of the room. A
            # mic stuck at a constant that happens to sit inside the band would
            # otherwise bank a reference for a level nothing produced -- and
            # because its ambient reading IS its signal reading, its rise is zero.
            in_band = target.low_db_spl <= observed_db_spl <= target.high_db_spl
            if in_band and rise_db >= min_rise_db:
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
                # The rise is measured against the EFFECTIVE floor, so that is
                # the number this sentence has to name. Saying "above the
                # {measured} dB SPL room" beside a rise computed from a
                # reconciled floor would be two different rooms in one sentence.
                floor_db_spl = sensitivity.db_spl_from_dbfs(effective_ambient_dbfs)
                return refuse(
                    REFUSE_MIC_NOT_OBSERVING,
                    f"the volume climbed {volume_db - start_db:.1f} dB to the "
                    f"ceiling and the mic never rose more than "
                    f"{max_rise_db:.1f} dB above the {floor_db_spl:.1f} dB SPL "
                    "room; check that the mic is capturing the right card and "
                    "is not muted",
                    commanded_climb_db=f"{volume_db - start_db:.2f}",
                    ambient_dbfs=f"{ambient_dbfs:.1f}",
                    effective_ambient_dbfs=f"{effective_ambient_dbfs:.1f}",
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
            await set_main_volume_db(volume_db)

        slope = (
            "unmeasured" if slope_db_per_db is None
            else f"{slope_db_per_db:.2f} dB per commanded dB"
        )
        return refuse(
            REFUSE_LEVEL_UNCONVERGED,
            f"the ramp took its whole {max_readings}-reading budget without "
            f"landing in the band; the chain measured {slope} across the last "
            "two readings",
            slope_db_per_db=(
                "" if slope_db_per_db is None else f"{slope_db_per_db:.3f}"
            ),
        )
    finally:
        await run_teardown(
            "fade_and_stop",
            _fade_and_stop(
                from_db=volume_db,
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
