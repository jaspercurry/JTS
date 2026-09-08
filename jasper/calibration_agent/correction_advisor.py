# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room-advisor narration and bounded proposals; never writes CamillaDSP.

The packet contains summaries, not raw audio or device identifiers. Simulation
informs a proposal. The numeric overlap check cannot verify a model's claims.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from jasper.correction import strategy as _strategy
from jasper.log_event import log_event

from . import key_provisioning, model_client, prompt, response
from . import proposal_sim
from .advisor_context import _curve_summary
from .curves import curve_values

logger = logging.getLogger(__name__)

CONTEXT_SCHEMA_VERSION = 1
INTERPRET_KIND = "jts_correction_interpret"
PROPOSE_KIND = "jts_correction_proposal_review"

# Bass band the residual and modes are summarized over (the design band top).
_RESIDUAL_BAND_HZ = (20.0, 350.0)

_INTERPRET_SYSTEM = """\
You are the JTS audio tuning assistant, explaining a room-correction
measurement to the person who took it, in plain language. You are NOT
the DSP authority — deterministic JTS code owns the measurement math,
the accept/reject verdict, and every filter.

Explain, in a few short sentences a non-expert can follow:
- what the measured room response is doing (the biggest peaks/dips and
  roughly where),
- what JTS's correction is targeting and why,
- what the acceptance verdict means (did the re-measurement confirm an
  improvement, or is it inconclusive),
- if a crossover region is present, that a dip there is the subwoofer
  hand-off, not a room mode to boost.

Rules:
- Every number you state about the room MUST be one already in the
  evidence packet — never invent or re-estimate a frequency, dB value,
  or verdict. Restraint first: good rooms need little correction.
- Correction claims are things the measurement/re-measure established;
  state them as facts with their numbers.
- Any preference/taste suggestion ("this might sound warmer") is
  subjective — phrase it as a question, not a fact.
- Do not emit CamillaDSP config, filters, FIR taps, or volume. Output
  only the contracted JSON.
"""

_PROPOSE_SYSTEM = prompt._SYSTEM_INSTRUCTIONS + """

You may additionally propose, when the evidence supports it:
- propose_correction_peq_adjustment: a bounded alternative room-
  correction filter set (freq_hz/q/gain_db), within the active strategy
  caps in the packet. JTS discloses simulated ringing and predicted worsening;
  simulation does not veto a valid experiment or establish a measured result.
  Applying requires user confirmation. Cuts-only is the default.
- propose_target_move: a bounded suggestion to move the shared
  house-curve target (a named target id, or a warmth value in range).
  Taste, not correction — pair it with a question. It is surfaced as a
  suggestion only; the household changes the target themselves in the
  correction flow. JTS never applies it automatically.

Cite measured numbers from the packet with their meaning and units. Proposed
filter values are hypotheses within the bounds, not measured facts. Keep
predictions, observations, and taste separate; a numeric overlap check cannot
verify that a claim has the right source, units, or meaning.
"""


def _residual_summary(
    measured: Any,
    target: Any,
) -> dict[str, Any]:
    """A downsampled, quantized target minus measured residual (delta-first).

    The model sees the DEVIATION from target rather than two curves it must
    subtract. Derived, quantized, <=9 points, no raw audio.
    """
    m = _pairs(measured)
    t = _pairs(target)
    if not m or not t:
        return {"available": False}
    # Align target onto the measured grid by index (both are the analysis log
    # grid in practice, same length).
    if len(m) != len(t):
        return {"available": False}
    lo, hi = _RESIDUAL_BAND_HZ
    residual = {
        "freqs_hz": [f for (f, _), _ in zip(m, t, strict=False) if lo <= f <= hi],
        "magnitude_db": [
            round(mv - tv, 3)
            for (f, mv), (_, tv) in zip(m, t, strict=False)
            if lo <= f <= hi
        ],
    }
    summary = _curve_summary(residual)
    summary["band_hz"] = [lo, hi]
    summary["meaning"] = "measured minus target; positive = too loud vs target"
    return summary


