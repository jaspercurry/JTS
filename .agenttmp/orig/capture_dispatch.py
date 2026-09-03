# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which screens an anchor capture must clear, and in what order (#2291 5a-vii).

The last of the capture-consuming decision layers.
:mod:`.spatial` owns the ladders of the three phases a household *walks* — the
cloud positions, the lateral poses, the entry baseline.  This one owns the
ladders of the three it *sits still* for: CHECK, MEASURE and VERIFY.  Between
them every capture the session consumes is now screened by a stated ladder
rather than by a method reaching into the object it hangs off.

**This module DECIDES; it does not act** — the split :mod:`.accountability` set
and :mod:`.admission` restated.  A ladder answers "is this recording evidence
about the speaker, and if not, which finding is the honest one".  Everything a
finding then *causes* stays with the session: recomposing the MEASURE program
for a silent auto-retry, banking the gain plan and ambient report, stashing the
analysis for a deferred fit, publishing CHECK evidence across the seam, raising
:class:`CrossoverV2FlowError` on a candidate-less analysis, and constructing the
``PhaseVerdict`` the phone reads.  A pure ladder asked the same question twice
answers the same way, which is what makes a replayed capture safe to re-screen.

**No household vocabulary lives here.**  A refusal leaves as a *kind*;
:mod:`.refusal_copy` maps it through ``SCREEN_KIND_REASONS`` and renders the
sentence from ``REASON_REGISTRY``.  That is the same boundary :mod:`.spatial`
draws, and the reason this module can exist at all — see the note on the
vocabulary below.

Three deliberate shapes, because each looks like something this module should
have done differently:

* **Two MEASURE facts arrive as callables, not values.**  ``sweep_schedule_ok``
  and ``alignment_delay_plausible`` are asked ONLY once the rungs above them
  pass, and both would change observable behaviour if resolved eagerly: the
  first reaches ``program_for_phase``, which *raises* when MEASURE has no
  composed program, and hoisting that raise above the glitch rung would move a
  failure the shipped flow reports later.  So they are ports, invoked exactly
  where the session invokes them — the call-count reason :mod:`.admission`
  made ``apply_failure_code`` a callable.  Every other fact is a value, because
  every other fact is already in hand.
* **The MEASURE ladder returns a directive, not just a kind.**  Three of its
  rungs re-arm the phase and one of those backs the level off, and the guard
  label two rungs set is the field telemetry tells shared codes apart by.  Those
  are facts about *which rung fired*, so the rung is where they are decided; the
  re-arm itself is the session's.
* **The ripple reservation is a rung that does not refuse.**  Owner ruling
  #2087 converted it from a gate to a disclosure, so it lives here as a
  predicate (:func:`ripple_reservation_due`) and the session does the
  disclosing.  Keeping it beside the refusals is the point: it is one more
  thing this ladder decides about a capture, and a reader who finds only the
  refusals here would conclude MEASURE has no other judgement to make.

