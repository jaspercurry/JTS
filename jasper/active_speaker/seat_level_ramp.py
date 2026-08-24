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
   can never bank a reference. **That floor is only ever measured with the
   speaker SILENT**, and never derived from a reading taken while the tone
   played — measured silence is the anti-coincidence property that makes "rise"
   mean "responded to the speaker" rather than "happened to be louder". A
   transient inside that half-second would otherwise become "the room" for the
   whole run, so when a climb reading CONTRADICTS the floor the pass stops the
   tone and re-measures the silence ONCE
   (:data:`REFUSE_MIC_FEED_LOST` if that window is unreadable), discloses both
   windows, and continues against the second one. That second window is
   anti-coincident with the SPEAKER but NOT independent of the trigger — it is
   taken because a reading was low, a second later, and a room lull can span
   both, which ADDS one way for the banking guard to fail rather than removing
   one. What the pass buys is on the other error type: a contaminated window no
   longer disqualifies good readings. :func:`_remeasure_silence` states both
   sides exactly and names the tests that pin them.
4. **What the refusal path saw.** Every settle window leaves a
   :class:`_WindowTrace`, so a refusal publishes the window it stopped in
   (``ramp.stopped_window`` / ``stopped_window_*``) — abandoned part-way by a
   sample-domain stop, or run to its own deadline by
   :data:`REFUSE_LEVEL_UNSETTLED`, which has no trip to name — sample count,
   min/median/max dB SPL, and the sample that tripped with its offset from the
   volume step — on the receipt, the event line, and
   the operator's own terminal, with the whole per-sample series one DEBUG line
   behind ``--verbose``. Without it, a stop reports a slug and a number that
   cannot be told apart from a level that rose and stayed.
