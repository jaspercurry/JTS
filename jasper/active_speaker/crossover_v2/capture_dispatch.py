# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which screens an anchor capture must clear, and in what order (#2291 5a-vii).

:mod:`.spatial` owns the ladders of the three phases a household WALKS; this one
owns the three it SITS STILL for: CHECK, MEASURE and VERIFY.

**This module DECIDES; it does not act.** A ladder answers "is this recording
evidence about the speaker, and if not, which finding is the honest one".
Everything a finding then CAUSES stays with the session, and a pure ladder asked
the same question twice answers the same way — which is what makes a replayed
capture safe to re-screen.

**No household vocabulary lives here.** A refusal leaves as a *kind*;
:mod:`.refusal_copy` maps it through ``SCREEN_KIND_REASONS`` and renders the
sentence. No ``jasper.web`` import and nothing from
:mod:`jasper.active_speaker.crossover_v2_flow`.

Two MEASURE facts arrive as CALLABLES rather than values because they are asked
ONLY once the rungs above them pass: ``sweep_schedule_ok`` reaches
``program_for_phase``, which raises when MEASURE has no composed program, so
resolving it eagerly would move an observable failure above the glitch rung.
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
# :mod:`.spatial` declares the five kinds the walked phases produce; the anchor
# phases produce those five plus these five. Two owners rather than one shared
# set, because a completeness test over one set would assert that a cloud
# position can report a channel-map mismatch, which it cannot.

#: CHECK heard the drivers on the wrong outputs.
SCREEN_CHANNEL_MAP_MISMATCH = "channel_map_mismatch"
#: The capture's timeline could not be attributed to the program it played.
#: Ahead of the one above in CHECK's ladder on purpose (#2644): a mis-anchored
#: capture reads every driver's window one pilot spacing from where that driver
#: actually played, which is how a correctly-wired speaker produced a
#: confident-looking "the drivers played out of order".
SCREEN_ANCHOR_AMBIGUOUS = "anchor_ambiguous"
#: The capture cleared its gates but sits too close to the room's own floor.
SCREEN_SNR_FLOOR = "snr_floor"
#: The curve bent, and this capture's OWN ambient evidence says the room did it.
SCREEN_NOISY_ROOM_LINEARITY = "noisy_room_linearity"
#: An alignment estimate exists and did not resolve inside its search window.
SCREEN_ALIGNMENT_UNRESOLVED = "alignment_unresolved"
#: An alignment estimate resolved to a delay physics rules out — the GCC
#: estimator returning a CONFIDENTLY WRONG lag, a measured failure mode rather
#: than a prior (a hardware run reported a confident −631 us against a declared
#: [50, 300] us search bound). Its own kind rather than shared with the 0.6 GCC
#: trust floor, so demoting that floor did not take this rejection's voice with
#: it: a physics fact and a prior are different answers.
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
#: :mod:`.refusal_copy`'s ``SCREEN_KIND_REASONS`` is checked for completeness
#: against THIS set, so a new rung in either owner cannot ship without a
#: household sentence.
CAPTURE_SCREEN_KINDS = SCREEN_KINDS | ANCHOR_SCREEN_KINDS


