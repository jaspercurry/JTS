# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which screens an anchor capture must clear, and in what order (#2291 5a-vii).

The eighth sibling, and the last of the capture-consuming decision layers.
:mod:`.spatial` owns the ladders of the three phases a household *walks* — the
cloud positions, the lateral poses, the entry baseline.  This one owns the
ladders of the three it *sits still* for: CHECK, MEASURE and VERIFY.  Between
them every capture the conductor consumes is now screened by a stated ladder
rather than by a method reaching into the object it hangs off.

**This module DECIDES; it does not act** — the split :mod:`.accountability` set
and :mod:`.admission` restated.  A ladder answers "is this recording evidence
about the speaker, and if not, which finding is the honest one".  Everything a
finding then *causes* stays with the conductor: recomposing the MEASURE program
for a silent auto-retry, banking the gain plan and ambient report, stashing the
analysis for a deferred fit, publishing CHECK evidence across the seam, raising
:class:`CrossoverV2FlowError` on a candidate-less analysis, and constructing the
``PhaseVerdict`` the phone reads.  A pure ladder asked the same question twice
answers the same way, which is what makes a replayed capture safe to re-screen.

**No household vocabulary lives here.**  A refusal leaves as a *kind*; the flow
maps it through ``SCREEN_KIND_REASONS`` and renders the sentence from
``REASON_REGISTRY``.  That is the same boundary :mod:`.spatial` draws, and the
reason this module can exist at all — see the note on the vocabulary below.

Three deliberate shapes, because each looks like something this module should
have done differently:

* **Two MEASURE facts arrive as callables, not values.**  ``sweep_schedule_ok``
  and ``alignment_delay_plausible`` are asked ONLY once the rungs above them
  pass, and both would change observable behaviour if resolved eagerly: the
  first reaches ``program_for_phase``, which *raises* when MEASURE has no
  composed program, and hoisting that raise above the glitch rung would move a
  failure the shipped flow reports later.  So they are ports, invoked exactly
  where the conductor invoked them — the call-count reason :mod:`.admission`
  made ``apply_failure_code`` a callable.  Every other fact is a value, because
  every other fact is already in hand.
* **The MEASURE ladder returns a directive, not just a kind.**  Three of its
  rungs re-arm the phase and one of those backs the level off, and the guard
  label two rungs set is the field telemetry tells shared codes apart by.  Those
  are facts about *which rung fired*, so the rung is where they are decided; the
  re-arm itself is the conductor's.
* **The ripple reservation is a rung that does not refuse.**  Owner ruling
  #2087 converted it from a gate to a disclosure, so it lives here as a
  predicate (:func:`ripple_reservation_due`) and the conductor does the
  disclosing.  Keeping it beside the refusals is the point: it is one more
  thing this ladder decides about a capture, and a reader who finds only the
  refusals here would conclude MEASURE has no other judgement to make.

**On the household vocabulary, and why it did not have to move.**
:mod:`.admission` recorded that the settle path could not leave without
"relocating the household vocabulary or inventing ports whose only purpose is
to dodge a type import".  That is true of moving a method *whole* — it returns
``PhaseVerdict``, which binds ``REASON_REGISTRY``, ``reason_message`` and
``TRANSIENT_AUTO_RETRY_CODES`` through ``to_relay_dict``.  It is not true of
the decision/act split this package actually uses: a ladder that answers with a
kind never touches the carrier, and the conductor that owns the carrier builds
it.  So the vocabulary stays where its consumers are and this organ still
leaves.  The full reasoning, and what was scoped out with it, is on the PR.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.audio_measurement.program_analysis import INTEGRITY_CHECK_SWEEP_HEARD

