# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The P6 tuning-LLM surfaced in the ``/correction/`` flow.

Two jobs, one agent, one target (revision plan §3.4):

* **Interpret** (:func:`interpret`) — a read-only, plain-language
  "here's what your room is doing" narration of the SERVER-computed
  result: the target−measured residual, detected modes, the P4
  acceptance verdict + numbers, the P5 crossover annotation, and the
  confidence findings. Correction claims cite the measured/verified
  numbers; the model authors no number a tool computed (enforced by
  :func:`check_number_provenance`). This is the *correction* loop:
  claims are VERIFIED by re-measure.

* **Propose** (:func:`propose`) — the confirm-gated proposer. The model
  proposes a bounded correction filter set and/or a target move; every
  correction proposal is validated (:mod:`.response`), then SIMULATED
  and rejected-if-it-would-ring (:mod:`.proposal_sim`) before it is ever
  offered for apply, and judged by the same P4 acceptance evaluator any
  correction faces. Preference/taste suggestions are phrased as
  questions. This is the *preference* loop where taste is subjective.

The packet is built from a live :class:`jasper.correction.session.MeasurementSession`
via :func:`build_correction_advisor_context`, reusing the redaction
discipline of :mod:`.advisor_context` (derived curves/summaries only,
NEVER raw audio, quantized numerics, no device identifiers). The paid
call reuses the household's OpenAI key from the ``jasper-secrets``
compartment (:mod:`.key_provisioning`).

Deterministic JTS code stays the DSP authority: this module NEVER writes
CamillaDSP. The endpoint layer routes an approved-and-confirmed proposal
through the session's existing apply path.
"""
from __future__ import annotations

import logging
from typing import Any

from jasper.log_event import log_event

from . import key_provisioning, model_client, prompt, response
from . import proposal_sim
from .advisor_context import _curve_summary

logger = logging.getLogger(__name__)

CONTEXT_SCHEMA_VERSION = 1
INTERPRET_KIND = "jts_correction_interpret"
PROPOSE_KIND = "jts_correction_proposal_review"

# Bass band the residual + modes are summarized over (matches the
# correction design band top).
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
  caps in the packet. JTS will SIMULATE it and reject it if it would ring
  or make the room measurably worse, then require the user to confirm
  before applying. Cuts-only is the default. Propose filter VALUES only.
- propose_target_move: a bounded move of the shared house-curve target
  (a named target id, or a warmth value in range). Taste, not
  correction — pair it with a question.

Every number you state MUST come from the evidence packet. Never author
a frequency, dB, Q, or verdict a tool computed.
"""


def _residual_summary(
    measured: Any,
    target: Any,
) -> dict[str, Any]:
    """A downsampled, quantized target−measured residual (delta-first).

    Reuses the :mod:`.advisor_context` curve downsampler on the residual
    so the model sees the DEVIATION from target (what correction acts on)
    rather than two curves it must subtract. Redaction-safe: derived,
    quantized, ≤9 points, no raw audio.
    """
    m = _pairs(measured)
    t = _pairs(target)
    if not m or not t:
        return {"available": False}
    # Align target onto the measured grid by nearest index (both are the
    # analysis log grid in practice, same length).
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
    freqs = getattr(curve, "freqs_hz", None)
    mags = getattr(curve, "magnitude_db", None)
    if freqs is None and isinstance(curve, dict):
        freqs = curve.get("freqs_hz")
        mags = curve.get("magnitude_db")
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
    freqs = getattr(curve, "freqs_hz", None)
    mags = getattr(curve, "magnitude_db", None)
    if freqs is None and isinstance(curve, dict):
        return curve if curve.get("freqs_hz") is not None else None
    if freqs is None or mags is None:
        return None
    return {"freqs_hz": list(freqs), "magnitude_db": list(mags)}