# --------------------------------------------------------------------------- #
# CHECK — is this room, at this level, measurable at all
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckScreens:
    """Every fact CHECK's ladder reads, stated by the caller.

    All fields are required, the rule :class:`~.spatial.CaptureScreens` set: a
    permissive default is how a caller that forgot to establish a fact gets a
    pass instead of an error.

    ``gain_plan_present`` and ``gain_plan_snr_floor_ok`` are two fields rather
    than one optional because the ladder reads them at two rungs and means
    different things by them: the linearity rung asks "did the gain solve ALREADY
    judge this room noisy", the final rung refuses an absent plan outright.
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

    Order is load-bearing:

    1. **Stimulus located.** A capture whose stimulus was never found is not
       evidence about anything.
    2. **Anchor attributed.** Every rung below reads per-driver windows, and the
       channel map is the rung that turns a slid window into a household
       instruction to rewire a correctly-wired speaker (#2644). Retriable,
       because re-recording is exactly what fixes it.
    3. **Channel map.** Explicit ``False`` only — ``None`` is no evidence.
    4. **Pilot SNR**, ahead of linearity (#1838): below the floor the
       ambient-subtracted two-pilot delta is not evidence either way, so the
       honest finding is the room and the level, never the microphone.
    5. **Linearity** (W6.12). CHECK is the one phase that can tell the room from
       the microphone, because its gain solve already produced a band-resolved
       ambient verdict against THIS capture.
    6. **The gain solve itself.** No plan, or a plan that could not clear the
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
    code apart (§5.2 forbids a new user-facing code for a capture-glitch class,
    so the journal's ``guard=`` field is the only way to know which fired).
    Empty when the kind alone is the whole finding.

    ``rearm`` asks for the silent auto-retry; ``rearm_backoff_db`` is the level
    it comes back at, ``0.0`` reproducing the no-argument call exactly.
    """

    kind: str
    guard: str = ""
    rearm: bool = False
    rearm_backoff_db: float = 0.0


@dataclass(frozen=True)
class MeasureScreens:
    """Every fact MEASURE's ladder reads.

    Two are **callables**, not values, and the module docstring says why. The
    rest are plain answers already in the caller's hand.

    The three alignment fields encode the shipped alignment ladder without
    importing its thresholds: ``alignment_present`` gates both rungs (a
    trims-only candidate has no estimate and skips them),
    ``alignment_status_ok`` is the resolve verdict, and
    ``delay_physically_plausible`` is the physics backstop, asked ONLY of a
    resolved estimate. The GCC trust floor is a receipt disclosure, not a
    ladder rung here.
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

    **"Too quiet" runs before "glitched"** (D3, #1838). A capture nobody could
    hear produces the same symptoms as a spliced one — the locator lands the
    sweeps wrong, the residual blows past its ceiling, and the glitch signal
    fires on noise. Low SNR CAUSES the glitch signal, so the level verdicts have
    to be asked first or the reported cause is never the real one.

    **Neither level rung re-arms**: re-running an inaudible measurement at the
    same level cannot succeed, and both kinds already carry a household action
    that can. The three transient rungs (glitch, schedule, clipped) DO re-arm
    silently, and the clipped one comes back quieter. ``sweep_schedule`` is the
    xrun detector — a uniform whole-capture shift the repeat-pair drift check is
    structurally blind to — and it shares the glitch kind with ``guard`` as the
    discriminator.

    ``clip_retry_backoff_db`` is stated rather than imported: it is the flow's
    policy number, and inputs are stated, never reached for.
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
    #2087). A predicted ripple above the threshold says the two branches sum
    less coherently in this room than the calibration corpus did; the capture is
    still accepted, and what changes is what the household is TOLD.

    The caller establishes that a candidate EXISTS; this owns the other half of
    the shipped skip — without an alignment estimate there is no reservation to
    make.
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

    ``integrity_payload`` is set only on the capture-integrity arm, the same
    shape :class:`~.spatial.EntryBaselineScreen` carries: the household screen
    wants the record beside the code.
    """

    kind: str
    integrity_payload: Mapping[str, Any] | None = None