5. **What "settled" means: the instrument's own stability, never a guessed
   lag.** A reading is settled when two consecutive :data:`MIC_WINDOW_S`
   windows agree within :data:`SETTLED_AGREE_DB`; until they do the pass keeps
   reading, and a reading that never agrees inside :data:`SETTLE_TIMEOUT_S`
   REFUSES (:data:`REFUSE_LEVEL_UNSETTLED`) instead of banking the last number
   it happened to see. What this replaced was a fixed drain-then-median — a lag
   model, and a wrong one: on jts3 (2026-08-24) the level was still climbing
   +6.03 dB WITHIN the final window at the top step, so the pass banked a frame
   about 5 dB under the level that actually arrived (−12.50 dB commanded,
   ≈79-80 dB SPL at the seat; issue #2919). No mechanism had to be named to fix
   it, because nothing here models one. A settled chain pays the same second it
   always did (two windows); only a moving one pays more, and every extra window
   is more samples run past the commissioning stop, never fewer. Two costs it
   does carry, both stated where they are set rather than left to be found: what
   agreement still leaves on the table scales with the chain's own time constant
   (:func:`_settle_reading`), and a converging pass can spend the whole timeout
   on every reading (:data:`SETTLE_TIMEOUT_S`).
6. **The same rule guards the ARTIFACT, one level up.** A reference is banked
   only when two consecutive settled READINGS agree
   (:data:`BANK_CONFIRM_READINGS`), because what a single reading leaves on the
   table is not just a wrong label — it is a wrong NUMBER ON DISK. The
   per-sample commissioning stop guards the pass; the banked volume outlives it,
   ``write_seat_level_reference`` validates the volume range and nothing about
   where the chain ends up, and every later session consumes that volume with no
   equivalent stop. Before this, a chain with headroom above the band could
   converge and bank a reference whose level ARRIVES at 89 dB SPL against an
   85 dB SPL stop. Two readings are a whole reading apart, so the same agreement
   bar over that longer baseline catches creep the window test cannot — and what
   is banked is no longer a model of a chain but a level that two separate
   measurements agreed on.

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
nothing. Only a reading inside the band, clear of a floor this pass measured
in SILENCE, banks a reference.
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

# One WINDOW: the span whose median is one observation of the level. Half a
# second is about 12 samples from the wired meter's 2048-frame ALSA period
# (~42.7 ms at 48 kHz) — enough that the median is a level rather than a
# sample, and short enough that the two windows a settled reading normally
# costs take the same second the retired fixed settle took.
#
# NOT a lag model. Nothing here claims to know how long this chain needs after
# a volume step; that is what :data:`SETTLED_AGREE_DB` measures instead.
MIC_WINDOW_S = 0.5

# When two consecutive windows are the SAME LEVEL, and so the whole definition
# of settled: the pass keeps reading until it sees that agreement, and banks
# the later of the two. The instrument's own stability says when a reading may
# be believed — no guessed delay, nothing to re-tune when a chain changes.
#
# The number is sized against the room's own wander at a settled step: on jts3
# (2026-08-24) a converged window varied 0.42 dB across its thirds while a
# window that was still climbing moved 6.03 dB, so 0.5 dB separates the two by
# an order of magnitude.
#
# **It bounds a RATE, not a distance, and that is the residual.** A reading is
# banked once consecutive medians move less than this per :data:`MIC_WINDOW_S`
# — at the shipped values, once the level is moving slower than 1 dB/s (the
# knob below reaches 6 dB/s at its maximum). What the level has LEFT to travel
# at that moment is that rate times the chain's own time constant, so the
# banked level can sit ``(agree_db / MIC_WINDOW_S) x tau`` under the level that
# eventually arrives — about 1 dB per second of tau at the shipped values, and
# unbounded in tau. :func:`_settle_reading` states the consequence and the
# measurements; this is the number that sets its scale.
#
# A deploy-time knob for the same reason :data:`MIC_RESPONSE_MIN_RISE_DB` is
# one: how still a room actually sits is a property only hardware knows, and a
# room that cannot hold 0.5 dB would otherwise have no way to be measured at
# all. Widening it lowers the bar for "settled", so it is disclosed on the
# receipt (``ramp.settle_agree_db``) and on the start event.
SETTLED_AGREE_DB = 0.5

# How long ONE reading may spend proving it settled before the pass refuses it
# (:data:`REFUSE_LEVEL_UNSETTLED`). The honest bound on a wait with no lag
# model behind it: a chain that is genuinely still moving gets the time it
# needs, and one that never stops moving is reported rather than banked.
#
# Generous on purpose — the default covers a chain settling with a time
# constant of about three seconds, which is well past anything jts3's climb
# showed — because the cost of being too tight is refusing a healthy chain.
# A dead feed does NOT wait this out: a window with no finite sample in it is
# :data:`REFUSE_MIC_FEED_LOST` after one window.
#
# **This number sets the pass's audible worst case, and a HEALTHY pass can pay
# it.** Agreement is tested before the timeout, so a window landing at the bound
# still settles: a chain that keeps disagreeing and then agrees just under the
# bar spends the whole timeout on a reading that CONVERGES — pinned by
# ``test_a_converging_pass_can_spend_the_whole_settle_timeout_per_reading``.
# Audible time is therefore bounded by ``walk_reading_budget`` readings at this
# timeout each — about 88 s at the shipped values and about 330 s at the knob's
# maximum — and the silent re-measure adds none of it. The bound is stated as a
# relation rather than a stopwatch reading on purpose: the seconds move whenever
# the reading count or the window length does, and a decimal nothing re-derives
# is how prose starts lying. Not a hearing hazard — every sample is still under
# the commissioning stop, and the doctrine's nanny test says a bound that names
# no damage mechanism does not earn a gate — but it is not "only a broken pass
# pays this" either, which is why it is stated rather than left to be found.
#
# Env-overridable so an operator whose chain settles slower than this has a way
# forward that is not a redeploy; bounded so the knob cannot turn a leveling
# pass into an unbounded tone. Raising it is not free in the other direction
# either — see :func:`_settle_reading` on what a longer wait banks.
SETTLE_TIMEOUT_S = 8.0

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
# ceiling — while a mic pinned at a CONSTANT never emerges at all, because ITS
# ambient reading and ITS signal reading are the same number. That is the easy
# half of "not listening"; a wrong-card mic hears a room that wanders, so its
# two readings differ and it can clear this bar on its own. What holds it back
# is that the floor is a level measured in SILENCE (:func:`_remeasure_silence`
# states exactly how far that goes, and where it does not).
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

# How many samples of ONE window are retained for its trace. A window is
# MIC_WINDOW_S long and the wired meter delivers one sample per 2048-frame ALSA
# period (~42.7 ms at 48 kHz), so a production window holds about 12. This
# bounds a sample source that delivers faster than a sound card can, so neither
# the retained list nor the single DEBUG line built from it can grow without
# limit. The cap is PER WINDOW and a reading keeps only its last window's
# trace, so a reading that takes many windows to settle costs one bounded line
# each and carries one bounded trace out. The sample that STOPPED a window is
# recorded outside the cap, so truncation can never lose it.
WINDOW_TRACE_MAX_SAMPLES = 256

# How many extra silent READINGS one pass can spend re-measuring a contradicted
# floor. ONE, and not a knob: a second reading answers "was the first one
# contaminated?", and a third would only be answering the same question again
# with the same instrument. A pass whose floor is contradicted twice is telling
# the operator about the room, not about the ambient window, and the rise gate
# already reports that as `mic_not_observing`.
#
# Readings, not windows: a silent re-measure settles the same way every other
# reading does, so what it costs the watchdog is one settle, not one window.
REMEASURE_READINGS = 1

# How many extra readings the BANK CONFIRM adds to the walk's budget. One: on a
# chain that has actually settled the confirm agrees the first time it is asked,
# so one reading (about a second) is what a converging pass really costs.
#
# It is ADDED rather than taken out of the miss budget on purpose. Folding it in
# would silently cost a chain one of its allowed misses, so a chain that needed
# its whole budget to reach the band would arrive there and then have nothing
# left to confirm with — a refusal manufactured by accounting rather than by
# anything measured. A chain whose readings keep disagreeing still spends only
# the walk's own budget and then refuses; the wait is never unbounded.
BANK_CONFIRM_READINGS = 1

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
# The level never stopped moving, so no number from it may be believed. ONE slug
# for one question asked at two scales, because it IS one question: consecutive
# WINDOWS that keep disagreeing until SETTLE_TIMEOUT_S (a reading that cannot be
# believed at all), and consecutive READINGS that keep disagreeing until the
# walk's budget runs out (a level that reaches the band and then creeps out from
# under it). The refusal's detail names which scale and quotes the two figures
# that disagreed; the operator's action — the level is still moving, wait for
# the chain or look at why — is the same either way.
#
# Still a different question from `spl_level_unconverged` above, which is the
# ramp failing to REACH the band at all. That one is about where the level is;
# this one is about whether it is holding still.
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
    moment the READING began — which, for a climb reading, is the moment the
    volume step was commanded. Every window of one settling reading shares that
    origin, so a reading that took five windows to settle reads as one timeline
    rather than five that each restart at zero. That offset is read from the
    pass's OWN injected clock when the sample is processed, so it lags capture
    by at most one poll interval plus one chunk; this pass runs on one clock and
    a second time base carried on the sample would not survive the fake clock
    the tests inject.

    ``seen`` is the true finite-sample count and exceeds ``len(samples)`` only
    when :data:`WINDOW_TRACE_MAX_SAMPLES` truncated the retained series.
    ``trip`` is the sample that STOPPED the window, recorded outside that cap;
    it is ``None`` for a window that ended on its own deadline and for a clipped
    capture, whose level is meaningless by definition.

    **Not only a sample-domain stop publishes one of these.** Every refusal
    carrying a reading attaches its last window as ``ramp.stopped_window`` /
    ``stopped_window_*``, and since #2919 that includes
    :data:`REFUSE_LEVEL_UNSETTLED` — a reading whose windows never agreed. Its
    window has ``trip`` ``None`` and ended on its own deadline, so the thing to
    read there is the median against the medians the refusal's own prose names,
    not a trip that does not exist.
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
    """A refusal's last window as ``stopped_window_*`` fields on its event line.

    "The window it stopped in", which is an abandoned one for a sample-domain
    stop and a completed-but-disagreeing one for
    :data:`REFUSE_LEVEL_UNSETTLED` — the latter has no ``trip``.

    ``stopped_window_``, not ``window_``: ``seat_level_start`` already emits
    ``window_dbfs`` for the TARGET BAND converted to mic dBFS, which is a
    different concept entirely, and one prefix answering two questions is how a
    journal grep starts returning the wrong rows. This is the one owner of the
    ``stopped_window_`` prefix, so the journal's vocabulary and the receipt's
    ``ramp.stopped_window`` object cannot drift into two names for one number.
    """
    return {f"stopped_window_{key}": value for key, value in summary.items()}


def _window_phrase(summary: dict[str, Any]) -> str:
    """One sentence of a refusal's last window, for the operator's own terminal.

    Every refusal that got as far as a reading has one, so the ``trip`` clause
    is conditional rather than assumed: a sample-domain stop abandoned its
    window and names the sample that ended it, while
    :data:`REFUSE_LEVEL_UNSETTLED`'s window ran to its own deadline and has no
    trip to name — there the median is the whole content, read against the two
    medians the refusal's own detail already quotes.

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

    ``rms_dbfs`` is the median of EVERY finite sample in the window that settled
    it — no sample is dropped for being quiet, because "the speaker is still
    under the room" is evidence the step arithmetic uses. ``None`` means the
    reading produced no believable level: the mic delivered nothing finite, a
    sample-domain stop abandoned the window, or the level never settled.

    ``samples`` counts the finite samples behind that median, which is what the
    converged receipt's ``steps[].samples`` has always meant. ``windows`` is how
    many windows the reading took to settle — two when the level was already
    still, more when it was moving — and is the pass's own measurement of how
    long this chain takes to answer a step. ``trace`` is the last window, and
    on a sample-domain stop it is the window that was abandoned.
    """

    rms_dbfs: float | None
    samples: int
    trace: _WindowTrace
    windows: int = 1
    refusal: str | None = None
    detail: str | None = None


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
    profile's commissioning SPL stop (the hard-stop list's measured ceiling).
    Both are checked before the median, so the pass cannot sit at an
    over-ceiling level for the rest of a window — and because a reading that is
    still moving now takes MORE windows rather than one fixed one, a rising
    level is checked against that stop on more samples than it ever was, never
    fewer.

    ``started`` is the moment the READING began (the volume step, for a climb
    reading), and every offset on the trace is measured from it, so the windows
    of one settling reading form a single timeline. ``attempt`` is this window's
    1-based place in that reading.

    Every window — the ambient one and every climb one, stopped or not — leaves
    a :class:`_WindowTrace` on the reading and emits its per-sample series as
    ONE DEBUG line (``event=active_speaker.seat_level_window_samples``, one line
    per window rather than one per sample). ``window`` names which reading that
    line belongs to: ``"ambient"``, ``"silence"``, or the commanded volume the
    climb was sitting at. The line is built only when DEBUG is actually enabled,
    so an ordinary run pays nothing for it.
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

    Two of the three numbers behind "settled" reach here as parameters and the
    third does not, and the asymmetry is the domain's: ``agree_db`` and
    ``timeout_s`` are run-scoped and operator-overridable, resolved once in
    :func:`run_seat_level_ramp` so one pass has one answer, while
    :data:`MIC_WINDOW_S` is a fixed property of the meter with no override path.
    This function is where that constant is APPLIED — it is read here and handed
    to :func:`_window_reading`, which stays a pure function of the length it is
    given — so the receipt's ``settle_window_s`` has exactly one value to
    publish and no second writer to drift against.

    **The instrument's own stability is what "settled" means here.** There is no
    lag model and no drained transport delay: a window taken while the level is
    still moving disagrees with the one after it, so the pass simply keeps
    reading. That is what the retired fixed settle could not do — it waited a
    guessed half-second, took a median, and banked it however hard the level was
    still climbing (jts3 2026-08-24: +6.03 dB WITHIN the final window, so the
    banked frame read about 5 dB under the level that arrived — issue #2919).

    The LATER of the two agreeing windows is the reading. They are the same
    level by construction; the later one is the most recent evidence, and on a
    level with any residual climb left in it, it is the louder of the two.

    Three ways out other than agreement, and none of them banks a number:

    * a sample-domain stop or a clipped capture — returned straight from the
      window that fired it, with the window it abandoned attached;
    * a window with no finite sample in it — :data:`REFUSE_MIC_FEED_LOST`, after
      ONE window, so a dead feed never waits out the timeout;
    * ``timeout_s`` elapsed with the windows still disagreeing —
      :data:`REFUSE_LEVEL_UNSETTLED`, naming the last two and their distance.
      A reading always gets at least two windows before this can fire, because
      "two consecutive windows agree" cannot be answered with fewer.

    **What agreement does not buy, because the difference is the residual.**
    What is bounded is the RATE, never the remaining distance: banking happens
    once consecutive medians move less than ``agree_db`` per window, and what
    the level has LEFT to travel at that moment is that rate times the chain's
    own time constant. So::

        residual ~= (agree_db / MIC_WINDOW_S) x tau

    — about **1 dB per second of tau** at the shipped values, and **unbounded in
    tau**. Measured end-to-end on the synthetic first-order rig in
    ``tests/test_active_speaker_seat_level.py`` (2026-08-24, defaults): tau =
    0.81 s banks 0.42 dB low, tau = 3 s banks 2.67 dB low, tau = 5 s banks
    4.59 dB low. That last one is the size and the DIRECTION of the very defect
    #2919 closes, and tau = 3 s is inside the range :data:`SETTLE_TIMEOUT_S`
    says it covers — so this is a real operating region, not a corner. What the
    fix buys is still large and still real: the jts3 chain this was built for
    arrives in about 0.9 s, where the residual is a few tenths of a dB against
    the ~5 dB the fixed settle banked.

    **A LOW window count is not evidence of stillness.** ``windows == 2`` has
    two causes and they are opposite: a level that was genuinely still, and a
    level moving so slowly that consecutive medians never differ by
    ``agree_db`` at all. The second reads most reassuring exactly where the
    error is largest — measured on the same rig, a tau = 30 s approach settles
    in the minimum two windows while banking **19.46 dB** low. Read ``windows``
    as this chain's answer time, never as a confidence score, and read it
    against its neighbours: a climb whose readings take 2, 5, 8, 8 windows is
    describing a chain, while a slow chain that reports 2 everywhere is
    describing the bar.

    **What this residual is NOT allowed to reach: the artifact.** Everything
    above is about one reading. A reading's residual is a label error while the
    pass is running, and the per-sample commissioning stop is watching — but the
    BANKED volume outlives the pass, and nothing downstream re-checks it, so a
    residual that survived into ``seat_level_reference.json`` would be a level
    no stop ever sees again. That is why banking takes two consecutive READINGS
    that agree (:data:`BANK_CONFIRM_READINGS` and the confirm in
    :func:`_walk_to_the_band`): the second reading is a whole reading later, so
    the same bar over that longer baseline catches the creep this residual
    describes. Measured on the rig above, the bank confirm turns every
    hot-banking case into a refusal and tightens the ones that still bank.

    Narrowing the reading's own residual further would need a model of what is
    moving — the thing this pass deliberately does not have, and (per #2919)
    does not need in order to delete a wrong one. What it owes instead is
    disclosure, which is ``windows`` on every step of the receipt plus this
    paragraph.
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
                        f"{was:.1f} then {now:.1f} dB SPL ({moved_db:+.1f} dB "
                        f"apart, against a {float(agree_db):.1f} dB agreement "
                        f"bar), and the {float(timeout_s):.0f} s settle timeout "
                        "ran out; a level that has not settled is not banked"
                    ),
                )
        previous = reading.rms_dbfs


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
    AT the ceiling and never above it, 9.16 audible seconds of which one
    reading sits at the ceiling, refusal ``mic_not_observing``, household volume
    restored — 23.2 dB louder than the retired 20 dB span reached before
    refusing. An on-metal dead-mic run is on the bench checklist; these numbers
    are a model.

    Re-derived on the same model under #2919's stability wait: eight readings,
    every one settling in the minimum two windows, because a mic pinned at a
    constant is the most settled thing there is. The 0.30 s against the 8.86 s
    this said before is the fake clock's polling granularity landing differently
    across two ``MIC_WINDOW_S`` deadlines than across one of twice the length;
    the real per-reading cost of a still level — one second — did not move.
    """
    return max_rise_db < min_rise_db and at_ceiling


def walk_reading_budget(*, start_db: float, ceiling_db: float) -> int:
    """How many settled readings one climb may spend, at most.

    One at the start volume, one per bite from there to the ceiling, the misses
    the chain is allowed (:data:`MAX_MISSED_FULL_STEPS`), and the bank confirm
    (:data:`BANK_CONFIRM_READINGS`). Because the bite is a fixed fraction of the
    span, the bite count is ``ceil(1 / BITE_FRACTION)`` for every chain, so this
    number is the same whatever the hardware.

    **The single owner of that count.** :func:`_walk_to_the_band` spends it and
    :func:`_watchdog_seconds` prices it, and two writers of one number is
    exactly how a backstop starts firing before the honest refusal does — the
    failure the whole watchdog derivation exists to avoid. Adding a reading to
    the walk therefore changes the budget here, once, and the price follows.
    """
    bite = bite_db(start_db=start_db, ceiling_db=ceiling_db)
    span_db = max(0.0, float(ceiling_db) - float(start_db))
    bites = math.ceil(span_db / bite) if bite > 0.0 else 0
    return 1 + bites + MAX_MISSED_FULL_STEPS + BANK_CONFIRM_READINGS


def _watchdog_seconds(
    *, start_db: float, ceiling_db: float, settle_timeout_s: float = SETTLE_TIMEOUT_S
) -> float:
    """This pass's own worst case, priced as the readings it actually takes.

    :func:`walk_reading_budget` — every reading the climb may spend — plus the
    ONE silent re-measure a contradicted floor can cost
    (:func:`_remeasure_silence`), each priced at ``settle_timeout_s``, the most
    one reading can spend, plus :data:`WATCHDOG_SLACK_S`.

    **The budget is substantially reachable, and that is why it is priced this
    way.** A settled reading normally costs two windows, about a second — but
    agreement is tested BEFORE the timeout, so a window landing at the bound
    settles rather than refusing, and a chain that keeps disagreeing until just
    under the bar spends the whole timeout on a reading that CONVERGES, and a
    synthetic chain doing exactly that is pinned in
    ``tests/test_active_speaker_seat_level.py``. A budget priced at "a second a
    reading" would have fired the backstop on that healthy pass and reported
    ``seat_level_watchdog_expired`` — a slug that names nothing — instead of
    letting it finish. Pricing the ceiling is what keeps this a backstop against
    a wedged awaitable rather than a governor on the walk's shape.

    Scope, because the budget is wider than the thing it guards: the ambient read
    happens BEFORE the timeout scope opens (its result is what the pass logs and
    what the guards read), so this budget covers the tone-playing walk only. The
    reading count here is exactly what the walk can spend inside that scope —
    :func:`walk_reading_budget`, the one owner of that number, plus the one
    silent re-measure — so the margin is :data:`WATCHDOG_SLACK_S` and nothing
    else. A feed that never returns during the ambient read is not bounded here
    — nothing has been mutated at that point (no tone, no latch, fader unmoved)
    and the operator's interrupt is the stop. Priced against the ACTUAL start
    and the ACTUAL bite, so it cannot repeat the retired kernel's mistake of
    budgeting a continuous climb for a walk that does not climb continuously.
    """
    readings = (
        walk_reading_budget(start_db=start_db, ceiling_db=ceiling_db)
        + REMEASURE_READINGS
    )
    return readings * float(settle_timeout_s) + WATCHDOG_SLACK_S


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

    Persists a reference only on a reading inside the band that rose clear of a
    floor THIS PASS MEASURED IN SILENCE — the first ambient window, or the one
    re-measured mid-climb when a reading contradicted it. A floor derived from a
    reading taken while the tone played would not answer the question this gate
    asks: a mic that is not observing the speaker still hears a room, and
    re-basing its floor onto its own quietest climb reading would let a later,
    louder wander clear the bar and bank a reference nothing produced.
    """
    window_low_dbfs, window_high_dbfs = validate_seat_level_window(
        target=target, sensitivity=sensitivity
    )
    ceiling_db = seat_level_ceiling_db(max_main_volume_db)
    min_rise_db = bounded_env_float(
        "JASPER_SEAT_LEVEL_MIN_RISE_DB", MIC_RESPONSE_MIN_RISE_DB, lo=1.0, hi=20.0
    )
    # The two halves of "settled", read once here so one pass has one answer:
    # how close two consecutive windows must be, and how long a reading may
    # spend proving it. Bounded, and disclosed on the receipt and the start
    # event, because widening the first one lowers the bar for banking.
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