def _pairs(curve: Any) -> list[tuple[float, float]]:
    values = curve_values(curve)
    if values is None:
        return []
    freqs, mags = values
    if not isinstance(freqs, (list, tuple)) or not isinstance(mags, (list, tuple)):
        return []
    out: list[tuple[float, float]] = []
    for f, m in zip(freqs, mags, strict=False):
        try:
            out.append((float(f), float(m)))
        except (TypeError, ValueError):
            continue
    return out


def _curve_as_dict(curve: Any) -> dict[str, Any] | None:
    values = curve_values(curve)
    if values is None:
        return None
    freqs, mags = values
    return {"freqs_hz": list(freqs), "magnitude_db": list(mags)}


def _strategy_bounds(session: Any) -> dict[str, Any]:
    """The active session's correction-strategy caps as a plain dict.

    ``resolve_correction_strategy`` falls back to the default strategy for an
    unknown id and never raises, so this always returns a real cap set.
    """
    strat = _strategy.resolve_correction_strategy(
        getattr(session, "strategy_choice", None)
        or _strategy.DEFAULT_CORRECTION_STRATEGY_ID
    )
    return strat.to_dict()


def build_correction_advisor_context(session: Any) -> dict[str, Any]:
    """Build the redacted, server-data-only packet the tuning LLM sees.

    A curated, quantized VIEW of what the measurement pipeline already
    computed, never a recomputation: no raw audio, no device identifiers, no
    absolute paths.
    """
    design = getattr(session, "design_report", None) or {}
    confidence = getattr(session, "confidence_report", None) or {}
    acceptance = getattr(session, "acceptance", None)
    crossover = design.get("crossover_region")
    variance_cap_block = design.get("spatial_variance_cap")

    measured = getattr(session, "measured_curve", None)
    target = getattr(session, "target_curve", None)
    predicted = getattr(session, "predicted_curve", None)
    verify_before_after = getattr(session, "verify_before_after", None)

    return {
        "artifact_schema_version": CONTEXT_SCHEMA_VERSION,
        "kind": "jts_correction_advisor_context",
        "privacy": {
            "raw_audio_excluded": True,
            "device_identifiers_excluded": True,
            "absolute_paths_excluded": True,
            "numerics_quantized": True,
        },
        "session": {
            "state": getattr(getattr(session, "state", None), "value", None),
            "target_choice": getattr(session, "target_choice", None),
            "strategy_choice": getattr(session, "strategy_choice", None),
            "positions_measured": getattr(session, "current_position", None),
            "total_positions": getattr(session, "total_positions", None),
        },
        "curves": {
            "measured_summary": _curve_summary(_curve_as_dict(measured) or {}),
            "target_summary": _curve_summary(_curve_as_dict(target) or {}),
            "predicted_summary": _curve_summary(_curve_as_dict(predicted) or {}),
            "residual_summary": _residual_summary(measured, target),
        },
        "detected_modes": _detected_modes(design),
        "correction": {
            "strategy_bounds": _strategy_bounds(session),
            "predicted_metrics": _predicted_metrics(design),
            "filter_count": len(getattr(session, "peqs", []) or []),
            "crossover_region": _crossover_summary(crossover),
            "spatial_variance_cap": _variance_cap_summary(variance_cap_block),
        },
        "acceptance": acceptance,
        "verify_before_after": _verify_summary(verify_before_after),
        "confidence": _confidence_findings(confidence),
    }


def _detected_modes(design: dict[str, Any]) -> dict[str, Any]:
    dom = design.get("dominant_residuals") or {}
    return {
        "band_hz": design.get("band_hz"),
        "peaks": [
            {
                "freq_hz": round(float(p.get("freq_hz", 0.0)), 1),
                "residual_db": round(float(p.get("residual_db", 0.0)), 2),
            }
            for p in (dom.get("peaks") or [])
            if isinstance(p, dict)
        ],
        "nulls": [
            {
                "freq_hz": round(float(n.get("freq_hz", 0.0)), 1),
                "residual_db": round(float(n.get("residual_db", 0.0)), 2),
            }
            for n in (dom.get("nulls") or [])
            if isinstance(n, dict)
        ],
    }


