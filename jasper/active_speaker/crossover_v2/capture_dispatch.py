# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-take recording integrity, capabilities and the next capture action."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Mapping

from jasper.audio_measurement import gate_disclosure
from jasper.audio_measurement.program import KIND_SWEEP, STIMULUS_KINDS
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    INTEGRITY_CHECK_SWEEP_HEARD,
    channel_map_isolation_db,
)
from jasper.audio_measurement.program_analysis.check import alignment_snr_gain_adjustment
from jasper.audio_measurement.program_analysis.model import MeasurementPriors, ProgramAnalysis
from .sweep_spec import REQUIRED_SAMPLE_RATE_HZ
from jasper.json_fields import finite_float

from . import refusal_copy as reasons

if TYPE_CHECKING:
    from jasper.audio_measurement.program import ExcitationProgram

TakeNext = Literal["accept", "retake_same", "retake_louder", "retake_quieter", "fix_and_retake", "stop"]
TakeCharge = Literal["speaker", "operator", "none"]

# Clip retries lower stimulus gain, never the admitted hardware ceiling.
CLIP_RETRY_BACKOFF_DB = 3.0
# dB, recorder transfer stability; see ADR-0182.
VERIFY_PILOT_TRANSFER_STEP_CEILING_DB = 0.35


@dataclass(frozen=True)
class TakeVerdict:
    ok: bool
    fault: str | None = None
    evidence: dict[str, float | bool | str] = field(default_factory=dict)
    capabilities: dict[str, bool] = field(default_factory=dict)
    next: TakeNext = "accept"
    # Absolute stimulus dBFS. Per-role targets are carried in evidence.
    next_gain_db: float | None = None
    charge: TakeCharge = "none"

    @property
    def gain_targets(self) -> dict[str, float]:
        return {key.removeprefix("next_gain_db."): float(value)
                for key, value in self.evidence.items() if key.startswith("next_gain_db.")}