**On the household vocabulary.**  This note used to argue that the vocabulary
did not have to move.  #2291 Phase 5c-ii moved it, to :mod:`.refusal_copy` — on a
fact that changed (the conductor dissolved and its spine landed in this
package, which cannot import the flow), not on that argument having been wrong.
The reasoning lives in that module's docstring and is not restated here.  What
is unchanged is the boundary above: a ladder still answers with a kind and still
never touches the carrier.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.audio_measurement.program import KIND_SWEEP, STIMULUS_KINDS
from jasper.audio_measurement.program_analysis import (
    INTEGRITY_CHECK_SWEEP_HEARD,
    channel_map_isolation_db,
)

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
    "LOCATE_MIN_CONFIDENCE",
    "SCREEN_ALIGNMENT_UNRESOLVED",
    "SCREEN_ANCHOR_AMBIGUOUS",
    "SCREEN_CHANNEL_MAP_MISMATCH",
    "SCREEN_DELAY_IMPLAUSIBLE",
    "SCREEN_NOISY_ROOM_LINEARITY",
    "SCREEN_SNR_FLOOR",
    "SWEEP_LOCATE_CONFIDENCE_FLOOR",
    "SWEEP_SCHEDULE_RESIDUAL_CEILING_MS",
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
#: The capture's timeline could not be attributed to the program it played.
#: Ahead of the one above in CHECK's ladder on purpose (issue #2644): a
#: mis-anchored capture reads every driver's window one pilot spacing from where
#: that driver actually played, which is how a correctly-wired speaker produced
#: a confident-looking "the drivers played out of order".
SCREEN_ANCHOR_AMBIGUOUS = "anchor_ambiguous"
#: The capture cleared its gates but sits too close to the room's own floor.
SCREEN_SNR_FLOOR = "snr_floor"
#: The curve bent, and this capture's OWN ambient evidence says the room did it.
SCREEN_NOISY_ROOM_LINEARITY = "noisy_room_linearity"
#: An alignment estimate exists and did not resolve inside its search window.
SCREEN_ALIGNMENT_UNRESOLVED = "alignment_unresolved"
#: An alignment estimate resolved to a delay physics rules out — the GCC
#: estimator returning a CONFIDENTLY WRONG lag, which is a measured failure
#: mode rather than a prior (a hardware run reported a confident −631 us
#: against a declared [50, 300] us search bound).
#:
#: **Its own kind since the nanny burn-down, and that split is the point.** It
#: shared ``low_alignment_confidence`` with the 0.6 GCC trust floor, so one
#: household sentence covered both — and when the floor was demoted this
#: rejection would have lost its voice with it. A physics fact and a prior are
#: different answers and now say different things.
SCREEN_DELAY_IMPLAUSIBLE = "delay_implausible"

#: The six kinds only an anchor phase can produce.
ANCHOR_SCREEN_KINDS = frozenset({
    SCREEN_ANCHOR_AMBIGUOUS,
    SCREEN_CHANNEL_MAP_MISMATCH,
    SCREEN_SNR_FLOOR,
    SCREEN_NOISY_ROOM_LINEARITY,
    SCREEN_ALIGNMENT_UNRESOLVED,
    SCREEN_DELAY_IMPLAUSIBLE,
})

#: Every kind any capture ladder in this package can return.
#: :mod:`.refusal_copy`'s ``SCREEN_KIND_REASONS`` (which the flow only
#: re-exports) is checked for completeness against THIS set, so a new rung
#: in either owner cannot ship without a household sentence.
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
    anchor_ambiguous: bool
    channel_map_ok: bool | None
    pilot_snr_ok: bool | None
    linearity_ok: bool | None
    gain_plan_present: bool
    gain_plan_snr_floor_ok: bool