def _predicted_metrics(design: dict[str, Any]) -> dict[str, Any]:
    pred = design.get("predicted") or {}
    return {
        "predicted_rms_improvement_db": _round_opt(pred.get("rms_db")),
        "predicted_max_improvement_db": _round_opt(pred.get("max_abs_db")),
        "filter_count": pred.get("filter_count"),
        "total_positive_boost_db": _round_opt(pred.get("total_positive_boost_db")),
        "note": "PREDICTED from the filter model, not a re-measurement.",
    }


def _crossover_summary(crossover: Any) -> dict[str, Any] | None:
    if not isinstance(crossover, dict):
        return None
    return {
        "corner_hz": _round_opt(crossover.get("corner_hz"), 1),
        "no_boost_band_hz": [
            _round_opt(x, 1) for x in (crossover.get("no_boost_band_hz") or [])
        ],
        "excluded_boost_count": len(crossover.get("excluded_boosts") or []),
    }


def _variance_cap_summary(cap: Any) -> dict[str, Any] | None:
    """The cross-position depth cap, quantized for the tuning model.

    Load-bearing rather than decorative: without it the packet shows residual
    error the design did not correct and no reason for it, and "correct harder"
    is exactly the advice the cap exists to refuse. ``note`` keeps the two
    registers apart — bin counts describe the CEILING, while
    ``filters_depth_trimmed`` and ``max_overshoot_db`` are about shipped filters.
    """
    if not isinstance(cap, dict):
        return None
    return {
        "available": cap.get("available"),
        "reason": cap.get("reason"),
        "position_count": cap.get("position_count"),
        "n_bins": cap.get("n_bins"),
        "n_bins_capped": cap.get("n_bins_capped"),
        "n_bins_no_cut": cap.get("n_bins_no_cut"),
        "max_depth_forgone_db": _round_opt(cap.get("max_depth_forgone_db")),
        "worst_freq_hz": _round_opt(cap.get("worst_freq_hz"), 1),
        "filters_depth_trimmed": cap.get("filters_depth_trimmed"),
        "max_overshoot_db": _round_opt(cap.get("max_overshoot_db")),
        "note": (
            "n_bins_* and max_depth_forgone_db are the depth ALLOWED at these "
            "frequencies, limited by seat-to-seat spread — not a count of "
            "filters removed. filters_depth_trimmed and max_overshoot_db are "
            "measured on the filters that shipped."
        ),
    }


def _verify_summary(vba: Any) -> dict[str, Any] | None:
    if not isinstance(vba, dict):
        return None
    delta = vba.get("delta") or {}
    return {
        "band_hz": vba.get("band_hz"),
        "measured_rms_delta_db": _round_opt(delta.get("rms_db")),
        "measured_max_delta_db": _round_opt(delta.get("max_db")),
        "note": "MEASURED before/after from the verify sweep (real, not predicted).",
    }


def _confidence_findings(confidence: dict[str, Any]) -> list[dict[str, Any]]:
    findings = confidence.get("findings") or []
    out: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        out.append({
            "code": finding.get("code"),
            "severity": finding.get("severity"),
            "message": finding.get("message"),
        })
    return out


def _round_opt(value: Any, digits: int = 2) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None



def _packet_numbers(context: dict[str, Any]) -> set[float]:
    """Rounded scalar values, without field or unit identity."""
    numbers: set[float] = set()

    def _walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            numbers.add(round(float(value), 1))
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                _walk(v)

    _walk(context)
    return numbers


_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_UNIT_RE = re.compile(r"\s*k?(?:dB|Hz)\b", re.IGNORECASE)