from .spatial import (
    SCREEN_CAPTURE_GLITCH,
    SCREEN_CLIPPED,
    SCREEN_KINDS,
    SCREEN_LINEARITY_FAILED,
    SCREEN_LOCATE_FAILED,
    SCREEN_PILOT_LEVEL_COLLAPSE,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

__all__ = [
    "ANCHOR_SCREEN_KINDS",
    "CAPTURE_SCREEN_KINDS",
    "SCREEN_ALIGNMENT_UNRESOLVED",
    "SCREEN_CHANNEL_MAP_MISMATCH",
    "SCREEN_LOW_ALIGNMENT_CONFIDENCE",
    "SCREEN_NOISY_ROOM_LINEARITY",
    "SCREEN_SNR_FLOOR",
    "CheckScreens",
    "MeasureScreen",
    "MeasureScreens",
    "VerifyIntegrityScreen",
    "check_screens",
    "measure_screens",
    "ripple_reservation_due",
    "verify_integrity_screens",
]


# --------------------------------------------------------------------------- #
# the kinds this module adds to the screen vocabulary
# --------------------------------------------------------------------------- #
#
# :mod:`.spatial` declared the five kinds the walked phases produce. The three
# anchor phases produce those five plus these five, and they are declared HERE
# rather than appended there because a completeness test over one shared set
# would then assert that a cloud position can report a channel-map mismatch —
# which it cannot, since it never plays the per-driver program that establishes
# one. Two owners, one union (:data:`CAPTURE_SCREEN_KINDS`), which is what the
# flow's registry is checked against.

#: CHECK heard the drivers on the wrong outputs.
SCREEN_CHANNEL_MAP_MISMATCH = "channel_map_mismatch"
#: The capture cleared its gates but sits too close to the room's own floor.
SCREEN_SNR_FLOOR = "snr_floor"
#: The curve bent, and this capture's OWN ambient evidence says the room did it.
SCREEN_NOISY_ROOM_LINEARITY = "noisy_room_linearity"
#: An alignment estimate exists and did not resolve inside its search window.
SCREEN_ALIGNMENT_UNRESOLVED = "alignment_unresolved"
#: An alignment estimate exists and is not trustworthy enough to build on.
SCREEN_LOW_ALIGNMENT_CONFIDENCE = "low_alignment_confidence"

#: The five kinds only an anchor phase can produce.
ANCHOR_SCREEN_KINDS = frozenset({
    SCREEN_CHANNEL_MAP_MISMATCH,
    SCREEN_SNR_FLOOR,
    SCREEN_NOISY_ROOM_LINEARITY,
    SCREEN_ALIGNMENT_UNRESOLVED,
    SCREEN_LOW_ALIGNMENT_CONFIDENCE,
})

#: Every kind any capture ladder in this package can return.  The flow's
#: ``SCREEN_KIND_REASONS`` is checked for completeness against THIS set, so a
#: new rung in either owner cannot ship without a household sentence.
CAPTURE_SCREEN_KINDS = SCREEN_KINDS | ANCHOR_SCREEN_KINDS


# --------------------------------------------------------------------------- #
# CHECK — is this room, at this level, measurable at all
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckScreens:
    """Every fact CHECK's ladder reads, stated by the caller.

    All fields are required, the rule the :class:`~.spatial.CaptureScreens`
    review set: a permissive default is how a caller that forgot to establish a
    fact gets a pass instead of an error.

    ``gain_plan_present`` and ``gain_plan_snr_floor_ok`` are two fields rather
    than one optional because the ladder reads them at two different rungs and
    means different things by them — the linearity rung asks "did the gain
    solve ALREADY judge this room noisy", which is only answerable when a plan
    exists, while the final rung refuses an absent plan outright.
    """

    stimulus_located: bool
    channel_map_ok: bool | None
    pilot_snr_ok: bool | None
    linearity_ok: bool | None
    gain_plan_present: bool
    gain_plan_snr_floor_ok: bool


def check_screens(screens: CheckScreens) -> str | None:
    """CHECK's ladder: the refusal kind, or ``None`` to accept.

    Order is load-bearing and unchanged from the shipped verdict:

    1. **Stimulus located.**  A capture whose stimulus was never found is not
       evidence about anything.
    2. **Channel map.**  Explicit ``False`` only — ``None`` is no evidence.
    3. **Pilot SNR.**  Ahead of linearity for issue #1838's reason: below the
       floor the ambient-subtracted two-pilot delta is not evidence either way
       (``linearity_ok`` is already ``None``), so the honest finding is the
       room and the level, never the phone's microphone.
    4. **Linearity.**  W6.12 — do not blame the microphone when the room was
       the cause.  CHECK is the one phase that can tell, because its gain solve
       already produced a band-resolved ambient verdict against THIS capture;
       when that verdict says the room was noisy, say so.
    5. **The gain solve itself.**  No plan, or a plan that could not clear the
       floor, and there is nothing for MEASURE to play.
    """
    if not screens.stimulus_located:
        return SCREEN_LOCATE_FAILED
    if screens.channel_map_ok is False:
        return SCREEN_CHANNEL_MAP_MISMATCH
    if screens.pilot_snr_ok is False:
        return SCREEN_SNR_FLOOR
    if screens.linearity_ok is False:
        if screens.gain_plan_present and not screens.gain_plan_snr_floor_ok:
            return SCREEN_NOISY_ROOM_LINEARITY
        return SCREEN_LINEARITY_FAILED
    if not screens.gain_plan_present or not screens.gain_plan_snr_floor_ok:
        return SCREEN_SNR_FLOOR
    return None


# --------------------------------------------------------------------------- #
# MEASURE — is this the per-driver evidence a candidate may be built on
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MeasureScreen:
    """One MEASURE rung's finding, and what it asks the conductor to do.

    ``guard`` is the telemetry label that tells two rungs sharing one household
    code apart (§5.2's "never a new user-facing code for a capture-glitch
    class" convention means the journal's ``guard=`` field is the only way to
    know which check fired).  Empty when the kind alone is the whole finding.

    ``rearm`` asks for the silent auto-retry, and ``rearm_backoff_db`` is the
    level it should come back at — ``0.0`` reproducing the conductor's
    no-argument call exactly.
    """

    kind: str
    guard: str = ""
    rearm: bool = False
    rearm_backoff_db: float = 0.0


@dataclass(frozen=True)
class MeasureScreens:
    """Every fact MEASURE's ladder reads.

    Two are **callables**, not values, and the module docstring says why: they
    are asked only once the rungs above them pass, and resolving them eagerly
    would move an observable failure.  The rest are plain answers already in
    the caller's hand.

    The four alignment fields encode the shipped three-rung alignment ladder
    without importing its thresholds.  ``alignment_present`` gates all three
    (a trims-only candidate has no estimate and skips them entirely);
    ``alignment_status_ok`` is the resolve verdict; ``alignment_confidence_ok``
    is the trust floor; ``delay_physically_plausible`` is the backstop, which
    the shipped ladder asks ONLY of a resolved estimate.
    """

    stimulus_located: bool
    pilot_snr_ok: bool | None
    sweep_locate_confidence_ok: bool
    glitch_detected: bool
    sweep_schedule_ok: Callable[[], bool]
    any_sweep_clipped: bool
    linearity_ok: bool | None
    alignment_present: bool
    alignment_status_ok: bool
    alignment_confidence_ok: bool
    delay_physically_plausible: Callable[[], bool]


def measure_screens(
    screens: MeasureScreens, *, clip_retry_backoff_db: float
) -> MeasureScreen | None:
    """MEASURE's ladder: the finding and its directive, or ``None`` to accept.

    The order is the whole point of this function, and two rungs of it were
    bought with a live incident.

    **"Too quiet" runs before "glitched" (D3, issue #1838).**  A capture nobody
    could hear produces the same symptoms as a spliced one — the locator lands
    the sweeps wrong, the residual blows past its ceiling, and the glitch
    signal fires on noise.  Until #1838 the glitch rung sat second and
    swallowed both level verdicts, so a session whose MEASURE played 33 dB
    below flat was told its capture had glitched and silently re-armed the same
    unwinnable level until it timed out.  Low SNR CAUSES the glitch signal, so
    the level verdicts have to be asked first or the reported cause is never
    the real one.

    **Neither level rung re-arms.**  Re-running an inaudible measurement at the
    same level cannot succeed, and both kinds already carry a household action
    that can.

    The three transient rungs (glitch, schedule, clipped) DO re-arm, silently;
    the clipped one comes back quieter.  ``sweep_schedule`` is the 2026-07-22
    xrun detector — a uniform whole-capture shift the repeat-pair drift check
    is structurally blind to — and it shares the glitch kind deliberately, with
    ``guard`` as the discriminator.

    ``clip_retry_backoff_db`` is stated rather than imported: it is the flow's
    policy number, and "inputs are stated, never reached for" is the rule
    :mod:`.priors` set for exactly this.
    """
    if not screens.stimulus_located:
        return MeasureScreen(SCREEN_LOCATE_FAILED)
    if screens.pilot_snr_ok is False:
        return MeasureScreen(SCREEN_PILOT_LEVEL_COLLAPSE)
    if not screens.sweep_locate_confidence_ok:
        return MeasureScreen(SCREEN_LOCATE_FAILED, guard="sweep_locate_confidence")
    if screens.glitch_detected:
        return MeasureScreen(SCREEN_CAPTURE_GLITCH, rearm=True)
    if not screens.sweep_schedule_ok():
        return MeasureScreen(
            SCREEN_CAPTURE_GLITCH, guard="sweep_schedule", rearm=True,
        )
    if screens.any_sweep_clipped:
        return MeasureScreen(
            SCREEN_CLIPPED, rearm=True, rearm_backoff_db=clip_retry_backoff_db,
        )
    if screens.linearity_ok is False:
        return MeasureScreen(SCREEN_LINEARITY_FAILED)
    if screens.alignment_present and not screens.alignment_status_ok:
        return MeasureScreen(SCREEN_ALIGNMENT_UNRESOLVED)
    if screens.alignment_present and not screens.alignment_confidence_ok:
        return MeasureScreen(SCREEN_LOW_ALIGNMENT_CONFIDENCE)
    if (
        screens.alignment_present
        and screens.alignment_status_ok
        and not screens.delay_physically_plausible()
    ):
        return MeasureScreen(SCREEN_LOW_ALIGNMENT_CONFIDENCE)
    return None


def ripple_reservation_due(
    *,
    predicted_ripple_db: float,
    has_alignment: bool,
    disclosure_threshold_db: float,
) -> bool:
    """Does this accepted MEASURE owe the household a ripple reservation?

    **This decides a disclosure, never a refusal** (owner ruling 2026-08-03,
    issue #2087).  A predicted ripple above the threshold says the two branches
    sum less coherently in this room than the calibration corpus did; the
    capture is still accepted, and what changes is what the household is TOLD —
    not what is built, fitted, gated or applied.

    The caller establishes that a candidate EXISTS — it has to, to read the
    ripple off it — and this function owns the other half of the shipped skip:
    without an alignment estimate (a trims-only path) there is no reservation
    to make, the same condition the converted gate carried.
    """
    if not has_alignment:
        return False
    return predicted_ripple_db > disclosure_threshold_db


# --------------------------------------------------------------------------- #
# VERIFY — is this replay evidence at all, before anything is graded from it
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerifyIntegrityScreen:
    """One VERIFY integrity finding, with the payload its screen needs.

    ``integrity_payload`` is set only on the capture-integrity arm — the same
    shape :class:`~.spatial.EntryBaselineScreen` carries, and for the same
    reason: the household screen wants the record beside the code.  The other
    arms carry none, because the kind is the whole finding.
    """

    kind: str
    integrity_payload: Mapping[str, Any] | None = None


def verify_integrity_screens(
    analysis: "ProgramAnalysis", *, stimulus_located: bool
) -> VerifyIntegrityScreen | None:
    """VERIFY's pre-grade ladder: a refusal, or ``None`` to go on grading.

    Everything below this in the shipped verdict — the gate-comparability rule,
    G3's pilot-transfer step, the tracking-max comparison — reads conductor
    state that outlives one capture, so it stays with the conductor.  What
    leaves is the part that asks only about THIS recording, and it runs first
    for the reason MEASURE puts the same class of check first: a spliced or
    clipped recording is not evidence about the speaker, so no verdict drawn
    from it is worth reporting.

    **The one difference from** :func:`~.spatial.entry_baseline_screens`, which
    reads as the same ladder: an **absent** integrity record is
    no-evidence-and-continue here, and UNUSABLE there.  ``None`` is the
    pre-#1971 analysis shape and means no evidence — the same convention
    ``linearity_ok`` and ``pilot_snr_ok`` use — and it is not a silent pass,
    because the diagnostic prints ``integrity=unavailable`` for it, distinct
    from ``integrity=ok``.  The entry baseline fails closed on the same input
    because it exists ONLY to be compared, and a before-side nobody graded
    cannot carry a before→after claim.

    Two kinds out of one record, because the two failures need different
    household actions and #1838's D3 is explicit they must not share one: a
    sweep nobody could hear is a level/mic problem re-running cannot fix, while
    a spliced or clipped timeline is the transient capture-glitch class §5.2
    routes to the shared code.

    ``analysis`` arrives whole, and ``stimulus_located`` separately, for the
    split :func:`~.spatial.entry_baseline_screens` documents: this ladder
    CONSUMES the integrity record rather than testing a fact about it, while
    ``stimulus_located`` is the answer of a flow-side predicate whose owner
    stays there because MEASURE's and CHECK's verdicts share it.
    """
    if not stimulus_located:
        return VerifyIntegrityScreen(SCREEN_LOCATE_FAILED)
    if analysis.pilot_snr_ok is False:
        return VerifyIntegrityScreen(SCREEN_PILOT_LEVEL_COLLAPSE)
    integrity = analysis.capture_integrity
    if integrity is not None and integrity.failed:
        payload = {"capture_integrity": integrity.to_dict()}
        if INTEGRITY_CHECK_SWEEP_HEARD in integrity.failed:
            return VerifyIntegrityScreen(
                SCREEN_LOCATE_FAILED, integrity_payload=payload,
            )
        return VerifyIntegrityScreen(
            SCREEN_CAPTURE_GLITCH, integrity_payload=payload,
        )
    if analysis.linearity_ok is False:
        return VerifyIntegrityScreen(SCREEN_LINEARITY_FAILED)
    return None