def assess(
    analysis: ProgramAnalysis, *, phase: str,
    priors: MeasurementPriors | None = None,
    program: ExcitationProgram | None = None,
    gain_db: Mapping[str, float] | None = None,
    gain_ceiling_db: Mapping[str, float] | None = None,
    pilot_transfer_prior: Mapping[str, float] | None = None,
) -> TakeVerdict:
    """Assess one recording without a session, player or mutable retry state."""
    if phase not in {"check", "measure", "verify"}:
        raise ValueError(f"unsupported assessment phase: {phase}")
    priors = priors or MeasurementPriors()
    gains = dict(gain_db) if gain_db is not None else {
        seg.role or "summed": seg.gain_db for seg in program.segments
        if seg.kind in STIMULUS_KINDS
    } if program is not None else {}
    ceilings = gain_ceiling_db or {}
    anchor, drift, alignment = analysis.anchor, analysis.drift, analysis.alignment
    sample_rate = program.sample_rate_hz if program else REQUIRED_SAMPLE_RATE_HZ
    schedule_residual_ms, _ = _sweep_schedule_diag_fields(analysis, sample_rate)
    stimuli = [loc for loc in analysis.locations if loc.kind in STIMULUS_KINDS]
    evidence: dict[str, float | bool | str] = {
        "mic_meter_status": analysis.mic_meter_status or "unmeasured",
        "anchor_ambiguous": analysis.anchor_ambiguous or bool(anchor and anchor.ambiguous),
        "glitch_detected": bool(analysis.glitch_detected),
        "frame_loss": bool(analysis.frame_ledger and analysis.frame_ledger.lost_at),
        "glitch_inputs": ",".join(drift.glitch_inputs) if drift else "",
    }
    figures = {
        "anchor_presence": anchor.presence if anchor else None,
        "anchor_confidence": anchor.confidence if anchor else None,
        "anchor_corroborated": anchor.corroborated if anchor else None,
        "epsilon_ppm": drift.epsilon_ppm if drift else None,
        "max_residual_samples": drift.max_residual_samples if drift else None,
        "schedule_residual_ms_worst": schedule_residual_ms,
        "repeat_level_delta_db": drift.repeat_level_delta_db if drift else None,
        "discontinuity_samples": analysis.discontinuity_samples,
        "peak_dbfs": max((loc.peak_dbfs for loc in stimuli), default=None),
        "locate_confidence_min": min((loc.confidence for loc in stimuli), default=None),
    }
    evidence.update({key: value if isinstance(value, bool) else float(value)
                     for key, value in figures.items() if value is not None})
    responses = (*analysis.driver_responses, *((analysis.summed_response,) if analysis.summed_response else ()))
    capabilities = {
        "magnitude": bool(responses),
        "delay_estimate": alignment is not None and alignment.status == ALIGNMENT_OK,
        "level_solve": analysis.gain_plan is not None and analysis.gain_plan.snr_floor_ok,
    }
    for response in responses:
        for decision in ("magnitude", "alignment"):
            # Magnitude occupies the top-level SNR block; alignment is nested.
            block = (response.snr or {}) if decision == "magnitude" else (response.snr or {}).get("alignment", {})
            worst = block.get("worst_relevant") or {}
            band: Mapping[str, Any] = next((row for row in block.get("bands", ())
                         if row.get("band_id") == worst.get("band_id")), {})
            prefix = f"snr.{response.role}.{decision}"
            if worst.get("band_id") is not None:
                evidence[f"{prefix}.band_id"] = str(worst["band_id"])
            for key in ("estimated_snr_db", "shortfall_db"):
                value = finite_float(band.get(key))
                if value is not None:
                    evidence[f"{prefix}.{key}"] = value
            evidence[f"{prefix}.verdict"] = str(worst.get("verdict", "unknown"))
            if worst.get("verdict") == "insufficient":
                capabilities["delay_estimate" if decision == "alignment" else "magnitude"] = False
    if alignment is not None:
        evidence.update(alignment_status=alignment.status, delay_us=float(alignment.delay_us))
        bounds = priors.alignment_delay_bounds_us
        plausible = bounds is None or bounds[0] <= abs(alignment.delay_us) <= bounds[1]
        evidence["delay_physically_plausible"] = plausible
        capabilities["delay_estimate"] &= plausible

    verdict = TakeVerdict(True, evidence=evidence, capabilities=capabilities)

    def refuse(code: str, *, next: TakeNext = "fix_and_retake", charge: TakeCharge = "operator",
               targets: Mapping[str, float] | None = None, ok: bool = False) -> TakeVerdict:
        targets = targets or {}
        return replace(verdict, ok=ok, fault=code, next=next, charge=charge,
                       next_gain_db=max(targets.values(), default=None),
                       evidence={**evidence, **{f"next_gain_db.{role}": float(gain)
                                               for role, gain in targets.items()}},
                       capabilities=capabilities if ok else {key: False for key in capabilities})

    adjusted = alignment_snr_gain_adjustment(analysis.driver_responses, gains, ceilings)

    def quiet(code: str) -> TakeVerdict:
        return refuse(code, next="retake_louder" if adjusted else "fix_and_retake",
                      charge="speaker" if gains else "operator", targets=adjusted)

    if evidence["frame_loss"]:
        return refuse(reasons.REASON_DRIFT_BASELINES_DISAGREE, next="retake_same", charge="speaker")
    if not _stimulus_locate_ok(analysis):
        return quiet(reasons.REASON_LOCATE_FAILED)
    if evidence["anchor_ambiguous"]:
        return refuse(reasons.REASON_ANCHOR_AMBIGUOUS)
    if analysis.delta_implausible:
        return (refuse(reasons.REASON_ANCHOR_AMBIGUOUS) if analysis.pilot_snr_ok is True
                else quiet(reasons.REASON_SNR_FLOOR))
    if analysis.pilot_snr_ok is False:
        return quiet(reasons.REASON_SNR_FLOOR if phase == "check" else reasons.REASON_PILOT_LEVEL_COLLAPSE)
    # Retire when locate can resolve the timeline without a corroborating witness.
    if anchor is not None and anchor.corroborated is False:
        return quiet(reasons.REASON_ANCHOR_TOO_QUIET)
    if analysis.mic_meter_status in {"low", "too_quiet"}:
        return quiet(reasons.REASON_PILOT_LEVEL_COLLAPSE)
    if not _sweep_locate_confidence_ok(analysis):
        evidence["guard"] = "sweep_locate_confidence"
        return quiet(reasons.REASON_LOCATE_FAILED)
    integrity = analysis.capture_integrity
    if integrity is not None and INTEGRITY_CHECK_SWEEP_HEARD in integrity.failed:
        return quiet(reasons.REASON_LOCATE_FAILED)
    if _any_sweep_clipped(analysis) or analysis.mic_meter_status in {"too_loud", "clipping"}:
        targets = {role: gain - CLIP_RETRY_BACKOFF_DB for role, gain in gains.items()}
        return refuse(reasons.REASON_CLIPPED, next="retake_quieter", charge="speaker", targets=targets)
    if analysis.glitch_detected or (analysis.discontinuity_samples or 0) != 0 or (integrity and integrity.failed):
        charge: TakeCharge = ("operator" if drift and drift.glitch_inputs == ("repeat_level_disagree",)
                              and not analysis.discontinuity_samples else "speaker")
        return refuse(reasons.REASON_DRIFT_BASELINES_DISAGREE, next="retake_same", charge=charge)
    if not _sweep_schedule_ok(analysis, sample_rate):
        evidence["guard"] = "sweep_schedule"
        return refuse(reasons.REASON_DRIFT_BASELINES_DISAGREE, next="retake_same", charge="speaker")
    if phase == "check" and analysis.channel_map_ok is False:
        return refuse(reasons.REASON_CHANNEL_MAP_MISMATCH, next="stop", charge="none", ok=True)
    if analysis.linearity_ok is False:
        code = (reasons.REASON_NOISY_ROOM_LINEARITY if phase == "check" and analysis.gain_plan
                and not analysis.gain_plan.snr_floor_ok else reasons.REASON_AGC_BEHAVIORAL_FAIL)
        return refuse(code)
    if phase == "check" and not capabilities["level_solve"]:
        return quiet(reasons.REASON_SNR_FLOOR)
    if phase == "verify" and pilot_transfer_prior:
        transfer = _pilot_transfer_by_role(analysis)
        step = max((abs(value - pilot_transfer_prior[role]) for role, value in transfer.items()
                    if role in pilot_transfer_prior), default=None)
        if step is not None:
            evidence["pilot_transfer_step_db"] = float(step)
            if step > VERIFY_PILOT_TRANSFER_STEP_CEILING_DB:
                return refuse(reasons.REASON_VERIFY_LEVEL_SHIFT, ok=True)
    if adjusted:
        return replace(verdict, next="retake_louder", next_gain_db=max(adjusted.values()), charge="speaker",
                       evidence={**evidence, **{f"next_gain_db.{role}": gain for role, gain in adjusted.items()}})
    return verdict