def check_number_provenance(
    text: str,
    context: dict[str, Any],
    *,
    tolerance: float = 0.5,
) -> dict[str, Any]:
    """Flag numbers absent from the packet, without verifying their claims.

    Matching ignores source fields, units, and meaning. Small integers without
    units are exempt. Legacy ``ok``/``unverified`` fields describe only this
    overlap heuristic; they confer no measurement or apply authority.
    """
    allowed = _packet_numbers(context)
    unverified: list[float] = []
    source = text or ""
    for match in _NUMBER_RE.finditer(source):
        try:
            value = float(match.group(0))
        except ValueError:
            continue
        rounded = round(value, 1)
        # Exempt small counts and ordinals, but a unit suffix makes it a
        # measurement claim and never exempt.
        has_unit = bool(_UNIT_RE.match(source, match.end()))
        if not has_unit and abs(value) <= 30 and float(value).is_integer():
            continue
        if any(abs(rounded - a) <= tolerance for a in allowed):
            continue
        unverified.append(value)
    return {
        "kind": "number_overlap",
        "verifies_claims": False,
        "ok": not unverified,
        "unverified": unverified,
    }



def _model_kwargs(environ: "dict[str, str] | None"):
    """Resolve (api_key, default_model) for the tuning call, or raise
    :class:`model_client.AdvisorModelError` when no key is configured."""
    api_key = key_provisioning.read_openai_key(environ=environ)
    if not api_key:
        raise model_client.AdvisorModelError(key_provisioning.NO_KEY_NUDGE)
    return api_key, key_provisioning.resolve_tuning_model(environ=environ)