def _strategy_bounds(session: Any) -> dict[str, Any]:
    """The active session's correction-strategy caps, as a plain dict a
    proposal is bounded by. ``resolve_correction_strategy`` itself falls
    back to the default strategy for an unknown id (it never raises), so
    this always returns a real cap set."""
    from jasper.correction import strategy as _strategy

    strat = _strategy.resolve_correction_strategy(
        getattr(session, "strategy_choice", None)
        or _strategy.DEFAULT_CORRECTION_STRATEGY_ID
    )
    return strat.to_dict()


def build_correction_advisor_context(session: Any) -> dict[str, Any]:
    """Build the redacted, server-data-only packet the tuning LLM sees.

    Everything here is already computed by the measurement pipeline — the
    packet is a curated, quantized VIEW, not a recomputation. It contains
    NO raw audio, NO device identifiers, NO absolute paths: only derived
    curves/summaries, the design report's residual + modes, the P4
    acceptance verdict, the P5 crossover annotation, and confidence
    findings.
    """
    design = getattr(session, "design_report", None) or {}
    confidence = getattr(session, "confidence_report", None) or {}
    acceptance = getattr(session, "acceptance", None)
    crossover = design.get("crossover_region")

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
            # The delta-first representation the model reasons about.
            "residual_summary": _residual_summary(measured, target),
        },
        "detected_modes": _detected_modes(design),
        "correction": {
            "strategy_bounds": _strategy_bounds(session),
            "predicted_metrics": _predicted_metrics(design),
            "filter_count": len(getattr(session, "peqs", []) or []),
            "crossover_region": _crossover_summary(crossover),
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


# ---------------------------------------------------------------------
# Number provenance — the LLM never authors a number a tool computed
# ---------------------------------------------------------------------

def _packet_numbers(context: dict[str, Any]) -> set[float]:
    """Every numeric fact in the packet the model is allowed to cite.

    A user-facing number in the model's prose must round-match one of
    these (see :func:`check_number_provenance`)."""
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


_NUMBER_RE = None


def _number_regex():
    global _NUMBER_RE
    if _NUMBER_RE is None:
        import re

        # A signed decimal, optionally with a unit suffix we ignore.
        _NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
    return _NUMBER_RE


def check_number_provenance(
    text: str,
    context: dict[str, Any],
    *,
    tolerance: float = 0.5,
) -> dict[str, Any]:
    """Verify user-facing numerics in ``text`` trace to the packet.

    The provenance guard (revision plan §3.4): the LLM narrates
    correction facts, but it may not author a number a deterministic tool
    computed. We extract decimals from the model's prose and flag any that
    do not round-match (within ``tolerance``) a number present in the
    evidence packet. Small integers (0..~30, counts / small ordinals like
    "a few dB", "2 positions") are exempt — they are ordinary prose, not
    claimed measurement facts.

    Returns ``{ok, unverified: [floats]}``; ``ok`` is True when no
    unverified measurement-scale number appears. This is advisory
    surface state for the endpoint to log/annotate — the deterministic
    apply gate does not depend on it, but a failed provenance check is a
    strong "don't trust this narration's numbers" signal.
    """
    allowed = _packet_numbers(context)
    unverified: list[float] = []
    for match in _number_regex().findall(text or ""):
        try:
            value = float(match)
        except ValueError:
            continue
        rounded = round(value, 1)
        # Exempt small counts / ordinals that are ordinary prose.
        if abs(value) <= 30 and float(value).is_integer():
            continue
        if any(abs(rounded - a) <= tolerance for a in allowed):
            continue
        unverified.append(value)
    return {"ok": not unverified, "unverified": unverified}


# ---------------------------------------------------------------------
# LLM calls — interpret (read-only) + propose (confirm-gated)
# ---------------------------------------------------------------------

def _model_kwargs(environ: "dict[str, str] | None"):
    """Resolve (api_key, default_model) for the tuning call, or raise
    :class:`model_client.AdvisorModelError` when no key is configured."""
    api_key = key_provisioning.read_openai_key(environ=environ)
    if not api_key:
        raise model_client.AdvisorModelError(
            "no OpenAI key configured — add one at /voice to enable the "
            "tuning assistant"
        )
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

    Builds the correction packet, asks the model for a plain-language
    explanation (contracted JSON), validates it against the deterministic
    response contract (so it cannot smuggle an action), and runs the
    provenance check on the summary/message text.
    """
    context = build_correction_advisor_context(session)
    package = prompt.build_advisor_prompt_package(
        _advisor_packet_for_model(context, allow_actions=False),
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
        advisor_context=_advisor_packet_for_model(context, allow_actions=False),
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

    The model may propose bounded correction / target moves; every
    correction proposal is validated + deterministically simulated
    (:mod:`.proposal_sim`) and judged by P4's acceptance evaluator.
    NOTHING is applied here — proposals that survive are returned with
    their simulation verdict for the endpoint to surface for user
    confirmation.
    """
    context = build_correction_advisor_context(session)
    packet = _advisor_packet_for_model(context, allow_actions=True)
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
    reviewed = _review_actions(session, context, validation)
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
    context: dict[str, Any],
    validation: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turn each validated action into a user-facing proposal card.

    Correction PEQ proposals get deterministically simulated + judged;
    only a simulate-accepted one is marked ``applicable`` (offer the
    confirm-apply). Target moves are marked applicable (bounded taste,
    user confirms). Preference/explain/remeasure actions pass through as
    read-only notes.
    """
    reviewed: list[dict[str, Any]] = []
    for action in validation.get("validated_action_plan") or []:
        atype = action.get("type")
        if atype == response.ACTION_PROPOSE_CORRECTION_PEQ:
            reviewed.append(_review_correction_peq(session, action))
        elif atype == response.ACTION_PROPOSE_TARGET_MOVE:
            reviewed.append({
                "type": atype,
                "applicable": True,
                "requires_user_confirmation": True,
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
        "applicable": sim.accepted,
        "requires_user_confirmation": True,
        "correction_peqs": peqs,
        "rationale": action.get("rationale"),
        "simulation": sim.to_dict(),
        "kind": "room_correction",
    }


def _advisor_packet_for_model(
    context: dict[str, Any],
    *,
    allow_actions: bool,
) -> dict[str, Any]:
    """Fold the correction context into the shape the response validator +
    prompt builder expect: it carries an ``advisor_policy.allowed_actions``
    list (so :func:`response._policy_allows` permits the correction/target
    proposals) plus a ``correction`` block with the live strategy bounds.
    """
    strategy_bounds = (context.get("correction") or {}).get("strategy_bounds")
    allowed = [
        {"id": "explain", "allowed": True, "reasons": []},
        {"id": "recommend_remeasure", "allowed": True, "reasons": []},
    ]
    if allow_actions:
        allowed.extend([
            {
                "id": "propose_correction_peq_adjustment",
                "allowed": True,
                "reasons": [],
            },
            {"id": "propose_target_move", "allowed": True, "reasons": []},
            {"id": "propose_preference_eq_audition", "allowed": True, "reasons": []},
        ])
    packet = dict(context)
    packet["advisor_policy"] = {
        "mode": "correction_flow_bounded_actions",
        "allowed_actions": allowed,
    }
    # response._correction_bounds reads advisor_context["correction"]["strategy_bounds"].
    packet["correction"] = {
        **(context.get("correction") or {}),
        "strategy_bounds": strategy_bounds,
    }
    return packet


def _narration_text(advisor: dict[str, Any]) -> str:
    """The plain-language text the panel renders: the summary plus any
    explain-action messages, concatenated."""
    parts: list[str] = []
    summary = advisor.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    for action in advisor.get("action_plan") or []:
        if isinstance(action, dict) and action.get("type") == response.ACTION_EXPLAIN:
            message = action.get("message")
            if isinstance(message, str) and message.strip():
                parts.append(message.strip())
    return "\n\n".join(parts)