def _clipped_stimulus_peaks(analysis: ProgramAnalysis) -> list[float]:
    return [float(loc.peak_dbfs) for loc in analysis.locations
            if loc.kind in STIMULUS_KINDS and loc.clipped]


def _any_sweep_clipped(analysis: ProgramAnalysis) -> bool:
    return bool(_clipped_stimulus_peaks(analysis))


def ripple_reservation_due(
    *,
    predicted_ripple_db: float,
    has_alignment: bool,
    disclosure_threshold_db: float,
) -> bool:
    """Does this accepted MEASURE owe the household a ripple reservation?

    **This decides a disclosure, never a refusal.** See ADR-0002. A predicted
    ripple above the threshold says the two branches sum less coherently in
    this room than the calibration corpus did; the capture is still accepted,
    and what changes is what the household is TOLD.

    The caller establishes that a candidate EXISTS; this owns the other half of
    the shipped skip — without an alignment estimate there is no reservation to
    make.
    """
    if not has_alignment:
        return False
    return predicted_ripple_db > disclosure_threshold_db


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
    return gate_disclosure.build_gate_disclosure(response.gating).delta_band_hz


def _gate_disclosure(response: Any) -> str | None:
    """``_gate_floor_source`` and its floors, rendered as one sentence.

    Rendered, never composed here: the copy has a single writer,
    ``gate_disclosure.describe_gate``, so the per-position evidence file and the
    retained-capture sidecar cannot describe one gate two different ways.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    return gate_disclosure.describe_gate(response.gating)


def _gate_moved_rms_db(response: Any) -> float | None:
    """How far the gate moved the response's SHAPE, in dB RMS.

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
    return gate_disclosure.build_gate_disclosure(response.gating).delta_rms_db


def _gate_reflection_delay_ms(response: Any) -> float | None:
    """The first reflection's arrival AFTER the direct one, in ms.

    The physical quantity, and deliberately NOT the gating block's own
    ``first_reflection_ms``, which is an absolute time inside the analysed IR and
    an artifact of the deconvolution window's origin.

    ``None`` when either side is unknown, and ALSO the honest answer on a capture
    whose window was capped at the search ceiling: nothing was found, so there is
    no arrival to time.
    """
    if response is None or not getattr(response, "gating", None):
        return None
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
    the sentence to get them; a screen still reads only
    ``disclosure`` and ``reflection_measured``.
    """
    if response is None or not getattr(response, "gating", None):
        return None
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

    The measurement-honesty raw material: VERIFY replays the identical
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


def _pilot_diag_fields(pilot: Any | None) -> dict[str, float | bool | None]:
    """One pilot's linearity/SNR/channel-map diagnostics, ``None``-safe.

    Channel-map publishes BOTH raw rises AND the isolation ratio derived from
    them. The ratio is what the CROSS verdict is decided on
    (``CHANNEL_MAP_MIN_ISOLATION_DB``), so a refusal has to name it; the raws
    stay so an operator can see which half of the ratio moved. The ratio comes
    from ``channel_map_isolation_db`` — the same function the verdict used.

    ``delta_implausible`` (#2647) rides along per pilot: the aggregate on the
    check-diag line says the finding fired, this says which driver's delta
    was the one no real wiring could produce.
    """
    if pilot is None:
        return {
            "snr_db": None,
            "captured_delta_db": None,
            "programmed_delta_db": None,
            "channel_map_target_rise_db": None,
            "channel_map_cross_rise_db": None,
            "channel_map_isolation_db": None,
            "delta_implausible": None,
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
        "delta_implausible": bool(pilot.delta_implausible),
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