def interpret(
    session: Any,
    *,
    user_message: str | None = None,
    environ: "dict[str, str] | None" = None,
    transport: "model_client.Transport | None" = None,
    timeout_sec: float | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Read-only "explain my room" narration. One paid call.

    Read-only is a property of THIS function's return, not of the validator:
    only the narration is surfaced, there is no ``proposals`` key and no apply
    route. :func:`propose` is the route that reviews and offers actions.
    """
    context = build_correction_advisor_context(session)
    package = prompt.build_advisor_prompt_package(
        _advisor_packet_for_model(context),
        user_message=user_message
        or "Explain what my room measurement shows, in plain language.",
    )
    package["messages"][0]["content"] = _INTERPRET_SYSTEM
    api_key, model = _model_kwargs(environ)
    call = model_client.call_advisor(
        package,
        environ=environ,
        transport=transport,
        api_key=api_key,
        default_model=model,
        timeout_sec=timeout_sec,
        max_output_tokens=max_output_tokens,
    )
    advisor = call.get("advisor_response") or {}
    validation = response.validate_advisor_response(
        advisor,
        advisor_context=_advisor_packet_for_model(context),
    )
    narration = _narration_text(advisor)
    provenance = check_number_provenance(narration, context)
    log_event(
        logger,
        "correction_advisor.interpret",
        provenance_ok=provenance["ok"],
        unverified_numbers=len(provenance["unverified"]),
        input_tokens=(call.get("usage") or {}).get("input_tokens"),
        output_tokens=(call.get("usage") or {}).get("output_tokens"),
    )
    return {
        "artifact_schema_version": 1,
        "kind": INTERPRET_KIND,
        "explanation": narration,
        "summary": advisor.get("summary"),
        "recommended_next_action": advisor.get("recommended_next_action"),
        "validation_accepted": validation["accepted"],
        "provenance": provenance,
        "usage": call.get("usage") or {},
        "side_effects": ["provider_api_call"],
    }


def propose(
    session: Any,
    *,
    user_message: str | None = None,
    environ: "dict[str, str] | None" = None,
    transport: "model_client.Transport | None" = None,
    timeout_sec: float | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """The confirm-gated proposer. One paid call.

    Every correction proposal is validated and deterministically simulated
    (:mod:`.proposal_sim`). NOTHING is applied here.
    """
    context = build_correction_advisor_context(session)
    packet = _advisor_packet_for_model(context)
    package = prompt.build_advisor_prompt_package(
        packet,
        user_message=user_message
        or "Suggest any bounded improvement to my room correction or target.",
    )
    package["messages"][0]["content"] = _PROPOSE_SYSTEM
    api_key, model = _model_kwargs(environ)
    call = model_client.call_advisor(
        package,
        environ=environ,
        transport=transport,
        api_key=api_key,
        default_model=model,
        timeout_sec=timeout_sec,
        max_output_tokens=max_output_tokens,
    )
    advisor = call.get("advisor_response") or {}
    validation = response.validate_advisor_response(advisor, advisor_context=packet)
    reviewed = _review_actions(session, validation)
    narration = _narration_text(advisor)
    provenance = check_number_provenance(narration, context)
    log_event(
        logger,
        "correction_advisor.propose",
        validation_accepted=validation["accepted"],
        proposals=len(reviewed),
        applicable=sum(1 for r in reviewed if r.get("applicable")),
        provenance_ok=provenance["ok"],
        input_tokens=(call.get("usage") or {}).get("input_tokens"),
        output_tokens=(call.get("usage") or {}).get("output_tokens"),
    )
    return {
        "artifact_schema_version": 1,
        "kind": PROPOSE_KIND,
        "explanation": narration,
        "summary": advisor.get("summary"),
        "validation_accepted": validation["accepted"],
        "validation_issues": validation.get("issues") or [],
        "proposals": reviewed,
        "provenance": provenance,
        "usage": call.get("usage") or {},
        "side_effects": ["provider_api_call"],
    }


def _review_actions(
    session: Any,
    validation: dict[str, Any],
) -> list[dict[str, Any]]:
    """Offer valid correction proposals with simulation as disclosure."""
    reviewed: list[dict[str, Any]] = []
    for action in validation.get("validated_action_plan") or []:
        atype = action.get("type")
        if atype == response.ACTION_PROPOSE_CORRECTION_PEQ:
            reviewed.append(_review_correction_peq(session, action))
        elif atype == response.ACTION_PROPOSE_TARGET_MOVE:
            reviewed.append({
                "type": atype,
                "applicable": False,
                "suggestion_only": True,
                "target_id": action.get("target_id"),
                "warmth": action.get("warmth"),
                "rationale": action.get("rationale"),
                "kind": "preference_question",
            })
        else:
            reviewed.append({
                "type": atype,
                "applicable": False,
                "note": action.get("message") or action.get("reason")
                or action.get("rationale"),
            })
    return reviewed


def _review_correction_peq(session: Any, action: dict[str, Any]) -> dict[str, Any]:
    peqs = action.get("correction_peqs") or []
    bounds = action.get("strategy_bounds") or {}
    sim = proposal_sim.simulate_correction_proposal(
        peqs,
        measured=getattr(session, "measured_curve", None),
        baseline=getattr(session, "position1_curve", None)
        or getattr(session, "measured_curve", None),
        target=getattr(session, "target_curve", None),
        max_total_boost_db=float(bounds.get("max_total_boost_db", 0.0)),
        f_high_hz=float(bounds.get("f_high_hz", 350.0)),
    )
    return {
        "type": response.ACTION_PROPOSE_CORRECTION_PEQ,
        "applicable": True,
        "requires_user_confirmation": True,
        "correction_peqs": peqs,
        "rationale": action.get("rationale"),
        "simulation": sim.to_dict(),
        "kind": "room_correction",
    }


def _advisor_packet_for_model(context: dict[str, Any]) -> dict[str, Any]:
    """Carry the live strategy bounds into the validator's correction block."""
    packet = dict(context)
    # response._correction_bounds reads advisor_context["correction"]["strategy_bounds"].
    packet["correction"] = {
        **(context.get("correction") or {}),
        "strategy_bounds": (context.get("correction") or {}).get("strategy_bounds"),
    }
    return packet


def _narration_text(advisor: dict[str, Any]) -> str:
    """The summary plus any explain-action messages, concatenated.

    Clamped to ``response.TEXT_LIMIT_CHARS`` here because the narration is
    assembled from the UNvalidated model response, so the validator's own
    per-field bound does not apply.
    """
    parts: list[str] = []
    summary = advisor.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    for action in advisor.get("action_plan") or []:
        if isinstance(action, dict) and action.get("type") == response.ACTION_EXPLAIN:
            message = action.get("message")
            if isinstance(message, str) and message.strip():
                parts.append(message.strip())
    text = "\n\n".join(parts)
    limit = response.TEXT_LIMIT_CHARS
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text