async def _remeasure_silence(
    *,
    tone: "asyncio.Future[Any]",
    sensitivity: MicSensitivity,
    spl_ceiling_db_spl: float,
    play_continuous_tone: Callable[[], Awaitable[Any]],
    cancel_tone: Callable[[], None],
    next_samples: SampleSource,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    session_id: str,
    agree_db: float,
    settle_timeout_s: float,
) -> tuple[_Reading, "asyncio.Future[Any]"]:
    """Stop the tone, measure the room again, start the tone again.

    **Why the tone has to actually stop.** The floor this returns feeds the
    guards that decide whether the mic is observing the speaker and whether a
    reading may bank a reference. Those questions are only answerable against a
    level measured while the speaker is SILENT — that is the anti-coincidence
    property that makes "rise" mean "responded to the speaker" rather than
    "happened to be louder than a number we picked". A floor taken from a
    reading made while the tone played has no such property: a mic on the wrong
    card hears a room that wanders, and re-basing onto its own quietest wander
    would let a later, louder wander clear the bar.

    **What this window is NOT, stated because the difference is the residual.**
    It is anti-coincident with the SPEAKER — nothing this pass drives is playing
    while it is measured — but it is **not independent of the trigger**. It is
    taken BECAUSE a reading landed low, about a second later, and room lulls
    autocorrelate over seconds; a lull still present when the silent window runs
    hands back the same low level. So the observing/banking guard fails on
    ``P(the first ambient window was low)`` **plus** ``P(the first window was
    high AND the re-measure lands low inside the same lull)``, where before this
    pass existed it failed on the first term alone. **The second term is added,
    not traded**: the first is untouched by this pass, so the BANKING guard is
    marginally worse than it was, and the honest ledger is two-sided. What this
    pass buys is on the other error type — a contaminated ambient window used to
    disqualify GOOD readings for the rest of a run (jts3 run 87, the defect this
    exists for) and no longer does. Narrow cost, real benefit, different failure
    modes.

    The worked known-bad case — ambient window 66, a mic that never responds, a
    lull holding 60 across both windows, a later 67 clearing the 6 dB bar and
    BANKING — is pinned as a documented limitation in
    ``tests/test_active_speaker_seat_level.py``, and the pre-#2918 rule REFUSES
    that same case, which is asserted there rather than claimed
    (``test_the_lull_residual_is_INTRODUCED_by_the_re_measure_not_inherited``).
    Closing it needs a separator
    between "the level moved" and "the level moved BECAUSE of the speaker" — a
    response test, deliberately not built here: at these reading counts it is
    spoofable, and the doctrine's no-nannies rule (§5) says a gate earns its
    place by naming a damage mechanism, which measurement integrity is not. The
    receipt's ``ambient_remeasured`` and the event line's
    ``remeasured_delta_db`` are the operator's tell instead.

    Bounded and unconditional-once: exactly one extra
    :func:`_settle_reading` — normally two windows, about a second — priced into
    :func:`_watchdog_seconds`, with no retry and no lag to tune. The stimulus's
    own decay tail after ``cancel_tone`` needs no drained delay to absorb it:
    the tail IS a moving level, so the window that catches it disagrees with the
    one after it and the reading simply keeps going until the room is still. A
    room that never goes still refuses (:data:`REFUSE_LEVEL_UNSETTLED`) rather
    than handing the guards a floor measured off a decaying speaker.

    The fader is left exactly where the climb had it: the tone is off, so the
    speaker is silent whatever the volume says, and moving it would mean two
    more writes to reconcile on a path whose whole job is to observe. The cost
    of that choice is that both edges here are ABRUPT — the stimulus stops and
    restarts at a measurement level with no fade, unlike the pass's own
    end-of-run :func:`_fade_and_stop` — and #2919's settle lengthens the silence
    they bracket from a fixed second to as much as ``settle_timeout_s``. That is
    pre-existing behaviour and not a hearing-safety question (every sample is
    still under the commissioning stop), but it is a real one:
    https://github.com/jaspercurry/JTS/issues/2929.

    Returns the reading and the tone future the caller must now hold, because the
    old one is finished once its player has been cancelled and the caller's own
    teardown has to cancel the tone that is actually playing. When the silent
    window itself failed the stimulus is NOT restarted: the pass is about to
    refuse, and starting it again would put an audible blip in the room for
    exactly as long as the fade takes to kill it.
    """
    cancel_tone()
    tone.cancel()
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
    return reading, asyncio.ensure_future(play_continuous_tone())


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
    volume_db = start_db
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
    # The most recent pair of qualifying readings that DISAGREED, in dB SPL, for
    # the refusal that reports a level which reached the band and would not hold
    # still there. Distinct from the tail's ordinary never-arrived refusal.
    disagreed: tuple[float, float] | None = None
    # The floor every rise is measured against. ALWAYS a window measured with
    # the speaker silent: the pre-tone ambient, or -- once, when a reading
    # contradicts it -- a second silent window measured mid-climb. Never a
    # reading taken while the tone played.
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
        the receipt as ``ramp.stopped_window``, the event line as
        ``stopped_window_*``, and the
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
                )
            observed_db_spl = sensitivity.db_spl_from_dbfs(reading.rms_dbfs)
            # A reading CONTRADICTS the floor when it lands below it: the tone is
            # playing, so this reading is the room plus the speaker and cannot be
            # quieter than the room. One of the two windows is wrong, and the
            # honest instrument answer is to measure the silence again rather
            # than to believe the reading -- see `_remeasure_silence`.
            if remeasured_dbfs is None and reading.rms_dbfs < floor_dbfs:
                silent, tone = await _remeasure_silence(
                    tone=tone,
                    sensitivity=sensitivity,
                    spl_ceiling_db_spl=spl_ceiling_db_spl,
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
                    # How far the second silent window moved from the first, and
                    # what the triggering reading's rise became against it. Two
                    # numbers because they answer the two questions this event
                    # exists to make greppable, and both are already in hand:
                    #
                    # A LARGE NEGATIVE delta is the residual's signature — the
                    # silent window agreed with the low reading that triggered
                    # it, which is what a room lull persisting across the ~1 s
                    # between them looks like, and is indistinguishable here
                    # from a mic that never responds (see `_remeasure_silence`).
                    # A POSITIVE delta means the floor went UP, so the trigger
                    # reading and everything under the new floor publish
                    # NEGATIVE rises: conservative for banking, but worth
                    # saying rather than leaving to be derived from two other
                    # fields on the line.
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
                # agreed — this chain's own answer time, measured every run
                # instead of guessed once (#2919). NOT a confidence score:
                # `windows == 2` means EITHER the level was already still OR it
                # was moving too slowly to be caught at the agreement bar, and
                # the second case is where the banked level is furthest out
                # (see `_settle_reading` on the residual). Read it against its
                # neighbours, not on its own.
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
            # sometimes (a constant that happens to sit there; a wrong-card mic
            # hearing a room that wanders through it), and the only thing
            # separating that from a real answer is whether the reading rose
            # above a floor the speaker was NOT contributing to. Measure the
            # floor while the tone plays and the separation is gone: the mic's
            # own quietest wander becomes the bar its loudest wander clears.
            in_band = target.low_db_spl <= observed_db_spl <= target.high_db_spl
            if in_band and rise_db >= min_rise_db:
                # THE BANK CONFIRM: a reference is banked only when two
                # consecutive READINGS agree, which is the same rule a reading
                # itself is settled by, applied one level up.
                #
                # Why it has to exist at this level too: a reading settles when
                # two windows agree, which bounds the RATE, not the distance —
                # so a slowly-creeping chain settles honestly and still banks a
                # level it is going to leave behind (`_settle_reading` states
                # the residual and its measurements). The per-sample
                # commissioning stop cannot catch that: it guards the pass,
                # while the number outlives it, and every later session consumes
                # the banked volume with no equivalent stop. Two readings are
                # separated by at least two windows, so the same bar over that
                # longer baseline is a strictly stricter test of creep — which
                # is exactly the failure it is here for.
                #
                # Nothing speculative is refused. A confirm that agrees banks
                # immediately; only a MEASURED disagreement costs anything, and
                # what it costs is another reading inside the same budget.
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
                    disagreed = (
                        sensitivity.db_spl_from_dbfs(candidate_dbfs),
                        observed_db_spl,
                    )
                    log_event(
                        logger,
                        "active_speaker.seat_level_bank_unconfirmed",
                        level=logging.WARNING,
                        session=session_id,
                        at_db=f"{volume_db:.2f}",
                        was_db_spl=f"{disagreed[0]:.2f}",
                        now_db_spl=f"{disagreed[1]:.2f}",
                        moved_db=f"{disagreed[1] - disagreed[0]:+.2f}",
                        agree_db=f"{agree_db:.2f}",
                    )
                candidate_dbfs = reading.rms_dbfs
                # The step that landed here made its prediction and was right —
                # it reached the band — so it must not also be charged a miss on
                # the way back round when the confirm disagrees. Before the
                # confirm this branch always returned, so the flag was never
                # read again; now it is, and a step that hit the band would
                # otherwise spend the budget meant for steps that MISSED it.
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
                # The rise is measured against the floor this pass last
                # measured IN SILENCE, so that is the number this sentence has
                # to name. Saying "above the {first window} dB SPL room" beside
                # a rise computed from a RE-MEASURED floor would be two
                # different rooms in one sentence.
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
            await set_main_volume_db(volume_db)

        if disagreed is not None:
            # It DID reach the band -- repeatedly -- and would not hold still
            # there, which is a different sentence from "never arrived" and
            # sends the operator somewhere else entirely.
            was, now = disagreed
            return refuse(
                REFUSE_LEVEL_UNSETTLED,
                f"the level reached the band and would not hold still in it: "
                f"the last two readings at {volume_db:.2f} dB read {was:.1f} "
                f"then {now:.1f} dB SPL ({now - was:+.1f} dB apart, against a "
                f"{agree_db:.1f} dB agreement bar) and the "
                f"{max_readings}-reading budget ran out; a reference is banked "
                "only from two readings that agree",
                was_db_spl=f"{was:.1f}",
                now_db_spl=f"{now:.1f}",
                agree_db=f"{agree_db:.2f}",
            )
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