def check_screens(screens: CheckScreens) -> str | None:
    """CHECK's ladder: the refusal kind, or ``None`` to accept.

    Order is load-bearing and unchanged from the shipped verdict apart from
    rung 2, which #2644 inserted ABOVE the channel map for the reason stated
    there:

    1. **Stimulus located.**  A capture whose stimulus was never found is not
       evidence about anything.
    2. **Anchor attributed.**  A capture whose timeline the analyzer could not
       pin is not evidence about WHICH driver played WHAT, and every rung below
       reads per-driver windows.  It sits above the channel map specifically
       because the channel map is the rung that turns a slid window into a
       household instruction to rewire a correctly-wired speaker (issue #2644),
       and it is retriable because re-recording is exactly what fixes it.
    3. **Channel map.**  Explicit ``False`` only — ``None`` is no evidence.
    4. **Pilot SNR.**  Ahead of linearity for issue #1838's reason: below the
       floor the ambient-subtracted two-pilot delta is not evidence either way
       (``linearity_ok`` is already ``None``), so the honest finding is the
       room and the level, never the phone's microphone.
    5. **Linearity.**  W6.12 — do not blame the microphone when the room was
       the cause.  CHECK is the one phase that can tell, because its gain solve
       already produced a band-resolved ambient verdict against THIS capture;
       when that verdict says the room was noisy, say so.
    6. **The gain solve itself.**  No plan, or a plan that could not clear the
       floor, and there is nothing for MEASURE to play.
    """
    if not screens.stimulus_located:
        return SCREEN_LOCATE_FAILED
    if screens.anchor_ambiguous:
        return SCREEN_ANCHOR_AMBIGUOUS
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
    """One MEASURE rung's finding, and what it asks the session to do.

    ``guard`` is the telemetry label that tells two rungs sharing one household
    code apart (§5.2's "never a new user-facing code for a capture-glitch
    class" convention means the journal's ``guard=`` field is the only way to
    know which check fired).  Empty when the kind alone is the whole finding.

    ``rearm`` asks for the silent auto-retry, and ``rearm_backoff_db`` is the
    level it should come back at — ``0.0`` reproducing the session's
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

    The three alignment fields encode the shipped alignment ladder without
    importing its thresholds.  ``alignment_present`` gates both rungs (a
    trims-only candidate has no estimate and skips them entirely);
    ``alignment_status_ok`` is the resolve verdict; ``delay_physically_plausible``
    is the physics backstop, which the ladder asks ONLY of a resolved estimate.

    **A fourth rung was here and is now a disclosure.**  The GCC trust floor
    (``ALIGNMENT_CONFIDENCE_TRUST_FLOOR``) refused a capture whose
    capture/seed confidence fell below 0.6; the nanny burn-down demoted it, so
    the confidence rides the receipt as provenance and this ladder no longer
    reads it.
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
    if (
        screens.alignment_present
        and screens.alignment_status_ok
        and not screens.delay_physically_plausible()
    ):
        return MeasureScreen(SCREEN_DELAY_IMPLAUSIBLE)
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
    G3's pilot-transfer step, the tracking-max comparison — reads session
    state that outlives one capture, so it stays with the session.  What
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


# --------------------------------------------------------------------------- #
# the capture-integrity predicates, and the two MEASURE thresholds two of
# them read (#2291 Phase 5c-ii)
#
# Private on purpose, and re-exported from the flow under these exact
# spellings, because the session calls them by those names. The precedent
# for a private name crossing a module boundary is ``fc_sweep``'s
# ``_fc_rejection``.
# --------------------------------------------------------------------------- #

# Measurement-honesty gate G2 (2026-07-22): an ``event=outputd.xrun`` playback
# glitch on 2026-07-22 hardware shifted a MEASURE capture's three sweeps
# −25…−28 ms off their SCHEDULED slot with per-segment locate confidence
# 0.07-0.12 (the measured clean corpus's WORST capture ran ≤1.5 ms residual
# at ≥0.6926 confidence) while ``glitch_detected`` stayed False — the
# repeat-pair drift check (``_estimate_drift``) is structurally blind to a
# uniform whole-capture shift (its own residual guard demeans per role, so
# it only catches a WITHIN-driver desync), and ``_stimulus_locate_ok`` passed
# on the max() confidence across every located stimulus, so one good segment
# masked three bad sweeps (that max() is per-ROLE since #1838's D8, which
# narrows but does not close the hole — a role's own pilots can still be the
# segment that clears it, which is why this per-sweep floor exists). Both
# thresholds carry wide margin on both sides of the two clusters above.
#
# The two are read by DIFFERENT gates since #1838's D3: the residual ceiling
# by ``_sweep_schedule_ok`` (a glitch — silent auto-retry), the confidence
# floor by ``_sweep_locate_confidence_ok`` (too quiet — no retry).
#
# Both have a deliberate twin one layer down —
# ``program_analysis.SWEEP_SCHEDULE_RESIDUAL_CEILING_MS`` /
# ``SWEEP_LOCATE_CONFIDENCE_FLOOR`` — which apply the SAME two judgments to
# VERIFY's single ``KIND_SUMMED_SWEEP`` (issue #1971), a segment kind neither
# gate here has ever filtered for. They are duplicated and pinned by
# tests/test_measurement_integrity_floor_contracts.py: a deliberate move of
# either number must update BOTH copies and that test.
#
# WHY duplicated, restated correctly (#2291 Phase 5c-iii, closing the #2391
# gate's docket item). The original reason given was that ``program_analysis``
# cannot import from this module without inverting the dependency. True, but
# it answers the wrong question — the LEGAL direction is available and this
# module already uses it (``INTEGRITY_CHECK_SWEEP_HEARD`` is imported from
# ``program_analysis`` above), so these two could be imported rather than
# re-declared. They are not, for a reason about the NUMBERS rather than the
# import graph: they judge different segment kinds through different gates,
# and bench work may well settle them at different values, and two owners
# can diverge by editing
# one line where one owner would first have to be re-split. The contract test
# is what keeps "equal today" honest until then; it is the guard for the
# duplication, not an excuse for it.
SWEEP_SCHEDULE_RESIDUAL_CEILING_MS = 5.0
SWEEP_LOCATE_CONFIDENCE_FLOOR = 0.3


def _sweep_locate_confidence_ok(analysis: ProgramAnalysis) -> bool:
    """False when a MEASURE sweep was only weakly located — i.e. too quiet.

    Split out of :func:`_sweep_schedule_ok` by D3 (issue #1838). The two
    halves of that gate answer different questions and deserve different
    verdicts:

    * a sweep whose RESIDUAL is out of bounds landed off its scheduled slot —
      a timeline splice, a genuine capture glitch, retry;
    * a sweep the locator could barely find at all is not a splice. It is a
      capture too quiet to hear, and the fix is the level or the mic, not a
      retry of the same level.

    In session cap_-Us10xORVNlFa_dgi-sP7g the sweeps located at 0.0298
    against this 0.3 floor, the mis-located sweeps then produced a 1018-sample
    residual, and the residual tripped ``glitch_detected`` — so the household
    was told the capture glitched and the flow silently re-armed the same
    unwinnable level. Low SNR CAUSES the glitch signal; ordering this check
    ahead of it is what makes the reported cause the real one.

    Same ``KIND_SWEEP`` domain as :func:`_sweep_schedule_ok`: the leading
    pilot pair's short, quiet windows locate coarsely by design and would
    manufacture spurious fires here.

    That domain is MEASURE-only, and deliberately stays so. VERIFY's sweep is
    ``KIND_SUMMED_SWEEP``, and the same judgment is made for it one layer down
    by ``program_analysis._verify_capture_integrity`` (issue #1971) — where
    the capture's own record can also say which MEASURE-era checks could not
    run there at all.
    """
    return all(
        loc.confidence >= SWEEP_LOCATE_CONFIDENCE_FLOOR
        for loc in analysis.locations
        if loc.kind == KIND_SWEEP
    )


def _sweep_schedule_ok(analysis: ProgramAnalysis, sample_rate_hz: int) -> bool:
    """False when a MEASURE sweep landed off its scheduled slot
    (measurement-honesty gate G2, 2026-07-22 — the xrun detector; see
    :data:`SWEEP_SCHEDULE_RESIDUAL_CEILING_MS` for the evidence).

    Since D3 (issue #1838) this is the RESIDUAL half of G2 only. The
    locate-confidence half moved to :func:`_sweep_locate_confidence_ok`,
    which runs earlier and answers "too quiet" instead of "glitched" — see
    that function for why the two must not share a verdict.

    ``sample_rate_hz`` is deliberately the CALLER's own MEASURE program rate,
    not something read off ``analysis`` itself:
    ``analyze_program_capture`` HARD-REFUSES a capture whose sample rate
    disagrees with the program's own (``capture rate != program rate``,
    ``jasper.audio_measurement.program_analysis``), and the capture spec
    fixes every capture at ``REQUIRED_SAMPLE_RATE_HZ`` (48 kHz,
    ``jasper.active_speaker.crossover_v2.sweep_spec``) — so no resampling ever
    runs between the WAV and this analysis, and ``SegmentLocation.residual_samples``
    is always expressed in exactly that domain (the session's own composed
    program's ``sample_rate_hz``).

    Filtered to ``KIND_SWEEP`` only — mirrors ``_estimate_drift``'s exclusion
    of the leading pilot pair from residual/drift logic (their short/quiet
    windows locate more coarsely and would manufacture spurious fires here).
    VERIFY's ``KIND_SUMMED_SWEEP`` is out of this domain on purpose; see
    :func:`_sweep_locate_confidence_ok` for where its twin lives (#1971).
    No sweeps at all (nothing to judge) passes — the pre-existing
    ``_stimulus_locate_ok`` check, which runs earlier in ``_measure_verdict``'s
    ladder, already covers "nothing usable in this capture".
    """
    sweeps = [loc for loc in analysis.locations if loc.kind == KIND_SWEEP]
    if not sweeps:
        return True
    for loc in sweeps:
        residual_ms = abs(loc.residual_samples) / sample_rate_hz * 1000.0
        if residual_ms > SWEEP_SCHEDULE_RESIDUAL_CEILING_MS:
            return False
    return True


def _sweep_schedule_diag_fields(
    analysis: ProgramAnalysis, sample_rate_hz: int,
) -> tuple[float | None, float | None]:
    """``(sweep_residual_ms_worst, sweep_locate_confidence_min)`` — diagnostic
    only, over the SAME ``KIND_SWEEP`` domain ``_sweep_schedule_ok`` and
    ``_sweep_locate_confidence_ok`` gate on (one figure each, since #1838's
    D3 split them), but never itself gates a verdict. ``sweep_residual_ms_worst`` is the
    SIGNED residual (not its magnitude) of whichever sweep has the largest
    absolute residual, so a reviewer sees which direction the schedule broke,
    not just how far. ``(None, None)`` when there are no sweeps to judge —
    mirrors ``_sweep_schedule_ok``'s own "nothing to judge" stance.
    """
    sweeps = [loc for loc in analysis.locations if loc.kind == KIND_SWEEP]
    if not sweeps:
        return None, None
    worst = max(sweeps, key=lambda loc: abs(loc.residual_samples))
    residual_ms_worst = worst.residual_samples / sample_rate_hz * 1000.0
    confidence_min = min(loc.confidence for loc in sweeps)
    return residual_ms_worst, confidence_min


def _gate_window_ms(response: Any) -> float | None:
    if response is None:
        return None
    window = response.gating.get("window_ms") if response.gating else None
    return float(window) if isinstance(window, (int, float)) else None


def _gate_floor_source(response: Any) -> str | None:
    """WHY ``_gate_window_ms`` is what it is — travels beside it everywhere.

    ``gating.FLOOR_MEASURED`` = a reflection onset was found and the window
    stops at it; ``gating.FLOOR_SEARCH_BOUND`` = the search reached
    ``gating.SEARCH_T_MAX_MS`` without finding one and the window was CAPPED
    there. Both print as the same ``gate_window_ms`` number, and the whole
    2026-07-30 corpus was the second state while every consumer read it as
    the first (issue #1966). ``None`` is an ungateable capture, never a
    guess. See ``program_analysis._gate_floor_source_of``, which does the
    same job for the retained-capture sidecar.
    """
    if response is None:
        return None
    source = response.gating.get("floor_source") if response.gating else None
    return str(source) if isinstance(source, str) else None


def _gate_trusted_band_hz(response: Any) -> tuple[float, float] | None:
    """The band this capture's own gate says it can be judged over (#2521).

    Read, never derived here: the band POLICY has one owner,
    ``jasper.audio_measurement.gate_disclosure.evaluation_band_hz``, which
    the pre/post-gate delta calls with this capture's TRUSTED floor
    (``2.5/T``) and the band its stimulus actually radiated;
    ``build_gate_disclosure`` is the single typed reader of the block that
    delta writes into. This function only picks that pair off the typed
    record, so no consumer can recompute a second opinion about which bins a
    capture supports.

    ``None`` for an ungateable capture, a capture whose program declared no
    sweep bounds, or an empty intersection: there is no band this capture can
    be judged over, and that is the finding. A caller must NOT substitute the
    raw grid edges — doing exactly that is what let the delta probe grade
    22,480 Hz on a capture trusted only to 20,000 (#2521).

    Imported inside the function, matching ``_gate_disclosure`` below.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.build_gate_disclosure(response.gating).delta_band_hz


def _gate_disclosure(response: Any) -> str | None:
    """``_gate_floor_source`` and its floors, rendered as one sentence.

    Rendered, never composed here: the copy has a single writer,
    ``jasper.audio_measurement.gate_disclosure.describe_gate``, so the
    per-position evidence file and the retained-capture sidecar cannot
    describe the same gate two different ways.

    Imported inside the function, matching how this module reaches its
    other cross-package helpers — it deliberately does not import
    :mod:`~jasper.audio_measurement.gating` at module scope either, and
    reads a gating block purely as data.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.describe_gate(response.gating)


def _gate_moved_rms_db(response: Any) -> float | None:
    """How far the gate moved the response's SHAPE, in dB RMS (ticket 1.5).

    The number :func:`_gate_disclosure`'s sentence already narrates ("the gate
    moved the response 2.59 dB RMS over 357-20000 Hz"), taken off the same
    typed record rather than re-derived, so the digits in the prose and the
    digits in the field cannot disagree. Read
    :attr:`~jasper.audio_measurement.gate_disclosure.GateDisclosure.delta_rms_db`'s
    own module docstring before reading the number: it is only interpretable
    beside ``gate_floor_source``, because a small delta means "genuinely clean"
    on a measured bound and "nothing was proven" on a ceiling-capped one.

    ``None`` when no delta could be priced at all — an ungateable capture, or
    a capture whose program declared no radiated band, where inventing a
    reading is exactly the over-report ``evaluation_band_hz`` refuses to make.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.build_gate_disclosure(response.gating).delta_rms_db


def _gate_reflection_delay_ms(response: Any) -> float | None:
    """The first reflection's arrival AFTER the direct one, in ms (ticket 1.5).

    The physical quantity, and deliberately NOT the gating block's own
    ``first_reflection_ms``: that field is an absolute time inside the analysed
    IR, an artifact of the deconvolution window's origin, and
    :attr:`~jasper.audio_measurement.gate_disclosure.GateDisclosure.reflection_delay_ms`
    — the single owner of the subtraction — calls it meaningless to a reader on
    its own. The delay is what :func:`_gate_disclosure`'s sentence narrates and
    what a reader can turn into a reflector distance.

    ``None`` when either side is unknown, and ALSO the honest answer on a
    capture whose window was capped at the search ceiling: nothing was found,
    so there is no arrival to time.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.build_gate_disclosure(response.gating).reflection_delay_ms


def _gate_entanglement_floor(
    response: Any, *, declared_first_bounce_s: float | None = None
) -> tuple[float | None, str]:
    """``(floor_hz, source)`` — the ROOM's floor at this capture, with provenance.

    Read off the same typed record as every other gate fact, so a position
    row and the sentence beside it cannot state two different floors.
    ``declared_first_bounce_s`` is the operator's rig geometry evaluated at
    THIS capture's own distance
    (:func:`jasper.audio_measurement.measurement_geometry.declared_first_bounce_s`),
    and is only reached when the gate measured no reflection to time (#3502).

    A capture with no gating block still has a room: the floor survives an
    ungateable capture, because the geometry that sets it is the rig's rather
    than the window's. ``(None, unknown)`` is the honest pair when nothing was
    declared and nothing was measured, and it is the ordinary one.
    """
    from jasper.audio_measurement import gate_disclosure

    d = gate_disclosure.build_gate_disclosure(
        getattr(response, "gating", None),
        declared_first_bounce_s=declared_first_bounce_s,
    )
    return d.entanglement_floor_hz, d.entanglement_floor_source


def _gate_record(
    response: Any, *, declared_first_bounce_s: float | None = None
) -> dict[str, Any] | None:
    """The gate reduced to the facts that leave this capture, or ``None``.

    ``{"disclosure": <the sentence>, "reflection_measured": <bool>,
    "moved_rms_db": <float|None>, "reflection_delay_ms": <float|None>,
    "entanglement_floor_hz": <float|None>,
    "entanglement_floor_source": <str>}``. Every one is
    :mod:`~jasper.audio_measurement.gate_disclosure`'s own derivation, taken
    off ONE typed record built here at compose time — the sentence is
    :func:`~jasper.audio_measurement.gate_disclosure.render_gate`'s, the
    bool is
    :attr:`~jasper.audio_measurement.gate_disclosure.GateDisclosure.gated_anything`
    (the single owner of "may this record claim reflections were removed"),
    and the numbers are that record's fields. None is re-derived downstream.

    ``declared_first_bounce_s`` reaches that record unchanged and is the
    entanglement floor's fallback source — see
    :func:`_gate_entanglement_floor`. It is a number rather than a path so
    the loading of the operator's file stays with the caller that knows which
    capture's distance to evaluate it at.

    **A reduction, not the block.** What travels to the wizard's durable state
    is these derived facts rather than the gating fragment itself, so the
    state file does not take a dependency on
    :mod:`~jasper.audio_measurement.gating`'s schema — that schema is versioned
    and moves (it went 1 -> 2 in R9), and a screen re-deriving copy from it
    would be a second place the two epistemic states could be collapsed back
    into one. A response with no gating block yields ``None``: absent stays
    absent, and no screen invents a gate that was never applied.

    **The two numbers were added because the sentence was the only copy of
    them** (ticket 1.5). A screen still reads only ``disclosure`` and
    ``reflection_measured``; the numbers exist so a READER of the banked round
    does not have to parse prose to get them, which the evidence packet's own
    ``not_evaluated`` block used to say it could not do. They stay derivations
    of the one typed record for the same reason the sentence is, so widening
    this record buys no new schema dependency.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    typed = gate_disclosure.build_gate_disclosure(
        response.gating, declared_first_bounce_s=declared_first_bounce_s
    )
    return {
        "disclosure": gate_disclosure.render_gate(typed),
        "reflection_measured": typed.gated_anything,
        "moved_rms_db": typed.delta_rms_db,
        "reflection_delay_ms": typed.reflection_delay_ms,
        "entanglement_floor_hz": typed.entanglement_floor_hz,
        "entanglement_floor_source": typed.entanglement_floor_source,
    }


def _pilot_by_role(analysis: ProgramAnalysis, role: str) -> Any | None:
    for pilot in analysis.pilots:
        if pilot.role == role:
            return pilot
    return None


def _pilot_transfer_by_role(analysis: ProgramAnalysis) -> dict[str, float]:
    """Per-role pilot transfer: captured hi level minus the programmed hi gain.

    Measurement-honesty gate G3's raw material (2026-07-22): VERIFY replays
    the identical program through the identical applied graph on every
    attempt, so this transfer should not move between attempts either — see
    :data:`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB`. Excludes any pilot whose
    ``programmed_hi_gain_db`` is unset (a legacy program built without
    ``leading_pilot_gains_db`` never threads it, per
    ``program_analysis.PilotObservation``'s docstring) — nothing to compare
    that pilot against.

    ``level_hi_dbfs`` safety note: ``PilotObservation``'s own docstring warns
    it "must never feed an ABSOLUTE-level consumer" (ambient subtraction
    shifts it by however much ambient power was removed). This use is safe
    for TWO independent reasons.

    (1) It is a RELATIVE cross-ATTEMPT comparison (this attempt's transfer
    minus the FIRST attempt's), never a true absolute-level read.

    (2) The ambient-difference confound the older version of this note
    deferred is now REAL but bounded far below the gate. Until issue #1810
    (2026-07-28) a VERIFY pilot pair had no ambient window at all, so
    subtraction was a literal no-op here; it now has a ~1 s pre-pilot window
    and subtraction is live. The bound: ``_verify_verdict`` refuses any
    attempt whose ``pilot_snr_ok`` is False BEFORE reaching the G3 block, so
    every attempt that gets here cleared ``PILOT_MIN_SNR_DB`` (≈12.4 dB) on
    the QUIET pilot — and the HI pilot sits a further
    ``PILOT_LEVEL_DELTA_DB`` (10 dB) above it, i.e. ≥22.4 dB in-band SNR. At
    that SNR the subtraction moves ``level_hi_dbfs`` by at most
    ``10·log10(1 − 10**−2.24)`` ≈ **0.025 dB**, so two admissible attempts can
    differ by at most ~0.05 dB from this term alone — an order of magnitude
    under :data:`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB` (0.35 dB). Lowering
    that ceiling toward ~0.1 dB, or raising ``PILOT_AMBIENT_WINDOW_S``'s trust
    without the SNR gate in front of it, is what would put this back in play.
    """
    return {
        pilot.role: pilot.level_hi_dbfs - pilot.programmed_hi_gain_db
        for pilot in analysis.pilots
        if pilot.programmed_hi_gain_db is not None
    }


def _pilot_diag_fields(pilot: Any | None) -> dict[str, float | None]:
    """One pilot's linearity/SNR/channel-map diagnostics, ``None``-safe.

    Channel-map publishes BOTH raw rises AND the isolation ratio derived from
    them. The ratio is what the CROSS verdict is decided on since 2026-08-21
    (``CHANNEL_MAP_MIN_ISOLATION_DB``), so a refusal has to name it; the raws
    stay so a sweep of these lines is still comparable against every diag
    logged before that switch, and so an operator can see which half of the
    ratio moved. The ratio is read from `channel_map_isolation_db` — the same
    function the verdict used — never recomputed here.
    """
    if pilot is None:
        return {
            "snr_db": None,
            "captured_delta_db": None,
            "programmed_delta_db": None,
            "channel_map_target_rise_db": None,
            "channel_map_cross_rise_db": None,
            "channel_map_isolation_db": None,
        }
    snr_db = pilot.snr_db
    target_rise = pilot.channel_map_target_rise_db
    cross_rise = pilot.channel_map_cross_rise_db
    isolation = channel_map_isolation_db(target_rise, cross_rise)
    return {
        "snr_db": round(snr_db, 2) if math.isfinite(snr_db) else None,
        "captured_delta_db": round(float(pilot.captured_delta_db), 3),
        "programmed_delta_db": round(float(pilot.programmed_delta_db), 3),
        "channel_map_target_rise_db": (
            round(target_rise, 3) if target_rise is not None else None
        ),
        "channel_map_cross_rise_db": (
            round(cross_rise, 3) if cross_rise is not None else None
        ),
        "channel_map_isolation_db": (
            round(isolation, 3) if isolation is not None else None
        ),
    }


# --------------------------------------------------------------------------- #
# the locate-confidence screen
# --------------------------------------------------------------------------- #

# A located stimulus below this correlation confidence reads as "couldn't hear
# the speaker" (locate_failed).
LOCATE_MIN_CONFIDENCE = 0.1
def _stimulus_locate_ok(analysis: ProgramAnalysis) -> bool:
    """False when any ROLE's stimuli all failed the locate-confidence floor.

    D8 (issue #1838). This used to be ``max(confidences) >= floor`` over every
    stimulus segment in the capture, which is effectively no floor at all on a
    multi-driver program: one clearly-located segment anywhere cleared the
    gate for the whole capture, so a capture in which an entire driver was
    inaudible passed and went on to be analysed as if both drivers had been
    heard. Grouping by role first makes the gate mean what its name says.

    Per ROLE, not per SEGMENT, deliberately. A role's segments are not
    equally locatable by design — a two-level pilot pair's quiet side sits
    10 dB under its loud side and locates more coarsely — so requiring every
    segment to clear the floor would fail captures that are fine. One
    confidently-located stimulus is enough to say "this driver was heard";
    zero is not. Role-less stimuli (a summed sweep, which carries no role)
    group together and are held to the same rule.

    The stricter per-SWEEP floor that MEASURE also applies lives in
    :func:`_sweep_locate_confidence_ok`.
    """
    by_role: dict[str | None, float] = {}
    for loc in analysis.locations:
        if loc.kind not in STIMULUS_KINDS:
            continue
        best = by_role.get(loc.role)
        if best is None or loc.confidence > best:
            by_role[loc.role] = loc.confidence
    if not by_role:
        return False
    return all(best >= LOCATE_MIN_CONFIDENCE for best in by_role.values())