def verify_integrity_screens(
    analysis: "ProgramAnalysis", *, stimulus_located: bool
) -> VerifyIntegrityScreen | None:
    """VERIFY's pre-grade ladder: a refusal, or ``None`` to go on grading.

    Everything below this in the shipped verdict reads session state that
    outlives one capture, so it stays with the session. What leaves is the part
    that asks only about THIS recording, and it runs first: a spliced or clipped
    recording is not evidence about the speaker.

    **The one difference from** :func:`~.spatial.entry_baseline_screens`: an
    ABSENT integrity record is no-evidence-and-continue here and UNUSABLE there.
    ``None`` is the pre-#1971 analysis shape and means no evidence — the
    convention ``linearity_ok`` and ``pilot_snr_ok`` use — and the diagnostic
    prints ``integrity=unavailable`` for it. The entry baseline fails closed on
    the same input because it exists ONLY to be compared.

    Two kinds out of one record, because the two failures need different
    household actions and #1838's D3 is explicit they must not share one: a
    sweep nobody could hear is a level/mic problem re-running cannot fix, while a
    spliced or clipped timeline is the transient capture-glitch class.

    ``analysis`` arrives whole and ``stimulus_located`` separately because this
    ladder CONSUMES the integrity record, while ``stimulus_located`` is the
    answer of a flow-side predicate MEASURE's and CHECK's verdicts also share.
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
# them read.
#
# Private on purpose, and re-exported from the flow under these exact spellings
# because the session calls them by those names; the precedent is ``fc_sweep``'s
# ``_fc_rejection``.
# --------------------------------------------------------------------------- #

# Measurement-honesty gate G2: an ``event=outputd.xrun`` playback glitch shifted
# a MEASURE capture's three sweeps −25…−28 ms off their SCHEDULED slot at
# per-segment locate confidence 0.07-0.12, while the measured clean corpus's
# WORST capture ran ≤1.5 ms residual at ≥0.6926 confidence — and
# ``glitch_detected`` stayed False, because the repeat-pair drift check is
# structurally blind to a uniform whole-capture shift. Both thresholds carry
# wide margin on both sides of those two clusters.
#
# The two are read by DIFFERENT gates: the residual ceiling by
# ``_sweep_schedule_ok`` (a glitch — silent auto-retry), the confidence floor by
# ``_sweep_locate_confidence_ok`` (too quiet — no retry).
#
# Both have a deliberate twin one layer down —
# ``program_analysis.SWEEP_SCHEDULE_RESIDUAL_CEILING_MS`` /
# ``SWEEP_LOCATE_CONFIDENCE_FLOOR`` — applying the SAME two judgments to VERIFY's
# ``KIND_SUMMED_SWEEP``, a segment kind neither gate here filters for. They are
# duplicated rather than imported because they judge different segment kinds
# through different gates and bench work may settle them at different values;
# tests/test_measurement_integrity_floor_contracts.py pins the pair, so a
# deliberate move of either number must update BOTH copies and that test.
SWEEP_SCHEDULE_RESIDUAL_CEILING_MS = 5.0
SWEEP_LOCATE_CONFIDENCE_FLOOR = 0.3


def _sweep_locate_confidence_ok(analysis: ProgramAnalysis) -> bool:
    """False when a MEASURE sweep was only weakly located — i.e. too quiet.

    Split out of :func:`_sweep_schedule_ok` by D3 (#1838): a sweep whose
    RESIDUAL is out of bounds landed off its scheduled slot and is a genuine
    capture glitch worth retrying, while a sweep the locator could barely find
    is a capture too quiet to hear, whose fix is the level or the mic. In one
    session the sweeps located at 0.0298 against this 0.3 floor, the mis-located
    sweeps produced a 1018-sample residual, and the residual tripped
    ``glitch_detected`` — so the flow silently re-armed the same unwinnable
    level.

    Same ``KIND_SWEEP`` domain as :func:`_sweep_schedule_ok`: the leading pilot
    pair's short, quiet windows locate coarsely by design and would manufacture
    spurious fires. VERIFY's ``KIND_SUMMED_SWEEP`` is judged one layer down by
    ``program_analysis._verify_capture_integrity`` (#1971).
    """
    return all(
        loc.confidence >= SWEEP_LOCATE_CONFIDENCE_FLOOR
        for loc in analysis.locations
        if loc.kind == KIND_SWEEP
    )


def _sweep_schedule_ok(analysis: ProgramAnalysis, sample_rate_hz: int) -> bool:
    """False when a MEASURE sweep landed off its scheduled slot
    (measurement-honesty gate G2 — the xrun detector; see
    :data:`SWEEP_SCHEDULE_RESIDUAL_CEILING_MS` for the evidence).

    Since D3 (#1838) this is the RESIDUAL half of G2 only; the locate-confidence
    half is :func:`_sweep_locate_confidence_ok`, which runs earlier.

    ``sample_rate_hz`` is the CALLER's own MEASURE program rate, not something
    read off ``analysis``: ``analyze_program_capture`` hard-refuses a capture
    whose sample rate disagrees with the program's, and the capture spec fixes
    every capture at ``REQUIRED_SAMPLE_RATE_HZ`` (48 kHz), so no resampling ever
    runs between the WAV and this analysis and ``residual_samples`` is always in
    that domain.

    Filtered to ``KIND_SWEEP`` only, mirroring ``_estimate_drift``'s exclusion of
    the leading pilot pair. No sweeps at all passes — ``_stimulus_locate_ok``
    runs earlier and already covers "nothing usable in this capture".
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
    only, over the SAME ``KIND_SWEEP`` domain the two gates above use, and never
    itself a verdict. ``sweep_residual_ms_worst`` is the SIGNED residual (not its
    magnitude) of whichever sweep has the largest absolute residual, so a
    reviewer sees which direction the schedule broke. ``(None, None)`` when there
    are no sweeps to judge.
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

    ``gating.FLOOR_MEASURED`` = a reflection onset was found and the window stops
    at it; ``gating.FLOOR_SEARCH_BOUND`` = the search reached
    ``gating.SEARCH_T_MAX_MS`` without finding one and the window was CAPPED
    there. Both print as the same ``gate_window_ms`` number, and a whole corpus
    was the second state while every consumer read it as the first (#1966).
    ``None`` is an ungateable capture, never a guess.
    """
    if response is None:
        return None
    source = response.gating.get("floor_source") if response.gating else None
    return str(source) if isinstance(source, str) else None


def _gate_trusted_band_hz(response: Any) -> tuple[float, float] | None:
    """The band this capture's own gate says it can be judged over (#2521).

    Read, never derived here: the band POLICY has one owner,
    ``gate_disclosure.evaluation_band_hz``, called with this capture's TRUSTED
    floor (``2.5/T``) and the band its stimulus actually radiated. This function
    only picks that pair off the typed record.

    ``None`` for an ungateable capture, a capture whose program declared no sweep
    bounds, or an empty intersection — that is the finding. A caller must NOT
    substitute the raw grid edges: doing exactly that is what let the delta probe
    grade 22,480 Hz on a capture trusted only to 20,000 (#2521).
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.build_gate_disclosure(response.gating).delta_band_hz


def _gate_disclosure(response: Any) -> str | None:
    """``_gate_floor_source`` and its floors, rendered as one sentence.

    Rendered, never composed here: the copy has a single writer,
    ``gate_disclosure.describe_gate``, so the per-position evidence file and the
    retained-capture sidecar cannot describe one gate two different ways.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.describe_gate(response.gating)


def _gate_moved_rms_db(response: Any) -> float | None:
    """How far the gate moved the response's SHAPE, in dB RMS (ticket 1.5).

    The number :func:`_gate_disclosure`'s sentence already narrates, taken off
    the same typed record rather than re-derived, so the digits in the prose and
    in the field cannot disagree. Only interpretable beside
    ``gate_floor_source``: a small delta means "genuinely clean" on a measured
    bound and "nothing was proven" on a ceiling-capped one.

    ``None`` when no delta could be priced at all — an ungateable capture, or one
    whose program declared no radiated band.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.build_gate_disclosure(response.gating).delta_rms_db


def _gate_reflection_delay_ms(response: Any) -> float | None:
    """The first reflection's arrival AFTER the direct one, in ms (ticket 1.5).

    The physical quantity, and deliberately NOT the gating block's own
    ``first_reflection_ms``, which is an absolute time inside the analysed IR and
    an artifact of the deconvolution window's origin.

    ``None`` when either side is unknown, and ALSO the honest answer on a capture
    whose window was capped at the search ceiling: nothing was found, so there is
    no arrival to time.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.build_gate_disclosure(response.gating).reflection_delay_ms


def _gate_entanglement_floor(
    response: Any, *, declared_first_bounce_s: float | None = None
) -> tuple[float | None, str]:
    """``(floor_hz, source)`` — the ROOM's floor at this capture, with provenance.

    Read off the same typed record as every other gate fact, so a position row
    and the sentence beside it cannot state two different floors.
    ``declared_first_bounce_s`` is the operator's rig geometry evaluated at THIS
    capture's own distance, and is only reached when the gate measured no
    reflection to time (#3502).

    A capture with no gating block still has a room: the floor survives an
    ungateable capture, because the geometry that sets it is the rig's rather
    than the window's. ``(None, unknown)`` is the honest — and ordinary — pair
    when nothing was declared and nothing was measured.
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

    Every field is :mod:`~jasper.audio_measurement.gate_disclosure`'s own
    derivation, taken off ONE typed record built here at compose time; none is
    re-derived downstream. ``reflection_measured`` is ``gated_anything``, the
    single owner of "may this record claim reflections were removed".

    **A reduction, not the block.** What travels to the wizard's durable state is
    these derived facts rather than the gating fragment itself, so the state file
    takes no dependency on :mod:`~jasper.audio_measurement.gating`'s schema —
    that schema is versioned and moves. A response with no gating block yields
    ``None``: no screen invents a gate that was never applied.

    The two numbers exist so a READER of the banked round does not have to parse
    the sentence to get them (ticket 1.5); a screen still reads only
    ``disclosure`` and ``reflection_measured``.
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

    Measurement-honesty gate G3's raw material: VERIFY replays the identical
    program through the identical applied graph on every attempt, so this
    transfer should not move between attempts either. Excludes any pilot whose
    ``programmed_hi_gain_db`` is unset — there is nothing to compare it against.

    ``PilotObservation`` warns that ``level_hi_dbfs`` must never feed an
    ABSOLUTE-level consumer, because ambient subtraction shifts it. This use is
    safe for two independent reasons. (1) It is a RELATIVE cross-ATTEMPT
    comparison, never a true absolute-level read. (2) The confound is bounded far
    below the gate: ``_verify_verdict`` refuses any attempt whose ``pilot_snr_ok``
    is False before reaching G3, so every attempt here cleared
    ``PILOT_MIN_SNR_DB`` (≈12.4 dB) on the QUIET pilot and the HI pilot sits a
    further ``PILOT_LEVEL_DELTA_DB`` (10 dB) above, i.e. ≥22.4 dB in-band SNR. At
    that SNR the subtraction moves ``level_hi_dbfs`` by at most
    ``10·log10(1 − 10**−2.24)`` ≈ 0.025 dB, so two admissible attempts differ by
    at most ~0.05 dB from this term — an order of magnitude under
    :data:`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB` (0.35 dB). Lowering that
    ceiling toward ~0.1 dB, or trusting ``PILOT_AMBIENT_WINDOW_S`` without the
    SNR gate in front of it, is what would put this back in play.
    """
    return {
        pilot.role: pilot.level_hi_dbfs - pilot.programmed_hi_gain_db
        for pilot in analysis.pilots
        if pilot.programmed_hi_gain_db is not None
    }


def _pilot_diag_fields(pilot: Any | None) -> dict[str, float | None]:
    """One pilot's linearity/SNR/channel-map diagnostics, ``None``-safe.

    Channel-map publishes BOTH raw rises AND the isolation ratio derived from
    them. The ratio is what the CROSS verdict is decided on
    (``CHANNEL_MAP_MIN_ISOLATION_DB``), so a refusal has to name it; the raws
    stay so an operator can see which half of the ratio moved. The ratio comes
    from ``channel_map_isolation_db`` — the same function the verdict used.
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

    Per ROLE, not per SEGMENT, and not a max() over the whole capture (D8,
    #1838). A max() over every segment is effectively no floor at all on a
    multi-driver program: one clearly-located segment anywhere cleared the gate,
    so a capture in which an entire driver was inaudible passed. Per-SEGMENT
    would be too strict the other way — a two-level pilot pair's quiet side sits
    10 dB under its loud side and locates more coarsely. One confidently-located
    stimulus says "this driver was heard"; zero does not. Role-less stimuli (a
    summed sweep) group together under the same rule.

    The stricter per-SWEEP floor MEASURE also applies is
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
